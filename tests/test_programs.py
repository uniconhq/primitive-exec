"""The program run in-process: its inputs, and every program it runs, each in
an isolate box of its own.

The tests that run programs need root and isolate, as a step does, so they
are skipped elsewhere and run in the privileged test container
(`tests/Dockerfile`), started by `test_image.py`, where `UNICON_TESTS_IN_BOXES`
makes a skip a failure.
"""

import itertools
import json
import os
import re
import resource
import signal
import sys
from pathlib import Path
from typing import Any

import pytest

from exec import box, inputs, programs, report, shared
from exec.main import main
from support import (
    BOX_USERS,
    ENTRANT,
    GAME_LINKS,
    NOTES,
    SECRET,
    STEP_MEMORY_MB,
    Programs,
    Spec,
    TestItem,
    by_run,
    declaration,
    facts_of,
    game,
)
from support import (
    PROGRAMS_PROGRAM as PROGRAM,
)
from support import (
    programs_batch_inputs as batch_inputs,
)

IN_BOXES = os.environ.get("UNICON_TESTS_IN_BOXES") == "1"
EXEC_OUTCOMES = ("time_limit", "memory_limit", "output_limit", "runtime_error")


@pytest.fixture
def boxes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give interact the tests' box users and step memory, or skip a test
    that runs programs where interact cannot make boxes."""
    able = sys.platform == "linux" and os.geteuid() == 0
    if not (able and Path(box.ISOLATE).is_file()):
        if IN_BOXES:
            pytest.fail("the test container has no root with isolate")
        pytest.skip("runs programs in isolate boxes: run in the test container")
    monkeypatch.setenv(box.BOX_USERS_VARIABLE, BOX_USERS)
    monkeypatch.setenv(box.STEP_MEMORY_VARIABLE, STEP_MEMORY_MB)


class Batch:
    """One run of interact over a batch: its `outputs.json`, and the facts of
    each item by run."""

    def __init__(self, work: Path, document: dict[str, Any]) -> None:
        self.work = work
        self.document = document

    @property
    def outputs(self) -> dict[str, dict[str, Any]]:
        return by_run(self.document)

    def facts(self, run: str) -> dict[str, dict[str, Any]]:
        return facts_of(self.work, self.outputs[run])


@pytest.fixture
def run(boxes: None, tmp_path: Path) -> Any:
    """Run interact in-process over a batch of test items, each call in a
    working directory of its own."""
    count = itertools.count()

    def batch(items: list[TestItem]) -> Batch:
        work = tmp_path / f"work-{next(count)}"
        batch_inputs(work, items)
        assert main(["interact", str(work)]) == 0
        document: dict[str, Any] = json.loads(
            (work / "outputs.json").read_text(encoding="utf-8")
        )
        return Batch(work, document)

    return batch


def one(run: Any, programs: Programs, links: list[str]) -> dict[str, dict[str, Any]]:
    """Run one item and return its facts, checking its outcome."""
    done = run([("main-1", programs, links)])
    assert "error" not in done.document, done.document
    assert done.outputs["main-1"]["outcome"] == "accepted"
    facts: dict[str, dict[str, Any]] = done.facts("main-1")
    return facts


def program(args: str, **options: Any) -> Spec:
    """A program playing `PROGRAM`'s case `args`."""
    return Spec(PROGRAM, args, **options)


# The game.


def test_the_guessing_game_correct_wrong_and_too_slow(run: Any) -> None:
    """One batch, three items: a right entrant is the judge's `0` with its
    reports as outputs; a wrong one the judge's `1`; a slow one over its time
    limit, which stops the judge before it reads the entrant gone, so the
    judge judges nothing."""
    done = run(
        [
            ("main-guess", game("guess"), GAME_LINKS),
            ("main-wrong", game("wrong"), GAME_LINKS),
            ("main-slow", game("slow"), GAME_LINKS),
        ]
    )
    outputs = done.outputs
    right = done.facts("main-guess")
    assert right["judge"]["verdict"] == "0"
    assert right["judge"]["message"] == "found"
    assert right["judge"]["reports"] == {"guesses": 10}
    assert right["entrant"]["ended"] == "finished"
    assert outputs["main-guess"]["judge.guesses"] == 10
    assert outputs["main-guess"]["entrant.ended"] == "finished"
    assert outputs["main-guess"]["facts"] == {"file": "out/1/facts.json"}

    wrong = done.facts("main-wrong")
    assert wrong["judge"]["verdict"] == "1"
    assert wrong["judge"]["message"] == "too many guesses"
    assert "reports" not in wrong["judge"]
    assert "judge.guesses" not in outputs["main-wrong"]

    slow = done.facts("main-slow")
    assert slow["entrant"]["ended"] == "time_limit"
    assert slow["entrant"]["time_ms"] >= 1000
    assert "stopped_by" not in slow["entrant"]
    assert slow["judge"]["stopped_by"] == "entrant"
    assert slow["judge"]["ended"] == "runtime_error"
    assert slow["judge"]["signal"] == "SIGKILL"
    assert "verdict" not in slow["judge"]
    assert slow["judge"]["ended_at"] >= slow["entrant"]["ended_at"]


def test_an_entrant_that_leaves_early_is_judged_by_the_judge(run: Any) -> None:
    facts = one(run, game("leave"), GAME_LINKS)
    assert facts["entrant"]["ended"] == "finished"
    assert facts["judge"]["verdict"] == "1"
    assert facts["judge"]["message"] == "the program stopped guessing"
    assert facts["judge"]["ended_at"] >= facts["entrant"]["ended_at"]


