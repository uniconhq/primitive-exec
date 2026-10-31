"""Running the batch: each item's programs in their boxes, one alone with
a file on its standard input and its standard output kept, or several
side by side joined by links, and what each did.
"""

import contextlib
import errno
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import IO, BinaryIO

from exec.box import (
    BOX_CGROUPS,
    BOX_FOLDERS,
    BOX_USERS_VARIABLE,
    NOT_STARTED,
    Run,
    box_size_kb,
    box_users,
    cache_binding,
    check_memory,
    exit_code,
    fresh_box,
    hand_over,
    isolate_command,
    isolate_said,
    open_stdin,
    place_files,
    read_meta,
    set_up_step,
    step_memory_kb,
)
from exec.inputs import WALL_CLOCK, Entry, Item, Link, pipe_names
from exec.report import TEXTS, ReportError, judge, read_report, reported, write_json
from exec.shared import OUTPUT_LIMIT, RUN_PATH, PrimitiveError

REFUSED_ARGUMENTS = frozenset({errno.E2BIG})
"""What the kernel answers when the command line is too long for it, which
only `args` can make it: the contestant's input, not the step, failed.
"""
ELF_MAGIC = b"\x7fELF"
BOX = 0
"""The box every item of the one-program form runs in, one after another,
made afresh each time."""


@dataclass(frozen=True)
class Program:
    """A program ready to run: its format and the file the command starts,
    as the box sees it."""

    kind: str
    path: Path

    def command(self, entry: Entry) -> list[str]:
        """The command line that runs this program, the entry's arguments
        last. The interpreters are the image's own, which every box sees
        under `/usr`; Python's is started by its real path, outside any
        virtual environment exec runs in.
        """
        if self.kind == "native":
            return [str(self.path), *entry.args]
        if self.kind == "python":
            interpreter = os.path.realpath(sys.executable)
            return [interpreter, "-I", "-B", str(self.path), *entry.args]
        java = shutil.which("java", path=RUN_PATH)
        if java is None:
            raise PrimitiveError("this image has no Java runtime to run a jar with")
        return [
            os.path.realpath(java),
            f"-Xmx{max(16, int(entry.memory_limit))}m",
            "-Xss64m",
            "-XX:+UseSerialGC",
            "-XX:-UsePerfData",
            "-XX:+ExitOnOutOfMemoryError",
            "-Djava.io.tmpdir=.",
            "-jar",
            str(self.path),
            *entry.args,
        ]


def run_batch(root: Path, items: list[Item]) -> list[dict[str, object]]:
    """Check the step has a box user for each program of a run and that its
    memory holds each item's boxes side by side (`check_memory`), set the
    step up for its boxes, capped at that memory (`step_memory_kb`), then
    run the items one after another, in the order given, each in a fresh
    set of boxes with fresh copies of its programs, and return one outputs
    entry per item.

    isolate's meta files and its own standard error, and the named pipes, go
    in a folder of exec's in the container's `/tmp`, which no box sees
    but for its own folder of pipes.
    """
    if len({item.several for item in items}) > 1:
        raise PrimitiveError(
            "a batch mixes the one-program form and the several-programs form"
        )
    users = box_users()
    most = max((len(item.programs) for item in items), default=0)
    if most > len(users):
        raise PrimitiveError(
            f"a run has {most} programs, and {BOX_USERS_VARIABLE} gives "
            f"{len(users)} box users, one for each"
        )
    memory_kb = step_memory_kb()
    for item in items:
        needed = sum(box_size_kb(entry) for entry in item.programs)
        check_memory(needed, memory_kb, item.run)
    set_up_step(users, memory_kb)
    scratch = Path(tempfile.mkdtemp(prefix="exec-"))
    kinds: dict[Path, str] = {}
    results: list[dict[str, object]] = []
    try:
        for item in items:
            for entry in item.programs:
                if entry.program not in kinds:
                    kinds[entry.program] = detect(entry.program)
            if item.several:
                outputs = run_programs(root, item, kinds, scratch, users)
            else:
                outputs = run_item(root, item, kinds, scratch, users)
            results.append({"run": item.run, "outputs": outputs})
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return results


