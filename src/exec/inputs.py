"""Reading `inputs.json`: the batch's items, the inputs of each of their
programs, and the manifest a program that reports declares, all checked
before any box is made.
"""

import json
import math
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from exec.shared import FILE_NAME, SCHEMA_VERSION, PrimitiveError

RUN_ID = re.compile(r"[A-Za-z0-9_-]+")
"""A run's id, which only the forge knows the meaning of. An item's output
goes under its place in the batch, not its id."""
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
class Entry:
    """One program of a run: the name the step gives it, the program, the
    two limits, the arguments, and the named files placed in its working
    directory: `files` it reads, and `loads`, code the program loads, such
    as the entrant's module a test harness imports. `stdin` is the file for
    its standard input, if any. `manifest` is the reports the program
    declares, when it is one that reports, `self_reported` says it runs the
    entrant's code, `stops_others` that its ending stops the run's other
    programs, and `cached` is the item folder of the step's cache its cached
    named files are in. `loads`, `stdin` and `self_reported` are the
    one-program form's alone, and `stops_others` the several-programs
    form's.
    """

    name: str
    program: Path
    time_limit: float
    memory_limit: float
    args: tuple[str, ...] = ()
    files: dict[str, Path] = field(default_factory=dict)
    loads: dict[str, Path] = field(default_factory=dict)
    stdin: Path | None = None
    manifest: dict[str, Declared] | None = None
    self_reported: bool = False
    stops_others: bool = False
    cached: Path | None = None

    @property
    def wall_limit(self) -> float:
        """The wall-clock seconds the program may take: twice the time limit
        plus one.

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
class Item:
    """One run: its id, its place in the batch from 1, its programs in the
    order given, the `n`th in box `n`, the links between them in the order
    given, which says which link is a program's first, and whether it is
    of the several-programs form. An item of the one-program form is one
    program and no links."""

    run: str
    number: int
    programs: tuple[Entry, ...]
    links: tuple[Link, ...] = ()
    several: bool = True


def read_inputs(root: Path) -> list[Item]:
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


def read_item(root: Path, entry: object, number: int) -> Item:
    """Check one batch entry, the `number`th, and return it as an item: of
    the one-program form, `program` and its inputs, one program and no
    links; or of the several-programs form, `programs` and `links`."""
    if not isinstance(entry, dict) or not isinstance(entry.get("inputs"), dict):
        raise PrimitiveError("a batch item has no inputs object")
    run = entry.get("run")
    if not isinstance(run, str) or len(run) > 255 or not RUN_ID.fullmatch(run):
        raise PrimitiveError(f"the batch item run {run!r} is not a run id")
    inputs = entry["inputs"]
    if "program" in inputs and "programs" in inputs:
        raise PrimitiveError("a batch item gives both program and programs")
    if "programs" not in inputs:
        program = read_program(root, "program", inputs, f"run {run}", several=False)
        return Item(run=run, number=number, programs=(program,), several=False)
    value = inputs["programs"]
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
    item = Item(run=run, number=number, programs=programs, links=links)
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
    """Check one program of run `run` of the several-programs form, `name`,
    and return it."""
    if not isinstance(name, str) or not PROGRAM_NAME.fullmatch(name):
        raise PrimitiveError(f"the run {run} names a program {name!r}, not a name")
    if name == WALL_CLOCK:
        raise PrimitiveError(
            f"the run {run} names a program {WALL_CLOCK}, what stopped_by says "
            "of the wall clock"
        )
    if not isinstance(inputs, dict):
        raise PrimitiveError(f"the program {name} of run {run} has no inputs object")
    return read_program(root, name, inputs, f"program {name} of run {run}", True)


def read_program(
    root: Path, name: str, inputs: dict[str, object], what: str, several: bool
) -> Entry:
    """Check the inputs of one program, `what`, as the errors name it, and
    return it as the entry `name`: of the several-programs form, whose own
    outputs no report may be named for and which reads `stops_others`, or
    of the one-program form, which reads `loads`, `stdin` and
    `self_reported`."""
    files = named_files(root, inputs, "files", what)
    loads = {} if several else named_files(root, inputs, "loads", what)
    clash = sorted(files.keys() & loads.keys())
    if clash:
        raise PrimitiveError(
            f"the {what} names {clash[0]} in both files and loads; each name "
            "is one file in its working directory"
        )
    given: list[Path] = []
    manifest = None
    if "manifest" in inputs:
        given.append(input_file(root, inputs, "manifest", what))
        manifest = read_manifest(
            given[-1],
            f"the input named manifest of {what}",
            PROGRAMS_OWN_NAMES if several else OWN_NAMES,
        )
    self_reported = not several and flag(inputs, "self_reported", what)
    stops_others = several and flag(inputs, "stops_others", what)
    if self_reported and manifest is None:
        raise PrimitiveError(
            f"the {what} is self_reported without a manifest, so it reports nothing"
        )
    program = input_file(root, inputs, "program", what)
    stdin = None
    if not several and "stdin" in inputs:
        stdin = input_file(root, inputs, "stdin", what)
        given.append(stdin)
    entry = Entry(
        name=name,
        program=program,
        time_limit=positive_number(inputs, "time_limit", what),
        memory_limit=positive_number(inputs, "memory_limit", what),
        args=arguments(inputs, what),
        files=files,
        loads=loads,
        stdin=stdin,
        manifest=manifest,
        self_reported=self_reported,
        stops_others=stops_others,
        cached=cached_folder(
            root,
            [program, *given, *files.values(), *loads.values()],
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


def input_file(root: Path, inputs: dict[str, object], name: str, what: str) -> Path:
    """Resolve a file input of `what`, a program, to a path, refusing
    anything outside `in/`."""
    return in_file(root, inputs.get(name), f"the input named {name} of {what}")


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


def named_files(
    root: Path, inputs: dict[str, object], name: str, what: str
) -> dict[str, Path]:
    """Read a `named_files` input of `what`, a program, `{"files": {<name>:
    {"file": "in/..."}}}`, as each name and its path under `in/`. Absent,
    there are none.
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


def positive_number(inputs: dict[str, object], name: str, what: str) -> float:
    """Read a number input of `what`, a program, that must be above zero."""
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


def flag(inputs: dict[str, object], name: str, what: str) -> bool:
    """Read a true-or-false input of `what`, a program. Absent, it is false."""
    value = inputs.get(name, False)
    if not isinstance(value, bool):
        raise PrimitiveError(f"the input named {name} of {what} is not true or false")
    return value


def arguments(inputs: dict[str, object], what: str) -> tuple[str, ...]:
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


MAX_PROGRAMS = 8
PROGRAM_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
WALL_CLOCK = "wall_clock"
LINK = re.compile(r"([a-z][a-z0-9_]{0,63}) -> ([a-z][a-z0-9_]{0,63})")


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


def pipe_names(item: Item) -> dict[str, list[str]]:
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
