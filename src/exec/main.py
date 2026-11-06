"""The image's command: read the batch, run it, and write `outputs.json`."""

import contextlib
import shutil
import sys
from pathlib import Path

from exec.inputs import read_inputs
from exec.programs import run_batch
from exec.report import write_json
from exec.shared import SCHEMA_VERSION, PrimitiveError, no_room


def main(argv: list[str]) -> int:
    """Run every item `inputs.json` names and write `outputs.json`: a batch's
    outputs, or the outputs alone of inputs given alone.

    Exits 0 whenever `outputs.json` was written, error or not; the harness
    reads the outcomes and any error from that file. A write the step's
    disk had no room for, wherever it failed, is the step's error too, and
    what exec wrote for the runs is removed first, so `outputs.json` has
    room.
    """
    root = Path(argv[1]) if len(argv) > 1 else Path("/work")
    try:
        items = read_inputs(root)
        batch = run_batch(root, items)
        document: dict[str, object] = (
            {"schema_version": SCHEMA_VERSION, "outputs": batch[0]["outputs"]}
            if items and items[0].alone
            else {"schema_version": SCHEMA_VERSION, "batch": batch}
        )
    except (PrimitiveError, OSError) as error:
        failed = error if isinstance(error, PrimitiveError) else no_room(error)
        if failed is None:
            raise
        print(f"exec: {failed}", file=sys.stderr)
        document = {"schema_version": SCHEMA_VERSION, "error": str(failed)}
        cause = error if isinstance(error, OSError) else error.__cause__
        if isinstance(cause, OSError) and no_room(cause):
            emptied(root)
    write_json(root / "outputs.json", document)
    return 0


def emptied(root: Path) -> None:
    """Remove what exec wrote under `root` for the runs, every run's output
    under `out/`, any compile folder left and a partial `outputs.json`, so
    a disk that had no room has room for `outputs.json`, whose error voids
    the runs' outputs."""
    found = [*(root / "out").glob("*"), *root.glob("compile-*")]
    for path in [*found, root / "outputs.json.partial"]:
        with contextlib.suppress(OSError):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
