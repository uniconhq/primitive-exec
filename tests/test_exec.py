"""The program's own logic, run in-process on Linux.

The runs use Python binaries on the interpreter running the tests, started
through a `sandbox-exec` built from this checkout with the host's gcc.
"""

import ctypes
import errno
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import zipfile
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import exec as primitive
from support import (
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


@pytest.fixture(scope="session")
def helper(tmp_path_factory: pytest.TempPathFactory) -> str:
    """`sandbox-exec` built from source."""
    if shutil.which("gcc") is None:
        pytest.skip("gcc is not installed")
    built = tmp_path_factory.mktemp("helper") / "sandbox-exec"
    source = Path(__file__).resolve().parent.parent / "src" / "sandbox-exec.c"
    command = ["gcc", "-O2", "-Wall", "-Werror", "-o", str(built), str(source)]
    subprocess.run(command, check=True)
    return str(built)


@pytest.fixture(autouse=True)
def tables(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Stand-in System V tables, empty, so a sweep in these tests never
    removes an object of the machine running them, which is not a step
    container with an IPC namespace of its own. The image tests sweep the
    real ones.
    """
    folder = tmp_path / "sysvipc"
    folder.mkdir()
    for kind in ("shm", "msg", "sem"):
        (folder / kind).write_text(f"       key      {kind}id perms\n")
    monkeypatch.setattr(primitive, "SYSVIPC", folder)
    return folder


@pytest.fixture
def run(helper: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """Run the program over a batch of the test program's cases."""
    monkeypatch.setattr(primitive, "HELPER", helper)
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
        "returncode": 0,
        "cpu_seconds": 0.1,
        "wall_seconds": 0.2,
        "memory_kb": 1000,
        "output_bytes": 10,
        "stopped": None,
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


JUDGED: list[tuple[dict[str, Any], str, str]] = [
    ({}, "native", "accepted"),
    ({"stopped": "time"}, "native", "time_limit"),
    ({"stopped": "memory", "returncode": -9}, "native", "memory_limit"),
    ({"stopped": "output", "returncode": -9}, "native", "output_limit"),
    ({"returncode": -signal.SIGXFSZ}, "native", "output_limit"),
    (
        {"output_bytes": primitive.OUTPUT_LIMIT + 1, "returncode": 1},
        "python",
        "output_limit",
    ),
    ({"cpu_seconds": 1.01}, "native", "time_limit"),
    ({"wall_seconds": 3.5}, "native", "time_limit"),
    ({"returncode": -signal.SIGXCPU}, "native", "time_limit"),
    ({"memory_kb": 64 * 1024 + 1}, "native", "memory_limit"),
    ({"returncode": 3}, "java", "memory_limit"),
    ({"returncode": 3}, "native", "runtime_error"),
    ({"returncode": -signal.SIGSEGV}, "native", "runtime_error"),
]
"""Runs and how `judge` ends each, one of every way it decides."""


@pytest.mark.parametrize(("changes", "kind", "outcome"), JUDGED)
def test_judge(changes: dict[str, Any], kind: str, outcome: str) -> None:
    """The stop reason first, then output, time and memory, then the exit."""
    assert primitive.judge(make_run(**changes), ITEM, kind) == outcome


def test_the_declaration_says_what_each_outcome_it_reports_means() -> None:
    """The outcome port's outcomes are exactly the ones `judge` gives and a
    report's verdict, each with a line saying when, for the workflow editor's
    drawer.
    """
    reported = {
        primitive.judge(make_run(**changes), ITEM, kind) for changes, kind, _ in JUDGED
    } | {primitive.verdict_outcome(accepted) for accepted in (True, False)}

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


def test_cleanup_that_fails_after_an_error_does_not_hide_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
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
    monkeypatch.setattr(primitive, "landlock_abi", lambda: 6)
    monkeypatch.setattr(primitive, "execute", fail)
    monkeypatch.setattr(primitive, "remove_tree", fail_cleanup)
    assert primitive.main(["exec", str(tmp_path)]) == 0
    result = json.loads((tmp_path / "outputs.json").read_text())
    assert result["error"] == "the first error"
    assert "the cleanup error" in capsys.readouterr().err


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


def test_a_run_without_stdin_reads_nothing(
    helper: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no `stdin`, standard input is empty, as `/dev/null` is."""
    monkeypatch.setattr(primitive, "HELPER", helper)
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
    run: Any, tmp_path: Path
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


def test_a_binary_in_no_known_format_is_an_error(tmp_path: Path) -> None:
    """A program exec cannot run is the step failing, not the contestant."""
    batch_inputs(tmp_path, b"#!/bin/sh\n", [("main-1", "")])
    primitive.main(["exec", str(tmp_path)])
    result = json.loads((tmp_path / "outputs.json").read_text())
    assert "not a native executable" in result["error"]


def test_every_outcome(run: Any, tmp_path: Path) -> None:
    """One batch reaches each outcome, keeps each output and measures each run,
    answering in the batch's order under each item's run, with each output
    under the item's place in the batch.
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
    assert outputs["main-grow"]["memory_kb"] > 64 * 1024
    assert outputs["samples-ok"]["memory_kb"] < 64 * 1024
    assert not list(work.glob(".exec-*"))


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
    """Printing too much is an output limit, and the output is cut at the limit."""
    result = run([("main-flood", "flood\n")], time_limit=10)
    assert by_run(result)["main-flood"]["outcome"] == "output_limit"
    output = tmp_path / "work" / "out" / "1" / "output"
    assert output.stat().st_size == primitive.OUTPUT_LIMIT


def test_processes_left_behind_are_killed(run: Any) -> None:
    """A process a run leaves behind, even in its own session, is killed."""
    result = run([("main-linger", "linger\n")])
    assert by_run(result)["main-linger"]["outcome"] == "accepted"
    assert primitive.child_pids() == []


def test_runs_write_only_their_own_directory(run: Any, tmp_path: Path) -> None:
    """A run cannot write the working directory or the rest of /tmp."""
    work = tmp_path / "work"
    run([("main-escape", f"escape\n{work}\n")])
    lines = (work / "out" / "1" / "output").read_text().splitlines()
    assert lines == [
        f"refused {work}/escaped 13",
        f"refused {work}/inputs.json 13",
        "refused /tmp/escaped 13",
        "cwd ['here']",
    ]


def test_a_run_that_kills_its_launcher_is_a_runtime_error(
    run: Any, tmp_path: Path
) -> None:
    """From Landlock ABI 6 the run may not signal `sandbox-exec` at all; below
    it, killing `sandbox-exec` leaves no report and the run is judged on what
    the program measured itself. Either way it is the run's own runtime
    error, never an error of the step.
    """
    result = run([("main-parricide", "parricide\n"), ("main-ok", "double\n21\n")])
    outputs = by_run(result)
    assert outputs["main-parricide"]["outcome"] == "runtime_error"
    assert outputs["main-ok"]["outcome"] == "accepted"
    said = printed(tmp_path / "work", outputs["main-parricide"]).splitlines()
    if primitive.landlock_abi() >= 6:
        assert said == [f"refused {errno.EPERM}"]


def test_a_run_that_stops_its_launcher_is_still_ended_on_time(run: Any) -> None:
    """A binary that sends SIGSTOP to `sandbox-exec`, where Landlock lets it,
    and then spins is stopped at its time limit, and the whole item is over
    within a second or so of that, not after a long wait for the helper.
    """
    started = time.monotonic()
    result = run([("main-freeze", "freeze\n")], time_limit=0.5)
    elapsed = time.monotonic() - started
    assert by_run(result)["main-freeze"]["outcome"] == "time_limit"
    assert elapsed < 4


def test_a_run_the_kernel_killed_for_memory_is_over_the_memory_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = tmp_path / "memory.events"
    events.write_text("low 0\nhigh 0\nmax 3\noom 1\noom_kill 2\n")
    monkeypatch.setattr(primitive, "OOM_EVENTS", events)
    item = primitive.Item(
        "main-x",
        1,
        tmp_path / "binary",
        tmp_path / "input",
        time_limit=1.0,
        memory_limit=64,
    )
    run = primitive.Run(-9, 0.1, 0.2, 1000, 0, None, out_of_memory=True)

    assert primitive.oom_kills() == 2
    assert primitive.judge(run, item, "native") == "memory_limit"


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
    assert outputs["main-hidden-memory"]["memory_kb"] > 64 * 1024


def test_memory_spread_over_processes_is_added_up(run: Any) -> None:
    """Two processes that each hold 40 MB of their own at the same moment are
    over a 64 MB limit together, though each alone is under it.
    """
    result = run([("main-split", "split-memory\n")], time_limit=3, memory_limit=64)
    outputs = by_run(result)
    assert outputs["main-split"]["outcome"] == "memory_limit"
    assert outputs["main-split"]["memory_kb"] > 64 * 1024


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


def test_what_the_cgroup_holds_beyond_the_start_of_a_run_counts(tmp_path: Path) -> None:
    """Everything charged to the run's cgroup beyond what it held when the
    reading began counts, but for pages of files on disk; files in a tmpfs,
    which the cgroup counts as file pages too, count.
    """
    (tmp_path / "memory.current").write_text(str(910 * 2**20))
    (tmp_path / "memory.stat").write_text(
        f"anon {10 * 2**20}\nfile {900 * 2**20}\nshmem 0\n"
    )
    memory = primitive.RunMemory(cgroup=tmp_path)
    (tmp_path / "memory.current").write_text(str(84 * 2**20))
    (tmp_path / "memory.stat").write_text(
        f"anon {20 * 2**20}\nfile {60 * 2**20}\nshmem {50 * 2**20}\nsock {4 * 2**20}\n"
    )

    memory.read(set())

    assert memory.peak_kb == (84 - 10 - 10) * 1024


def test_a_cgroup_with_no_memory_accounting_adds_no_reading(tmp_path: Path) -> None:
    memory = primitive.RunMemory(cgroup=tmp_path / "absent")

    memory.read(set())

    assert (memory.held_before, memory.peak_kb) == (None, 0)


def test_a_run_outside_a_cgroup_of_its_own_is_warned_about(
    run: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """A test machine's cgroup holds other processes too, so its memory is
    not read, and the program says so.
    """
    assert primitive.own_cgroup() is None

    outputs = by_run(run([("main-ok", "double\n21\n")]))

    assert outputs["main-ok"]["outcome"] == "accepted"
    assert primitive.NO_CGROUP in capsys.readouterr().err


def test_the_total_is_read_only_when_it_could_raise_the_peak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading the processes' total walks every page they hold, so it is
    skipped while their resident memory added up is not above the peak, and
    read again only after `TOTAL_SPACING` times its own cost has passed.
    """
    resident = {1: (30_000, 30_000)}
    read: list[int] = []

    def proportional(pid: int, resident_kb: int) -> int:
        read.append(pid)
        return resident_kb

    monkeypatch.setattr(primitive, "resident_memory", resident.__getitem__)
    monkeypatch.setattr(primitive, "proportional_memory", proportional)
    memory = primitive.RunMemory()
    memory.read({1})
    assert (memory.peak_kb, read) == (30_000, [])
    resident[2] = (40_000, 40_000)
    memory.read({1, 2})
    assert (memory.peak_kb, sorted(read)) == (70_000, [1, 2])
    memory.total_due = float("inf")
    memory.read({1, 2})
    assert len(read) == 2
    memory.total_due = 0.0
    resident.update({1: (30_000, 10_000), 2: (40_000, 10_000)})
    memory.read({1, 2})
    assert (memory.peak_kb, len(read)) == (70_000, 2)


def test_a_native_binary_the_kernel_will_not_run_is_a_runtime_error(
    helper: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(primitive, "HELPER", helper)
    batch_inputs(tmp_path, b"\x7fELF" + bytes(60), [("main-1", "")])
    primitive.main(["exec", str(tmp_path)])
    result = json.loads((tmp_path / "outputs.json").read_text())
    outputs = by_run(result)["main-1"]
    assert outputs["outcome"] == "runtime_error"
    assert (outputs["time_ms"], outputs["memory_kb"]) == (0, 0)


def test_the_report_reads_only_the_lines_sandbox_exec_writes() -> None:
    report = b"P 12\nR 0 5 6 7\nR 0 0 0 0\nZ 1\n\xff\nE x\nR 1"
    assert primitive.parse_report(report) == {"P": [12], "R": [0, 5, 6, 7]}


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
    """A later run cannot read an earlier run's output or input, the step's
    own files, or list the working directory or /tmp, so nothing one test
    leaves reaches the next; the system's files it can.
    """
    work = str(tmp_path / "work")
    result = run([("main-first", "double\n21\n"), ("main-peek", peek_input(work))])
    outputs = by_run(result)
    assert outputs["main-peek"]["outcome"] == "accepted"
    assert printed(tmp_path / "work", outputs["main-first"]) == "42\n"
    lines = printed(tmp_path / "work", outputs["main-peek"]).splitlines()
    assert lines == peeked(work)


@pytest.mark.parametrize("abi", [0, 2], ids=["none", "below-truncate"])
def test_without_landlock_nothing_runs(
    run: Any, monkeypatch: pytest.MonkeyPatch, abi: int
) -> None:
    """A kernel without Landlock, with Landlock turned off, or with an ABI
    below 3, which cannot keep a run from truncating a file it may only
    read, is an error of the step, never a run left less confined.
    """
    monkeypatch.setattr(primitive, "landlock_abi", lambda: abi)
    result = run([("main-ok", "double\n21\n")])
    assert set(result) == {"schema_version", "error"}
    assert "Landlock ABI 3" in result["error"]
    assert "Linux 6.2" in result["error"]


def test_a_ruleset_the_kernel_refuses_is_an_error(
    run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(directory: Path, readable: list[Path], abi: int) -> int:
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(primitive, "landlock_ruleset", refuse)
    result = run([("main-ok", "double\n21\n")])
    assert "could not be confined with Landlock" in result["error"]


def test_the_launcher_starts_nothing_without_a_ruleset(helper: str) -> None:
    """`sandbox-exec` refuses to run a binary it has no Landlock ruleset for."""
    read_end, write_end = os.pipe()
    done = subprocess.run(
        [helper, str(write_end), "-1", "/bin/true"], pass_fds=(write_end,)
    )
    os.close(write_end)
    report = os.read(read_end, 64)
    os.close(read_end)
    assert done.returncode == 2
    assert primitive.parse_report(report) == {"L": [errno.EBADF]}


def test_the_sweep_removes_every_object_the_kernel_lists(
    tables: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tables / "shm").write_text(
        "       key      shmid perms\n"
        "  1592590336          5   600\n"
        "           0          9  1600\n"
    )
    (tables / "sem").write_text("       key      semid perms\n  0   7   600\n")
    removed: list[tuple[str, int]] = []
    monkeypatch.setattr(
        primitive, "remove_sysv", lambda kind, ident: removed.append((kind, ident))
    )
    primitive.sweep_sysv()
    assert removed == [("shm", 5), ("shm", 9), ("sem", 7)]


def test_an_object_already_gone_is_passed_over() -> None:
    primitive.remove_sysv("shm", 2**31 - 1)
    primitive.remove_sysv("msg", 2**31 - 1)
    primitive.remove_sysv("sem", 2**31 - 1)


def test_an_object_that_cannot_be_removed_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A segment left behind would carry one run's data to the next."""

    class Refusing:
        @staticmethod
        def shmctl(*_: object) -> int:
            ctypes.set_errno(errno.EPERM)
            return -1

    monkeypatch.setattr(primitive, "libc", Refusing)
    with pytest.raises(primitive.PrimitiveError, match="shm 5, could not be removed"):
        primitive.remove_sysv("shm", 5)


def test_an_attribute_on_a_locked_file_or_folder_is_removed(tmp_path: Path) -> None:
    """A run that marks a path and then takes its access away, which Landlock
    does not stop, still has its mark removed."""
    folder = tmp_path / "in" / "3"
    folder.mkdir(parents=True)
    marked = folder / "input"
    marked.write_text("hidden")
    os.setxattr(marked, "user.cache", b"x")
    os.setxattr(folder, "user.cache", b"x")
    marked.chmod(0o444)
    folder.chmod(0)
    primitive.clear_attributes(tmp_path)
    assert os.listxattr(folder) == []
    assert os.listxattr(marked) == []


def test_an_attribute_that_cannot_be_removed_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One left behind would carry a run's data to the next."""
    os.setxattr(tmp_path, "user.cache", b"x")

    def refuse(*_: object, **__: object) -> None:
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "removexattr", refuse)
    with pytest.raises(primitive.PrimitiveError, match="could not be removed"):
        primitive.clear_attributes(tmp_path)


def test_a_path_gone_or_without_attributes_is_passed_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unsupported(*_: object, **__: object) -> None:
        raise OSError(errno.ENOTSUP, "Operation not supported")

    monkeypatch.setattr(os, "listxattr", unsupported)
    primitive.clear_attributes(tmp_path)
    primitive.clear_attributes(tmp_path / "gone")


@pytest.mark.parametrize(
    ("pids", "limit"), [("64\n", 64), ("4096\n", 128), ("max\n", 128), (None, 128)]
)
def test_the_process_limit_is_the_containers_or_the_declarations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pids: str | None, limit: int
) -> None:
    path = tmp_path / "pids.max"
    if pids is not None:
        path.write_text(pids)
    monkeypatch.setattr(primitive, "PIDS_MAX", path)
    assert primitive.process_limit() == limit


def test_a_filter_that_cannot_be_installed_is_an_error(
    run: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`sandbox-exec` reports a filter the kernel refuses and starts nothing;
    the run is not started unfiltered."""
    refusing = tmp_path / "refusing-helper"
    refusing.write_text(
        f"#!{os.path.realpath(sys.executable)}\nimport os, sys\n"
        f"os.write(int(sys.argv[1]), b'S {errno.EINVAL}\\n')\nsys.exit(127)\n"
    )
    refusing.chmod(0o755)
    monkeypatch.setattr(primitive, "HELPER", str(refusing))
    result = run([("main-ok", "double\n21\n")])
    assert set(result) == {"schema_version", "error"}
    assert "could not be put under its system call filter" in result["error"]


def test_the_launcher_filters_sockets(helper: str, tmp_path: Path) -> None:
    """`sandbox-exec` refuses an internet socket to what it starts and lets
    a Unix one through."""
    script = (
        "import socket\n"
        "for family in (socket.AF_INET, socket.AF_UNIX):\n"
        "    try:\n"
        "        socket.socket(family, socket.SOCK_DGRAM).close()\n"
        "        print('made', family.name)\n"
        "    except OSError as error:\n"
        "        print('refused', family.name, error.errno)\n"
    )
    python = os.path.realpath(sys.executable)
    read_end, write_end = os.pipe()
    ruleset = primitive.landlock_ruleset([tmp_path], [Path(sys.base_prefix)], 3)
    done = subprocess.run(
        [helper, str(write_end), str(ruleset), python, "-I", "-c", script],
        pass_fds=(write_end, ruleset),
        capture_output=True,
        text=True,
    )
    os.close(write_end)
    os.close(ruleset)
    os.close(read_end)
    assert done.stdout.splitlines() == [
        f"refused AF_INET {errno.EPERM}",
        "made AF_UNIX",
    ]


def test_a_machine_without_system_v_tables_warns_and_runs(
    run: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(primitive, "SYSVIPC", tmp_path / "missing")
    outputs = by_run(run([("main-ok", "double\n21\n")]))
    assert outputs["main-ok"]["outcome"] == "accepted"
    assert primitive.NO_SYSVIPC in capsys.readouterr().err


def test_a_sweep_with_nothing_to_remove_takes_under_a_millisecond() -> None:
    started = time.perf_counter()
    for _ in range(1000):
        primitive.sweep_sysv()
    assert time.perf_counter() - started < 1.0


def test_each_item_runs_a_copy_of_the_binary_of_its_own(
    run: Any, tmp_path: Path
) -> None:
    """An attribute or a time one run sets on the file it runs, which Landlock
    does not stop, is gone for the next.
    """
    outputs = by_run(run([("main-mark", "mark\n"), ("main-read", "read-mark\n")]))
    work = tmp_path / "work"
    assert printed(work, outputs["main-mark"]) == "marked\n"
    assert printed(work, outputs["main-read"]) == "no attribute\ntime fresh\n"


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
        accepted=True,
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
    assert not report.accepted
    assert report.outputs == {"message": "wrong on line 2"}
    assert report.shown == {"score": b"oops"}


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
        ({"verdict": b""}, "has a verdict '', not 0 or 1"),
        ({"verdict": b"2"}, "has a verdict '2', not 0 or 1"),
        ({"verdict": b"0 1"}, "has a verdict '0 1', not 0 or 1"),
        ({"verdict": b"accepted"}, "has a verdict 'accepted', not 0 or 1"),
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


def test_each_run_reports_into_a_fresh_folder(run: Any, tmp_path: Path) -> None:
    """The folder is outside the run's working directory, and what one run
    left in its folder is not there for the next."""
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
        ("report\nverdict 0\nscore 1", "accepted"),
    ],
)
def test_a_self_reported_run_that_does_not_judge_is_the_entrants(
    run: Any, stdin: str, outcome: str
) -> None:
    """Where the program runs the entrant's code, what it did is the run's
    own outcome, never a fault (`SIMPLER-CORE.md` 7.3), with no report."""
    result = run(
        [("main-1", stdin)], manifest=SCORE, self_reported=True, time_limit=0.5
    )
    outputs = by_run(result)["main-1"]
    assert outputs["outcome"] == outcome
    assert ("score" in outputs) == (outcome == "accepted")


def test_a_link_in_the_report_folder_reads_nothing_for_the_run(
    run: Any, tmp_path: Path
) -> None:
    """A link to the next run's input, which the run may not read, is a
    fault, and exec does not read the input for it."""
    work = tmp_path / "work"
    result = run(
        [("main-1", f"report-link\n{work}/in/3/input\n"), ("main-2", "hidden answer")],
        manifest=SCORE,
    )
    assert "the report of run main-1 holds message, not a plain file" in result["error"]
    assert "hidden" not in json.dumps(result)


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
    """Landlock refuses a link from outside the run's own folders, so a run
    cannot hand exec the next run's input to read for it."""
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
