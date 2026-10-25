"""The program's own logic, in-process.

The tests of its logic run anywhere. The tests that run programs need root
and isolate, in a privileged container entered through `userns.py`, so exec
makes its boxes as it does in a step: they are skipped elsewhere, and
`test_image.py` runs them in the test container `tests/Dockerfile` builds
on the image. The runs use Python binaries on the image's interpreter.
"""

import errno
import json
import os
import re
import resource
import stat
import sys
import time
import traceback
import zipfile
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import exec as primitive
from support import (
    BOX_USERS,
    PROGRAM,
    SCORE,
    batch_inputs,
    by_run,
    declaration,
    peek_input,
    peeked,
    printed,
    python_binary,
    returned_outcomes,
)

IN_BOXES = os.environ.get("UNICON_TESTS_IN_BOXES") == "1"
"""Set in the test container, where a test that runs programs must run:
there a missing isolate or root fails it rather than skipping it."""


@pytest.fixture
def boxes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give exec the tests' box users, or skip a test that runs programs
    where exec cannot make boxes."""
    able = sys.platform == "linux" and os.geteuid() == 0
    if not (able and Path(primitive.ISOLATE).is_file()):
        if IN_BOXES:
            pytest.fail("the test container has no root with isolate")
        pytest.skip("runs programs in isolate boxes: run in the test container")
    monkeypatch.setenv(primitive.BOX_USERS_VARIABLE, BOX_USERS)


@pytest.fixture
def run(boxes: None, tmp_path: Path) -> Any:
    """Run the program over a batch of the test program's cases."""
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()

    def batch(items: list[tuple[str, str]], **options: Any) -> dict[str, Any]:
        work = tmp_path / "work"
        batch_inputs(work, binary, items, **options)
        assert primitive.main(["exec", str(work)]) == 0
        document: dict[str, Any] = json.loads(
            (work / "outputs.json").read_text(encoding="utf-8")
        )
        return document

    return batch


def make_run(**changes: Any) -> primitive.Run:
    """A run that exited cleanly well inside every limit, with some fields changed."""
    fields: dict[str, Any] = {
        "status": "",
        "returncode": 0,
        "cpu_seconds": 0.1,
        "memory_kb": 1000,
        "output_bytes": 10,
    }
    return primitive.Run(**(fields | changes))


ITEM = primitive.Item(
    "main-1", 1, Path("b"), Path("i"), time_limit=1.0, memory_limit=64
)


@pytest.mark.parametrize(
    ("limit", "ms"), [(1.0, 1000), (0.1, 100), (0.3, 300), (2.5, 2500), (0.0005, 1)]
)
def test_the_time_limit_in_milliseconds_is_rounded_up(limit: float, ms: int) -> None:
    item = primitive.Item("main-1", 1, Path("b"), Path("i"), limit, 64)
    assert item.time_limit_ms == ms


OVER = primitive.OUTPUT_LIMIT + 1
JUDGED: list[tuple[dict[str, Any], str, str]] = [
    ({}, "native", "accepted"),
    (
        {"status": "SG", "returncode": -9, "out_of_memory": True},
        "native",
        "memory_limit",
    ),
    ({"status": "RE", "returncode": 1, "no_inodes": True}, "native", "memory_limit"),
    ({"no_inodes": True}, "native", "memory_limit"),
    ({"status": "TO", "returncode": -9}, "native", "time_limit"),
    (
        {"status": "SG", "returncode": -13, "output_bytes": OVER},
        "python",
        "output_limit",
    ),
    ({"output_bytes": OVER}, "native", "output_limit"),
    ({"status": "RE", "returncode": 3}, "java", "memory_limit"),
    ({"status": "RE", "returncode": 3}, "native", "runtime_error"),
    ({"status": "SG", "returncode": -11}, "native", "runtime_error"),
    ({"status": "RE", "returncode": 127}, "native", "runtime_error"),
    ({"status": "TO", "out_of_memory": True}, "native", "memory_limit"),
    ({"status": "TO", "output_bytes": OVER}, "native", "time_limit"),
]
"""Runs and how `judge` ends each, one of every way it decides, and the order
between them."""


@pytest.mark.parametrize(("changes", "kind", "outcome"), JUDGED)
def test_judge(changes: dict[str, Any], kind: str, outcome: str) -> None:
    """Memory first, then time, then output, then how the program ended."""
    assert primitive.judge(make_run(**changes), ITEM, kind) == outcome


def test_the_declaration_says_what_each_outcome_it_reports_means() -> None:
    """The outcome port's outcomes are exactly the ones `judge` gives and a
    report's verdict, each with a line saying when, for the workflow editor's
    drawer.
    """
    reported = {
        primitive.judge(make_run(**changes), ITEM, kind) for changes, kind, _ in JUDGED
    } | {
        primitive.verdict_outcome(verdict)
        for verdict in ("0", "1", *primitive.VERDICT_WORDS)
    }

    outcomes = declaration()["outputs"]["outcome"]["outcomes"]

    assert list(outcomes) == [
        "accepted",
        "wrong_answer",
        "time_limit",
        "memory_limit",
        "output_limit",
        "runtime_error",
    ]
    assert set(outcomes) == reported
    assert all(isinstance(when, str) and "\n" not in when for when in outcomes.values())


def test_the_declaration_says_every_outcome_judge_can_give() -> None:
    """The declared outcomes are every one `judge`, a report's verdict and
    a self-reported run's fault can give, read from their source rather than
    from the runs `JUDGED` picks, so a new way to an outcome needs its line
    too.
    """
    outcomes = declaration()["outputs"]["outcome"]["outcomes"]

    found = returned_outcomes(primitive.judge) | returned_outcomes(
        primitive.verdict_outcome
    )
    assert set(outcomes) == found | returned_outcomes(primitive.reported)


def test_detect(tmp_path: Path) -> None:
    """The three formats are told apart by content, and anything else is an error."""
    native = tmp_path / "native"
    native.write_bytes(b"\x7fELF" + bytes(60))
    python = python_binary(tmp_path / "python", "print(1)\n")
    java = tmp_path / "java"
    with zipfile.ZipFile(java, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\r\n")
    other = tmp_path / "other"
    other.write_text("#!/bin/sh\necho hi\n")
    assert primitive.detect(native) == "native"
    assert primitive.detect(python) == "python"
    assert primitive.detect(java) == "java"
    with pytest.raises(primitive.PrimitiveError, match="not a native executable"):
        primitive.detect(other)


@pytest.mark.parametrize(
    ("inputs", "message"),
    [
        ({"time_limit": 0}, "time_limit of run main-1 is not a positive number"),
        ({"time_limit": True}, "time_limit of run main-1 is not a positive number"),
        ({"time_limit": float("nan")}, "inputs.json is not valid JSON"),
        ({"memory_limit": "64"}, "memory_limit of run main-1 is not a positive"),
        (
            {"program": {"file": "in/../inputs.json"}},
            "program of run main-1 is outside",
        ),
        ({"stdin": {"file": "in/none"}}, "stdin of run main-1 is not in the working"),
        ({"stdin": "in/2/input"}, "stdin of run main-1 is not a file"),
        ({"args": 3}, "args of run main-1 is not text"),
        ({"time_limit": 0.0009}, "time_limit of run main-1 is under a millisecond"),
        ({"memory_limit": 0.0005}, "memory_limit of run main-1 is under a kilobyte"),
    ],
)
def test_inputs_it_cannot_use_are_an_error(
    tmp_path: Path, inputs: dict[str, Any], message: str
) -> None:
    """Anything that stops the primitive working is `error` alone, in one sentence."""
    batch_inputs(tmp_path, b"", [("main-1", "")])
    document = json.loads((tmp_path / "inputs.json").read_text())
    document["batch"][0]["inputs"].update(inputs)
    (tmp_path / "inputs.json").write_text(json.dumps(document))
    assert primitive.main(["exec", str(tmp_path)]) == 0
    result = json.loads((tmp_path / "outputs.json").read_text())
    assert set(result) == {"schema_version", "error"}
    assert message in result["error"]


def test_a_limit_too_large_to_be_a_number_is_an_error(tmp_path: Path) -> None:
    """`1e999` is valid JSON, which Python reads as infinity."""
    batch_inputs(tmp_path, b"", [("main-1", "")])
    text = (tmp_path / "inputs.json").read_text()
    document = json.loads(text)
    document["batch"][0]["inputs"]["memory_limit"] = 123456789
    (tmp_path / "inputs.json").write_text(
        json.dumps(document).replace("123456789", "1e999")
    )
    assert primitive.main(["exec", str(tmp_path)]) == 0
    result = json.loads((tmp_path / "outputs.json").read_text())
    assert "memory_limit of run main-1 is not a positive number" in result["error"]


def test_a_version_5_batch_is_an_error(tmp_path: Path) -> None:
    """exec speaks contract version 6 alone, whose items are keyed by run."""
    batch_inputs(tmp_path, b"", [("main-1", "")])
    document = json.loads((tmp_path / "inputs.json").read_text())
    document["schema_version"] = 5
    (tmp_path / "inputs.json").write_text(json.dumps(document))
    primitive.main(["exec", str(tmp_path)])
    result = json.loads((tmp_path / "outputs.json").read_text())
    assert "not written against contract version 6" in result["error"]
    assert result["schema_version"] == 6


@pytest.mark.parametrize(
    ("runs", "message"),
    [
        (["main-1", "main-1"], "for the same run"),
        (["main/1"], "is not a run id"),
        (["main.1"], "is not a run id"),
        ([""], "is not a run id"),
        (["x" * 256], "is not a run id"),
    ],
)
def test_items_must_be_for_distinct_runs(
    tmp_path: Path, runs: list[str], message: str
) -> None:
    """Each item names one run, by an id of letters, digits, `_` and `-`, and no
    two the same one."""
    batch_inputs(tmp_path, b"", [(f"main-{n}", "") for n in range(len(runs))])
    document = json.loads((tmp_path / "inputs.json").read_text())
    for entry, run in zip(document["batch"], runs, strict=True):
        entry["run"] = run
    (tmp_path / "inputs.json").write_text(json.dumps(document))
    primitive.main(["exec", str(tmp_path)])
    assert message in json.loads((tmp_path / "outputs.json").read_text())["error"]


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"x": {"file": "in/2/input"}}, "files of run main-1 is not named files"),
        ({"files": [{"file": "in/2/input"}]}, "files of run main-1 is not named files"),
        ({"files": {"../x": {"file": "in/2/input"}}}, "a file '../x', not a plain"),
        ({"files": {".": {"file": "in/2/input"}}}, "a file '.', not a plain name"),
        ({"files": {".hidden": {"file": "in/2/input"}}}, "'.hidden', not a plain"),
        ({"files": {"a/b": {"file": "in/2/input"}}}, "a file 'a/b', not a plain"),
        ({"files": {"": {"file": "in/2/input"}}}, "a file '', not a plain name"),
        ({"files": {"a": {"file": "in/../inputs.json"}}}, "file a of the input"),
        ({"files": {"a": {"file": "in/none"}}}, "is not in the working directory"),
        ({"files": {"a": "in/2/input"}}, "file a of the input named files"),
    ],
)
def test_named_files_it_cannot_use_are_an_error(
    tmp_path: Path, value: object, message: str
) -> None:
    batch_inputs(tmp_path, b"", [("main-1", "")])
    document = json.loads((tmp_path / "inputs.json").read_text())
    document["batch"][0]["inputs"]["files"] = value
    (tmp_path / "inputs.json").write_text(json.dumps(document))
    primitive.main(["exec", str(tmp_path)])
    assert message in json.loads((tmp_path / "outputs.json").read_text())["error"]