def test_a_judge_that_crashes_judges_nothing(run: Any) -> None:
    facts = one(run, game("guess", "crash"), GAME_LINKS)
    judge = facts["judge"]
    assert judge["ended"] == "runtime_error"
    assert "verdict" not in judge and "stopped_by" not in judge
    assert "signal" not in judge
    assert facts["entrant"]["ended"] == "finished"
    assert judge["ended_at"] < facts["entrant"]["ended_at"]


def test_a_judge_that_crashes_always_ends_before_the_entrant_it_let_go(
    run: Any,
) -> None:
    """The entrant waiting on a judge that crashed ends only once interact
    lets it see the judge gone, often in the same millisecond: 6 of 40 ended
    at the judge's time when each wake's time was the clock's alone, which
    the verdict script reads as a judge that ended after the entrant."""
    items = [(f"main-{n}", game("guess", "crash"), GAME_LINKS) for n in range(40)]
    done = run(items)
    assert "error" not in done.document, done.document
    for name, _, _ in items:
        facts = done.facts(name)
        assert facts["judge"]["ended"] == "runtime_error"
        assert facts["entrant"]["ended"] == "finished"
        assert facts["judge"]["ended_at"] < facts["entrant"]["ended_at"], name


def test_a_judge_over_its_own_memory_stops_the_entrant(run: Any) -> None:
    facts = one(run, game("guess", "grow", judge_memory_limit=64), GAME_LINKS)
    assert facts["judge"]["ended"] == "memory_limit"
    assert facts["judge"]["memory_kb"] >= 48 * 1024
    assert "stopped_by" not in facts["judge"]
    assert facts["entrant"]["stopped_by"] == "judge"
    assert facts["entrant"]["memory_kb"] < 48 * 1024


def test_a_judge_over_its_own_time_stops_the_entrant(run: Any) -> None:
    programs = game("sleep")
    programs["judge"] = Spec(PROGRAM, "spin", time_limit=0.5, manifest=NOTES)
    facts = one(run, programs, GAME_LINKS)
    assert facts["judge"]["ended"] == "time_limit"
    assert facts["judge"]["time_ms"] >= 500
    assert facts["entrant"]["stopped_by"] == "judge"


@pytest.mark.parametrize(
    ("entrant", "judge", "verdict"),
    [("leave", "pipe", None), ("fail", "pipe", None), ("fail", "judge-pipe", "1")],
)
def test_a_judge_killed_by_the_closed_pipe_ends_after_the_entrant(
    run: Any, entrant: str, judge: str, verdict: str | None
) -> None:
    """A native judge writing to an entrant that has ended dies of `SIGPIPE`,
    and only once interact has seen the entrant end: the fact that tells it
    from a judge that crashed on its own. A verdict written first stands."""
    facts = one(run, game(entrant, judge), GAME_LINKS)
    assert facts["judge"]["ended"] == "runtime_error"
    assert facts["judge"]["signal"] == "SIGPIPE"
    assert facts["judge"]["ended_at"] >= facts["entrant"]["ended_at"]
    assert facts["judge"].get("verdict") == verdict
    assert facts["entrant"]["ended"] == (
        "finished" if entrant == "leave" else "runtime_error"
    )


@pytest.mark.parametrize(
    ("entrant", "judge", "said", "verdict"),
    [
        ("leave", "pipe-python", None, None),
        ("guess", "farewell", None, "0"),
        ("guess", "farewell-native", "SIGPIPE", "0"),
    ],
)
def test_a_judge_that_writes_to_an_entrant_that_left_keeps_its_verdict(
    run: Any, entrant: str, judge: str, said: str | None, verdict: str | None
) -> None:
    """A Python judge writing to an entrant that has ended raises and exits
    1, with no signal, and a native one dies of `SIGPIPE`; either ends after
    the entrant, and its report is read though it did not finish, so a
    verdict written first is in its facts."""
    facts = one(run, game(entrant, judge), GAME_LINKS)
    assert facts["judge"]["ended"] == "runtime_error"
    assert facts["judge"].get("signal") == said
    assert facts["judge"]["ended_at"] >= facts["entrant"]["ended_at"]
    assert facts["judge"].get("verdict") == verdict
    assert facts["entrant"]["ended"] == "finished"


def test_a_verdict_written_before_a_stop_stands(run: Any) -> None:
    """A judge that wrote a `1` and waits is stopped for the entrant's limit,
    and its whole report is read: the verdict script's rule 2."""
    facts = one(run, game("sleep", "judge-wait", entrant_time_limit=0.5), GAME_LINKS)
    assert facts["entrant"]["ended"] == "time_limit"
    assert facts["entrant"]["stopped_by"] == "wall_clock"
    assert facts["judge"]["stopped_by"] == "entrant"
    assert facts["judge"]["verdict"] == "1"
    assert facts["judge"]["message"] == "no"


def test_a_program_that_never_flushes_ends_at_its_wall_clock(run: Any) -> None:
    """Two programs each waiting on the other use no CPU: the first wall
    clock to run out, the entrant's, twice its time limit and a second,
    ends it as a `time_limit`, stopped by the wall clock, and it stops the
    judge."""
    facts = one(run, game("unflushed", entrant_time_limit=0.5), GAME_LINKS)
    entrant = facts["entrant"]
    assert entrant["ended"] == "time_limit"
    assert entrant["stopped_by"] == "wall_clock"
    assert entrant["time_ms"] == 500
    assert 2000 <= entrant["ended_at"] < 4000
    assert facts["judge"]["stopped_by"] == "entrant"
    assert "verdict" not in facts["judge"]


def test_a_judge_may_name_an_outcome_as_its_verdict(run: Any) -> None:
    facts = one(run, game("guess", "word"), GAME_LINKS)
    assert facts["judge"]["verdict"] == "time_limit"
    assert facts["judge"]["message"] == "too slow"


# Stopping.


