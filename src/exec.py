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

Each run goes in an isolate box of its own (https://github.com/ioi/isolate),
made fresh for it and thrown away after it: a box user of its own, its own
PID, IPC, mount and network namespaces, a cgroup that holds its memory and
counts its CPU, and a tmpfs of its own as its folder, holding a fresh copy of
the program and of its named files, and nothing of the step's. exec runs as
root in a privileged container to make the boxes, and sets the step up for
them first (`set_up_step`): it refuses to start on root Docker's identity
user ID map, where its root would be the machine's own. Any failure there
is an error of the step; no program runs outside a box.

A run given a `manifest`, an organiser's program such as a checker, also gets
an empty folder of its own in its box, named in `$UNICON_REPORT`, and writes
there one file per value it reports: `verdict`, `0` for accepted, `1` for a
wrong answer, or an outcome word for a program that judged another's run,
and each report the manifest declares. Its reports become outputs of their
own names on `0`, and a report that does not hold, or a run of it that
crashes or breaks a limit, is an error of the step, since the program did
not judge; for a `self_reported` run, one that runs the entrant's code, it
is the run's `runtime_error` or limit instead.
"""

import contextlib
import ctypes
import errno
import json
import math
import os
import re
import resource
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from stat import S_IMODE, S_ISREG
from typing import IO, BinaryIO, cast

SCHEMA_VERSION = 6
OUTPUT_LIMIT = 32 * 1024 * 1024
PROCESS_LIMIT = 128
"""The most processes and threads a run's box user may have at once
(`RLIMIT_NPROC`), below the container's own pids limit in `primitive.yaml`,
so a fork bomb stops at the box's limit and leaves exec and isolate room."""
JAVA_OUT_OF_MEMORY = 3
RUN_PATH = "/usr/local/bin:/usr/bin:/bin"
FILE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
"""A name a named file is placed under in the run's working directory: never
`.` or `..`, never a path, never hidden."""
NAMED_FILE_MODE = 0o444
RUN_ID = re.compile(r"[A-Za-z0-9_-]+")
"""A run's id, which only the forge knows the meaning of. An item's output
goes under its place in the batch, not its id."""
REFUSED_ARGUMENTS = frozenset({errno.E2BIG})
"""What the kernel answers when the command line is too long for it, which
only `args` can make it: the contestant's input, not the step, failed.
"""
ELF_MAGIC = b"\x7fELF"

ISOLATE = "/usr/local/bin/isolate"
LAUNCHER = "/usr/local/bin/box-launch"
"""The image's box launcher, from `src/box-launch.c`, which every program
starts through, in the box, under a seccomp filter of ours beside
isolate's: it refuses the keyring, inotify and fanotify calls, whose
kernel objects boxes share. Every box sees it under `/usr`."""
LAUNCHER_CHECK_KB = 64 * 1024
"""The memory, and the tmpfs, of the box the launcher's self-test runs in."""
ISOLATE_CONFIG = Path("/usr/local/etc/isolate")
"""Where the image's isolate reads its configuration, fixed when it is built.
exec writes it at the step's start, with the step's own box users."""
BOX_ROOT = Path("/var/local/lib/isolate")
LOCK_ROOT = Path("/run/isolate/locks")
CGROUP = Path("/sys/fs/cgroup")
"""The step container's cgroup, the root of its own cgroup namespace: a
privileged container on cgroup v2 sees its own cgroup there, writable."""
STEP_CGROUP = CGROUP / "step"
"""Everything the step runs, capped together at the memory the planner
gave the step (`set_up_cgroup`): exec's own processes in `main`, the
boxes in `isolate`. The container's own cgroup is the namespace's root,
whose limits only the machine sets."""
MAIN_CGROUP = STEP_CGROUP / "main"
BOX_CGROUPS = STEP_CGROUP / "isolate"
CONTAINER_MEMORY = CGROUP / "memory.max"
"""The container's memory limit in bytes, or `max` for none: the machine's
slot, which the step's own memory is never more than outside the tests."""
STEP_MEMORY_KB = 112 * 1024
"""What the step needs of its memory beside the boxes and the copies, at
the least: the image's step wrapper, unicon-step, the container's first
process, whose peak was measured at about 35 MB with 48 MB allowed for it,
then the primitive itself, isolate, and the kernel's own use, 64 MB."""
STEP_MEMORY_VARIABLE = "UNICON_STEP_MEMORY_MB"
CACHE_MOUNT = "/cache"
"""Where a box sees the item folder of the step's cache that its cached
named files are in, bound read-only."""
CONTROLLERS = "+cpu +memory"
BOX_DEV = Path("/run/boxdev")
"""The `/dev` every box gets in place of the container's, which in a
privileged container holds the machine's devices, its disks among them."""
BOX_DEVICES = ("null", "zero", "random", "urandom")
STDIN_MOUNT = Path("/run/boxstdin")
"""Where exec opens a run's `stdin` file: bound there, opened, and unbound
at once, so the path of the open file is `/`, never its own."""
BOX_LINKS = {
    "fd": "/proc/self/fd",
    "stdin": "/proc/self/fd/0",
    "stdout": "/proc/self/fd/1",
    "stderr": "/proc/self/fd/2",
}
BOX_USERS_VARIABLE = "UNICON_BOX_USERS"
BOX_USERS = re.compile(r"([0-9]{1,9})-([0-9]{1,9})")
FIRST_BOX_USER = 10_000
LAST_BOX_USER = 65_000
"""The range every step's box users lie in: above the image's system users
and below `nobody`, and inside the 65,536 IDs a rootless Docker's user maps."""
UID_MAP = Path("/proc/self/uid_map")
ROOT_DOCKER = ["0", "0", "4294967295"]
"""The first line of `/proc/self/uid_map` where the container's root is the
machine's own, as under root Docker: every user ID maps to itself."""
DEV_ROOT_DOCKER_VARIABLE = "UNICON_DEV_ROOT_DOCKER"
"""Set to `1` by a development stack alone, so exec runs under root Docker."""
BOX = 0
GIVEN = ("program", "stdin", "manifest")
"""The file inputs of a run beside its named files."""
"""The box every item runs in, one after another, made afresh each time."""
BOX_INODES = 60_000
"""The most files and folders a box's tmpfs holds. Removing them takes
several times what making them did, on no run's clock, so a run that fills
them is stopped for its memory, which every entry in a tmpfs takes."""
BOX_ERROR_LIMIT = 4096
"""The most of isolate's own standard error an error of the step quotes."""
ISOLATE_STACK = 256 * 1024 * 1024
ISOLATE_OPEN_FILES = BOX_INODES + 1024
"""The stack and open files isolate needs: after a run it gives the box's
tree back to root by recursing into it with a folder open at each level,
and a run may nest folders as deep as the box's inode cap. A tree of about
100,000 folders took more than an 8 MB stack. The open-file count, the cap
plus a margin, stays under 65,536, the hard limit a container is given
where Docker sets none."""
BOX_FOLDERS = ("tmp", "shm")
"""The folders on the box's tmpfs that are the run's `/tmp` and `/dev/shm`,
so they share its size and inode cap: isolate's own tmpfs for them takes
no size or inode options, and held over a million files."""
JAVA_CONFIG = "/etc/java-21-openjdk"
"""The one folder of `/etc` a box sees: the Java runtime's own, where the
links in its installation lead. The rest of the container's `/etc` names
the container, its address and its name server."""
WRITE_BY_OTHERS = 0o022
MS_NOSUID = 2
MS_NODEV = 4
MS_BIND = 4096
MNT_DETACH = 2
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
PROGRAMS_OWN_NAMES = OWN_NAMES | {
    "ended",
    "signal",
    "ended_at",
    "stopped_by",
    "facts",
    "reports",
}
"""`OWN_NAMES` plus what the several-programs form gives of each program."""
REPORT_KEYS = frozenset({"type", "optional", "better", "at_least", "at_most"})
TEXTS = ("label", "message", "staff_message")
"""The text files a report may hold besides its reports, each an output of
its name: a finer name for the outcome, a message for the entrant, and one for
staff alone."""
VERDICT_WORDS = ("time_limit", "memory_limit", "output_limit", "runtime_error")
"""The outcomes a report's verdict may name besides `0` and `1`: a program
that judged another program's run, as `unicon/interactive`'s verdict script
judges the programs exec ran together, says whose limit it was, or that it
crashed. From a `self_reported` run, the entrant's code, such a word is the
run's `runtime_error`, so that code never chooses its own outcome."""
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
    """What a run given a manifest reported: its verdict, `0`, `1` or one of
    `VERDICT_WORDS`, the outputs it gives, and the files only shown with the
    run, by name."""

    verdict: str
    outputs: dict[str, object]
    shown: dict[str, bytes]


@dataclass(frozen=True)
class Item:
    """One run: its id, its place in the batch from 1, the program, the file
    for standard input if any, the two limits, the arguments, and the named
    files placed in its working directory: `files` it reads, and `loads`, code
    the program loads, such as the entrant's module a test harness imports.
    `manifest` is the reports the program declares, when it is one that
    reports, `self_reported` says it runs the entrant's code, and `cached`
    is the item folder of the step's cache its cached named files are in.
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
    cached: Path | None = None

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
    """A program ready to run: its format and the file the command starts,
    as the box sees it."""

    kind: str
    path: Path

    def command(self, item: Item) -> list[str]:
        """The command line that runs this program for one item, the item's
        arguments last. The interpreters are the image's own, which every box
        sees under `/usr`; Python's is started by its real path, outside any
        virtual environment exec runs in.
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
            os.path.realpath(java),
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
    """What one run did, as isolate's meta file and the box's tmpfs tell it.

    `status` is isolate's: empty when the program exited 0, `RE` for another
    exit code, `SG` for a signal, `TO` for a time limit. `returncode` is the
    exit code, or minus the signal that killed it. `memory_kb` is the peak of
    the box's cgroup (`cg-mem`). `out_of_memory` says the kernel killed a
    process of the box for the box's memory, and `no_inodes` that the box's
    tmpfs had no file or folder left to give.
    """

    status: str
    returncode: int
    cpu_seconds: float
    memory_kb: int
    output_bytes: int
    out_of_memory: bool = False
    no_inodes: bool = False


NOT_STARTED = Run("RE", 127, 0.0, 0, 0)
"""A run that could not start, for its command line: a `runtime_error`."""


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


def read_inputs(root: Path) -> list[Item | ProgramsItem]:
    """Read `inputs.json` and return its items in order."""
    try:
        document = json.loads(
            (root / "inputs.json").read_bytes(), parse_constant=refuse_constant
        )
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


def read_item(root: Path, entry: object, number: int) -> Item | ProgramsItem:
    """Check one batch entry, the `number`th, and return it as an item."""
    if not isinstance(entry, dict) or not isinstance(entry.get("inputs"), dict):
        raise PrimitiveError("a batch item has no inputs object")
    run = entry.get("run")
    if not isinstance(run, str) or len(run) > 255 or not RUN_ID.fullmatch(run):
        raise PrimitiveError(f"the batch item run {run!r} is not a run id")
    inputs = entry["inputs"]
    if "program" in inputs and "programs" in inputs:
        raise PrimitiveError("a batch item gives both program and programs")
    if "programs" in inputs:
        return programs_read_item(root, entry, number)
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
    item = Item(
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
        cached=cached_folder(
            root,
            [
                *(
                    input_file(root, inputs, name, run)
                    for name in GIVEN
                    if name in inputs
                ),
                *files.values(),
                *loads.values(),
            ],
            f"the run {run}",
        ),
    )
    # isolate reads a limit of 0 as none, and takes the time in whole
    # milliseconds and the memory in whole kilobytes.
    if item.time_limit < 0.001:
        raise PrimitiveError(
            f"the input named time_limit of run {run} is under a millisecond, "
            "which isolate would read as no limit"
        )
    if item.memory_limit_kb == 0:
        raise PrimitiveError(
            f"the input named memory_limit of run {run} is under a kilobyte, "
            "which isolate would read as no limit"
        )
    return item


def read_manifest(
    path: Path, what: str, own: frozenset[str] = OWN_NAMES
) -> dict[str, Declared]:
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
    return {name: declared(name, port, what, own) for name, port in reports.items()}


def refuse_constant(constant: str) -> object:
    """Refuse `NaN` and `Infinity`, which Python's JSON reader takes but JSON
    has not."""
    raise ValueError(f"{constant} is not JSON")


def declared(
    name: str, port: object, what: str, own: frozenset[str] = OWN_NAMES
) -> Declared:
    """Check one report a manifest declares, `name`, and return it."""
    if not REPORT_NAME.fullmatch(name):
        raise PrimitiveError(f"{what} declares {name!r}, not a report name")
    if name in own:
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
    """Resolve one file value to a path: `{"file": "in/..."}` to one under
    `in/`, or `{"file": "cache/<item>/<name>"}`, a cached file, to one
    straight in an item folder of the step's cache, which the step's
    `cache` links to, wherever that is."""
    if not isinstance(value, dict) or not isinstance(value.get("file"), str):
        raise PrimitiveError(f"{what} is not a file")
    named = str(value["file"])
    path = (root / named).resolve()
    if named.startswith("cache/"):
        if path.parent.parent != (root / "cache").resolve():
            raise PrimitiveError(f"{what} is not a cached file of an item")
    elif not path.is_relative_to((root / "in").resolve()):
        raise PrimitiveError(f"{what} is outside in/")
    if not path.is_file():
        raise PrimitiveError(f"{what} is not in the working directory")
    return path


def cached_folder(root: Path, files: list[Path], what: str) -> Path | None:
    """The item folder of the step's cache that the cached ones of `files`,
    every file `what` is given, are in, if any are: one folder, holding
    those files and nothing else, since it is bound whole into the box
    (`cache_binding`), where a file of another run's or program's would be
    there to read."""
    cache = (root / "cache").resolve()
    folders = {path.parent for path in files if path.parent.parent == cache}
    if len(folders) > 1:
        raise PrimitiveError(f"{what} names cached files of more than one item")
    if not folders:
        return None
    folder = folders.pop()
    own = {path.name for path in files if path.parent == folder}
    others = sorted({path.name for path in folder.iterdir()} - own)
    if others:
        raise PrimitiveError(
            f"{what} has an item folder in the cache that also holds {others[0]!r}, "
            "a file not its own"
        )
    return folder


def cache_binding(folder: Path | None) -> list[str]:
    """isolate's option binding an item folder of the step's cache into the
    box as `CACHE_MOUNT`, read-only, as isolate binds by default, and never
    executable, or none for no folder."""
    if folder is None:
        return []
    return [f"--dir={CACHE_MOUNT.lstrip('/')}={folder}:noexec"]


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
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not 0 < value < math.inf
    ):
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


