"""Environment assembly links each file from the machine's store, lists
the entries it links, and refuses a file it copied."""

import asyncio
import contextlib
import fcntl
import hashlib
import io
import json
import os
import shutil
import stat
import tarfile
import threading
import time
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from rattler.exceptions import ExtractError, IoError, LinkError, TransactionError

from exec.compile import cache, environment
from exec.compile.environment import Toolchain
from exec.shared import PrimitiveError

TOOLS = hashlib.sha256(b"tools").hexdigest()
"""The toolchain line the tests' exec has."""
NEWER = hashlib.sha256(b"newer tools").hexdigest()
"""The toolchain line of another exec on the same machine."""
WHEEL = "example-1.0-py3-none-any.whl"
PACKAGE = "python-3.14-0"
"""The conda package the tests' installer links, by its store entry."""
ARCHIVE = "Archive0"
"""The uv cache entry the tests' installer links the wheel from."""
SITE = "lib/python3.14/site-packages"


@dataclass
class Installs:
    """What the tests' installers were handed and how they install: each
    prefix rattler and uv installed into, the wheels uv was handed, and
    whether a file is copied rather than linked, as across two
    filesystems."""

    conda: list[Path] = field(default_factory=list)
    wheels: list[tuple[Path, list[Path]]] = field(default_factory=list)
    copy: bool = False


