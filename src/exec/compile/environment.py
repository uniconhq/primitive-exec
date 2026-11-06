"""Assemble a locked toolchain once, into a read-only folder of the
machine's cache made of hard links into its store of unpacked packages."""

import asyncio
import contextlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import rattler
from rattler.exceptions import (
    CacheDirError,
    ConvertSubdirError,
    ExtractError,
    InstallerError,
    IoError,
    LinkError,
    ParseSubdirError,
    TransactionError,
)

from exec.compile import cache
from exec.shared import PrimitiveError

DIGEST = re.compile(r"^[0-9a-f]{64}$")
RETRY_SECONDS = 0.1
"""How long a step that found the folder not ready, and another step
holding its lock, waits before it looks again."""
PREFIX = "/env"
"""The prefix every box sees a toolchain at, written into what is
installed."""
ENV = Path(PREFIX)
"""Where a folder being assembled is reached for its build: a link made in
exec's own container, so both installers write `/env` into what they
install and still link from the store, which a bind mount, a mount of its
own, would turn into copies."""
UV = Path("/usr/local/lib/unicon-exec/bin/uv")
WHEEL_NAME = re.compile(r"^[A-Za-z0-9_.+!-]+\.whl$")
JAR_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.+-]*\.jar$")
NPM_NAME = r"(?:@[A-Za-z0-9_~-][A-Za-z0-9_.~-]*/)?[A-Za-z0-9_~-][A-Za-z0-9_.~-]*"
NPM_PATH = re.compile(rf"^node_modules/{NPM_NAME}(?:/node_modules/{NPM_NAME})*$")
"""Where npm's lock places a package: under `node_modules/`, or under
another package's own `node_modules/`, a name never `.` or `..`."""
CONDA_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
JAVA_FOLDER = PurePosixPath("lib/java")
"""Where a folder holds the locked Maven jars, which every JVM's class path
names."""
JAVA_LIBRARIES = f"{PREFIX}/{JAVA_FOLDER}"
"""Where a box sees the locked Maven jars."""
NPM_FOLDER = PurePosixPath("lib")
"""What a locked npm package's place in the lock, `node_modules/...`, is
under in a folder."""
STORED_JAR = "file.jar"
"""A Maven jar's one file in its store entry, `files/<sha256>/`."""
NPM_UNPACKED = 300 * 1024 * 1024
"""The most an npm package's files may take unpacked, which a tarball of
the lock's own size and SHA-256 could still pass."""
UV_FILES = frozenset(
    {"INSTALLER", "REQUESTED", "direct_url.json", "uv_cache.json", "RECORD"}
)
"""What uv writes into each `dist-info` folder itself, rather than links."""
RATTLER_FILES = frozenset({"CACHEDIR.TAG"})
"""What rattler writes at the top of an environment itself, beside
`conda-meta/`."""
LINKS = rattler.Config.from_toml("allow-ref-links = false\nallow-hard-links = true\n")
"""rattler links each file from the store: a copy-on-write copy, which it
makes first where a filesystem has them, would be a file of the folder's
own."""


@dataclass(frozen=True)
class Environment:
    folder: Path
    fingerprint: str


@dataclass(frozen=True)
class Lock:
    """One language's part of the step's environment lock, its shape
    checked: what the folder is named by, the platform its conda packages
    are for, each piece's file by its SHA-256, and the packages."""

    fingerprint: str
    platform: str
    pieces: dict[str, str]
    conda: list[dict[str, Any]]
    pypi: list[dict[str, Any]] = field(default_factory=list)
    maven: list[dict[str, Any]] = field(default_factory=list)
    npm: list[dict[str, Any]] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)
    """The conda packages the task asked for by name beside the
    toolchain, which a C or C++ program is linked against."""


CONDA_FIELDS = {"sha256": str, "file_name": str, "size": int, "record": dict}
PYPI_FIELDS = {"sha256": str, "file_name": str, "size": int}
MAVEN_FIELDS = {"sha256": str, "file_name": str, "size": int}
NPM_FIELDS = {"sha256": str, "path": str, "size": int}
RATTLER_ERRORS = (
    InstallerError,
    LinkError,
    IoError,
    ExtractError,
    TransactionError,
    ConvertSubdirError,
    ParseSubdirError,
    CacheDirError,
)
"""What rattler raises for packages it cannot read or install, none of
them sharing a base but `Exception`."""


