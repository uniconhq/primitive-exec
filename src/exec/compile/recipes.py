"""Every language exec compiles and runs, one recipe each, in `RECIPES`:
which files are its sources and which holds main, the compile and the
program it makes, the format that program is in and the command that runs
it, the environment and the box limits of both, and what a run's exit says.
"""

import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from exec.compile import dotnet, java, native, node, notebook, python
from exec.compile.environment import JAVA_LIBRARIES, Lock, tool
from exec.compile.shared import (
    CompileError,
    Sources,
    argument,
    code_only,
    copy_plain,
    write_folder,
)
from exec.shared import PrimitiveError, no_room

COMPILE_MEMORY_KB = 1024 * 1024
"""A compile box's memory, as compile's own container had, but where a
recipe needs more."""
PROCESSES = 128
"""A box user's processes and threads, compile or run (decision 11 of the
design: twice the most measured, and at least 128), which isolate counts
together; each recipe bounds the threads of a runtime that grows them with
the CPUs it sees."""
OUT = "/box/out"
"""Where a compiler writes, in the compile box's tmpfs."""
PROGRAM = "/box/program"
"""Where a run's box holds its copy of the program."""
NODE_MODULES = "/env/lib/node_modules"
"""Where the locked npm packages are, in the lock's tree layout."""
KOTLIN_LIBRARY = "/env/libexec/kotlin/lib/kotlin-stdlib.jar"
"""Kotlin's standard library, which a Kotlin program runs with, as the
locked compiler ships it."""
THREADS = {"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}
"""One BLAS thread, where it would start one for each CPU the box sees."""
JAVA = {"JAVA_HOME": "/env/lib/jvm"}
"""What conda's activation of the locked JDK would set."""
JVM_HEADROOM_KB = 40 * 1024
NODE_HEADROOM_KB = 64 * 1024
"""The memory a box gets beside the run's limit, for a runtime whose own
start takes it."""
DOTNET = {
    "DOTNET_ROOT": "/env/lib/dotnet",
    "PATH": "/env/lib/dotnet:/env/bin:/usr/local/bin:/usr/bin:/bin",
    "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
    "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
    "DOTNET_NOLOGO": "1",
    "HOME": "/tmp",
}
"""What conda's activation of the locked dotnet would set, its folder on
`PATH`, and every first-run and telemetry step off, with a writable home
in the box's `/tmp`, or dotnet looks for the network."""


@dataclass(frozen=True)
class Compile:
    """A compile as a recipe sees it: the sources as prepared, exec's copy
    of them, the compile box's `OUT` as exec reaches it, and the locked
    toolchain, its folder assembled."""

    sources: Sources
    copy: Path
    out: Path
    folder: Path
    lock: Lock


@dataclass(frozen=True)
class Launch:
    """A run's program as its recipe starts it: the folder of its locked
    toolchain, or None where the run needs none, what it starts from, read
    from the program (`Format.start`), and the run's memory limit."""

    folder: Path | None
    start: str | None
    memory_mb: int


@dataclass(frozen=True)
class Format:
    """A format a program is in: what it is called, whether a file is one,
    what it starts from, whether a box holds it unpacked, a folder at
    `PROGRAM`, rather than as the one file, and whether that file is run
    itself."""

    name: str
    detect: Callable[[Path], bool]
    start: Callable[[Path], str | None] = lambda _: None
    unpacked: bool = False
    executable: bool = False


@dataclass(frozen=True)
class Recipe:
    """One language: its name in a log, its sources' extensions, how a
    source that holds main is told (none where the entry is named or the
    folder's one source) and how the log names those, and for a one-file
    folder the name it is compiled under (`named`), else a plain name of
    its first extension; the compile command (none where nothing runs),
    the folders made for it under `OUT`, its environment, memory and
    processes, and how the program is made
    from what it wrote; links placed beside the program, in exec's copy
    for the compile and in each run's box; the program's format, the run
    command, without the entry's arguments, its environment, the memory a
    box gets beside the run's limit and its processes, isolate options of
    the language's own, and the exit code its runtime ends with when the
    program ran out of its memory."""

    name: str
    extensions: tuple[str, ...]
    command: Callable[[Compile], list[str] | None]
    program: Callable[[Compile, Path], None]
    format: Format
    run: Callable[[Launch], list[str]]
    holds_main: Callable[[str], bool] | None = None
    mains: tuple[str, str] = ("", "")
    named: Callable[[Path], tuple[str, str | None]] | None = None
    main_class: Callable[[str, str], str | None] | None = None
    alone: bool = False
    prepared: Callable[[Sources, Path], Sources] | None = None
    made: tuple[str, ...] = ()
    compile_env: Mapping[str, str] = field(default_factory=dict)
    compile_memory_kb: int = COMPILE_MEMORY_KB
    links: Mapping[str, str] = field(default_factory=dict)
    run_env: Mapping[str, str] = field(default_factory=dict)
    headroom_kb: int = 0
    processes: int = PROCESSES
    box: tuple[str, ...] = ()
    out_of_memory: int | None = None
    uses_folder: Callable[[Lock], bool] = lambda _: True
    """Whether a run needs the toolchain at `/env`: not a program linked
    whole, which a compile the cache held then never assembles."""

    @property
    def many(self) -> str:
        """How the log names several of the language's sources."""
        return "notebooks" if self.name == "notebook" else f"{self.name} sources"


def is_elf(path: Path) -> bool:
    """Whether `path` is an ELF executable."""
    with path.open("rb") as stream:
        return stream.read(4) == native.ELF_MAGIC


def zip_names(path: Path) -> set[str] | None:
    """The names in the zip archive at `path`, or None for no archive."""
    if not zipfile.is_zipfile(path):
        return None
    with zipfile.ZipFile(path) as archive:
        return set(archive.namelist())


def is_zip_application(path: Path) -> bool:
    """Whether `path` is a zip application, with a `__main__.py`."""
    return "__main__.py" in (zip_names(path) or set())


def is_jar(path: Path) -> bool:
    """Whether `path` is a jar, with a manifest."""
    return java.MANIFEST in (zip_names(path) or set())


def is_folder(path: Path) -> bool:
    """Whether `path` is a folder program: a zip archive whose comment
    names one of its files, the one the run starts from."""
    names = zip_names(path)
    return names is not None and folder_start(path) in names


def folder_start(path: Path) -> str:
    """The file a folder program starts from, which its comment names."""
    with zipfile.ZipFile(path) as archive:
        return archive.comment.decode("utf-8", "replace")


NATIVE = Format("a native executable", is_elf, executable=True)
ZIP_APPLICATION = Format("a Python zip application", is_zip_application)
JAR = Format("a Java jar", is_jar, java.jar_main_class)
FOLDER = Format("a folder of files", is_folder, folder_start, unpacked=True)


def copied_folder(c: Compile, target: Path) -> None:
    """The program of a language run from its sources: the folder as
    prepared, starting from the entry."""
    write_folder(
        [(path, c.copy / path) for path in c.sources.files], target, c.sources.entry
    )


def taken(c: Compile, target: Path, name: str = "program") -> None:
    """Take the one file the compiler wrote, `OUT/<name>`, as the
    program."""
    try:
        copy_plain(c.out / name, target)
    except (OSError, ValueError) as error:
        if isinstance(error, OSError) and (full := no_room(error)):
            raise full from error
        raise CompileError("the compiler wrote no program") from None


def nothing(c: Compile) -> None:
    """No compiler runs: the program is the folder, as prepared."""
    return None


def every_source(tool_name: str, *options: str) -> Callable[[Compile], list[str]]:
    """A check that runs `tool_name` with `options` over each source in turn,
    for a checker that takes one file, stopping at the first that fails."""

    def command(c: Compile) -> list[str]:
        loop = f'for f; do {tool_name} {" ".join(options)} "$f" || exit; done'
        return ["/bin/sh", "-c", loop, "sh", *map(argument, c.sources.sources)]

    return command


def packages(lock: Lock) -> bool:
    """Whether the lock lists conda packages of the task's own beside the
    toolchain, which a C or C++ program links."""
    return bool(lock.packages)


def native_run(launch: Launch) -> list[str]:
    """A native program runs as itself."""
    return [PROGRAM]


def python_run(launch: Launch) -> list[str]:
    """A zip application runs on the locked Python, isolated, writing no
    bytecode."""
    assert launch.folder is not None
    return [tool(launch.folder, "python3"), "-I", "-B", PROGRAM]


def jvm_run(launch: Launch, *libraries: str) -> list[str]:
    """A jar runs on the locked Java, its heap the run's memory limit, its
    threads bounded, with the locked jars and `libraries` on its class
    path beside it, from its manifest's main class: `-jar` would take the
    manifest's class path alone."""
    assert launch.folder is not None and launch.start is not None
    path = ":".join([PROGRAM, f"{JAVA_LIBRARIES}/*", *libraries])
    return [
        tool(launch.folder, "java"),
        f"-Xmx{max(16, launch.memory_mb)}m",
        "-Xss64m",
        "-XX:+UseSerialGC",
        "-XX:-UsePerfData",
        "-XX:+ExitOnOutOfMemoryError",
        java.JVM_THREADS,
        "-Djava.io.tmpdir=.",
        "-cp",
        path,
        launch.start,
    ]


def unpacked_run(name: str, *options: str) -> Callable[[Launch], list[str]]:
    """A folder program runs with the locked `name` and `options`, from the
    file it starts from."""

    def run(launch: Launch) -> list[str]:
        assert launch.folder is not None and launch.start is not None
        return [tool(launch.folder, name), *options, f"{PROGRAM}/{launch.start}"]

    return run


def node_run(launch: Launch) -> list[str]:
    """A JavaScript program runs on the locked Node, its heap the run's
    memory limit, as Java's is."""
    # V8 collects old garbage only as its heap nears its own limit. With
    # that limit past the box's, a loop dropping 8 MB arrays beside live
    # data of half the memory limit was killed by the box as memory_limit
    # at 128, 256 and 512 MB; with the heap at the limit it ran to the end,
    # its peak within the box's headroom for Node.
    heap = f"--max-old-space-size={max(16, launch.memory_mb)}"
    return unpacked_run("node", heap)(launch)


def dotnet_run(launch: Launch) -> list[str]:
    """A C# program runs on the locked dotnet, from its assembly."""
    assert launch.folder is not None and launch.start is not None
    return [dotnet.host(launch.folder), f"{PROGRAM}/{launch.start}"]


def held_jar(c: Compile, target: Path) -> None:
    """Take the jar kotlinc wrote, with a manifest naming the entry's
    class."""
    taken(c, target, "main.jar")
    try:
        java.kotlin_packed(target, c.sources)
    except (CompileError, zipfile.BadZipFile, OSError) as error:
        target.unlink(missing_ok=True)
        if isinstance(error, CompileError):
            raise
        if isinstance(error, OSError) and (full := no_room(error)):
            raise full from error
        raise CompileError("the compiler wrote no program") from None


def checked_main(java_text: str) -> bool:
    """Whether Java code declares a `static void main`."""
    return java.java_holds_main(code_only(java_text, "java"))


RECIPES: dict[str, Recipe] = {
    "c": Recipe(
        "C",
        (".c",),
        lambda c: native.command(c.sources, "c", f"{OUT}/program", c.folder, c.lock),
        taken,
        NATIVE,
        native_run,
        holds_main=lambda text: native.c_holds_main(code_only(text, "c")),
        mains=("defines main", "define main"),
        alone=True,
        uses_folder=packages,
    ),
    "cpp": Recipe(
        "C++",
        (".cpp", ".cc", ".cxx"),
        lambda c: native.command(c.sources, "cpp", f"{OUT}/program", c.folder, c.lock),
        taken,
        NATIVE,
        native_run,
        holds_main=lambda text: native.c_holds_main(code_only(text, "c")),
        mains=("defines main", "define main"),
        alone=True,
        uses_folder=packages,
    ),
    "java": Recipe(
        "Java",
        (".java",),
        lambda c: java.command(c.sources, f"{OUT}/classes", c.lock),
        lambda c, target: java.packed(c.out, c.sources, target),
        JAR,
        jvm_run,
        holds_main=checked_main,
        mains=("declares static void main", "declare static void main"),
        named=java.single_name,
        main_class=java.entry_class,
        made=("classes",),
        compile_env=JAVA,
        run_env=JAVA,
        headroom_kb=JVM_HEADROOM_KB,
        out_of_memory=java.OUT_OF_MEMORY,
    ),
    "kotlin": Recipe(
        "Kotlin",
        (".kt",),
        lambda c: java.kotlin_command(c.sources, f"{OUT}/main.jar", c.lock),
        held_jar,
        JAR,
        lambda launch: jvm_run(launch, KOTLIN_LIBRARY),
        holds_main=lambda text: java.kotlin_holds_main(code_only(text, "java")),
        mains=("declares fun main", "declare fun main"),
        main_class=java.kotlin_class,
        compile_env={**JAVA, "CONDA_PREFIX": "/env"},
        compile_memory_kb=3 * COMPILE_MEMORY_KB // 2,
        run_env=JAVA,
        headroom_kb=JVM_HEADROOM_KB,
        out_of_memory=java.OUT_OF_MEMORY,
    ),
    "python": Recipe(
        "Python",
        (".py",),
        lambda c: python.command(c.sources, "python3"),
        lambda c, target: python.write_zip_application(c.copy, c.sources, target),
        ZIP_APPLICATION,
        python_run,
        holds_main=lambda text: python.PYTHON_MAIN.search(text) is not None,
        mains=('checks __name__ == "__main__"', 'check __name__ == "__main__"'),
        run_env=THREADS,
    ),
    "ipynb": Recipe(
        "notebook",
        (".ipynb",),
        lambda c: python.command(c.sources, "python3"),
        lambda c, target: python.write_zip_application(c.copy, c.sources, target),
        ZIP_APPLICATION,
        python_run,
        prepared=notebook.joined,
        run_env=THREADS,
    ),
    "javascript": Recipe(
        "JavaScript",
        (".js", ".mjs", ".cjs"),
        every_source("node", "--check"),
        copied_folder,
        FOLDER,
        node_run,
        links={"node_modules": NODE_MODULES},
        headroom_kb=NODE_HEADROOM_KB,
        out_of_memory=node.OUT_OF_MEMORY,
    ),
    "typescript": Recipe(
        "TypeScript",
        (".ts", ".mts", ".cts"),
        lambda c: node.tsc_command(c.sources, f"{OUT}/{node.EMITTED}", c.lock),
        lambda c, target: node.emitted(c.out, c.copy, c.sources, target),
        FOLDER,
        node_run,
        compile_env={"CONDA_PREFIX": "/env"},
        links={"node_modules": NODE_MODULES},
        headroom_kb=NODE_HEADROOM_KB,
        out_of_memory=node.OUT_OF_MEMORY,
    ),
    "csharp": Recipe(
        "C#",
        (".cs",),
        lambda c: dotnet.csc_command(c.sources, f"{OUT}/{dotnet.ASSEMBLY}", c.folder),
        lambda c, target: dotnet.packed(c.out, c.folder, target),
        FOLDER,
        dotnet_run,
        holds_main=lambda text: dotnet.holds_main(code_only(text, "java")),
        mains=("declares static Main", "declare static Main"),
        main_class=lambda text, _: dotnet.main_type(code_only(text, "java")),
        compile_env=DOTNET,
        run_env=DOTNET,
        headroom_kb=32 * 1024,
    ),
    "rust": Recipe(
        "Rust",
        (".rs",),
        lambda c: native.rust_command(c.sources, f"{OUT}/program", c.folder),
        taken,
        NATIVE,
        native_run,
        holds_main=lambda text: native.rust_holds_main(code_only(text, "c")),
        mains=("defines fn main", "define fn main"),
    ),
    "r": Recipe(
        "R",
        (".R", ".r"),
        nothing,
        copied_folder,
        FOLDER,
        unpacked_run("Rscript"),
        run_env=THREADS,
        headroom_kb=72 * 1024,
        box=("--dir=etc/alternatives=/etc/alternatives",),
    ),
    "ruby": Recipe(
        "Ruby",
        (".rb",),
        every_source("ruby", "-c"),
        copied_folder,
        FOLDER,
        unpacked_run("ruby"),
        headroom_kb=16 * 1024,
    ),
}
LANGUAGES = tuple(RECIPES)


def recipe(language: str) -> Recipe:
    """The recipe of `language`, one of `LANGUAGES`."""
    return RECIPES[language]


def start_of(language: str, program: Path) -> str | None:
    """What `program`, compiled from `language`, starts from, refusing a
    file not in the language's format: a program the cache gave back that
    is not one is an error of the step."""
    form = RECIPES[language].format
    if not form.detect(program):
        raise PrimitiveError(f"the {language} program is not {form.name}")
    return form.start(program)


def unpack(program: Path, folder: Path, links: Mapping[str, str]) -> None:
    """Unpack the folder program at `program`, exec's own, into `folder`,
    readable by the run, with `links` beside its files."""
    folder.mkdir(mode=0o755)
    with zipfile.ZipFile(program) as archive:
        for member in archive.infolist():
            path = PurePosixPath(member.filename)
            if path.is_absolute() or ".." in path.parts or member.is_dir():
                raise PrimitiveError("the folder program holds an unsafe path")
            target = folder.joinpath(*path.parts)
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("xb") as copy:
                while chunk := source.read(1 << 20):
                    copy.write(chunk)
            target.chmod(0o644)
    for name, pointed in links.items():
        (folder / name).symlink_to(pointed)


def unpacked_kb(program: Path) -> int:
    """What the folder program at `program` takes in a box's tmpfs once
    unpacked, in kilobytes: each file to whole 4 KB pages, and a page for
    each folder."""
    with zipfile.ZipFile(program) as archive:
        members = archive.infolist()
    folders = {PurePosixPath(member.filename).parent for member in members}
    pages = sum(-(-member.file_size // 4096) for member in members) + len(folders)
    return 4 * pages
