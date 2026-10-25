#!/usr/local/bin/python3 -I
"""The exec primitive: run one program under limits and measure the run, once
per item of a batch.

The harness mounts a working directory at `/work` (or the directory named by
the first argument) holding `inputs.json` and the files under `in/`. The
inputs are a batch: one item per run, each naming the program, an optional
file to feed it on standard input, optional named files to place in its
working directory, optional arguments for its command line, a time limit in
seconds and a memory limit in megabytes. For every item, in the
order given, it runs the program, keeps what it printed as
`out/<n>/output`, `n` being the item's place in the batch, and reports the
CPU time, the peak memory and an outcome, then writes `outputs.json` with one
entry per item in the same order. The order is the forge's: it runs the runs
whose results entrants see sooner first, so a run can tell nothing to one
whose results are shown before its own.

A run that goes over a limit is an ordinary result (`time_limit`,
`memory_limit`, `output_limit`, `runtime_error`), never an error of the step,
and every item reports its time and memory whatever its outcome.
`error` in `outputs.json` is kept for the cases where the primitive could not
work at all, such as a missing input or a program in no format it runs.

Each run is a fresh process in a fresh empty directory, running a fresh copy
of the program, with every process it left behind killed, and every System V
shared memory segment, message queue and semaphore set and every `user`
extended attribute under the working directory it left removed, before the
next item starts. Every run is put under
Landlock: it may write only inside that directory, its report folder if it
has one, and to /dev/null, read only the system's own directories, its
program and those, and may not
trace anything outside itself, and from Landlock ABI 6 it may not signal
anything outside itself either. A kernel without Landlock ABI 3 is an error of
the step: the runs are not started less confined.

A run given a `manifest`, an organiser's program such as a checker, also gets
an empty folder of its own outside its working directory, named in
`$UNICON_REPORT`, and writes there one file per value it reports: `verdict`,
`0` for accepted or `1` for a wrong answer, and each report the manifest
declares. Its reports become outputs of their own names on `0`, and a report
that does not hold, or a run of it that crashes or breaks a limit, is an error
of the step, since the program did not judge; for a `self_reported` run, one
that runs the entrant's code, it is the run's `runtime_error` or limit
instead.
"""

import ctypes
import errno
import json
import math
import os
import re
import resource
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from stat import S_IMODE, S_ISDIR, S_ISLNK, S_ISREG
from typing import BinaryIO

SCHEMA_VERSION = 6
OUTPUT_LIMIT = 32 * 1024 * 1024
POLL_SECONDS = 0.005
TOTAL_SPACING = 4.0
"""How long to wait before reading a run's total memory again, as a multiple
of how long the last reading took. A reading walks every page the run holds,
about 25 microseconds a megabyte where it was measured, so at 4 this
program spends at most a fifth of its time on it however much memory the run
holds, and the run, which shares the container's one CPU with it, keeps the
rest.
"""
STRAY_SECONDS = 1.0
"""How long a stopped run's helper has to report before its whole group is
killed."""
REAP_SECONDS = 5.0
OWNER_FOLDER = 0o700
OWNER_FILE = 0o600
OWNER_PROGRAM = 0o700
OOM_EVENTS = Path("/sys/fs/cgroup/memory.events")
PIDS_MAX = Path("/sys/fs/cgroup/pids.max")
PROCESS_LIMIT = 128
"""The most processes and threads a run's user may have at once, `pids` in
`primitive.yaml`, when the container's cgroup sets none lower: a Woodpecker
step's container has no pids limit of its own."""
CGROUP = Path("/sys/fs/cgroup")
NO_CGROUP = (
    "exec: this program is not alone in a cgroup of its own with cgroup v2 "
    "memory accounting, so memory a run keeps outside its processes, in files, "
    "pipes or sockets, is not counted against its limit"
)
JAVA_OUT_OF_MEMORY = 3
RUN_PATH = "/usr/local/bin:/usr/bin:/bin"
HELPER = "/usr/local/bin/sandbox-exec"
FILE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
"""A name a named file is placed under in the run's working directory: never
`.` or `..`, never a path, never hidden."""
NAMED_FILE_MODE = 0o444
RUN_ID = re.compile(r"[A-Za-z0-9_-]+")
"""A run's id, which only the forge knows the meaning of. An item's output
goes under its place in the batch, not its id."""
REPORT_LINE = re.compile(rb"([PSLER]) (-?[0-9]{1,20}(?: -?[0-9]{1,20}){0,3})\n")
REFUSED_BINARY = frozenset(
    {errno.ENOEXEC, errno.ENOMEM, errno.E2BIG, errno.EINVAL, errno.ELIBBAD}
)
"""What the kernel answers when it will not run a native program as built: the
contestant's program, not the step, failed.
"""
REFUSED_ARGUMENTS = frozenset({errno.E2BIG})
"""What the kernel answers when the command line is too long for it, which
only `args` can make it: the contestant's input, not the step, failed.
"""
ELF_MAGIC = b"\x7fELF"

PR_SET_DUMPABLE = 4
PR_SET_CHILD_SUBREAPER = 36
SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1
MINIMUM_LANDLOCK_ABI = 3
"""Reads and writes can both be confined from the first Landlock ABI, Linux
5.13, but below ABI 3, Linux 6.2, `truncate` of a file named by path is not,
so a run could empty a file it may only read. Renaming across directories is
confined from ABI 2, and signals from ABI 6, which a run is kept from on top.
"""
FS_WRITE_FILE = 1 << 1
FS_READ_FILE = 1 << 2
FS_READ_DIR = 1 << 3
FS_WRITES = sum(1 << bit for bit in (1, 4, 5, 6, 7, 8, 9, 10, 11, 12))
FS_READS = FS_READ_FILE | FS_READ_DIR
FS_REFER = 1 << 13
FS_TRUNCATE = 1 << 14
READABLE_DIRECTORIES = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/etc",
    "/proc",
    "/sys",
)
"""What a run may read besides its program and its own directory: the system's
programs, libraries and settings, which the interpreters need (Java its
configuration under /etc, its container support /proc/self and
/sys/fs/cgroup), and nothing under /work or /tmp. The image holds nothing
secret there. Those that do not exist on the machine are passed over.
"""
READABLE_DEVICES = ("/dev/zero", "/dev/random", "/dev/urandom")
"""Devices a run may read besides /dev/null, which it may also write."""
SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0
SCOPE_SIGNAL = 1 << 1
SYSVIPC = Path("/proc/sysvipc")
"""The kernel's tables of the System V objects in this program's IPC
namespace, the step container's own, one line each after a header."""
NO_SYSVIPC = (
    "exec: this machine shows no /proc/sysvipc, so System V objects a run "
    "leaves cannot be found and removed; a kernel without them has none to leave"
)
IPC_RMID = 0
GONE = frozenset({errno.EINVAL, errno.EIDRM})
"""What removing a System V object answers when it is already gone."""
NO_ATTRIBUTES = frozenset({errno.ENOENT, errno.ENOTSUP, errno.EOPNOTSUPP})
"""What listing or removing an attribute answers for a path that is gone, or
on a filesystem without attributes."""
REPORT_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
"""A report a manifest declares, which becomes an output of that name and a
file of that name in the report folder."""
OWN_NAMES = frozenset(
    {
        "verdict",
        "label",
        "message",
        "staff_message",
        "output",
        "time_ms",
        "memory_kb",
        "outcome",
        "extras",
    }
)
"""What no report may be named: the report folder's own files and exec's own
outputs."""
REPORT_KEYS = frozenset({"type", "optional", "better", "at_least", "at_most"})
TEXTS = ("label", "message", "staff_message")
"""The text files a report may hold besides its reports, each an output of
its name: a finer name for the outcome, a message for the entrant, and one for
staff alone."""
REPORT_TEXT_LIMIT = 64 * 1024
"""The most bytes one file in a report folder may hold."""
REPORT_FILES = 64
"""The most files a report folder may hold."""
DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
"""A reported number, as JSON writes one: so `1e-05`, as Python prints a small
float, is one, and `nan`, `inf` and `0x10` are not."""
WHOLE = re.compile(r"-?(?:0|[1-9][0-9]*)")