def read_lock(lock_file: Path, language: str) -> Lock:
    """The part of the lock at `lock_file` for `language`, or a step fault
    naming what is wrong with it."""
    unreadable = PrimitiveError("the environment lock could not be read")
    try:
        document = json.loads(lock_file.read_bytes())
    except (OSError, ValueError) as error:
        raise unreadable from error
    if not isinstance(document, dict) or not isinstance(
        document.get("languages"), dict
    ):
        raise unreadable
    entry = document["languages"].get("python" if language == "ipynb" else language)
    if entry is None:
        raise PrimitiveError(f"the environment does not lock {language}")
    pieces = document.get("pieces")
    if not (
        document.get("version") == 3
        and document.get("prefix") == "/env"
        and isinstance(entry, dict)
        and isinstance(entry.get("fingerprint"), str)
        and DIGEST.fullmatch(entry["fingerprint"])
        and isinstance(pieces, dict)
        and all(isinstance(name, str) for name in [*pieces, *pieces.values()])
        and shaped(entry.get("conda", []), CONDA_FIELDS)
        and shaped(entry.get("pypi", []), PYPI_FIELDS)
        and shaped(entry.get("maven", []), MAVEN_FIELDS)
        and shaped(entry.get("npm", []), NPM_FIELDS)
        and isinstance(entry.get("asked", {}), dict)
        and isinstance(asked := entry.get("asked", {}).get("conda", []), list)
        and all(isinstance(name, str) for name in asked)
    ):
        raise unreadable
    platform = document.get("platform")
    unknown = PrimitiveError(
        f"the environment lock names no platform conda knows: {platform!r}"
    )
    if not isinstance(platform, str):
        raise unknown
    try:
        rattler.Subdir(platform)
    except (TypeError, ParseSubdirError) as error:
        raise unknown from error
    return Lock(
        entry["fingerprint"],
        platform,
        pieces,
        entry.get("conda", []),
        entry.get("pypi", []),
        entry.get("maven", []),
        entry.get("npm", []),
        [found[0] for name in asked if (found := CONDA_NAME.match(name))],
    )


def shaped(value: object, fields: dict[str, type]) -> bool:
    """Whether `value` is a list of objects each holding `fields`, each of
    its type, a size never a boolean."""
    return isinstance(value, list) and all(
        isinstance(record, dict)
        and all(
            isinstance(record.get(name), kind)
            and not isinstance(record.get(name), bool)
            for name, kind in fields.items()
        )
        for record in value
    )


class Toolchain:
    """A language's locked toolchain for the batch: the fingerprint its
    compiles are keyed by, read from the lock, and its folder, assembled
    the first time a compile or a run needs it (`assembled`) and held until
    the batch's `stack` closes. A compile the machine's cache holds, of a
    program whose box needs no `/env`, never assembles it."""

    def __init__(self, root: Path, lock: Lock, stack: contextlib.ExitStack) -> None:
        self.root = root
        self.lock = lock
        self.stack = stack
        self.environment: Environment | None = None

    @property
    def fingerprint(self) -> str:
        """The fingerprint of the locked toolchain."""
        return self.lock.fingerprint

    def folder(self) -> Path:
        """The assembled folder, assembled now if it is not yet."""
        if self.environment is None:
            self.environment = self.stack.enter_context(assembled(self.root, self.lock))
        return self.environment.folder


