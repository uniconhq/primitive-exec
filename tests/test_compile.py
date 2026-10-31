"""Compiling a program's source, in-process: reading the folder, the Python
zip application, the compile box and the machine's cache.

The tests of reading a folder and of the zip application run anywhere. The
tests that compile need root and isolate, as a step does, so they are
skipped elsewhere and run in the privileged test container
(`tests/Dockerfile`), started by `test_image.py`, where
`UNICON_TESTS_IN_BOXES` makes a skip a failure.
"""

import errno
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from exec import box
from exec.compile import build, cache, java, notebook, prepare, python, shared
from exec.main import main
from support import BOX_USERS, STEP_MEMORY_MB, write_source

IN_BOXES = os.environ.get("UNICON_TESTS_IN_BOXES") == "1"

SOURCES = {
    "python": ("main.py", "print(int(input()) * 2)\n"),
    "c": (
        "main.c",
        '#include <stdio.h>\nint main(void) { int n; scanf("%d", &n);'
        ' printf("%d\\n", n * 2); return 0; }\n',
    ),
    "cpp": (
        "main.cpp",
        "#include <bits/stdc++.h>\nint main() { long long n; std::cin >> n;"
        ' std::cout << n * 2 << "\\n"; }\n',
    ),
    "java": (
        "Solution.java",
        "package contest.a;\nimport java.util.*;\n"
        "public class Solution {\n  public static void main(String[] args) {\n"
        "    System.out.println(new Scanner(System.in).nextInt() * 2);\n  }\n}\n",
    ),
}
"""A program in each language that doubles the number it reads."""

FOLDERS: dict[str, tuple[dict[str, str], str | None]] = {
    "c": (
        {
            "main.c": '#include <stdio.h>\n#include "lib/twice.h"\n'
            'int main(void) { int n; scanf("%d", &n);'
            ' printf("%d\\n", twice(n)); return 0; }\n',
            "lib/twice.h": "int twice(int n);\n",
            "lib/twice.c": '#include "twice.h"\nint twice(int n) { return 2 * n; }\n',
            "notes.txt": "not a source\n",
        },
        None,
    ),
    "cpp": (
        {
            "solution.cpp": "#include <iostream>\nint twice(int n);\n"
            "int main() { int n; std::cin >> n; std::cout << twice(n) << '\\n'; }\n",
            "src/twice.cc": "int twice(int n) { return 2 * n; }\n",
            "test/driver.cpp": "int main() { return 1; }\n",
        },
        "solution.cpp",
    ),
    "java": (
        {
            "com/example/Main.java": "package com.example;\n"
            "import com.example.util.Twice;\n"
            "public class Main {\n  public static void main(String[] args) {\n"
            "    int n = new java.util.Scanner(System.in).nextInt();\n"
            "    System.out.println(Twice.of(n));\n  }\n}\n",
            "com/example/util/Twice.java": "package com.example.util;\n"
            "public class Twice { public static int of(int n) { return 2 * n; } }\n",
        },
        None,
    ),
    "python": (
        {
            "app/main.py": "import sys\nfrom twice import twice\n"
            "print(twice(int(sys.stdin.readline())))\n",
            "app/twice.py": "def twice(n):\n    return 2 * n\n",
            "tools/check.py": 'if __name__ == "__main__":\n    print("not this one")\n',
        },
        "app/main.py",
    ),
}
"""A program of several files in nested folders for each language, and the
entry it names, None when it is found without one. Each doubles the number it
reads."""

BROKEN = {
    "python": ("main.py", "print(int(input()) * 2\n", "was never closed"),
    "c": ("main.c", "int main(void) { return x; }\n", "main.c:1:"),
    "cpp": ("main.cpp", "int main() { return x; }\n", "main.cpp:1:"),
    "java": (
        "Main.java",
        "public class Main { void f() { int x = } }\n",
        "Main.java:1:",
    ),
}


def make_folder(root: Path, files: dict[str, str]) -> Path:
    """Write files by path under a folder and return it."""
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def packed(
    tmp_path: Path, files: dict[str, str], language: str, entry: str | None = None
) -> Path:
    """Prepare a folder of `files` and pack it as a Python program, as exec
    does once the check passes, and return the program."""
    folder = make_folder(tmp_path / "folder", files)
    copy = tmp_path / "copy"
    sources = prepare.prepare(folder, copy, language, entry)
    program = tmp_path / "program"
    python.write_zip_application(copy, sources, program)
    return program


