"""The image, run on Docker under the harness's sandbox flags.

Most Python binaries are built here as zip applications whose `__main__.py` is
the program itself; native and Java binaries, and one Python binary with
compile's launcher and `source/` folder, come from the compile primitive's own
image, so these tests are also the check that the two primitives agree on the
binary format.
"""

import errno
import json
import time
from pathlib import Path
from typing import Any

import pytest

from support import (
    PROGRAM,
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
    owned: bool = False,
    files: dict[str, dict[str, bytes]] | None = None,
    loads: dict[str, dict[str, bytes]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Run one batch under the container limits the declaration gives it."""
    batch_inputs(
        work, binary, items, time_limit, memory_limit, args, files=files, loads=loads
    )
    limits = container_limits(time_limit, memory_limit, len(items))
    return by_run(run_image(work, limits, owned=owned))


def outcomes(outputs: dict[str, dict[str, Any]]) -> dict[str, object]:
    """Each item's outcome."""
    return {test: entry["outcome"] for test, entry in outputs.items()}


def test_python_runs_reach_every_outcome(
    tmp_path: Path, run_image: RunImage, check: Check
) -> None:
    """One batch in the sandbox reaches each outcome, keeps each output and
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
    """A run writes only its own directory, and nothing it leaves reaches the next."""
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
        "refused /work/escaped 13",
        "refused /work/inputs.json 13",
        "refused /tmp/escaped 13",
        "cwd ['here']",
    ]
    assert printed(work, outputs["main-census"]) == "3\n"


def test_a_run_that_takes_away_its_files_modes_faults_nothing(
    tmp_path: Path, run_image: RunImage
) -> None:
    """A run may change the modes of the files and folders its user owns, as
    the harness lays them out: the working directory, `in/` and the next
    run's input, `out/` and its own output among them; each is given back
    after it, so the next run and the harness still read and write them.
    """
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    items = [("main-chmod", "chmod\n/work\n"), ("main-next", "double\n21\n")]
    outputs = run_batch(run_image, work, binary, items, owned=True)
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
    """In the sandbox, a later run cannot read an earlier run's output or its
    input, the step's own files, or list /work or /tmp; the system's files
    it can.
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
    makes is removed before the next, which could otherwise read in it what
    the first was given.
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
    outputs = run_batch(run_image, work, binary, items, owned=True)
    assert printed(work, outputs["main-mark"]) == "marked\n"
    assert printed(work, outputs["main-read"]) == "no attribute\ntime fresh\n"


def test_a_run_finds_no_attribute_an_earlier_run_set_on_a_file_it_named(
    tmp_path: Path, run_image: RunImage
) -> None:
    """An extended attribute is no place to keep work for the next test:
    one set on the next item's input, on `out/` or on the run's own output,
    files it can name and write though Landlock keeps it from reading
    them, is gone for the next run."""
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    paths = "/work/in/3/input /work/out /work/out/1/output"
    items = [
        ("main-mark", f"mark-paths\n{paths}\n"),
        ("main-read", f"read-paths\n{paths}\n"),
    ]
    outputs = run_batch(run_image, work, binary, items)
    marked = printed(work, outputs["main-mark"]).splitlines()
    assert [line.split()[0] for line in marked] == ["marked", "marked", "marked"]
    found = printed(work, outputs["main-read"]).splitlines()
    assert [line.split()[0] for line in found] == ["none", "none", "none"]


def test_a_run_that_locks_what_it_marked_leaves_no_attribute(
    tmp_path: Path, run_image: RunImage
) -> None:
    """A run that takes the access away from what it marked, a file by
    `chmod 0444` and a folder by `chmod 0`, still leaves nothing for the next
    run: each path is given back before its attributes are removed."""
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    paths = "/work/in/3/input /work/out/1/output /work/in/3"
    items = [
        ("main-mark", "mark-and-lock\n"),
        ("main-read", f"read-paths\n{paths}\n"),
    ]
    outputs = run_batch(run_image, work, binary, items, owned=True)
    assert outcomes(outputs) == {"main-mark": "accepted", "main-read": "accepted"}
    assert printed(work, outputs["main-mark"]) == "marked and locked\n"
    found = printed(work, outputs["main-read"]).splitlines()
    assert [line.split()[0] for line in found] == ["none", "none", "none"]


def test_a_run_that_locks_its_own_copy_does_not_stop_the_next(
    tmp_path: Path, run_image: RunImage
) -> None:
    """A run can name its copy of the binary, the scratch folder it is in and
    the binary under `in/`, and take this program's access to them away; the
    next item still gets its copy, so no run can end the batch in an error."""
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    items = [("main-lock", "lock-scratch\n"), ("main-next", "double\n21\n")]
    outputs = run_batch(run_image, work, binary, items, owned=True)
    assert outcomes(outputs) == {"main-lock": "accepted", "main-next": "accepted"}
    assert printed(work, outputs["main-lock"]) == "locked\n"
    assert printed(work, outputs["main-next"]) == "42\n"


def test_a_run_can_make_no_posix_message_queue(
    tmp_path: Path, run_image: RunImage
) -> None:
    """A POSIX queue outlives its process and none can be listed to remove,
    so a run is given no bytes for one, and the next run finds none."""
    binary = python_binary(tmp_path / "build" / "binary", PROGRAM).read_bytes()
    work = tmp_path / "work"
    items = [("main-leave", "leave-mq\n"), ("main-find", "find-mq\n")]
    outputs = run_batch(run_image, work, binary, items)
    assert outcomes(outputs) == {"main-leave": "accepted", "main-find": "accepted"}
    assert printed(work, outputs["main-leave"]).split()[0] == "refused"
    assert printed(work, outputs["main-find"]) == f"none {errno.ENOENT}\n"


def test_named_files_are_placed_read_only_in_the_sandbox(
    tmp_path: Path, run_image: RunImage, check: Check
) -> None:
    """In the sandbox, as the harness lays `in/` out: the program sees exactly
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
    result = run_image(work, container_limits(1.0, 64, len(items)), owned=True)
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


def test_a_hostile_run_cannot_reach_the_program_that_watches_it(
    tmp_path: Path, run_image: RunImage
) -> None:
    """In the sandbox: signalling the launcher is refused from Landlock ABI 6
    and killing it below that is the run's own runtime error, either way
    without a traceback whose cost could reach the time limit on a busy
    machine, work and memory hidden in a child still count, the launcher's descriptors
    and the program's memory cannot be opened, and SIGINT does not stop the
    program.
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
        "refused 13",
        "refused 13",
    ]
    interrupted = printed(work, outputs["main-interrupt"]).splitlines()
    assert interrupted[-1] == "still here"
    assert printed(work, outputs["main-ok"]) == "42\n"


def test_memory_is_the_whole_runs(tmp_path: Path, run_image: RunImage) -> None:
    """In the sandbox, two processes holding 40 MB each are over a 64 MB limit
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
    """In the sandbox, memory a run keeps outside its processes is memory:
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
        "/etc/os-release",
    ]
    items = [("main-first", "double 21"), ("main-peek", " ".join(["peek", *paths]))]
    outputs = run_batch(run_image, work, binary, items)
    assert outcomes(outputs) == {"main-first": "accepted", "main-peek": "accepted"}
    lines = printed(work, outputs["main-peek"]).splitlines()
    assert lines == [
        "refused /work/out/1/output",
        "refused /work/in/2/input",
        "refused /work/inputs.json",
        "read /etc/os-release",
    ]


def test_c_binaries_from_compile(
    tmp_path: Path, run_image: RunImage, compiled: Compiled
) -> None:
    """A C binary from compile runs under the sandbox's rules."""
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


def test_python_args_in_the_sandbox(tmp_path: Path, run_image: RunImage) -> None:
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