class PrimitiveError(Exception):
    """The primitive could not do its work at all.

    The message is one sentence for a person and becomes `error` in
    `outputs.json`, which the harness turns into a `system_error`.
    """


class ReportError(Exception):
    """What a run given a manifest reported does not hold: the end of a
    sentence that begins with the run's report."""


@dataclass(frozen=True)
class Declared:
    """One report a manifest declares: a `number`, with the bounds every value
    lies within, both inclusive, or a `text`; and whether a verdict of `0` may
    leave it out. Which way is better is the forge's to carry, not exec's to
    check.
    """

    kind: str
    optional: bool = False
    at_least: Decimal | None = None
    at_most: Decimal | None = None


@dataclass(frozen=True)
class Report:
    """What a run given a manifest reported: whether it judged the run
    accepted, the outputs it gives, and the files only shown with the run, by
    name."""

    accepted: bool
    outputs: dict[str, object]
    shown: dict[str, bytes]


@dataclass(frozen=True)
class Item:
    """One run: its id, its place in the batch from 1, the program, the file
    for standard input if any, the two limits, the arguments, and the named
    files placed in its working directory: `files` it reads, and `loads`, code
    the program loads, such as the entrant's module a test harness imports.
    `manifest` is the reports the program declares, when it is one that
    reports, and `self_reported` says it runs the entrant's code.
    """

    run: str
    number: int
    program: Path
    stdin: Path | None
    time_limit: float
    memory_limit: float
    args: tuple[str, ...] = ()
    files: dict[str, Path] = field(default_factory=dict)
    loads: dict[str, Path] = field(default_factory=dict)
    manifest: dict[str, Declared] | None = None
    self_reported: bool = False

    @property
    def wall_limit(self) -> float:
        """The wall-clock seconds a run may take: twice the time limit plus one.

        The time limit itself is on CPU time; the wall-clock limit stops a run
        that sleeps or waits without using the CPU.
        """
        return 2 * self.time_limit + 1

    @property
    def memory_limit_kb(self) -> int:
        """The memory limit in kilobytes, the unit peak memory is measured in."""
        return int(self.memory_limit * 1024)

    @property
    def time_limit_ms(self) -> int:
        """The time limit in whole milliseconds, rounded up: the least time a
        run stopped at it reports. Rounding to a microsecond first keeps a
        limit such as 0.1 s, a little over 100 ms as a float, at 100.
        """
        return math.ceil(round(self.time_limit * 1000, 3))


@dataclass(frozen=True)
class Program:
    """A program ready to run: its format and the file the command starts."""

    kind: str
    path: Path

    def readable(self) -> list[Path]:
        """What a run of this program reads besides the system's directories:
        the program, and for Python the interpreter's own installation, which
        is under /usr in the image. The interpreter is started by its real
        path, outside any virtual environment this program runs in, so it
        reads no `pyvenv.cfg` it could not be allowed.
        """
        if self.kind == "python":
            return [self.path, Path(sys.base_prefix)]
        return [self.path]

    @property
    def mode(self) -> int:
        """The access its file's owner needs: running it too, for a native
        program the kernel starts itself.
        """
        return OWNER_PROGRAM if self.kind == "native" else OWNER_FILE

    def command(self, item: Item) -> list[str]:
        """The command line that runs this program for one item, the item's
        arguments last.
        """
        if self.kind == "native":
            return [str(self.path), *item.args]
        if self.kind == "python":
            interpreter = os.path.realpath(sys.executable)
            return [interpreter, "-I", "-B", str(self.path), *item.args]
        java = shutil.which("java", path=RUN_PATH)
        if java is None:
            raise PrimitiveError("this image has no Java runtime to run a jar with")
        return [
            java,
            f"-Xmx{max(16, int(item.memory_limit))}m",
            "-Xss64m",
            "-XX:+UseSerialGC",
            "-XX:-UsePerfData",
            "-XX:+ExitOnOutOfMemoryError",
            "-Djava.io.tmpdir=.",
            "-jar",
            str(self.path),
            *item.args,
        ]


@dataclass(frozen=True)
class Run:
    """What one run did: how it ended and what it used.

    `returncode` is the exit code, or minus the signal that killed it.
    `stopped` names the limit this program stopped it for, if any.
    """

    returncode: int
    cpu_seconds: float
    wall_seconds: float
    memory_kb: int
    output_bytes: int
    stopped: str | None
    out_of_memory: bool = False


@dataclass
class RunMemory:
    """The memory of one run's processes, read while the run goes on.

    `peak_kb` is the highest of three readings, in kilobytes. One is the peak
    resident memory of each process alone, which the kernel keeps itself
    (`VmHWM`), so no process's own peak between two readings is missed. The
    second is the resident memory of all the run's processes added up, with
    each page several of them share divided among them (`Pss`): a child
    forked from the program shares its parent's pages until it changes them,
    so it is charged only for the pages it changes, while two processes that
    each fill memory of their own are charged for both. The third is what
    `cgroup`, the step container's, holds beyond what it held when the
    reading began, before the run started (`held_kb`): that counts what no
    process's pages do, the files a run keeps in its own directory, which is
    in a tmpfs, files it deleted and keeps open, memory files, and pipe and
    socket buffers, and only one run goes at a time. The kernel keeps no peak
    of the second and the third, so memory held for less than the time
    between two readings is not seen; the container's own memory limit holds
    it.

    Reading the total walks every page the run holds, so it is read only
    when it could raise the peak, that is when the processes' resident memory
    added up, which the total never exceeds, is above the peak: never for a
    run of one process. It is read again only once `TOTAL_SPACING` times as
    long as the last reading took has passed.
    """

    cgroup: Path | None = None
    peak_kb: int = 0
    total_due: float = 0.0
    held_before: int | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        if self.cgroup is not None:
            self.held_before = held_kb(self.cgroup)

    def read(self, processes: set[int]) -> None:
        """Read the memory of the run's processes and raise the peak."""
        held = held_kb(self.cgroup) if self.cgroup is not None else None
        if held is not None and self.held_before is not None:
            self.peak_kb = max(self.peak_kb, held - self.held_before)
        resident: dict[int, int] = {}
        for pid in processes:
            highest, resident[pid] = resident_memory(pid)
            self.peak_kb = max(self.peak_kb, highest)
        started = time.monotonic()
        if sum(resident.values()) <= self.peak_kb or started < self.total_due:
            return
        total = sum(proportional_memory(pid, kb) for pid, kb in resident.items())
        self.peak_kb = max(self.peak_kb, total)
        ended = time.monotonic()
        self.total_due = ended + TOTAL_SPACING * (ended - started)