def test_a_name_in_both_files_and_loads_is_an_error(tmp_path: Path) -> None:
    """Each name is one file in the run's working directory; the save should
    have caught two."""
    named = {"main-1": {"solution.py": b"x"}}
    batch_inputs(tmp_path, b"", [("main-1", "")], files=named, loads=named)
    primitive.main(["exec", str(tmp_path)])
    result = json.loads((tmp_path / "outputs.json").read_text())
    assert "names solution.py in both files and loads" in result["error"]


@pytest.mark.parametrize(
    ("given", "users"),
    [("60000-60009", range(60000, 60010)), ("10000-10000", range(10000, 10001))],
)
def test_the_box_users_are_the_steps_range(
    monkeypatch: pytest.MonkeyPatch, given: str, users: range
) -> None:
    monkeypatch.setenv(primitive.BOX_USERS_VARIABLE, given)
    assert primitive.box_users() == users


@pytest.mark.parametrize(
    ("given", "message"),
    [
        (None, "UNICON_BOX_USERS is not set"),
        ("60000", "is not <first>-<last>"),
        ("60000-", "is not <first>-<last>"),
        (" 60000-60009", "is not <first>-<last>"),
        ("-1-5", "is not <first>-<last>"),
        ("9999-10005", "not a range within 10000 to 65000"),
        ("64999-65001", "not a range within 10000 to 65000"),
        ("60009-60000", "not a range within 10000 to 65000"),
    ],
)
def test_box_users_it_cannot_use_are_an_error(
    monkeypatch: pytest.MonkeyPatch, given: str | None, message: str
) -> None:
    if given is None:
        monkeypatch.delenv(primitive.BOX_USERS_VARIABLE, raising=False)
    else:
        monkeypatch.setenv(primitive.BOX_USERS_VARIABLE, given)
    with pytest.raises(primitive.PrimitiveError, match=re.escape(message)):
        primitive.box_users()


@pytest.mark.parametrize("first", ["0 0 4294967295", "  0   0 4294967295  ", ""])
def test_root_dockers_identity_map_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first: str
) -> None:
    """The step's root is the machine's own where every user ID maps to
    itself, and exec does not start; nor where it cannot read the map."""
    uid_map = tmp_path / "uid_map"
    uid_map.write_text(f"{first}\n" if first else "")
    monkeypatch.setattr(primitive, "UID_MAP", uid_map)
    with pytest.raises(primitive.PrimitiveError, match="machine's own root"):
        primitive.check_shape()


def test_a_rootless_map_passes_as_root_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid_map = tmp_path / "uid_map"
    uid_map.write_text(
        "         0       1000          1\n         1     100000      65536\n"
    )
    monkeypatch.setattr(primitive, "UID_MAP", uid_map)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    primitive.check_shape()
    monkeypatch.setattr(os, "geteuid", lambda: 65532)
    with pytest.raises(primitive.PrimitiveError, match="runs as root"):
        primitive.check_shape()


def test_on_root_docker_nothing_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole batch is an error, before any of the step is touched."""
    uid_map = tmp_path / "uid_map"
    uid_map.write_text("0 0 4294967295\n")
    monkeypatch.setattr(primitive, "UID_MAP", uid_map)
    monkeypatch.setenv(primitive.BOX_USERS_VARIABLE, BOX_USERS)

    def touched() -> None:
        raise AssertionError("the cgroup was set up")

    monkeypatch.setattr(primitive, "set_up_cgroup", touched)
    work = tmp_path / "work"
    binary = python_binary(tmp_path / "p", "print(1)\n").read_bytes()
    batch_inputs(work, binary, [("main-1", "")])
    primitive.main(["exec", str(work)])
    result = json.loads((work / "outputs.json").read_text())
    assert set(result) == {"schema_version", "error"}
    assert "machine's own root" in result["error"]
    assert not (work / "out").exists()


def box_user_processes() -> list[int]:
    """The processes of the tests' box users that are left, from /proc."""
    first, last = (int(user) for user in BOX_USERS.split("-"))
    found = []
    for entry in Path("/proc").iterdir():
        try:
            status = (entry / "status").read_text()
        except OSError:
            continue
        uids = next(line for line in status.splitlines() if line.startswith("Uid:"))
        if first <= int(uids.split()[1]) <= last:
            found.append(int(entry.name))
    return found


def test_a_run_without_stdin_reads_nothing(boxes: None, tmp_path: Path) -> None:
    """With no `stdin`, standard input is empty, as `/dev/null` is."""
    program = python_binary(tmp_path / "build" / "program", PROGRAM).read_bytes()
    work = tmp_path / "work"
    batch_inputs(work, program, [("main-1", "")])
    document = json.loads((work / "inputs.json").read_text())
    del document["batch"][0]["inputs"]["stdin"]
    (work / "inputs.json").write_text(json.dumps(document))
    primitive.main(["exec", str(work)])
    outputs = by_run(json.loads((work / "outputs.json").read_text()))["main-1"]
    assert outputs["outcome"] == "accepted"
    assert printed(work, outputs) == ""


def test_named_files_are_in_the_working_directory_and_cannot_be_written(
    run: Any, tmp_path: Path
) -> None:
    """The program sees exactly its named files, by their names, readable and
    not writable, and each run only its own."""
    files = {"main-1": {"answer": b"42\n", "grid.txt": b"..#\n"}, "main-2": {}}
    loads = {"main-1": {"solution.py": b"def answer(): return 7\n"}}
    items = [("main-1", "named\nanswer\n"), ("main-2", "named\n")]
    outputs = by_run(run(items, files=files, loads=loads))
    assert printed(tmp_path / "work", outputs["main-1"]).splitlines() == [
        "answer 0o444 3 42",
        "grid.txt 0o444 4 ..#",
        "solution.py 0o444 23 def answer(): re",
        f"refused answer {errno.EACCES}",
    ]
    assert printed(tmp_path / "work", outputs["main-2"]) == ""