def run_python(program: Path, stdin: str) -> str:
    """Run a Python program the way exec does and return what it printed."""
    return subprocess.run(
        [sys.executable, "-I", "-B", str(program)],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout


@pytest.mark.parametrize(
    ("text", "package", "types"),
    [
        (
            "package a.b;\npublic class Solution { public static class Inner {} }",
            "a.b",
            [("Solution", True)],
        ),
        (
            "/* public class Fake {} */\n// public class Other\n"
            "class Main { String s = \"public class Nope {\"; char c = '{'; }\n"
            "record Pair(int a, int b) {}\nenum Colour { RED }\n",
            "",
            [("Main", False), ("Pair", False), ("Colour", False)],
        ),
        (
            'class Helper { public static class Main {} String t = """\n'
            'public class InText {\n"""; }\npublic final class Main {}\n',
            "",
            [("Helper", False), ("Main", True)],
        ),
        ("package  x . y ;\ninterface Shape {}\n", "x.y", [("Shape", False)]),
        (
            "/* c */ package p;\npublic  final\nclass Solution {}\npublic void x;\n"
            'final public class Twice {}\nclass Open { String t = """\n}',
            "p",
            [("Solution", True), ("Twice", True), ("Open", False)],
        ),
        ("", "", []),
    ],
)
def test_java_declarations(
    text: str, package: str, types: list[tuple[str, bool]]
) -> None:
    """Only top-level types count, and comments and strings never do."""
    assert java.java_declarations(text) == (package, types)


@pytest.mark.parametrize(
    ("name", "suffix", "used"),
    [
        ("main.cpp", ".cpp", "main.cpp"),
        ("solution_2.c", ".c", "solution_2.c"),
        ("-o.c", ".c", "main.c"),
        ("my file.py", ".py", "main.py"),
        ("prog.c", ".cpp", "main.cpp"),
        (".hidden.py", ".py", "main.py"),
    ],
)
def test_source_name(name: str, suffix: str, used: str) -> None:
    """The contestant's file name is kept when it cannot be read as an option."""
    assert prepare.source_name(Path("in") / name, suffix) == used


@pytest.mark.parametrize(
    ("path", "used"),
    [
        ("main.c", "main.c"),
        ("src/a.c", "src/a.c"),
        ("_util.py", "_util.py"),
        ("-o.c", "./-o.c"),
        ("@args.java", "./@args.java"),
        (".hidden/a.c", "./.hidden/a.c"),
    ],
)
def test_argument(path: str, used: str) -> None:
    """A path a compiler could read as an option or an argument file is
    prefixed with `./`."""
    assert shared.argument(path) == used


@pytest.mark.parametrize(
    ("language", "text", "holds"),
    [
        ("c", "int main(void) { return 0; }\n", True),
        ("c", "int main(int argc, char **argv)\n{\n  return 0;\n}\n", True),
        ("c", "int main(void);\nint helper(void) { return 1; }\n", False),
        ("c", "/* int main(void) { } */\nint f(void) { return 0; }\n", False),
        ("c", 'const char *s = "int main() {";\n', False),
        ("cpp", "auto main() -> int { return 0; }\n", True),
        ("cpp", "struct Game { void run(); };\nvoid Game::main() {}\n", False),
        ("cpp", "int f(Game g) { g.main(); return 0; }\n", False),
        ("java", "class A { public static void main(String[] a) {} }", True),
        ("java", "class A { static public void main(String... a) {} }", True),
        ("java", "class A { public void main(String[] a) {} }", False),
        ("java", "class A { // public static void main(String[] a)\n}", False),
        ("java", "class A { final public\n  static void main (String[] a) {} }", True),
        ("java", 'class A { String s = """\nstatic void main(\n"""; }', False),
        ("java", "class A { static int main(String[] a) {} }", False),
        ("c", "int main(void) { /* left open", True),
        ("c", "/* left open\nint main(void) { return 0; }\n", False),
        ("c", 'char *s = "open\nint main(void) { return 0; }\n', True),
        ("cpp", 'int main(int a = f("{")) { return 0; }\n', True),
        ("c", "int main(void) /* x */ ;\n", False),
        ("python", 'if __name__ == "__main__":\n    main()\n', True),
        ("python", "if '__main__' == __name__:\n    main()\n", True),
        ("python", "def main():\n    pass\nmain()\n", False),
        ("python", '    if __name__ == "__main__":\n        pass\n', False),
    ],
)
def test_holds_main(tmp_path: Path, language: str, text: str, holds: bool) -> None:
    """A file holds main by the language's own sign of a program's start."""
    path = tmp_path / "source"
    path.write_text(text)
    assert prepare.holds_main(path, language) is holds


@pytest.mark.parametrize(
    ("text", "language", "code"),
    [
        ("a /* b */ c // d\ne", "c", "a   c  \ne"),
        ('x = "a\\"b"; y', "c", "x =  ; y"),
        ("c = '\\''; d", "java", "c =  ; d"),
        ('s = "open\nnext', "c", "s =  \nnext"),
        ("a /* open", "c", "a  "),
        ("a / b // end", "c", "a / b  "),
        ('t = """\nq " "" \\""" x\n"""; u', "java", "t =  ; u"),
        ('t = """ open /* x', "java", "t =  "),
        ('a """b""" c', "c", "a     c"),
    ],
)
def test_code_only(text: str, language: str, code: str) -> None:
    """Each comment and literal becomes one space; one left open runs to the
    end of its line, or for a block comment or text block to the end."""
    assert shared.code_only(text, language) == code


@pytest.mark.parametrize(
    ("language", "text"),
    [
        ("c", "/*a" * 40000),
        ("java", "/*a" * 40000),
        ("java", 'class A { String s = """\n' + "a" * 200_000),
        ("java", 'class A { String s = """' + '""\\"' * 50_000),
        ("c", "'" * 200_000),
        ("java", "public " * 30_000),
        ("java", "static " * 30_000),
        ("java", "\n" * 200_000),
        ("java", "package " + "a." * 100_000),
        ("c", "main()" * 40_000),
    ],
)
def test_reading_a_source_takes_linear_time(
    tmp_path: Path, language: str, text: str
) -> None:
    """Sources shaped to make a backtracking search slow are read in well
    under a second."""
    path = tmp_path / "source"
    path.write_text(text)
    started = time.monotonic()
    prepare.holds_main(path, language)
    if language == "java":
        java.java_declarations(text)
    assert time.monotonic() - started < 1


def test_reading_the_folder_past_its_deadline_is_a_compile_error(
    tmp_path: Path,
) -> None:
    """Preparing the folder is stopped at its deadline as a failed compile
    with a line in the log."""
    folder = make_folder(tmp_path / "folder", {"main.py": "pass\n"})
    with pytest.raises(shared.CompileError) as raised:
        prepare.prepare(folder, tmp_path / "copy", "python", None, time.monotonic())
    assert str(raised.value) == (
        f"reading the source folder took longer than {prepare.PREPARE_SECONDS}"
        " seconds and was stopped"
    )


def test_a_folder_that_cannot_be_prepared_compiles_nothing(tmp_path: Path) -> None:
    """A prepare that fails is the compile's log and no program, before any
    box or the cache is asked."""
    (tmp_path / "folder").mkdir()
    built = build.build(tmp_path / "folder", "python", None, tmp_path / "work")
    assert built == build.Built("the source folder holds no files\n", None)
    assert built.outcome == "compile_error"


def test_folder_files_lists_regular_files_by_sorted_path(tmp_path: Path) -> None:
    """Nested files are listed by their path; links are left out."""
    folder = make_folder(tmp_path / "f", {"b.c": "", "a/z.c": "", "a/b/y.h": ""})
    (folder / "link.c").symlink_to(folder / "b.c")
    (folder / "linked").symlink_to(folder / "a", target_is_directory=True)
    assert prepare.folder_files(folder) == ["a/b/y.h", "a/z.c", "b.c"]


def test_a_folder_of_too_many_files_is_a_compile_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The number of files and folders is bounded before anything is copied."""
    monkeypatch.setattr(prepare, "FOLDER_LIMIT", 3)
    folder = make_folder(tmp_path / "f", {f"{n}.c": "" for n in range(4)})
    with pytest.raises(shared.CompileError, match="more than 3 files and folders"):
        prepare.folder_files(folder)


@pytest.mark.parametrize(
    ("entry", "used"),
    [("src/main.py", "src/main.py"), ("./src/main.py", "src/main.py")],
)
def test_entry_path(entry: str, used: str) -> None:
    """The entry is a path relative to the folder."""
    assert prepare.entry_path(entry, ["src/main.py"]) == used


@pytest.mark.parametrize(
    "entry", ["main.py", "/src/main.py", "src/../src/main.py", "src", "."]
)
def test_an_entry_not_in_the_folder_is_a_compile_error(entry: str) -> None:
    """Only a file of the folder can be the entry."""
    with pytest.raises(shared.CompileError, match="is not a file in the source"):
        prepare.entry_path(entry, ["src/main.py"])


@pytest.mark.parametrize(
    ("files", "language", "entry"),
    [
        ({"main.py": "x = 1\n", "data.txt": ""}, "python", "main.py"),
        (
            {"a.py": "", "b.py": 'if __name__ == "__main__":\n    pass\n'},
            "python",
            "b.py",
        ),
        ({"a.c": "int a;\n", "b/c.c": "int main(void) { return 0; }\n"}, "c", "b/c.c"),
        (
            {
                "A.java": "class A {}",
                "B.java": "class B { public static void main(String[] a) {} }",
            },
            "java",
            "B.java",
        ),
    ],
)
def test_find_entry(
    tmp_path: Path, files: dict[str, str], language: str, entry: str
) -> None:
    """With no entry named, the one source, or else the one holding main."""
    folder = make_folder(tmp_path / "f", files)
    sources = [name for name in files if name.endswith(shared.EXTENSIONS[language])]
    assert prepare.find_entry(folder, sorted(sources), language) == entry


@pytest.mark.parametrize(
    ("files", "language", "message"),
    [
        (
            {"a.py": "", "b.py": ""},
            "python",
            'no Python source checks __name__ == "__main__"',
        ),
        (
            {"a.c": "int main(void) { return 0; }", "b.c": "int main() {}"},
            "c",
            "a.c, b.c each define main",
        ),
        ({"notes.txt": "", "data.txt": ""}, "java", "holds no Java source file"),
    ],
)
def test_an_entry_that_is_not_clear_is_a_compile_error(
    tmp_path: Path, files: dict[str, str], language: str, message: str
) -> None:
    """None or several candidates ask for the entry to be named."""
    folder = make_folder(tmp_path / "f", files)
    sources = [name for name in files if name.endswith(shared.EXTENSIONS[language])]
    with pytest.raises(shared.CompileError, match=message):
        prepare.find_entry(folder, sorted(sources), language)


@pytest.mark.parametrize(
    ("files", "entry", "line"),
    [
        (
            {"a.py": 'if __name__ == "__main__": pass\n', "b.py": ""},
            "c.py",
            "the entry c.py is not a file in the source folder",
        ),
        (
            {"a.py": "", "notes.txt": ""},
            "notes.txt",
            "the entry notes.txt is not a Python source file (.py)",
        ),
        (
            {
                "a.py": 'if __name__ == "__main__": pass\n',
                "b.py": 'if __name__ == "__main__": pass\n',
            },
            None,
            'the entry is not clear: a.py, b.py each check __name__ == "__main__";'
            " name the file to start from as the entry",
        ),
        ({"main.py": ""}, "other.py", "the entry other.py is not a file"),
    ],
)
def test_an_entry_problem_is_a_compile_error(
    tmp_path: Path, files: dict[str, str], entry: str | None, line: str
) -> None:
    """A wrong or missing entry is the contestant's to fix: the log says how."""
    folder = make_folder(tmp_path / "folder", files)
    with pytest.raises(shared.CompileError) as raised:
        prepare.prepare(folder, tmp_path / "copy", "python", entry)
    assert str(raised.value).startswith(line)


def test_a_folder_that_cannot_be_copied_is_a_compile_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder too large for the scratch space is the contestant's."""

    def full(*args: object, **kwargs: object) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    folder = make_folder(tmp_path / "folder", {"a.py": "", "b.py": ""})
    monkeypatch.setattr(shutil, "copyfile", full)
    with pytest.raises(shared.CompileError) as raised:
        prepare.prepare(folder, tmp_path / "copy", "python", "a.py")
    assert str(raised.value) == (
        "the source folder could not be copied to compile it: No space left on device"
    )


@pytest.mark.parametrize(
    ("files", "named"),
    [
        ({"anything.java": "public class Solution {}"}, "Solution.java"),
        ({"x.java": "package a.b;\nclass Helper {}"}, "Main.java"),
        ({"x.c": "int main(void) { return 0; }"}, "x.c"),
        ({"my prog.cpp": "int main() {}"}, "main.cpp"),
    ],
)
def test_the_one_file_of_a_folder_is_copied_under_the_name_it_compiles_as(
    tmp_path: Path, files: dict[str, str], named: str
) -> None:
    """Java under its public class's name, the others under their own name
    when it is plain."""
    folder = make_folder(tmp_path / "folder", files)
    language = {"java": "java", "c": "c", "cpp": "cpp"}[named.rsplit(".", 1)[1]]
    sources = prepare.prepare(folder, tmp_path / "copy", language, None)
    assert sources.files == sources.sources == [named]
    assert (tmp_path / "copy" / named).is_file()


def test_a_main_beside_the_entry_is_left_out_for_c_and_cpp(tmp_path: Path) -> None:
    """A second `main`, a test driver, is not given the compiler."""
    files, entry = FOLDERS["cpp"]
    folder = make_folder(tmp_path / "folder", files)
    sources = prepare.prepare(folder, tmp_path / "copy", "cpp", entry)
    assert sources.sources == ["solution.cpp", "src/twice.cc"]
    assert sources.files == sorted(files)


def test_python_packs_to_a_zip_application(tmp_path: Path) -> None:
    """The program holds the source under `source/` and a launcher as
    `__main__.py`, behind a shebang line."""
    name, text = SOURCES["python"]
    program = packed(tmp_path, {name: text}, "python")
    assert program.read_bytes().startswith(b"#!/usr/bin/env python3\n")
    with zipfile.ZipFile(program) as archive:
        assert archive.read("source/main.py").decode() == text
        assert "__main__.py" in archive.namelist()
    assert run_python(program, "21\n") == "42\n"


def test_a_python_folder_keeps_its_layout(tmp_path: Path) -> None:
    """Every file goes in with its path, and the entry imports from its own
    folder."""
    files, entry = FOLDERS["python"]
    program = packed(tmp_path, files, "python", entry)
    with zipfile.ZipFile(program) as archive:
        names = set(archive.namelist())
    assert names == {"__main__.py", *(f"source/{name}" for name in files)}
    assert run_python(program, "21\n") == "42\n"


def test_a_python_entry_runs_as_the_main_module(tmp_path: Path) -> None:
    """The entry sees itself as `__main__`, with its own path as `__file__`."""
    files = {
        "pkg/__init__.py": "",
        "pkg/helper.py": "VALUE = 7\n",
        "run.py": "import sys\nfrom pkg.helper import VALUE\n"
        'if __name__ == "__main__":\n'
        "    print(VALUE, __file__.endswith('/source/run.py'),"
        " sys.modules['__main__'].__file__ == __file__)\n",
    }
    assert run_python(packed(tmp_path, files, "python"), "") == "7 True True\n"


def notebook_of(*cells: tuple[str, str | list[str]], **fields: Any) -> str:
    """A notebook of nbformat 4 holding `cells`, each its type and source."""
    document = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {},
        "cells": [
            {"cell_type": kind, "metadata": {}, "source": source}
            | ({"outputs": [], "execution_count": None} if kind == "code" else {})
            for kind, source in cells
        ],
    }
    return json.dumps(document | fields)


