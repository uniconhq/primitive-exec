"""The image, run on Docker as an interact step container is: privileged, as
root, through `userns.py` (see `conftest.py`).

Every program is given as its source, which exec compiles before the
runs. The verdict script of `unicon/interactive@v2` is run on exec's image
over the facts interact wrote, the two chained by hand as the forge will
chain them.
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from support import (
    BOX_USERS,
    GAME_LINKS,
    GUESSES,
    ISOLATE_LIMITS,
    NAME,
    ROOT,
    SCHEMA_VERSION,
    SECRET,
    SIBLING_VERDICT,
    STEP_MEMORY_MB,
    Check,
    RunImage,
    Spec,
    TestItem,
    by_run,
    facts_of,
    game,
)
from support import (
    programs_batch_inputs as batch_inputs,
)
from support import (
    programs_container_limits as container_limits,
)

pytestmark = pytest.mark.image

ENTRANT_CPP = """
#include <bits/stdc++.h>
int main(int argc, char** argv) {
    std::string c = argv[1];
    if (c == "leave") return 0;
    int low = 1, high = 1000;
    for (;;) {
        int guess = (low + high) / 2;
        std::cout << guess << std::endl;
        std::string answer;
        if (!(std::cin >> answer)) return 0;
        if (answer == "higher") low = guess + 1;
        else if (answer == "lower") high = guess - 1;
        else return 0;
    }
}
"""

JUDGE_CPP = """
#include <bits/stdc++.h>
#include <unistd.h>
static void put(const char* name, const std::string& value) {
    std::ofstream(std::string(getenv("UNICON_REPORT")) + "/" + name) << value << "\\n";
}
int main(int argc, char** argv) {
    if (std::string(argv[1]) == "greet") {
        usleep(300000);
        std::string y(1 << 20, 'y');
        fwrite(y.data(), 1, y.size(), stdout);
        fflush(stdout);
    }
    int secret;
    std::ifstream("secret") >> secret;
    for (int guesses = 1; guesses <= 20; guesses++) {
        int guess;
        if (!(std::cin >> guess)) {
            put("message", "the program stopped guessing");
            put("verdict", "1");
            return 0;
        }
        if (guess == secret) {
            put("message", "found");
            put("guesses", std::to_string(guesses));
            put("verdict", "0");
            std::cout << "correct" << std::endl;
            return 0;
        }
        std::cout << (guess < secret ? "higher" : "lower") << std::endl;
    }
    put("message", "too many guesses");
    put("verdict", "1");
    return 0;
}
"""
"""A native judge, which `SIGPIPE` kills as it kills most: `greet` writes
1 MB before it reads, and `plain` reads first."""


def test_the_guessing_game_through_the_image(
    tmp_path: Path, run_image: RunImage, programs_check: Check
) -> None:
    """The inputs and the outputs are the contract's, each program's facts
    and the judge's reports `<name>.<fact>`, and `facts` the file of
    them."""
    work = tmp_path / "work"
    items: list[TestItem] = [
        ("main-guess", game("guess"), GAME_LINKS),
        ("main-wrong", game("wrong"), GAME_LINKS),
        ("main-slow", game("slow"), GAME_LINKS),
    ]
    batch_inputs(work, items)
    programs_check(json.loads((work / "inputs.json").read_text()), "inputs_file")
    result = run_image(work, container_limits(items))
    programs_check(
        result,
        "outputs_file",
        {"entrant": frozenset(), "judge": frozenset(GUESSES["reports"])},
    )
    outputs = by_run(result)
    assert outputs["main-guess"]["judge.guesses"] == 10
    assert outputs["main-guess"]["entrant.ended"] == "finished"
    assert outputs["main-wrong"]["entrant.ended"] in ("finished", "runtime_error")
    assert outputs["main-slow"]["entrant.ended"] == "time_limit"
    assert outputs["main-slow"]["judge.stopped_by"] == "entrant"
    facts = facts_of(work, outputs["main-wrong"])
    assert facts["judge"]["verdict"] == "1"
    assert outputs["main-slow"]["facts"] == {"file": "out/3/facts.json"}


def test_native_programs_compile_and_play(tmp_path: Path, run_image: RunImage) -> None:
    """A native judge and entrant, each compiled once for the batch, play
    the game, and a native judge writing to an entrant that left dies of
    `SIGPIPE`, after the entrant ended."""

    def pair(entrant_case: str, judge_case: str) -> dict[str, Spec]:
        return {
            "entrant": Spec(ENTRANT_CPP, entrant_case, language="cpp"),
            "judge": Spec(
                JUDGE_CPP,
                judge_case,
                language="cpp",
                time_limit=2,
                files=SECRET,
                manifest=GUESSES,
            ),
        }

    work = tmp_path / "work"
    items: list[TestItem] = [
        ("main-guess", pair("guess", "plain"), GAME_LINKS),
        ("main-leave", pair("leave", "greet"), GAME_LINKS),
    ]
    batch_inputs(work, items)
    outputs = by_run(run_image(work, container_limits(items)))
    assert outputs["main-guess"]["entrant.log"] == ""
    assert outputs["main-guess"]["judge.guesses"] == 10
    left = facts_of(work, outputs["main-leave"])
    assert left["entrant"]["ended"] == "finished"
    assert left["judge"]["signal"] == "SIGPIPE"
    assert left["judge"]["ended_at"] >= left["entrant"]["ended_at"]


def test_programs_the_container_cannot_hold_together_are_an_error_of_the_step(
    tmp_path: Path, run_image: RunImage
) -> None:
    """Two programs of 512 MB each, side by side, with their copies and the
    step's own room, do not fit a step of 1136 MB, the least the declaration
    gives: exec says so before any program runs, rather than leave the
    step's limit to kill one and be read as its own memory limit."""
    played = game("guess", entrant_memory_limit=512, judge_memory_limit=512)
    items: list[TestItem] = [("main-1", played, GAME_LINKS)]
    batch_inputs(tmp_path / "work", items)
    limits = container_limits(items) | {"memory_mb": 1136}
    result = run_image(tmp_path / "work", limits)
    assert result["error"].startswith("the run main-1 needs 1025 MB")
    assert "step's 1136 MB leaves 1024 MB" in result["error"]