def detect(program: Path) -> str:
    """Tell which of the three program formats a file is.

    `native` is an ELF executable, `python` a zip application with a
    `__main__.py`, `java` a jar with a manifest.
    """
    with program.open("rb") as stream:
        if stream.read(4) == ELF_MAGIC:
            return "native"
    if zipfile.is_zipfile(program):
        with zipfile.ZipFile(program) as archive:
            names = set(archive.namelist())
        if "__main__.py" in names:
            return "python"
        if "META-INF/MANIFEST.MF" in names:
            return "java"
    raise PrimitiveError(
        "the program is not a native executable, a Python zip application or a Java jar"
    )


def run_item(
    root: Path, item: Item, kinds: dict[Path, str], scratch: Path, users: range
) -> dict[str, object]:
    """Run an item of the one-program form, its program of the format
    `kinds` gives it, in a fresh box and return its outputs, all of them
    whatever the outcome: what the program printed, its CPU time and its
    peak memory.

    The box's folder is a tmpfs of its own, laid out as `/box/program`, the
    copy of the program, `/box/run`, the run's working directory holding its
    named files, `/box/tmp` and `/box/shm`, which the run sees as `/tmp` and
    `/dev/shm` too, so the tmpfs's size and inode cap hold them, and for a
    program given a manifest `/box/report`, its report folder, which is read
    before the tmpfs goes.

    The time is the CPU time, and on `time_limit` at least the limit: a run
    the wall clock stopped, one that slept or waited, used little CPU.
    """
    (entry,) = item.programs
    kind = kinds[entry.program]
    out = root / "out" / str(item.number)
    out.mkdir(parents=True, exist_ok=True)
    with fresh_box(BOX, box_size_kb(entry)) as box:
        program = prepare(item, entry, kind, box)
        place_files(box / "run", entry.files | entry.loads, entry.cached)
        for name in BOX_FOLDERS:
            (box / name).mkdir()
            (box / name).chmod(0o1777)
        report = None
        if entry.manifest is not None:
            report = box / "report"
            report.mkdir()
        reports = report is not None
        run = execute(item.run, entry, program, reports, out / "output", scratch, users)
        run = replace(run, no_inodes=os.statvfs(box).f_ffree == 0)
        outcome = judge(run, kind)
        time_ms = round(run.cpu_seconds * 1000)
        if outcome == "time_limit":
            time_ms = max(time_ms, entry.time_limit_ms)
        outputs: dict[str, object] = {
            "output": {"file": f"out/{item.number}/output"},
            "time_ms": time_ms,
            "memory_kb": run.memory_kb,
            "outcome": outcome,
        }
        if report is not None:
            outputs |= reported(item, entry, outcome, report, out)
    return outputs


def prepare(item: Item, entry: Entry, kind: str, box: Path) -> Program:
    """Copy a program of the item, of format `kind`, into its box, before
    its clock starts, executable for a native executable, since the file
    under `in/` may not be, and readable for the others.

    The program is the copy's owner while it runs, so it may change its
    mode, times or attributes; the copy goes with the box.
    """
    try:
        shutil.copyfile(entry.program, box / "program")
    except OSError as error:
        which = entry.name if item.several else f"of run {item.run}"
        raise PrimitiveError(
            f"the program {which} could not be copied: {error.strerror}"
        ) from error
    (box / "program").chmod(0o755 if kind == "native" else 0o644)
    return Program(kind, Path("/box/program"))