THREE_CELLS = notebook_of(
    ("markdown", "# Doubling\nRead a number, print twice it."),
    ("code", ["import sys\n", "n = int(sys.stdin.readline())"]),
    ("code", "print(n * 2)"),
)


def test_a_notebook_runs_as_one_program(tmp_path: Path) -> None:
    """Its code cells, in order, are one Python file, packed as a Python
    program is; its markdown is passed over."""
    program = packed(tmp_path, {"double.ipynb": THREE_CELLS}, "ipynb")
    assert run_python(program, "21\n") == "42\n"


def test_a_notebooks_code_names_each_cell() -> None:
    assert notebook.notebook_code(THREE_CELLS, "double.ipynb") == (
        "# coding: utf-8\n# cell 2\nimport sys\nn = int(sys.stdin.readline())\n"
        "# cell 3\nprint(n * 2)\n"
    )


def test_a_notebook_is_checked_with_the_python_beside_it(tmp_path: Path) -> None:
    """The joined notebook comes first among the sources the check is
    given, then the folder's Python files; it imports the modules beside
    it."""
    files = {
        "work/run.ipynb": notebook_of(
            ("code", "from twice import twice\nprint(twice(int(input())))")
        ),
        "work/twice.py": "def twice(n):\n    return 2 * n\n",
        "notes.txt": "kept\n",
    }
    folder = make_folder(tmp_path / "folder", files)
    sources = prepare.prepare(folder, tmp_path / "copy", "ipynb", None)
    assert sources.sources == ["work/run.ipynb", "work/twice.py"]
    assert run_python(packed(tmp_path / "packed", files, "ipynb"), "4\n") == "8\n"