@pytest.mark.parametrize(
    ("case", "limits", "ended"),
    [
        ("grow", {"memory_limit": 32}, "memory_limit"),
        ("spin", {"time_limit": 0.3}, "time_limit"),
        ("flood", {}, "output_limit"),
    ],
)
def test_a_program_over_its_limit_stops_the_others(
    run: Any, case: str, limits: dict[str, float], ended: str
) -> None:
    programs = {
        "a": program("sleep"),
        "over": program(case, **limits),
        "c": program("sleep", time_limit=5),
    }
    facts = one(run, programs, [])
    assert facts["over"]["ended"] == ended
    assert "stopped_by" not in facts["over"]
    for name in ("a", "c"):
        assert facts[name]["stopped_by"] == "over"
        assert facts[name]["ended"] == "runtime_error"
        assert facts[name]["signal"] == "SIGKILL"
        assert facts[name]["ended_at"] >= facts["over"]["ended_at"]
        assert facts[name]["ended_at"] < 2500


def test_stops_others_stops_them_however_it_ends(run: Any) -> None:
    programs = {
        "a": program("leave", stops_others=True),
        "b": program("sleep", time_limit=5),
    }
    facts = one(run, programs, [])
    assert facts["a"]["ended"] == "finished"
    assert facts["b"]["stopped_by"] == "a"
    assert facts["b"]["ended_at"] < 2000


def test_a_program_ending_without_stops_others_leaves_the_others(run: Any) -> None:
    programs = {
        "a": program("leave"),
        "b": program("end", manifest=NOTES),
    }
    facts = one(run, programs, ["a -> b"])
    assert "stopped_by" not in facts["b"]
    assert facts["b"]["ended"] == "finished"
    assert facts["b"]["message"] == ""


def test_a_program_that_cannot_start_is_a_runtime_error_and_others_run(
    run: Any,
) -> None:
    programs = {
        "a": program("say \0"),
        "b": program("end", manifest=NOTES),
    }
    facts = one(run, programs, ["a -> b"])
    assert facts["a"]["ended"] == "runtime_error"
    assert facts["a"]["ended_at"] == 0
    assert facts["a"]["time_ms"] == 0
    assert facts["b"]["message"] == ""


# Links.


def test_a_pair(run: Any) -> None:
    programs = {"a": program("say hello"), "b": program("end", manifest=NOTES)}
    assert one(run, programs, ["a -> b"])["b"]["message"] == "hello"


def test_a_chain_of_three(run: Any) -> None:
    programs = {
        "a": program("say 1"),
        "b": program("add 2"),
        "c": program("end", manifest=NOTES),
    }
    assert one(run, programs, ["a -> b", "b -> c"])["c"]["message"] == "1 2"


def test_a_ring(run: Any) -> None:
    programs = {
        "a": program("ring x", manifest=NOTES),
        "b": program("add y"),
        "c": program("add z"),
    }
    facts = one(run, programs, ["a -> b", "b -> c", "c -> a"])
    assert facts["a"]["message"] == "x y z"


def test_links_past_the_first_are_named_pipes(run: Any) -> None:
    """A program's first link each way is its standard stream, and each
    further one a named pipe in its folder, linked from its working
    directory; nothing else is there."""
    programs = {
        "x": program("hub", manifest=NOTES),
        "y": program("add d"),
        "z": program("add c"),
    }
    links = ["x -> z", "x -> y", "z -> x", "y -> x"]
    facts = one(run, programs, links)
    pipes = "['from_y', 'to_y']"
    assert facts["x"]["message"] == f"{pipes} {pipes} ['a c', 'b d']"


def test_a_program_reads_no_other_programs_files(run: Any) -> None:
    """The other box's folder is not in this one, no process of the other
    box is to be seen, and this program's own links cannot be opened the
    other way through `/proc/self/fd`, so it cannot read what it writes
    or write what it reads."""
    programs = {
        "secret": program("secret", files=SECRET),
        "pry": program("pry 0", manifest=NOTES, stops_others=True),
    }
    facts = one(run, programs, ["secret -> pry", "pry -> secret"])
    seen = json.loads(facts["pry"]["message"])
    assert seen["found"] == ["ENOENT", "EACCES", "EACCES"], seen
    assert seen["seen"] <= 1, seen
    assert "done" not in seen["tried"], seen
    assert facts["secret"]["stopped_by"] == "pry"


def test_a_named_pipe_opens_only_the_way_it_runs(run: Any) -> None:
    programs = {
        "a": program("pipe-ways", manifest=NOTES, stops_others=True),
        "b": program("sleep"),
        "c": program("sleep"),
    }
    facts = one(run, programs, ["a -> c", "a -> b", "c -> a", "b -> a"])
    assert facts["a"]["message"] == "['EACCES', 'EACCES', 'EROFS']"


def test_a_standard_stream_cannot_be_reopened(run: Any) -> None:
    """A first link each way is an anonymous pipe, root's, so a program
    cannot change its standard output's mode or reopen either standard
    stream the other way through `/proc/self/fd`."""
    programs = {
        "a": program("streams", manifest=NOTES, stops_others=True),
        "b": program("sleep"),
    }
    facts = one(run, programs, ["a -> b", "b -> a"])
    assert json.loads(facts["a"]["message"]) == ["EPERM", "EACCES", "EACCES"]


# Boxes.


def test_each_box_has_its_own_tmpfs(run: Any) -> None:
    """One program filling its `/tmp` leaves the other's free: each box's
    tmpfs is its own, as large as its memory limit."""
    programs = {
        "a": program("fill 40"),
        "b": program("free 40", manifest=NOTES, stops_others=True),
    }
    facts = one(run, programs, ["a -> b"])
    message = facts["b"]["message"]
    assert facts["b"]["ended"] == "finished", facts
    free = int(message.split()[1])
    assert free >= 50, message
    assert facts["a"]["memory_kb"] >= 40 * 1024
    assert facts["a"]["stopped_by"] == "b"


