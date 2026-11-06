"""A Python package change does not invalidate a locked C compile."""

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from support import RunImage, batch_inputs, by_run, printed

pytestmark = pytest.mark.image


def test_a_python_package_change_reuses_the_c_program_without_its_toolchain(
    tmp_path: Path, run_image: RunImage, capfd: pytest.CaptureFixture[str]
) -> None:
    work = tmp_path / "work"
    source = '#include <stdio.h>\nint main(void) { printf("731\\n"); }\n'
    batch_inputs(work, source, [("isolation", "")], language="c", memory_limit=128)
    first = by_run(run_image(work))
    assert first["isolation"]["outcome"] == "accepted"
    assert printed(work, first["isolation"]) == "731\n"
    assert "compiled the c source" in capfd.readouterr().err

    wheel = work / "in/extra.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("extra/__init__.py", "value = 1\n")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    path = work / "in/environment.json"
    lock = json.loads(path.read_bytes())
    python = lock["languages"]["python"]
    python["pypi"].append(
        {
            "name": "extra",
            "version": "1",
            "file_name": "extra.whl",
            "sha256": digest,
            "size": wheel.stat().st_size,
        }
    )
    hashes = [piece["sha256"] for piece in python["conda"] + python["pypi"]]
    python["fingerprint"] = hashlib.sha256(
        json.dumps(["/env", *sorted(hashes)], separators=(",", ":")).encode()
    ).hexdigest()
    lock["pieces"][digest] = "in/extra.whl"
    path.write_text(json.dumps(lock))

    second = by_run(run_image(work))
    assert second["isolation"]["outcome"] == "accepted"
    assert printed(work, second["isolation"]) == "731\n"
    log = capfd.readouterr().err
    assert "took the compiled program" in log and "compiled the c source" not in log
    assert "reused environment" not in log and "assembled environment" not in log
