"""Environment assembly copies checked wheel files and never follows archive links."""

import asyncio
import contextlib
import hashlib
import json
import stat
import threading
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from rattler.exceptions import ExtractError, IoError, LinkError, TransactionError

from exec.compile import cache, environment
from exec.compile.environment import Toolchain
from exec.shared import PrimitiveError


def setup_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    root = tmp_path / "work"
    root.mkdir()
    machine = tmp_path / "cache"
    for name in ("programs", "envs", "locks"):
        (machine / name).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cache, "CACHE", machine)
    toolchain = tmp_path / "toolchain"
    toolchain.write_text("tools\n")
    monkeypatch.setattr(cache, "TOOLCHAIN", toolchain)

    async def installed(
        root: Path,
        pieces: dict[str, str],
        records: list[dict[str, Any]],
        target: Path,
        platform: str,
    ) -> None:
        (target / "lib/python3.14/site-packages").mkdir(parents=True)

    monkeypatch.setattr(environment, "install_conda", installed)
    wheel = root / "package.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("example/__init__.py", "answer = 42\n")
        archive.writestr("example-1.data/purelib/helper.py", "value = 7\n")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    lock = root / "environment.json"
    lock.write_text(
        json.dumps(
            {
                "version": 2,
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
                        "pypi": [{"sha256": digest, "size": wheel.stat().st_size}],
                    }
                },
            }
        )
    )
    return root, lock, wheel


def assembled(
    root: Path, lock: Path, language: str
) -> contextlib.AbstractContextManager[environment.Environment]:
    return environment.assembled(root, environment.read_lock(lock, language))


def test_wheels_are_copied_once_and_the_folder_is_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, lock, wheel = setup_environment(tmp_path, monkeypatch)
    with assembled(root, lock, "python") as first:
        assert first is not None
        assert stat.S_IMODE(first.folder.stat().st_mode) == 0o755
        module = first.folder / "lib/python3.14/site-packages/example/__init__.py"
        assert module.read_text() == "answer = 42\n"
        assert (
            first.folder / "lib/python3.14/site-packages/helper.py"
        ).read_text() == "value = 7\n"
        assert module.stat().st_nlink == 1
        assert module.stat().st_ino != wheel.stat().st_ino
        inode = module.stat().st_ino
    wheel.unlink()
    with assembled(root, lock, "ipynb") as second:
        assert second == first and module.stat().st_ino == inode


def test_a_folder_an_older_exec_assembled_is_assembled_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, lock, _ = setup_environment(tmp_path, monkeypatch)
    with assembled(root, lock, "python") as first:
        marker = json.loads((first.folder / "ready.json").read_text())
        assert marker == environment.marker("a" * 64, "tools")
        inode = first.folder.stat().st_ino
    cache.TOOLCHAIN.write_text("newer tools\n")
    with assembled(root, lock, "python") as second:
        assert second.folder.stat().st_ino != inode
        marker = json.loads((second.folder / "ready.json").read_text())
        assert marker == environment.marker("a" * 64, "newer tools")
        assert list(second.folder.parent.iterdir()) == [second.folder]
    with assembled(root, lock, "python") as third:
        assert third.folder.stat().st_ino == second.folder.stat().st_ino


def test_a_missing_piece_is_a_primitive_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, lock, wheel = setup_environment(tmp_path, monkeypatch)
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
    root, lock, _ = setup_environment(tmp_path, monkeypatch)
    monkeypatch.setattr(environment, "RETRY_SECONDS", 0.01)

    def refused(*_: object) -> None:
        raise AssertionError("the folder was assembled a second time")

    monkeypatch.setattr(environment, "construct", refused)
    fingerprint = "a" * 64
    folder = cache.CACHE / "envs" / fingerprint
    taken: list[Path] = []

    def assemble() -> None:
        with assembled(root, lock, "python") as found:
            taken.append(found.folder)

    with cache.locked(fingerprint, kind="envs", shared=True):
        waiting = threading.Thread(target=assemble, daemon=True)
        waiting.start()
        time.sleep(0.1)
        assert waiting.is_alive()
        folder.mkdir()
        (folder / "ready.json").write_text(
            json.dumps(environment.marker(fingerprint, "tools"))
        )
        waiting.join(timeout=5)
        assert not waiting.is_alive(), "the step waited behind the shared lock"
    assert taken == [folder]