def main(argv: list[str]) -> int:
    """Run every item `inputs.json` names and write `outputs.json`.

    Exits 0 whenever `outputs.json` was written, error or not; the harness
    reads the outcomes and any error from that file.
    """
    root = Path(argv[1]) if len(argv) > 1 else Path("/work")
    try:
        items = read_inputs(root)
        document: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "batch": run_batch(root, items),
        }
    except PrimitiveError as error:
        print(f"exec: {error}", file=sys.stderr)
        document = {"schema_version": SCHEMA_VERSION, "error": str(error)}
    write_json(root / "outputs.json", document)
    return 0


def read_inputs(root: Path) -> list[Item]:
    """Read `inputs.json` and return its items in order."""
    try:
        document = json.loads((root / "inputs.json").read_bytes())
    except FileNotFoundError:
        raise PrimitiveError("inputs.json is not in the working directory") from None
    except ValueError:
        raise PrimitiveError("inputs.json is not valid JSON") from None
    if not isinstance(document, dict):
        raise PrimitiveError("inputs.json is not a JSON object")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise PrimitiveError(
            f"inputs.json is not written against contract version {SCHEMA_VERSION}"
        )
    batch = document.get("batch")
    if not isinstance(batch, list):
        raise PrimitiveError("inputs.json has no batch list")
    items = [read_item(root, entry, number) for number, entry in enumerate(batch, 1)]
    if len({item.run for item in items}) != len(items):
        raise PrimitiveError("two items in the batch are for the same run")
    return items


def read_item(root: Path, entry: object, number: int) -> Item:
    """Check one batch entry, the `number`th, and return it as an item."""
    if not isinstance(entry, dict) or not isinstance(entry.get("inputs"), dict):
        raise PrimitiveError("a batch item has no inputs object")
    run = entry.get("run")
    if not isinstance(run, str) or len(run) > 255 or not RUN_ID.fullmatch(run):
        raise PrimitiveError(f"the batch item run {run!r} is not a run id")
    inputs = entry["inputs"]
    files = named_files(root, inputs, "files", run)
    loads = named_files(root, inputs, "loads", run)
    clash = sorted(files.keys() & loads.keys())
    if clash:
        raise PrimitiveError(
            f"the run {run} names {clash[0]} in both files and loads; each name "
            "is one file in its working directory"
        )
    manifest = None
    if "manifest" in inputs:
        manifest = read_manifest(
            input_file(root, inputs, "manifest", run),
            f"the input named manifest of run {run}",
        )
    self_reported = inputs.get("self_reported", False)
    if not isinstance(self_reported, bool):
        raise PrimitiveError(
            f"the input named self_reported of run {run} is not true or false"
        )
    if self_reported and manifest is None:
        raise PrimitiveError(
            f"the run {run} is self_reported without a manifest, so it reports nothing"
        )
    return Item(
        run=run,
        number=number,
        program=input_file(root, inputs, "program", run),
        stdin=input_file(root, inputs, "stdin", run) if "stdin" in inputs else None,
        time_limit=positive_number(inputs, "time_limit", run),
        memory_limit=positive_number(inputs, "memory_limit", run),
        args=arguments(inputs, run),
        files=files,
        loads=loads,
        manifest=manifest,
        self_reported=self_reported,
    )


def read_manifest(path: Path, what: str) -> dict[str, Declared]:
    """Read a manifest, the reports a program declares in its `program.yaml`,
    as the forge gives it: that file's JSON form, `{"reports": {<name>:
    {"type": "number", "optional": false, "better": "higher", "at_least": 0,
    "at_most": 1}}}`, `type` `number` when left out. The forge reads the YAML
    with its own parser and checks it at the save, so exec has no second
    parser to differ from it; it checks again what it relies on.
    """
    try:
        document = json.loads(
            path.read_bytes(),
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=refuse_constant,
        )
    except ValueError, RecursionError:
        raise PrimitiveError(f"{what} is not valid JSON") from None
    except ArithmeticError:
        raise PrimitiveError(f"{what} holds a number too large to read") from None
    if not isinstance(document, dict) or set(document) != {"reports"}:
        raise PrimitiveError(f"{what} is not an object holding reports alone")
    reports = document["reports"]
    if not isinstance(reports, dict):
        raise PrimitiveError(f"{what} has reports that are not an object")
    return {name: declared(name, port, what) for name, port in reports.items()}


def refuse_constant(constant: str) -> object:
    """Refuse `NaN` and `Infinity`, which Python's JSON reader takes but JSON
    has not."""
    raise ValueError(f"{constant} is not JSON")


def declared(name: str, port: object, what: str) -> Declared:
    """Check one report a manifest declares, `name`, and return it."""
    if not REPORT_NAME.fullmatch(name):
        raise PrimitiveError(f"{what} declares {name!r}, not a report name")
    if name in OWN_NAMES:
        raise PrimitiveError(f"{what} declares {name}, a name exec gives already")
    if not isinstance(port, dict) or not set(port) <= REPORT_KEYS:
        raise PrimitiveError(f"{what} declares {name} with keys it does not know")
    kind = port.get("type", "number")
    optional = port.get("optional", False)
    at_least, at_most = port.get("at_least"), port.get("at_most")
    if kind not in ("number", "text") or not isinstance(optional, bool):
        raise PrimitiveError(f"{what} declares {name} as neither a number nor a text")
    if kind == "text" and set(port) & {"better", "at_least", "at_most"}:
        raise PrimitiveError(f"{what} gives the text {name} a direction or a bound")
    if "better" in port and port["better"] not in ("higher", "lower"):
        raise PrimitiveError(f"{what} says {name} is better neither higher nor lower")
    for key in ("at_least", "at_most"):
        if key in port and not (
            isinstance(port[key], Decimal) and math.isfinite(float(port[key]))
        ):
            raise PrimitiveError(f"{what} bounds {name} by something not a number")
    if at_least is not None and at_most is not None and at_least > at_most:
        raise PrimitiveError(f"{what} bounds {name} at least above at most")
    return Declared(kind, optional, at_least, at_most)


def input_file(root: Path, inputs: dict[str, object], name: str, run: str) -> Path:
    """Resolve a file input to a path, refusing anything outside `in/`."""
    return in_file(root, inputs.get(name), f"the input named {name} of run {run}")


def in_file(root: Path, value: object, what: str) -> Path:
    """Resolve one file value, `{"file": "in/..."}`, to a path under `in/`."""
    if not isinstance(value, dict) or not isinstance(value.get("file"), str):
        raise PrimitiveError(f"{what} is not a file")
    path = (root / str(value["file"])).resolve()
    if not path.is_relative_to((root / "in").resolve()):
        raise PrimitiveError(f"{what} is outside in/")
    if not path.is_file():
        raise PrimitiveError(f"{what} is not in the working directory")
    return path


def named_files(
    root: Path, inputs: dict[str, object], name: str, run: str
) -> dict[str, Path]:
    """Read a `named_files` input, `{"files": {<name>: {"file": "in/..."}}}`,
    as each name and its path under `in/`. Absent, there are none.
    """
    if name not in inputs:
        return {}
    value = inputs[name]
    what = f"the input named {name} of run {run}"
    if not isinstance(value, dict) or set(value) != {"files"}:
        raise PrimitiveError(f"{what} is not named files")
    files = value["files"]
    if not isinstance(files, dict):
        raise PrimitiveError(f"{what} is not named files")
    found = {}
    for file_name, file in files.items():
        if len(file_name) > 255 or not FILE_NAME.fullmatch(file_name):
            raise PrimitiveError(f"{what} names a file {file_name!r}, not a plain name")
        found[file_name] = in_file(root, file, f"the file {file_name} of {what}")
    return found