def test_memory_is_charged_only_to_its_own_box(run: Any) -> None:
    programs = {
        "big": program("hold 100", memory_limit=160),
        "small": program("small", manifest=NOTES, stops_others=True),
    }
    facts = one(run, programs, ["big -> small"])
    assert facts["big"]["memory_kb"] >= 100 * 1024
    assert facts["small"]["memory_kb"] < 32 * 1024
    assert facts["small"]["message"] == "small"


def test_a_program_over_its_own_memory_is_charged_only_its_own(run: Any) -> None:
    programs = {
        "hold": program("hold 30", memory_limit=64),
        "grow": program("grow once held", memory_limit=48),
    }
    facts = one(run, programs, ["hold -> grow"])
    assert facts["grow"]["ended"] == "memory_limit"
    assert facts["grow"]["memory_kb"] <= 49 * 1024
    assert facts["hold"]["ended"] == "runtime_error"
    assert facts["hold"]["stopped_by"] == "grow"
    assert 30 * 1024 <= facts["hold"]["memory_kb"] < 48 * 1024


def test_each_item_has_fresh_boxes(run: Any) -> None:
    done = run(
        [
            ("main-1", {"a": program("leave-tmp", manifest=NOTES)}, []),
            ("main-2", {"a": program("find-tmp", manifest=NOTES)}, []),
        ]
    )
    assert done.facts("main-2")["a"]["message"] == "[] []"
    assert done.outputs["main-2"]["facts"] == {"file": "out/2/facts.json"}


def test_eight_programs_run_side_by_side(run: Any) -> None:
    names = [f"p{number}" for number in range(8)]
    programs = {name: program("say 1") for name in names[:-1]}
    programs[names[-1]] = program("end", manifest=NOTES)
    links = [f"{writer} -> {names[-1]}" for writer in names[:-1]]
    facts = one(run, programs, links)
    assert len(facts) == 8
    assert facts[names[-1]]["message"] == "1"


# The box's walls, copied from exec.


@pytest.mark.parametrize("folder", [".", "/tmp", "/dev/shm"])
def test_filling_a_tmpfs_is_over_the_memory_limit(run: Any, folder: str) -> None:
    """Every page a program writes to its box's tmpfs, `/tmp` or `/dev/shm`
    is its box's memory: filling one ends `memory_limit`, never the
    container's disk."""
    facts = one(run, {"a": program(f"fill-at {folder}", memory_limit=48)}, [])
    assert facts["a"]["ended"] == "memory_limit"


@pytest.mark.parametrize("folder", [".", "/tmp", "/dev/shm"])
def test_too_many_files_in_a_tmpfs_is_over_the_memory_limit(
    run: Any, folder: str
) -> None:
    """A box's tmpfs holds 60,000 files and folders across `.`, `/tmp` and
    `/dev/shm`: a program making more is told `ENOSPC`, and interact gives
    `memory_limit`."""
    # Reach the inode cap before the CPU cap, as the single-program case does.
    facts = one(
        run, {"a": program(f"many-at {folder}", time_limit=4, memory_limit=256)}, []
    )
    assert facts["a"]["ended"] == "memory_limit"


def test_a_tree_as_deep_as_the_box_holds_ends_only_its_own_program(run: Any) -> None:
    """isolate gives a box's tree back with a folder open at each level, so
    interact raises its own stack and open-files limits; a program nesting
    folders until the box has no inode left ends `memory_limit`, and the
    others go on."""
    programs = {
        "deep": program("nest", time_limit=20, memory_limit=256),
        "other": program("sleep", time_limit=20),
    }
    facts = one(run, programs, [])
    assert facts["deep"]["ended"] == "memory_limit"
    assert facts["other"]["stopped_by"] == "deep"


def test_a_fork_bomb_stops_at_the_process_limit(run: Any) -> None:
    programs = {"a": program("fork-bomb", manifest=NOTES)}
    facts = one(run, programs, [])
    assert facts["a"]["message"] == "stopped True"


def test_a_box_dev_holds_only_its_devices(run: Any) -> None:
    """A box's `/dev` holds four devices and the standard links, and the
    machine's block devices cannot even be named."""
    paths = "/dev/mem /dev/kmsg /dev/sda"
    programs = {"a": program(f"dev {paths}", manifest=NOTES)}
    seen = json.loads(one(run, programs, [])["a"]["message"])
    assert seen["dev"] == [
        "fd",
        "null",
        "random",
        "shm",
        "stderr",
        "stdin",
        "stdout",
        "urandom",
        "zero",
    ]
    assert seen["blocked"] == ["ENOENT", "ENOENT", "ENOENT"]


def test_a_box_etc_holds_only_java(run: Any) -> None:
    programs = {"a": program("etc", manifest=NOTES)}
    assert json.loads(one(run, programs, [])["a"]["message"]) == ["java-21-openjdk"]


def test_a_box_reaches_no_network(run: Any) -> None:
    programs = {"a": program("net", manifest=NOTES)}
    message = one(run, programs, [])["a"]["message"]
    assert message != "done", message


def test_a_key_a_program_adds_is_refused(run: Any) -> None:
    """The box user's keyring is not namespaced, so a key would outlive the
    box; the box launcher's filter refuses the keyring calls, and isolate's
    `keyctl` too."""
    programs = {"a": program("keyring", manifest=NOTES)}
    assert json.loads(one(run, programs, [])["a"]["message"]) == {
        "add_key": "ENOSYS",
        "request_key": "ENOSYS",
        "keyctl": "ENOSYS",
    }


