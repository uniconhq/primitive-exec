"""Reading the source folder before any compiler runs: its files, the entry
named or found, and a copy laid out as the compiler reads it, a notebook
joined into one Python file. Plain Python in exec, outside any box, on
exec's own copy, under `PREPARE_SECONDS`.
"""

import math
import os
import re
import shutil
import time
from pathlib import Path, PurePosixPath

from exec.compile.java import (
    file_name,
    java_declarations,
    java_holds_main,
    java_main_class,
)
from exec.compile.native import c_holds_main
from exec.compile.notebook import notebook_code, read_notebook
from exec.compile.python import PYTHON_MAIN
from exec.compile.shared import (
    EXTENSIONS,
    LANGUAGE_NAMES,
    CompileError,
    Sources,
    code_only,
    read_text,
)

MAIN_DESCRIPTIONS = {
    "c": ("defines main", "define main"),
    "cpp": ("defines main", "define main"),
    "java": ("declares static void main", "declare static void main"),
    "python": ('checks __name__ == "__main__"', 'check __name__ == "__main__"'),
}
"""How the log says which files could be the entry, for one file and for many."""
FOLDER_LIMIT = 1000
"""The most files and folders the source folder may hold."""
NAMES_SHOWN = 10
PREPARE_SECONDS = 10
"""How long reading and copying the source folder may take, before any
compiler runs."""
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def prepare(
    folder: Path,
    source: Path,
    language: str,
    entry: str | None,
    deadline: float = math.inf,
) -> Sources:
    """Copy the source folder to `source` and work out what to compile,
    stopped as a compile error once `time.monotonic()` passes `deadline`.

    A folder of one file is that file, whatever it is called, copied under the
    name it is compiled as. A larger folder is copied with its layout, its
    sources are its files with the language's extensions, and the entry is
    the one named, or else the one source, or else the one source that holds
    main (see the README). For C and C++ a source other than the entry that
    holds main is left out; for Java the entry gives the class the jar runs.
    A notebook entry is replaced in the copy by its code cells, joined into
    one Python file under the notebook's name, and checked with the folder's
    Python files.

    Everything here is linear in the size of the folder, so the deadline is
    a backstop, checked between files: a folder that still takes that long
    ends as `compile_error` with a line in the log, never as the step
    killed at its time limit.
    """
    sources = prepare_folder(folder, source, language, entry, deadline)
    if language != "ipynb":
        return sources
    notebook = source / sources.entry
    try:
        code = notebook_code(read_notebook(notebook, sources.entry), sources.entry)
        notebook.write_text(code, encoding="utf-8")
    except OSError as error:
        raise CompileError(
            f"the notebook could not be read to compile it: {error.strerror}"
        ) from None
    modules = [path for path in sources.files if path.endswith(".py")]
    return Sources(sources.files, [sources.entry, *modules], sources.entry)


def in_time(deadline: float) -> None:
    """Stop reading the source folder once `deadline` has passed."""
    if time.monotonic() > deadline:
        raise CompileError(
            f"reading the source folder took longer than {PREPARE_SECONDS} seconds"
            " and was stopped"
        )


def prepare_folder(
    folder: Path,
    source: Path,
    language: str,
    entry: str | None,
    deadline: float = math.inf,
) -> Sources:
    """Copy the source folder to `source` and work out what to compile, as
    `prepare` says, a notebook still as it was written."""
    files = folder_files(folder, deadline)
    if entry is not None:
        entry = entry_path(entry, files)
    if len(files) == 1:
        name, main_class = single_file(folder / files[0], language)
        copy_file(folder / files[0], source / name)
        return Sources([name], [name], name, main_class)
    sources = [path for path in files if path.endswith(EXTENSIONS[language])]
    if entry is None:
        entry = find_entry(folder, sources, language, deadline)
    elif entry not in sources:
        raise CompileError(
            f"the entry {entry} is not a {LANGUAGE_NAMES[language]} source file"
            f" ({', '.join(EXTENSIONS[language])})"
        )
    main_class = None
    if language == "java":
        main_class = java_main_class(*java_declarations(read_text(folder / entry)))
    elif language != "python":
        kept = []
        for path in sources:
            in_time(deadline)
            if path == entry or not holds_main(folder / path, language):
                kept.append(path)
        sources = kept
    for path in files:
        in_time(deadline)
        copy_file(folder / path, source / path)
    return Sources(files, sources, entry, main_class)