def positive_number(inputs: dict[str, object], name: str, run: str) -> float:
    """Read a number input that must be above zero."""
    value = inputs.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise PrimitiveError(
            f"the input named {name} of run {run} is not a positive number"
        )
    return float(value)


def arguments(inputs: dict[str, object], run: str) -> tuple[str, ...]:
    """Read `args`, the optional text appended to the program's command line,
    split on spaces: a run of spaces separates two arguments, and spaces at
    either end separate nothing. There is no quoting, so no argument holds a
    space. Absent, the program gets no arguments.
    """
    value = inputs.get("args")
    if value is None:
        return ()
    if not isinstance(value, str):
        raise PrimitiveError(f"the input named args of run {run} is not text")
    return tuple(part for part in value.split(" ") if part)


def run_batch(root: Path, items: list[Item]) -> list[dict[str, object]]:
    """Run every item in order and return one outputs entry per item.

    This program makes itself the subreaper of everything it starts, and not
    dumpable, so no run can trace it or open its memory or file descriptors
    even without Landlock. It ignores SIGINT, the one signal Python would act
    on: as the container's first process the kernel already keeps every
    signal it has no handler for away from it, so a run cannot stop it.
    """
    prctl(PR_SET_CHILD_SUBREAPER, 1)
    prctl(PR_SET_DUMPABLE, 0)
    if own_cgroup() is None:
        print(NO_CGROUP, file=sys.stderr)
    if not SYSVIPC.is_dir():
        print(NO_SYSVIPC, file=sys.stderr)
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        return run_all(root, items)
    finally:
        signal.signal(signal.SIGINT, previous)


def run_all(root: Path, items: list[Item]) -> list[dict[str, object]]:
    """Run the items one after another, in the order given, each on a copy of
    its program of its own.

    A run may take this program's access away from the program under `in/`
    and from the scratch folder its copy is in, both of which it can name, so
    each is given back before the copy is made and before it is removed: a
    run that could make the next copy fail would end the whole batch in an
    error, which a run whose results are shown later could do or not by what
    its input holds.

    Nothing runs unless the kernel offers Landlock at `MINIMUM_LANDLOCK_ABI`
    or later: without it a run could read the other runs' inputs and outputs
    and write the files `outputs.json` is built from.
    """
    abi = landlock_abi()
    if abi < MINIMUM_LANDLOCK_ABI:
        raise PrimitiveError(
            "this machine's kernel does not offer Landlock ABI 3, which every run "
            "is confined with; it needs Linux 6.2 or later with Landlock enabled"
        )
    scratch = Path(tempfile.mkdtemp(prefix=".exec-", dir=root))
    kinds: dict[Path, str] = {}
    results: list[dict[str, object]] = []
    try:
        for item in items:
            give_back_along(root, item.program)
            give_back(scratch)
            if item.program not in kinds:
                kinds[item.program] = detect(item.program)
            program = prepare(item, kinds[item.program], scratch)
            try:
                outputs = run_item(root, item, program, abi)
            finally:
                give_back(scratch)
                program.path.unlink(missing_ok=True)
            results.append({"run": item.run, "outputs": outputs})
    finally:
        give_back_tree(root)
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


def prepare(item: Item, kind: str, scratch: Path) -> Program:
    """Get the item's program, of format `kind`, ready to run: copied into the
    scratch directory for this item alone, and for a native executable marked
    executable there, since the file under `in/` may not be.

    A run is this program's user, so it may set an attribute, a mode or a
    time on the file it runs, which Landlock does not stop; a copy of its own,
    removed after it, leaves none of them for the next item. The copy is made
    before the item's clock starts, so its time is not the run's.
    """
    path = scratch / f"program-{item.number}"
    shutil.copyfile(item.program, path)
    if kind == "native":
        path.chmod(0o755)
    return Program(kind, path)


def run_item(root: Path, item: Item, program: Program, abi: int) -> dict[str, object]:
    """Run one item and return its outputs, all of them whatever the outcome:
    what the program printed, its CPU time and its peak memory.

    The time is the CPU time, and on `time_limit` at least the limit: a run
    the wall clock stopped, one that slept or waited, used little CPU. An
    argument holding a NUL character, which no command line can carry, is a
    run that could not start, a `runtime_error`, like a program the kernel
    will not run.
    """
    give_back_along(root, root / "out")
    if item.stdin is not None:
        give_back_along(root, item.stdin)
    give_back_along(root, program.path, program.mode)
    out = root / "out" / str(item.number)
    out.mkdir(parents=True, exist_ok=True)
    output = out / "output"
    report = None
    if item.manifest is not None:
        report = Path(tempfile.mkdtemp(prefix="report-"))
    try:
        cwd = Path(tempfile.mkdtemp(prefix="run-"))
        try:
            place_files(root, cwd, item.files | item.loads)
            descriptor = os.open(
                output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644
            )
            stdin_path = item.stdin if item.stdin is not None else Path(os.devnull)
            with stdin_path.open("rb") as stdin, os.fdopen(descriptor, "wb") as stdout:
                if any("\0" in argument for argument in item.args):
                    run = Run(127, 0.0, 0.0, 0, 0, None)
                else:
                    run = execute(program, cwd, report, stdin, stdout, item, abi)
        finally:
            reap_strays()
            sweep_sysv()
            clear_attributes(root)
            give_back_tree(cwd)
            shutil.rmtree(cwd, ignore_errors=True)
            give_back_along(root, output)
        if run.output_bytes > OUTPUT_LIMIT:
            os.truncate(output, OUTPUT_LIMIT)
        outcome = judge(run, item, program.kind)
        time_ms = round(run.cpu_seconds * 1000)
        if outcome == "time_limit":
            time_ms = max(time_ms, item.time_limit_ms)
        outputs: dict[str, object] = {
            "output": {"file": f"out/{item.number}/output"},
            "time_ms": time_ms,
            "memory_kb": run.memory_kb,
            "outcome": outcome,
        }
        if report is not None:
            outputs |= reported(item, outcome, report, out)
    finally:
        if report is not None:
            give_back_tree(report)
            shutil.rmtree(report, ignore_errors=True)
    return outputs


def reported(item: Item, outcome: str, folder: Path, out: Path) -> dict[str, object]:
    """The outputs a run given a manifest adds from its report `folder`, and
    the outcome its verdict gives, the run's processes all gone.

    Each report is an output on a verdict of `0` alone, as an output that
    counts is written only on `accepted`; the texts in `TEXTS` are outputs on
    either verdict, and every other file is copied under `out/extras/`, shown
    with the run and never a column, as are the reports under `1`.

    A run that did not end `accepted` did not judge, nor did one whose
    report does not hold: an error of the step, retried and then left for
    staff, unless the run is `self_reported`, when what the entrant's code
    did is the run's own outcome, its limit or a `runtime_error`, with no
    report.
    """
    if outcome != "accepted":
        if item.self_reported:
            return {}
        raise PrimitiveError(
            f"the program given a manifest for run {item.run} did not judge: it "
            f"ended in {outcome.replace('_', ' ')}"
        )
    try:
        report = read_report(folder, item.manifest or {})
    except ReportError as fault:
        if item.self_reported:
            return {"outcome": "runtime_error"}
        raise PrimitiveError(f"the report of run {item.run} {fault}") from None
    outputs = dict(report.outputs)
    if report.shown:
        extras = out / "extras"
        extras.mkdir(exist_ok=True)
        for name, content in report.shown.items():
            (extras / name).write_bytes(content)
        outputs["extras"] = {"folder": f"out/{item.number}/extras"}
    outputs["outcome"] = verdict_outcome(report.accepted)
    return outputs