def test_a_program_can_watch_no_file(run: Any) -> None:
    """A cached file is one inode in every box that binds it, so a watch on
    it would see another grading open it: the box launcher's filter
    refuses inotify and fanotify, as isolate refuses F_NOTIFY."""
    programs = {"a": program("notify", manifest=NOTES)}
    assert json.loads(one(run, programs, [])["a"]["message"]) == {
        "inotify_init": "ENOSYS",
        "inotify_init1": "ENOSYS",
        "fanotify_init": "ENOSYS",
    }


def test_a_program_cannot_take_the_launchers_filter_off(run: Any) -> None:
    """The box launcher's filter is on beside isolate's, under
    `no_new_privs`, with no capability. A program cannot clear
    `no_new_privs`, and a filter it loads allowing every call is one more
    filter, so the keyring and inotify calls stay refused."""
    programs = {"a": program("filter", manifest=NOTES)}
    assert json.loads(one(run, programs, [])["a"]["message"]) == {
        "clear no_new_privs": "EINVAL",
        "allow all": "done",
        "add_key": "ENOSYS",
        "inotify_init1": "ENOSYS",
        "NoNewPrivs": "1",
        "Seccomp": "2",
        "Seccomp_filters": "3",
        "CapPrm": "0000000000000000",
        "CapEff": "0000000000000000",
    }


def test_the_step_is_capped_at_the_memory_it_was_given(run: Any) -> None:
    """The step's cgroup, which holds interact's own processes and every
    box, is capped at the memory the planner gave the step, whatever its
    items need within it: steps side by side in one slot stay within what
    the planner gave each."""
    two = {name: Spec(ENTRANT, "leave", memory_limit=96) for name in ("a", "b")}
    done = run([("main-1", {"a": Spec(ENTRANT, "leave")}, []), ("main-2", two, [])])
    assert "error" not in done.document, done.document
    capped = (box.STEP_CGROUP / "memory.max").read_text()
    assert int(capped) == int(STEP_MEMORY_MB) * 2**20
    assert box.BOX_CGROUPS.parent == box.STEP_CGROUP
    assert str(os.getpid()) in (box.MAIN_CGROUP / "cgroup.procs").read_text()


def test_the_steps_memory_is_what_it_was_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(box.STEP_MEMORY_VARIABLE, "640")
    assert box.step_memory_kb() == 640 * 1024


@pytest.mark.parametrize(
    ("given", "message"),
    [
        (None, "UNICON_STEP_MEMORY_MB is not set"),
        ("0", "is not a whole number of MB"),
        ("-512", "is not a whole number of MB"),
        ("512.5", "is not a whole number of MB"),
        (" 512", "is not a whole number of MB"),
        ("0512", "is not a whole number of MB"),
    ],
)
def test_a_steps_memory_it_cannot_use_is_an_error(
    monkeypatch: pytest.MonkeyPatch, given: str | None, message: str
) -> None:
    if given is None:
        monkeypatch.delenv(box.STEP_MEMORY_VARIABLE, raising=False)
    else:
        monkeypatch.setenv(box.STEP_MEMORY_VARIABLE, given)
    with pytest.raises(shared.PrimitiveError, match=re.escape(message)):
        box.step_memory_kb()


def test_the_box_launchers_self_test_passes_in_a_box(boxes: None) -> None:
    box.set_up_step(box.box_users(), box.step_memory_kb())
    box.check_launcher()


@pytest.mark.parametrize(
    ("launcher", "told"),
    [
        ("/bin/false", "Exited with error status 1"),
        ("/usr/bin/env", "--self-test"),
    ],
)
def test_a_box_launcher_that_fails_its_self_test_stops_the_step(
    run: Any, monkeypatch: pytest.MonkeyPatch, launcher: str, told: str
) -> None:
    """A launcher that cannot set its filter up would end every program as
    a runtime error, its message gone with the box's standard error: the
    step fails instead, saying what the launcher said, and nothing runs."""
    monkeypatch.setattr(box, "LAUNCHER", launcher)
    done = run([("main-1", game("guess"), GAME_LINKS)])
    assert set(done.document) == {"schema_version", "error"}
    assert done.document["error"].startswith("the box launcher's self-test failed")
    assert told in done.document["error"]
    assert not (done.work / "out").exists()


def test_a_hard_limit_below_what_isolate_needs_stops_the_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = resource.getrlimit

    def low(asked: int) -> tuple[int, int]:
        return (1024, 1024) if asked == resource.RLIMIT_NOFILE else real(asked)

    monkeypatch.setattr(resource, "getrlimit", low)
    monkeypatch.setattr(resource, "setrlimit", lambda *_: None)
    with pytest.raises(shared.PrimitiveError, match="hard limit on open files"):
        box.raise_limits()


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
    box.raise_limits()
    assert made == {
        resource.RLIMIT_STACK: (box.ISOLATE_STACK, resource.RLIM_INFINITY),
        resource.RLIMIT_NOFILE: (box.ISOLATE_OPEN_FILES, 524288),
    }


# Reports.


@pytest.mark.parametrize(
    ("judge", "fault"),
    [
        ("no-verdict", "the report of program judge of run main-1 has no verdict"),
        ("bad-number", "has guesses 0, below its least, 1"),
    ],
)
def test_a_program_that_finished_with_a_report_that_does_not_hold_is_an_error(
    run: Any, judge: str, fault: str
) -> None:
    done = run([("main-1", game("leave", judge), GAME_LINKS)])
    assert fault in done.document["error"]
    assert "batch" not in done.document


def test_a_link_left_in_a_report_is_a_report_that_does_not_hold(run: Any) -> None:
    done = run([("main-1", {"a": program("link-report", manifest=NOTES)}, [])])
    assert "holds label, not a plain file" in done.document["error"]


