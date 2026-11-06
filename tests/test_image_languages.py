"""Each language exec 4 added, and a package of each source, run in the
image as a step runs them: compiled once in exec's compile box, from its
fixture lock's toolchain, then run in boxes.

Each language's program reads a mode from standard input: `double`
prints twice the number after it, `wrong` one less, the answer a checker
would refuse, which exec still runs to the end as `accepted`; `spin`
runs past its time and `grow` past its memory; `crash` exits 3, which is
a runtime error, as Kotlin's exception is.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from support import RunImage, batch_inputs, by_run, container_limits, printed

pytestmark = pytest.mark.image

KOTLIN = """
fun main() {
    val parts = readln().trim().split(" ")
    when (parts[0]) {
        "double" -> println(parts[1].toLong() * 2)
        "wrong" -> println(parts[1].toLong() * 2 - 1)
        "spin" -> { var x = 0L; while (true) { x += 1 } }
        "grow" -> {
            val kept = ArrayList<LongArray>()
            while (true) kept.add(LongArray(1 shl 17) { 1L })
        }
        "crash" -> throw IllegalStateException("crashed")
    }
}
"""
JAVASCRIPT = """
const [mode, value] = require("fs").readFileSync(0, "utf8").trim().split(/\\s+/);
if (mode === "double") console.log(2 * Number(value));
else if (mode === "wrong") console.log(2 * Number(value) - 1);
else if (mode === "spin") for (;;) {}
else if (mode === "grow") {
  const kept = [];
  for (;;) kept.push(Buffer.alloc(1 << 20, 1));
} else if (mode === "crash") process.exit(3);
"""
TYPESCRIPT = """
import { readFileSync } from "node:fs";

const [mode, value]: string[] = readFileSync(0, "utf8").trim().split(/\\s+/);
const kept: Buffer[] = [];
if (mode === "double") console.log(2 * Number(value));
else if (mode === "wrong") console.log(2 * Number(value) - 1);
else if (mode === "spin") for (;;) {}
else if (mode === "grow") for (;;) kept.push(Buffer.alloc(1 << 20, 1));
else if (mode === "crash") process.exit(3);
"""
CSHARP = """
using System;
using System.Collections.Generic;

class Program
{
    static void Main()
    {
        var parts = Console.ReadLine().Trim().Split(' ');
        var kept = new List<byte[]>();
        switch (parts[0])
        {
            case "double": Console.WriteLine(long.Parse(parts[1]) * 2); break;
            case "wrong": Console.WriteLine(long.Parse(parts[1]) * 2 - 1); break;
            case "spin": for (long x = 0; ; x++) { }
            case "grow":
                while (true)
                {
                    var block = new byte[1 << 20];
                    Array.Fill(block, (byte)1);
                    kept.Add(block);
                }
            case "crash": Environment.Exit(3); break;
        }
    }
}
"""
RUST = """
use std::io::Read;

fn main() {
    let mut input = String::new();
    std::io::stdin().read_to_string(&mut input).unwrap();
    let parts: Vec<&str> = input.split_whitespace().collect();
    let value = || parts[1].parse::<i64>().unwrap();
    match parts[0] {
        "double" => println!("{}", value() * 2),
        "wrong" => println!("{}", value() * 2 - 1),
        "spin" => {
            let mut x: u64 = 0;
            loop {
                x = std::hint::black_box(x.wrapping_add(1));
            }
        }
        "grow" => {
            let mut kept = Vec::new();
            loop {
                kept.push(vec![1u8; 1 << 20]);
            }
        }
        "crash" => std::process::exit(3),
        _ => {}
    }
}
"""
R = """
parts <- strsplit(trimws(readLines(file("stdin"), n = 1)), " ")[[1]]
mode <- parts[1]
if (mode == "double") {
  cat(as.numeric(parts[2]) * 2, "\\n", sep = "")
} else if (mode == "wrong") {
  cat(as.numeric(parts[2]) * 2 - 1, "\\n", sep = "")
} else if (mode == "spin") {
  repeat {}
} else if (mode == "grow") {
  kept <- list()
  repeat kept[[length(kept) + 1]] <- rep(1, 2^17)
} else if (mode == "crash") {
  quit(status = 3)
}
"""
RUBY = """
mode, value = $stdin.read.split
case mode
when "double" then puts value.to_i * 2
when "wrong" then puts value.to_i * 2 - 1
when "spin" then loop {}
when "grow"
  kept = []
  loop { kept << ("x" * (1 << 20)) }