def run_batch(root: Path, items: list[Item | ProgramsItem]) -> list[dict[str, object]]:
    """Check the step's memory holds each item's box (`check_memory`), set
    the step up for its boxes, capped at that memory (`step_memory_kb`),
    then run the items one after another, in the order given, each in a fresh box
    with a fresh copy of its program, and return one outputs entry per item.

    isolate's meta file and its own standard error go in a folder of exec's
    in the container's `/tmp`, which no box sees.
    """
    if any(isinstance(item, ProgramsItem) for item in items):
        if not all(isinstance(item, ProgramsItem) for item in items):
            raise PrimitiveError(
                "a batch mixes the one-program form and the several-programs form"
            )
        return programs_run_batch(root, cast(list[ProgramsItem], items))
    runs = cast(list[Item], items)
    users = box_users()
    memory_kb = step_memory_kb()
    for item in runs:
        check_memory(box_size_kb(item), memory_kb, item.run)
    set_up_step(users, memory_kb)
    scratch = Path(tempfile.mkdtemp(prefix="exec-"))
    kinds: dict[Path, str] = {}
    results: list[dict[str, object]] = []
    try:
        for item in runs:
            if item.program not in kinds:
                kinds[item.program] = detect(item.program)
            outputs = run_item(root, item, kinds[item.program], scratch, users)
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
    root: Path, item: Item, kind: str, scratch: Path, users: range
) -> dict[str, object]:
    """Run one item, its program of format `kind`, in a fresh box and return
    its outputs, all of them whatever the outcome: what the program printed,
    its CPU time and its peak memory.

    The box's folder is a tmpfs of its own, laid out as `/box/program`, the
    copy of the program, `/box/run`, the run's working directory holding its
    named files, `/box/tmp` and `/box/shm`, which the run sees as `/tmp` and
    `/dev/shm` too, so the tmpfs's size and inode cap hold them, and for a
    program given a manifest `/box/report`, its report folder, which is read
    before the tmpfs goes.

    The time is the CPU time, and on `time_limit` at least the limit: a run
    the wall clock stopped, one that slept or waited, used little CPU.
    """
    out = root / "out" / str(item.number)
    out.mkdir(parents=True, exist_ok=True)
    with fresh_box(BOX, box_size_kb(item)) as box:
        program = prepare(item, kind, box)
        place_files(box / "run", item.files | item.loads, item.cached)
        for name in BOX_FOLDERS:
            (box / name).mkdir()
            (box / name).chmod(0o1777)
        report = None
        if item.manifest is not None:
            report = box / "report"
            report.mkdir()
        reports = report is not None
        run = execute(item, program, reports, out / "output", scratch, users)
        run = replace(run, no_inodes=os.statvfs(box).f_ffree == 0)
        outcome = judge(run, item, kind)
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
    return outputs