def test_a_program_loads_code_from_its_working_directory(
    run: Any, tmp_path: Path
) -> None:
    loads = {"main-1": {"solution.py": b"def answer():\n    return 7\n"}}
    outputs = by_run(run([("main-1", "load\n")], loads=loads))
    assert outputs["main-1"]["outcome"] == "accepted"
    assert printed(tmp_path / "work", outputs["main-1"]) == "7\n"


def test_a_run_that_removes_or_changes_its_named_files_changes_no_later_run(
    boxes: None, tmp_path: Path
) -> None:
    """A run owns its copies, so it may make one writable or remove it; the
    next run that names the same file under `in/` still gets it whole."""
    work = tmp_path / "work"
    items = [("main-1", "spoil\n"), ("main-2", "named\n")]
    batch_inputs(work, python_binary(tmp_path / "b" / "p", PROGRAM).read_bytes(), items)
    (work / "in" / "shared").mkdir()
    (work / "in" / "shared" / "answer").write_text("42\n")
    document = json.loads((work / "inputs.json").read_text())
    for entry in document["batch"]:
        entry["inputs"]["files"] = {"files": {"answer": {"file": "in/shared/answer"}}}
    (work / "inputs.json").write_text(json.dumps(document))
    primitive.main(["exec", str(work)])
    outputs = by_run(json.loads((work / "outputs.json").read_text()))
    assert printed(work, outputs["main-1"]) == "spoiled\n"
    assert printed(work, outputs["main-2"]) == "answer 0o444 3 42\n"


def test_a_binary_in_no_known_format_is_an_error(boxes: None, tmp_path: Path) -> None:
    """A program exec cannot run is the step failing, not the contestant."""
    batch_inputs(tmp_path, b"#!/bin/sh\n", [("main-1", "")])
    primitive.main(["exec", str(tmp_path)])
    result = json.loads((tmp_path / "outputs.json").read_text())
    assert "not a native executable" in result["error"]


def test_every_outcome(run: Any, tmp_path: Path) -> None:
    """One batch reaches each outcome, keeps each output and measures each run,
    answering in the batch's order under each item's run, with each output
    under the item's place in the batch, and leaves no box behind.
    """
    runs = ["samples-ok", "main-spin", "main-sleep", "main-grow", "main-fail"]
    cases = ["double\n21\n", "spin\n", "sleep\n", "grow\n", "fail\n"]
    items = list(zip(runs, cases, strict=True))
    result = run(items, time_limit=0.5, memory_limit=64)
    outputs = by_run(result)
    assert [entry["run"] for entry in result["batch"]] == runs
    assert {run: entry["outcome"] for run, entry in outputs.items()} == {
        "samples-ok": "accepted",
        "main-spin": "time_limit",
        "main-sleep": "time_limit",
        "main-grow": "memory_limit",
        "main-fail": "runtime_error",
    }
    work = tmp_path / "work"
    assert [entry["output"]["file"] for entry in outputs.values()] == [
        f"out/{number}/output" for number in range(1, 6)
    ]
    assert printed(work, outputs["samples-ok"]) == "42\n"
    assert printed(work, outputs["main-fail"]) == "partial\n"
    for entry in outputs.values():
        assert set(entry) == {"output", "time_ms", "memory_kb", "outcome"}
        assert isinstance(entry["time_ms"], int)
        assert isinstance(entry["memory_kb"], int)
        assert entry["memory_kb"] > 0
    assert outputs["main-grow"]["memory_kb"] >= 64 * 1024
    assert outputs["samples-ok"]["memory_kb"] < 64 * 1024
    assert not os.path.ismount(primitive.BOX_ROOT / str(primitive.BOX) / "box")
    assert not (primitive.BOX_ROOT / str(primitive.BOX)).exists()


def test_a_run_stopped_at_its_time_limit_reports_at_least_the_limit(
    run: Any,
) -> None:
    """Stopped by the CPU limit or by the wall clock, a run reports at least
    the time limit, though one that slept used next to no CPU.
    """
    items = [("main-spin", "spin\n"), ("main-sleep", "sleep\n")]
    outputs = by_run(run(items, time_limit=0.3))
    assert outputs["main-spin"]["outcome"] == "time_limit"
    assert outputs["main-sleep"]["outcome"] == "time_limit"
    assert 300 <= outputs["main-spin"]["time_ms"] < 1300
    assert outputs["main-sleep"]["time_ms"] == 300


@pytest.mark.parametrize(
    ("args", "argv"),
    [
        ("--seed 7", ["--seed", "7"]),
        (
            "  --threshold   0.5 --heuristic true ",
            ["--threshold", "0.5", "--heuristic", "true"],
        ),
        ("", []),
        (None, []),
    ],
)
def test_args_reach_the_command_line_split_on_spaces(
    run: Any, tmp_path: Path, args: str | None, argv: list[str]
) -> None:
    """`args` is appended to the binary's command line, split on spaces, with
    no empty arguments; absent, the binary gets none.
    """
    given = {"main-1": args} if args is not None else None
    outputs = by_run(run([("main-1", "argv\n")], args=given))
    assert outputs["main-1"]["outcome"] == "accepted"
    assert printed(tmp_path / "work", outputs["main-1"]) == f"{argv!r}\n"


def test_args_are_each_items_own(run: Any, tmp_path: Path) -> None:
    items = [("main-1", "argv\n"), ("main-2", "argv\n"), ("main-3", "argv\n")]
    outputs = by_run(run(items, args={"main-1": "a b", "main-3": "c"}))
    work = tmp_path / "work"
    assert [printed(work, outputs[test]) for test, _ in items] == [
        "['a', 'b']\n",
        "[]\n",
        "['c']\n",
    ]


def test_args_no_command_line_can_carry_are_a_runtime_error(
    run: Any, tmp_path: Path
) -> None:
    """An argument holding a NUL character, or longer than the kernel takes,
    is the contestant's input failing, never an error of the step.
    """
    items = [("main-nul", "argv\n"), ("main-long", "argv\n"), ("main-ok", "argv\n")]
    args = {"main-nul": "a \0 b", "main-long": "x" * (256 * 1024)}
    outputs = by_run(run(items, args=args))
    assert {test: entry["outcome"] for test, entry in outputs.items()} == {
        "main-nul": "runtime_error",
        "main-long": "runtime_error",
        "main-ok": "accepted",
    }
    for entry in outputs.values():
        assert set(entry) == {"output", "time_ms", "memory_kb", "outcome"}
    assert printed(tmp_path / "work", outputs["main-nul"]) == ""


def test_output_over_the_limit(run: Any, tmp_path: Path) -> None:
    """Printing too much is an output limit, and the output is cut at the
    limit. The output goes to exec through a pipe, so 32 MB of it costs a
    64 MB box nothing of its memory."""
    result = run([("main-flood", "flood\n")], time_limit=10, memory_limit=64)
    outputs = by_run(result)["main-flood"]
    assert outputs["outcome"] == "output_limit"
    assert outputs["memory_kb"] < 32 * 1024
    output = tmp_path / "work" / "out" / "1" / "output"
    assert output.stat().st_size == primitive.OUTPUT_LIMIT


def test_processes_left_behind_are_killed(run: Any) -> None:
    """A process a run leaves behind, even in its own session, goes with the
    box's PID namespace when the program ends."""
    started = time.monotonic()
    result = run([("main-linger", "linger\n")])
    assert by_run(result)["main-linger"]["outcome"] == "accepted"
    assert time.monotonic() - started < 10
    assert box_user_processes() == []


def test_runs_write_only_their_own_box(run: Any, tmp_path: Path) -> None:
    """A run cannot name the working directory; its `/tmp` is its box's own,
    not the container's."""
    work = tmp_path / "work"
    outputs = by_run(run([("main-escape", f"escape\n{work}\n")]))
    assert printed(work, outputs["main-escape"]).splitlines() == [
        f"refused {work}/escaped 2",
        f"refused {work}/inputs.json 2",
        "wrote /tmp/escaped",
        "cwd ['here']",
    ]
    assert not Path("/tmp/escaped").exists()


def test_a_run_that_signals_the_box_keeper_is_refused(run: Any, tmp_path: Path) -> None:
    """The program's parent is isolate's, outside its user, so killing it is
    refused, and the run is its own runtime error, never an error of the step.
    """
    result = run([("main-parricide", "parricide\n"), ("main-ok", "double\n21\n")])
    outputs = by_run(result)
    assert outputs["main-parricide"]["outcome"] == "runtime_error"
    assert outputs["main-ok"]["outcome"] == "accepted"
    said = printed(tmp_path / "work", outputs["main-parricide"]).splitlines()
    assert said == [f"refused {errno.EPERM}"]


def test_a_run_that_tries_to_stop_its_parent_is_still_ended_on_time(run: Any) -> None:
    started = time.monotonic()
    result = run([("main-freeze", "freeze\n")], time_limit=0.5)
    elapsed = time.monotonic() - started
    assert by_run(result)["main-freeze"]["outcome"] == "time_limit"
    assert elapsed < 4