def test_the_outputs_a_notebook_holds_are_passed_over(tmp_path: Path) -> None:
    text = json.loads(THREE_CELLS)
    text["cells"][2]["outputs"] = [
        {"output_type": "stream", "name": "stdout", "text": ["%pip install x\n"]}
    ]
    program = packed(tmp_path, {"double.ipynb": json.dumps(text)}, "ipynb")
    assert run_python(program, "5\n") == "10\n"


@pytest.mark.parametrize(
    ("cell", "shown"),
    [
        ("%pip install numpy", "%pip install numpy"),
        ("import os\n!ls -la", "!ls -la"),
        ("%%time\nx = 1", "%%time"),
        ("for i in range(2):\n    %time f(i)", "%time f(i)"),
        ("a = 1\x0cb = 2\nc = 3\n%who", "%who"),
    ],
)
def test_a_magic_is_a_compile_error_naming_its_cell(
    tmp_path: Path, cell: str, shown: str
) -> None:
    """There is no IPython when the program runs, so a magic or a shell
    command could not run there."""
    text = notebook_of(("markdown", "intro"), ("code", cell), ("code", "print(1)"))
    folder = make_folder(tmp_path / "folder", {"run.ipynb": text})
    with pytest.raises(shared.CompileError) as raised:
        prepare.prepare(folder, tmp_path / "copy", "ipynb", None)
    assert str(raised.value) == (
        f"cell 2 of the notebook run.ipynb runs {shown!r}, which only IPython"
        " runs; there is none when the program runs"
    )