def execute(
    run: str,
    entry: Entry,
    program: Program,
    reports: bool,
    output: Path,
    scratch: Path,
    users: range,
) -> Run:
    """Run the program of run `run`, of the one-program form, in box `BOX`,
    already made and filled, by `isolate_command`, and return what it did.

    The item folder of the step's cache its cached files are in, if any, is
    bound read-only as `CACHE_MOUNT` (`cache_binding`). isolate's
    `--special-files` is always given, as the programs form gives it: it keeps the
    links to cached files in the working directory, where isolate would
    remove them, and so too a link a run leaves in its report folder,
    which is a report that does not hold, whether or not the run had a
    cached file.

    Standard input is the `stdin` file, or `/dev/null`, opened by exec
    through a mount that is gone before the run (`open_stdin`) and handed
    to the box (`hand_over`), so only its path under `/work/in`, which may
    say which test it is, is hidden from the run. A cached `stdin` file is
    in the box all the same, under its random name, in the item folder
    bound at `CACHE_MOUNT`.
    Standard output is a pipe exec reads up to `OUTPUT_LIMIT` and one byte
    more, then closes, so output costs the box no memory and a run printing
    on past the limit dies of `SIGPIPE`; the output kept is cut to the
    limit.

    An argument holding a NUL character, which no command line can carry,
    and a command line too long for the kernel, which only the item's
    arguments can make, are a run that could not start, a `runtime_error`.
    isolate failing itself (`XX`) is an error of the step.
    """
    if any("\0" in argument for argument in entry.args):
        output.write_bytes(b"")
        return NOT_STARTED
    meta = scratch / "meta"
    errors = scratch / "errors"
    meta.unlink(missing_ok=True)
    more = [*cache_binding(entry.cached), "--special-files"]
    command = isolate_command(BOX, meta, entry, reports, more, program.command(entry))
    if entry.stdin is None:
        descriptor = os.open(os.devnull, os.O_RDONLY)
    else:
        descriptor = open_stdin(entry.stdin)
    with (
        os.fdopen(descriptor, "rb") as stdin,
        errors.open("wb") as stderr,
        output.open("wb") as stdout,
    ):
        if entry.stdin is not None:
            hand_over(stdin.fileno(), run, users)
        try:
            keeper = subprocess.Popen(
                command, stdin=stdin, stdout=subprocess.PIPE, stderr=stderr
            )
        except OSError as error:
            if error.errno in REFUSED_ARGUMENTS:
                return NOT_STARTED
            raise PrimitiveError(
                f"isolate could not be started: {error.strerror}"
            ) from None
        with keeper:
            assert keeper.stdout is not None
            printed = pump(keeper.stdout.fileno(), stdout, OUTPUT_LIMIT)
            keeper.stdout.close()
            code = keeper.wait()
    found = read_meta(meta)
    status = found.get("status", "")
    if code not in (0, 1) or status == "XX":
        said = found.get("message") or errors.read_bytes()
        raise PrimitiveError(
            f"isolate could not run the program: {isolate_said(code, said)}"
        )
    return Run(
        status=status,
        returncode=exit_code(found, 0 if status == "" else -9),
        cpu_seconds=float(found.get("time", "0")),
        memory_kb=int(found.get("cg-mem", "0")),
        output_bytes=printed,
        out_of_memory=found.get("cg-oom-killed") == "1",
    )


def pump(source: int, target: BinaryIO, limit: int) -> int:
    """Copy what `source` gives into `target`, up to `limit` bytes, and
    return how many bytes it gave, reading at most one past `limit`: a
    reader that went on would let a run print without end."""
    given = 0
    while given <= limit:
        chunk = os.read(source, min(1 << 16, limit + 1 - given))
        if not chunk:
            break
        target.write(chunk[: max(0, limit - given)])
        given += len(chunk)
    return given


FACTS = ("time_ms", "memory_kb", "ended", "signal", "ended_at", "stopped_by")
OVER_LIMIT = frozenset({"time_limit", "memory_limit", "output_limit"})
PIPES = "/pipes"
PIPE_MODE = 0o240
INTERRUPTED = "Interrupted"
WALL_CLOCK_MESSAGE = "Time limit exceeded (wall clock)"


@dataclass
class Ends:
    """A program's ends of the links, opened by exec before any box
    starts: the descriptors that are its standard input and output, when a
    link is, the named pipes in its folder of them, and the stand-ins
    exec holds for all of its ends, one opened in the program's place
    for each, until exec has seen the program end. The program at the
    other end then sees no end of input or `SIGPIPE` before exec has
    read how this one ended and stopped it where that stops the others, so
    a judge stopped for the entrant's limit is stopped before it can read
    the entrant gone and judge that."""

    stdin: int | None = None
    stdout: int | None = None
    named: list[str] = field(default_factory=list)
    stand_ins: list[int] = field(default_factory=list)


@dataclass
class Running:
    """One program while the item runs: its entry, its box and that box's
    folder, its ends of the links, the isolate that keeps it, a descriptor
    for that process and one on its box's `cgroup.events`, and the thread
    reading its unlinked standard output. `seen` says the box's cgroup has
    been seen holding a process, `ended_at` is in milliseconds from just
    before the first program started, `stopped_by` what stopped it, and
    `run` what it did, once
    its isolate has exited.
    """

    entry: Entry
    number: int
    kind: str
    box: Path
    meta: Path
    errors: Path
    ends: Ends = field(default_factory=Ends)
    keeper: subprocess.Popen[bytes] | None = None
    keeper_fd: int | None = None
    events_fd: int | None = None
    reader: threading.Thread | None = None
    printed: list[int] = field(default_factory=lambda: [0])
    seen: bool = False
    ended_at: int | None = None
    stopped_by: str | None = None
    run: Run | None = None