def test_work_in_a_child_counts_against_the_time_limit(run: Any) -> None:
    """A child the binary never waits for still spends the run's time."""
    result = run([("main-hidden-work", "hidden-work\n")], time_limit=0.5)
    outputs = by_run(result)
    assert outputs["main-hidden-work"]["outcome"] == "time_limit"
    assert outputs["main-hidden-work"]["time_ms"] >= 500


def test_memory_in_a_child_counts_against_the_memory_limit(run: Any) -> None:
    result = run([("main-hidden-memory", "hidden-memory\n")], memory_limit=64)
    outputs = by_run(result)
    assert outputs["main-hidden-memory"]["outcome"] == "memory_limit"
    assert outputs["main-hidden-memory"]["memory_kb"] >= 64 * 1024


def test_memory_spread_over_processes_is_added_up(run: Any) -> None:
    """Two processes that each hold 40 MB of their own at the same moment are
    over a 64 MB limit together, though each alone is under it.
    """
    result = run([("main-split", "split-memory\n")], time_limit=3, memory_limit=64)
    outputs = by_run(result)
    assert outputs["main-split"]["outcome"] == "memory_limit"
    assert outputs["main-split"]["memory_kb"] >= 64 * 1024


def test_a_forked_child_is_not_charged_again_for_what_it_shares(run: Any) -> None:
    """A child forked from a binary holding 40 MB, which touches nothing of its
    own, costs the run almost nothing: the pages it shares with its parent
    count once, not once in each process.
    """
    items = [("main-alone", "hold-memory\n"), ("main-shared", "shared-memory\n")]
    outputs = by_run(run(items, time_limit=3, memory_limit=64))
    alone, shared = outputs["main-alone"], outputs["main-shared"]
    assert alone["outcome"] == "accepted"
    assert shared["outcome"] == "accepted"
    assert alone["memory_kb"] > 40 * 1024
    assert shared["memory_kb"] < alone["memory_kb"] + 4 * 1024


def test_memory_held_and_memory_in_a_child_read_as_held(run: Any) -> None:
    """A program holding 40 MB reads within 10% of 40 MB over one holding
    nothing, its interpreter's own; a child holding 40 MB counts the same.
    """
    items = [
        ("main-base", "double\n21\n"),
        ("main-alone", "hold-memory\n"),
        ("main-child", "child-memory\n"),
    ]
    outputs = by_run(run(items, time_limit=3, memory_limit=128))
    base = outputs["main-base"]["memory_kb"]
    for test in ("main-alone", "main-child"):
        assert outputs[test]["outcome"] == "accepted"
        held = outputs[test]["memory_kb"] - base
        assert abs(held - 40 * 1024) <= 4 * 1024, (test, held)


@pytest.mark.parametrize("cached", [False, True], ids=["cold", "cached"])
def test_reading_a_large_input_counts_what_the_box_reads_in(
    run: Any, tmp_path: Path, cached: bool
) -> None:
    """A program that reads a 200 MB input and holds little: the box's cgroup
    is charged the page cache its reads bring in, up to its limit, which it
    takes back from the file's pages and does not kill for. So `memory_kb`
    for such a run is near the limit when the file was not in memory yet,
    and the interpreter's own when it was, read in by another process. The
    run is `accepted` either way."""
    work = tmp_path / "work"
    binary = python_binary(tmp_path / "b" / "p", PROGRAM).read_bytes()
    items = [("main-base", "double\n21\n"), ("main-read", "")]
    batch_inputs(work, binary, items, time_limit=5, memory_limit=64)
    with (work / "in" / "3" / "input").open("wb") as stream:
        stream.write(b"read-input\n")
        for _ in range(200):
            stream.write(os.urandom(2**20))
        stream.flush()
        os.fsync(stream.fileno())
        if cached:
            with (work / "in" / "3" / "input").open("rb") as again:
                while again.read(2**20):
                    pass
        else:
            os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
    primitive.main(["exec", str(work)])
    outputs = by_run(json.loads((work / "outputs.json").read_text()))
    read, base = outputs["main-read"], outputs["main-base"]
    assert read["outcome"] == "accepted"
    assert printed(work, read) == "read 200\n"
    if cached:
        assert read["memory_kb"] < base["memory_kb"] + 8 * 1024
    else:
        assert read["memory_kb"] > 56 * 1024


def test_a_native_binary_the_kernel_will_not_run_is_a_runtime_error(
    boxes: None, tmp_path: Path
) -> None:
    batch_inputs(tmp_path, b"\x7fELF" + bytes(60), [("main-1", "")])
    primitive.main(["exec", str(tmp_path)])
    result = json.loads((tmp_path / "outputs.json").read_text())
    outputs = by_run(result)["main-1"]
    assert outputs["outcome"] == "runtime_error"
    assert outputs["time_ms"] < 100


