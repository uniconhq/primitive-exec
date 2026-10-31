"""C and C++: whether a source defines `main`, and gcc's command line for
one statically linked executable.
"""

import re

from exec.compile.shared import Sources, argument

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


def command(sources: Sources, language: str, binary: str) -> list[str]:
    """gcc's command line for C (`c`) or C++ (`cpp`), writing one statically
    linked executable to `binary`: the entry first, then the program's
    other sources, run in the folder they are in."""
    others = [path for path in sources.sources if path != sources.entry]
    if language == "c":
        compiler = ["gcc", "-x", "c", "-std=gnu17"]
    else:
        compiler = ["g++", "-x", "c++", "-std=gnu++20"]
    return [
        *compiler,
        "-O2",
        "-pipe",
        "-static",
        "-DONLINE_JUDGE",
        "-iquote",
        ".",
        "-o",
        binary,
        *map(argument, [sources.entry, *others]),
        "-lm",
    ]