def run_programs(
    root: Path, item: Item, kinds: dict[Path, str], scratch: Path, users: range
) -> dict[str, object]:
    """Run an item of the several-programs form, its programs side by side,
    each of the format `kinds` gives it, in a fresh box, and return
    its outputs: each program's facts as `<name>.<fact>`, the reports of
    each given a manifest whose verdict is `0` as `<name>.<report>`, the
    facts of all as the file `facts`, and the outcome, `accepted`.

    Each box's folder is a tmpfs of its own, laid out as exec's: `/box/program`,
    the copy of the program, `/box/run`, its working directory, holding its
    named files and the links to its named pipes, `/box/tmp` and `/box/shm`,
    and for a program given a manifest `/box/report`, read before the tmpfs
    goes. Every box is made and filled before the first program starts.
    """
    out = root / "out" / str(item.number)
    out.mkdir(parents=True, exist_ok=True)
    pipes = scratch / "pipes" / str(item.number)
    named = pipe_names(item)
    with contextlib.ExitStack() as stack:
        running: list[Running] = []
        programs: list[Program] = []
        for number, entry in enumerate(item.programs):
            box = stack.enter_context(fresh_box(number, box_size_kb(entry)))
            each = Running(
                entry=entry,
                number=number,
                kind=kinds[entry.program],
                box=box,
                meta=scratch / f"meta-{number}",
                errors=scratch / f"errors-{number}",
            )
            programs.append(prepare(item, entry, each.kind, box))
            place_files(box / "run", entry.files, entry.cached)
            for name in named[entry.name]:
                (box / "run" / name).symlink_to(f"{PIPES}/{name}")
            for name in BOX_FOLDERS:
                (box / name).mkdir()
                (box / name).chmod(0o1777)
            if entry.manifest is not None:
                (box / "report").mkdir()
            each.events_fd = os.open(
                BOX_CGROUPS / f"box-{number}" / "cgroup.events", os.O_RDONLY
            )
            stack.callback(os.close, each.events_fd)
            populated(each.events_fd)
            each.meta.unlink(missing_ok=True)
            running.append(each)
        stack.callback(shutil.rmtree, pipes, True)
        make_links(item, running, pipes, users, stack)
        started = time.monotonic()
        for each, program in zip(running, programs, strict=True):
            each.keeper = start(each, program, pipes, stack)
        watch(running, started)
        facts = {each.entry.name: program_facts(each, item) for each in running}
    write_json(out / "facts.json", facts)
    outputs: dict[str, object] = {}
    for name, found in facts.items():
        outputs |= {f"{name}.{fact}": found[fact] for fact in FACTS if fact in found}
        if found.get("verdict") == "0":
            reports = found.get("reports", {})
            assert isinstance(reports, dict)
            outputs |= {f"{name}.{report}": value for report, value in reports.items()}
    outputs["facts"] = {"file": f"out/{item.number}/facts.json"}
    outputs["outcome"] = "accepted"
    return outputs


def make_links(
    item: Item,
    running: list[Running],
    pipes: Path,
    users: range,
    stack: contextlib.ExitStack,
) -> None:
    """Make each link before any box starts and give each program its ends,
    closed with `stack`.

    A program's first link out is handed to it as its standard output, and
    its first in as its standard input; any other end is opened through a
    named pipe in the program's folder under `pipes`, which its box sees as
    `PIPES`. A link that is a standard stream at both ends is an anonymous
    pipe (`os.pipe`), owned by root, which no box can reopen by a path; a
    link that is a named pipe at either end is a FIFO, owned by the writing
    program's box user and in the reading program's group (`PIPE_MODE`), box
    `n`'s user being the `n`th, so each box can open its own end by its path
    and neither can open it the other way. Every end opened here is also the
    program's stand-in, kept after the program has its own.
    """
    by_name = {each.entry.name: each for each in running}
    for each in running:
        (pipes / each.entry.name).mkdir(mode=0o755, parents=True)
    for number, link in enumerate(item.links):
        writer, reader = by_name[link.writer], by_name[link.reader]
        as_stdout = writer.ends.stdout is None
        as_stdin = reader.ends.stdin is None
        if as_stdout and as_stdin:
            reading, writing = os.pipe()
            os.set_blocking(reading, True)
        else:
            reading, writing = fifo_link(
                link,
                number,
                writer.number,
                reader.number,
                as_stdout,
                as_stdin,
                pipes,
                users,
            )
        stack.callback(close_quietly, reading)
        stack.callback(close_quietly, writing)
        writer.ends.stand_ins.append(writing)
        if as_stdout:
            writer.ends.stdout = writing
        else:
            writer.ends.named.append(f"to_{link.reader}")
        reader.ends.stand_ins.append(reading)
        if as_stdin:
            reader.ends.stdin = reading
        else:
            reader.ends.named.append(f"from_{link.writer}")