when "crash" then exit 3
end
"""
LANGUAGES = {
    "kotlin": ("main.kt", KOTLIN, 'fun main() { val x: Int = "text" }\n'),
    "javascript": ("main.js", JAVASCRIPT, "console.log(;\n"),
    "typescript": ("main.ts", TYPESCRIPT, "const x: number = 'text';\n"),
    "csharp": (
        "Program.cs",
        CSHARP,
        'class P { static void Main() { int x = "a"; } }\n',
    ),
    "rust": ("main.rs", RUST, 'fn main() { let x: i32 = "text"; }\n'),
    "r": ("main.R", R, None),
    "ruby": ("main.rb", RUBY, "def broken(\n"),
}
"""Each language's program, the file it is in, and a source that does not
compile, or none for a language with no compile."""
ITEMS = [
    ("main-double", "double 21\n"),
    ("main-wrong", "wrong 21\n"),
    ("main-spin", "spin\n"),
    ("main-grow", "grow\n"),
    ("main-crash", "crash\n"),
]


def run_batch(
    run_image: RunImage,
    work: Path,
    source: dict[str, str],
    language: str,
    items: list[tuple[str, str]],
    time_limit: float = 2.0,
    memory_limit: float = 128,
    entry: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Run one batch of `source` in `language`, starting from `entry`, under
    the container limits the declaration gives it."""
    batch_inputs(work, source, items, time_limit, memory_limit, language=language)
    if entry is not None:
        document = json.loads((work / "inputs.json").read_text())
        for each in document["batch"]:
            each["inputs"]["entry"] = entry
        (work / "inputs.json").write_text(json.dumps(document))
    limits = container_limits(time_limit, memory_limit, len(items))
    return by_run(run_image(work, limits))


@pytest.mark.parametrize("language", sorted(LANGUAGES))
def test_each_new_language_compiles_runs_and_reaches_every_limit(
    tmp_path: Path, run_image: RunImage, language: str
) -> None:
    """A program of each language exec 4 added compiles once and runs on its
    locked toolchain: a right answer and a wrong one both run to the end,
    their output kept for the checker, and a run over its time, over its
    memory and one that crashes each end as such."""
    name, source, _ = LANGUAGES[language]
    work = tmp_path / "work"
    outputs = run_batch(run_image, work, {name: source}, language, ITEMS)
    assert {run: found["outcome"] for run, found in outputs.items()} == {
        "main-double": "accepted",
        "main-wrong": "accepted",
        "main-spin": "time_limit",
        "main-grow": "memory_limit",
        "main-crash": "runtime_error",
    }
    assert printed(work, outputs["main-double"]) == "42\n"
    assert printed(work, outputs["main-wrong"]) == "41\n"
    assert outputs["main-spin"]["time_ms"] >= 2000
    assert outputs["main-double"]["log"] == outputs["main-grow"]["log"]


@pytest.mark.parametrize(
    "language", sorted(name for name, (*_, broken) in LANGUAGES.items() if broken)
)
def test_a_source_that_does_not_compile_is_a_compile_error_with_its_log(
    tmp_path: Path, run_image: RunImage, language: str
) -> None:
    """A source its compiler or checker refuses runs nothing: every item is
    `compile_error`, with the compiler's log."""
    name, _, broken = LANGUAGES[language]
    assert broken is not None
    work = tmp_path / "work"
    outputs = run_batch(run_image, work, {name: broken}, language, ITEMS[:1])
    assert outputs["main-double"]["outcome"] == "compile_error"
    assert name in outputs["main-double"]["log"]