def setup_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, Installs]:
    """A work folder with a lock of one conda package and one wheel, the
    machine's cache, and installers that stand in for rattler and uv: each
    links its files from an entry it makes in the store, writes the files
    the real one writes itself, and records what it was handed."""
    root = tmp_path / "work"
    root.mkdir()
    machine = tmp_path / "cache"
    for name in ("programs", "envs", "locks"):
        (machine / name).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cache, "CACHE", machine)
    toolchain = tmp_path / "toolchain"
    toolchain.write_text(TOOLS + "\n")
    monkeypatch.setattr(cache, "TOOLCHAIN", toolchain)
    monkeypatch.setattr(environment, "ENV", tmp_path / "env")
    installs = Installs()

    def place(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if installs.copy:
            shutil.copyfile(source, target)
        else:
            os.link(source, target)

    async def installed(
        root: Path,
        pieces: dict[str, str],
        records: list[dict[str, Any]],
        target: Path,
        platform: str,
    ) -> None:
        installs.conda.append(target)
        entry = machine / "store" / "conda" / PACKAGE
        if not entry.exists():
            (entry / "bin").mkdir(parents=True)
            (entry / "bin/python3").write_text("python")
            (entry / "etc").mkdir()
            (entry / "etc/python.cfg").write_text("prefix=PLACEHOLDER\n")
        place(entry / "bin/python3", target / "bin/python3")
        (target / "etc").mkdir()
        (target / "etc/python.cfg").write_text("prefix=/env\n")
        (target / SITE).mkdir(parents=True)
        (target / "conda-meta").mkdir()
        paths = [
            {"_path": "bin/python3", "path_type": "hardlink"},
            {
                "_path": "etc/python.cfg",
                "path_type": "hardlink",
                "prefix_placeholder": "PLACEHOLDER",
            },
        ]
        (target / "conda-meta" / f"{PACKAGE}.json").write_text(
            json.dumps(
                {"extracted_package_dir": str(entry), "paths_data": {"paths": paths}}
            )
        )
        (target / "CACHEDIR.TAG").write_text("Signature\n")

    def wheels_installed(prefix: Path, wheels: list[Path]) -> None:
        installs.wheels.append((prefix, wheels))
        archive = machine / "store" / "uv" / "archive-v0" / ARCHIVE
        if not archive.exists():
            (archive / "example").mkdir(parents=True)
            (archive / "example/__init__.py").write_text("answer = 42\n")
            (archive / "example-1.0.dist-info").mkdir()
            (archive / "example-1.0.dist-info/METADATA").write_text("Name: example\n")
        site = prefix / SITE
        for name in ("example/__init__.py", "example-1.0.dist-info/METADATA"):
            place(archive / name, site / name)
        dist = site / "example-1.0.dist-info"
        (dist / "INSTALLER").write_text("uv\n")
        (dist / "REQUESTED").write_text("")
        (dist / "RECORD").write_text("example/__init__.py,,\n../../../bin/example,,\n")
        (prefix / "bin/example").write_text(f"#!{prefix}/bin/python3\nimport example\n")

    monkeypatch.setattr(environment, "install_conda", installed)
    monkeypatch.setattr(environment, "install_wheels", wheels_installed)
    wheel = root / "piece"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("example/__init__.py", "answer = 42\n")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    lock = root / "environment.json"
    lock.write_text(
        json.dumps(
            {
                "version": 3,
                "prefix": "/env",
                "platform": "linux-aarch64",
                "pieces": {digest: wheel.name},
                "languages": {
                    "python": {
                        "fingerprint": "a" * 64,
                        "conda": [
                            {
                                "sha256": "b" * 64,
                                "file_name": "python.conda",
                                "size": 1,
                                "record": {"name": "python", "version": "3.14"},
                            }
                        ],
                        "pypi": [
                            {
                                "sha256": digest,
                                "file_name": WHEEL,
                                "size": wheel.stat().st_size,
                            }
                        ],
                    }
                },
            }
        )
    )
    return root, lock, wheel, installs


def assembled(
    root: Path, lock: Path, language: str
) -> contextlib.AbstractContextManager[environment.Environment]:
    return environment.assembled(root, environment.read_lock(lock, language))


def test_a_folder_links_its_files_from_the_store_lists_its_entries_and_is_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The folder links each file from the store, and its `ready.json`
    lists the entries it links; a file of its own is read-only, a linked
    one keeps the store's mode, and the wheel is handed to uv from its copy
    in the store, which a second assembly leaves untouched, since uv keys
    its unpacked copy by the file's inode change time. A second step reuses
    the folder, its pieces gone."""
    root, lock, wheel, installs = setup_environment(tmp_path, monkeypatch)
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    store = cache.CACHE / "store"
    with assembled(root, lock, "python") as first:
        folder = first.folder
        assert stat.S_IMODE(folder.stat().st_mode) == 0o755
        listed = json.loads((folder / "ready.json").read_text())
        entries = [f"conda/{PACKAGE}", f"uv/archive-v0/{ARCHIVE}", f"wheels/{digest}"]
        assert listed == environment.marker("a" * 64, TOOLS) | {"store": entries}
        module = folder / SITE / "example/__init__.py"
        unpacked = store / "uv/archive-v0" / ARCHIVE / "example/__init__.py"
        assert module.samefile(unpacked)
        python = store / "conda" / PACKAGE / "bin/python3"
        assert (folder / "bin/python3").samefile(python)
        assert stat.S_IMODE(module.stat().st_mode) & 0o200
        assert not stat.S_IMODE((folder / "etc/python.cfg").stat().st_mode) & 0o222
        assert stat.S_IMODE((folder / "etc").stat().st_mode) == 0o555
    stored = store / "wheels" / digest / WHEEL
    assert stored.read_bytes() == wheel.read_bytes()
    assert not stat.S_IMODE(stored.stat().st_mode) & 0o222
    assert [path.name for path in stored.parent.iterdir()] == [WHEEL]
    assert [wheels for _, wheels in installs.wheels] == [[stored]]
    wheel.unlink()
    with assembled(root, lock, "ipynb") as second:
        assert second == first
    assert len(installs.wheels) == 1
    changed = stored.stat().st_ctime_ns
    cache.TOOLCHAIN.write_text(NEWER + "\n")
    with assembled(root, lock, "python"):
        assert stored.stat().st_ctime_ns == changed
    assert len(installs.wheels) == 2


def npm_tarball(path: Path, members: list[tuple[tarfile.TarInfo, bytes]]) -> str:
    """Write an npm tarball of `members` to `path` and return its SHA-256."""
    with tarfile.open(path, "w:gz") as archive:
        for member, content in members:
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tar_member(name: str, kind: bytes = tarfile.REGTYPE, mode: int = 0o644) -> Any:
    member = tarfile.TarInfo(name)
    member.type, member.mode = kind, mode
    if kind == tarfile.SYMTYPE:
        member.linkname = "/etc/passwd"
    return member


def test_maven_jars_and_npm_packages_are_placed_from_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A locked jar is linked into `lib/java/` and an npm package into its
    place under `lib/`, each from its store entry, `files/<sha256>/`,
    unpacked once and read-only, which the folder's `ready.json` lists; an
    npm tarball's first folder is dropped and what npm would not write, a
    link or a path out, is passed over."""
    root, lock, _, _ = setup_environment(tmp_path, monkeypatch)
    jar = root / "jar-piece"
    jar.write_bytes(b"PK jar")
    jar_digest = hashlib.sha256(jar.read_bytes()).hexdigest()
    tarball = root / "npm-piece"
    npm_digest = npm_tarball(
        tarball,
        [
            (tar_member("package/index.js"), b"module.exports = 1\n"),
            (tar_member("package/bin/run", mode=0o755), b"#!/usr/bin/env node\n"),
            (tar_member("package/../../escape"), b"out"),
            (tar_member("package/link", tarfile.SYMTYPE), b""),
        ],
    )
    document = json.loads(lock.read_text())
    document["pieces"] |= {jar_digest: jar.name, npm_digest: tarball.name}
    language = document["languages"]["python"]
    language["maven"] = [
        {"sha256": jar_digest, "file_name": "lib-1.0.jar", "size": 6},
    ]
    language["npm"] = [
        {
            "sha256": npm_digest,
            "path": "node_modules/a/node_modules/@b/c",
            "size": tarball.stat().st_size,
        }
    ]
    lock.write_text(json.dumps(document))
    store = cache.CACHE / "store" / "files"
    with assembled(root, lock, "python") as found:
        folder = found.folder
        assert (folder / "lib/java/lib-1.0.jar").samefile(
            store / jar_digest / environment.STORED_JAR
        )
        placed = folder / "lib/node_modules/a/node_modules/@b/c"
        assert sorted(path.name for path in placed.rglob("*")) == [
            "bin",
            "index.js",
            "run",
        ]
        assert (placed / "index.js").samefile(store / npm_digest / "index.js")
        assert stat.S_IMODE((placed / "index.js").stat().st_mode) == 0o444
        assert stat.S_IMODE((placed / "bin/run").stat().st_mode) == 0o555
        assert stat.S_IMODE((store / npm_digest).stat().st_mode) == 0o555
        listed = json.loads((folder / "ready.json").read_text())["store"]
        assert {f"files/{jar_digest}", f"files/{npm_digest}"} <= set(listed)
    assert not (tmp_path / "escape").exists()
    assert sorted(path.name for path in store.iterdir()) == sorted(
        [jar_digest, npm_digest]
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("maven", {"file_name": "../x.jar"}, "is not a plain jar name"),
        ("npm", {"path": "node_modules/../x"}, "place .* is not safe"),
        ("npm", {"path": "lib/x"}, "place .* is not safe"),
    ],
)
def test_a_jar_or_npm_package_placed_outside_its_folder_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: dict[str, str],
    message: str,
) -> None:
    root, lock, _, _ = setup_environment(tmp_path, monkeypatch)
    piece = root / "piece-2"
    piece.write_bytes(b"x")
    digest = hashlib.sha256(b"x").hexdigest()
    document = json.loads(lock.read_text())
    document["pieces"][digest] = piece.name
    shape = {
        "sha256": digest,
        "size": 1,
        "file_name": "x.jar",
        "path": "node_modules/x",
    }
    document["languages"]["python"][field] = [shape | value]
    lock.write_text(json.dumps(document))
    with (
        pytest.raises(PrimitiveError, match=message),
        assembled(root, lock, "python"),
    ):
        pass


