"""The machine's cache of compiled programs, in the step's `/cache`
volume beside the wrapper's folders:

    /cache/programs/<key>/program      the compiled program, mode 0444
    /cache/programs/<key>/result.json  {outcome, log, program_sha256, created}
    /cache/locks/programs-<key>.lock   held while a step looks up, compiles,
                                       stores or copies it out

The wrapper lays the cache out for a step that is given one, sweeps a
folder a compile left half made, `programs/<key>.<anything>`, once its lock
is free, and evicts the least recently used folders, by their mtime,
skipping one whose lock is held. Only exec, as root, writes here; no box
sees `programs/`.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from exec.compile.shared import LOG_LIMIT, Sources, copy_plain
from exec.shared import PrimitiveError

CACHE = Path("/cache")
TOOLCHAIN = Path("/usr/local/lib/unicon-exec/toolchain")
"""One line written when the image is built: the SHA-256 of its installed
packages and versions, its Python's and exec's own files. Two images with
different compilers never share a compiled program."""
PROGRAM_MODE = 0o444
RESULT_LIMIT = 8 * LOG_LIMIT
"""The most of `result.json` read back: its log, each character escaped at
worst, and three short values."""


@dataclass(frozen=True)
class Cached:
    """A compile as the cache holds it: its outcome, its log, and on
    `accepted` the program's SHA-256."""

    outcome: str
    log: str
    program_sha256: str | None


def in_use() -> bool:
    """Whether the step has the machine's cache: the wrapper lays out
    `programs/` in it for a step given one."""
    return (CACHE / "programs").is_dir() and (CACHE / "locks").is_dir()


def toolchain() -> str:
    """The image's toolchain line, `TOOLCHAIN`."""
    try:
        return TOOLCHAIN.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as error:
        raise PrimitiveError(
            f"the image's toolchain file {TOOLCHAIN} could not be read, so no "
            "compiled program can be cached safely"
        ) from error


def key(tools: str, language: str, sources: Sources, source: Path) -> str:
    """The key of a compile: the SHA-256 of a canonical JSON of the
    toolchain line, the language, what was given the compiler, and each
    file of the prepared copy at `source`, its path and its SHA-256, by
    path."""
    files = [[path, file_sha256(source / path)] for path in sources.files]
    document = {
        "toolchain": tools,
        "language": language,
        "entry": sources.entry,
        "sources": sources.sources,
        "main_class": sources.main_class,
        "files": sorted(files),
    }
    text = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    """The SHA-256 of the file at `path`, read in pieces."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


@contextlib.contextmanager
def locked(key: str) -> Iterator[None]:
    """Hold `locks/programs-<key>.lock` for the block: the wrapper evicts no
    folder whose lock is held, and a second step with the same source waits
    for the first to store its compile. The lock holds only while its path
    still names the file locked, since the wrapper's sweep removes a lock
    no one holds; a removed one is taken again under its new file."""
    path = CACHE / "locks" / f"programs-{key}.lock"
    while True:
        try:
            held = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CREAT, 0o444)
        except OSError as error:
            raise PrimitiveError(
                f"the machine's cache lock for {key} could not be taken: "
                f"{error.strerror}"
            ) from None
        try:
            fcntl.flock(held, fcntl.LOCK_EX)
            with contextlib.suppress(FileNotFoundError):
                if os.path.samestat(os.fstat(held), os.lstat(path)):
                    break
        except BaseException:
            os.close(held)
            raise
        os.close(held)
    try:
        yield
    finally:
        os.close(held)


def look_up(key: str, program: Path) -> Cached | None:
    """The compile stored under `key`, its program copied to `program` and
    checked against its SHA-256, or None when there is none. A folder that
    does not hold what it says is removed, so the program is compiled and
    stored again. The folder's mtime is its last use, which the wrapper
    evicts by, so a hit touches it. Called with the key's lock held."""
    folder = CACHE / "programs" / key
    try:
        found = read_result(folder / "result.json")
    except FileNotFoundError:
        return None
    except (OSError, ValueError, RecursionError) as error:
        thrown_out(key, folder, program, error)
        return None
    try:
        if found.outcome == "accepted":
            copied = copy_plain(folder / "program", program)
            if copied != found.program_sha256:
                raise ValueError("the program is not the one stored")
        os.utime(folder)
    except (OSError, ValueError) as error:
        thrown_out(key, folder, program, error)
        return None
    return found


def thrown_out(key: str, folder: Path, program: Path, error: Exception) -> None:
    """Remove the cache's folder for `key`, which does not hold what it
    says, and any of its program copied to `program`, so it is compiled
    and stored again."""
    print(
        f"exec: the cached compile {key} is not whole ({error}); compiling again",
        flush=True,
        file=sys.stderr,
    )
    program.unlink(missing_ok=True)
    discard(folder)


def read_result(path: Path) -> Cached:
    """A folder's `result.json`, refusing anything that is not one exec
    wrote."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("result.json is not a plain file")
        text = stream.read(RESULT_LIMIT + 1)
    if len(text) > RESULT_LIMIT:
        raise ValueError("result.json is too large")
    document = json.loads(text)
    if not isinstance(document, dict):
        raise ValueError("result.json is not an object")
    outcome, log = document.get("outcome"), document.get("log")
    sha256 = document.get("program_sha256")
    if not isinstance(log, str) or len(log) > 2 * LOG_LIMIT:
        raise ValueError("result.json has no log")
    if outcome == "accepted" and isinstance(sha256, str) and len(sha256) == 64:
        return Cached(outcome, log, sha256)
    if outcome == "compile_error" and sha256 is None:
        return Cached(outcome, log, None)
    raise ValueError("result.json has no outcome exec gives")


def store(key: str, outcome: str, log: str, program: Path | None) -> None:
    """Store a compile under `key`: into a folder of its own,
    `programs/<key>.<anything>`, the program first and `result.json` last,
    then renamed into place, so a folder under its key is whole. Called with
    the key's lock held. A store that fails leaves the step's compile as it
    was: the next step compiles again."""
    programs = CACHE / "programs"
    building = None
    try:
        building = Path(tempfile.mkdtemp(prefix=f"{key}.", dir=programs))
        building.chmod(0o755)
        sha256 = None
        if program is not None:
            sha256 = copy_plain(program, building / "program")
            (building / "program").chmod(PROGRAM_MODE)
        result = {
            "outcome": outcome,
            "log": log,
            "program_sha256": sha256,
            "created": int(time.time()),
        }
        (building / "result.json").write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8"
        )
        discard(programs / key)
        building.rename(programs / key)
    except (OSError, ValueError) as error:
        print(f"exec: the compile {key} could not be cached: {error}", file=sys.stderr)
        if building is not None:
            discard(building)


def discard(path: Path) -> None:
    """Remove a folder of the cache, if it is there and can be: one left
    behind is a miss again, or swept by the wrapper."""
    with contextlib.suppress(OSError):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