def fifo_link(
    link: Link,
    number: int,
    writer_box: int,
    reader_box: int,
    as_stdout: bool,
    as_stdin: bool,
    pipes: Path,
    users: range,
) -> tuple[int, int]:
    """One link that is a named pipe at an end: a FIFO owned by the writing
    program's box user and in the reading program's group, hard-linked into
    each program's folder under the name its box opens it by, when that end
    is a named pipe and not a standard stream. It is opened here for the
    stand-in, the reading end first so the writing end does not wait for a
    reader. Returns the reading and writing descriptors."""
    fifo = pipes / f"link-{number}"
    os.mkfifo(fifo, 0o600)
    os.chown(fifo, users[writer_box], users[reader_box])
    fifo.chmod(PIPE_MODE)
    reading = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    writing = os.open(fifo, os.O_WRONLY)
    os.set_blocking(reading, True)
    if not as_stdout:
        os.link(fifo, pipes / link.writer / f"to_{link.reader}")
    if not as_stdin:
        os.link(fifo, pipes / link.reader / f"from_{link.writer}")
    fifo.unlink()
    return reading, writing


def close_quietly(descriptor: int) -> None:
    """Close a descriptor that may be closed already."""
    with contextlib.suppress(OSError):
        os.close(descriptor)


def start(
    each: Running, program: Program, pipes: Path, stack: contextlib.ExitStack
) -> subprocess.Popen[bytes] | None:
    """Start one program in its box, already made and filled, and return
    the isolate that keeps it, or None when it could not start.

    The isolate command line is exec's (`isolate_command`), for box
    `each.number`, with the program's folder of named pipes bound read-only
    as `PIPES`, the item folder of the step's cache its cached named files
    are in, if any, bound read-only as `CACHE_MOUNT` (`cache_binding`), and
    isolate's `--special-files`, which keeps the links to both in the
    working directory where isolate would remove them. The
    folder is in the container's `/tmp`, which a step may mount `noexec`,
    as Docker mounts a tmpfs; in a user namespace that flag cannot be taken
    away, so the folder is bound `noexec` too, or isolate could not bind
    it. Standard input and
    output are the program's first links, handed over, exec keeping its
    own end of each as a stand-in (`Ends`); standard input is
    `/dev/null` without one, and standard output without one is read and
    thrown away up to `OUTPUT_LIMIT` and one byte more, as exec reads it.
    Standard error goes nowhere.

    An argument holding a NUL character and a command line too long for the
    kernel are a program that could not start, as under exec.
    """
    entry = each.entry
    stdin, stdout = each.ends.stdin, each.ends.stdout
    bound = f"--dir={PIPES.lstrip('/')}={pipes / entry.name}:noexec"
    command = isolate_command(
        each.number,
        each.meta,
        entry,
        entry.manifest is not None,
        [bound, *cache_binding(entry.cached), "--special-files"],
        program.command(entry),
    )
    if any("\0" in argument for argument in entry.args):
        return None
    try:
        with each.errors.open("wb") as stderr:
            keeper = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL if stdin is None else stdin,
                stdout=subprocess.PIPE if stdout is None else stdout,
                stderr=stderr,
            )
    except OSError as error:
        if error.errno in REFUSED_ARGUMENTS:
            return None
        raise PrimitiveError(
            f"isolate could not be started: {error.strerror}"
        ) from None
    stack.callback(stop_quietly, keeper)
    if keeper.stdout is not None:
        each.reader = threading.Thread(
            target=throw_away, args=(keeper.stdout, each.printed), daemon=True
        )
        each.reader.start()
    return keeper


