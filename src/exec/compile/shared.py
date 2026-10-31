"""What every part of compiling shares: the languages, the caps on the log
and the program, the error a source that cannot compile raises, the
sources as prepared, reading a source's code without its comments and
literals, and copying a plain file out of a box or the cache.
"""

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

LANGUAGES = ("c", "cpp", "java", "python", "ipynb")
LANGUAGE_NAMES = {
    "c": "C",
    "cpp": "C++",
    "java": "Java",
    "python": "Python",
    "ipynb": "notebook",
}
EXTENSIONS = {
    "c": (".c",),
    "cpp": (".cpp", ".cc", ".cxx"),
    "java": (".java",),
    "python": (".py",),
    "ipynb": (".ipynb",),
}
"""The files of a folder that are compiled as the language's sources."""
LOG_LIMIT = 64 * 1024
BINARY_LIMIT = 32 * 1024 * 1024
TOO_LARGE = "the program would be larger than 32 MB\n"
"""What the log ends with when the program is over `BINARY_LIMIT`: the zip
application and the jar are written by exec, which the compiler's
file-size limit does not hold, so their size is checked once they are
written, and a write that fails is taken as one over it."""
PLAIN_START = re.compile(r"[A-Za-z0-9_]")
C_LITERAL = (
    r'"[^"\\\n]*+(?:\\.[^"\\\n]*+)*+"?'
    r"|'[^'\\\n]*+(?:\\.[^'\\\n]*+)*+'?"
)
JAVA_TEXT_BLOCK = r'"""[^"\\]*+(?:(?:\\.|"(?!""))[^"\\]*+)*+(?:""")?'
NOISE = {
    "c": re.compile(rf"{C_LITERAL}|/", re.DOTALL),
    "java": re.compile(rf"{JAVA_TEXT_BLOCK}|{C_LITERAL}|/", re.DOTALL),
}
"""A whole literal, or a `/` that may open a comment. A string or character
literal runs to its closing quote, escapes skipped, or else to the end of its
line; a Java text block to its closing triple quote, or else to the end of
the text. Every part is possessive, so a literal is read once."""


class CompileError(Exception):
    """The entrant's folder cannot be compiled as it is.

    The message is one line of the compile log, saying why, and the outcome is
    `compile_error`.
    """


@dataclass(frozen=True)
class Sources:
    """The source folder as copied for compiling, by paths relative to the copy.

    `files` is every file of the folder, `sources` those the compiler is given,
    `entry` the file to start from and, for Java, `main_class` the class the
    jar runs, with its package.
    """

    files: list[str]
    sources: list[str]
    entry: str
    main_class: str | None = None


def read_text(path: Path) -> str:
    """A source file's text, any bytes that are not UTF-8 replaced. A file
    over `BINARY_LIMIT`, which could make no program, is a compile error
    before it is read, since exec reads it whole, outside any box."""
    if path.stat().st_size > BINARY_LIMIT:
        raise CompileError(f"the source file {path.name} is larger than 32 MB")
    return path.read_bytes().decode("utf-8", errors="replace")


def argument(path: str) -> str:
    """A relative path as a compiler argument, never read as an option or an
    argument file.
    """
    return path if PLAIN_START.match(path) else f"./{path}"


def code_only(text: str, language: str) -> str:
    """The text with each comment and string or character literal replaced by
    one space, for C and C++ (`c`) or Java (`java`).

    One pass from the start, in one of a few states: code, kept up to the
    next `/` or quote; a line comment, skipped to the end of its line; a
    block comment, to `*/`; a string or character literal, to its closing
    quote, escapes skipped, or else to the end of its line; and for Java a
    text block, to its closing triple quote. A block comment or text block
    left open runs to the end of the text. Nothing is read twice, so the time
    is linear in the length of the text whatever it holds.
    """
    noise = NOISE[language]
    kept: list[str] = []
    position = looked = 0
    while found := noise.search(text, looked):
        start, end = found.span()
        if text[start] == "/":
            follower = text[start + 1 : start + 2]
            if follower == "/":
                end = text.find("\n", start + 2)
            elif follower == "*":
                end = text.find("*/", start + 2)
                end = -1 if end < 0 else end + 2
            else:
                looked = end
                continue
        kept += (text[position:start], " ")
        if end < 0:
            return "".join(kept)
        position = looked = end
    kept.append(text[position:])
    return "".join(kept)


def copy_plain(origin: Path, target: Path) -> str:
    """Copy the plain file at `origin`, never through a link and at most
    `BINARY_LIMIT` bytes, to a new file at `target`, and return its
    SHA-256."""
    descriptor = os.open(origin, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as source:
        found = os.fstat(descriptor)
        if not stat.S_ISREG(found.st_mode) or found.st_size > BINARY_LIMIT:
            raise ValueError(f"{origin.name} is not a plain file of at most 32 MB")
        with target.open("xb") as copy:
            while chunk := source.read(1 << 20):
                digest.update(chunk)
                copy.write(chunk)
                if copy.tell() > BINARY_LIMIT:
                    raise ValueError(f"{origin.name} grew past 32 MB")
    return digest.hexdigest()