def test_a_report_folder_swapped_for_a_link_reads_nothing_for_the_program(
    run: Any, tmp_path: Path
) -> None:
    """The box's user owns `/box`, so a program can put a link where its
    report folder was, to a folder of root's the box cannot name; interact
    opens the folder without following it, so the report does not hold,
    nothing of root's is read, and `outputs.json` is still written."""
    hidden = tmp_path / "hidden"
    hidden.mkdir(mode=0o700)
    (hidden / "verdict").write_text("0\n")
    (hidden / "token").write_text("root-only-secret\n")
    (hidden / "token").chmod(0o600)
    programs = {"a": program(f"swap-report {hidden}", manifest=NOTES)}
    done = run([("main-1", programs, [])])
    assert (
        "report of program a of run main-1 could not be opened"
        in (done.document["error"])
    )
    assert "root-only" not in json.dumps(done.document)


def test_a_report_of_a_program_that_did_not_finish_is_left_out_if_it_does_not_hold(
    run: Any,
) -> None:
    programs = {
        "a": program("grow", memory_limit=32, manifest={"reports": {"n": {}}}),
    }
    facts = one(run, programs, [])
    assert facts["a"]["ended"] == "memory_limit"
    assert "verdict" not in facts["a"]


# Inputs.


def test_the_declaration_names_every_fact() -> None:
    port = declaration("programs")["inputs"]["programs"]
    assert tuple(port["outputs"]) == (*programs.FACTS, "log")
    assert port["outputs"]["log"]["once"] is True
    ended = port["outputs"]["ended"]["options"]
    assert ended == ["finished", *EXEC_OUTCOMES]
    assert set(programs.FACTS) <= inputs.PROGRAMS_OWN_NAMES
    assert tuple(report.VERDICT_WORDS) == EXEC_OUTCOMES
    assert len(port["inputs"]) == 9


def item(programs: dict[str, Any], links: object = None) -> dict[str, Any]:
    inputs: dict[str, Any] = {"programs": {"programs": programs}}
    if links is not None:
        inputs["links"] = links
    return {"run": "main-1", "inputs": inputs}


def entry(**more: Any) -> dict[str, Any]:
    source = {"source": {"folder": "in/p"}, "language": "python"}
    return source | {"time_limit": 1, "memory_limit": 64} | more


@pytest.mark.parametrize(
    ("given", "fault"),
    [
        (item({}), "does not hold one to 8 programs"),
        (
            item({f"p{n}": entry() for n in range(9)}),
            "does not hold one to 8 programs",
        ),
        ({"run": "main-1", "inputs": {"programs": {}}}, "is not programs"),
        (item({"Judge": entry()}), "names a program 'Judge', not a name"),
        (item({"a" * 65: entry()}), "not a name"),
        (item({"wall_clock": entry()}), "names a program wall_clock"),
        (item({"a": entry(), "b": entry()}, ["a->b"]), "not <from> -> <to>"),
        (item({"a": entry()}, "a -> b"), "is not a list of links"),
        (item({"a": entry()}, ["a -> b"]), "links b, not one of its programs"),
        (item({"a": entry()}, ["a -> a"]), "links a to itself"),
        (
            item({"a": entry(), "b": entry()}, ["a -> b", "a -> b"]),
            "gives a -> b twice",
        ),
        (
            item(
                {
                    "a": entry(files={"files": {"to_c": {"file": "in/f"}}}),
                    "b": entry(),
                    "c": entry(),
                },
                ["a -> b", "a -> c"],
            ),
            "has a named file to_c, the name of one of its pipes",
        ),
        (item({"a": entry(stops_others="yes")}), "stops_others of program a"),
        (item({"a": entry(time_limit=0.0001)}), "under a millisecond"),
        (item({"a": entry(memory_limit=0.0001)}), "under a kilobyte"),
        (item({"a": entry(time_limit=True)}), "is not a positive number"),
        (item({"a": entry(args=["x"])}), "args of program a of run main-1"),
        (item({"a": entry(source={"folder": "../p"})}), "is outside in/"),
        (item({"a": entry(language="go")}), "language of program a of run main-1"),
        (
            item({"a": entry(manifest={"file": "in/m"})}),
            "declares ended, a name exec gives already",
        ),
        (
            {
                "run": "main-1",
                "inputs": {
                    "source": {"folder": "in/p"},
                    "programs": {"programs": {"a": entry()}},
                },
            },
            "gives both source and programs",
        ),
    ],
)
def test_inputs_that_do_not_hold_are_refused(
    tmp_path: Path, given: dict[str, Any], fault: str
) -> None:
    (tmp_path / "in" / "p").mkdir(parents=True)
    (tmp_path / "in" / "f").write_text("")
    (tmp_path / "in" / "m").write_text(json.dumps({"reports": {"ended": {}}}))
    document = {"schema_version": 6, "batch": [given]}
    (tmp_path / "inputs.json").write_text(json.dumps(document))
    with pytest.raises(shared.PrimitiveError) as raised:
        inputs.read_inputs(tmp_path)
    assert fault in str(raised.value)


@pytest.mark.parametrize(
    ("more", "message"),
    [
        (
            {"time_limit": True},
            "the input named time_limit of program a of run main-1 is not a "
            "positive number",
        ),
        (
            {"stops_others": "yes"},
            "the input named stops_others of program a of run main-1 is not true "
            "or false",
        ),
        (
            {"files": {"files": {"a b": {"file": "in/p"}}}},
            "the input named files of program a of run main-1 names a file "
            "'a b', not a plain name",
        ),
        (
            {"source": {"folder": "../p"}},
            "the input named source of program a of run main-1 is outside in/",
        ),
    ],
)
def test_an_input_that_does_not_hold_is_named_with_its_program(
    tmp_path: Path, more: dict[str, Any], message: str
) -> None:
    (tmp_path / "in" / "p").mkdir(parents=True)
    document = {"schema_version": 6, "batch": [item({"a": entry(**more)})]}
    (tmp_path / "inputs.json").write_text(json.dumps(document))
    with pytest.raises(shared.PrimitiveError) as raised:
        inputs.read_inputs(tmp_path)
    assert str(raised.value) == message