def throw_away(source: IO[bytes], printed: list[int]) -> None:
    """Read a program's unlinked standard output into nothing, as exec reads
    a run's, and keep in `printed` how many bytes it gave."""
    with source, Path(os.devnull).open("wb") as nowhere:
        printed[0] = pump(source.fileno(), nowhere, OUTPUT_LIMIT)


def stop_quietly(keeper: subprocess.Popen[bytes]) -> None:
    """Kill an isolate still running while an error ends the batch, and
    reap it; the box's clean-up kills what it kept."""
    if keeper.poll() is None:
        keeper.kill()
        keeper.wait()


def watch(running: list[Running], started: float) -> None:
    """Wait for every program to end.

    Each program's ending time is when its box's cgroup empties after it
    was seen holding a process, which `cgroup.events` tells, or failing
    that when its isolate exits, in milliseconds from `started`, taken just
    before the first program was started. The programs that end at one wake
    are given the same time, and each wake a time at least 1 ms after the
    one before, so a program that ended because another's end was let go,
    which is always at a later wake, is always later, though both fell in
    one millisecond. Each `cgroup.events` was read once as it was opened,
    so it wakes a poll only on a change. When an isolate exits, its program's
    run is read (`reap`), and if it is the first to end over a limit of its
    own (`OVER_LIMIT`), or to end at all with `stops_others`, and nothing
    stopped it, every program still running is stopped, `stopped_by` it
    once isolate says it stopped it. Only then are its stand-ins closed, so
    the others see it gone; a stopper's are held until every program has
    ended, so none it stopped sees it gone first.
    """
    poller = select.poll()
    owners: dict[int, Running] = {}
    for each in running:
        if each.keeper is None:
            each.run = NOT_STARTED
            each.ended_at = 0
            continue
        each.keeper_fd = os.pidfd_open(each.keeper.pid)
        assert each.events_fd is not None
        poller.register(each.keeper_fd, select.POLLIN)
        poller.register(each.events_fd, select.POLLPRI)
        owners[each.keeper_fd] = owners[each.events_fd] = each
    stopper = None
    held: list[int] = []
    now = -1
    for each in running:
        if each.run is not None:
            stopper = let_go(each, running, stopper, held)
    while owners:
        ready = poller.poll()
        now = max(round((time.monotonic() - started) * 1000), now + 1)
        for descriptor, _ in ready:
            owner = owners.get(descriptor)
            if owner is None:
                continue
            each = owner
            if descriptor == each.events_fd:
                if populated(descriptor):
                    each.seen = True
                elif each.seen and each.ended_at is None:
                    each.ended_at = now
                continue
            reap(each)
            for done in (each.keeper_fd, each.events_fd):
                assert done is not None
                poller.unregister(done)
                del owners[done]
            os.close(descriptor)
            if each.ended_at is None:
                each.ended_at = now
            stopper = let_go(each, running, stopper, held)
    for stand_in in held:
        close_quietly(stand_in)


def let_go(
    each: Running, running: list[Running], stopper: str | None, held: list[int]
) -> str | None:
    """Once a program has ended, stop the others if it is the first whose
    ending stops them, then close its stand-ins, or put them in `held` when
    it stopped the others; return the stopper, if any."""
    if stopper is None and stops(each):
        stopper = stop_others(each, running)
        held.extend(each.ends.stand_ins)
    else:
        for stand_in in each.ends.stand_ins:
            close_quietly(stand_in)
    return stopper


def populated(descriptor: int) -> bool:
    """Whether a box's cgroup holds a process, from its `cgroup.events`,
    read again from the start, which also readies it to wake the next
    poll."""
    os.lseek(descriptor, 0, os.SEEK_SET)
    lines = os.read(descriptor, 4096).decode().splitlines()
    return "populated 1" in lines


def stops(each: Running) -> bool:
    """Whether a program that ended stops the others: it was not stopped
    itself, and it ended over a limit of its own or has `stops_others`."""
    assert each.run is not None
    if each.run.interrupted:
        return False
    over = judge(each.run, each.kind) in OVER_LIMIT
    return over or each.entry.stops_others