EXEC_LIMITS = {"time_ms": 13000, "cpu_ms": 13000, "memory_mb": 1136, "pids": 160}
"""What the harness gives an exec step running the verdict script per item,
by exec's `limits_from`, under the workflow's time limit of 5 s and 64 MB;
`ONCE_TIME_MS` is added once for the batch."""
ONCE_TIME_MS = 40000


def verdicts(
    tmp_path: Path,
    run_image: RunImage,
    script: str,
    facts: list[tuple[str, bytes]],
) -> dict[str, Any]:
    """Run the verdict script on this image's one-program form over each
    item's facts, as the `decide` step of `unicon/interactive@v2` does, and
    return its outputs.json."""
    manifest = yaml.safe_load((SIBLING_VERDICT / "program.yaml").read_text())
    work = tmp_path / f"exec-{len(list(tmp_path.glob('exec-*')))}"
    (work / "in" / "verdict").mkdir(parents=True)
    (work / "in" / "verdict" / "verdict.py").write_text(script)
    (work / "in" / "manifest.json").write_text(json.dumps(manifest))
    batch = []
    for number, (run, content) in enumerate(facts, 1):
        (work / "in" / str(number)).mkdir()
        (work / "in" / str(number) / "facts").write_bytes(content)
        inputs = {
            "source": {"folder": "in/verdict"},
            "language": "python",
            "manifest": {"file": "in/manifest.json"},
            "time_limit": 5,
            "memory_limit": 64,
            "files": {"files": {"facts": {"file": f"in/{number}/facts"}}},
        }
        batch.append({"run": run, "inputs": inputs})
    document = {"schema_version": SCHEMA_VERSION, "batch": batch}
    (work / "inputs.json").write_text(json.dumps(document))
    count = len(facts)
    limits = {
        name: value * count if name.endswith("_ms") else value
        for name, value in EXEC_LIMITS.items()
    }
    limits["time_ms"] += ONCE_TIME_MS
    return run_image(work, limits | {"output_mb": 64})