def test_a_stored_wheel_that_is_not_the_wheel_its_name_says_is_copied_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, lock, wheel, _ = setup_environment(tmp_path, monkeypatch)
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    stored = cache.CACHE / "store" / "wheels" / digest / WHEEL
    stored.parent.mkdir(parents=True)
    stored.write_bytes(b"not the wheel")
    with assembled(root, lock, "python"):
        assert stored.read_bytes() == wheel.read_bytes()
    assert [path.name for path in stored.parent.parent.iterdir()] == [digest]


def stored_together(
    root: Path,
    lock: environment.Lock,
    monkeypatch: pytest.MonkeyPatch,
    copy: Any,
) -> list[object]:
    """What two steps storing `lock`'s wheels at once each got, the wheels
    or what was raised, each step's copy written only once both copy."""
    together = threading.Barrier(2)

    def copied(source: Path, target: Path) -> Path:
        together.wait(timeout=5)
        return Path(copy(source, target))

    monkeypatch.setattr(shutil, "copyfile", copied)
    found: list[object] = []

    def store() -> None:
        try:
            found.append(environment.stored_wheels(root, lock))
        except Exception as error:
            found.append(error)

    threads = [threading.Thread(target=store) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    return found


def test_two_steps_storing_one_wheel_at_once_both_take_one_whole_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store's lock is only shared, so two steps, as of two exec lines
    or two slots, may store one wheel at once: both take it, from the one
    copy linked first, and neither fails or removes the other's."""
    root, lock, wheel, _ = setup_environment(tmp_path, monkeypatch)
    read = environment.read_lock(lock, "python")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    stored = cache.CACHE / "store" / "wheels" / digest / WHEEL
    copy = shutil.copyfile
    for _ in range(20):
        environment.remove(stored.parent)
        found = stored_together(root, read, monkeypatch, copy)
        assert found == [[stored], [stored]]
        assert stored.read_bytes() == wheel.read_bytes()
        assert [path.name for path in stored.parent.iterdir()] == [WHEEL]


def test_two_packages_installing_one_file_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The package installed last would win the file, and the other's
    record still claim it, so a lock where two packages, a wheel and a
    conda package here, install the same file is refused, naming it."""
    root, lock, _, _ = setup_environment(tmp_path, monkeypatch)
    install = environment.install_wheels

    def clashing(prefix: Path, wheels: list[Path]) -> None:
        install(prefix, wheels)
        dist = prefix / SITE / "example-1.0.dist-info"
        with (dist / "RECORD").open("a") as record:
            record.write("../../../bin/python3,,\n")

    monkeypatch.setattr(environment, "install_wheels", clashing)
    with pytest.raises(
        PrimitiveError,
        match=f"{PACKAGE} and example-1.0.dist-info, install the same file bin/python3",
    ):
        with assembled(root, lock, "python"):
            pytest.fail("a folder two packages claim a file of is not assembled")


def test_the_store_lock_is_held_shared_while_a_folder_is_assembled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wrapper frees no store entry while `locks/store.lock` is held,
    and an entry unpacked for a folder is listed nowhere until the folder
    is whole, so the lock is held shared while the folder is assembled and
    released once it is."""
    root, lock, _, installs = setup_environment(tmp_path, monkeypatch)
    path = cache.CACHE / "locks" / "store.lock"
    seen: list[str] = []
    install = environment.install_wheels

    def tried(prefix: Path, wheels: list[Path]) -> None:
        install(prefix, wheels)
        descriptor = os.open(path, os.O_RDONLY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            seen.append("shared")
        finally:
            os.close(descriptor)

    monkeypatch.setattr(environment, "install_wheels", tried)
    with assembled(root, lock, "python"):
        descriptor = os.open(path, os.O_RDONLY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)
    assert seen == ["shared"] and len(installs.wheels) == 1


def test_a_file_copied_rather_than_linked_fails_the_assembly_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store on another filesystem than the folder makes both tools copy,
    rattler without a word, and the folder is refused rather than made of
    copies, nothing of it left under its name."""
    root, lock, _, installs = setup_environment(tmp_path, monkeypatch)
    installs.copy = True
    with pytest.raises(PrimitiveError, match="bin/python3 was copied rather than"):
        with assembled(root, lock, "python"):
            pytest.fail("a folder of copies is not assembled")
    assert list((cache.CACHE / "envs").iterdir()) == []


def test_the_build_goes_through_env_and_where_it_cannot_the_launchers_are_pointed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both installers write into the folder through `/env`, a link removed
    after, so each writes `/env` where it names its prefix. Where `/env`
    cannot be made, they install at the folder's own path, rattler told to
    write `/env`, and uv's launchers are pointed at `/env` after."""
    root, lock, _, installs = setup_environment(tmp_path, monkeypatch)
    with assembled(root, lock, "python"):
        pass
    link = environment.ENV
    assert [prefix for prefix, _ in installs.wheels] == [link] == installs.conda
    assert not link.exists() and not link.is_symlink()
    monkeypatch.setattr(environment, "ENV", tmp_path / "missing" / "env")
    cache.TOOLCHAIN.write_text(NEWER + "\n")
    with assembled(root, lock, "python") as other:
        ((prefix, _),) = installs.wheels[1:]
        assert prefix.parent == other.folder.parent and prefix != other.folder
        launcher = (other.folder / "bin/example").read_text()
        assert launcher == "#!/env/bin/python3\nimport example\n"


def test_the_launchers_uv_writes_at_a_folders_own_path_are_pointed_at_env(
    tmp_path: Path,
) -> None:
    """uv writes a launcher's first line naming the Python, or, for a path
    too long for one, shell lines that do; a file linked from the store, or
    any other script, is left as it is."""
    folder = tmp_path / "folder"
    (folder / "bin").mkdir(parents=True)
    python = f"{folder}/bin/python3"
    (folder / "bin/short").write_text(f"#!{python}\nimport a\n")
    (folder / "bin/long").write_text(
        f"#!/bin/sh\n'''exec' '{python}' \"$0\" \"$@\"\n' '''\nimport b\n"
    )
    (folder / "bin/other").write_text("#!/bin/sh\necho\n")
    (tmp_path / "stored").write_text(f"#!{python}\n")
    os.link(tmp_path / "stored", folder / "bin/linked")
    environment.point_launchers(folder)
    assert (folder / "bin/short").read_text() == "#!/env/bin/python3\nimport a\n"
    assert (folder / "bin/long").read_text() == "#!/env/bin/python3\nimport b\n"
    assert (folder / "bin/other").read_text() == "#!/bin/sh\necho\n"
    assert (folder / "bin/linked").read_text() == f"#!{python}\n"


def test_an_installed_link_out_of_the_folder_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, lock, _, _ = setup_environment(tmp_path, monkeypatch)
    install = environment.install_wheels

    def escaping(prefix: Path, wheels: list[Path]) -> None:
        install(prefix, wheels)
        (prefix / SITE / "escape").symlink_to(tmp_path)

    monkeypatch.setattr(environment, "install_wheels", escaping)
    with pytest.raises(PrimitiveError, match="links outside its environment"):
        with assembled(root, lock, "python"):
            pytest.fail("a folder that links out is not assembled")


def test_a_folder_is_ready_only_with_its_fingerprint_prefix_and_toolchain_line(
    tmp_path: Path,
) -> None:
    (tmp_path / "ready.json").write_text(
        json.dumps(environment.marker("a" * 64, TOOLS) | {"store": ["conda/x"]})
    )
    assert environment.ready(tmp_path, "a" * 64, TOOLS)
    assert not environment.ready(tmp_path, "a" * 64, NEWER)
    assert not environment.ready(tmp_path, "c" * 64, TOOLS)
    (tmp_path / "ready.json").write_text("[]")
    assert not environment.ready(tmp_path, "a" * 64, TOOLS)


def test_two_toolchain_lines_give_two_folders_and_neither_waits_on_the_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder is named by the lock's fingerprint and the toolchain line,
    so an exec of another line assembles a folder of its own while the
    first's is held or being assembled, and each keeps its own (#572)."""
    root, lock, _, _ = setup_environment(tmp_path, monkeypatch)
    with assembled(root, lock, "python") as first:
        assert first.folder.name == f"{'a' * 64}-{TOOLS}"
        marker = json.loads((first.folder / "ready.json").read_text())
        assert marker.items() >= environment.marker("a" * 64, TOOLS).items()
        inode = first.folder.stat().st_ino
    cache.TOOLCHAIN.write_text(NEWER + "\n")
    found: list[Path] = []

    def other() -> None:
        with assembled(root, lock, "python") as second:
            found.append(second.folder)

    with cache.locked(first.folder.name, kind="envs"):
        waiting = threading.Thread(target=other, daemon=True)
        waiting.start()
        waiting.join(timeout=5)
        assert not waiting.is_alive(), "the other line waited on the first's lock"
    (second,) = found
    assert second.name == f"{'a' * 64}-{NEWER}"
    marker = json.loads((second / "ready.json").read_text())
    assert marker.items() >= environment.marker("a" * 64, NEWER).items()
    assert first.folder.stat().st_ino == inode
    assert sorted(path.name for path in (cache.CACHE / "envs").iterdir()) == sorted(
        [first.folder.name, second.name]
    )


def test_a_toolchain_line_that_is_not_a_sha256_is_a_named_step_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wrapper discards a folder whose name is not two SHA-256s, so an
    image whose line is not one assembles none."""
    root, lock, _, _ = setup_environment(tmp_path, monkeypatch)
    cache.TOOLCHAIN.write_text("tools\n")
    with pytest.raises(PrimitiveError, match="is not a SHA-256"):
        with assembled(root, lock, "python"):
            pytest.fail("a folder cannot be named by this line")


def test_a_missing_piece_is_a_primitive_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, lock, wheel, _ = setup_environment(tmp_path, monkeypatch)
    wheel.unlink()
    with pytest.raises(PrimitiveError, match="does not match"):
        with assembled(root, lock, "python"):
            pytest.fail("an environment with missing bytes cannot run")


def test_a_step_that_finds_the_folder_unready_never_queues_behind_its_readers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A step that found the folder not ready, while another holds its
    shared lock, does not wait for the exclusive lock, where the steps that
    take the shared lock after it would go first, but looks again and takes
    the folder once it is ready."""
    root, lock, _, _ = setup_environment(tmp_path, monkeypatch)
    monkeypatch.setattr(environment, "RETRY_SECONDS", 0.01)

    def refused(*_: object) -> None:
        raise AssertionError("the folder was assembled a second time")

    monkeypatch.setattr(environment, "construct", refused)
    fingerprint = "a" * 64
    folder = cache.CACHE / "envs" / f"{fingerprint}-{TOOLS}"
    taken: list[Path] = []

    def assemble() -> None:
        with assembled(root, lock, "python") as found:
            taken.append(found.folder)

    with cache.locked(folder.name, kind="envs", shared=True):
        waiting = threading.Thread(target=assemble, daemon=True)
        waiting.start()
        time.sleep(0.1)
        assert waiting.is_alive()
        folder.mkdir()
        (folder / "ready.json").write_text(
            json.dumps(environment.marker(fingerprint, TOOLS))
        )
        waiting.join(timeout=5)
        assert not waiting.is_alive(), "the step waited behind the shared lock"
    assert taken == [folder]


@pytest.mark.parametrize(
    "spoil",
    [
        lambda lock: lock.update(version=2),
        lambda lock: lock.update(pieces=["piece"]),
        lambda lock: lock.update(pieces={"a" * 64: 7}),
        lambda lock: lock["languages"]["python"].update(fingerprint=7),
        lambda lock: lock["languages"]["python"].update(conda={}),
        lambda lock: lock["languages"]["python"]["conda"][0].pop("size"),
        lambda lock: lock["languages"]["python"]["conda"][0].update(size=True),
        lambda lock: lock["languages"]["python"]["conda"][0].update(record=[]),
        lambda lock: lock["languages"]["python"]["pypi"][0].update(size="1"),
        lambda lock: lock["languages"]["python"]["pypi"].append(None),
        lambda lock: lock["languages"]["python"].update(maven=[{"sha256": "a"}]),
        lambda lock: lock["languages"]["python"].update(npm=[{"path": 1}]),
        lambda lock: lock["languages"]["python"].update(asked=[]),
        lambda lock: lock["languages"]["python"].update(asked={"conda": [1]}),
        lambda lock: lock.update(languages=[]),
    ],
)
def test_a_lock_of_the_wrong_shape_is_a_named_step_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spoil: Any
) -> None:
    root, lock, _, _ = setup_environment(tmp_path, monkeypatch)
    document = json.loads(lock.read_text())
    spoil(document)
    lock.write_text(json.dumps(document))
    with pytest.raises(PrimitiveError, match="lock could not be read"):
        with assembled(root, lock, "python"):
            pytest.fail("a lock of the wrong shape cannot be assembled")


@pytest.mark.parametrize("platform", ["linux-sparc", None, 7])
def test_a_platform_conda_does_not_know_is_a_named_step_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: object
) -> None:
    _, lock, _, _ = setup_environment(tmp_path, monkeypatch)
    document = json.loads(lock.read_text())
    document["platform"] = platform
    lock.write_text(json.dumps(document))
    with pytest.raises(PrimitiveError, match="names no platform conda knows"):
        environment.read_lock(lock, "python")