def stop_others(each: Running, running: list[Running]) -> str:
    """Stop every program still running for `each`, by asking its isolate,
    which stops the box and says so in its meta file, and return the
    stopper's name."""
    for other in running:
        if other.run is None and other.keeper is not None:
            other.stopped_by = each.entry.name
            other.keeper.send_signal(signal.SIGTERM)
    return each.entry.name


def reap(each: Running) -> None:
    """Read what one program did once its isolate has exited, its box still
    there. Its `stopped_by` stands where isolate says it stopped it, and is
    the wall clock where that stopped it."""
    assert each.keeper is not None
    code = each.keeper.wait()
    if each.reader is not None:
        each.reader.join()
    run = finish(each, code)
    each.run = replace(run, no_inodes=os.statvfs(each.box).f_ffree == 0)
    if not each.run.interrupted:
        each.stopped_by = WALL_CLOCK if each.run.wall_clock else None


def finish(each: Running, code: int) -> Run:
    """What a program did, from its isolate's exit `code` and meta file, as
    exec reads them. An isolate stopped before it could take a stop as one,
    which kills it before its box is made, is a program stopped before it
    ran. isolate failing itself (`XX`) is an error of the step.
    """
    found = read_meta(each.meta)
    status = found.get("status", "")
    asked = each.stopped_by is not None
    if asked and code == -signal.SIGTERM and not found:
        return Run("SG", -signal.SIGKILL, 0.0, 0, 0, interrupted=True)
    if code not in (0, 1) or status == "XX":
        said = found.get("message") or each.errors.read_bytes()
        raise PrimitiveError(
            f"isolate could not run the program {each.entry.name}: "
            f"{isolate_said(code, said)}"
        )
    interrupted = asked and found.get("message") == INTERRUPTED
    if interrupted:
        returncode = -signal.SIGKILL
    else:
        returncode = exit_code(found, 0 if status == "" else -9)
    return Run(
        status=status,
        returncode=returncode,
        cpu_seconds=float(found.get("time", "0")),
        memory_kb=int(found.get("cg-mem", "0")),
        output_bytes=each.printed[0],
        out_of_memory=found.get("cg-oom-killed") == "1",
        interrupted=interrupted,
        wall_clock=status == "TO" and found.get("message") == WALL_CLOCK_MESSAGE,
    )


def program_facts(each: Running, item: Item) -> dict[str, object]:
    """One program's facts, its box still there: its time and memory, how it
    ended, `finished` for a clean exit and otherwise its outcome as exec
    judges a run, the signal that ended it, when it ended and what stopped
    it; and for a program given a manifest, its report.

    The report is read as exec reads one, however the program ended, so a
    verdict written before it was stopped stands: its `verdict` as written,
    its texts, its `reports` on a verdict of `0`, and every other file as
    `extras`. A program that finished with a report that does not hold did
    not judge, an error of the step, as under exec; one that did not finish
    has its report if it holds, and none if not.
    """
    assert each.run is not None and each.ended_at is not None
    run, entry = each.run, each.entry
    outcome = judge(run, each.kind)
    time_ms = round(run.cpu_seconds * 1000)
    if outcome == "time_limit":
        time_ms = max(time_ms, entry.time_limit_ms)
    facts: dict[str, object] = {
        "time_ms": time_ms,
        "memory_kb": run.memory_kb,
        "ended": "finished" if outcome == "accepted" else outcome,
    }
    if run.returncode < 0:
        facts["signal"] = signal_name(-run.returncode)
    facts["ended_at"] = each.ended_at
    if each.stopped_by is not None:
        facts["stopped_by"] = each.stopped_by
    if entry.manifest is None:
        return facts
    try:
        report = read_report(each.box / "report", entry.manifest)
    except ReportError as fault:
        if outcome == "accepted":
            raise PrimitiveError(
                f"the report of program {entry.name} of run {item.run} {fault}"
            ) from None
        return facts
    facts["verdict"] = report.verdict
    facts |= {name: report.outputs[name] for name in TEXTS if name in report.outputs}
    reports = {
        name: value for name, value in report.outputs.items() if name not in TEXTS
    }
    if reports:
        facts["reports"] = reports
    if report.shown:
        facts["extras"] = {
            name: content.decode() for name, content in report.shown.items()
        }
    return facts


def signal_name(number: int) -> str:
    """A signal's name, such as `SIGPIPE`."""
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"signal {number}"