def verdict_outcome(accepted: bool) -> str:
    """The outcome a report's verdict gives: `0`, judged and counted, is
    `accepted`, and `1` is `wrong_answer`."""
    return "accepted" if accepted else "wrong_answer"


def read_report(folder: Path, manifest: dict[str, Declared]) -> Report:
    """Read what a run wrote to its report folder, against what its manifest
    declares, raising `ReportError` when it does not hold: no `verdict`, or
    one that is not `0` or `1`; under `0`, a declared report that is not
    optional missing, or a number that is not a finite decimal or lies
    outside its bounds; on either verdict, a file that is not UTF-8 text
    without a NUL, as every file is shown as text, a file over
    `REPORT_TEXT_LIMIT`, more than `REPORT_FILES` of them, or anything there
    that is not a plain file under a plain name.
    """
    give_back_tree(folder)
    files = report_files(folder)
    if "verdict" not in files:
        raise ReportError("has no verdict")
    verdict = files.pop("verdict").strip()
    if verdict not in (b"0", b"1"):
        raise ReportError(f"has a verdict {shown_bytes(verdict)}, not 0 or 1")
    accepted = verdict == b"0"
    outputs: dict[str, object] = {}
    for name in TEXTS:
        if name in files:
            outputs[name] = report_text(name, files.pop(name))
    if accepted:
        for name, port in manifest.items():
            if name not in files:
                if not port.optional:
                    raise ReportError(f"has no {name}, which the manifest declares")
                continue
            content = files.pop(name)
            if port.kind == "text":
                outputs[name] = report_text(name, content)
            else:
                outputs[name] = report_number(name, content, port)
    for name, content in files.items():
        report_text(name, content)
    return Report(accepted, outputs, files)


def report_files(folder: Path) -> dict[str, bytes]:
    """Every file in a report folder by name, each read whole, none through a
    link: a run that left a link there to a file it may not read, the next
    run's input, would have exec read that file for it. The folder is listed
    only as far as one file past `REPORT_FILES`, so a run that left a
    million costs exec no more.
    """
    entries: list[str] = []
    try:
        with os.scandir(folder) as listing:
            for entry in listing:
                entries.append(entry.name)
                if len(entries) > REPORT_FILES:
                    raise ReportError(f"holds more than {REPORT_FILES} files")
    except OSError as error:
        raise ReportError(f"could not be listed: {error.strerror}") from None
    files: dict[str, bytes] = {}
    for name in sorted(entries):
        if len(name) > 255 or not FILE_NAME.fullmatch(name):
            shown = shown_bytes(os.fsencode(name))
            raise ReportError(f"holds {shown}, not a plain name")
        try:
            descriptor = os.open(
                folder / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            )
        except OSError:
            raise ReportError(f"holds {name}, not a plain file") from None
        if not S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise ReportError(f"holds {name}, not a plain file")
        with os.fdopen(descriptor, "rb") as stream:
            content = stream.read(REPORT_TEXT_LIMIT + 1)
        if len(content) > REPORT_TEXT_LIMIT:
            raise ReportError(f"holds {name}, over {REPORT_TEXT_LIMIT // 1024} KB")
        files[name] = content
    return files


