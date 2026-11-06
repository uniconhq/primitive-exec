"""What every part of exec shares: the contract version, the most a
program may print, the `PATH` every box gets, a plain file name, and the
error that ends the step, a full disk's among them.
"""

import errno
import re

SCHEMA_VERSION = 6
OUTPUT_LIMIT = 32 * 1024 * 1024
RUN_PATH = "/env/bin:/usr/local/bin:/usr/bin:/bin"
FILE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
"""A name a named file is placed under in the run's working directory: never
`.` or `..`, never a path, never hidden."""


class PrimitiveError(Exception):
    """The primitive could not do its work at all.

    The message is one sentence for a person and becomes `error` in
    `outputs.json`, which the harness turns into a `system_error`.
    """


def no_room(error: OSError) -> PrimitiveError | None:
    """The step's error for `error` when it is a write the step's disk had
    no room for (`ENOSPC`), or `None` for any other. A full disk is the
    machine's to fix, never the entrant's however much they gave, so it
    ends the step as a `system_error` against the primitive."""
    if error.errno != errno.ENOSPC:
        return None
    return PrimitiveError(f"the step's disk is full: {error.strerror}")