@pytest.mark.parametrize(
    "error",
    [
        LinkError("link"),
        IoError("io"),
        ExtractError("extract"),
        TransactionError("transaction"),
        OSError("record"),
        TypeError("size"),
    ],
)
def test_what_rattler_raises_installing_is_a_named_step_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    async def failed(*_: object) -> None:
        raise error

    monkeypatch.setattr(environment, "install_records", failed)
    with pytest.raises(PrimitiveError, match="could not be installed"):
        asyncio.run(
            environment.install_conda(
                tmp_path, {}, [], tmp_path / "target", "linux-aarch64"
            )
        )


def test_a_malformed_conda_record_is_a_named_step_fault(tmp_path: Path) -> None:
    piece = tmp_path / "piece"
    piece.write_bytes(b"conda")
    digest = hashlib.sha256(b"conda").hexdigest()
    records = [{"sha256": digest, "file_name": "python.conda", "size": 5, "record": {}}]
    with pytest.raises(PrimitiveError, match="could not be installed"):
        asyncio.run(
            environment.install_conda(
                tmp_path,
                {digest: piece.name},
                records,
                tmp_path / "target",
                "linux-aarch64",
            )
        )


def test_two_programs_share_one_held_environment_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exec import inputs, programs
    from exec.compile.build import Built

    root, lock, _, _ = setup_environment(tmp_path, monkeypatch)
    monkeypatch.setattr(programs, "box_users", lambda: [60000, 60001])
    monkeypatch.setattr(programs, "step_memory_kb", lambda: 1024 * 1024)
    monkeypatch.setattr(programs, "check_room", lambda *_: None)
    monkeypatch.setattr(programs, "set_up_step", lambda *_: None)

    def refused(*_: object, toolchain: Toolchain) -> Built:
        toolchain.folder()
        return Built("refused", None)

    monkeypatch.setattr(programs, "build", refused)
    held: set[str] = set()
    real = cache.locked

    @contextlib.contextmanager
    def checked(
        key: str, *, kind: str = "programs", shared: bool = False, wait: bool = True
    ) -> Iterator[None]:
        assert key not in held, "the step would wait on its own environment lock"
        held.add(key)
        try:
            with real(key, kind=kind, shared=shared, wait=wait):
                yield
        finally:
            held.remove(key)

    monkeypatch.setattr(cache, "locked", checked)
    item = inputs.Item(
        run="main",
        number=1,
        programs=tuple(
            inputs.Entry(
                name, inputs.Source(root / name, "python", environment=lock), 1, 64
            )
            for name in ("entrant", "judge")
        ),
    )
    outputs = programs.run_batch(root, [item])[0]["outputs"]
    assert isinstance(outputs, dict) and outputs["outcome"] == "compile_error"
    assert not held