@contextlib.contextmanager
def assembled(root: Path, lock: Lock) -> Iterator[Environment]:
    """The folder of `lock`'s toolchain, assembled now if the machine's
    cache has none ready, held under its lock's shared side for the block.
    A folder is named by the lock's fingerprint and the image's toolchain
    line together, `envs/<fingerprint>-<toolchain>`, so each exec has
    folders of its own and none waits on or assembles another's."""
    fingerprint = lock.fingerprint
    if not cache.in_use():
        raise PrimitiveError("the environment needs the machine's /cache volume")
    tools = cache.toolchain()
    if not DIGEST.fullmatch(tools):
        raise PrimitiveError(
            f"the image's toolchain line {tools!r} is not a SHA-256, so it "
            "cannot name an environment's folder"
        )
    name = f"{fingerprint}-{tools}"
    folder = cache.CACHE / "envs" / name
    folder.parent.mkdir(exist_ok=True)
    built = False
    while True:
        with cache.locked(name, kind="envs", shared=True):
            if ready(folder, fingerprint, tools):
                folder.chmod(0o755)
                os.utime(folder / "ready.json")
                verb = "assembled" if built else "reused"
                print(
                    f"exec: {verb} environment {fingerprint} at /env", file=sys.stderr
                )
                yield Environment(folder, fingerprint)
                return
        # A step waiting for the exclusive lock would wait behind every
        # step that takes the shared one after it, as flock grants them
        # first; one that cannot take it at once looks again instead, and
        # its shared lock waits behind a step that is assembling the folder.
        with contextlib.ExitStack() as exclusive:
            try:
                exclusive.enter_context(cache.locked(name, kind="envs", wait=False))
            except BlockingIOError:
                time.sleep(RETRY_SECONDS)
                continue
            if not ready(folder, fingerprint, tools):
                construct(root, lock, folder, tools)
                built = True


def construct(root: Path, lock: Lock, folder: Path, tools: str) -> None:
    """Assemble `lock`'s toolchain as `folder`, out of hard links into the
    machine's store, `/cache/store`, where each package is unpacked once:
    conda packages by rattler into its cache, `store/conda`, wheels by uv
    into its own, `store/uv`, from each wheel's copy in `store/wheels`, and
    Maven jars and npm packages by exec into `store/files`.
    The folder's `ready.json` lists the store entries it links. The
    store's lock is held shared throughout, since the wrapper frees an
    entry no folder lists, and an entry unpacked here is listed nowhere
    until the folder is whole."""
    if not lock.conda:
        raise PrimitiveError("the environment has no locked toolchain")
    try:
        with cache.store_locked():
            temporary = Path(
                tempfile.mkdtemp(prefix=f"{folder.name}.", dir=folder.parent)
            )
            try:
                build(root, lock, temporary)
                placed = place_files(root, lock, temporary)
                entries = sorted({*checked(temporary, lock), *placed})
                seal(temporary)
                (temporary / "ready.json").write_text(
                    json.dumps(marker(lock.fingerprint, tools) | {"store": entries})
                )
                # A folder under the name that is not ready, which no exec
                # leaves, is moved aside whole before it is removed, so its
                # name never holds a folder half gone; a leftover, under the
                # name of a folder being made, is the wrapper's to sweep.
                stale = None
                if folder.exists():
                    stale = Path(
                        tempfile.mkdtemp(prefix=f"{folder.name}.", dir=folder.parent)
                    )
                    folder.replace(stale)
                temporary.rename(folder)
                if stale is not None:
                    remove(stale)
            finally:
                if temporary.exists():
                    remove(temporary)
    except OSError as error:
        raise PrimitiveError(
            f"the environment could not be assembled: {error.strerror or error}"
        ) from error


def build(root: Path, lock: Lock, folder: Path) -> None:
    """Install the lock's conda packages, then its wheels, into `folder`:
    through `ENV`, a link to it for the build, or, where that cannot be
    made, at its own path with `/env` written in its place, the launchers
    uv writes pointed at `/env` after."""
    with through_env(folder) as prefix:
        asyncio.run(install_conda(root, lock.pieces, lock.conda, prefix, lock.platform))
        if lock.pypi:
            if len(list((folder / "lib").glob("python*/site-packages"))) != 1:
                raise PrimitiveError(
                    "the locked Python has no unique site-packages folder"
                )
            install_wheels(prefix, stored_wheels(root, lock))
    if prefix != ENV:
        point_launchers(folder)


@contextlib.contextmanager
def through_env(folder: Path) -> Iterator[Path]:
    """The path to install into `folder` through: `ENV`, made a link to it
    for the block and removed after, or `folder` itself where `ENV` cannot
    be made, as when the container's root is read-only."""
    try:
        ENV.symlink_to(folder, target_is_directory=True)
    except OSError:
        yield folder
        return
    try:
        yield ENV
    finally:
        with contextlib.suppress(OSError):
            ENV.unlink()


