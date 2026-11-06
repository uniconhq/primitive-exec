"""Assemble the test lock with the real offline installer inside the slim image."""

import json
import shutil
import sys
from pathlib import Path


def main() -> None:
    sys.path.insert(0, "/usr/local/lib/unicon-exec")
    from exec.compile import cache
    from exec.compile.environment import assembled, read_lock

    root = Path("/work")
    for name in ("programs", "locks", "envs", "items"):
        (cache.CACHE / name).mkdir(exist_ok=True)
    lock = json.loads(Path("/fixture.json").read_bytes())
    item = root / "pieces"
    item.mkdir(exist_ok=True)
    for digest in lock["pieces"]:
        shutil.copyfile(Path("/archives") / digest, item / digest)
    file = root / "environment.json"
    file.write_text(json.dumps(lock))
    for language in lock["languages"]:
        with assembled(root, read_lock(file, language)):
            pass
    (cache.CACHE / "test-lock.json").write_text(file.read_text())


if __name__ == "__main__":
    main()