def test_interactive_v1_judges_through_both_forms(
    tmp_path: Path, run_image: RunImage
) -> None:
    """interact plays each game, and the verdict script, compiled and run by
    exec given its manifest and the facts, gives the verdict
    `unicon/interactive@v2` promises: the judge's `0` or `1`, the entrant's
    limit, and the entrant's crash or a wrong answer for an entrant that
    left; a judge's `0` however it ended once the entrant had, by
    `SIGPIPE` or a raised error; and a judge that crashed, or raised with no
    verdict, is an error of the step."""
    folder = Path(os.environ.get("VERDICT_SCRIPT", SIBLING_VERDICT))
    if not (folder / "verdict.py").is_file():
        pytest.skip("no verdict script; set VERDICT_SCRIPT to its folder")
    script = (folder / "verdict.py").read_text()
    expected = {
        "main-guess": (game("guess"), "accepted"),
        "main-wrong": (game("wrong"), "wrong_answer"),
        "main-slow": (game("slow"), "time_limit"),
        "main-grow": (game("grow", entrant_memory_limit=48), "memory_limit"),
        "main-leave": (game("leave", "pipe"), "wrong_answer"),
        "main-fail": (game("fail", "pipe"), "runtime_error"),
        "main-raise": (game("leave", "pipe-python"), None),
        "main-farewell": (game("guess", "farewell"), "accepted"),
        "main-farewell-native": (game("guess", "farewell-native"), "accepted"),
        "main-crash": (game("guess", "crash"), None),
    }
    work = tmp_path / "work"
    items: list[TestItem] = [
        (run, programs, GAME_LINKS) for run, (programs, _) in expected.items()
    ]
    batch_inputs(work, items)
    outputs = by_run(run_image(work, container_limits(items)))
    facts = {
        run: (work / outputs[run]["facts"]["file"]).read_bytes() for run in expected
    }

    judged = [(run, facts[run]) for run, (_, outcome) in expected.items() if outcome]
    result = by_run(verdicts(tmp_path, run_image, script, judged))
    assert {run: result[run]["outcome"] for run in result} == {
        run: outcome for run, (_, outcome) in expected.items() if outcome
    }
    assert result["main-guess"]["message"] == "found"
    assert result["main-wrong"]["message"] == "too many guesses"
    assert result["main-guess"]["extras"] == {"folder": "out/1/extras"}

    for run in [run for run, (_, outcome) in expected.items() if not outcome]:
        faulted = verdicts(tmp_path, run_image, script, [(run, facts[run])])
        assert "did not judge" in faulted["error"], run


def test_on_root_docker_the_image_refuses_to_start(
    tmp_path: Path, run_image: RunImage, rootless: bool
) -> None:
    """Started as its entrypoint is, privileged under root Docker, whose user
    ID map is the identity, the step would be the machine's own root:
    interact runs nothing and says why."""
    if rootless:
        pytest.skip("the daemon is rootless: there is no root Docker here")
    work = tmp_path / "work"
    items: list[TestItem] = [("main-1", game("guess"), GAME_LINKS)]
    batch_inputs(work, items)
    result = run_image(work, container_limits(items), root_docker=True)
    assert set(result) == {"schema_version", "error"}
    assert "machine's own root" in result["error"]
    assert not (work / "out").exists()


def test_the_in_process_tests_pass_in_the_test_container(
    image: str, rootless: bool
) -> None:
    """`test_interact.py` in the test container built on the image,
    privileged, as root through `userns.py`, or as it is on a rootless
    daemon, where every test that runs programs makes its boxes as a step
    does and none is skipped."""
    tag = f"{NAME}:test-container"
    build = ["docker", "build", "--quiet", "--tag", tag, "--build-arg"]
    build += [f"IMAGE={image}", "--file", str(ROOT / "tests" / "Dockerfile")]
    subprocess.run([*build, str(ROOT)], check=True, capture_output=True)
    command = ["docker", "run", "--rm", "--privileged", *ISOLATE_LIMITS]
    command += [f"--env=UNICON_BOX_USERS={BOX_USERS}"]
    command += [f"--env=UNICON_STEP_MEMORY_MB={STEP_MEMORY_MB}"]
    command += ["--volume", f"{ROOT}:/src:ro", tag]
    command += [] if rootless else ["python3", "tests/userns.py"]
    command += ["pytest", "-p", "no:cacheprovider", "tests/test_programs.py"]
    done = subprocess.run(command, capture_output=True, text=True, timeout=1800)
    assert done.returncode == 0, done.stdout[-6000:] + done.stderr[-2000:]
