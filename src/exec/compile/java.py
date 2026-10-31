"""Java: a source's package and top-level types, the class the jar runs,
javac's command line, and the runnable jar.
"""

import re
import zipfile
from pathlib import Path

from exec.compile.shared import Sources, argument, code_only

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


def command(sources: Sources, classes: str) -> list[str]:
    """javac's command line, writing every source's classes under `classes`."""
    return [
        "javac",
        "-J-Xmx512m",
        "-J-XX:+UseSerialGC",
        "-J-XX:TieredStopAtLevel=1",
        "-J-XX:-UsePerfData",
        "-encoding",
        "UTF-8",
        "-proc:none",
        "-Xlint:none",
        "-XDsuppressNotes",
        "-d",
        classes,
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
        jar.writestr("META-INF/MANIFEST.MF", manifest)
        for name, path in files:
            jar.write(path, name)