def test_a_json_file_is_written_whole_past_a_planted_link(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("untouched")
    (tmp_path / "outputs.json.partial").symlink_to(target)
    primitive.write_json(tmp_path / "outputs.json", {"schema_version": 5})
    assert target.read_text() == "untouched"
    assert not (tmp_path / "outputs.json").is_symlink()
    assert json.loads((tmp_path / "outputs.json").read_text()) == {"schema_version": 5}


def test_a_run_reads_neither_another_runs_files_nor_the_steps(
    run: Any, tmp_path: Path
) -> None:
    """A later run cannot name an earlier run's output or input, the step's
    own files, or the working directory, so nothing one test leaves reaches
    the next; the system's files it can read.
    """
    work = str(tmp_path / "work")
    result = run([("main-first", "double\n21\n"), ("main-peek", peek_input(work))])
    outputs = by_run(result)
    assert outputs["main-peek"]["outcome"] == "accepted"
    assert printed(tmp_path / "work", outputs["main-first"]) == "42\n"
    lines = printed(tmp_path / "work", outputs["main-peek"]).splitlines()
    assert lines == peeked(work)


def test_a_connection_out_fails(run: Any, tmp_path: Path) -> None:
    """A box has a network namespace of its own with no way out, so a
    connection out fails though the container has a network;
    a Unix socket, which Java and Python use, works. io_uring and the
    kernel's keyrings are refused, the keyrings by isolate's filter and our
    patch to it."""
    outputs = by_run(run([("main-1", "sockets\n")]))
    assert outputs["main-1"]["outcome"] == "accepted"
    assert printed(tmp_path / "work", outputs["main-1"]).splitlines() == [
        f"connect AF_INET {errno.ENETUNREACH}",
        f"connect AF_INET6 {errno.ENETUNREACH}",
        "socketpair AF_UNIX True",
        f"io_uring_setup {errno.ENOSYS}",
        f"add_key {errno.ENOSYS}",
        f"request_key {errno.ENOSYS}",
        f"keyctl {errno.ENOSYS}",
    ]


def test_a_key_one_run_adds_is_not_there_for_the_next(run: Any, tmp_path: Path) -> None:
    """The keyrings are not namespaced: a key added to the box user's own
    keyring would outlive the box. A run can add none, and the next finds
    none, nor lists one in /proc/keys."""
    outputs = by_run(run([("main-add", "sockets\n"), ("main-find", "find-key\n")]))
    assert printed(tmp_path / "work", outputs["main-find"]).splitlines() == [
        f"request_key {errno.ENOSYS}",
        "listed False",
    ]


def test_nothing_one_run_leaves_is_there_for_the_next(run: Any, tmp_path: Path) -> None:
    """Files a run leaves in its working directory, its `/tmp` and
    `/dev/shm`, and a mark or a time it sets on its copy of the program, are
    gone for the next run, which finds a box of only its own program and
    working directory."""
    items = [
        ("main-leave", "leave-files\n"),
        ("main-mark", "mark\n"),
        ("main-find", "find-files\n"),
        ("main-read", "read-mark\n"),
    ]
    outputs = by_run(run(items))
    work = tmp_path / "work"
    assert printed(work, outputs["main-leave"]) == "left\n"
    assert printed(work, outputs["main-mark"]) == "marked\n"
    assert printed(work, outputs["main-find"]).splitlines() == [
        "left False",
        "/tmp/left False",
        "/dev/shm/left False",
        "box ['program', 'run', 'shm', 'tmp']",
    ]
    assert printed(work, outputs["main-read"]) == "no attribute\ntime fresh\n"


def test_a_fork_bomb_stops_at_the_process_limit(run: Any, tmp_path: Path) -> None:
    """A run stops at 128 processes of its box user, and the next run starts
    with none of them left."""
    items = [("main-bomb", "fork-bomb\n"), ("main-next", "double\n21\n")]
    outputs = by_run(run(items))
    assert printed(tmp_path / "work", outputs["main-bomb"]) == "stopped 11 True\n"
    assert printed(tmp_path / "work", outputs["main-next"]) == "42\n"
    assert box_user_processes() == []


@pytest.mark.parametrize("kind", ["shm", "msg", "sem"])
def test_a_run_finds_no_system_v_object_an_earlier_run_left(
    run: Any, tmp_path: Path, kind: str
) -> None:
    """Each box has an IPC namespace of its own, gone with it."""
    items = [
        ("main-leave", f"leave-sysv\n{kind} 3\n"),
        ("main-find", f"find-sysv\n{kind}\n"),
    ]
    outputs = by_run(run(items))
    assert printed(tmp_path / "work", outputs["main-leave"]) == f"made {kind} 3\n"
    assert printed(tmp_path / "work", outputs["main-find"]) == f"found {kind} 0\n"


def test_a_run_finds_no_posix_message_queue_an_earlier_run_left(
    run: Any, tmp_path: Path
) -> None:
    """A POSIX queue belongs to the IPC namespace too: a run may make one,
    and the next finds none."""
    outputs = by_run(run([("main-leave", "leave-mq\n"), ("main-find", "find-mq\n")]))
    assert printed(tmp_path / "work", outputs["main-leave"]) == "made 0\n"
    assert printed(tmp_path / "work", outputs["main-find"]) == f"none {errno.ENOENT}\n"


def test_a_box_cannot_open_a_block_device(run: Any, tmp_path: Path) -> None:
    """A privileged container's /dev holds the machine's devices; a box's
    /dev holds four devices and the standard links, so the machine's disks
    cannot even be named."""
    devices = sorted(
        str(path)
        for path in Path("/dev").iterdir()
        if stat.S_ISBLK(path.stat().st_mode)
    )
    assert devices, "the test container shows no block device to try"
    paths = " ".join([*devices[:4], "/dev/mem", "/dev/kmsg"])
    outputs = by_run(run([("main-1", f"devices\n{paths}\n")]))
    lines = printed(tmp_path / "work", outputs["main-1"]).splitlines()
    assert lines[0] == (
        "dev ['fd', 'null', 'random', 'shm', 'stderr', 'stdin', 'stdout', "
        "'urandom', 'zero']"
    )
    assert lines[1:] == [f"refused {path} {errno.ENOENT}" for path in paths.split()]


@pytest.mark.parametrize("folder", [".", "/tmp", "/dev/shm"])
def test_a_run_writing_past_its_memory_is_over_the_memory_limit(
    run: Any, folder: str
) -> None:
    """Every page a run writes to its box's tmpfs, its `/tmp` or `/dev/shm`
    is charged to its memory, so filling one ends at the memory limit, never
    the container's disk or memory."""
    outputs = by_run(run([("main-fill", f"fill\n{folder}\n")], memory_limit=64))
    assert outputs["main-fill"]["outcome"] == "memory_limit"


@pytest.mark.parametrize("folder", [".", "/tmp", "/dev/shm"])
def test_a_run_making_too_many_files_is_over_the_memory_limit(
    run: Any, tmp_path: Path, folder: str
) -> None:
    """The box's tmpfs holds 100,000 files and folders, its `/tmp` and
    `/dev/shm` among them: a run making more is told `ENOSPC`, and exec,
    which finds none free after it, gives `memory_limit`; one that makes
    fewer is judged as ever."""
    items = [("main-many", f"files\n{folder}\n"), ("main-few", "folders\n1000\n")]
    outputs = by_run(run(items, time_limit=4, memory_limit=512))
    assert printed(tmp_path / "work", outputs["main-many"]) == "stopped 28 True\n"
    assert outputs["main-many"]["outcome"] == "memory_limit"
    assert outputs["main-few"]["outcome"] == "accepted"


def test_a_tree_as_deep_as_the_box_holds_is_given_back(
    run: Any, tmp_path: Path
) -> None:
    """isolate gives the box's tree back to root after a run with a folder
    open at each level, so exec raises the stack and open-files limits it
    inherits: a run nesting folders until the box has no inode left ends
    `memory_limit`, and the batch goes on. The image test runs it under a
    low soft limit on open files."""
    items = [("main-deep", "nest\n"), ("main-next", "folders\n10\n")]
    outputs = by_run(run(items, time_limit=20, memory_limit=512))
    assert printed(tmp_path / "work", outputs["main-deep"]) == "nested 28 True\n"
    assert outputs["main-deep"]["outcome"] == "memory_limit"
    assert outputs["main-next"]["outcome"] == "accepted"


@pytest.mark.parametrize(
    ("kind", "name"),
    [
        (resource.RLIMIT_STACK, "stack"),
        (resource.RLIMIT_NOFILE, "open files"),
    ],
)
def test_a_hard_limit_below_what_isolate_needs_stops_the_step(
    monkeypatch: pytest.MonkeyPatch, kind: int, name: str
) -> None:
    real = resource.getrlimit

    def low(asked: int) -> tuple[int, int]:
        return (1024, 1024) if asked == kind else real(asked)

    monkeypatch.setattr(resource, "getrlimit", low)
    monkeypatch.setattr(resource, "setrlimit", lambda *_: None)
    with pytest.raises(primitive.PrimitiveError, match=f"hard limit on {name}, 1024"):
        primitive.raise_limits()


def test_the_soft_limits_isolate_inherits_are_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits = {
        resource.RLIMIT_STACK: (8 << 20, resource.RLIM_INFINITY),
        resource.RLIMIT_NOFILE: (1024, 524288),
    }
    made: dict[int, tuple[int, int]] = {}
    monkeypatch.setattr(resource, "getrlimit", limits.__getitem__)
    monkeypatch.setattr(resource, "setrlimit", made.__setitem__)
    primitive.raise_limits()
    assert made == {
        resource.RLIMIT_STACK: (
            primitive.ISOLATE_STACK,
            resource.RLIM_INFINITY,
        ),
        resource.RLIMIT_NOFILE: (primitive.ISOLATE_OPEN_FILES, 524288),
    }


def test_a_stdin_file_others_can_write_is_not_changed_by_a_run(
    boxes: None, tmp_path: Path
) -> None:
    """The box can open its standard input again through `/proc/self/fd/0`
    by the file's own mode, so exec takes write away from group and others
    first: a run given a 0666 input cannot append to it for a later run, or
    a later step, to read."""
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    batch_inputs(
        work, binary, [("main-1", "append-stdin\n"), ("main-2", "folders\n10\n")]
    )
    for path in work.rglob("*"):
        path.chmod(0o777 if path.is_dir() else 0o666)
    assert primitive.main(["exec", str(work)]) == 0
    outputs = by_run(json.loads((work / "outputs.json").read_text()))
    assert printed(work, outputs["main-1"]) == f"refused {errno.EACCES}\n"
    stdin = work / "in" / "2" / "input"
    assert stdin.read_text() == "append-stdin\n"
    assert stat.S_IMODE(stdin.stat().st_mode) == 0o644


def test_a_run_reads_its_stdin_file_but_not_its_name(
    boxes: None, tmp_path: Path
) -> None:
    """The path a box reads for its standard input is `/`, not the file's
    under `/work/in`, which may say which test it is."""
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    batch_inputs(work, binary, [("main-1", "stdin-path\nrest\n")])
    assert primitive.main(["exec", str(work)]) == 0
    outputs = by_run(json.loads((work / "outputs.json").read_text()))
    assert printed(work, outputs["main-1"]) == "/ rest\n"
    assert not primitive.STDIN_MOUNT.is_mount()


def test_a_stdin_file_a_box_user_owns_is_an_error(boxes: None, tmp_path: Path) -> None:
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    batch_inputs(work, binary, [("main-1", "folders\n10\n")])
    os.chown(work / "in" / "2" / "input", 60000, 60000)
    assert primitive.main(["exec", str(work)]) == 0
    result = json.loads((work / "outputs.json").read_text())
    assert set(result) == {"schema_version", "error"}
    assert (
        "stdin file of run main-1 is owned by user 60000, a box user" in result["error"]
    )


@pytest.mark.parametrize(
    ("code", "said", "told"),
    [
        (2, b"Cannot open directory d: Too many open files\n", "Cannot open directory"),
        (-11, b"", "killed by signal 11"),
        (2, b"  \n", "exited with code 2"),
    ],
)
def test_what_isolate_said_when_it_failed(code: int, said: bytes, told: str) -> None:
    assert told in primitive.isolate_said(code, said)


def exec_apart(work: Path, users: str, group: str) -> int:
    """Start exec over `work` in a child that is a step container of its own
    as far as exec can tell, in the tests' user namespace, as every step of
    a rootless Docker is: a cgroup namespace rooted at `group`, and its own
    mounts for the cgroup, `/run`, the box root and isolate's
    configuration. Return the child's process ID."""
    cgroup = primitive.CGROUP / group
    cgroup.mkdir(exist_ok=True)
    child = os.fork()
    if child:
        return child
    try:
        (cgroup / "cgroup.procs").write_text(str(os.getpid()))
        os.unshare(os.CLONE_NEWNS | os.CLONE_NEWCGROUP)
        primitive.mount("none", Path("/"), None, (1 << 14) | (1 << 18), None)
        # The container's cgroup mount came from a more privileged mount
        # namespace and cannot be unmounted here, so a tmpfs goes over it
        # first, and the namespace's own cgroup over that.
        primitive.mount("tmpfs", primitive.CGROUP, "tmpfs", 0, None)
        primitive.mount("cgroup2", primitive.CGROUP, "cgroup2", 0, None)
        for folder in ("/run", "/var/local/lib", "/usr/local/etc"):
            primitive.mount("tmpfs", Path(folder), "tmpfs", 0, "mode=0755")
        os.environ[primitive.BOX_USERS_VARIABLE] = users
        primitive.main(["exec", str(work)])
    except BaseException:
        traceback.print_exc()
        os._exit(1)
    os._exit(0)


@pytest.mark.parametrize(
    ("users", "apart"),
    [(("60000-60000", "60100-60100"), True), (("60000-60000", "60000-60000"), False)],
    ids=["distinct", "shared"],
)
def test_two_steps_at_once_share_a_process_count_only_on_the_same_users(
    boxes: None, tmp_path: Path, users: tuple[str, str], apart: bool
) -> None:
    """`RLIMIT_NPROC` counts each user's processes across the user
    namespace, every step of one rootless Docker, so two steps given
    distinct box users each fork up to their limit at once, and two given
    the same one share it."""
    primitive.set_up_step(primitive.box_users())
    binary = python_binary(tmp_path / "b" / "p", PROGRAM).read_bytes()
    works = []
    for number in range(2):
        work = tmp_path / f"work-{number}"
        batch_inputs(work, binary, [("main-1", "fork-hold\n")], time_limit=2)
        works.append(work)
    children = [
        exec_apart(work, given, f"apart-{number}")
        for number, (work, given) in enumerate(zip(works, users, strict=True))
    ]
    for child in children:
        assert os.waitstatus_to_exitcode(os.waitpid(child, 0)[1]) == 0
    forked = []
    for work in works:
        outputs = by_run(json.loads((work / "outputs.json").read_text()))["main-1"]
        forked.append(int(printed(work, outputs).split()[1]))
    for number in range(2):
        cgroup = primitive.CGROUP / f"apart-{number}"
        for inner in ("isolate", "step"):
            (cgroup / inner).rmdir()
        cgroup.rmdir()
    if apart:
        assert min(forked) >= primitive.PROCESS_LIMIT - 8, forked
    else:
        assert sum(forked) <= primitive.PROCESS_LIMIT + 8, forked


def test_a_box_a_dead_step_left_mounted_does_not_stop_the_next(
    run: Any, tmp_path: Path
) -> None:
    """A step killed in the middle of an item leaves its box initialised and
    its tmpfs mounted; the next item unmounts it and starts afresh."""
    primitive.set_up_step(primitive.box_users())
    primitive.isolate_box(primitive.BOX, "--init")
    folder = primitive.BOX_ROOT / str(primitive.BOX) / "box"
    primitive.mount("box-0", folder, "tmpfs", 0, "size=1m")
    (folder / "left").write_text("hidden answer")
    outputs = by_run(run([("main-find", "find-files\n")]))
    assert outputs["main-find"]["outcome"] == "accepted"
    assert "box ['program', 'run', 'shm', 'tmp']" in printed(
        tmp_path / "work", outputs["main-find"]
    )


def test_a_step_that_cannot_be_set_up_runs_nothing(
    run: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(primitive, "STEP_CGROUP", Path("/proc/no-such/step"))
    result = run([("main-ok", "double\n21\n")])
    assert set(result) == {"schema_version", "error"}
    assert "could not be set up for its boxes" in result["error"]
    assert not (tmp_path / "work" / "out").exists()


def test_cleanup_that_fails_after_an_error_does_not_hide_it(
    boxes: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(*_: object) -> None:
        raise primitive.PrimitiveError("the first error")

    def fail_cleanup(*_: object) -> None:
        raise primitive.PrimitiveError("the cleanup error")

    batch_inputs(
        tmp_path,
        python_binary(tmp_path / "p", "print(1)\n").read_bytes(),
        [("main-1", "")],
    )
    monkeypatch.setattr(primitive, "execute", fail)
    monkeypatch.setattr(primitive, "close_box", fail_cleanup)
    assert primitive.main(["exec", str(tmp_path)]) == 0
    result = json.loads((tmp_path / "outputs.json").read_text())
    assert result["error"] == "the first error"
    assert "the cleanup error" in capsys.readouterr().err
    monkeypatch.undo()
    primitive.close_box(primitive.BOX, primitive.BOX_ROOT / "0" / "box")


def write_manifest(tmp_path: Path, document: object) -> Path:
    """A manifest file holding `document` as JSON, or as written when text."""
    path = tmp_path / "manifest.json"
    text = document if isinstance(document, str) else json.dumps(document)
    path.write_text(text)
    return path


def test_a_manifest_reads_as_its_reports(tmp_path: Path) -> None:
    """A number is the default, with its bounds as decimals; a text has
    none; either may be optional."""
    path = write_manifest(
        tmp_path,
        {
            "reports": {
                "score": {"better": "higher", "at_least": 0, "at_most": 1},
                "error": {"type": "number", "at_least": 0.5},
                "feedback": {"type": "text", "optional": True},
            }
        },
    )
    manifest = primitive.read_manifest(path, "the manifest")
    assert manifest == {
        "score": primitive.Declared("number", False, Decimal(0), Decimal(1)),
        "error": primitive.Declared("number", False, Decimal("0.5"), None),
        "feedback": primitive.Declared("text", True),
    }


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("reports: {}", "is not valid JSON"),
        ('{"reports": {"score": {"at_least": NaN}}}', "is not valid JSON"),
        ('{"reports": {"score": {"at_most": Infinity}}}', "is not valid JSON"),
        ([], "holding reports alone"),
        ({"reports": {}, "title": "x"}, "holding reports alone"),
        ({"reports": []}, "reports that are not an object"),
        ({"reports": {"Score": {}}}, "declares 'Score', not a report name"),
        ({"reports": {"a" * 65: {}}}, "not a report name"),
        ({"reports": {"verdict": {}}}, "declares verdict, a name exec gives"),
        ({"reports": {"time_ms": {}}}, "declares time_ms, a name exec gives"),
        ({"reports": {"message": {"type": "text"}}}, "declares message, a name"),
        ({"reports": {"score": "number"}}, "with keys it does not know"),
        ({"reports": {"score": {"unit": "%"}}}, "with keys it does not know"),
        ({"reports": {"score": {"type": "file"}}}, "neither a number nor a text"),
        ({"reports": {"score": {"optional": 1}}}, "neither a number nor a text"),
        ({"reports": {"note": {"type": "text", "at_most": 1}}}, "a direction or a"),
        ({"reports": {"score": {"better": "up"}}}, "neither higher nor lower"),
        ({"reports": {"score": {"at_least": True}}}, "by something not a number"),
        ({"reports": {"score": {"at_least": "0"}}}, "by something not a number"),
        ('{"reports": {"score": {"at_most": 1e999}}}', "by something not a number"),
        ({"reports": {"score": {"at_least": 2, "at_most": 1}}}, "at least above"),
        ({"reports": {"score": {"better": None}}}, "neither higher nor lower"),
        ({"reports": {"score": {"at_least": None}}}, "by something not a number"),
        (
            '{"reports": {"score": {"at_most": 1e99999999999999999999}}}',
            "holds a number too large to read",
        ),
    ],
)
def test_a_manifest_it_cannot_use_is_an_error(
    tmp_path: Path, document: object, message: str
) -> None:
    path = write_manifest(tmp_path, document)
    with pytest.raises(primitive.PrimitiveError, match=re.escape(message)):
        primitive.read_manifest(path, "the manifest")


def write_report(folder: Path, files: dict[str, bytes]) -> Path:
    """A report folder holding `files`, by name."""
    folder.mkdir(exist_ok=True)
    for name, content in files.items():
        (folder / name).write_bytes(content)
    return folder


DIFF = {"score": primitive.Declared("number", False, Decimal(0), Decimal(1))}


def test_a_report_gives_its_reports_and_texts_and_shows_the_rest(
    tmp_path: Path,
) -> None:
    folder = write_report(
        tmp_path / "report",
        {
            "verdict": b"0\n",
            "score": b"0.5\n",
            "label": b"partial\n",
            "message": "line 3 differs, é\n".encode(),
            "staff_message": b"compared 12 lines\n",
            "notes": b"anything\n",
        },
    )
    report = primitive.read_report(folder, DIFF)
    assert report == primitive.Report(
        verdict="0",
        outputs={
            "score": 0.5,
            "label": "partial",
            "message": "line 3 differs, é",
            "staff_message": "compared 12 lines",
        },
        shown={"notes": b"anything\n"},
    )


def test_a_wrong_verdict_shows_its_reports_unchecked(tmp_path: Path) -> None:
    """Under `1` the reports don't count, so they are shown, not outputs, and
    one that would not hold is no fault."""
    folder = write_report(
        tmp_path / "report",
        {"verdict": b"1", "score": b"oops", "message": b"wrong on line 2"},
    )
    report = primitive.read_report(folder, DIFF)
    assert report.verdict == "1"
    assert report.outputs == {"message": "wrong on line 2"}
    assert report.shown == {"score": b"oops"}


@pytest.mark.parametrize("word", primitive.VERDICT_WORDS)
def test_an_outcome_word_is_a_verdict_and_shows_its_reports_unchecked(
    tmp_path: Path, word: str
) -> None:
    """A program that judged another's run, as `unicon/interactive`'s
    verdict script does, says whose limit it was; as under `1`, the reports
    don't count."""
    folder = write_report(
        tmp_path / "report",
        {"verdict": f"{word}\n".encode(), "score": b"oops", "label": b"slow"},
    )
    report = primitive.read_report(folder, DIFF)
    assert report.verdict == word
    assert report.outputs == {"label": "slow"}
    assert report.shown == {"score": b"oops"}
    assert primitive.verdict_outcome(report.verdict) == word


def test_an_optional_report_may_be_left_out(tmp_path: Path) -> None:
    manifest = {"score": primitive.Declared("number", optional=True)}
    folder = write_report(tmp_path / "report", {"verdict": b"0"})
    assert primitive.read_report(folder, manifest).outputs == {}


def test_a_text_report_is_an_output_too(tmp_path: Path) -> None:
    manifest = {"feedback": primitive.Declared("text")}
    folder = write_report(tmp_path / "report", {"verdict": b"0", "feedback": b"ok\n\n"})
    assert primitive.read_report(folder, manifest).outputs == {"feedback": "ok"}


@pytest.mark.parametrize(
    ("written", "value"),
    [
        (b"1", 1),
        (b"0", 0),
        (b"-0", 0),
        (b" 0.25 \n", 0.25),
        (b"1e-05\n", 1e-05),
        (b"5E-1", 0.5),
        (b"1.0", 1.0),
    ],
)
def test_a_number_is_a_decimal_as_json_writes_one(
    tmp_path: Path, written: bytes, value: float
) -> None:
    folder = write_report(tmp_path / "report", {"verdict": b"0", "score": written})
    reported = primitive.read_report(folder, DIFF).outputs["score"]
    assert reported == value
    assert type(reported) is type(value)


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ({}, "has no verdict"),
        ({"verdict": b""}, "has a verdict '', not 0, 1 or an outcome word"),
        ({"verdict": b"2"}, "has a verdict '2', not 0, 1 or an outcome word"),
        ({"verdict": b"0 1"}, "has a verdict '0 1', not 0, 1 or an outcome"),
        ({"verdict": b"accepted"}, "has a verdict 'accepted', not 0, 1 or an"),
        ({"verdict": b"wrong_answer"}, "has a verdict 'wrong_answer', not 0"),
        ({"verdict": b"Time_limit"}, "has a verdict 'Time_limit', not 0"),
        ({"verdict": b"time_limit 1"}, "has a verdict 'time_limit 1', not 0"),
        ({"verdict": b"0"}, "has no score, which the manifest declares"),
        ({"verdict": b"0", "score": b"nan"}, "has score 'nan', not a decimal"),
        ({"verdict": b"0", "score": b"inf"}, "has score 'inf', not a decimal"),
        ({"verdict": b"0", "score": b"0x1"}, "has score '0x1', not a decimal"),
        ({"verdict": b"0", "score": b"01"}, "has score '01', not a decimal"),
        ({"verdict": b"0", "score": b"+1"}, "has score '+1', not a decimal"),
        ({"verdict": b"0", "score": b"0,5"}, "has score '0,5', not a decimal"),
        ({"verdict": b"0", "score": b"1 1"}, "has score '1 1', not a decimal"),
        ({"verdict": b"0", "score": b""}, "has score '', not a decimal"),
        ({"verdict": b"0", "score": b"1e999"}, "has score '1e999', not a finite"),
        ({"verdict": b"0", "score": b"-1e999"}, "'-1e999', not a finite"),
        ({"verdict": b"0", "score": b"1e99999999999999999999"}, "not a finite"),
        ({"verdict": b"0", "score": b"1e-99999999999999999999"}, "not a finite"),
        ({"verdict": b"1", os.fsdecode(b"\xff"): b""}, "holds '\ufffd', not a plain"),
        ({"verdict": b"0", "score": b"-0.1"}, "has score -0.1, below its least, 0"),
        ({"verdict": b"0", "score": b"1.5"}, "has score 1.5, above its most, 1"),
        ({"verdict": b"0", "score": b"1.0000000000000000001"}, "above its most"),
        ({"verdict": b"0", "score": b"1", "message": b"\xff"}, "message not in UTF-8"),
        ({"verdict": b"0", "score": b"1", "label": b"a\0b"}, "label holding a NUL"),
        ({"verdict": b"1", "notes": b"\xfe"}, "has notes not in UTF-8"),
        ({"verdict": b"1", "message": b"x" * (64 * 1024 + 1)}, "message, over 64 KB"),
        ({"verdict": b"1", ".hidden": b""}, "holds '.hidden', not a plain name"),
        ({"verdict": b"1", "a b": b""}, "holds 'a b', not a plain name"),
    ],
)
def test_a_report_that_does_not_hold_is_a_fault(
    tmp_path: Path, files: dict[str, bytes], message: str
) -> None:
    """Each check `SIMPLER-CORE.md` 13.1 names, and what exec relies on."""
    folder = write_report(tmp_path / "report", files)
    with pytest.raises(primitive.ReportError, match=re.escape(message)):
        primitive.read_report(folder, DIFF)