def test_a_program_is_read_without_the_one_program_forms_own_inputs(
    tmp_path: Path,
) -> None:
    """`stdin`, `loads` and `self_reported` are the one-program form's alone,
    so a program of this form that gives them, whatever they hold, is read as
    without them."""
    (tmp_path / "in" / "p").mkdir(parents=True)
    given = entry(stdin={"file": "in/none"}, loads="x", self_reported="yes")
    document = {"schema_version": 6, "batch": [item({"a": given})]}
    (tmp_path / "inputs.json").write_text(json.dumps(document))
    (read,) = inputs.read_inputs(tmp_path)
    (program,) = read.programs
    assert read.form == "programs"
    assert program.stdin is None
    assert program.loads == {}
    assert not program.self_reported


def test_a_batch_that_mixes_the_two_forms_is_refused(tmp_path: Path) -> None:
    """One item of each form is refused before any box is made."""
    (tmp_path / "in" / "p").mkdir(parents=True)
    one = {
        "run": "main-1",
        "inputs": {
            "source": {"folder": "in/p"},
            "language": "python",
            "time_limit": 1,
            "memory_limit": 64,
        },
    }
    second = item({"a": entry()})
    second["run"] = "main-2"
    document = {"schema_version": 6, "batch": [one, second]}
    (tmp_path / "inputs.json").write_text(json.dumps(document))
    with pytest.raises(shared.PrimitiveError, match="mixes the one-program form"):
        programs.run_batch(tmp_path, inputs.read_inputs(tmp_path))


def test_a_batch_of_more_programs_than_box_users_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(box.BOX_USERS_VARIABLE, "60000-60001")
    monkeypatch.setenv(box.STEP_MEMORY_VARIABLE, STEP_MEMORY_MB)
    programs = {name: Spec(ENTRANT, "leave") for name in ("a", "b", "c")}
    batch_inputs(tmp_path / "work", [("main-1", programs, [])])
    assert main(["interact", str(tmp_path / "work")]) == 0
    result = json.loads((tmp_path / "work" / "outputs.json").read_text())
    assert "a run has 3 programs" in result["error"]


def test_pipe_names_follow_the_order_of_the_links() -> None:
    entries = tuple(
        inputs.Entry(
            name=name,
            source=inputs.Source(Path("p"), "python"),
            time_limit=1,
            memory_limit=1,
        )
        for name in ("a", "b", "c")
    )
    links = tuple(
        inputs.Link(*text.split(" -> "))
        for text in ["a -> b", "a -> c", "c -> a", "b -> a", "c -> b"]
    )
    named = inputs.pipe_names(inputs.Item("main-1", 1, entries, links))
    assert named == {"a": ["to_c", "from_b"], "b": ["from_c"], "c": ["to_b"]}


def test_a_signal_has_its_name() -> None:
    assert programs.signal_name(signal.SIGPIPE) == "SIGPIPE"
    assert programs.signal_name(200) == "signal 200"


MB = 1024 * 1024


@pytest.mark.parametrize(
    ("held", "given_mb", "needed_mb"),
    [
        (None, 256, 144),
        ("max\n", 256, 144),
        (f"{256 * MB}\n", 10**6, 144),
        (f"{10**6 * MB}\n", 256, 144),
    ],
)
def test_a_run_the_step_holds_is_let_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    held: str | None,
    given_mb: int,
    needed_mb: int,
) -> None:
    """With room in the step's memory, and in the container's when it has
    a limit, for the run's memory limits, its copies and the step's own, the
    run goes ahead."""
    memory = tmp_path / "memory.max"
    if held is not None:
        memory.write_text(held)
    monkeypatch.setattr(box, "CONTAINER_MEMORY", memory)
    box.check_memory(needed_mb * 1024, given_mb * 1024, "main-1")


@pytest.mark.parametrize(
    ("held", "given_mb"), [("max\n", 256), (f"{256 * MB}\n", 10**6)]
)
def test_a_run_the_step_cannot_hold_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held: str, given_mb: int
) -> None:
    """The step's memory, or the container's when that is less, holds too
    little for the run."""
    memory = tmp_path / "memory.max"
    memory.write_text(held)
    monkeypatch.setattr(box, "CONTAINER_MEMORY", memory)
    with pytest.raises(shared.PrimitiveError) as raised:
        box.check_memory(144 * 1024 + 1, given_mb * 1024, "main-1")
    assert str(raised.value) == (
        "the run main-1 needs 145 MB for its memory limits and the files copied "
        "in beside them, and the step's 256 MB leaves 144 MB for them"
    )