def report_text(name: str, content: bytes) -> str:
    """One reported text: UTF-8 with no NUL, which no text the forge keeps may
    hold, its trailing newlines left off, as `echo` and `print` add one."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise ReportError(f"has {name} not in UTF-8") from None
    if "\0" in text:
        raise ReportError(f"has {name} holding a NUL character")
    return text.rstrip("\n")


def report_number(name: str, content: bytes, port: Declared) -> int | float:
    """One reported number: a decimal as JSON writes one, with spaces and
    newlines around it, finite as a double and within the declared bounds,
    compared as the decimal written. A whole number is given as one.
    """
    text = content.strip()
    if not DECIMAL.fullmatch(text.decode("ascii", "replace")):
        raise ReportError(f"has {name} {shown_bytes(text)}, not a decimal number")
    try:
        value = Decimal(text.decode("ascii"))
        finite = math.isfinite(float(value))
    except ArithmeticError:
        finite = False
    if not finite:
        raise ReportError(f"has {name} {shown_bytes(text)}, not a finite number")
    if port.at_least is not None and value < port.at_least:
        raise ReportError(f"has {name} {value}, below its least, {port.at_least}")
    if port.at_most is not None and value > port.at_most:
        raise ReportError(f"has {name} {value}, above its most, {port.at_most}")
    return int(value) if WHOLE.fullmatch(text.decode("ascii")) else float(value)


def shown_bytes(content: bytes) -> str:
    """What a run wrote, for an error staff read: quoted, and cut short."""
    text = content[:24].decode("utf-8", "replace")
    return repr(text + "...") if len(content) > 24 else repr(text)


def place_files(root: Path, cwd: Path, files: dict[str, Path]) -> None:
    """Copy each named file into the run's working directory under its name,
    readable and not writable, before the run's clock starts, so the program
    opens `answer` or `input` as a plain relative path.

    Each run gets copies of its own, removed with its directory, and nothing
    is read back from them, so a run that makes its copy writable again, as
    the copy's owner may, changes only what it reads itself. A source under
    `in/` an earlier run took this program's access away from is given back
    first, as the program is.
    """
    for name, source in files.items():
        give_back_along(root, source)
        placed = cwd / name
        shutil.copyfile(source, placed)
        placed.chmod(NAMED_FILE_MODE)


def execute(
    program: Program,
    cwd: Path,
    report_folder: Path | None,
    stdin: BinaryIO,
    stdout: BinaryIO,
    item: Item,
    abi: int,
) -> Run:
    """Start one run and watch it until it and everything it started are gone.

    The program is started through `sandbox-exec`, which forks it and reports
    its process id and, once it ends, its exit status, CPU time and peak
    memory. Every few milliseconds every process of the run is read: their
    CPU time together, their memory (see `RunMemory`), the wall-clock time
    and the output size, and the program is killed as soon as one is over its
    limit. Resource limits set before the start back this up: CPU time, file
    size, stack, and no core dumps.

    The memory reported is the higher of the program's own peak, as
    `sandbox-exec` saw it, and the highest this program read while the run
    went on.

    The CPU time reported is the program's own, as `sandbox-exec` saw it, plus
    that of every process the run left behind, which this program reaps, so a
    run cannot hide work in a child it never waits for. A run that kills
    `sandbox-exec` is judged on what this program measured itself.

    A command line too long for the kernel, which only the item's arguments
    can make, is a run that could not start, judged as a native program the
    kernel will not run is.

    A run given a `report_folder` may write there too, and finds it named in
    `UNICON_REPORT`.
    """
    env = {"PATH": RUN_PATH, "HOME": str(cwd), "TMPDIR": str(cwd), "LANG": "C.UTF-8"}
    writable = [cwd]
    if report_folder is not None:
        env["UNICON_REPORT"] = str(report_folder)
        writable.append(report_folder)
    try:
        ruleset = landlock_ruleset(writable, program.readable(), abi)
    except OSError as error:
        raise PrimitiveError(
            f"the run could not be confined with Landlock: {error.strerror}"
        ) from None
    read_end, write_end = os.pipe()
    passed = (write_end, ruleset)
    memory = RunMemory(cgroup=own_cgroup())
    started = time.monotonic()
    try:
        helper = subprocess.Popen(
            [HELPER, str(write_end), str(ruleset), *program.command(item)],
            cwd=cwd,
            env=env,
            stdin=stdin,
            stdout=stdout,
            stderr=subprocess.DEVNULL,
            pass_fds=passed,
            start_new_session=True,
            preexec_fn=child_setup(item, process_limit()),
        )
    except (OSError, subprocess.SubprocessError) as error:
        os.close(read_end)
        if isinstance(error, OSError) and error.errno in REFUSED_ARGUMENTS:
            return Run(127, 0.0, 0.0, 0, 0, None)
        raise PrimitiveError(f"the program could not be started: {error}") from None
    finally:
        os.close(write_end)
        os.close(ruleset)
    os.set_blocking(read_end, False)
    report = bytearray()
    pid = 0
    stopped: str | None = None
    stopped_at = 0.0
    killed_before = oom_kills()
    cpu = 0.0
    me = os.getpid()
    while True:
        report += drain(read_end)
        pid = pid or parse_report(report).get("P", [0])[0]
        done, status, _ = os.wait4(helper.pid, os.WNOHANG)
        if done:
            break
        cpu, processes = sample(me, helper.pid)
        memory.read(processes)
        now = time.monotonic()
        if stopped is None:
            if cpu > item.time_limit or now - started > item.wall_limit:
                stopped = "time"
            elif memory.peak_kb > item.memory_limit_kb:
                stopped = "memory"
            elif os.fstat(stdout.fileno()).st_size > OUTPUT_LIMIT:
                stopped = "output"
            if stopped:
                stopped_at = now
                kill(pid or -helper.pid)
                # The run may have stopped `sandbox-exec` with SIGSTOP where
                # Landlock does not scope signals, and a stopped helper never
                # reaps the program; woken, it reports at once, and whatever
                # is left goes after STRAY_SECONDS.
                wake(helper.pid)
        elif now - stopped_at > STRAY_SECONDS:
            kill(-helper.pid)
        time.sleep(POLL_SECONDS)
    wall = time.monotonic() - started
    helper.returncode = os.waitstatus_to_exitcode(status)
    report += drain(read_end)
    os.close(read_end)
    lines = parse_report(report)
    before_strays = children_cpu()
    reap_strays()
    strays = children_cpu() - before_strays
    if "S" in lines:
        raise PrimitiveError(
            "the run could not be put under its system call filter: "
            f"{os.strerror(lines['S'][0])}"
        )
    if "L" in lines:
        raise PrimitiveError(
            f"the run could not be confined with Landlock: {os.strerror(lines['L'][0])}"
        )
    if "E" in lines:
        refused = lines["E"][0]
        contestants = REFUSED_BINARY if program.kind == "native" else frozenset()
        if refused not in contestants | REFUSED_ARGUMENTS:
            raise PrimitiveError(
                f"the program could not be started: {os.strerror(refused)}"
            )
        return Run(127, 0.0, wall, 0, 0, stopped)
    if "R" in lines:
        ended, user_us, system_us, maxrss_kb = lines["R"]
        returncode = os.waitstatus_to_exitcode(ended)
        reported = (user_us + system_us) / 1e6
    elif os.WIFSIGNALED(status):
        returncode, reported, maxrss_kb = -signal.SIGKILL, 0.0, 0
    else:
        raise PrimitiveError("a run ended without sandbox-exec reporting what it used")
    return Run(
        returncode=returncode,
        cpu_seconds=max(reported + strays, cpu),
        wall_seconds=wall,
        memory_kb=max(maxrss_kb, memory.peak_kb),
        output_bytes=os.fstat(stdout.fileno()).st_size,
        stopped=stopped,
        out_of_memory=returncode == -signal.SIGKILL and oom_kills() > killed_before,
    )


def drain(fd: int) -> bytes:
    """Read whatever a non-blocking pipe holds right now."""
    data = bytearray()
    while True:
        try:
            chunk = os.read(fd, 4096)
        except BlockingIOError:
            break
        if not chunk:
            break
        data += chunk
    return bytes(data)


def parse_report(report: bytes | bytearray) -> dict[str, list[int]]:
    """The complete lines of `sandbox-exec`'s report, by tag, as their numbers.

    A tag's first line counts: `sandbox-exec` writes each once. Anything that
    is not a line it writes is passed over.
    """
    lines: dict[str, list[int]] = {}
    for line in bytes(report).splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break
        matched = REPORT_LINE.fullmatch(line)
        if matched is None:
            continue
        tag = matched.group(1).decode()
        if tag not in lines:
            lines[tag] = [int(number) for number in matched.group(2).split()]
    return lines


def children_cpu() -> float:
    """CPU seconds of every child this program has reaped so far."""
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def process_limit() -> int:
    """The processes a run's user may have at once: the container's own pids
    limit, or `PROCESS_LIMIT` when it sets none or a higher one."""
    try:
        text = PIDS_MAX.read_text().strip()
    except OSError:
        return PROCESS_LIMIT
    return min(int(text), PROCESS_LIMIT) if text.isdigit() else PROCESS_LIMIT


def child_setup(item: Item, processes: int) -> Callable[[], None]:
    """The function the child runs between fork and exec."""

    def setup() -> None:
        """Set the run's resource limits, put the run first in line for the
        kernel's out-of-memory killer, and let SIGINT act again.

        A POSIX message queue outlives the process that made it, as a System
        V object does, and none can be listed to remove it, so a run may make
        none: `RLIMIT_MSGQUEUE` at 0 gives it no bytes for one.
        `RLIMIT_NPROC` at `processes` stops a fork bomb where the container
        has no pids limit; it counts every process and thread of the run's
        user, this program and `sandbox-exec` among them, so it holds only
        where the user is the grading's own.
        """
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_MSGQUEUE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NPROC, (processes, processes))
        resource.setrlimit(resource.RLIMIT_FSIZE, (OUTPUT_LIMIT + 1, OUTPUT_LIMIT + 1))
        cpu = math.ceil(item.time_limit) + 1
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
        stack = item.memory_limit_kb * 1024
        _, hard = resource.getrlimit(resource.RLIMIT_STACK)
        if hard == resource.RLIM_INFINITY or stack <= hard:
            resource.setrlimit(resource.RLIMIT_STACK, (stack, hard))
        try:
            Path("/proc/self/oom_score_adj").write_text("1000")
        except OSError:
            pass
        signal.signal(signal.SIGINT, signal.SIG_DFL)

    return setup


def judge(run: Run, item: Item, kind: str) -> str:
    """Give a run its outcome.

    The limit the run was stopped for comes first. Otherwise the output limit,
    the time limits and the memory limit are checked in that order against
    what was measured and how the run ended, and any other failure is a
    runtime error. A jar that ran out of Java heap exits with code 3, and a
    run the kernel killed for the container's memory is over the memory
    limit too: it held more than the container allows, between two readings
    or where this program could not count it.
    """
    if run.stopped:
        return {"time": "time_limit", "memory": "memory_limit"}.get(
            run.stopped, "output_limit"
        )
    if run.returncode == -signal.SIGXFSZ or run.output_bytes > OUTPUT_LIMIT:
        return "output_limit"
    if (
        run.returncode == -signal.SIGXCPU
        or run.cpu_seconds > item.time_limit
        or run.wall_seconds > item.wall_limit
    ):
        return "time_limit"
    if (
        run.memory_kb > item.memory_limit_kb
        or run.out_of_memory
        or (kind == "java" and run.returncode == JAVA_OUT_OF_MEMORY)
    ):
        return "memory_limit"
    if run.returncode != 0:
        return "runtime_error"
    return "accepted"


def sample(me: int, helper: int) -> tuple[float, set[int]]:
    """The run's CPU seconds so far and the process ids of the run.

    The run is every process descended from this program but `sandbox-exec`:
    the program, whatever it started, and whatever it left behind, which comes
    back to this program as the subreaper. Their CPU time is added up, with
    what each has reaped of its own children.
    """
    parents: dict[int, int] = {}
    ticks: dict[int, int] = {}
    for entry in (path.name for path in Path("/proc").iterdir()):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
        except OSError:
            continue
        fields = stat[stat.rindex(")") + 2 :].split()
        parents[int(entry)] = int(fields[1])
        ticks[int(entry)] = sum(int(field) for field in fields[11:15])
    run = descendants(me, parents) - {helper}
    cpu = sum(ticks[pid] for pid in run) / os.sysconf("SC_CLK_TCK")
    return cpu, run


def descendants(root: int, parents: dict[int, int]) -> set[int]:
    """Every process below `root`, from each process's parent."""
    children: dict[int, list[int]] = {}
    for pid, parent in parents.items():
        children.setdefault(parent, []).append(pid)
    found: set[int] = set()
    waiting = list(children.get(root, []))
    while waiting:
        pid = waiting.pop()
        if pid not in found:
            found.add(pid)
            waiting.extend(children.get(pid, []))
    return found


