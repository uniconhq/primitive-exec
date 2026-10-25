"""The image, run on Docker as an exec step container is: privileged, as
root, through `userns.py` (see `conftest.py`).

Most Python binaries are built here as zip applications whose `__main__.py` is
the program itself; native and Java binaries, and one Python binary with
compile's launcher and `source/` folder, come from the compile primitive's own
image, so these tests are also the check that the two primitives agree on the
binary format.
"""

import errno
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from support import (
    BOX_USERS,
    NAME,
    PROGRAM,
    ROOT,
    SCORE,
    SIBLING_DIFF,
    Check,
    Compiled,
    RunImage,
    batch_inputs,
    by_run,
    container_limits,
    peek_input,
    peeked,
    printed,
    python_binary,
)

pytestmark = pytest.mark.image

CPP = """
#include <bits/stdc++.h>
int main(int argc, char** argv) {
    std::string c; std::cin >> c;
    if (c == "argv") for (int i = 1; i < argc; i++) std::cout << argv[i] << "\\n";
    if (c == "double") { long long n; std::cin >> n; std::cout << n * 2 << "\\n"; }
    if (c == "spin") { volatile unsigned long x = 0; for (;;) x = x + 1; }
    if (c == "grow") {
        std::vector<std::vector<char>> v;
        for (;;) v.emplace_back(8 << 20, 'x');
    }
    if (c == "crash") { std::vector<int> v; return v.at(5); }
    if (c == "peek") {
        std::string p;
        while (std::cin >> p) {
            FILE* f = std::fopen(p.c_str(), "rb");
            std::cout << (f ? "read " : "refused ") << p << "\\n";
            if (f) std::fclose(f);
        }
    }
}
"""

C = """
#include <stdio.h>
int main(void) {
    long long n;
    if (scanf("%lld", &n) != 1) return 1;
    printf("%lld\\n", n * 2);
    return 0;
}
"""

JAVA = """
import java.util.*;
public class Main {
    public static void main(String[] args) {
        Scanner in = new Scanner(System.in);
        String c = in.next();
        if (c.equals("argv")) for (String a : args) System.out.println(a);
        if (c.equals("double")) System.out.println(in.nextLong() * 2);
        if (c.equals("grow")) {
            List<long[]> keep = new ArrayList<>();
            while (true) keep.add(new long[1 << 20]);
        }
        if (c.equals("crash")) throw new IllegalStateException("no");
    }
}
"""