def test_a_text_of_64_kb_is_kept_whole(tmp_path: Path) -> None:
    text = b"x" * (64 * 1024)
    folder = write_report(tmp_path / "report", {"verdict": b"1", "message": text})
    assert primitive.read_report(folder, DIFF).outputs["message"] == text.decode()


def test_a_report_of_too_many_files_is_a_fault(tmp_path: Path) -> None:
    files = {f"note{n}": b"" for n in range(primitive.REPORT_FILES)}
    folder = write_report(tmp_path / "report", files | {"verdict": b"1"})
    with pytest.raises(primitive.ReportError, match="more than 64 files"):
        primitive.read_report(folder, DIFF)


@pytest.mark.parametrize("kind", ["link", "fifo", "folder"])
def test_a_report_file_that_is_not_a_plain_file_is_a_fault(
    tmp_path: Path, kind: str
) -> None:
    """A link is not followed, so a run cannot have exec read for it a file
    it may not read itself; a FIFO is not waited on."""
    secret = tmp_path / "secret"
    secret.write_text("hidden answer")
    folder = write_report(tmp_path / "report", {"verdict": b"1"})
    if kind == "link":
        (folder / "message").symlink_to(secret)
    elif kind == "fifo":
        os.mkfifo(folder / "message")
    else:
        (folder / "message").mkdir()
    with pytest.raises(primitive.ReportError, match="holds message, not a plain file"):
        primitive.read_report(folder, DIFF)