def folder_files(folder: Path, deadline: float = math.inf) -> list[str]:
    """Every regular file under `folder`, as sorted paths relative to it.

    Links are neither followed nor listed. A folder that is empty, holds more
    than `FOLDER_LIMIT` entries or a name that is not UTF-8 is a compile
    error.
    """
    found: list[str] = []
    entries = 0
    for directory, folders, names in folder.walk():
        in_time(deadline)
        entries += len(folders) + len(names)
        if entries > FOLDER_LIMIT:
            raise CompileError(
                f"the source folder holds more than {FOLDER_LIMIT} files and folders"
            )
        for name in names:
            path = directory / name
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(folder).as_posix()
            try:
                relative.encode("utf-8")
            except UnicodeEncodeError:
                shown = os.fsencode(relative).decode("utf-8", errors="replace")
                raise CompileError(
                    f"the file name {shown} in the source folder is not UTF-8"
                ) from None
            found.append(relative)
    if not found:
        raise CompileError("the source folder holds no files")
    return sorted(found)


def entry_path(entry: str, files: list[str]) -> str:
    """The entry as a path relative to the folder, refusing one that is not a
    file in it.
    """
    path = PurePosixPath(entry)
    relative = path.as_posix()
    if path.is_absolute() or ".." in path.parts or relative not in files:
        raise CompileError(f"the entry {entry} is not a file in the source folder")
    return relative


def find_entry(
    folder: Path, sources: list[str], language: str, deadline: float = math.inf
) -> str:
    """The entry when none was named: the one source, or the one that holds main.

    Anything else is a compile error that asks for the entry to be named.
    """
    name = LANGUAGE_NAMES[language]
    if not sources:
        raise CompileError(
            f"the source folder holds no {name} source file"
            f" ({', '.join(EXTENSIONS[language])})"
        )
    if len(sources) == 1:
        return sources[0]
    if language == "ipynb":
        raise CompileError(
            f"the entry is not clear: the source folder holds {len(sources)}"
            " notebooks; name the one to run as the entry"
        )
    holders = []
    for path in sources:
        in_time(deadline)
        if holds_main(folder / path, language):
            holders.append(path)
    if len(holders) == 1:
        return holders[0]
    one, many = MAIN_DESCRIPTIONS[language]
    if holders:
        shown = ", ".join(holders[:NAMES_SHOWN])
        if len(holders) > NAMES_SHOWN:
            shown += f" and {len(holders) - NAMES_SHOWN} more"
        found = f"{shown} each {many}"
    else:
        found = f"no {name} source {one}"
    raise CompileError(
        f"the entry is not clear: {found}; name the file to start from as the entry"
    )


def holds_main(path: Path, language: str) -> bool:
    """Whether a source file looks like a program's start.

    C and C++: it defines a function `main`. Java: it declares a `static void
    main`. Python: it has a top-level `if __name__ == "__main__":`. Comments
    and string literals are ignored for C, C++ and Java.
    """
    text = read_text(path)
    if language == "python":
        return PYTHON_MAIN.search(text) is not None
    if language == "java":
        return java_holds_main(code_only(text, "java"))
    return c_holds_main(code_only(text, "c"))


def single_file(path: Path, language: str) -> tuple[str, str | None]:
    """The name the only file of a folder is compiled under, and for Java the
    class the jar runs.

    Java is named after the public top-level type, as javac requires, or
    `Main.java` when there is none; the others keep a plain name of the
    language's own extension and fall back to `main`.
    """
    if language != "java":
        return source_name(path, EXTENSIONS[language][0]), None
    package, types = java_declarations(read_text(path))
    return file_name(package, types), java_main_class(package, types)


def source_name(source: Path, suffix: str) -> str:
    """The name the source is compiled under, so the log names the entrant's file.

    A name that could be read as an option or a path falls back to `main`.
    """
    name = source.name
    if SAFE_NAME.fullmatch(name) and name.endswith(suffix):
        return name
    return "main" + suffix


def copy_file(origin: Path, target: Path) -> None:
    """Copy one file of the source folder, a failure being a compile error."""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(origin, target)
    except OSError as error:
        raise CompileError(
            f"the source folder could not be copied to compile it: {error.strerror}"
        ) from None
