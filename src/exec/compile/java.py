"""Java and Kotlin: a source's package and top-level types, the class the
jar runs, the compilers' command lines, and the jar.
"""

import re
import shutil
import zipfile
from pathlib import Path, PurePosixPath

from exec.compile.environment import JAVA_LIBRARIES, Lock
from exec.compile.shared import (
    BINARY_LIMIT,
    CompileError,
    Sources,
    argument,
    code_only,
    plain_files,
    read_text,
)

JAVA_NAME = r"[A-Za-z_$][A-Za-z0-9_$]*"
JAVA_FILE_NAME_LIMIT = 200
"""The longest public class name the source is saved under; a longer one could
not name a file, so javac is left to refuse it as a compile error.
"""
JAVA_PACKAGE = re.compile(
    rf"^[^\S\n]*package\s+({JAVA_NAME}(?:\s*\.\s*{JAVA_NAME})*)\s*;", re.M
)
JAVA_TYPE = r"(?:class|interface|enum|record)\s++"
JAVA_TOP_LEVEL = re.compile(
    r"[{}]|(?<![A-Za-z0-9_$])(?:"
    r"((?:(?:public|abstract|final|sealed|non-sealed|strictfp)\s++)++)"
    rf"(?:{JAVA_TYPE}({JAVA_NAME}))?|{JAVA_TYPE}({JAVA_NAME}))"
)
"""A brace, a run of type modifiers with the type it declares if any, or a
type declared without modifiers."""
KOTLIN_MAIN = re.compile(r"(?<![A-Za-z0-9_$.])fun\s++main\s*+\(")
"""A function named `main`: `fun main(`, not a member reached through `.`."""
KOTLIN_PACKAGE = re.compile(
    rf"^[^\S\n]*package\s+({JAVA_NAME}(?:\s*\.\s*{JAVA_NAME})*)", re.M
)
KOTLIN_JVM_NAME = re.compile(r"@file\s*:\s*JvmName\s*\(")
KOTLIN_JVM_NAMED = re.compile(rf'@file\s*:\s*JvmName\s*\(\s*"({JAVA_NAME})"\s*\)')
"""A file annotation naming the class of the file's top-level functions:
found in the code, its comments and literals blanked, and read from the
text, where its name is."""
JAR_UNPACKED = 8 * BINARY_LIMIT
"""The most kotlinc's jar may hold unpacked, which exec reads to write the
jar again with its own manifest."""
MANIFEST = "META-INF/MANIFEST.MF"
MAIN_CLASS = re.compile(r"^Main-Class:[ \t]*(\S+)[ \t]*\r?$", re.M)
OUT_OF_MEMORY = 3
"""The exit code of a JVM that ran out of heap, under
`-XX:+ExitOnOutOfMemoryError`."""
JVM_THREADS = "-XX:ActiveProcessorCount=2"
"""The CPUs a JVM sizes its threads by, whatever the box sees."""
JAVA_MAIN = re.compile(
    r"(?<![A-Za-z0-9_$])"
    r"((?:(?:public|protected|private|static|final|synchronized|strictfp)\s++)++)"
    r"(void\s++main\s*+\()?"
)
"""A run of method modifiers, with `void main(` if it follows."""


def java_holds_main(code: str) -> bool:
    """Whether Java code, its comments and literals blanked, declares a
    `static void main(`: `void main (` after a run of method modifiers that
    includes `static`.

    Each match takes a whole run of modifiers, so no run is read twice.
    """
    return any(
        match.group(2) and "static" in match.group(1).split()
        for match in JAVA_MAIN.finditer(code)
    )


def java_declarations(text: str) -> tuple[str, list[tuple[str, bool]]]:
    """Return a Java source's package and its top-level types.

    Comments and string literals are blanked first, then braces are counted so
    that only types declared outside every other type are listed, each with
    whether it is public. Each match takes a whole run of modifiers, so no
    run is read twice.
    """
    code = code_only(text, "java")
    package_match = JAVA_PACKAGE.search(code)
    package = re.sub(r"\s", "", package_match.group(1)) if package_match else ""
    types: list[tuple[str, bool]] = []
    depth = 0
    for match in JAVA_TOP_LEVEL.finditer(code):
        token = match.group()
        if token == "{":
            depth += 1
        elif token == "}":
            depth = max(0, depth - 1)
        elif depth == 0 and (name := match.group(2) or match.group(3)):
            types.append((name, "public" in (match.group(1) or "").split()))
    return package, types


def java_main_class(package: str, types: list[tuple[str, bool]]) -> str:
    """The class the jar runs, with its package: the entry's public top-level
    type, or else its top-level type named `Main`, or else its first top-level
    type.
    """
    public = [name for name, is_public in types if is_public]
    names = [name for name, _ in types]
    if public:
        main_class = public[0]
    elif "Main" in names or not names:
        main_class = "Main"
    else:
        main_class = names[0]
    return f"{package}.{main_class}" if package else main_class


def file_name(package: str, types: list[tuple[str, bool]]) -> str:
    """The name the only file of a folder is compiled under: its public
    top-level type's, as javac requires, or `Main.java` when there is none
    or its name could not name a file."""
    public = [name for name, is_public in types if is_public]
    named = public[0] if public and len(public[0]) <= JAVA_FILE_NAME_LIMIT else "Main"
    return f"{named}.java"


def kotlin_holds_main(code: str) -> bool:
    """Whether Kotlin code, its comments and literals blanked, declares a
    function `main`."""
    return KOTLIN_MAIN.search(code) is not None


