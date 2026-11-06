"""Compiling one source into a program exec runs: prepared in exec, looked
up in the machine's cache, and otherwise compiled in an isolate box of its
own, then stored.

The compile box is box `COMPILE_BOX`, made fresh for each compile before
any run's box, with the compile's limits. It sees the prepared copy of the
source at `/source`, bound read-only, and writes the program, or Java's
classes, to `/box/out`, on its tmpfs. exec reads back only plain files,
never through a link, since the compiler ran on the entrant's source as the
box's user, who owns everything in the box while it runs.
"""

import errno
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from exec.box import (
    BOX_FOLDERS,
    STEP_MEMORY_KB,
    Limits,
    box_command,
    check_isolate,
    exit_code,
    fresh_box,
    held_memory_kb,
    read_meta,
)
from exec.compile import cache
from exec.compile.environment import Toolchain, tool
from exec.compile.prepare import PREPARE_SECONDS, prepare
from exec.compile.recipes import Compile, Recipe, recipe
from exec.compile.shared import (
    BINARY_LIMIT,
    LOG_LIMIT,
    TOO_LARGE,
    CompileError,
    Sources,
    plain_folder,
)
from exec.shared import RUN_PATH, PrimitiveError, no_room

COMPILE_BOX = 0
COMPILE_SECONDS = 30
"""The CPU and wall-clock seconds the compiler may take."""
COMPILE_BOX_KB = 2 * BINARY_LIMIT // 1024
"""The compile box's tmpfs: the program at its cap, and as much again for
the compiler's own files, its objects and Java's classes."""
SOURCE = "/source"


@dataclass(frozen=True)
class Tool:
    """The result of running one compiler: its exit code and its log.

    The exit code is None when the compiler was stopped for a limit.
    """

    returncode: int | None
    log: str

    @property
    def ok(self) -> bool:
        """Whether the compiler finished and exited 0."""
        return self.returncode == 0


@dataclass(frozen=True)
class Built:
    """A compile: its log, the program, when the source compiled, and
    whether it may be cached: not a compile stopped at a limit, which a
    busy machine can reach where another would not."""

    log: str
    program: Path | None
    lasting: bool = True

    @property
    def outcome(self) -> str:
        """`accepted` when the source compiled, else `compile_error`."""
        return "compile_error" if self.program is None else "accepted"


def check_room(memory_kb: int, compile_kb: int) -> None:
    """Refuse the batch, before anything compiles, when the step's memory,
    `memory_kb`, or the container's, holds no compile box of `compile_kb`,
    the most any of its languages' compiles takes, beside
    `STEP_MEMORY_KB`."""
    held_kb = held_memory_kb(memory_kb)
    if compile_kb + STEP_MEMORY_KB > held_kb:
        raise PrimitiveError(
            f"compiling needs {compile_kb // 1024} MB for its box, and "
            f"the step's {held_kb // 1024} MB leaves "
            f"{max(0, held_kb - STEP_MEMORY_KB) // 1024} MB for it"
        )


def build(
    folder: Path,
    language: str,
    entry: str | None,
    work: Path,
    *,
    toolchain: Toolchain,
) -> Built:
    """Compile the source folder `folder` for `language`, starting from
    `entry`, into `work/program`, `work` a new folder of exec's own, with
    the locked `toolchain`.

    The folder is prepared first (`prepare`), into `work/source`, then the
    machine's cache is asked for the compile, under the compile's key, which
    holds the toolchain's fingerprint and the packages the task asked for,
    and holding its lock, and on a miss the toolchain is assembled and it
    is compiled in a box and stored. A step without the cache compiles
    every time.
    """
    work.mkdir()
    copy, program = work / "source", work / "program"
    try:
        sources = prepare(
            folder, copy, language, entry, time.monotonic() + PREPARE_SECONDS
        )
    except CompileError as error:
        return Built(f"{error}\n", None)
    if not cache.in_use():
        return compile_sources(sources, copy, language, work, toolchain)
    key = cache.key(
        cache.toolchain(),
        language,
        sources,
        copy,
        toolchain.fingerprint,
        toolchain.lock.packages,
    )
    with cache.locked(key):
        found = cache.look_up(key, program)
        if found is not None:
            print(
                f"exec: took the compiled program {key} from the cache", file=sys.stderr
            )
            return Built(found.log, program if found.outcome == "accepted" else None)
        built = compile_sources(sources, copy, language, work, toolchain)
        if built.lasting:
            cache.store(key, built.outcome, built.log, built.program)
    return built


def compile_sources(
    sources: Sources,
    copy: Path,
    language: str,
    work: Path,
    toolchain: Toolchain,
) -> Built:
    """Compile the prepared copy at `copy` in a fresh compile box into
    `work/program`, by the language's recipe, with the locked `toolchain`,
    assembled now, at `/env`. A recipe with no compile runs nothing, and
    its program is made from the copy."""
    started = time.monotonic()
    program = work / "program"
    own = recipe(language)
    environment = toolchain.folder()
    for name, pointed in own.links.items():
        (copy / name).symlink_to(pointed)
    with fresh_box(COMPILE_BOX, COMPILE_BOX_KB) as box:
        for name in (*BOX_FOLDERS, "out", *(f"out/{each}" for each in own.made)):
            (box / name).mkdir()
            (box / name).chmod(0o1777)
        this = Compile(sources, copy, box / "out", environment, toolchain.lock)
        command = own.command(this)
        result = Tool(0, "")
        if command is not None:
            compiler = installed(command[0], environment)
            print(f"exec: compiler {compiler}; network off", file=sys.stderr)
            result = run_tool([compiler, *command[1:]], copy, work, environment, own)
        if result.ok:
            result = take_program(result, own, this, program)
    print(
        f"exec: compiled the {language} source in {time.monotonic() - started:.1f} s",
        file=sys.stderr,
    )
    if not result.ok:
        program.unlink(missing_ok=True)
        return Built(result.log, None, lasting=result.returncode is not None)
    return Built(result.log, program)