def stored_wheels(root: Path, lock: Lock) -> list[Path]:
    """Each locked wheel at its path in the store,
    `wheels/<sha256>/<file name>`, as uv is always handed it. uv keys a
    wheel it has unpacked by its file's path and inode change time, so a
    wheel is written there once, from its verified piece, and never
    touched after, not even its mode or times, or uv would unpack it
    again; it is written again only when what is there is not the wheel
    its name says. It is a copy, not a link of the piece, which would
    share the piece's inode, whose times are its age for eviction."""
    wheels = []
    for wheel in lock.pypi:
        digest, name = wheel["sha256"], wheel["file_name"]
        if (
            not DIGEST.fullmatch(digest)
            or digest not in lock.pieces
            or not WHEEL_NAME.fullmatch(name)
        ):
            raise PrimitiveError("the environment is missing a wheel piece")
        stored = cache.CACHE / "store" / "wheels" / digest / name
        if not holds(stored, wheel["size"], digest):
            piece = root / lock.pieces[digest]
            if not holds(piece, wheel["size"], digest):
                raise PrimitiveError(
                    "an environment piece does not match its SHA-256 and size"
                )
            store_wheel(piece, stored, wheel["size"], digest)
        wheels.append(stored)
    return wheels


def holds(path: Path, size: int, digest: str) -> bool:
    """Whether `path` is a plain file of `size` bytes with SHA-256 `digest`."""
    return (
        path.is_file()
        and not path.is_symlink()
        and path.stat().st_size == size
        and cache.file_sha256(path) == digest
    )


def store_wheel(piece: Path, stored: Path, size: int, digest: str) -> None:
    """Copy the verified wheel `piece` to `stored`, written whole under a
    name of its own in the entry's folder and linked into place, so a file
    at `stored` is always whole. The store's lock is only shared, so
    another step may store the same wheel at once: the first copy linked
    is kept, and a copy there that is not this wheel is replaced. A step
    that takes the other's copy in the moment before its writer drops its
    own name for it may see it changed, and unpack it once more, which
    costs room only."""
    entry = stored.parent
    entry.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{stored.name}.", dir=entry)
    os.close(descriptor)
    temporary = Path(name)
    try:
        shutil.copyfile(piece, temporary)
        temporary.chmod(0o444)
        try:
            os.link(temporary, stored)
        except FileExistsError:
            if not holds(stored, size, digest):
                temporary.replace(stored)
    finally:
        temporary.unlink(missing_ok=True)


def install_wheels(prefix: Path, wheels: list[Path]) -> None:
    """Install `wheels` into the Python at `prefix` with uv, offline, with
    no dependencies and no configuration read, each file hard-linked from
    uv's cache in the store."""
    command = [str(UV), "pip", "install", "--offline", "--no-index", "--no-deps"]
    command += ["--no-config", "--no-progress", "--link-mode", "hardlink"]
    command += ["--cache-dir", str(cache.CACHE / "store" / "uv")]
    command += ["--python", str(prefix / "bin" / "python3")]
    try:
        done = subprocess.run(
            [*command, *map(str, wheels)],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin"},
            check=False,
        )
    except OSError as error:
        raise PrimitiveError(
            f"uv could not be started to install the locked wheels: {error.strerror}"
        ) from error
    if done.returncode != 0:
        said = [
            line.strip()
            for line in done.stderr.splitlines()
            if line.strip().startswith(("error:", "cause:"))
        ]
        raise PrimitiveError(
            "the locked wheels could not be installed: "
            + ("; ".join(said)[:500] or f"uv exited with code {done.returncode}")
        )


def point_launchers(folder: Path) -> None:
    """Point each launcher uv wrote in `folder`'s `bin/`, installed at the
    folder's own path, at `/env/bin/python3`, as uv writes it through
    `/env`: its first line names the folder's Python or, when that path is
    too long for a first line, the shell lines uv writes in its place do."""
    python = f"{folder}/bin/python3"
    starts = [
        f"#!{python}\n".encode(),
        f"#!/bin/sh\n'''exec' '{python}' \"$0\" \"$@\"\n' '''\n".encode(),
    ]
    for path in (folder / "bin").iterdir():
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            continue
        content = path.read_bytes()
        for start in starts:
            if content.startswith(start):
                path.write_bytes(
                    f"#!{PREFIX}/bin/python3\n".encode() + content[len(start) :]
                )
                break