def reporting(work: Path, result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """A batch's outputs keyed by run, with what its extras hold by name."""
    outputs = by_run(result)
    for entry in outputs.values():
        if "extras" in entry:
            folder = work / entry["extras"]["folder"]
            entry["extras"] = {p.name: p.read_text() for p in folder.iterdir()}
    return outputs


def test_a_program_given_a_manifest_reports_through_its_folder(
    run: Any, tmp_path: Path
) -> None:
    """Its reports are outputs under `0`; under `1` they are shown with the
    rest."""
    result = run(
        [
            ("main-1", "report\nverdict 0\nscore 1\nmessage fine\nnotes kept"),
            ("main-2", "report\nverdict 1\nscore 0.5\nlabel close"),
        ],
        manifest=SCORE,
    )
    outputs = reporting(tmp_path / "work", result)
    assert outputs["main-1"]["outcome"] == "accepted"
    assert outputs["main-1"]["score"] == 1
    assert outputs["main-1"]["message"] == "fine"
    assert outputs["main-1"]["extras"] == {"notes": "kept\n"}
    assert outputs["main-2"]["outcome"] == "wrong_answer"
    assert "score" not in outputs["main-2"]
    assert outputs["main-2"]["label"] == "close"
    assert outputs["main-2"]["extras"] == {"score": "0.5\n"}
    assert printed(tmp_path / "work", outputs["main-2"]) == "reported\n"


def test_a_program_given_a_manifest_may_name_an_outcome_as_its_verdict(
    run: Any, tmp_path: Path
) -> None:
    """An outcome word is the run's outcome, its texts outputs and its
    reports shown."""
    result = run(
        [("main-1", "report\nverdict time_limit\nscore 1\nmessage too slow")],
        manifest=SCORE,
    )
    outputs = reporting(tmp_path / "work", result)["main-1"]
    assert outputs["outcome"] == "time_limit"
    assert outputs["message"] == "too slow"
    assert outputs["extras"] == {"score": "1\n"}


def test_each_run_reports_into_a_fresh_folder(run: Any, tmp_path: Path) -> None:
    """The folder is outside the run's working directory, in its box, and
    what one run left in its folder is not there for the next."""
    manifest = {"reports": {"score": {"optional": True}}}
    result = run(
        [("main-1", "report\nverdict 0\nnotes left"), ("main-2", "report-where\n")],
        manifest=manifest,
    )
    outputs = reporting(tmp_path / "work", result)
    assert outputs["main-1"]["extras"] == {"notes": "left\n"}
    assert printed(tmp_path / "work", outputs["main-2"]) == "report True\nempty True\n"
    assert "extras" not in outputs["main-2"]


def test_a_program_given_no_manifest_has_no_report_folder(
    run: Any, tmp_path: Path
) -> None:
    outputs = by_run(run([("main-1", "no-report\n")]))
    assert printed(tmp_path / "work", outputs["main-1"]) == "report None\n"


@pytest.mark.parametrize(
    ("stdin", "message"),
    [
        ("report\nscore 1", "the report of run main-2 has no verdict"),
        ("report\nverdict 0\nscore 2", "the report of run main-2 has score 2, above"),
        ("report-then-fail\n", "run main-2 did not judge: it ended in runtime error"),
        ("spin\n", "run main-2 did not judge: it ended in time limit"),
    ],
)
def test_a_program_that_does_not_judge_is_an_error_of_the_step(
    run: Any, stdin: str, message: str
) -> None:
    """An organiser's program that reports wrongly, crashes or breaks a limit
    has not judged, so the batch is an error, which staff see."""
    result = run(
        [("main-1", "report\nverdict 0\nscore 1"), ("main-2", stdin)],
        manifest=SCORE,
        time_limit=0.5,
    )
    assert set(result) == {"schema_version", "error"}
    assert message in result["error"]


@pytest.mark.parametrize(
    ("stdin", "outcome"),
    [
        ("report\nscore 1", "runtime_error"),
        ("report\nverdict 0\nscore 2", "runtime_error"),
        ("report-then-fail\n", "runtime_error"),
        ("spin\n", "time_limit"),
        ("report\nverdict 0\nscore 1e99999999999999999999", "runtime_error"),
        ("report-bad-name\n", "runtime_error"),
        ("report\nverdict time_limit", "runtime_error"),
        ("report\nverdict memory_limit\nscore 1", "runtime_error"),
        ("report\nverdict 0\nscore 1", "accepted"),
    ],
)
def test_a_self_reported_run_that_does_not_judge_is_the_entrants(
    run: Any, stdin: str, outcome: str
) -> None:
    """Where the program runs the entrant's code, what it did is the run's
    own outcome, never a fault (`SIMPLER-CORE.md` 7.3), with no report, and
    an outcome word it writes is a `runtime_error`, so the entrant's code
    never chooses its own."""
    result = run(
        [("main-1", stdin)], manifest=SCORE, self_reported=True, time_limit=0.5
    )
    outputs = by_run(result)["main-1"]
    assert outputs["outcome"] == outcome
    assert ("score" in outputs) == (outcome == "accepted")


def test_a_link_in_the_report_folder_reads_nothing_for_the_run(
    run: Any, tmp_path: Path
) -> None:
    """A link to the next run's input, a path the box cannot name, is gone
    from the folder, as isolate removes every link a box leaves, so the
    report misses what the link stood for, and exec reads no input for it."""
    work = tmp_path / "work"
    result = run(
        [("main-1", f"report-link\n{work}/in/3/input\n"), ("main-2", "hidden answer")],
        manifest=SCORE,
    )
    assert "the report of run main-1 has no score" in result["error"]
    assert "hidden" not in json.dumps(result)


def test_a_report_folder_swapped_for_a_link_reads_nothing_for_the_run(
    run: Any, tmp_path: Path
) -> None:
    """The box's user owns `/box`, so a run can put a link where its report
    folder was, to a folder of root's the box cannot name; exec opens the
    folder without following it, so the report does not hold and nothing
    of root's is read."""
    hidden = tmp_path / "hidden"
    hidden.mkdir(mode=0o700)
    (hidden / "verdict").write_text("0\n")
    (hidden / "token").write_text("root-only-secret\n")
    (hidden / "token").chmod(0o600)
    result = run([("main-1", f"report-swap\n{hidden}\n")], manifest=SCORE)
    assert "the report of run main-1 could not be opened" in result["error"]
    assert "root-only" not in json.dumps(result)


@pytest.mark.parametrize(
    ("inputs", "message"),
    [
        ({"self_reported": True}, "is self_reported without a manifest"),
        ({"self_reported": "yes"}, "self_reported of run main-1 is not true or false"),
        ({"manifest": {"file": "in/2/input"}}, "manifest of run main-1 is not valid"),
        ({"manifest": {"file": "in/none"}}, "manifest of run main-1 is not in the"),
    ],
)
def test_a_manifest_or_self_report_it_cannot_use_is_an_error(
    tmp_path: Path, inputs: dict[str, Any], message: str
) -> None:
    batch_inputs(tmp_path, b"", [("main-1", "not json")])
    document = json.loads((tmp_path / "inputs.json").read_text())
    document["batch"][0]["inputs"].update(inputs)
    (tmp_path / "inputs.json").write_text(json.dumps(document))
    primitive.main(["exec", str(tmp_path)])
    assert message in json.loads((tmp_path / "outputs.json").read_text())["error"]


def test_a_hard_link_into_the_report_folder_is_refused(
    run: Any, tmp_path: Path
) -> None:
    """The next run's input is not in the box to link to, so a run cannot
    hand exec the next run's input to read for it."""
    work = tmp_path / "work"
    result = run(
        [
            ("main-1", f"report-hardlink\n{work}/in/3/input\n"),
            ("main-2", "report\nverdict 0\nscore 1"),
        ],
        manifest=SCORE,
    )
    outputs = by_run(result)["main-1"]
    assert printed(work, outputs).startswith("refused ")
    assert outputs["outcome"] == "accepted"
    assert "message" not in outputs
    assert "extras" not in outputs


MB = 1024 * 1024


@pytest.mark.parametrize(
    ("held", "needed_mb"),
    [(None, 10**6), ("max\n", 10**6), (f"{256 * MB}\n", 192)],
)
def test_a_run_the_container_holds_is_let_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held: str | None, needed_mb: int
) -> None:
    """With no limit on the container, or room for the run's memory limit,
    its copies and the step's own, the run goes ahead."""
    memory = tmp_path / "memory.max"
    if held is not None:
        memory.write_text(held)
    monkeypatch.setattr(primitive, "CONTAINER_MEMORY", memory)
    primitive.check_memory(needed_mb * 1024, "main-1")