def installed(name: str, environment: Path) -> str:
    """Find a locked tool without following a link outside its folder; a
    command the recipe gave by its full path, as a shell, is the image's
    own, or already found."""
    return name if name.startswith("/") else tool(environment, Path(name).name)


def take_program(tool: Tool, own: Recipe, this: Compile, program: Path) -> Tool:
    """Make the program from what the compiler wrote under `this.out`, the
    box's `OUT`, or from exec's own copy, by the recipe, and return the
    compile's result: `tool`, or a failure when there is no program or it
    is over `BINARY_LIMIT`. A program exec writes fails to be written as
    one over the limit does, as compile's did, since the folder is the
    entrant's; a full disk is the step's error."""
    if not plain_folder(this.out):
        return Tool(1, tool.log + "the compiler's folder is not a folder\n")
    try:
        own.program(this, program)
    except CompileError as error:
        program.unlink(missing_ok=True)
        return Tool(1, f"{tool.log}{error}\n")
    except OSError as error:
        if full := no_room(error):
            raise full from error
        return Tool(1, tool.log + TOO_LARGE)
    if program.stat().st_size > BINARY_LIMIT:
        return Tool(1, tool.log + TOO_LARGE)
    return tool


def run_tool(
    command: list[str], copy: Path, work: Path, environment: Path, own: Recipe
) -> Tool:
    """Run a compiler in the compile box, already made, with the assembled
    toolchain folder `environment` at `/env` and the prepared copy at
    `copy` bound read-only at `SOURCE`, its working directory, and return
    its exit code and its log.

    isolate holds the compile's limits: `COMPILE_SECONDS` of CPU and of
    wall-clock time, the recipe's compile memory in the box's cgroup and
    its processes, and files of at most `BINARY_LIMIT` bytes. Standard
    output and error are read together and kept up to `LOG_LIMIT` bytes;
    the rest is read and counted, so the compiler never waits on a full
    pipe. The environment holds `PATH`, `HOME` and `TMPDIR`, both the
    box's `/tmp`, and `LANG=C.UTF-8`, then the recipe's compile
    environment; the box binds what the recipe's own options bind.
    """
    meta, errors = work / "meta", work / "errors"
    memory_kb = own.compile_memory_kb
    limits = Limits(COMPILE_SECONDS, COMPILE_SECONDS, memory_kb, own.processes)
    env = {"PATH": RUN_PATH, "HOME": "/tmp", "TMPDIR": "/tmp", "LANG": "C.UTF-8"}
    env |= own.compile_env
    options = [
        f"--dir=env={environment}",
        *own.box,
        f"--dir={SOURCE.lstrip('/')}={copy}:noexec",
        f"--chdir={SOURCE}",
        f"--fsize={BINARY_LIMIT // 1024}",
        "--stderr-to-stdout",
    ]
    isolate = box_command(COMPILE_BOX, meta, limits, env, options, command)
    try:
        with errors.open("wb") as stderr:
            keeper = subprocess.Popen(
                isolate, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=stderr
            )
    except OSError as error:
        if error.errno == errno.E2BIG:
            return Tool(1, "the source folder's paths are too long to compile\n")
        raise PrimitiveError(
            f"isolate could not be started: {error.strerror}"
        ) from None
    with keeper:
        assert keeper.stdout is not None
        kept, dropped = drain(keeper.stdout.fileno())
        keeper.stdout.close()
        code = keeper.wait()
    found = read_meta(meta)
    status = found.get("status", "")
    check_isolate(code, found, errors, "the compiler")
    log = kept.decode("utf-8", errors="replace")
    if dropped:
        log += (
            f"\n[compile log cut at {LOG_LIMIT} bytes; {dropped} more bytes left out]\n"
        )
    returncode = exit_code(found, 0 if status == "" else -signal.SIGKILL)
    if status == "TO":
        log += (
            f"\ncompiling took longer than {COMPILE_SECONDS} seconds and was stopped\n"
        )
        return Tool(None, log)
    if found.get("cg-oom-killed") == "1":
        log += (
            f"\ncompiling took more than {memory_kb // 1024} MB of memory"
            " and was stopped\n"
        )
        return Tool(None, log)
    if returncode == -signal.SIGXFSZ:
        return Tool(None, log + TOO_LARGE)
    if returncode < 0:
        return Tool(None, log + f"\nthe compiler was killed by signal {-returncode}\n")
    return Tool(returncode, log)


def drain(source: int) -> tuple[bytes, int]:
    """Read `source` to its end, keeping the first `LOG_LIMIT` bytes, and
    return them and how many more it gave."""
    kept = bytearray()
    dropped = 0
    while chunk := os.read(source, 1 << 16):
        room = LOG_LIMIT - len(kept)
        kept += chunk[:room]
        dropped += max(0, len(chunk) - room)
    return bytes(kept), dropped