def checked(folder: Path, lock: Lock) -> list[str]:
    """The store entries the assembled `folder` links, by their paths under
    the store, refusing a folder with a file of its own that neither tool
    writes itself. A link that cannot be made, as from one filesystem to
    another, makes both tools copy, rattler without a word, so this is what
    sees it. A file of the folder's own is one with one link: a conda
    package's file with the prefix written into it, or marked not to be
    linked, in `conda-meta/` or rattler's own beside it; and what uv
    writes, its launchers in `bin/` and its files in each `dist-info`.
    Two packages that install the same file are refused too, since the
    one installed last would win and the other's record still claim it."""
    store = cache.CACHE / "store"
    entries = {f"wheels/{wheel['sha256']}" for wheel in lock.pypi}
    own = {PurePosixPath(name) for name in RATTLER_FILES}
    installed_by: dict[PurePosixPath, str] = {}

    def claim(path: PurePosixPath, package: str) -> None:
        if path in installed_by:
            raise PrimitiveError(
                f"two locked packages, {installed_by[path]} and {package}, "
                f"install the same file {path}"
            )
        installed_by[path] = package

    for record in sorted((folder / "conda-meta").glob("*.json")):
        document = json.loads(record.read_text(encoding="utf-8"))
        unpacked = Path(document.get("extracted_package_dir", ""))
        if unpacked.parent == store / "conda":
            entries.add(f"conda/{unpacked.name}")
        for path in document.get("paths_data", {}).get("paths", []):
            claim(PurePosixPath(os.path.normpath(path["_path"])), record.stem)
            if (
                path.get("path_type") != "hardlink"
                or "prefix_placeholder" in path
                or path.get("no_link")
            ):
                own.add(PurePosixPath(path["_path"]))
    archives = store / "uv" / "archive-v0"
    for dist in sorted((folder / "lib").glob("python*/site-packages/*.dist-info")):
        if (dist / "INSTALLER").read_text(errors="replace").strip() != "uv":
            continue
        own.update(PurePosixPath(dist.relative_to(folder), name) for name in UV_FILES)
        for row in (dist / "RECORD").read_text(encoding="utf-8").splitlines():
            if not row.strip():
                continue
            installed = Path(os.path.normpath(dist.parent / row.partition(",")[0]))
            if not installed.is_relative_to(folder):
                raise PrimitiveError(
                    f"the wheel {dist.name} installs a file outside its environment"
                )
            claim(PurePosixPath(installed.relative_to(folder)), dist.name)
            if installed.is_relative_to(folder / "bin"):
                own.add(PurePosixPath(installed.relative_to(folder)))
        metadata = dist / "METADATA"
        for archive in archives.iterdir() if archives.is_dir() else []:
            with contextlib.suppress(OSError):
                if (archive / dist.name / "METADATA").samefile(metadata):
                    entries.add(f"uv/archive-v0/{archive.name}")
                    break
    for path in sorted(folder.rglob("*")):
        relative = PurePosixPath(path.relative_to(folder))
        if (
            path.is_symlink()
            or not path.is_file()
            or relative.parts[0] == "conda-meta"
            or relative in own
        ):
            continue
        if path.stat().st_nlink == 1:
            raise PrimitiveError(
                f"the environment's file {relative} was copied rather than linked "
                "from the machine's store, which must be on the cache's own "
                "filesystem"
            )
    return sorted(entries)


def seal(folder: Path) -> None:
    """Make `folder` read-only: each folder in it, and each file of its own,
    one with one link. A file linked from the store is left as the store
    has it, since a change to its mode would change it in every folder that
    links it; a box sees `/env` only through a read-only mount. A link out
    of the folder is refused."""
    for path in folder.rglob("*"):
        if path.is_symlink():
            if not path.resolve().is_relative_to(folder.resolve()):
                raise PrimitiveError(
                    "an installed package links outside its environment"
                )
            continue
        if path.is_file():
            if path.stat().st_nlink == 1:
                path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
        elif path.is_dir():
            path.chmod(0o555)


