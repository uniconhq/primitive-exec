"""The image's command: read the batch, run it, and write `outputs.json`."""

import sys
from pathlib import Path

from exec.inputs import read_inputs
from exec.programs import run_batch
from exec.report import write_json
from exec.shared import SCHEMA_VERSION, PrimitiveError


def main(argv: list[str]) -> int:
    """Run every item `inputs.json` names and write `outputs.json`: a batch's
    outputs, or the outputs alone of inputs given alone.

    Exits 0 whenever `outputs.json` was written, error or not; the harness
    reads the outcomes and any error from that file.
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
    except PrimitiveError as error:
        print(f"exec: {error}", file=sys.stderr)
        document = {"schema_version": SCHEMA_VERSION, "error": str(error)}
    write_json(root / "outputs.json", document)
    return 0