def run_batch(
    run_image: RunImage,
    work: Path,
    binary: bytes,
    items: list[tuple[str, str]],
    time_limit: float = 1.0,
    memory_limit: float = 64,
    args: dict[str, str] | None = None,
    files: dict[str, dict[str, bytes]] | None = None,
    loads: dict[str, dict[str, bytes]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Run one batch under the container limits the declaration gives it."""
    batch_inputs(
        work, binary, items, time_limit, memory_limit, args, files=files, loads=loads
    )
    limits = container_limits(time_limit, memory_limit, len(items))
    return by_run(run_image(work, limits))


def outcomes(outputs: dict[str, dict[str, Any]]) -> dict[str, object]:
    """Each item's outcome."""
    return {test: entry["outcome"] for test, entry in outputs.items()}


def test_python_runs_reach_every_outcome(
    tmp_path: Path, run_image: RunImage, check: Check
) -> None:
    """One batch in boxes reaches each outcome, keeps each output and
    reports the time and memory of each, the time at least the limit for a
    run stopped by the CPU limit or by the wall clock.
    """
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    items = [
        ("samples-ok", "double\n21\n"),
        ("main-spin", "spin\n"),
        ("main-sleep", "sleep\n"),
        ("main-grow", "grow\n"),
        ("main-fail", "fail\n"),
    ]
    batch_inputs(work, binary, items, 0.5, 64)
    check(json.loads((work / "inputs.json").read_text()), "inputs_file")
    result = run_image(work, container_limits(0.5, 64, len(items)))
    check(result, "outputs_file")
    outputs = by_run(result)
    assert outcomes(outputs) == {
        "samples-ok": "accepted",
        "main-spin": "time_limit",
        "main-sleep": "time_limit",
        "main-grow": "memory_limit",
        "main-fail": "runtime_error",
    }
    assert printed(work, outputs["samples-ok"]) == "42\n"
    assert printed(work, outputs["main-fail"]) == "partial\n"
    assert outputs["main-spin"]["time_ms"] >= 500
    assert outputs["main-sleep"]["time_ms"] == 500
    assert all(entry["memory_kb"] > 0 for entry in outputs.values())
    assert sorted(path.name for path in work.iterdir()) == [
        "in",
        "inputs.json",
        "out",
        "outputs.json",
    ]


def test_a_batch_stopped_by_the_wall_clock_fits_its_time(
    tmp_path: Path, run_image: RunImage
) -> None:
    """A batch whose every run sleeps until the wall clock stops it, the
    longest a test can take, ends within the time the declaration gives the
    batch, counted from `docker run` to the container's exit, so a contestant
    cannot make the harness kill the container.
    """
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    items = [(f"main-sleep-{number}", "sleep\n") for number in range(4)]
    batch_inputs(work, binary, items, 1.0, 64)
    limits = container_limits(1.0, 64, len(items))
    seconds: list[float] = []
    outputs = by_run(run_image(work, limits, seconds))
    assert {entry["outcome"] for entry in outputs.values()} == {"time_limit"}
    assert {entry["time_ms"] for entry in outputs.values()} == {1000}
    assert seconds[0] < limits["time_ms"] / 1000


def test_output_over_the_limit_is_cut(tmp_path: Path, run_image: RunImage) -> None:
    """Printing past the output limit is `output_limit`, with the output cut there."""
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    items = [("main-flood", "flood\n")]
    outputs = run_batch(run_image, work, binary, items, time_limit=10)
    assert outcomes(outputs) == {"main-flood": "output_limit"}
    assert (work / "out" / "1" / "output").stat().st_size == 32 * 1024 * 1024


def test_runs_are_kept_apart(tmp_path: Path, run_image: RunImage) -> None:
    """A run cannot name `/work`, writes only its own box, sees only its own
    processes, and nothing it leaves reaches the next."""
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    items = [
        ("main-escape", "escape\n/work\n"),
        ("main-linger", "linger\n"),
        ("main-census", "census\n"),
    ]
    started = time.monotonic()
    outputs = run_batch(run_image, work, binary, items)
    assert time.monotonic() - started < 30
    assert outcomes(outputs) == {
        "main-escape": "accepted",
        "main-linger": "accepted",
        "main-census": "accepted",
    }
    assert printed(work, outputs["main-escape"]).splitlines() == [
        "refused /work/escaped 2",
        "refused /work/inputs.json 2",
        "wrote /tmp/escaped",
        "cwd ['here']",
    ]
    assert printed(work, outputs["main-census"]) == "1\n"


def test_a_run_that_takes_away_its_files_modes_faults_nothing(
    tmp_path: Path, run_image: RunImage
) -> None:
    """A run cannot name the working directory, `in/` or `out/` to change
    their modes, nor change the mode of its standard output, a pipe of
    exec's, so the next run and the harness still read and write them.
    """
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    items = [("main-chmod", "chmod\n/work\n"), ("main-next", "double\n21\n")]
    outputs = run_batch(run_image, work, binary, items)
    assert outcomes(outputs) == {"main-chmod": "accepted", "main-next": "accepted"}
    assert printed(work, outputs["main-chmod"]) == "changed\n"
    assert printed(work, outputs["main-next"]) == "42\n"


def test_a_run_that_leaves_a_path_past_path_max_faults_nothing(
    tmp_path: Path, run_image: RunImage
) -> None:
    """A run may build folders deeper than any path can name; what it left is
    cleared as far as it can be, and the next run is judged as ever."""
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    items = [("main-deep", "deep\n"), ("main-next", "double\n21\n")]
    outputs = run_batch(run_image, work, binary, items)
    assert outcomes(outputs) == {"main-deep": "accepted", "main-next": "accepted"}
    assert printed(work, outputs["main-next"]) == "42\n"


def test_a_run_reads_nothing_another_run_left(
    tmp_path: Path, run_image: RunImage
) -> None:
    """In a box, a later run cannot name an earlier run's output or its
    input, the step's own files, or /work; its own /tmp it lists, and the
    system's files it reads.
    """
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    items = [("main-first", "double\n21\n"), ("main-peek", peek_input("/work"))]
    outputs = run_batch(run_image, work, binary, items)
    assert outcomes(outputs) == {"main-first": "accepted", "main-peek": "accepted"}
    assert printed(work, outputs["main-first"]) == "42\n"
    assert printed(work, outputs["main-peek"]).splitlines() == peeked("/work")


@pytest.mark.parametrize("kind", ["shm", "msg", "sem"])
def test_a_run_finds_no_system_v_object_an_earlier_run_left(
    tmp_path: Path, run_image: RunImage, kind: str
) -> None:
    """A shared memory segment, a message queue or a semaphore set one run
    makes goes with its box's IPC namespace, before the next, which could
    otherwise read in it what the first was given.
    """
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    items = [
        ("main-leave", f"leave-sysv\n{kind} 1\n"),
        ("main-find", f"find-sysv\n{kind}\n"),
    ]
    outputs = run_batch(run_image, work, binary, items)
    assert outcomes(outputs) == {"main-leave": "accepted", "main-find": "accepted"}
    assert printed(work, outputs["main-leave"]) == f"made {kind} 1\n"
    assert printed(work, outputs["main-find"]) == f"found {kind} 0\n"


def test_a_run_that_leaves_many_segments_leaves_none(
    tmp_path: Path, run_image: RunImage
) -> None:
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    items = [("main-leave", "leave-sysv\nshm 100\n"), ("main-find", "find-sysv\nshm\n")]
    outputs = run_batch(run_image, work, binary, items)
    assert printed(work, outputs["main-leave"]) == "made shm 100\n"
    assert printed(work, outputs["main-find"]) == "found shm 0\n"


def test_a_run_finds_no_mark_an_earlier_run_set_on_the_binary(
    tmp_path: Path, run_image: RunImage
) -> None:
    """Each item runs a copy of the binary of its own, so an attribute or a
    time one run sets on it is gone for the next."""
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    items = [("main-mark", "mark\n"), ("main-read", "read-mark\n")]
    outputs = run_batch(run_image, work, binary, items)
    assert printed(work, outputs["main-mark"]) == "marked\n"
    assert printed(work, outputs["main-read"]) == "no attribute\ntime fresh\n"


def test_a_run_finds_nothing_an_earlier_run_left_in_its_box(
    tmp_path: Path, run_image: RunImage
) -> None:
    """Files a run leaves in its working directory, its `/tmp` and
    `/dev/shm` go with its box: the next finds a box of only its own program,
    working directory and empty `/tmp` and `/dev/shm`."""
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    items = [("main-leave", "leave-files\n"), ("main-find", "find-files\n")]
    outputs = run_batch(run_image, work, binary, items)
    assert printed(work, outputs["main-leave"]) == "left\n"
    assert printed(work, outputs["main-find"]).splitlines() == [
        "left False",
        "/tmp/left False",
        "/dev/shm/left False",
        "box ['program', 'run', 'shm', 'tmp']",
    ]


def test_a_run_that_buries_files_past_path_max_leaves_the_next_its_room(
    tmp_path: Path, run_image: RunImage
) -> None:
    """A run that writes 20 MB in a folder deeper than any path can name, and
    takes the access to it away, does not keep that room from the next: each
    later run finds as much of its box free as the first did."""
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    items = [
        ("main-1", "free\n"),
        ("main-2", "bury\n20\n"),
        ("main-3", "free\n"),
        ("main-4", "bury\n20\n"),
        ("main-5", "free\n"),
    ]
    outputs = run_batch(run_image, work, binary, items)
    assert set(outcomes(outputs).values()) == {"accepted"}
    free = [int(printed(work, outputs[f"main-{n}"]).split()[1]) for n in (1, 3, 5)]
    assert free[0] - min(free) <= 2, free


def test_a_run_that_makes_too_many_folders_is_over_its_memory(
    tmp_path: Path, run_image: RunImage
) -> None:
    """A box's tmpfs holds 100,000 files and folders, so a run that makes
    more fails with `ENOSPC`, and exec, finding none free after it, gives
    `memory_limit`, though the run is far under its memory limit; one that
    makes fewer does not."""
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    items = [("main-many", "folders\n150000\n"), ("main-few", "folders\n1000\n")]
    outputs = run_batch(run_image, work, binary, items, 4.0, 512)
    assert outcomes(outputs) == {"main-many": "memory_limit", "main-few": "accepted"}


def test_a_run_that_locks_its_own_copy_does_not_stop_the_next(
    tmp_path: Path, run_image: RunImage
) -> None:
    """A run can take the access away from its copy of the binary and the
    box folder it is in, though not from the binary under `in/`, which it
    cannot name; the next item still gets a box and a copy of its own, so no
    run can end the batch in an error."""
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    items = [("main-lock", "lock-scratch\n"), ("main-next", "double\n21\n")]
    outputs = run_batch(run_image, work, binary, items)
    assert outcomes(outputs) == {"main-lock": "accepted", "main-next": "accepted"}
    assert printed(work, outputs["main-lock"]) == "locked\n"
    assert printed(work, outputs["main-next"]) == "42\n"


def test_a_run_finds_no_posix_message_queue_an_earlier_run_left(
    tmp_path: Path, run_image: RunImage
) -> None:
    """A POSIX queue belongs to the box's IPC namespace: a run may make one,
    and the next run finds none."""
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    items = [("main-leave", "leave-mq\n"), ("main-find", "find-mq\n")]
    outputs = run_batch(run_image, work, binary, items)
    assert outcomes(outputs) == {"main-leave": "accepted", "main-find": "accepted"}
    assert printed(work, outputs["main-leave"]) == "made 0\n"
    assert printed(work, outputs["main-find"]) == f"none {errno.ENOENT}\n"


def test_named_files_are_placed_read_only_in_a_box(
    tmp_path: Path, run_image: RunImage, check: Check
) -> None:
    """In a box, as the harness lays `in/` out: the program sees exactly
    its named files and its loads, by name, and cannot open one to write."""
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    files = {"main-1": {"answer": b"42\n"}}
    loads = {"main-1": {"solution.py": b"def answer():\n    return 7\n"}}
    items = [("main-1", "named\nanswer\n"), ("main-2", "load\n")]
    batch_inputs(
        work, binary, items, files=files, loads=loads | {"main-2": loads["main-1"]}
    )
    check(json.loads((work / "inputs.json").read_text()), "inputs_file")
    result = run_image(work, container_limits(1.0, 64, len(items)))
    check(result, "outputs_file")
    outputs = by_run(result)
    assert printed(work, outputs["main-1"]).splitlines() == [
        "answer 0o444 3 42",
        "solution.py 0o444 27 def answer():",
        f"refused answer {errno.EACCES}",
    ]
    assert printed(work, outputs["main-2"]) == "7\n"


def test_a_large_named_file_costs_the_run_no_time(
    tmp_path: Path, run_image: RunImage
) -> None:
    """A 64 MB named file is copied before the run's clock starts, so a
    trivial program reports no more time with it than without."""
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    large = {f"main-{n}": {"data": b"x" * (64 * 1024 * 1024)} for n in range(3)}
    items = [(f"main-{n}", "double\n21\n") for n in range(3)]
    without = run_batch(
        run_image, tmp_path / "without", binary, items, memory_limit=256
    )
    with_file = run_batch(
        run_image, tmp_path / "with", binary, items, memory_limit=256, files=large
    )
    assert outcomes(with_file) == dict.fromkeys(large, "accepted")
    plain = sorted(entry["time_ms"] for entry in without.values())
    heavy = sorted(entry["time_ms"] for entry in with_file.values())
    assert heavy[1] <= plain[1] + 30, (plain, heavy)


def test_named_files_the_container_cannot_hold_are_an_error_of_the_step(
    tmp_path: Path, run_image: RunImage
) -> None:
    """A run's named files are copied into its box's tmpfs, charged to the
    container beside the box's own memory: 96 MB of them with a 64 MB
    limit do not fit a 192 MB container, and exec says so before any run,
    where the container's limit would kill it with no outputs at all.
    Without the files, the same container runs the batch."""
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    limits = container_limits(1.0, 64, 1) | {"memory_mb": 192}
    items = [("main-1", "double\n21\n")]
    files = {"main-1": {"data": b"x" * (96 * 1024 * 1024)}}
    batch_inputs(tmp_path / "large", binary, items, files=files)
    refused = run_image(tmp_path / "large", limits)
    assert refused["error"].startswith("the run main-1 needs 161 MB")
    assert "container's 192 MB leaves 128 MB" in refused["error"]
    batch_inputs(tmp_path / "small", binary, items)
    ran = by_run(run_image(tmp_path / "small", limits))
    assert outcomes(ran) == {"main-1": "accepted"}


def test_a_run_reaches_no_network_no_io_uring_and_no_keyring(
    tmp_path: Path, run_image: RunImage
) -> None:
    """A box has a network namespace of its own, so a connection out fails
    though the step container has Docker's network, as a Woodpecker step
    does; a Unix socket, which Java and Python use, works. isolate's filter
    refuses io_uring and `keyctl`, and our patch to it `add_key` and
    `request_key`, with ENOSYS, so a run leaves no key for a later run."""
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    items = [("main-1", "sockets\n"), ("main-2", "find-key\n")]
    outputs = run_batch(run_image, work, binary, items)
    assert outcomes(outputs) == {"main-1": "accepted", "main-2": "accepted"}
    # Linux's ENETUNREACH and ENOSYS, which the host running the tests may
    # number otherwise.
    assert printed(work, outputs["main-1"]).splitlines() == [
        "connect AF_INET 101",
        "connect AF_INET6 101",
        "socketpair AF_UNIX True",
        "io_uring_setup 38",
        "add_key 38",
        "request_key 38",
        "keyctl 38",
    ]
    assert printed(work, outputs["main-2"]).splitlines() == [
        "request_key 38",
        "listed False",
    ]


def test_a_fork_bomb_stops_at_the_process_limit_without_a_pids_limit(
    tmp_path: Path, run_image: RunImage
) -> None:
    """Under a container pids limit far above `pids`, as a Woodpecker step
    has, a run still stops at 128 processes of its box user, and the next
    run starts, its leftovers gone with its box."""
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    items = [("main-bomb", "fork-bomb\n"), ("main-next", "double\n21\n")]
    batch_inputs(work, binary, items)
    limits = container_limits(1.0, 64, len(items)) | {"pids": 4096}
    outputs = by_run(run_image(work, limits))
    assert outcomes(outputs) == {"main-bomb": "accepted", "main-next": "accepted"}
    # Linux's EAGAIN, which the host running the tests may number otherwise.
    assert printed(work, outputs["main-bomb"]) == "stopped 11 True\n"
    assert printed(work, outputs["main-next"]) == "42\n"


def test_a_hostile_run_cannot_reach_the_program_that_watches_it(
    tmp_path: Path, run_image: RunImage
) -> None:
    """In a box: killing its parent, isolate's, is refused and the run's own
    runtime error, work and memory hidden in a child still count, isolate's
    descriptors and memory cannot even be named, and SIGINT to the box's first
    process does nothing.
    """
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    items = [
        ("main-parricide", "parricide\n"),
        ("main-hidden-work", "hidden-work\n"),
        ("main-hidden-memory", "hidden-memory\n"),
        ("main-prying", "prying\n"),
        ("main-interrupt", "interrupt\n"),
        ("main-ok", "double\n21\n"),
    ]
    outputs = run_batch(run_image, work, binary, items, time_limit=0.5)
    assert outcomes(outputs) == {
        "main-parricide": "runtime_error",
        "main-hidden-work": "time_limit",
        "main-hidden-memory": "memory_limit",
        "main-prying": "accepted",
        "main-interrupt": "accepted",
        "main-ok": "accepted",
    }
    assert printed(work, outputs["main-parricide"]).splitlines() in (
        [f"refused {errno.EPERM}"],
        ["after"],
        [],
    )
    assert printed(work, outputs["main-prying"]).splitlines() == [
        f"refused {errno.ENOENT}",
        f"refused {errno.ENOENT}",
    ]
    interrupted = printed(work, outputs["main-interrupt"]).splitlines()
    assert interrupted[-1] == "still here"
    assert printed(work, outputs["main-ok"]) == "42\n"


def test_memory_is_the_whole_runs(tmp_path: Path, run_image: RunImage) -> None:
    """In a box, two processes holding 40 MB each are over a 64 MB limit
    together, also when both make themselves not dumpable so their pages
    cannot be read, and a child forked from a binary holding 40 MB is not
    charged again for the pages it shares with it.
    """
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    items = [
        ("main-split", "split-memory\n"),
        ("main-undumpable", "undumpable-split-memory\n"),
        ("main-alone", "hold-memory\n"),
        ("main-shared", "shared-memory\n"),
    ]
    outputs = run_batch(run_image, work, binary, items, time_limit=3)
    assert outcomes(outputs) == {
        "main-split": "memory_limit",
        "main-undumpable": "memory_limit",
        "main-alone": "accepted",
        "main-shared": "accepted",
    }
    alone = outputs["main-alone"]["memory_kb"]
    shared = outputs["main-shared"]["memory_kb"]
    assert isinstance(alone, int) and isinstance(shared, int)
    assert shared < alone + 4 * 1024


def test_memory_kept_in_files_counts(tmp_path: Path, run_image: RunImage) -> None:
    """In a box, memory a run keeps outside its processes is memory:
    24 MB held and 48 MB more written to files in its own directory, to files
    it deleted and keeps open, to memory files with no path at all, or left
    in pipes or sockets, is over a 64 MB limit, while 40 MB held beside a
    small file is not.
    """
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    items = [
        ("main-file", "file-memory\n"),
        ("main-unlinked", "unlinked-memory\n"),
        ("main-memfd", "memfd-memory\n"),
        ("main-pipe", "pipe-memory\n"),
        ("main-socket", "socket-memory\n"),
        ("main-small", "small-file\n"),
        ("main-alone", "hold-memory\n"),
    ]
    outputs = run_batch(run_image, work, binary, items, time_limit=3)
    assert outcomes(outputs) == {
        "main-file": "memory_limit",
        "main-unlinked": "memory_limit",
        "main-memfd": "memory_limit",
        "main-pipe": "memory_limit",
        "main-socket": "memory_limit",
        "main-small": "accepted",
        "main-alone": "accepted",
    }
    small = outputs["main-small"]["memory_kb"]
    alone = outputs["main-alone"]["memory_kb"]
    assert isinstance(small, int) and isinstance(alone, int)
    assert alone <= small < alone + 4 * 1024


def test_native_binaries_from_compile(
    tmp_path: Path, run_image: RunImage, compiled: Compiled, check: Check
) -> None:
    """A C++ binary from compile runs, is measured by its own size, hits
    limits, and gets its arguments.
    """
    binary = compiled("cpp", "main.cpp", CPP)
    work = tmp_path / "work"
    items = [
        ("main-ok", "double 21"),
        ("main-spin", "spin"),
        ("main-grow", "grow"),
        ("main-crash", "crash"),
        ("main-argv", "argv"),
    ]
    batch_inputs(work, binary, items, 1.0, 64, {"main-argv": "--seed 7"})
    check(json.loads((work / "inputs.json").read_text()), "inputs_file")
    result = run_image(work, container_limits(1.0, 64, len(items)))
    check(result, "outputs_file")
    outputs = by_run(result)
    assert outcomes(outputs) == {
        "main-ok": "accepted",
        "main-spin": "time_limit",
        "main-grow": "memory_limit",
        "main-crash": "runtime_error",
        "main-argv": "accepted",
    }
    assert printed(work, outputs["main-ok"]) == "42\n"
    assert isinstance(outputs["main-ok"]["memory_kb"], int)
    assert outputs["main-ok"]["memory_kb"] < 8 * 1024
    assert outputs["main-spin"]["time_ms"] >= 1000
    assert printed(work, outputs["main-argv"]) == "--seed\n7\n"


def test_a_native_run_reads_nothing_another_run_left(
    tmp_path: Path, run_image: RunImage, compiled: Compiled
) -> None:
    """The same confinement holds for a native binary: it can read its own
    input on standard input and the system's files, and no other test's.
    """
    binary = compiled("cpp", "main.cpp", CPP)
    work = tmp_path / "work"
    paths = [
        "/work/out/1/output",
        "/work/in/2/input",
        "/work/inputs.json",
        "/etc/resolv.conf",
        "/usr/lib/os-release",
    ]
    items = [("main-first", "double 21"), ("main-peek", " ".join(["peek", *paths]))]
    outputs = run_batch(run_image, work, binary, items)
    assert outcomes(outputs) == {"main-first": "accepted", "main-peek": "accepted"}
    lines = printed(work, outputs["main-peek"]).splitlines()
    assert lines == [
        "refused /work/out/1/output",
        "refused /work/in/2/input",
        "refused /work/inputs.json",
        "refused /etc/resolv.conf",
        "read /usr/lib/os-release",
    ]


def test_c_binaries_from_compile(
    tmp_path: Path, run_image: RunImage, compiled: Compiled
) -> None:
    """A C binary from compile runs in a box."""
    binary = compiled("c", "main.c", C)
    work = tmp_path / "work"
    items = [("main-1", "21\n"), ("main-2", "-4\n")]
    outputs = run_batch(run_image, work, binary, items)
    assert outcomes(outputs) == {"main-1": "accepted", "main-2": "accepted"}
    assert printed(work, outputs["main-1"]) == "42\n"
    assert printed(work, outputs["main-2"]) == "-8\n"


def test_java_binaries_from_compile(
    tmp_path: Path, run_image: RunImage, compiled: Compiled
) -> None:
    """A jar from compile runs on the image's Java and gets its arguments;
    running out of heap is memory.
    """
    binary = compiled("java", "Main.java", JAVA)
    work = tmp_path / "work"
    items = [
        ("main-ok", "double 21"),
        ("main-grow", "grow"),
        ("main-crash", "crash"),
        ("main-argv", "argv"),
    ]
    args = {"main-argv": "--threshold 0.5"}
    outputs = run_batch(run_image, work, binary, items, 3, 128, args)
    assert outcomes(outputs) == {
        "main-ok": "accepted",
        "main-grow": "memory_limit",
        "main-crash": "runtime_error",
        "main-argv": "accepted",
    }
    assert printed(work, outputs["main-ok"]) == "42\n"
    assert printed(work, outputs["main-argv"]) == "--threshold\n0.5\n"


def test_python_binaries_from_compile(
    tmp_path: Path, run_image: RunImage, compiled: Compiled
) -> None:
    """A zip application from compile runs on the image's Python and gets
    its arguments.
    """
    source = "import sys\nprint(int(input()) * 2, *sys.argv[1:])\n"
    binary = compiled("python", "main.py", source)
    work = tmp_path / "work"
    items = [("main-1", "21\n"), ("main-2", "-4\n")]
    outputs = run_batch(run_image, work, binary, items, args={"main-2": "a  b"})
    assert outcomes(outputs) == {"main-1": "accepted", "main-2": "accepted"}
    assert printed(work, outputs["main-1"]) == "42\n"
    assert printed(work, outputs["main-2"]) == "-8 a b\n"


def test_python_args_in_a_box(tmp_path: Path, run_image: RunImage) -> None:
    """`args` reaches a Python binary's command line split on spaces, each
    item its own, and a binary without them gets none.
    """
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    items = [("main-1", "argv\n"), ("main-2", "argv\n")]
    args = {"main-1": "--threshold 0.5 --heuristic true"}
    outputs = run_batch(run_image, work, binary, items, args=args)
    assert outcomes(outputs) == {"main-1": "accepted", "main-2": "accepted"}
    expected = ["--threshold", "0.5", "--heuristic", "true"]
    assert printed(work, outputs["main-1"]) == f"{expected!r}\n"
    assert printed(work, outputs["main-2"]) == "[]\n"


C_CHECKER = """
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static void put(const char *name, const char *value) {
    char path[4096];
    snprintf(path, sizeof path, "%s/%s", getenv("UNICON_REPORT"), name);
    FILE *f = fopen(path, "w");
    if (!f || fputs(value, f) < 0 || fclose(f) != 0) exit(2);
}
static long slurp(const char *name, char *buffer, long size) {
    FILE *f = fopen(name, "rb");
    if (!f) exit(3);
    long got = (long) fread(buffer, 1, size, f);
    fclose(f);
    return got;
}
int main(void) {
    static char a[4096], b[4096];
    long x = slurp("output", a, sizeof a), y = slurp("answer", b, sizeof b);
    int same = x == y && memcmp(a, b, x) == 0;
    put("verdict", same ? "0" : "1");
    put("score", same ? "1" : "0");
    put("label", same ? "same" : "differs");
    put("message", "c judged");
    put("staff_message", "compared bytes");
    return 0;
}
"""

CPP_CHECKER = """
#include <bits/stdc++.h>
static std::string slurp(const char *name) {
    std::ifstream in(name, std::ios::binary);
    if (!in) std::exit(3);
    return std::string(std::istreambuf_iterator<char>(in), {});
}
static void put(const std::string &name, const std::string &value) {
    std::ofstream out(std::string(std::getenv("UNICON_REPORT")) + "/" + name);
    out << value;
    if (!out) std::exit(2);
}
int main() {
    bool same = slurp("output") == slurp("answer");
    put("verdict", same ? "0" : "1");
    put("score", same ? "1" : "0");
    put("label", same ? "same" : "differs");
    put("message", "cpp judged");
    put("staff_message", "compared bytes");
}
"""

JAVA_CHECKER = """
import java.nio.file.*;
import java.util.Arrays;
public class Main {
    static void put(String name, String value) throws Exception {
        Files.writeString(Path.of(System.getenv("UNICON_REPORT"), name), value);
    }
    public static void main(String[] args) throws Exception {
        byte[] output = Files.readAllBytes(Path.of("output"));
        boolean same = Arrays.equals(output, Files.readAllBytes(Path.of("answer")));
        put("verdict", same ? "0" : "1");
        put("score", same ? "1" : "0");
        put("label", same ? "same" : "differs");
        put("message", "java judged");
        put("staff_message", "compared bytes");
    }
}
"""

PYTHON_CHECKER = """
import os

def put(name, value):
    with open(os.path.join(os.environ["UNICON_REPORT"], name), "w") as stream:
        print(value, file=stream)

same = open("output", "rb").read() == open("answer", "rb").read()
put("verdict", 0 if same else 1)
put("score", 1 if same else 0)
put("label", "same" if same else "differs")
put("message", "python judged")
put("staff_message", "compared bytes")
"""


@pytest.mark.parametrize(
    ("language", "name", "source"),
    [
        ("c", "main.c", C_CHECKER),
        ("cpp", "main.cpp", CPP_CHECKER),
        ("java", "Main.java", JAVA_CHECKER),
        ("python", "main.py", PYTHON_CHECKER),
    ],
)
def test_a_checker_in_each_language_writes_a_full_report(
    tmp_path: Path,
    run_image: RunImage,
    compiled: Compiled,
    check: Check,
    language: str,
    name: str,
    source: str,
) -> None:
    """A checker built by compile, given a manifest, reports through its
    folder from any language that writes a file: its reports are outputs on
    `0`, and on `1` they are shown in its extras."""
    binary = compiled(language, name, source)
    work = tmp_path / "work"
    files = {
        "main-same": {"output": b"42\n", "answer": b"42\n"},
        "main-differs": {"output": b"41\n", "answer": b"42\n"},
    }
    items = [("main-same", ""), ("main-differs", "")]
    batch_inputs(work, binary, items, 2.0, 256, files=files, manifest=SCORE)
    check(json.loads((work / "inputs.json").read_text()), "inputs_file")
    result = run_image(work, container_limits(2.0, 256, len(items)))
    check(result, "outputs_file", frozenset({"score"}))
    outputs = by_run(result)
    same, differs = outputs["main-same"], outputs["main-differs"]
    assert same["outcome"] == "accepted"
    assert same["score"] == 1
    assert (same["label"], same["message"]) == ("same", f"{language} judged")
    assert same["staff_message"] == "compared bytes"
    assert "extras" not in same
    assert differs["outcome"] == "wrong_answer"
    assert "score" not in differs
    assert differs["label"] == "differs"
    extras = work / differs["extras"]["folder"]
    assert (extras / "score").read_text().strip() == "0"


DIFF_CASES = [
    (b"1 2 3\n", b"1 2 3\n", True),
    (b"1 2 3", b"1 2 3\n", True),
    (b"1 2 3   \t\n", b"1 2 3\n", True),
    (b"1 2 3\r\n4\r\n", b"1 2 3\n4\n", True),
    (b"yes\n\n\n", b"yes\n", True),
    (b"yes\n", b"yes\n\n  \n", True),
    (b"", b"\n\n", True),
    (b"", b"", True),
    (b" 1 2 3\n", b"1 2 3\n", True),
    (b"1  2 3\n", b"1 2 3\n", True),
    (b"a\n\nb\n", b"a\nb\n", True),
    (b"\n\nyes\n", b"yes\n", True),
    (b"1\n2\n3\n", b"1 2 3\n", True),
    (b"1 2 3\n", b"1 2 4\n", False),
    (b"a\n", b"a\nb\n", False),
    (b"", b"0\n", False),
    (b"12 3\n", b"1 23\n", False),
]
"""diff-check@v2's cases and two more, each an output, an answer and whether
they match by words, as classic@v3 compares: leading whitespace, inner
spacing and blank lines, which counted for diff-check, count no more."""


def diff_script() -> tuple[str, dict[str, Any]]:
    """The diff script of unicon/classic@v3 and its manifest in JSON, from
    `DIFF_SCRIPT` or a deploy checkout beside this one."""
    folder = Path(os.environ.get("DIFF_SCRIPT", SIBLING_DIFF))
    if not (folder / "diff.py").is_file():
        pytest.skip("no diff script; set DIFF_SCRIPT to its folder")
    manifest: dict[str, Any] = yaml.safe_load((folder / "program.yaml").read_text())
    return (folder / "diff.py").read_text(), manifest


def test_the_diff_script_compares_words(
    tmp_path: Path, run_image: RunImage, compiled: Compiled, check: Check
) -> None:
    """Run through exec in manifest mode, the script gives each case
    accepted with a score of 1 when the words match, and wrong_answer with
    no score when they do not."""
    source, manifest = diff_script()
    binary = compiled("python", "diff.py", source)
    work = tmp_path / "work"
    items = [(f"case-{n}", "") for n in range(len(DIFF_CASES))]
    files = {
        run: {"output": output, "answer": answer}
        for (run, _), (output, answer, _) in zip(items, DIFF_CASES, strict=True)
    }
    batch_inputs(work, binary, items, 2.0, 64, files=files, manifest=manifest)
    result = run_image(work, container_limits(2.0, 64, len(items)))
    check(result, "outputs_file", frozenset({"score"}))
    outputs = by_run(result)
    for (run, _), (_, _, matches) in zip(items, DIFF_CASES, strict=True):
        if matches:
            assert (outputs[run]["outcome"], outputs[run]["score"]) == ("accepted", 1)
        else:
            assert outputs[run]["outcome"] == "wrong_answer"
            assert "score" not in outputs[run]


def test_the_diff_script_compares_the_largest_output_in_little_memory(
    tmp_path: Path, run_image: RunImage, compiled: Compiled
) -> None:
    """A 16 MB output against a 24 MB answer, both in the run's tmpfs, is
    read in blocks: the script holds a few megabytes, as diff-check did."""
    source, manifest = diff_script()
    binary = compiled("python", "diff.py", source)
    work = tmp_path / "work"
    large = b"1\n" * (8 * 1024 * 1024)
    files = {"case-1": {"output": large, "answer": large.replace(b"\n", b" \n")}}
    batch_inputs(
        work, binary, [("case-1", "")], 4.0, 64, files=files, manifest=manifest
    )
    limits = container_limits(4.0, 64, 1) | {"memory_mb": 512}
    outputs = by_run(run_image(work, limits))["case-1"]
    assert (outputs["outcome"], outputs["score"]) == ("accepted", 1)
    assert outputs["memory_kb"] < 32 * 1024


def test_on_root_docker_the_image_refuses_to_start(
    tmp_path: Path, run_image: RunImage
) -> None:
    """Started as its entrypoint is, privileged under root Docker, whose user
    ID map is the identity, the step would be the machine's own root: exec
    runs nothing and says why."""
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    batch_inputs(work, binary, [("main-1", "double\n21\n")])
    result = run_image(work, container_limits(1.0, 64, 1), root_docker=True)
    assert set(result) == {"schema_version", "error"}
    assert "machine's own root" in result["error"]
    assert not (work / "out").exists()


LOW_OPEN_FILES = ["--ulimit=nofile=1024:524288", "--ulimit=stack=8388608:-1"]
"""Docker Desktop's stack and a low soft limit on open files, under which
isolate, giving a deep tree back, ran out of both before exec raised them."""


def test_a_tree_as_deep_as_the_box_holds_ends_only_its_own_run(
    tmp_path: Path, run_image: RunImage
) -> None:
    """A run nesting folders until its box has no inode left, about 99,990
    deep, under a soft limit of 1,024 open files and an 8 MB stack: isolate,
    which gives the tree back with a folder open at each level, gets the
    limits exec raised, so the run ends `memory_limit` and the batch goes
    on, rather than isolate failing and the whole batch with it."""
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    items = [("main-deep", "nest\n"), ("main-next", "folders\n10\n")]
    batch_inputs(work, binary, items, time_limit=20, memory_limit=512)
    limits = container_limits(20, 512, len(items))
    outputs = by_run(run_image(work, limits, flags=LOW_OPEN_FILES))
    assert printed(work, outputs["main-deep"]) == "nested 28 True\n"
    assert outcomes(outputs) == {"main-deep": "memory_limit", "main-next": "accepted"}


def test_a_hard_limit_on_open_files_too_low_for_isolate_stops_the_step(
    tmp_path: Path, run_image: RunImage
) -> None:
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    batch_inputs(work, binary, [("main-1", "folders\n10\n")])
    limits = container_limits(1.0, 64, 1)
    result = run_image(work, limits, flags=["--ulimit=nofile=1024:1024"])
    assert set(result) == {"schema_version", "error"}
    assert "hard limit on open files, 1024" in result["error"]


def test_the_in_process_tests_pass_in_the_test_container(image: str) -> None:
    """`test_exec.py` in the test container built on the image, privileged,
    as root through `userns.py`, where every test that runs programs makes
    its boxes as a step does and none is skipped."""
    tag = f"{NAME}:test-container"
    build = ["docker", "build", "--quiet", "--tag", tag, "--build-arg"]
    build += [f"IMAGE={image}", "--file", str(ROOT / "tests" / "Dockerfile")]
    subprocess.run([*build, str(ROOT)], check=True, capture_output=True)
    command = ["docker", "run", "--rm", "--privileged"]
    command += [f"--env=UNICON_BOX_USERS={BOX_USERS}"]
    command += ["--volume", f"{ROOT}:/src:ro", tag, "python3", "tests/userns.py"]
    command += ["pytest", "-p", "no:cacheprovider", "tests/test_exec.py"]
    done = subprocess.run(command, capture_output=True, text=True, timeout=1800)
    assert done.returncode == 0, done.stdout[-6000:] + done.stderr[-2000:]