@pytest.mark.parametrize(
    "cell",
    [
        'print("%d" % 3)',
        "x = (10\n     % 3)\nprint(x)",
        'text = """\n!not a command\n%neither\n"""\nprint(len(text))',
        "# !ls in a comment\nprint(1)",
    ],
)
def test_a_percent_or_bang_python_reads_is_no_magic(tmp_path: Path, cell: str) -> None:
    folder = make_folder(
        tmp_path / "folder", {"run.ipynb": notebook_of(("code", cell))}
    )
    prepare.prepare(folder, tmp_path / "copy", "ipynb", None)


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("{", "the notebook run.ipynb is not valid JSON"),
        ("[]", "the notebook run.ipynb is not a notebook of format 4"),
        (
            json.dumps({"nbformat": 3, "worksheets": []}),
            "the notebook run.ipynb is not a notebook of format 4",
        ),
        (
            json.dumps({"nbformat": 4, "cells": {}}),
            "the notebook run.ipynb is not a notebook of format 4",
        ),
        (
            notebook_of(("code", "print(1)"), ("code", 7)),  # type: ignore[arg-type]
            "cell 2 of the notebook run.ipynb has no source text",
        ),
        (
            notebook_of(("code", "print('\ud83d')")),
            "cell 1 of the notebook run.ipynb holds text that is not Unicode",
        ),
    ],
)
def test_a_notebook_that_is_not_one_is_a_compile_error(
    tmp_path: Path, text: str, line: str
) -> None:
    """What an entrant uploads is theirs, so a broken notebook is an outcome,
    never an `error`."""
    folder = make_folder(tmp_path / "folder", {"run.ipynb": text})
    with pytest.raises(shared.CompileError) as raised:
        prepare.prepare(folder, tmp_path / "copy", "ipynb", None)
    assert str(raised.value) == line


def test_two_notebooks_need_the_entry_named(tmp_path: Path) -> None:
    files = {
        "a.ipynb": notebook_of(("code", "print('a')")),
        "b.ipynb": notebook_of(("code", "print('b')")),
    }
    folder = make_folder(tmp_path / "folder", files)
    with pytest.raises(shared.CompileError) as raised:
        prepare.prepare(folder, tmp_path / "copy", "ipynb", None)
    assert str(raised.value) == (
        "the entry is not clear: the source folder holds 2 notebooks; name the"
        " one to run as the entry"
    )
    assert run_python(packed(tmp_path / "named", files, "ipynb", "b.ipynb"), "") == (
        "b\n"
    )


def test_a_notebook_saved_with_a_byte_order_mark_runs(tmp_path: Path) -> None:
    program = packed(tmp_path, {"double.ipynb": "﻿" + THREE_CELLS}, "ipynb")
    assert run_python(program, "4\n") == "8\n"


def test_a_coding_line_in_the_first_cell_does_not_change_how_it_is_read(
    tmp_path: Path,
) -> None:
    """The joined file is UTF-8 and says so on its first line, so a cell's
    `# -*- coding: latin-1 -*-`, its second, is passed over."""
    code = "# -*- coding: latin-1 -*-\nprint('café')"
    program = packed(tmp_path, {"run.ipynb": notebook_of(("code", code))}, "ipynb")
    assert run_python(program, "") == "café\n"


def test_a_notebook_that_is_not_utf_8_is_a_compile_error(tmp_path: Path) -> None:
    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "run.ipynb").write_bytes(
        notebook_of(("code", "print('x')")).encode() + b"\xff"
    )
    with pytest.raises(shared.CompileError) as raised:
        prepare.prepare(folder, tmp_path / "copy", "ipynb", None)
    assert str(raised.value) == "the notebook run.ipynb is not UTF-8 text"


def test_a_notebook_over_32_mb_is_refused_before_it_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = notebook_of(("code", "print(1)"))
    monkeypatch.setattr(notebook, "BINARY_LIMIT", len(text) - 1)
    folder = make_folder(tmp_path / "folder", {"run.ipynb": text})
    with pytest.raises(shared.CompileError) as raised:
        prepare.prepare(folder, tmp_path / "copy", "ipynb", None)
    assert str(raised.value) == "the notebook run.ipynb is larger than 32 MB"


def test_a_cell_that_is_not_an_object_is_passed_over() -> None:
    text = json.loads(notebook_of(("code", "print(1)")))
    text["cells"].insert(0, "stray")
    assert (
        notebook.notebook_code(json.dumps(text), "run.ipynb")
        == "# coding: utf-8\n# cell 2\nprint(1)\n"
    )


def test_the_cache_key_is_the_compile_and_nothing_else(tmp_path: Path) -> None:
    """Two folders that prepare to the same copy share a key, as one file
    whose name could be an option and one already named `main.py`; a
    change to a file's bytes, its path, the entry or the toolchain does
    not."""

    def key_of(
        files: dict[str, str], tools: str = "t", entry: str | None = None
    ) -> str:
        root = tmp_path / str(len(list(tmp_path.iterdir())))
        folder = make_folder(root / "folder", files)
        sources = prepare.prepare(folder, root / "copy", "python", entry)
        return cache.key(tools, "python", sources, root / "copy")

    files = {"a.py": 'if __name__ == "__main__": pass\n', "b.py": "x = 1\n"}
    first = key_of(files)
    assert key_of({"-o.py": "print(1)\n"}) == key_of({"main.py": "print(1)\n"})
    assert key_of(files | {"b.py": "x = 2\n"}) != first
    assert key_of({"a.py": files["a.py"], "c/b.py": files["b.py"]}) != first
    assert key_of(files, entry="b.py") != first
    assert key_of(files, tools="u") != first