def remove(folder: Path) -> None:
    """Remove a folder exec made, whose folders it made read-only."""
    for parent, _, _ in folder.walk():
        with contextlib.suppress(OSError):
            parent.chmod(0o700)
    shutil.rmtree(folder, ignore_errors=True)


def marker(fingerprint: str, tools: str) -> dict[str, str]:
    """What a whole folder's `ready.json` holds besides the store entries it
    lists: the fingerprint it was assembled for, its prefix, and the
    toolchain line of the image that assembled it, as its name says."""
    return {"fingerprint": fingerprint, "prefix": PREFIX, "toolchain": tools}


def ready(folder: Path, fingerprint: str, tools: str) -> bool:
    wanted = marker(fingerprint, tools)
    try:
        document = json.loads((folder / "ready.json").read_text())
    except OSError, ValueError:
        return False
    return isinstance(document, dict) and all(
        document.get(key) == value for key, value in wanted.items()
    )


async def install_conda(
    root: Path,
    pieces: dict[str, str],
    records: list[dict[str, Any]],
    target: Path,
    platform: str,
) -> None:
    try:
        await install_records(root, pieces, records, target, platform)
    except (OSError, ValueError, TypeError, *RATTLER_ERRORS) as error:
        raise PrimitiveError(
            "the locked conda environment could not be installed"
        ) from error


async def install_records(
    root: Path,
    pieces: dict[str, str],
    records: list[dict[str, Any]],
    target: Path,
    platform: str,
) -> None:
    """Install the verified conda `records` at `target` from rattler's cache
    in the store, `/env` written into each, with no link script run. Each
    record's metadata goes in a folder of its own outside `/work`, since
    rattler reads it from a file."""
    with tempfile.TemporaryDirectory(prefix="conda-") as work:
        metadata_folder = Path(work)
        prepared = []
        for record in records:
            digest, filename = str(record["sha256"]), str(record["file_name"])
            if (
                not DIGEST.fullmatch(digest)
                or digest not in pieces
                or Path(filename).name != filename
                or "\\" in filename
            ):
                raise PrimitiveError("the environment is missing a safe conda piece")
            piece = root / pieces[digest]
            if (
                not piece.is_file()
                or piece.stat().st_size != record["size"]
                or cache.file_sha256(piece) != digest
            ):
                raise PrimitiveError(
                    "a conda piece does not match its SHA-256 and size"
                )
            named = piece.with_name(filename)
            if named.exists() and cache.file_sha256(named) != digest:
                raise PrimitiveError("two conda pieces have the same file name")
            if not named.exists():
                os.link(piece, named)
            metadata = metadata_folder / (digest + ".json")
            metadata.write_text(json.dumps(record["record"]))
            package = rattler.PackageRecord.from_index_json(
                metadata, size=int(record["size"]), sha256=digest
            )
            prepared.append(
                rattler.RepoDataRecord(
                    package,
                    filename,
                    named.resolve().as_uri(),
                    "https://conda.anaconda.org/conda-forge",
                )
            )
        await rattler.install(
            prepared,
            target_prefix=target,
            alternative_target_prefix=PREFIX,
            platform=rattler.Subdir(platform),
            execute_link_scripts=False,
            show_progress=False,
            cache_dir=cache.CACHE / "store" / "conda",
            config=LINKS,
        )


def tool(folder: Path, name: str) -> str:
    found = folder / "bin" / name
    resolved = found.resolve()
    if not resolved.is_relative_to(folder.resolve()) or not resolved.is_file():
        raise PrimitiveError(f"the locked environment has no tool {name}")
    return "/env/" + str(found.relative_to(folder))