def test_a_folder_of_ruby_files_runs_from_the_named_entry(
    tmp_path: Path, run_image: RunImage
) -> None:
    """A script's folder is the program, laid out as given: the entry
    requires a file beside it by its relative path."""
    source = {
        "main.rb": 'require_relative "lib/twice"\nputs twice($stdin.read.to_i)\n',
        "lib/twice.rb": "def twice(value) = value * 2\n",
    }
    work = tmp_path / "work"
    outputs = run_batch(
        run_image, work, source, "ruby", [("main-1", "21\n")], entry="main.rb"
    )
    assert outputs["main-1"]["outcome"] == "accepted"
    assert printed(work, outputs["main-1"]) == "42\n"


KOTLIN_SOLUTION = """
package task

import task.driver.twice

fun main() = println(twice(readln().toInt()))
"""
KOTLIN_DRIVER = """
@file:JvmName("TestDriver")
package task.driver

fun twice(value: Int) = value * 2

fun main() = println("the driver ran")
"""
CSHARP_SOLUTION = """
namespace Task;

static class Solution
{
    static void Main()
    {
        var value = int.Parse(System.Console.ReadLine()!);
        System.Console.WriteLine(Driver.Twice(value));
    }
}
"""
CSHARP_DRIVER = """
namespace Task
{
    static class Driver
    {
        public static int Twice(int value) => value * 2;

        static void Main() => System.Console.WriteLine("the driver ran");
    }
}
"""
DRIVEN = {
    "kotlin": (
        {"solution.kt": KOTLIN_SOLUTION, "Driver.kt": KOTLIN_DRIVER},
        "solution.kt",
        "Driver.kt",
    ),
    "csharp": (
        {"Solution.cs": CSHARP_SOLUTION, "Driver.cs": CSHARP_DRIVER},
        "Solution.cs",
        "Driver.cs",
    ),
}
"""A folder of two files that each hold main, a solution and a test
driver beside it, by language: the files, and each one as the entry."""


@pytest.mark.parametrize("language", sorted(DRIVEN))
def test_a_folder_of_two_mains_runs_the_named_entrys(
    tmp_path: Path, run_image: RunImage, language: str
) -> None:
    """Kotlin runs the class of the entry's top-level functions, by its
    file's name or its `@file:JvmName`, and C# the type whose `Main` the
    entry declares, though another file declares main too."""
    source, solution, driver = DRIVEN[language]
    for entry, said in ((solution, "42\n"), (driver, "the driver ran\n")):
        work = tmp_path / entry
        outputs = run_batch(
            run_image, work, source, language, [("main-1", "21\n")], entry=entry
        )
        assert outputs["main-1"]["outcome"] == "accepted", outputs["main-1"]["log"]
        assert printed(work, outputs["main-1"]) == said


def test_a_kotlin_entry_without_main_at_its_top_level_is_a_compile_error(
    tmp_path: Path, run_image: RunImage
) -> None:
    """An entry whose file class kotlinc did not write, as one with no
    top-level function, names the class it looked for."""
    source = {
        "Main.kt": "object Main { @JvmStatic fun main(args: Array<String>) {} }\n",
        "other.kt": "fun main() {}\n",
    }
    work = tmp_path / "work"
    outputs = run_batch(
        run_image, work, source, "kotlin", [("main-1", "")], entry="Main.kt"
    )
    assert outputs["main-1"]["outcome"] == "compile_error"
    assert "no class MainKt to run" in outputs["main-1"]["log"]


GARBAGE = """
const live = new Array(16 * 1024 * 1024).fill(1.5);
let kept = null;
for (let i = 0; i < 400; i++) kept = new Array(1024 * 1024).fill(i + 0.5);
console.log(live.length > 0 && kept[0] > 0);
"""