def resident_memory(pid: int) -> tuple[int, int]:
    """A process's peak and present resident memory in kilobytes, 0 and 0 when
    it has none.
    """
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return 0, 0
    found = {"VmHWM": 0, "VmRSS": 0}
    for line in status.splitlines():
        name, _, value = line.partition(":")
        if name in found:
            found[name] = int(value.split()[0])
    return found["VmHWM"], found["VmRSS"]


def proportional_memory(pid: int, resident_kb: int) -> int:
    """A process's resident memory in kilobytes with each page it shares
    divided among the processes that share it (`Pss`), 0 once it has ended.

    A process whose pages this program may not read, one that made itself not
    dumpable, is counted at `resident_kb`, its whole resident memory, so it
    cannot hide what it holds from the total.
    """
    try:
        rollup = Path(f"/proc/{pid}/smaps_rollup").read_text()
    except PermissionError:
        return resident_kb
    except OSError:
        return 0
    for line in rollup.splitlines():
        if line.startswith("Pss:"):
            return int(line.split()[1])
    return 0


def own_cgroup() -> Path | None:
    """The cgroup that holds this program and its runs and nothing else: the
    step container's, which sees its own cgroup as the root of its cgroup
    namespace (the harness asks for a private one, and the socket filter
    refuses the host's), with cgroup v2's
    memory accounting. None anywhere else, such as a test run on a machine,
    whose cgroup holds other processes too.
    """
    try:
        lines = Path("/proc/self/cgroup").read_text().splitlines()
    except OSError:
        return None
    if "0::/" not in lines or not (CGROUP / "memory.current").is_file():
        return None
    return CGROUP


def held_kb(cgroup: Path) -> int | None:
    """What the cgroup holds that a run can hold on purpose, in kilobytes:
    everything charged to it (`memory.current`) but the pages of files on
    disk, which the kernel takes back when it needs them (`file` in
    `memory.stat`, less `shmem`, which it counts there too and cannot take
    back). That is anonymous memory, files in a tmpfs, memory files, pipe and
    socket buffers, and the kernel's own memory for the processes. Both
    files are readable even where the cgroup is not writable; none where
    they are missing.
    """
    try:
        current = int((cgroup / "memory.current").read_text())
        stat = (cgroup / "memory.stat").read_text()
    except OSError, ValueError:
        return None
    counts = dict(line.partition(" ")[::2] for line in stat.splitlines())
    try:
        reclaimable = int(counts.get("file", "0")) - int(counts.get("shmem", "0"))
    except ValueError:
        return None
    return (current - reclaimable) // 1024


def oom_kills() -> int:
    """How many processes the kernel has killed in this container for want
    of memory, from its cgroup's `memory.events`, readable even where the
    cgroup is not writable; 0 where there is no such file.
    """
    try:
        events = OOM_EVENTS.read_text()
    except OSError:
        return 0
    for line in events.splitlines():
        name, _, count = line.partition(" ")
        if name == "oom_kill":
            return int(count)
    return 0


def wake(pid: int) -> None:
    """Continue a process that may have been stopped."""
    try:
        os.kill(pid, signal.SIGCONT)
    except ProcessLookupError, PermissionError:
        pass


def kill(pid: int) -> None:
    """Kill one process, or a whole process group when `pid` is negative."""
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError, PermissionError:
        pass


def reap_strays() -> None:
    """Kill and reap every process a run left behind.

    This program is the subreaper of everything it starts, so a process that
    outlived the run, even one that left its session, is one of its children.
    """
    deadline = time.monotonic() + REAP_SECONDS
    while time.monotonic() < deadline:
        for pid in child_pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            return
        time.sleep(POLL_SECONDS)
    raise PrimitiveError("processes left behind by a run could not be stopped")


def sweep_sysv() -> None:
    """Remove every System V shared memory segment, message queue and
    semaphore set in this program's IPC namespace, once a run's processes are
    gone, so no run finds what an earlier one left.

    The step container has an IPC namespace of its own, and every run is
    this program's user, so every object in it is one a run made and this
    program may remove. One that cannot be removed, for any reason but being
    gone already, is an error of the step: it would carry one run's data to
    the next. Without `SYSVIPC` the kernel has no System V objects to leave.
    """
    for kind in ("shm", "msg", "sem"):
        try:
            lines = (SYSVIPC / kind).read_text().splitlines()[1:]
        except FileNotFoundError:
            continue
        for line in lines:
            remove_sysv(kind, int(line.split()[1]))


def remove_sysv(kind: str, identifier: int) -> None:
    """Remove one System V object of `kind` by its id, `IPC_RMID`."""
    c = libc()
    if kind == "sem":
        result = c.semctl(identifier, 0, IPC_RMID)
    else:
        remove = c.shmctl if kind == "shm" else c.msgctl
        result = remove(identifier, IPC_RMID, None)
    if result != 0 and ctypes.get_errno() not in GONE:
        raise PrimitiveError(
            f"a System V object a run left, {kind} {identifier}, could not be "
            f"removed: {os.strerror(ctypes.get_errno())}"
        )


def clear_attributes(top: Path) -> None:
    """Remove every extended attribute in the `user` namespace from `top` and
    everything under it, once a run's processes are gone.

    A run may set one on any file or folder it can name and write, which
    Landlock does not stop: the next item's input, `out/`, its own output.
    Each holds a few kilobytes, as many as the filesystem allows, so a run
    could keep work there for the next one. It may set no other namespace
    without a capability. A run may also take this program's access away
    from what it set one on, with `chmod`, which Landlock does not stop
    either, so each path is given back before its attributes are listed or
    removed, and a folder before what is in it. A path that is gone, or a
    filesystem without attributes, is passed over; an attribute that still
    can't be removed is a `PrimitiveError`, since the next run would find it.
    """
    folders = [top]
    while folders:
        folder = folders.pop()
        give_back(folder)
        clear_attributes_of(folder)
        try:
            entries = list(os.scandir(folder))
        except FileNotFoundError:
            continue
        except OSError as error:
            raise PrimitiveError(
                f"a folder a run could have marked, {folder}, could not be "
                f"listed: {error.strerror}"
            ) from error
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                folders.append(Path(entry.path))
            elif not entry.is_symlink():
                give_back(Path(entry.path))
                clear_attributes_of(Path(entry.path))