@pytest.fixture
def boxes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give exec the tests' box users and step memory, or skip a test that
    compiles where exec cannot make boxes."""
    able = sys.platform == "linux" and os.geteuid() == 0
    if not (able and Path(box.ISOLATE).is_file()):
        if IN_BOXES:
            pytest.fail("the test container has no root with isolate")
        pytest.skip("compiles in isolate boxes: run in the test container")
    monkeypatch.setenv(box.BOX_USERS_VARIABLE, BOX_USERS)
    monkeypatch.setenv(box.STEP_MEMORY_VARIABLE, STEP_MEMORY_MB)


@pytest.fixture
def machine_cache(boxes: None) -> Iterator[Path]:
    """The machine's cache at `/cache`, laid out as the wrapper lays it out
    for a step given one, and removed after."""
    for name in ("programs", "locks"):
        (cache.CACHE / name).mkdir(parents=True)
    try:
        yield cache.CACHE
    finally:
        for name in ("programs", "locks"):
            shutil.rmtree(cache.CACHE / name)


@pytest.fixture
def compile_box(boxes: None, tmp_path: Path) -> Iterator[Path]:
    """The step set up and the compile box made, as `build` makes it, with
    an empty prepared copy; give the copy."""
    box.set_up_step(box.box_users(), box.step_memory_kb())
    copy = tmp_path / "copy"
    copy.mkdir()
    with box.fresh_box(build.COMPILE_BOX, build.COMPILE_BOX_KB) as folder:
        for name in (*box.BOX_FOLDERS, "out"):
            (folder / name).mkdir()
            (folder / name).chmod(0o1777)
        yield copy


def compile_step(
    work: Path,
    source: str | dict[str, str],
    language: str,
    entry: str | None = None,
    runs: tuple[str, ...] = ("main-1",),
) -> dict[str, dict[str, Any]]:
    """Run exec over a batch of the build form, each item compiling the
    one source folder `in/1/source`, and return each item's outputs by
    run."""
    write_source(work / "in" / "1" / "source", source, language)
    inputs: dict[str, Any] = {"source": {"folder": "in/1/source"}, "language": language}
    if entry is not None:
        inputs["entry"] = entry
    batch = [{"run": run, "inputs": inputs} for run in runs]
    document = {"schema_version": 6, "batch": batch}
    (work / "inputs.json").write_text(json.dumps(document))
    assert main(["exec", str(work)]) == 0
    result = json.loads((work / "outputs.json").read_text(encoding="utf-8"))
    assert "error" not in result, result
    return {entry["run"]: entry["outputs"] for entry in result["batch"]}


PYTHON = os.path.realpath(sys.executable)
"""The image's interpreter, which the box sees under `/usr`."""


def test_the_log_is_cut_at_its_limit(compile_box: Path, tmp_path: Path) -> None:
    """A compiler that prints without end leaves a log of bounded size."""
    command = [PYTHON, "-c", "import sys; sys.stdout.write('e' * 300000)"]
    tool = build.run_tool(command, compile_box, tmp_path)
    assert tool.ok
    assert tool.log.startswith("e" * shared.LOG_LIMIT + "\n[compile log cut at")
    assert f"{300000 - shared.LOG_LIMIT} more bytes left out" in tool.log