def test_a_run_the_container_cannot_hold_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = tmp_path / "memory.max"
    memory.write_text(f"{256 * MB}\n")
    monkeypatch.setattr(primitive, "CONTAINER_MEMORY", memory)
    with pytest.raises(primitive.PrimitiveError) as raised:
        primitive.check_memory(192 * 1024 + 1, "main-1")
    assert str(raised.value) == (
        "the run main-1 needs 193 MB for its memory limits and the files copied "
        "in beside them, and the container's 256 MB leaves 192 MB for them"
    )


def test_a_batch_with_a_run_the_container_cannot_hold_runs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The named files are counted with the memory limit, since they are
    copied into the box's tmpfs, charged to the step: so a run whose limit
    fits but whose files do not is refused before any program runs, an
    error of the step, not a run the container's limit kills."""
    memory = tmp_path / "memory.max"
    memory.write_text(f"{192 * MB}\n")
    monkeypatch.setattr(primitive, "CONTAINER_MEMORY", memory)
    monkeypatch.setenv(primitive.BOX_USERS_VARIABLE, BOX_USERS)
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    files = {"main-2": {"data": b"x" * (80 * MB)}}
    items = [("main-1", "double\n21\n"), ("main-2", "double\n21\n")]
    batch_inputs(work, binary, items, files=files)
    assert primitive.main(["exec", str(work)]) == 0
    result = json.loads((work / "outputs.json").read_text())
    assert result["error"].startswith("the run main-2 needs 145 MB")
    assert "batch" not in result
    assert not (work / "out").exists()