def clear_attributes_of(path: Path) -> None:
    """Remove the `user` attributes of one path, not following a link."""
    try:
        names = os.listxattr(path, follow_symlinks=False)
    except OSError as error:
        if error.errno in NO_ATTRIBUTES:
            return
        raise PrimitiveError(
            f"the attributes a run could have set on {path} could not be "
            f"listed: {error.strerror}"
        ) from error
    for name in names:
        if name.startswith("user."):
            try:
                os.removexattr(path, name, follow_symlinks=False)
            except OSError as error:
                if error.errno in NO_ATTRIBUTES | {errno.ENODATA}:
                    continue
                raise PrimitiveError(
                    f"an attribute a run set on {path} could not be removed: "
                    f"{error.strerror}"
                ) from error


def give_back(path: Path, needed: int | None = None) -> None:
    """Give this program's user back what it needs of `path`, should a run
    have taken it away: reading and writing a file (or `needed`), and also
    searching a folder.

    A run is this program's user, so it may change the mode of anything that
    user owns, which Landlock does not stop: its own output, `out/`, the
    folders it can name under the working directory and, where the harness
    lays them out as that user, `in/` and its files. Nothing here takes a
    mode away, so a path no run touched keeps its own. A link, a path
    another user owns and a path no longer there are left as they are, and
    so is one that cannot be named, such as one a run built past PATH_MAX:
    a run's paths never stop this program.
    """
    try:
        status = os.lstat(path)
        mode = status.st_mode
        if S_ISLNK(mode) or status.st_uid != os.geteuid():
            return
        if needed is None:
            needed = OWNER_FOLDER if S_ISDIR(mode) else OWNER_FILE
        if mode & needed != needed:
            path.chmod(S_IMODE(mode) | needed)
    except OSError:
        return


def give_back_along(root: Path, path: Path, needed: int | None = None) -> None:
    """Give back each folder from `root` down to `path`, then `path` itself,
    so it can be reached as well as used.
    """
    try:
        inside = path.resolve().relative_to(root.resolve())
    except ValueError:
        inside = None
    if inside is not None:
        folder = root
        for part in inside.parts[:-1]:
            give_back(folder)
            folder = folder / part
        give_back(folder)
    give_back(path, needed)


def give_back_tree(top: Path) -> None:
    """Give back `top` and everything under it, each folder before what is in
    it, so the harness can read every output and remove the working
    directory, and a run's own directory can be removed.
    """
    folders = [top]
    while folders:
        folder = folders.pop()
        give_back(folder)
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                folders.append(Path(entry.path))
            else:
                give_back(Path(entry.path))


def child_pids() -> list[int]:
    """List the processes whose parent is this program."""
    me = os.getpid()
    children = []
    for entry in (path.name for path in Path("/proc").iterdir()):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
        except OSError:
            continue
        if int(stat[stat.rindex(")") + 2 :].split()[1]) == me:
            children.append(int(entry))
    return children


def libc() -> ctypes.CDLL:
    """The C library, for prctl, the Landlock system calls and removing
    System V objects."""
    return ctypes.CDLL(None, use_errno=True)


def prctl(option: int, value: int) -> None:
    """Call prctl with one argument, raising on failure."""
    unused = ctypes.c_ulong(0)
    if libc().prctl(option, ctypes.c_ulong(value), unused, unused, unused) != 0:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))


def syscall(number: int, *arguments: int | ctypes.Array[ctypes.c_char] | None) -> int:
    """Make a raw system call, raising on failure."""
    function = libc().syscall
    function.restype = ctypes.c_long
    converted = [ctypes.c_long(a) if isinstance(a, int) else a for a in arguments]
    result: int = function(ctypes.c_long(number), *converted)
    if result < 0:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
    return result


def landlock_abi() -> int:
    """The Landlock ABI version the kernel offers, or 0 when it has none."""
    try:
        return syscall(
            SYS_LANDLOCK_CREATE_RULESET, None, 0, LANDLOCK_CREATE_RULESET_VERSION
        )
    except OSError:
        return 0


def landlock_ruleset(writable: list[Path], readable: list[Path], abi: int) -> int:
    """A Landlock ruleset for one run that writes `writable` and reads
    `readable`.

    Writing, creating, removing and renaming are allowed under each of
    `writable`, the working directory and a report folder if the run has
    one, and writing to /dev/null. Reading is allowed under them, of each
    file in `readable` and beneath each directory in it (the program, and the
    interpreter it needs), beneath the system directories in
    READABLE_DIRECTORIES, and of /dev/null and the devices in
    READABLE_DEVICES, and nowhere else: not the other runs' inputs under
    `in/`, their outputs under `out/`, nor `inputs.json`. From
    ABI 6 a process under it also may not signal, or reach an abstract Unix
    socket of, anything outside its own Landlock domain; from ABI 1 it may not
    trace anything outside it. Running a file needs reading it, so only the
    program and the system's programs can be run. Files already open, standard
    input and output, stay usable. `sandbox-exec` enforces it on the program
    alone, just before exec, so neither it nor this program is inside.
    """
    handled = FS_WRITES | FS_READS | FS_REFER | FS_TRUNCATE
    scoped = SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL if abi >= 6 else 0
    size = 8 if abi < 4 else 16 if abi < 6 else 24
    attr = ctypes.create_string_buffer(
        struct.pack("=QQQ", handled, 0, scoped)[:size], size
    )
    rules: list[tuple[str, int]] = [(str(path), handled) for path in writable]
    rules += [("/dev/null", FS_READ_FILE | FS_WRITE_FILE | FS_TRUNCATE)]
    rules += [
        (str(path), FS_READS if path.is_dir() else FS_READ_FILE) for path in readable
    ]
    rules += [(path, FS_READS) for path in READABLE_DIRECTORIES if Path(path).is_dir()]
    rules += [(path, FS_READ_FILE) for path in READABLE_DEVICES]
    ruleset = syscall(SYS_LANDLOCK_CREATE_RULESET, attr, size, 0)
    try:
        for path, access in rules:
            try:
                parent = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except FileNotFoundError:
                continue
            rule = ctypes.create_string_buffer(struct.pack("=Qi", access, parent), 12)
            try:
                syscall(
                    SYS_LANDLOCK_ADD_RULE, ruleset, LANDLOCK_RULE_PATH_BENEATH, rule, 0
                )
            finally:
                os.close(parent)
    except OSError:
        os.close(ruleset)
        raise
    return ruleset


def write_json(path: Path, document: dict[str, object]) -> None:
    """Write a JSON file whole: to a temporary name first, then renamed. The
    temporary file is made afresh, never opened through a link a run left.
    """
    partial = path.with_name(path.name + ".partial")
    partial.unlink(missing_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    with os.fdopen(os.open(partial, flags, 0o644), "w", encoding="utf-8") as stream:
        stream.write(json.dumps(document, ensure_ascii=False))
    partial.replace(path)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
