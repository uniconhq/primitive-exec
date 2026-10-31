"""Python and notebooks: the check every source passes, and the zip
application that runs the entry.
"""

import re
import zipfile
from pathlib import Path

from exec.compile.shared import Sources, argument

PYTHON_SHEBANG = b"#!/usr/bin/env python3\n"
PYTHON_FOLDER = "source"
"""Where a Python program holds the source folder, beside its `__main__.py`."""
PYTHON_LAUNCHER = """\
import sys
import zipimport
from types import ModuleType

entry = {entry!r}
archive = __file__.rpartition("/")[0]
path = archive + "/{folder}/" + entry
folder = path.rpartition("/")[0]
if sys.path and sys.path[0] == archive:
    sys.path[0] = folder
else:
    sys.path.insert(0, folder)
code = compile(zipimport.zipimporter(archive).get_data(path), path, "exec")
main = ModuleType("__main__")
main.__file__ = path
sys.modules["__main__"] = main
exec(code, main.__dict__)
"""
"""The `__main__.py` of every Python program: it runs the entry as the main
module, with the entry's own folder first on the import path, as `python3
<entry>` does."""
PYTHON_MAIN = re.compile(
    r"^if\s+(?:__name__\s*==\s*(['\"])__main__\1|(['\"])__main__\2\s*==\s*__name__)"
    r"\s*:",
    re.M,
)
BYTECODE = "/tmp/pycache"
"""Where the check writes its bytecode: the box's `/tmp`, since the sources
are read-only in the box."""


def command(sources: Sources, interpreter: str) -> list[str]:
    """The command line that checks every source with py_compile, using
    `interpreter`, the one programs run with."""
    return [
        interpreter,
        "-I",
        "-X",
        f"pycache_prefix={BYTECODE}",
        "-m",
        "py_compile",
        *map(argument, sources.sources),
    ]


def write_zip_application(source: Path, sources: Sources, binary: Path) -> None:
    """Write the folder at `source` and the launcher for its entry as a zip
    application, behind a shebang line, so the interpreter runs it as a
    script."""
    launcher = PYTHON_LAUNCHER.format(entry=sources.entry, folder=PYTHON_FOLDER)
    with binary.open("wb") as stream:
        stream.write(PYTHON_SHEBANG)
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("__main__.py", launcher)
            for path in sources.files:
                archive.write(source / path, f"{PYTHON_FOLDER}/{path}")