HEAP = """
const kept = [];
for (;;) kept.push(new Array(1 << 17).fill(1.5));
"""


def test_a_javascript_program_dropping_old_garbage_stays_within_its_memory(
    tmp_path: Path, run_image: RunImage
) -> None:
    """Node's heap is the run's memory limit, so V8 collects in time: 3.2 GB
    of 8 MB arrays dropped one by one beside 128 MB kept, under 256 MB,
    run to the end, where a heap past the box was killed as
    `memory_limit`. A heap that only grows reaches its limit, which V8
    ends by `SIGABRT`, `memory_limit` too."""
    for source, outcome in ((GARBAGE, "accepted"), (HEAP, "memory_limit")):
        work = tmp_path / outcome
        outputs = run_batch(
            run_image,
            work,
            {"main.js": source},
            "javascript",
            [("main-1", "")],
            time_limit=10,
            memory_limit=256,
        )
        assert outputs["main-1"]["outcome"] == outcome
        if outcome == "accepted":
            assert printed(work, outputs["main-1"]) == "true\n"


def test_a_javascript_folder_holding_node_modules_is_a_compile_error(
    tmp_path: Path, run_image: RunImage
) -> None:
    """A JavaScript program's `node_modules` is the locked one, linked
    beside it, so a folder that brings its own is refused."""
    source = {"main.js": "console.log(1)\n", "node_modules/x/index.js": ""}
    work = tmp_path / "work"
    outputs = run_batch(
        run_image, work, source, "javascript", [("main-1", "")], entry="main.js"
    )
    assert outputs["main-1"]["outcome"] == "compile_error"
    assert "node_modules/x/index.js" in outputs["main-1"]["log"]