def box_size_kb(item: Item) -> int:
    """The size of an item's box tmpfs, in kilobytes: its memory limit, which
    holds what the run writes there before the size does, as every page it
    writes is charged to the box's memory, plus the program and the named
    files exec copies in, which are charged to exec's, each to whole 4 KB
    pages. A cached named file is not copied but bound in."""
    named = [*item.files.values(), *item.loads.values()]
    copied = [item.program, *(path for path in named if path.parent != item.cached)]
    return item.memory_limit_kb + sum(
        4 * -(-path.stat().st_size // 4096) for path in copied
    )


def check_memory(needed_kb: int, memory_kb: int, run: str) -> None:
    """Refuse run `run`, before any program of the batch runs, when
    `needed_kb`, its boxes' memory limits and the files copied into their
    tmpfs, and `STEP_MEMORY_KB` are more than the step's memory,
    `memory_kb`, or the container's, when that is less. The limit would
    otherwise kill first: a program, which isolate reads as the box's own
    kill, a `memory_limit` that was not the program's, or the step itself,
    which then writes no outputs at all."""
    held_kb = memory_kb
    with contextlib.suppress(FileNotFoundError):
        text = CONTAINER_MEMORY.read_text().strip()
        if text != "max":
            held_kb = min(held_kb, int(text) // 1024)
    if needed_kb + STEP_MEMORY_KB > held_kb:
        raise PrimitiveError(
            f"the run {run} needs {math.ceil(needed_kb / 1024)} MB for its "
            "memory limits and the files copied in beside them, and the "
            f"step's {held_kb // 1024} MB leaves "
            f"{max(0, held_kb - STEP_MEMORY_KB) // 1024} MB for them"
        )


def prepare(item: Item, kind: str, box: Path) -> Program:
    """Copy the item's program, of format `kind`, into its box, before the
    run's clock starts, executable for a native executable, since the file
    under `in/` may not be, and readable for the others.

    The run is the copy's owner while it runs, so it may change its mode,
    times or attributes; the copy goes with the box.
    """
    try:
        shutil.copyfile(item.program, box / "program")
    except OSError as error:
        raise PrimitiveError(
            f"the program of run {item.run} could not be copied: {error.strerror}"
        ) from error
    (box / "program").chmod(0o755 if kind == "native" else 0o644)
    return Program(kind, Path("/box/program"))


def place_files(cwd: Path, files: dict[str, Path], cached: Path | None) -> None:
    """Copy each named file into the run's working directory under its name,
    readable and not writable, before the run's clock starts, so the program
    opens `answer` or `input` as a plain relative path.

    Each run gets copies of its own, which go with its box, and nothing is
    read back from them, so a run that makes its copy writable again, as the
    copy's owner may, changes only what it reads itself.

    A file in `cached`, the item folder of the step's cache bound into the
    box, is not copied: its name is a link to it there, `CACHE_MOUNT`/<its
    name>, which the run reads and cannot change.
    """
    cwd.mkdir(mode=0o755)
    for name, source in files.items():
        placed = cwd / name
        if source.parent == cached:
            placed.symlink_to(Path(CACHE_MOUNT) / source.name)
            continue
        try:
            shutil.copyfile(source, placed)
        except OSError as error:
            raise PrimitiveError(
                f"the named file {name} could not be placed: {error.strerror}"
            ) from error
        placed.chmod(NAMED_FILE_MODE)


def execute(
    item: Item,
    program: Program,
    reports: bool,
    output: Path,
    scratch: Path,
    users: range,
) -> Run:
    """Run one item's program in box `BOX`, already made and filled, by
    `isolate_command`, and return what it did.

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
    if any("\0" in argument for argument in item.args):
        output.write_bytes(b"")
        return NOT_STARTED
    meta = scratch / "meta"
    errors = scratch / "errors"
    meta.unlink(missing_ok=True)
    more = [*cache_binding(item.cached), "--special-files"]
    command = isolate_command(BOX, meta, item, reports, more, program.command(item))
    if item.stdin is None:
        descriptor = os.open(os.devnull, os.O_RDONLY)
    else:
        descriptor = open_stdin(item.stdin)
    with (
        os.fdopen(descriptor, "rb") as stdin,
        errors.open("wb") as stderr,
        output.open("wb") as stdout,
    ):
        if item.stdin is not None:
            hand_over(stdin.fileno(), item, users)
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
    if "exitcode" in found:
        returncode = int(found["exitcode"])
    elif "exitsig" in found:
        returncode = -int(found["exitsig"])
    else:
        returncode = 0 if status == "" else -9
    return Run(
        status=status,
        returncode=returncode,
        cpu_seconds=float(found.get("time", "0")),
        memory_kb=int(found.get("cg-mem", "0")),
        output_bytes=printed,
        out_of_memory=found.get("cg-oom-killed") == "1",
    )


def isolate_command(
    number: int,
    meta: Path,
    item: Item | Entry,
    reports: bool,
    more: list[str],
    run: list[str],
) -> list[str]:
    """The isolate command line that runs `run`, one program, in box
    `number`, already made and filled, writing its meta file to `meta`.

    isolate holds every limit: CPU time over all the box's processes
    (`--time`), the wall clock (`--wall-time`), the box's memory in its
    cgroup, without swap (`--cg-mem`), the stack at the memory limit, and the
    box user's processes (`--processes`). The box gets `/tmp` and `/dev/shm`
    from its own tmpfs, the step's `/dev` of four devices, and of `/etc` only
    `JAVA_CONFIG`, read-only, then the options in `more`, the caller's own.
    The environment holds only `PATH`, `HOME` and `TMPDIR`, both the working
    directory, `LANG=C.UTF-8`, and for a program given a manifest
    (`reports`) `UNICON_REPORT`. Standard error goes nowhere. The program
    starts through `LAUNCHER`, which runs it with that command line and
    environment once its filter is on.
    """
    env = {"PATH": RUN_PATH, "HOME": "/box/run", "TMPDIR": "/box/run"}
    env["LANG"] = "C.UTF-8"
    if reports:
        env["UNICON_REPORT"] = "/box/report"
    box = BOX_ROOT / str(number) / "box"
    return [
        ISOLATE,
        "--cg",
        f"--box-id={number}",
        "--silent",
        f"--meta={meta}",
        f"--time={item.time_limit}",
        f"--wall-time={item.wall_limit}",
        f"--cg-mem={item.memory_limit_kb}",
        f"--stack={item.memory_limit_kb}",
        f"--processes={PROCESS_LIMIT}",
        f"--dir=tmp={box / 'tmp'}:rw",
        f"--dir=dev={BOX_DEV}:dev",
        f"--dir=dev/shm={box / 'shm'}:rw",
        f"--dir={JAVA_CONFIG.lstrip('/')}",
        *more,
        "--chdir=/box/run",
        "--stderr=/dev/null",
        *(f"--env={name}={value}" for name, value in env.items()),
        "--run",
        "--",
        LAUNCHER,
        *run,
    ]


def open_stdin(path: Path) -> int:
    """Open a run's `stdin` file through `STDIN_MOUNT`, bound there and
    unbound once open. A box reads the path its standard input was opened
    at through `/proc/self/fd/0`, and the file's own path under `/work/in`
    may say which test it is; the path of a file opened through a mount
    that is gone is `/`."""
    STDIN_MOUNT.touch()
    mount(path, STDIN_MOUNT, None, MS_BIND, None)
    try:
        return os.open(STDIN_MOUNT, os.O_RDONLY)
    finally:
        unmount(STDIN_MOUNT, MNT_DETACH)


def hand_over(descriptor: int, item: Item, users: range) -> None:
    """Make the item's `stdin` file, open on `descriptor`, safe to hand to
    the box: the box can open it again through `/proc/self/fd/0`, by the
    file's own mode and owner, so write for its group and others is taken
    away, and a file one of the step's box users owns, who could write it
    whatever its mode, is an error of the step."""
    found = os.fstat(descriptor)
    if found.st_uid in users:
        raise PrimitiveError(
            f"the stdin file of run {item.run} is owned by user {found.st_uid}, "
            "a box user, who could change it from a box"
        )
    if found.st_mode & WRITE_BY_OTHERS:
        os.fchmod(descriptor, S_IMODE(found.st_mode) & ~WRITE_BY_OTHERS)


def isolate_said(code: int, said: str | bytes) -> str:
    """What isolate said when it failed, `said` its message or its standard
    error, or failing that how it ended: exit `code`, or a signal when
    negative."""
    if isinstance(said, bytes):
        said = said[:BOX_ERROR_LIMIT].decode("utf-8", "replace")
    said = said.strip()
    if said:
        return said
    if code < 0:
        return f"it was killed by signal {-code} and said nothing"
    return f"it exited with code {code} and said nothing"


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


def read_meta(path: Path) -> dict[str, str]:
    """isolate's meta file, `key:value` a line, as keys and values; empty
    where isolate wrote none, as when it failed before the run."""
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return {}
    return dict(line.partition(":")[::2] for line in lines if ":" in line)


def quietly(cleanup: Callable[[], None]) -> None:
    """Run `cleanup` while an error that ends the batch is on its way out: an
    error of its own goes to standard error, so it does not hide the first.
    The container goes with the batch, so what it could not undo stays with
    no later run."""
    try:
        cleanup()
    except Exception as error:
        print(f"exec: cleaning up after the error failed too: {error}", file=sys.stderr)


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
    report. A `self_reported` run's verdict word is a `runtime_error` too.
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
    if item.self_reported and report.verdict in VERDICT_WORDS:
        return {"outcome": "runtime_error"}
    outputs = dict(report.outputs)
    if report.shown:
        extras = out / "extras"
        extras.mkdir(exist_ok=True)
        for name, content in report.shown.items():
            (extras / name).write_bytes(content)
        outputs["extras"] = {"folder": f"out/{item.number}/extras"}
    outputs["outcome"] = verdict_outcome(report.verdict)
    return outputs


def verdict_outcome(verdict: str) -> str:
    """The outcome a report's verdict gives: `0`, judged and counted, is
    `accepted`, `1` is `wrong_answer`, and a word of `VERDICT_WORDS` is that
    outcome."""
    if verdict == "0":
        return "accepted"
    if verdict == "1":
        return "wrong_answer"
    return verdict


def read_report(folder: Path, manifest: dict[str, Declared]) -> Report:
    """Read what a run wrote to its report folder, against what its manifest
    declares, raising `ReportError` when it does not hold: no `verdict`, or
    one that is not `0`, `1` or a word of `VERDICT_WORDS`, under which, as
    under `1`, the reports are shown unchecked; under `0`, a declared report
    that is not optional missing, or a number that is not a finite decimal
    or lies outside its bounds; on any verdict, a file that is not UTF-8 text
    without a NUL, as every file is shown as text, a file over
    `REPORT_TEXT_LIMIT`, more than `REPORT_FILES` of them, or anything there
    that is not a plain file under a plain name.
    """
    files = report_files(folder)
    if "verdict" not in files:
        raise ReportError("has no verdict")
    written = files.pop("verdict").strip()
    verdict = written.decode("ascii", "replace")
    if verdict not in ("0", "1", *VERDICT_WORDS):
        raise ReportError(
            f"has a verdict {shown_bytes(written)}, not 0, 1 or an outcome word"
        )
    outputs: dict[str, object] = {}
    for name in TEXTS:
        if name in files:
            outputs[name] = report_text(name, files.pop(name))
    if verdict == "0":
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
    return Report(verdict, outputs, files)


def report_files(folder: Path) -> dict[str, bytes]:
    """Every file in a report folder by name, each read whole, none through a
    link: a run that left a link there to a file it may not read, the next
    run's input, would have exec read that file for it. The folder itself is
    opened without following a link, since the box's user owns `/box` and
    could put a link in its place, and everything in it is opened through
    that descriptor. The folder is listed only as far as one file past
    `REPORT_FILES`, so a run that left a million costs exec no more.
    """
    try:
        directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise ReportError(f"could not be opened: {error.strerror}") from None
    try:
        return folder_files(directory)
    finally:
        os.close(directory)


def folder_files(directory: int) -> dict[str, bytes]:
    """Every plain file in the report folder open on `directory`, by name,
    each opened through that descriptor, never through a link."""
    entries: list[str] = []
    try:
        with os.scandir(directory) as listing:
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
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
        except OSError:
            raise ReportError(f"holds {name}, not a plain file") from None
        try:
            if not S_ISREG(os.fstat(descriptor).st_mode):
                raise ReportError(f"holds {name}, not a plain file")
            content = read_capped(descriptor, REPORT_TEXT_LIMIT + 1)
        except OSError as error:
            raise ReportError(
                f"holds {name}, which could not be read: {error.strerror}"
            ) from None
        finally:
            os.close(descriptor)
        if len(content) > REPORT_TEXT_LIMIT:
            raise ReportError(f"holds {name}, over {REPORT_TEXT_LIMIT // 1024} KB")
        files[name] = content
    return files


def read_capped(descriptor: int, limit: int) -> bytes:
    """Up to `limit` bytes of an open file, read to its end or the limit."""
    chunks: list[bytes] = []
    read = 0
    while read < limit:
        chunk = os.read(descriptor, limit - read)
        if not chunk:
            break
        chunks.append(chunk)
        read += len(chunk)
    return b"".join(chunks)


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


def judge(run: Run | ProgramsRun, item: Item | Entry, kind: str) -> str:
    """Give a run its outcome, from what isolate and the box's tmpfs say, in
    this order.

    A run the kernel killed for the box's memory is `memory_limit`, though
    isolate reads that as a signal too, and so is one that left its tmpfs no
    file or folder to give: it was stopped by what its files held, as every
    one of them takes memory. Then `TO`, isolate's for the CPU time and the
    wall clock both, is `time_limit`. Then output over `OUTPUT_LIMIT` is
    `output_limit`, before the signal, since a run that printed on died of
    `SIGPIPE` when exec stopped reading. A jar that ran out of Java heap
    exits with code 3, `memory_limit`. Any other signal or failure is a
    runtime error.
    """
    if run.out_of_memory or run.no_inodes:
        return "memory_limit"
    if run.status == "TO":
        return "time_limit"
    if run.output_bytes > OUTPUT_LIMIT:
        return "output_limit"
    if kind == "java" and run.returncode == JAVA_OUT_OF_MEMORY:
        return "memory_limit"
    if run.status in ("RE", "SG") or run.returncode != 0:
        return "runtime_error"
    return "accepted"


def box_users() -> range:
    """The step's box users, from `UNICON_BOX_USERS`, `<first>-<last>`, both
    included, which whoever starts the step sets: users no other step that
    can run at the same time on the machine has, and no container's user,
    all within 10,000 to 65,000. exec runs one box at a time, so it needs
    one; it is given them all so a step that runs boxes side by side can
    have more."""
    text = os.environ.get(BOX_USERS_VARIABLE)
    if text is None:
        raise PrimitiveError(
            f"{BOX_USERS_VARIABLE} is not set, so exec has no box users of the "
            "step's own to run the programs as"
        )
    matched = BOX_USERS.fullmatch(text)
    if matched is None:
        raise PrimitiveError(f"{BOX_USERS_VARIABLE} is not <first>-<last>: {text!r}")
    first, last = int(matched.group(1)), int(matched.group(2))
    if not FIRST_BOX_USER <= first <= last <= LAST_BOX_USER:
        raise PrimitiveError(
            f"{BOX_USERS_VARIABLE} is {text}, not a range within "
            f"{FIRST_BOX_USER} to {LAST_BOX_USER}"
        )
    return range(first, last + 1)


def step_memory_kb() -> int:
    """The step's memory in kilobytes, from `UNICON_STEP_MEMORY_MB`, which
    whoever starts the step sets, from the step's job: what the forge's
    planner gave the step, its largest item's boxes' memory limits, the
    files copied in beside them, and the room `primitive.yaml` adds for
    what the step does outside its boxes. The step is capped at it, so
    steps side by side in one slot of a machine stay within what each was
    planned; exec does not work it out again."""
    text = os.environ.get(STEP_MEMORY_VARIABLE)
    if text is None:
        raise PrimitiveError(
            f"{STEP_MEMORY_VARIABLE} is not set, so exec does not know the "
            "memory the step was given"
        )
    if re.fullmatch(r"[1-9][0-9]{0,6}", text) is None:
        raise PrimitiveError(
            f"{STEP_MEMORY_VARIABLE} is not a whole number of MB: {text!r}"
        )
    return int(text) * 1024


def set_up_step(users: range, memory_kb: int) -> None:
    """Set the step container up for isolate's boxes, as root, in this order:
    refuse root Docker's identity map (`check_shape`), raise the limits
    isolate inherits (`raise_limits`), give the step a cgroup capped at
    `memory_kb` and the boxes theirs under it
    (`set_up_cgroup`), a `/dev` (`set_up_devices`), write isolate's
    configuration with the step's box users, and run the box launcher's
    self-test (`check_launcher`). Any failure is an error of the step, and
    nothing runs. Each step is safe to take again, so a step that
    runs exec twice, as the tests do, sets up once.
    """
    check_shape()
    raise_limits()
    try:
        set_up_cgroup(memory_kb)
        set_up_devices()
        ISOLATE_CONFIG.write_text(
            f"box_root = {BOX_ROOT}\n"
            f"lock_root = {LOCK_ROOT}\n"
            f"cg_root = {BOX_CGROUPS}\n"
            f"first_uid = {users.start}\n"
            f"first_gid = {users.start}\n"
            f"num_boxes = {len(users)}\n"
        )
        BOX_ROOT.mkdir(parents=True, exist_ok=True)
        LOCK_ROOT.mkdir(parents=True, exist_ok=True)
        check_launcher()
    except OSError as error:
        raise PrimitiveError(
            f"the step could not be set up for its boxes, {error.filename}: "
            f"{error.strerror}; exec runs as root in a privileged container"
        ) from None


def check_launcher() -> None:
    """Run `LAUNCHER --self-test` once in box 0, as a run starts: it sets
    its filter up, checks that a call the filter refuses is refused, and
    exits 0. A run's box sends its standard error nowhere, so a launcher
    that could not set its filter up would end every run as the program's
    `runtime_error`; here it is an error of the step, with what the
    launcher said."""
    with tempfile.TemporaryDirectory(prefix="launcher-") as scratch:
        meta = Path(scratch) / "meta"
        with fresh_box(0, LAUNCHER_CHECK_KB):
            done = subprocess.run(
                [
                    ISOLATE,
                    "--cg",
                    "--box-id=0",
                    "--silent",
                    f"--meta={meta}",
                    "--time=5",
                    "--wall-time=10",
                    f"--cg-mem={LAUNCHER_CHECK_KB}",
                    f"--dir=dev={BOX_DEV}:dev",
                    "--run",
                    "--",
                    LAUNCHER,
                    "--self-test",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
            )
        found = read_meta(meta)
    if done.returncode == 0:
        return
    if "exitcode" in found:
        code = int(found["exitcode"])
    elif "exitsig" in found:
        code = -int(found["exitsig"])
    else:
        code = done.returncode
    said = done.stderr or found.get("message", "")
    raise PrimitiveError(
        "the box launcher's self-test failed, so no program can run under its "
        f"filter: {isolate_said(code, said)}"
    )


def check_shape() -> None:
    """Refuse root Docker's identity map: where the first line of
    `/proc/self/uid_map` is exactly `0 0 4294967295`, as under root Docker,
    a privileged step would be the machine's root. exec is meant for a
    container whose root is a user namespace's, as under rootless Docker;
    this check catches root Docker, not every map whose root is the
    machine's. Without a map to read, it does not start either. A
    development stack, whose laptop has root Docker alone, lets it through
    with `UNICON_DEV_ROOT_DOCKER=1`, saying so on standard error."""
    try:
        first = UID_MAP.read_text().splitlines()[0].split()
    except OSError, IndexError:
        first = None
    if first == ROOT_DOCKER and os.environ.get(DEV_ROOT_DOCKER_VARIABLE) == "1":
        print(
            f"exec: WARNING: {DEV_ROOT_DOCKER_VARIABLE} is set, so this step runs "
            "under root Docker, where a box that breaks out is the machine's root. "
            "Only a development stack may set it.",
            file=sys.stderr,
        )
    elif first is None or first == ROOT_DOCKER:
        raise PrimitiveError(
            "this container has root Docker's identity user ID map, where its "
            "root is the machine's own root; exec runs only where it is a user "
            "namespace's, as under rootless Docker"
        )
    if os.geteuid() != 0:
        raise PrimitiveError(
            "exec runs as root in a privileged container, to make its boxes"
        )


def raise_limits() -> None:
    """Raise exec's own stack and open-files limits to what isolate needs,
    `ISOLATE_STACK` and `ISOLATE_OPEN_FILES`, for isolate inherits them; a
    box gets isolate's own limits, not these. Where the container's hard
    limit is below that, exec does not start, rather than have isolate fail
    on a run's deep tree and end the batch in an error."""
    for kind, name, need in (
        (resource.RLIMIT_STACK, "stack", ISOLATE_STACK),
        (resource.RLIMIT_NOFILE, "open files", ISOLATE_OPEN_FILES),
    ):
        soft, hard = resource.getrlimit(kind)
        if hard != resource.RLIM_INFINITY and hard < need:
            raise PrimitiveError(
                f"the container's hard limit on {name}, {hard}, is below the "
                f"{need} isolate needs to give back a box's deepest tree"
            )
        if soft != resource.RLIM_INFINITY and soft < need:
            resource.setrlimit(kind, (need, hard))


def set_up_cgroup(memory_kb: int) -> None:
    """Make the step's cgroup, `step`, with the CPU and memory controllers
    on, and under it `main`, which every process in the container's cgroup
    moves to, and `isolate`, which isolate puts each box's cgroup under. A
    cgroup with processes in it cannot hand controllers to its children,
    hence `main`; a process that will not move, gone already, is passed
    over. `step` is capped, with no swap, at `memory_kb`, what the forge's
    planner gave the step (`step_memory_kb`), so steps side by side in one
    machine's slot stay within what the planner gave each."""
    MAIN_CGROUP.mkdir(parents=True, exist_ok=True)
    for pid in (CGROUP / "cgroup.procs").read_text().split():
        with contextlib.suppress(OSError):
            (MAIN_CGROUP / "cgroup.procs").write_text(pid)
    (CGROUP / "cgroup.subtree_control").write_text(CONTROLLERS)
    (STEP_CGROUP / "cgroup.subtree_control").write_text(CONTROLLERS)
    (STEP_CGROUP / "memory.max").write_text(str(memory_kb * 1024))
    with contextlib.suppress(FileNotFoundError):
        (STEP_CGROUP / "memory.swap.max").write_text("0")
    BOX_CGROUPS.mkdir(exist_ok=True)
    (BOX_CGROUPS / "cgroup.subtree_control").write_text(CONTROLLERS)


def set_up_devices() -> None:
    """Make `BOX_DEV`, the `/dev` every box gets: the container's null,
    zero, random and urandom bound in, the `fd` and standard stream links,
    and an empty `shm`, where each box's own `/box/shm` is bound. A box
    then cannot even name the machine's devices."""
    (BOX_DEV / "shm").mkdir(parents=True, exist_ok=True)
    for name in BOX_DEVICES:
        node = BOX_DEV / name
        if not node.exists():
            node.touch()
        if not node.is_mount():
            mount(Path("/dev") / name, node, None, MS_BIND, None)
    for name, target in BOX_LINKS.items():
        if not (BOX_DEV / name).is_symlink():
            (BOX_DEV / name).symlink_to(target)


@contextlib.contextmanager
def fresh_box(number: int, size_kb: int) -> Iterator[Path]:
    """Make box `number` afresh and give its folder, a tmpfs of its own of
    `size_kb` kilobytes and `BOX_INODES` files and folders; on every way out,
    unmount the tmpfs and clean the box up.

    A tmpfs an earlier item left mounted, should its clean-up not have run,
    is unmounted first: isolate refuses to clean a box with a mount in it,
    and `--init` cleans first. A fresh box every item is required, not only
    tidy: a box run twice reads the first run's peak memory and kill.
    """
    folder = BOX_ROOT / str(number) / "box"
    if folder.is_mount():
        unmount(folder)
    isolate_box(number, "--init")
    try:
        mount(
            f"box-{number}",
            folder,
            "tmpfs",
            MS_NOSUID | MS_NODEV,
            f"size={size_kb}k,nr_inodes={BOX_INODES},mode=0700",
        )
    except BaseException:
        quietly(lambda: isolate_box(number, "--cleanup"))
        raise
    try:
        yield folder
    except BaseException:
        quietly(lambda: close_box(number, folder))
        raise
    close_box(number, folder)


def close_box(number: int, folder: Path) -> None:
    """Unmount box `number`'s tmpfs, which frees all it held, and clean the
    box up, its cgroup with it."""
    unmount(folder)
    isolate_box(number, "--cleanup")


def isolate_box(number: int, action: str) -> None:
    """Run `isolate --cg` on box `number` for `action`, `--init` or
    `--cleanup`, raising on failure."""
    done = subprocess.run(
        [ISOLATE, "--cg", f"--box-id={number}", action],
        capture_output=True,
        check=False,
    )
    if done.returncode != 0:
        said = isolate_said(done.returncode, done.stderr)
        raise PrimitiveError(f"isolate {action} of box {number} failed: {said}")


def mount(
    source: Path | str, target: Path, kind: str | None, flags: int, data: str | None
) -> None:
    """Mount `source` on `target`, as mount(2), raising on failure."""
    result = libc().mount(
        os.fsencode(source),
        os.fsencode(target),
        None if kind is None else kind.encode(),
        ctypes.c_ulong(flags),
        None if data is None else data.encode(),
    )
    if result != 0:
        code = ctypes.get_errno()
        raise PrimitiveError(f"{target} could not be mounted: {os.strerror(code)}")


def unmount(target: Path, flags: int = 0) -> None:
    """Unmount what is mounted on `target`, as umount2(2) with `flags`,
    raising on failure."""
    if libc().umount2(os.fsencode(target), flags) != 0:
        code = ctypes.get_errno()
        raise PrimitiveError(f"{target} could not be unmounted: {os.strerror(code)}")


def libc() -> ctypes.CDLL:
    """The C library, for mount and umount2."""
    return ctypes.CDLL(None, use_errno=True)


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


MAX_PROGRAMS = 8


PROGRAM_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")


WALL_CLOCK = "wall_clock"


LINK = re.compile(r"([a-z][a-z0-9_]{0,63}) -> ([a-z][a-z0-9_]{0,63})")


FACTS = ("time_ms", "memory_kb", "ended", "signal", "ended_at", "stopped_by")


OVER_LIMIT = frozenset({"time_limit", "memory_limit", "output_limit"})


PIPES = "/pipes"


PIPE_MODE = 0o240


INTERRUPTED = "Interrupted"


WALL_CLOCK_MESSAGE = "Time limit exceeded (wall clock)"


PROGRAMS_GIVEN = ("program", "manifest")


@dataclass(frozen=True)
class Entry:
    """One program of an item: the name the step gives it, the program, the
    two limits, the arguments, the named files placed in its working
    directory, the reports it declares when it is given a manifest,
    whether its ending stops the others, and the item folder of the step's
    cache its cached named files are in.
    """

    name: str
    program: Path
    time_limit: float
    memory_limit: float
    args: tuple[str, ...] = ()
    files: dict[str, Path] = field(default_factory=dict)
    manifest: dict[str, Declared] | None = None
    stops_others: bool = False
    cached: Path | None = None

    @property
    def wall_limit(self) -> float:
        """The wall-clock seconds the program may take: twice the time limit
        plus one, as under exec.

        The time limit itself is on CPU time; the wall-clock limit stops a
        program that sleeps or waits without using the CPU, such as one
        waiting on another that waits on it.
        """
        return 2 * self.time_limit + 1

    @property
    def memory_limit_kb(self) -> int:
        """The memory limit in kilobytes, the unit peak memory is measured in."""
        return int(self.memory_limit * 1024)

    @property
    def time_limit_ms(self) -> int:
        """The time limit in whole milliseconds, rounded up: the least time a
        program stopped at it reports. Rounding to a microsecond first keeps
        a limit such as 0.1 s, a little over 100 ms as a float, at 100.
        """
        return math.ceil(round(self.time_limit * 1000, 3))


@dataclass(frozen=True)
class Link:
    """One link: what `writer` writes, `reader` reads."""

    writer: str
    reader: str


@dataclass(frozen=True)
class ProgramsItem:
    """One run: its id, its place in the batch from 1, its programs in the
    order given, the `n`th in box `n`, and the links between them in the
    order given, which says which link is a program's first."""

    run: str
    number: int
    programs: tuple[Entry, ...]
    links: tuple[Link, ...] = ()


@dataclass(frozen=True)
class ProgramsProgram:
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


@dataclass(frozen=True)
class ProgramsRun:
    """What one program did, as isolate's meta file and the box's tmpfs tell
    it.

    `status` is isolate's: empty when the program exited 0, `RE` for another
    exit code, `SG` for a signal, `TO` for a time limit. `returncode` is the
    exit code, or minus the signal that killed it. `memory_kb` is the peak of
    the box's cgroup (`cg-mem`). `out_of_memory` says the kernel killed a
    process of the box for the box's memory, and `no_inodes` that the box's
    tmpfs had no file or folder left to give. `interrupted` says isolate
    stopped the box because exec asked, and `wall_clock` that the wall
    clock stopped it.
    """

    status: str
    returncode: int
    cpu_seconds: float
    memory_kb: int
    output_bytes: int
    out_of_memory: bool = False
    no_inodes: bool = False
    interrupted: bool = False
    wall_clock: bool = False


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
    run: ProgramsRun | None = None


def programs_read_item(root: Path, entry: object, number: int) -> ProgramsItem:
    """Check one batch entry, the `number`th, and return it as an item."""
    if not isinstance(entry, dict) or not isinstance(entry.get("inputs"), dict):
        raise PrimitiveError("a batch item has no inputs object")
    run = entry.get("run")
    if not isinstance(run, str) or len(run) > 255 or not RUN_ID.fullmatch(run):
        raise PrimitiveError(f"the batch item run {run!r} is not a run id")
    inputs = entry["inputs"]
    value = inputs.get("programs")
    if not isinstance(value, dict) or set(value) != {"programs"}:
        raise PrimitiveError(f"the input named programs of run {run} is not programs")
    given = value["programs"]
    if not isinstance(given, dict) or not 0 < len(given) <= MAX_PROGRAMS:
        raise PrimitiveError(
            f"the input named programs of run {run} does not hold one to "
            f"{MAX_PROGRAMS} programs"
        )
    programs = tuple(read_entry(root, name, each, run) for name, each in given.items())
    links = read_links(inputs.get("links", []), {each.name for each in programs}, run)
    item = ProgramsItem(run=run, number=number, programs=programs, links=links)
    named = pipe_names(item)
    for program in programs:
        clash = sorted(program.files.keys() & set(named[program.name]))
        if clash:
            raise PrimitiveError(
                f"the program {program.name} of run {run} has a named file "
                f"{clash[0]}, the name of one of its pipes"
            )
    return item


def read_entry(root: Path, name: object, inputs: object, run: str) -> Entry:
    """Check one program of run `run`, `name`, and return it."""
    if not isinstance(name, str) or not PROGRAM_NAME.fullmatch(name):
        raise PrimitiveError(f"the run {run} names a program {name!r}, not a name")
    if name == WALL_CLOCK:
        raise PrimitiveError(
            f"the run {run} names a program {WALL_CLOCK}, what stopped_by says "
            "of the wall clock"
        )
    if not isinstance(inputs, dict):
        raise PrimitiveError(f"the program {name} of run {run} has no inputs object")
    what = f"program {name} of run {run}"
    manifest = None
    if "manifest" in inputs:
        manifest = read_manifest(
            programs_input_file(root, inputs, "manifest", what),
            f"the input named manifest of {what}",
            PROGRAMS_OWN_NAMES,
        )
    stops_others = inputs.get("stops_others", False)
    if not isinstance(stops_others, bool):
        raise PrimitiveError(
            f"the input named stops_others of {what} is not true or false"
        )
    files = programs_named_files(root, inputs, "files", what)
    entry = Entry(
        name=name,
        program=programs_input_file(root, inputs, "program", what),
        time_limit=programs_positive_number(inputs, "time_limit", what),
        memory_limit=programs_positive_number(inputs, "memory_limit", what),
        args=programs_arguments(inputs, what),
        files=files,
        manifest=manifest,
        stops_others=stops_others,
        cached=cached_folder(
            root,
            [
                *(
                    programs_input_file(root, inputs, n, what)
                    for n in PROGRAMS_GIVEN
                    if n in inputs
                ),
                *files.values(),
            ],
            f"the {what}",
        ),
    )
    # isolate reads a limit of 0 as none, and takes the time in whole
    # milliseconds and the memory in whole kilobytes.
    if entry.time_limit < 0.001:
        raise PrimitiveError(
            f"the input named time_limit of {what} is under a millisecond, "
            "which isolate would read as no limit"
        )
    if entry.memory_limit_kb == 0:
        raise PrimitiveError(
            f"the input named memory_limit of {what} is under a kilobyte, "
            "which isolate would read as no limit"
        )
    return entry


def read_links(value: object, names: set[str], run: str) -> tuple[Link, ...]:
    """Read `links`, a list of `"<writer> -> <reader>"`, each between two
    programs of the run and given once. Absent, there are none."""
    what = f"the input named links of run {run}"
    if not isinstance(value, list):
        raise PrimitiveError(f"{what} is not a list of links")
    links: list[Link] = []
    for text in value:
        matched = LINK.fullmatch(text) if isinstance(text, str) else None
        if matched is None:
            raise PrimitiveError(f"{what} holds {text!r}, not <from> -> <to>")
        link = Link(matched.group(1), matched.group(2))
        for name in (link.writer, link.reader):
            if name not in names:
                raise PrimitiveError(f"{what} links {name}, not one of its programs")
        if link.writer == link.reader:
            raise PrimitiveError(f"{what} links {link.writer} to itself")
        if link in links:
            raise PrimitiveError(f"{what} gives {text} twice")
        links.append(link)
    return tuple(links)


def pipe_names(item: ProgramsItem) -> dict[str, list[str]]:
    """The named pipes in each program's working directory: for each link
    after a program's first out, `to_<reader>`, and after its first in,
    `from_<writer>`, in the order the links are given."""
    names: dict[str, list[str]] = {entry.name: [] for entry in item.programs}
    writes: set[str] = set()
    reads: set[str] = set()
    for link in item.links:
        if link.writer in writes:
            names[link.writer].append(f"to_{link.reader}")
        writes.add(link.writer)
        if link.reader in reads:
            names[link.reader].append(f"from_{link.writer}")
        reads.add(link.reader)
    return names


def programs_input_file(
    root: Path, inputs: dict[str, object], name: str, what: str
) -> Path:
    """Resolve a file input of `what`, a program, to a path, refusing
    anything outside `in/`."""
    return in_file(root, inputs.get(name), f"the input named {name} of {what}")


def programs_named_files(
    root: Path, inputs: dict[str, object], name: str, what: str
) -> dict[str, Path]:
    """Read a `named_files` input of `what`, `{"files": {<name>: {"file":
    "in/..."}}}`, as each name and its path under `in/`. Absent, there are
    none.
    """
    if name not in inputs:
        return {}
    value = inputs[name]
    what = f"the input named {name} of {what}"
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


def programs_positive_number(inputs: dict[str, object], name: str, what: str) -> float:
    """Read a number input of `what` that must be above zero."""
    value = inputs.get(name)
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not 0 < value < math.inf
    ):
        raise PrimitiveError(
            f"the input named {name} of {what} is not a positive number"
        )
    return float(value)


def programs_arguments(inputs: dict[str, object], what: str) -> tuple[str, ...]:
    """Read `args`, the optional text appended to the program's command line,
    split on spaces: a run of spaces separates two arguments, and spaces at
    either end separate nothing. There is no quoting, so no argument holds a
    space. Absent, the program gets no arguments.
    """
    value = inputs.get("args")
    if value is None:
        return ()
    if not isinstance(value, str):
        raise PrimitiveError(f"the input named args of {what} is not text")
    return tuple(part for part in value.split(" ") if part)


def programs_run_batch(
    root: Path, items: list[ProgramsItem]
) -> list[dict[str, object]]:
    """Check the step's memory holds each item's boxes side by side
    (`check_memory`), set the step up for its boxes, capped at that memory
    (`step_memory_kb`), then run the items one after another, in the order
    given, each in a fresh set of boxes with fresh copies of its programs,
    and return one outputs entry per item.

    isolate's meta files and its own standard error, and the named pipes, go
    in a folder of exec's in the container's `/tmp`, which no box sees
    but for its own folder of pipes.
    """
    users = box_users()
    most = max((len(item.programs) for item in items), default=0)
    if most > len(users):
        raise PrimitiveError(
            f"a run has {most} programs, and {BOX_USERS_VARIABLE} gives "
            f"{len(users)} box users, one for each"
        )
    memory_kb = step_memory_kb()
    for item in items:
        needed = sum(programs_box_size_kb(entry) for entry in item.programs)
        check_memory(needed, memory_kb, item.run)
    set_up_step(users, memory_kb)
    scratch = Path(tempfile.mkdtemp(prefix="programs-"))
    kinds: dict[Path, str] = {}
    results: list[dict[str, object]] = []
    try:
        for item in items:
            for entry in item.programs:
                if entry.program not in kinds:
                    kinds[entry.program] = programs_detect(entry.program)
            outputs = run_programs(root, item, kinds, scratch, users)
            results.append({"run": item.run, "outputs": outputs})
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return results


def programs_detect(program: Path) -> str:
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
        "a program is not a native executable, a Python zip application or a Java jar"
    )