def kotlin_class(text: str, name: str) -> str:
    """The class kotlinc compiles the top-level functions of the file
    `name` holding `text` into, with its package: the one its
    `@file:JvmName` names, or else the file's name without `.kt`, each
    character no letter or digit made `_`, a first ASCII letter made
    capital, and `Kt` after it, as kotlinc names it."""
    code = code_only(text, "java")
    package_match = KOTLIN_PACKAGE.search(code)
    package = re.sub(r"\s", "", package_match.group(1)) if package_match else ""
    named = KOTLIN_JVM_NAMED.search(text) if KOTLIN_JVM_NAME.search(code) else None
    if named:
        facade = named.group(1)
    else:
        stem = PurePosixPath(name).name.removesuffix(".kt")
        part = "".join(each if each.isalnum() else "_" for each in stem) or "_"
        if "a" <= part[0] <= "z":
            part = part[0].upper() + part[1:]
        facade = f"{part}Kt"
    return f"{package}.{facade}" if package else facade


def single_name(path: Path) -> tuple[str, str | None]:
    """The name the only Java file of a folder is compiled under, and the
    class the jar runs."""
    package, types = java_declarations(read_text(path))
    return file_name(package, types), java_main_class(package, types)


def entry_class(text: str, name: str) -> str:
    """The class the jar runs, from the entry's text."""
    return java_main_class(*java_declarations(text))


def class_path(lock: Lock) -> list[str]:
    """The class path option naming each locked Maven jar, or none."""
    jars = [f"{JAVA_LIBRARIES}/{jar['file_name']}" for jar in lock.maven]
    return ["-cp", ":".join(jars)] if jars else []


def command(sources: Sources, classes: str, lock: Lock) -> list[str]:
    """javac's command line, writing every source's classes under `classes`,
    the locked jars on its class path."""
    return [
        "javac",
        "-J-Xmx512m",
        "-J-XX:+UseSerialGC",
        "-J-XX:TieredStopAtLevel=1",
        "-J-XX:-UsePerfData",
        f"-J{JVM_THREADS}",
        "-encoding",
        "UTF-8",
        "-proc:none",
        "-Xlint:none",
        "-XDsuppressNotes",
        *class_path(lock),
        "-d",
        classes,
        *map(argument, sources.sources),
    ]


def kotlin_command(sources: Sources, jar: str, lock: Lock) -> list[str]:
    """kotlinc's command line, writing every source's classes into the jar
    `jar`, whose manifest names the class with main, the locked jars on its
    class path."""
    return [
        "kotlinc",
        f"-J{JVM_THREADS}",
        "-J-XX:+UseSerialGC",
        "-nowarn",
        *class_path(lock),
        "-d",
        jar,
        *map(argument, sources.sources),
    ]


def class_file(main_class: str) -> str:
    """Where javac writes `main_class`, under its classes folder."""
    return main_class.replace(".", "/") + ".class"


def write_jar(files: list[tuple[str, Path]], binary: Path, main_class: str) -> None:
    """Write the class files, each `(its name in the jar, where it is)`, into
    a jar that runs `main_class`."""
    manifest = f"Manifest-Version: 1.0\r\nMain-Class: {main_class}\r\n\r\n"
    with zipfile.ZipFile(binary, "w", zipfile.ZIP_DEFLATED) as jar:
        jar.writestr(MANIFEST, manifest)
        for name, path in files:
            jar.write(path, name)


def packed(out: Path, sources: Sources, target: Path) -> None:
    """Pack the classes javac wrote under `classes` in the compile box's
    out folder, `out` as exec reaches it, into a jar that runs the entry's
    class, refusing a compile that made no such class."""
    main_class = sources.main_class or "Main"
    classes = plain_files(out / "classes")
    if class_file(main_class) not in dict(classes):
        missing = f"no class named {main_class} to run"
        raise CompileError(f"{missing}; put main in a public class")
    write_jar(classes, target, main_class)


def kotlin_packed(jar: Path, sources: Sources) -> None:
    """Write the jar kotlinc wrote, taken to `jar`, again with a manifest
    naming the entry's class, refusing a compile that made no such class:
    kotlinc names a class itself only when one file holds main."""
    main_class = sources.main_class or "MainKt"
    written = jar.with_name(f"{jar.name}.partial")
    with zipfile.ZipFile(jar) as kotlinc:
        listed = [each for each in kotlinc.infolist() if each.filename != MANIFEST]
        if class_file(main_class) not in {each.filename for each in listed}:
            entry = PurePosixPath(sources.entry).name
            missing = f"no class {main_class} to run"
            raise CompileError(
                f"{missing}; declare fun main at the top level of {entry}"
            )
        if sum(each.file_size for each in listed) > JAR_UNPACKED:
            raise CompileError("the compiled classes are larger than 256 MB")
        manifest = f"Manifest-Version: 1.0\r\nMain-Class: {main_class}\r\n\r\n"
        with zipfile.ZipFile(written, "x", zipfile.ZIP_DEFLATED) as out:
            out.writestr(MANIFEST, manifest)
            for each in listed:
                with kotlinc.open(each) as source, out.open(each.filename, "w") as sink:
                    shutil.copyfileobj(source, sink)
    written.replace(jar)


def jar_main_class(path: Path) -> str | None:
    """The class the jar at `path` names in its manifest, if any."""
    with zipfile.ZipFile(path) as jar:
        try:
            manifest = jar.read(MANIFEST).decode("utf-8", "replace")
        except KeyError:
            return None
    found = MAIN_CLASS.search(manifest)
    return found.group(1) if found else None