@pytest.mark.parametrize(
    ("language", "content", "assembles"),
    [
        ("cpp", b"\x7fELF native", False),
        ("python", b"python", True),
    ],
)
def test_a_cached_compile_assembles_the_toolchain_only_for_a_box_that_needs_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    language: str,
    content: bytes,
    assembles: bool,
) -> None:
    """A compile the machine's cache holds assembles no toolchain folder,
    and a native program runs without one; a Python or Java program runs on
    its toolchain at `/env`, assembled before any run."""
    from exec import inputs, programs
    from exec.compile.build import Built

    root, lock, _, _ = setup_environment(tmp_path, monkeypatch)
    document = json.loads(lock.read_text())
    document["languages"]["cpp"] = document["languages"]["python"] | {
        "fingerprint": "c" * 64,
        "pypi": [],
    }
    lock.write_text(json.dumps(document))
    if content == b"python":
        with zipfile.ZipFile(tmp_path / "program", "w") as archive:
            archive.writestr("__main__.py", "pass\n")
        content = (tmp_path / "program").read_bytes()
    monkeypatch.setattr(programs, "box_users", lambda: [60000])
    monkeypatch.setattr(programs, "step_memory_kb", lambda: 1024 * 1024)
    monkeypatch.setattr(programs, "check_room", lambda *_: None)
    monkeypatch.setattr(programs, "set_up_step", lambda *_: None)
    monkeypatch.setattr(programs, "check_memory", lambda *_: None)
    folders: list[Path] = []

    def from_the_cache(
        folder: Path,
        language: str,
        entry: str | None,
        work: Path,
        *,
        toolchain: Toolchain,
    ) -> Built:
        work.mkdir()
        (work / "program").write_bytes(content)
        return Built("", work / "program")

    def ran(
        root: Path, item: inputs.Item, compiled: dict[Any, Any], *_: object
    ) -> dict[str, object]:
        (own,) = compiled.values()
        folders.append(own.toolchain.environment)
        return {"outcome": "accepted"}

    monkeypatch.setattr(programs, "build", from_the_cache)
    monkeypatch.setattr(programs, "run_item", ran)
    source = inputs.Source(root / "main", language, environment=lock)
    item = inputs.Item(
        run="main",
        number=1,
        programs=(inputs.Entry("main", source, 1, 64),),
        form="one",
    )
    programs.run_batch(root, [item])
    (found,) = folders
    assert (found is not None) == assembles
    assert [path.name for path in (cache.CACHE / "envs").iterdir()] == (
        [f"{'a' * 64}-{TOOLS}"] if assembles else []
    )


def test_a_missing_or_wrong_conda_piece_is_a_step_fault(tmp_path: Path) -> None:
    digest = "a" * 64
    records = [{"sha256": digest, "file_name": "python.conda", "size": 1, "record": {}}]
    with pytest.raises(PrimitiveError, match="missing a safe conda piece"):
        asyncio.run(
            environment.install_conda(
                tmp_path, {}, records, tmp_path / "target", "linux-aarch64"
            )
        )
    (tmp_path / digest).write_bytes(b"x")
    with pytest.raises(PrimitiveError, match="does not match"):
        asyncio.run(
            environment.install_conda(
                tmp_path,
                {digest: digest},
                records,
                tmp_path / "target",
                "linux-aarch64",
            )
        )


def test_an_invalid_program_entry_remains_a_named_input_fault(tmp_path: Path) -> None:
    from exec.inputs import read_item

    with pytest.raises(
        PrimitiveError, match="program bad of run r1 has no inputs object"
    ):
        read_item(
            tmp_path,
            {
                "run": "r1",
                "inputs": {"programs": {"programs": {"bad": 7}}},
            },
            1,
        )