def run_programs(
    root: Path, item: ProgramsItem, kinds: dict[Path, str], scratch: Path, users: range
) -> dict[str, object]:
    """Run one item's programs side by side, each in a fresh box, and return
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
        programs: list[ProgramsProgram] = []
        for number, entry in enumerate(item.programs):
            box = stack.enter_context(fresh_box(number, programs_box_size_kb(entry)))
            each = Running(
                entry=entry,
                number=number,
                kind=kinds[entry.program],
                box=box,
                meta=scratch / f"meta-{number}",
                errors=scratch / f"errors-{number}",
            )
            programs.append(programs_prepare(entry, each.kind, box))
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
    programs_write_json(out / "facts.json", facts)
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
    item: ProgramsItem,
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


def programs_box_size_kb(entry: Entry) -> int:
    """The size of a program's box tmpfs, in kilobytes: its memory limit,
    which holds what the program writes there before the size does, as every
    page it writes is charged to the box's memory, plus the program and the
    named files exec copies in, each to whole 4 KB pages. A cached named
    file is not copied but bound in."""
    named = entry.files.values()
    copied = [entry.program, *(path for path in named if path.parent != entry.cached)]
    return entry.memory_limit_kb + sum(
        4 * -(-path.stat().st_size // 4096) for path in copied
    )


def programs_prepare(entry: Entry, kind: str, box: Path) -> ProgramsProgram:
    """Copy the program, of format `kind`, into its box, before its clock
    starts, executable for a native executable, since the file under `in/`
    may not be, and readable for the others.

    The program is the copy's owner while it runs, so it may change its
    mode, times or attributes; the copy goes with the box.
    """
    try:
        shutil.copyfile(entry.program, box / "program")
    except OSError as error:
        raise PrimitiveError(
            f"the program {entry.name} could not be copied: {error.strerror}"
        ) from error
    (box / "program").chmod(0o755 if kind == "native" else 0o644)
    return ProgramsProgram(kind, Path("/box/program"))


def start(
    each: Running, program: ProgramsProgram, pipes: Path, stack: contextlib.ExitStack
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
            each.run = ProgramsRun("RE", 127, 0.0, 0, 0)
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
    over = judge(each.run, each.entry, each.kind) in OVER_LIMIT
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


def finish(each: Running, code: int) -> ProgramsRun:
    """What a program did, from its isolate's exit `code` and meta file, as
    exec reads them. An isolate stopped before it could take a stop as one,
    which kills it before its box is made, is a program stopped before it
    ran. isolate failing itself (`XX`) is an error of the step.
    """
    found = read_meta(each.meta)
    status = found.get("status", "")
    asked = each.stopped_by is not None
    if asked and code == -signal.SIGTERM and not found:
        return ProgramsRun("SG", -signal.SIGKILL, 0.0, 0, 0, interrupted=True)
    if code not in (0, 1) or status == "XX":
        said = found.get("message") or each.errors.read_bytes()
        raise PrimitiveError(
            f"isolate could not run the program {each.entry.name}: "
            f"{isolate_said(code, said)}"
        )
    interrupted = asked and found.get("message") == INTERRUPTED
    if interrupted:
        returncode = -signal.SIGKILL
    elif "exitcode" in found:
        returncode = int(found["exitcode"])
    elif "exitsig" in found:
        returncode = -int(found["exitsig"])
    else:
        returncode = 0 if status == "" else -9
    return ProgramsRun(
        status=status,
        returncode=returncode,
        cpu_seconds=float(found.get("time", "0")),
        memory_kb=int(found.get("cg-mem", "0")),
        output_bytes=each.printed[0],
        out_of_memory=found.get("cg-oom-killed") == "1",
        interrupted=interrupted,
        wall_clock=status == "TO" and found.get("message") == WALL_CLOCK_MESSAGE,
    )


def program_facts(each: Running, item: ProgramsItem) -> dict[str, object]:
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
    outcome = judge(run, entry, each.kind)
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


def programs_write_json(path: Path, document: Mapping[str, object]) -> None:
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
