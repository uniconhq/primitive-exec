"""Environment assembly copies checked wheel files and never follows archive links."""

import contextlib
import hashlib
import json
import stat
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from exec.compile import cache, environment
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
                        "pypi": [{"sha256": digest, "size": wheel.stat().st_size}],
                    }
                },
            }
        )
    )
    return root, lock, wheel


def test_wheels_are_copied_once_and_the_folder_is_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, lock, wheel = setup_environment(tmp_path, monkeypatch)
    with environment.assembled(root, lock, "python") as first:
        assert first is not None
        assert stat.S_IMODE(first.folder.stat().st_mode) == 0o755
        module = first.folder / "site-packages/example/__init__.py"
        assert module.read_text() == "answer = 42\n"
        assert (first.folder / "site-packages/helper.py").read_text() == "value = 7\n"
        assert module.stat().st_nlink == 1
        assert module.stat().st_ino != wheel.stat().st_ino
        inode = module.stat().st_ino
    wheel.unlink()
    with environment.assembled(root, lock, "ipynb") as second:
        assert second == first and module.stat().st_ino == inode


def test_a_missing_piece_is_a_primitive_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, lock, wheel = setup_environment(tmp_path, monkeypatch)
    wheel.unlink()
    with pytest.raises(PrimitiveError, match="does not match"):
        with environment.assembled(root, lock, "python"):
            pytest.fail("an environment with missing bytes cannot run")


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
    monkeypatch.setattr(programs, "build", lambda *_, **__: Built("refused", None))
    held: set[str] = set()
    real = cache.locked

    @contextlib.contextmanager
    def checked(key: str, *, kind: str = "programs") -> Iterator[None]:
        assert key not in held, "the step would wait on its own environment lock"
        held.add(key)
        try:
            with real(key, kind=kind):
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
