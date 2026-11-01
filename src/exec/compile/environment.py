"""Assemble verified wheels once, copying into a private read-only environment."""

import asyncio
import contextlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
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
    pypi: list[dict[str, Any]]


CONDA_FIELDS = {"sha256": str, "file_name": str, "size": int, "record": dict}
PYPI_FIELDS = {"sha256": str, "size": int}
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
        document.get("version") == 2
        and document.get("prefix") == "/env"
        and isinstance(entry, dict)
        and isinstance(entry.get("fingerprint"), str)
        and DIGEST.fullmatch(entry["fingerprint"])
        and isinstance(pieces, dict)
        and all(isinstance(name, str) for name in [*pieces, *pieces.values()])
        and shaped(entry.get("conda", []), CONDA_FIELDS)
        and shaped(entry.get("pypi", []), PYPI_FIELDS)
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
    fingerprint = lock.fingerprint
    if not cache.in_use():
        raise PrimitiveError("the environment needs the machine's /cache volume")
    tools = cache.toolchain()
    folder = cache.CACHE / "envs" / fingerprint
    folder.parent.mkdir(exist_ok=True)
    built = False
    while True:
        with cache.locked(fingerprint, kind="envs", shared=True):
            if ready(folder, fingerprint, tools):
                folder.chmod(0o755)
                os.utime(folder)
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
                exclusive.enter_context(
                    cache.locked(fingerprint, kind="envs", wait=False)
                )
            except BlockingIOError:
                time.sleep(RETRY_SECONDS)
                continue
            if not ready(folder, fingerprint, tools):
                construct(root, lock, folder, tools)
                built = True


def construct(root: Path, lock: Lock, folder: Path, tools: str) -> None:
    try:
        temporary = Path(
            tempfile.mkdtemp(prefix=f"{lock.fingerprint}.", dir=folder.parent)
        )
    except OSError as error:
        raise PrimitiveError(
            f"the environment could not be assembled: {error.strerror or error}"
        ) from error
    try:
        if not lock.conda:
            raise PrimitiveError("the environment has no locked toolchain")
        asyncio.run(
            install_conda(root, lock.pieces, lock.conda, temporary, lock.platform)
        )
        sites = sorted((temporary / "lib").glob("python*/site-packages"))
        if lock.pypi and len(sites) != 1:
            raise PrimitiveError("the locked Python has no unique site-packages folder")
        site_packages = (
            str(sites[0].relative_to(temporary)) if sites else "site-packages"
        )
        for wheel in lock.pypi:
            digest = wheel["sha256"]
            if not DIGEST.fullmatch(digest) or digest not in lock.pieces:
                raise PrimitiveError("the environment is missing a wheel piece")
            path = root / lock.pieces[digest]
            if (
                not path.is_file()
                or path.stat().st_size != wheel["size"]
                or cache.file_sha256(path) != digest
            ):
                raise PrimitiveError(
                    "an environment piece does not match its SHA-256 and size"
                )
            unpack_wheel(path, temporary, site_packages=site_packages)
        for path in temporary.rglob("*"):
            if path.is_symlink():
                if not path.resolve().is_relative_to(temporary.resolve()):
                    raise PrimitiveError(
                        "an installed package links outside its environment"
                    )
                continue
            if path.is_file():
                if path.stat().st_nlink > 1:
                    copy = path.with_name(path.name + ".private-copy")
                    shutil.copy2(path, copy)
                    copy.replace(path)
                path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
            elif path.is_dir():
                path.chmod(0o555)
        (temporary / "ready.json").write_text(
            json.dumps(marker(lock.fingerprint, tools))
        )
        # The folder it replaces is moved aside whole before it is removed,
        # so its name never holds a folder half gone; a leftover, under the
        # name of a folder being made, is the wrapper's to sweep.
        stale = None
        if folder.exists():
            stale = Path(
                tempfile.mkdtemp(prefix=f"{lock.fingerprint}.", dir=folder.parent)
            )
            folder.replace(stale)
        temporary.rename(folder)
        if stale is not None:
            remove(stale)
    except OSError as error:
        raise PrimitiveError(
            f"the environment could not be assembled: {error.strerror or error}"
        ) from error
    finally:
        if temporary.exists():
            remove(temporary)