def test_a_compiler_that_runs_too_long_is_stopped(
    compile_box: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Going over the compile time is a failed compile with a note in the log."""
    monkeypatch.setattr(build, "COMPILE_SECONDS", 1)
    command = [
        PYTHON,
        "-c",
        "import time; print('started', flush=True); time.sleep(30)",
    ]
    tool = build.run_tool(command, compile_box, tmp_path)
    assert not tool.ok
    assert (
        tool.log == "started\n\ncompiling took longer than 1 seconds and was stopped\n"
    )


def test_a_compiler_over_its_memory_is_stopped(
    compile_box: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(build, "COMPILE_MEMORY_KB", 64 * 1024)
    command = [PYTHON, "-c", "x = bytearray(256 << 20)"]
    tool = build.run_tool(command, compile_box, tmp_path)
    assert not tool.ok
    assert tool.log.endswith(
        "\ncompiling took more than 64 MB of memory and was stopped\n"
    )


def test_a_compiler_writing_a_file_over_the_limit_is_stopped(
    compile_box: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(build, "BINARY_LIMIT", 1024 * 1024)
    command = [
        PYTHON,
        "-c",
        "import signal; signal.signal(signal.SIGXFSZ, signal.SIG_DFL);"
        " open('/tmp/big', 'wb').write(bytes(2 << 20))",
    ]
    tool = build.run_tool(command, compile_box, tmp_path)
    assert not tool.ok
    assert tool.log.endswith(shared.TOO_LARGE)


def test_a_command_line_too_long_to_start_is_a_compile_error(
    compile_box: Path, tmp_path: Path
) -> None:
    """Paths the contestant chose can make a command too long for the system."""
    command = [PYTHON, "-c", "pass", "x" * (256 * 1024)]
    tool = build.run_tool(command, compile_box, tmp_path)
    assert not tool.ok
    assert tool.log == "the source folder's paths are too long to compile\n"


def test_the_compiler_reads_the_source_and_cannot_change_it(
    compile_box: Path, tmp_path: Path
) -> None:
    """The prepared copy is the compiler's working directory, bound
    read-only, so not even a copy anyone may write changes."""
    compile_box.chmod(0o777)
    (compile_box / "main.c").write_text("int main(void) { return 0; }\n")
    (compile_box / "main.c").chmod(0o666)
    command = [
        PYTHON,
        "-c",
        "import os; print(os.getcwd(), open('main.c').read().strip(), flush=True);"
        " open('main.c', 'a')",
    ]
    tool = build.run_tool(command, compile_box, tmp_path)
    assert not tool.ok
    assert tool.log.startswith("/source int main(void) { return 0; }\n")
    assert "Read-only file system" in tool.log
    assert (compile_box / "main.c").read_text() == "int main(void) { return 0; }\n"


@pytest.mark.parametrize("language", ["c", "cpp", "java", "python"])
def test_each_language_compiles_a_folder(
    boxes: None, tmp_path: Path, language: str
) -> None:
    """A folder of nested sources gives one program, in the build form."""
    files, entry = FOLDERS[language]
    outputs = compile_step(tmp_path, files, language, entry)
    assert outputs == {"main-1": {"log": "", "outcome": "accepted"}}


@pytest.mark.parametrize("language", ["c", "cpp", "java", "python"])
def test_a_source_that_does_not_compile_is_a_compile_error(
    boxes: None, tmp_path: Path, language: str
) -> None:
    """The compiler's message is the log, and every item gives it."""
    name, text, expected = BROKEN[language]
    outputs = compile_step(tmp_path, {name: text}, language, runs=("a", "b"))
    assert outputs["a"] == outputs["b"]
    assert outputs["a"]["outcome"] == "compile_error"
    assert expected in outputs["a"]["log"]


def test_a_build_given_alone_is_answered_alone(boxes: None, tmp_path: Path) -> None:
    """A step the forge does once, such as a build at the save, gives its
    inputs alone, not in a batch, and exec answers with its outputs alone."""
    write_source(tmp_path / "in" / "1" / "source", "print(1)\n", "python")
    inputs = {"source": {"folder": "in/1/source"}, "language": "python"}
    document = {"schema_version": 6, "inputs": inputs}
    (tmp_path / "inputs.json").write_text(json.dumps(document))
    assert main(["exec", str(tmp_path)]) == 0
    result = json.loads((tmp_path / "outputs.json").read_text(encoding="utf-8"))
    assert result == {
        "schema_version": 6,
        "outputs": {"log": "", "outcome": "accepted"},
    }


def test_every_python_source_is_checked(boxes: None, tmp_path: Path) -> None:
    """A broken file the entry never imports is still a compile error."""
    files = {"main.py": "print(1)\n", "unused/broken.py": "def f(:\n"}
    outputs = compile_step(tmp_path, files, "python", "main.py")
    assert outputs["main-1"]["outcome"] == "compile_error"
    assert "broken.py" in outputs["main-1"]["log"]


def test_a_broken_module_beside_a_notebook_fails_the_compile(
    boxes: None, tmp_path: Path
) -> None:
    files = {"run.ipynb": notebook_of(("code", "print(1)")), "helper.py": "def f(:\n"}
    outputs = compile_step(tmp_path, files, "ipynb")
    assert outputs["main-1"]["outcome"] == "compile_error"
    assert "helper.py" in outputs["main-1"]["log"]


def test_a_syntax_error_in_a_cell_names_the_notebook(
    boxes: None, tmp_path: Path
) -> None:
    text = notebook_of(("code", "x = 1"), ("code", "print(x"))
    outputs = compile_step(tmp_path, {"run.ipynb": text}, "ipynb")
    assert outputs["main-1"]["outcome"] == "compile_error"
    assert "run.ipynb" in outputs["main-1"]["log"]
    assert "was never closed" in outputs["main-1"]["log"]


def test_java_with_no_class_to_run_is_a_compile_error(
    boxes: None, tmp_path: Path
) -> None:
    """The jar runs the entry's class; an entry that declares none looks for
    `Main`, and the log says it is not there."""
    files = {
        "A.java": "class A { public static void main(String[] a) {} }\n",
        "B.java": "// nothing here yet\n",
    }
    outputs = compile_step(tmp_path, files, "java", "B.java")
    assert outputs["main-1"] == {
        "log": "no class named Main to run; put main in a public class\n",
        "outcome": "compile_error",
    }


def test_a_program_over_the_limit_is_a_compile_error(
    boxes: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The zip application and the jar are written by exec, so their size is
    checked once written."""
    monkeypatch.setattr(build, "BINARY_LIMIT", 100)
    outputs = compile_step(tmp_path, "print(1)\n" * 100, "python")
    assert outputs["main-1"] == {"log": shared.TOO_LARGE, "outcome": "compile_error"}


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "/cache/locks/x", f"{box.BOX_ROOT}/x", "/work/x"]
)
def test_the_compiler_cannot_read_outside_its_box(
    machine_cache: Path, tmp_path: Path, path: str
) -> None:
    """A source naming a file of the container, the machine's cache, the
    folder every box is made in or the working directory finds nothing
    there, though each is there."""
    secrets = [machine_cache / "locks" / "x", box.BOX_ROOT / "x"]
    secrets.append(tmp_path / "x")
    for secret in secrets:
        secret.write_text("secret\n")
    text = f'#include "{path.replace("/work", str(tmp_path))}"\n'
    text += "int main(void) { return 0; }\n"
    try:
        outputs = compile_step(tmp_path, text, "c")
    finally:
        (box.BOX_ROOT / "x").unlink()
    assert outputs["main-1"]["outcome"] == "compile_error"
    assert "No such file or directory" in outputs["main-1"]["log"]
    assert "secret" not in outputs["main-1"]["log"]


@pytest.mark.parametrize("path", ["/proc/1/environ", "/proc/self/environ"])
def test_the_compiler_reads_nothing_of_the_steps_environment(
    machine_cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """The step's environment, where the wrapper holds the door's token,
    reaches no compile: a source including a process's environment is told
    none of it, the compiler's own being empty in its box."""
    monkeypatch.setenv("UNICON_DATA_TOKEN", "secret-token")
    text = f'#include "{path}"\nint main(void) {{ return 0; }}\n'
    outputs = compile_step(tmp_path, text, "c")
    assert "secret-token" not in json.dumps(outputs)


def test_a_second_compile_of_the_same_source_comes_from_the_cache(
    machine_cache: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The second step compiles nothing, and gets the same outputs."""
    name, text = SOURCES["c"]
    first = compile_step(tmp_path / "first", {name: text}, "c")
    assert "exec: compiled the c source" in capsys.readouterr().err
    (stored,) = (machine_cache / "programs").iterdir()
    key = stored.name
    assert (stored / "program").stat().st_mode & 0o777 == cache.PROGRAM_MODE
    assert json.loads((stored / "result.json").read_text())["outcome"] == "accepted"
    second = compile_step(tmp_path / "second", {name: text}, "c")
    said = capsys.readouterr().err
    assert f"exec: took the compiled program {key} from the cache" in said
    assert "exec: compiled" not in said
    assert second == first


def test_a_compile_error_is_cached_with_its_log(
    machine_cache: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    name, text, _ = BROKEN["c"]
    first = compile_step(tmp_path / "first", {name: text}, "c")
    capsys.readouterr()
    second = compile_step(tmp_path / "second", {name: text}, "c")
    assert "from the cache" in capsys.readouterr().err
    assert second == first
    assert first["main-1"]["outcome"] == "compile_error"


@pytest.mark.parametrize("damage", ["program", "result", "link"])
def test_a_cached_compile_that_is_not_whole_is_compiled_again(
    machine_cache: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    damage: str,
) -> None:
    """A folder of the cache that does not hold what it says is removed,
    compiled again and stored again."""
    name, text = SOURCES["c"]
    compile_step(tmp_path / "first", {name: text}, "c")
    (stored,) = (machine_cache / "programs").iterdir()
    key = stored.name
    program = stored / "program"
    if damage == "program":
        program.chmod(0o644)
        program.write_bytes(b"\x7fELF not this one")
    elif damage == "result":
        (stored / "result.json").write_text("{")
    else:
        program.unlink()
        program.symlink_to("/etc/passwd")
    capsys.readouterr()
    outputs = compile_step(tmp_path / "second", {name: text}, "c")
    said = capsys.readouterr().err
    assert f"exec: the cached compile {key} is not whole" in said
    assert "exec: compiled the c source" in said
    assert outputs == {"main-1": {"log": "", "outcome": "accepted"}}
    assert not program.is_symlink()
    sha256 = json.loads((stored / "result.json").read_text())["program_sha256"]
    assert cache.file_sha256(program) == sha256


def test_without_the_cache_every_step_compiles(
    boxes: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    name, text = SOURCES["python"]
    for step in ("first", "second"):
        compile_step(tmp_path / step, {name: text}, "python")
        assert "exec: compiled the python source" in capsys.readouterr().err


def test_a_compile_stopped_at_a_limit_is_not_cached(
    machine_cache: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A busy machine can stop a compile another would finish, so the next
    step compiles again rather than read the stop from the cache."""
    monkeypatch.setattr(build, "COMPILE_MEMORY_KB", 8 * 1024)
    name, text = SOURCES["cpp"]
    outputs = compile_step(tmp_path / "first", {name: text}, "cpp")
    assert outputs["main-1"]["log"].endswith("MB of memory and was stopped\n")
    assert list((machine_cache / "programs").iterdir()) == []
    capsys.readouterr()
    compile_step(tmp_path / "second", {name: text}, "cpp")
    assert "exec: compiled the cpp source" in capsys.readouterr().err


def test_a_cached_compile_missing_its_program_is_compiled_again(
    machine_cache: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    name, text = SOURCES["c"]
    compile_step(tmp_path / "first", {name: text}, "c")
    (stored,) = (machine_cache / "programs").iterdir()
    (stored / "program").unlink()
    capsys.readouterr()
    outputs = compile_step(tmp_path / "second", {name: text}, "c")
    said = capsys.readouterr().err
    assert f"exec: the cached compile {stored.name} is not whole" in said
    assert outputs == {"main-1": {"log": "", "outcome": "accepted"}}
    assert (stored / "program").is_file()


def test_a_program_left_as_a_link_is_no_program(
    compile_box: Path, tmp_path: Path
) -> None:
    """What the compiler leaves in `/box/out` is the box user's; a link
    there is read as no program, never followed."""
    out = box.BOX_ROOT / str(build.COMPILE_BOX) / "box" / "out"
    (out / "program").symlink_to("/etc/passwd")
    sources = shared.Sources(["main.c"], ["main.c"], "main.c")
    tool = build.take_program(
        build.Tool(0, ""), sources, compile_box, "c", out, tmp_path / "program"
    )
    assert tool == build.Tool(1, "the compiler wrote no program\n")
    assert not (tmp_path / "program").exists()


def test_a_python_program_that_cannot_be_written_is_a_compile_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The folder is the entrant's, so packing it failing is theirs too."""

    def full(*args: object) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(python, "write_zip_application", full)
    folder = make_folder(tmp_path / "folder", {"main.py": "print(1)\n"})
    sources = prepare.prepare(folder, tmp_path / "copy", "python", None)
    (tmp_path / "out").mkdir()
    tool = build.take_program(
        build.Tool(0, ""),
        sources,
        tmp_path / "copy",
        "python",
        tmp_path / "out",
        tmp_path / "program",
    )
    assert tool == build.Tool(1, shared.TOO_LARGE)


def test_a_source_over_32_mb_is_refused_before_it_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """exec reads a source whole to find the entry, so one that could make
    no program is the entrant's to fix, never exec's memory."""
    monkeypatch.setattr(shared, "BINARY_LIMIT", 10)
    folder = make_folder(
        tmp_path / "folder", {"a.c": "int x;\n", "b.c": "int y;\n" * 2}
    )
    with pytest.raises(shared.CompileError) as raised:
        prepare.prepare(folder, tmp_path / "copy", "c", None)
    assert str(raised.value) == "the source file b.c is larger than 32 MB"
