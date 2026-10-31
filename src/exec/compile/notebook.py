"""A notebook's code cells joined into one Python file, refusing a line
only IPython runs.
"""

import io
import json
import tokenize
from pathlib import Path

from exec.compile.shared import BINARY_LIMIT, CompileError

NOTEBOOK_FORMAT = 4
MAGIC_STARTS = ("%", "!")
"""What starts a line IPython reads as a magic or a shell command, which no
Python runs: there is no IPython when a notebook's program runs."""


def read_notebook(path: Path, name: str) -> str:
    """A notebook's text, which JSON has as UTF-8: one that is not, or that
    is over `BINARY_LIMIT` and so could not make a program, is a compile
    error, before it is parsed."""
    if path.stat().st_size > BINARY_LIMIT:
        raise CompileError(f"the notebook {name} is larger than 32 MB")
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise CompileError(f"the notebook {name} is not UTF-8 text") from None


def notebook_code(text: str, name: str) -> str:
    """A notebook's code cells, in order, as one Python file, each behind a
    line naming its cell, so a traceback's line can be found in the notebook.
    The file's first line says it is UTF-8, as it is written, so a coding
    line in the first cell, now its second line, does not change how
    Python reads it.

    The notebook is read as nbformat 4 JSON; its markdown and raw cells and
    every output it holds are passed over. A notebook that is not one, or a
    code cell with a line IPython alone would run, a magic or a shell
    command, is a compile error naming the cell, counted from 1 among all its
    cells as the notebook shows them.
    """
    try:
        document = json.loads(text.removeprefix("﻿"))
    except ValueError, RecursionError:
        raise CompileError(f"the notebook {name} is not valid JSON") from None
    cells = document.get("cells") if isinstance(document, dict) else None
    if not isinstance(cells, list) or document.get("nbformat") != NOTEBOOK_FORMAT:
        raise CompileError(
            f"the notebook {name} is not a notebook of format {NOTEBOOK_FORMAT}"
        )
    parts = ["# coding: utf-8\n"]
    for number, cell in enumerate(cells, 1):
        if not isinstance(cell, dict) or cell.get("cell_type") != "code":
            continue
        code = cell_source(cell.get("source"), name, number)
        magic = magic_line(code)
        if magic is not None:
            raise CompileError(
                f"cell {number} of the notebook {name} runs {magic[:80]!r}, which"
                " only IPython runs; there is none when the program runs"
            )
        parts.append(f"# cell {number}\n{code}\n")
    return "".join(parts)


def cell_source(source: object, name: str, number: int) -> str:
    """A code cell's text: a string, or a list of strings joined as written,
    and Unicode throughout: JSON can carry half of a surrogate pair, which
    no Python file holds."""
    if isinstance(source, str):
        text = source
    elif isinstance(source, list) and all(isinstance(line, str) for line in source):
        text = "".join(source)
    else:
        raise CompileError(f"cell {number} of the notebook {name} has no source text")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise CompileError(
            f"cell {number} of the notebook {name} holds text that is not Unicode"
        ) from None
    return text


def magic_line(code: str) -> str | None:
    """The first line of a cell that starts a statement with `%` or `!`, as
    a magic (`%pip`, `%%time`) or a shell command (`!ls`) does, or None.

    The cell is read as Python tokens, so a `%` that continues an expression
    or sits inside a string is not one. A cell Python cannot read as tokens
    is left to the compile, which says why.
    """
    starting = True
    try:
        for token in tokenize.generate_tokens(io.StringIO(code).readline):
            if token.type == tokenize.NEWLINE:
                starting = True
            elif token.type in (
                tokenize.NL,
                tokenize.COMMENT,
                tokenize.INDENT,
                tokenize.DEDENT,
            ):
                continue
            elif starting:
                if token.string in MAGIC_STARTS:
                    return token.line.strip()
                starting = False
    except tokenize.TokenError, SyntaxError:
        return None
    return None
