"""Node and TypeScript: how Node ends when its heap is full, tsc's
command line, and the folder program of the JavaScript it emits beside the
folder's other files.
"""

import signal
from pathlib import Path, PurePosixPath

from exec.compile.environment import Lock
from exec.compile.shared import (
    CompileError,
    Sources,
    argument,
    plain_files,
    write_folder,
)

OUT_OF_MEMORY = -signal.SIGABRT
"""How a Node run whose heap reached its limit ends, as a run's return
code: V8 aborts it, as `process.abort()` alone would otherwise."""
EMITTED = "js"
"""Where tsc writes, under the compile box's out folder."""
EMITTED_SUFFIXES = {".ts": ".js", ".mts": ".mjs", ".cts": ".cjs"}
"""What each TypeScript file's JavaScript is named, as tsc names it."""


def types(lock: Lock) -> list[str]:
    """The type packages the lock places at the top of `node_modules`,
    `@types/<name>`, by the name tsc's `--types` takes: tsc 6 reads none
    it is not named."""
    found = []
    for package in lock.npm:
        parts = package["path"].split("/")
        if len(parts) == 3 and parts[1] == "@types":
            found.append(parts[2])
    return sorted(found)


def tsc_command(sources: Sources, out: str, lock: Lock) -> list[str]:
    """tsc's command line, emitting each source's JavaScript under `out`
    in the folder's own layout, as Node's own modules resolve, with the
    locked type packages."""
    named = types(lock)
    return [
        "tsc",
        *(["--types", ",".join(named)] if named else []),
        "--outDir",
        out,
        "--rootDir",
        ".",
        "--module",
        "nodenext",
        "--pretty",
        "false",
        *map(argument, sources.sources),
    ]


def emitted_name(path: str) -> str:
    """The JavaScript file tsc emits for the TypeScript file `path`."""
    pure = PurePosixPath(path)
    return pure.with_suffix(EMITTED_SUFFIXES[pure.suffix]).as_posix()


def emitted(out: Path, copy: Path, sources: Sources, target: Path) -> None:
    """The folder program of a TypeScript compile: the JavaScript tsc wrote
    under `out`'s `EMITTED`, and the folder's files that are not
    TypeScript, as prepared in `copy`, starting from the entry's
    JavaScript."""
    files = dict(plain_files(out / EMITTED))
    start = emitted_name(sources.entry)
    if start not in files:
        raise CompileError(f"tsc wrote no {start} to run")
    kept = [
        (path, copy / path)
        for path in sources.files
        if PurePosixPath(path).suffix not in EMITTED_SUFFIXES and path not in files
    ]
    write_folder(sorted([*kept, *files.items()]), target, start)