def test_a_run_whose_programs_together_do_not_fit_runs_nothing(
    boxes: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The programs run side by side, so their memory limits and copies are
    added up: two of 512 MB, each of which fits alone, do not fit together
    in what a step of 1136 MB leaves, and the batch is refused before any
    program runs, an error of the step, not a program the step's limit
    kills."""
    monkeypatch.setattr(box, "CONTAINER_MEMORY", tmp_path / "none")
    monkeypatch.setenv(box.BOX_USERS_VARIABLE, BOX_USERS)
    monkeypatch.setenv(box.STEP_MEMORY_VARIABLE, "1136")
    work = tmp_path / "work"
    played = game("guess", entrant_memory_limit=512, judge_memory_limit=512)
    batch_inputs(work, [("main-1", played, GAME_LINKS)])
    assert main(["interact", str(work)]) == 0
    result = json.loads((work / "outputs.json").read_text())
    assert result["error"].startswith("the run main-1 needs 1025 MB")
    assert not (work / "out").exists()


# Cached files: in the step's cache, bound into the box, never copied.


def cache_the_secret(work: Path) -> Path:
    """Move the judge's `secret` of the one item into an item folder of a
    step's cache elsewhere, which `cache` links to, as the step wrapper lays
    it out, and name it there; return the item folder."""
    item = work.parent / "cache" / "items" / "step-k2" / "Wq81"
    item.mkdir(parents=True)
    (work / "in" / "1" / "judge" / "files" / "secret").rename(item / "secret")
    (item / "secret").chmod(0o444)
    item.chmod(0o555)
    (work / "cache").symlink_to(item.parent)
    document = json.loads((work / "inputs.json").read_text())
    judge = document["batch"][0]["inputs"]["programs"]["programs"]["judge"]
    judge["files"] = {"files": {"secret": {"file": "cache/Wq81/secret"}}}
    (work / "inputs.json").write_text(json.dumps(document))
    return item


def test_a_cached_file_is_read_through_the_steps_cache_link(tmp_path: Path) -> None:
    work = tmp_path / "work"
    batch_inputs(work, [("main-1", game("guess"), GAME_LINKS)])
    item = cache_the_secret(work).resolve()
    read = inputs.read_inputs(work)[0]
    assert read.form == "programs"
    programs = read.programs
    judge = next(entry for entry in programs if entry.name == "judge")
    assert judge.files == {"secret": item / "secret"}
    assert judge.cached == item
    program = tmp_path / "program"
    program.write_bytes(b"x" * 5000)
    assert box.box_size_kb(judge, program) == judge.memory_limit_kb + 8


def test_cached_files_of_two_items_in_one_program_are_refused(tmp_path: Path) -> None:
    work = tmp_path / "work"
    spec = program("noop", files={"a": b"1", "b": b"2"})
    batch_inputs(work, [("main-1", {"solo": spec}, [])])
    step = tmp_path / "cache" / "items" / "step-k2"
    for name, folder in (("a", "Wq81"), ("b", "Zr3")):
        (step / folder).mkdir(parents=True)
        (step / folder / name).write_text(name)
    (work / "cache").symlink_to(step)
    document = json.loads((work / "inputs.json").read_text())
    solo = document["batch"][0]["inputs"]["programs"]["programs"]["solo"]
    solo["files"] = {
        "files": {
            "a": {"file": "cache/Wq81/a"},
            "b": {"file": "cache/Zr3/b"},
        }
    }
    (work / "inputs.json").write_text(json.dumps(document))
    with pytest.raises(shared.PrimitiveError) as raised:
        inputs.read_inputs(work)
    assert str(raised.value) == (
        "the program solo of run main-1 names cached files of more than one item"
    )


def test_a_judge_reads_a_cached_file_bound_into_its_box(
    boxes: None, tmp_path: Path
) -> None:
    work = tmp_path / "work"
    batch_inputs(work, [("main-1", game("guess"), GAME_LINKS)])
    item = cache_the_secret(work)
    assert main(["interact", str(work)]) == 0
    done = Batch(work, json.loads((work / "outputs.json").read_text()))
    assert "error" not in done.document, done.document
    assert done.outputs["main-1"]["outcome"] == "accepted"
    assert done.facts("main-1")["judge"]["verdict"] == "0"
    assert (item / "secret").read_bytes() == SECRET["secret"]


def cache_named_files(work: Path, name: str, item: str) -> Path:
    """Move every named file of program `name` of the one item into its own
    item folder `item` of a step's cache elsewhere, which `cache` links to,
    and name them there; return the item folder."""
    step = work.parent / "cache" / "items" / "step-k2"
    folder = step / item
    folder.mkdir(parents=True)
    if not (work / "cache").exists():
        (work / "cache").symlink_to(step)
    document = json.loads((work / "inputs.json").read_text())
    inputs = document["batch"][0]["inputs"]["programs"]["programs"][name]
    for file_name in inputs["files"]["files"]:
        (work / "in" / "1" / name / "files" / file_name).rename(folder / file_name)
        inputs["files"]["files"][file_name] = {"file": f"cache/{item}/{file_name}"}
    (work / "inputs.json").write_text(json.dumps(document))
    return folder


def test_an_item_folder_holding_another_programs_file_is_refused(
    tmp_path: Path,
) -> None:
    """Each program's cached files are in an item folder of its own, bound
    whole into its box: one that also holds a file of another program's
    would put that file in this one's box, so it is refused."""
    work = tmp_path / "work"
    batch_inputs(work, [("main-1", game("guess"), GAME_LINKS)])
    folder = cache_named_files(work, "judge", "Wq81")
    (folder / "entrants-own").write_text("not the judge's")
    with pytest.raises(shared.PrimitiveError) as raised:
        inputs.read_inputs(work)
    assert str(raised.value) == (
        "the program judge of run main-1 has an item folder in the cache that "
        "also holds 'entrants-own', a file not its own"
    )


def test_a_program_sees_only_its_own_cached_files(boxes: None, tmp_path: Path) -> None:
    """Two programs of one run, each with a cached file in an item folder of
    its own: each box's `/cache` holds its own file alone."""
    work = tmp_path / "work"
    programs = {
        "judge": program("list /cache", manifest=NOTES, files={"answer": b"7"}),
        "entrant": program("list /cache", manifest=NOTES, files={"input": b"1"}),
    }
    batch_inputs(work, [("main-1", programs, [])])
    cache_named_files(work, "judge", "Wq81")
    cache_named_files(work, "entrant", "Zr3")
    assert main(["interact", str(work)]) == 0
    done = Batch(work, json.loads((work / "outputs.json").read_text()))
    assert "error" not in done.document, done.document
    facts = done.facts("main-1")
    assert json.loads(facts["judge"]["message"]) == ["answer"]
    assert json.loads(facts["entrant"]["message"]) == ["input"]