@pytest.mark.parametrize(
    "spoil",
    [
        lambda lock: lock.update(version=3),
        lambda lock: lock.update(pieces=["piece"]),
        lambda lock: lock.update(pieces={"a" * 64: 7}),
        lambda lock: lock["languages"]["python"].update(fingerprint=7),
        lambda lock: lock["languages"]["python"].update(conda={}),
        lambda lock: lock["languages"]["python"]["conda"][0].pop("size"),
        lambda lock: lock["languages"]["python"]["conda"][0].update(size=True),
        lambda lock: lock["languages"]["python"]["conda"][0].update(record=[]),
        lambda lock: lock["languages"]["python"]["pypi"][0].update(size="1"),
        lambda lock: lock["languages"]["python"]["pypi"].append(None),
        lambda lock: lock.update(languages=[]),
    ],
)
def test_a_lock_of_the_wrong_shape_is_a_named_step_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spoil: Any
) -> None:
    root, lock, _ = setup_environment(tmp_path, monkeypatch)
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
    _, lock, _ = setup_environment(tmp_path, monkeypatch)
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


def test_java_home_is_the_folder_above_the_locked_javas_own_file(
    tmp_path: Path,
) -> None:
    """The compile box and each run's box get the one `JAVA_HOME`, read from
    where the lock's JDK put `java`, and a JDK whose `java` leads outside
    its folder or is not there is a step fault."""
    folder = tmp_path / "env"
    (folder / "lib/jvm/bin").mkdir(parents=True)
    (folder / "lib/jvm/bin/java").write_text("")
    (folder / "bin").mkdir()
    (folder / "bin/java").symlink_to("../lib/jvm/bin/java")
    assert environment.jdk_home(folder) == "/env/lib/jvm"
    (folder / "bin/java").unlink()
    (folder / "bin/java").write_text("")
    assert environment.jdk_home(folder) == "/env"
    (folder / "bin/java").unlink()
    (tmp_path / "java").write_text("")
    (folder / "bin/java").symlink_to(tmp_path / "java")
    with pytest.raises(PrimitiveError, match="no tool java"):
        environment.jdk_home(folder)
    (folder / "bin/java").unlink()
    with pytest.raises(PrimitiveError, match="no tool java"):
        environment.jdk_home(folder)


@pytest.mark.parametrize("path", ["../outside", "/outside", "pkg/../../outside"])
def test_wheel_paths_cannot_leave_the_environment(tmp_path: Path, path: str) -> None:
    wheel = tmp_path / "bad.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(path, "bad")
    with pytest.raises(PrimitiveError, match="unsafe"):
        environment.unpack_wheel(wheel, tmp_path / "env")


def test_two_programs_share_one_held_environment_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exec import inputs, programs
    from exec.compile.build import Built

    root, lock, _ = setup_environment(tmp_path, monkeypatch)
    monkeypatch.setattr(programs, "box_users", lambda: [60000, 60001])
    monkeypatch.setattr(programs, "step_memory_kb", lambda: 1024 * 1024)
    monkeypatch.setattr(programs, "check_room", lambda _: None)
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

    root, lock, _ = setup_environment(tmp_path, monkeypatch)
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
    monkeypatch.setattr(programs, "check_room", lambda _: None)
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
        ["a" * 64] if assembles else []
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


def test_a_wheel_cannot_write_through_a_conda_packages_external_directory_link(
    tmp_path: Path,
) -> None:
    target, outside = tmp_path / "env", tmp_path / "outside"
    site = target / "lib/python3.14/site-packages"
    site.mkdir(parents=True)
    outside.mkdir()
    (site / "linked").symlink_to(outside, target_is_directory=True)
    wheel = tmp_path / "package.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("linked/escaped.py", "unexpected = True\n")
    with pytest.raises(PrimitiveError, match="outside"):
        environment.unpack_wheel(
            wheel, target, site_packages="lib/python3.14/site-packages"
        )
    assert list(outside.iterdir()) == []


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