JUNIT = {
    "Main.java": """
import org.junit.platform.launcher.Launcher;
import org.junit.platform.launcher.core.LauncherFactory;
import org.junit.platform.launcher.listeners.SummaryGeneratingListener;
import static org.junit.platform.engine.discovery.DiscoverySelectors.selectClass;
import static org.junit.platform.launcher.core.LauncherDiscoveryRequestBuilder.request;

public class Main {
    public static void main(String[] args) {
        var listener = new SummaryGeneratingListener();
        Launcher launcher = LauncherFactory.create();
        var found = request().selectors(selectClass(AddTest.class)).build();
        launcher.execute(found, listener);
        var summary = listener.getSummary();
        System.out.println(summary.getTestsSucceededCount() + " passed "
            + summary.getTestsFailedCount() + " failed");
    }
}
""",
    "AddTest.java": """
import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

class AddTest {
    @Test void adds() { assertEquals(4, 2 + 2); }
    @Test void addsWrongly() { assertEquals(5, 2 + 2); }
}
""",
}
KOTLIN_TEST = {
    "main.kt": """
import org.junit.platform.engine.discovery.DiscoverySelectors.selectClass
import org.junit.platform.launcher.core.LauncherDiscoveryRequestBuilder.request
import org.junit.platform.launcher.core.LauncherFactory
import org.junit.platform.launcher.listeners.SummaryGeneratingListener

fun main() {
    val listener = SummaryGeneratingListener()
    val found = request().selectors(selectClass(AddTest::class.java)).build()
    LauncherFactory.create().execute(found, listener)
    val summary = listener.summary
    println("${summary.testsSucceededCount} passed ${summary.testsFailedCount} failed")
}
""",
    "AddTest.kt": """
import kotlin.test.Test
import kotlin.test.assertEquals

class AddTest {
    @Test fun adds() = assertEquals(4, 2 + 2)
    @Test fun addsWrongly() = assertEquals(5, 2 + 2)
}
""",
}
JEST = {
    "main.js": """
process.on("uncaughtException", (error) => console.log(error.stack));
const { runCLI } = require("jest");

const config = JSON.stringify({ rootDir: __dirname });
runCLI(
  { _: [], $0: "jest", config, runInBand: true, watchman: false, cache: false },
  [__dirname],
).then(({ results }) => {
  console.log(`${results.numPassedTests} passed ${results.numFailedTests} failed`);
});
""",
    "add.test.js": """
test("adds", () => expect(2 + 2).toBe(4));
test("adds wrongly", () => expect(2 + 2).toBe(5));
""",
}
CHAI = {
    "main.mts": """
import { expect } from "chai";

let passed = 0;
let failed = 0;
for (const [sum, expected] of [[2 + 2, 4], [2 + 2, 5]]) {
  try {
    expect(sum).to.equal(expected);
    passed += 1;
  } catch {
    failed += 1;
  }
}
console.log(`${passed} passed ${failed} failed`);
""",
}
TESTTHAT = {
    "main.R": """
library(testthat)
script <- sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE))
found <- as.data.frame(test_file(file.path(dirname(script), "test_add.R"),
                                 reporter = "silent", stop_on_failure = FALSE))
cat(sum(found$passed), "passed", sum(found$failed), "failed\\n")
""",
    "test_add.R": """
test_that("adds", expect_equal(2 + 2, 4))
test_that("adds wrongly", expect_equal(2 + 2, 5))
""",
}
GTEST = {
    "main.cpp": """
#include <gtest/gtest.h>
#include <iostream>

TEST(Add, Adds) { EXPECT_EQ(2 + 2, 4); }
TEST(Add, AddsWrongly) { EXPECT_EQ(2 + 2, 5); }

int main(int argc, char **argv) {
    ::testing::InitGoogleTest(&argc, argv);
    (void)RUN_ALL_TESTS();
    auto *unit = ::testing::UnitTest::GetInstance();
    std::cout << unit->successful_test_count() << " passed "
              << unit->failed_test_count() << " failed\\n";
}
""",
}
EIGEN = {
    "main.cpp": """
#include <Eigen/Dense>
#include <iostream>

int main() {
    Eigen::Matrix2d m;
    m << 4, 3, 6, 3;
    std::cout << m.determinant() << " passed 0 failed\\n";
}
""",
}
PACKAGES = {
    "junit": ("java", JUNIT, "Main.java", "1 passed 1 failed"),
    "kotlin-test": ("kotlin", KOTLIN_TEST, "main.kt", "1 passed 1 failed"),
    "jest": ("javascript", JEST, "main.js", "1 passed 1 failed"),
    "chai": ("typescript", CHAI, None, "1 passed 1 failed"),
    "testthat": ("r", TESTTHAT, "main.R", "1 passed 1 failed"),
    "gtest": ("cpp", GTEST, None, "1 passed 1 failed"),
    "eigen": ("cpp", EIGEN, None, "-6 passed 0 failed"),
}
"""A program using one locked package of each source, by the package: its
language, its files, its entry, and the last line it prints."""


@pytest.mark.parametrize("package", sorted(PACKAGES))
def test_a_package_of_each_source_is_locked_placed_and_runs(
    tmp_path: Path, run_image: RunImage, package: str
) -> None:
    """Maven's JUnit 5 and kotlin-test from `/env/lib/java/`, npm's Jest,
    run in band, and chai from the `node_modules` beside the program, R's
    testthat and C++'s gtest and eigen from conda, the last two through
    pkg-config: each runs a passing test and a failing one, or for eigen a
    determinant."""
    language, source, entry, last = PACKAGES[package]
    work = tmp_path / "work"
    outputs = run_batch(
        run_image,
        work,
        source,
        language,
        [("main-1", "")],
        time_limit=10,
        memory_limit=512,
        entry=entry,
    )
    assert outputs["main-1"]["outcome"] == "accepted", outputs["main-1"]["log"]
    assert printed(work, outputs["main-1"]).splitlines()[-1].strip() == last