def place_files(root: Path, lock: Lock, folder: Path) -> list[str]:
    """Place the lock's Maven jars in `folder`'s `JAVA_FOLDER`, by their
    file names, and its npm packages at their places in the lock under
    `NPM_FOLDER`, each a hard link of its store entry's files, unpacked
    once into `store/files/<sha256>/`; return those entries. A file placed
    where a conda package or another placed one has a file already is two
    packages installing the same file, refused."""
    entries = []
    for jar in lock.maven:
        name = jar["file_name"]
        if not JAR_NAME.fullmatch(name):
            raise PrimitiveError(
                f"the locked jar name {name!r} is not a plain jar name"
            )
        entry = stored_file(root, lock, jar, store_jar)
        target = folder / JAVA_FOLDER
        target.mkdir(parents=True, exist_ok=True)
        link_into(entry / STORED_JAR, target / name, name)
        entries.append(f"files/{jar['sha256']}")
    for package in sorted(lock.npm, key=lambda each: each["path"]):
        place = package["path"]
        if not NPM_PATH.fullmatch(place) or {".", ".."} & set(place.split("/")):
            raise PrimitiveError(
                f"the locked npm package's place {place!r} is not safe"
            )
        entry = stored_file(root, lock, package, store_npm)
        target = folder / NPM_FOLDER / place
        for directory, _, names in entry.walk():
            within = target / directory.relative_to(entry)
            within.mkdir(parents=True, exist_ok=True)
            for name in names:
                link_into(directory / name, within / name, place)
        entries.append(f"files/{package['sha256']}")
    return entries


def link_into(stored: Path, target: Path, package: str) -> None:
    """Hard-link a store entry's file into a folder being assembled."""
    try:
        os.link(stored, target)
    except FileExistsError:
        raise PrimitiveError(
            f"two locked packages, {package} among them, install the same file "
            f"{target.name}"
        ) from None


def stored_file(
    root: Path,
    lock: Lock,
    piece: dict[str, Any],
    unpack: Callable[[Path, Path], None],
) -> Path:
    """The store entry of a Maven or npm `piece`, `store/files/<sha256>/`,
    unpacked by `unpack` from its verified piece into a folder of its own
    under a hidden name, `.<sha256>.<random>`, which the wrapper sweeps
    once it is an hour old under the store's lock held exclusive, then
    renamed into place, so an entry is always whole; one another step
    renamed first is kept. Its folders and files are read-only, as every
    folder that links them sees them."""
    digest = piece["sha256"]
    if not DIGEST.fullmatch(digest) or digest not in lock.pieces:
        raise PrimitiveError("the environment is missing a file piece")
    entry: Path = cache.CACHE / "store" / "files" / digest
    if entry.is_dir() and not entry.is_symlink():
        return entry
    source = root / lock.pieces[digest]
    if not holds(source, piece["size"], digest):
        raise PrimitiveError("an environment piece does not match its SHA-256 and size")
    entry.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{digest}.", dir=entry.parent))
    try:
        unpack(source, temporary)
        for directory, _, names in temporary.walk(top_down=False):
            for name in names:
                path = directory / name
                path.chmod(0o555 if path.stat().st_mode & 0o111 else 0o444)
            directory.chmod(0o555)
        try:
            temporary.rename(entry)
        except OSError as error:
            if not entry.is_dir():
                raise error from None
    finally:
        if temporary.exists():
            remove(temporary)
    return entry


def store_jar(source: Path, entry: Path) -> None:
    """A jar's store entry: a copy of it, `STORED_JAR`."""
    shutil.copyfile(source, entry / STORED_JAR)


def store_npm(source: Path, entry: Path) -> None:
    """An npm package's store entry: its tarball unpacked as npm unpacks
    one, its first folder dropped and every entry npm would not write
    passed over: a link, a device or a pipe, and a path that is absolute
    or climbs out with `..`. A tarball that is not one, or whose files
    would take more than `NPM_UNPACKED`, is refused."""
    total = 0
    try:
        with tarfile.open(source, "r:gz") as archive:
            for member in archive:
                parts = PurePosixPath(member.name).parts[1:]
                if (
                    not parts
                    or member.name.startswith("/")
                    or ".." in parts
                    or not (member.isfile() or member.isdir())
                ):
                    continue
                target = entry.joinpath(*parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                total += member.size
                if total > NPM_UNPACKED:
                    raise PrimitiveError("an npm package unpacks to more than 300 MB")
                target.parent.mkdir(parents=True, exist_ok=True)
                extracted = archive.extractfile(member)
                assert extracted is not None
                target.unlink(missing_ok=True)
                with extracted, target.open("xb") as copy:
                    shutil.copyfileobj(extracted, copy)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
    except (tarfile.TarError, EOFError, OSError) as error:
        if isinstance(error, OSError) and error.errno is not None:
            raise
        raise PrimitiveError("a locked npm package is not a tarball") from error