def remove(folder: Path) -> None:
    """Remove a folder exec made, whose folders it made read-only."""
    for parent, _, _ in folder.walk():
        with contextlib.suppress(OSError):
            parent.chmod(0o700)
    shutil.rmtree(folder, ignore_errors=True)


def unpack_wheel(
    wheel: Path, target: Path, *, site_packages: str = "site-packages"
) -> None:
    try:
        with zipfile.ZipFile(wheel) as archive:
            for entry in archive.infolist():
                path = PurePosixPath(entry.filename)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in entry.filename
                    or stat.S_ISLNK(entry.external_attr >> 16)
                ):
                    raise PrimitiveError("a wheel holds an unsafe path or link")
                if not path.parts:
                    continue
                parts = path.parts
                scheme = site_packages
                if parts[0].endswith(".data"):
                    if len(parts) < 3:
                        continue
                    mapped = {
                        "purelib": site_packages,
                        "platlib": site_packages,
                        "scripts": "bin",
                        "data": "",
                        "headers": "include",
                    }.get(parts[1])
                    if mapped is None:
                        raise PrimitiveError("a wheel names an unknown install folder")
                    scheme = mapped
                    parts = parts[2:]
                output = target / scheme / Path(*parts)
                if not output.resolve().is_relative_to(target.resolve()):
                    raise PrimitiveError("a wheel path links outside its environment")
                if entry.is_dir():
                    output.mkdir(parents=True, exist_ok=True)
                    continue
                output.parent.mkdir(parents=True, exist_ok=True)
                if output.exists():
                    raise PrimitiveError("two wheels install the same file")
                with archive.open(entry) as source, output.open("xb") as destination:
                    if scheme == "bin":
                        first = source.readline()
                        destination.write(
                            b"#!/env/bin/python3\n"
                            if first.rstrip() in (b"#!python", b"#!pythonw")
                            else first
                        )
                    shutil.copyfileobj(source, destination, 1024 * 1024)
                output.chmod(0o555 if entry.external_attr >> 16 & 0o111 else 0o444)
    except (OSError, zipfile.BadZipFile) as error:
        raise PrimitiveError("an environment wheel could not be unpacked") from error


def marker(fingerprint: str, tools: str) -> dict[str, str]:
    """What a whole folder's `ready.json` holds: the fingerprint it was
    assembled for, its prefix, and the toolchain line of the image that
    assembled it, so a folder an older exec assembled is assembled again."""
    return {"fingerprint": fingerprint, "prefix": "/env", "toolchain": tools}


def ready(folder: Path, fingerprint: str, tools: str) -> bool:
    try:
        return bool(
            json.loads((folder / "ready.json").read_text())
            == marker(fingerprint, tools)
        )
    except OSError, ValueError:
        return False


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
    with tempfile.TemporaryDirectory(prefix="conda-", dir=root) as work:
        scratch = Path(work)
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
            metadata = scratch / (digest + ".json")
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
            alternative_target_prefix="/env",
            platform=rattler.Subdir(platform),
            execute_link_scripts=False,
            show_progress=False,
            cache_dir=scratch / "packages",
        )


def jdk_home(folder: Path) -> str:
    """`JAVA_HOME` for the JDK locked in `folder`, as a box sees it under
    `/env`: the folder above its `java`'s own file."""
    found = (folder / "bin" / "java").resolve()
    if not found.is_relative_to(folder.resolve()) or not found.is_file():
        raise PrimitiveError("the locked environment has no tool java")
    return str(
        PurePosixPath("/env") / found.parent.parent.relative_to(folder.resolve())
    )


def tool(folder: Path, name: str) -> str:
    found = folder / "bin" / name
    resolved = found.resolve()
    if not resolved.is_relative_to(folder.resolve()) or not resolved.is_file():
        raise PrimitiveError(f"the locked environment has no tool {name}")
    return "/env/" + str(found.relative_to(folder))
