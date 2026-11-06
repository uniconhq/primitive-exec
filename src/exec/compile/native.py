"""C, C++ and Rust: whether a source defines `main`, gcc's command line,
for one statically linked executable or, beside the task's own conda
packages, one linked against them in `/env`, and rustc's.
"""

import json
import re
from pathlib import Path, PurePosixPath

from exec.compile.environment import Lock, tool
from exec.compile.shared import Sources, argument

ELF_MAGIC = b"\x7fELF"
RUST_MAIN = re.compile(r"(?<![\w.:])fn\s++main\s*+\(")
"""A function named `main`: `fn main(`, not a method reached through `.`
or a path."""
PKG_CONFIG_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*")
PKG_CONFIG_FOLDERS = (PurePosixPath("lib/pkgconfig"), PurePosixPath("share/pkgconfig"))
"""Where pkg-config, its prefix `/env`, looks for a package's `.pc` file."""
LINKED = ["-I/env/include", "-L/env/lib", "-Wl,-rpath,/env/lib"]
"""What a program linked against the task's conda packages compiles with:
their headers and libraries in `/env`, which its run then sees too."""
WITH_FLAGS = 'flags=$(pkg-config --cflags --libs {names}) || exit; exec "$@" $flags'
"""The shell line that runs the compiler, its arguments the shell's own,
with pkg-config's flags for `names` after them, split as pkg-config
prints them."""

C_MAIN_NAME = re.compile(r"(?<![\w.:>])main\s*+\(")
C_STOP = re.compile(r"[;{}]")
C_PARAMETERS_LIMIT = 2000
C_AFTER_PARAMETERS_LIMIT = 200
"""How far a C or C++ `main(` may be from the `)` that closes its parameters,
and that `)` from the `{` of its body, for the file to count as defining main."""


def c_holds_main(code: str) -> bool:
    """Whether C or C++ code, its comments and literals blanked, defines `main`.

    That is `main(` not after a name, `.`, `::` or `->`, then within
    `C_PARAMETERS_LIMIT` characters a `)` with no `;`, `{` or `}` before it,
    then within `C_AFTER_PARAMETERS_LIMIT` characters, none of them `;`, a
    brace or a parenthesis, the `{` of a body. Each `main(` is checked against
    the first `;` or brace after it, found once for every `main(` before it,
    so the code is read in one pass.
    """
    stop = closing = -1
    for match in C_MAIN_NAME.finditer(code):
        opened = match.end()
        if stop < opened:
            found = C_STOP.search(code, opened)
            if found is None:
                return False
            stop = found.start()
            start = max(0, stop - C_AFTER_PARAMETERS_LIMIT - 1)
            closing = max(code.rfind("(", start, stop), code.rfind(")", start, stop))
        if (
            code[stop] == "{"
            and closing >= opened
            and code[closing] == ")"
            and closing - opened <= C_PARAMETERS_LIMIT
        ):
            return True
    return False


def command(
    sources: Sources, language: str, binary: str, folder: Path, lock: Lock
) -> list[str]:
    """gcc's command line for C (`c`) or C++ (`cpp`), writing one executable
    to `binary`: the entry first, then the program's other sources, run in
    the folder they are in. It is linked statically, but where the lock
    lists conda packages of the task's own (`Lock.packages`): then it is
    linked against them in `/env`, with the flags pkg-config gives for
    each `.pc` file they install, read in the box, header-only ones too."""
    others = [path for path in sources.sources if path != sources.entry]
    if language == "c":
        compiler = [tool(folder, "gcc"), "-x", "c", "-std=gnu17"]
    else:
        compiler = [tool(folder, "g++"), "-x", "c++", "-std=gnu++20"]
    line = [
        *compiler,
        "-O2",
        "-pipe",
        # The toolchain's glibc keeps threads in a libpthread of their own,
        # which a static link leaves out without it.
        "-pthread",
        *(LINKED if lock.packages else ["-static"]),
        "-DONLINE_JUDGE",
        "-iquote",
        ".",
        "-o",
        binary,
        *map(argument, [sources.entry, *others]),
        "-lm",
    ]
    names = pkg_config_names(folder, lock.packages)
    if not names:
        return line
    script = WITH_FLAGS.format(names=" ".join(names))
    return ["/bin/sh", "-c", script, "sh", *line]


def pkg_config_names(folder: Path, packages: list[str]) -> list[str]:
    """The pkg-config names of the `.pc` files the conda `packages`, by
    name, install in the assembled `folder`, from each one's record in
    its `conda-meta/`, in order."""
    names = set()
    for record in sorted((folder / "conda-meta").glob("*.json")):
        document = json.loads(record.read_text(encoding="utf-8"))
        if document.get("name") not in packages:
            continue
        for each in document.get("files", []):
            path = PurePosixPath(each)
            if path.suffix == ".pc" and path.parent in PKG_CONFIG_FOLDERS:
                names.add(path.stem)
    return sorted(name for name in names if PKG_CONFIG_NAME.fullmatch(name))


def rust_holds_main(code: str) -> bool:
    """Whether Rust code, its comments and literals blanked, defines a
    function `main`."""
    return RUST_MAIN.search(code) is not None


def rust_command(sources: Sources, binary: str, folder: Path) -> list[str]:
    """rustc's command line, writing one executable to `binary` from the
    entry, the crate's root, which names its other files as modules."""
    return [
        tool(folder, "rustc"),
        "--edition",
        "2024",
        "-O",
        "-o",
        binary,
        argument(sources.entry),
    ]
