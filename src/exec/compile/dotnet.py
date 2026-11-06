"""C#: whether a source declares `Main`, the compiler's command line,
called directly, and the folder program of the assembly it writes with
the runtime configuration exec writes beside it.
"""

import json
import re
from pathlib import Path

from exec.compile.shared import (
    CompileError,
    Sources,
    argument,
    plain_files,
    write_folder,
)
from exec.shared import PrimitiveError

ASSEMBLY = "main.dll"
CONFIG = "main.runtimeconfig.json"
"""What `dotnet main.dll` reads the runtime to start from."""
FRAMEWORK = "Microsoft.NETCore.App"
CSHARP_MAIN = re.compile(
    r"(?<![A-Za-z0-9_])static\s++(?:async\s++)?(?:void|int|Task(?:\s*+<\s*+int\s*+>)?)"
    r"\s++Main\s*+\("
)
"""A `static` `Main` a program can start from, as C# allows it to return."""
CSHARP_NAME = r"@?[A-Za-z_][A-Za-z0-9_]*"
CSHARP_SCOPE = re.compile(
    r"[{};]"
    rf"|(?<![A-Za-z0-9_@.])namespace\s++({CSHARP_NAME}(?:\s*+\.\s*+{CSHARP_NAME})*+)"
    rf"|(?<![A-Za-z0-9_@.])(?:class|struct|interface|record)\s++"
    rf"(?:(?:class|struct)\s++)?({CSHARP_NAME})"
    rf"|({CSHARP_MAIN.pattern})"
)
"""A brace or semicolon, a namespace, a type declared, or a `static Main`."""


def holds_main(code: str) -> bool:
    """Whether C# code, its comments and literals blanked, declares a
    `static Main`."""
    return CSHARP_MAIN.search(code) is not None


def main_type(code: str) -> str | None:
    """The type whose `static Main` C# code, its comments and literals
    blanked, declares first, named with its namespaces and the types it is
    in, as `-main:` takes it, or None for none in a type. Braces are
    counted, each opening the namespace or type declared before it, if
    any, and a namespace ended by `;` holds the rest of the file. Each
    match is one token, so nothing is read twice."""
    scopes: list[tuple[str, bool] | None] = []
    outer: list[str] = []
    pending: tuple[str, bool] | None = None
    for match in CSHARP_SCOPE.finditer(code):
        token = match.group()
        if token == "{":
            scopes.append(pending)
            pending = None
        elif token == "}":
            if scopes:
                scopes.pop()
            pending = None
        elif token == ";":
            if pending is not None and not pending[1] and not scopes:
                outer = [pending[0]]
            pending = None
        elif match.group(1) or match.group(2):
            name = re.sub(r"[\s@]", "", match.group(1) or match.group(2))
            pending = (name, match.group(2) is not None)
        else:
            held = [each for each in scopes if each is not None]
            if not any(is_type for _, is_type in held):
                return None
            return ".".join([*outer, *(name for name, _ in held)])
    return None


def dotnet_folder(folder: Path) -> Path:
    """The locked dotnet's own folder in the assembled `folder`."""
    return folder / "lib" / "dotnet"


def host(folder: Path) -> str:
    """The locked dotnet's own `dotnet`, as a box sees it: conda puts none
    in `bin/`."""
    found = dotnet_folder(folder) / "dotnet"
    if found.is_symlink() or not found.is_file():
        raise PrimitiveError("the locked environment has no tool dotnet")
    return env_path(folder, found)


def env_path(folder: Path, path: Path) -> str:
    """`path`, in the assembled `folder`, as a box sees it under `/env`."""
    return "/env/" + path.relative_to(folder).as_posix()


def csc_command(sources: Sources, out: str, folder: Path) -> list[str]:
    """The command line that runs the locked SDK's compiler, `csc.dll`, on
    the locked dotnet, writing one assembly to `out`, every source in it,
    referencing each assembly of the runtime's reference pack: `dotnet
    build` cannot run in a box, which refuses `flock`."""
    root = dotnet_folder(folder)
    compilers = sorted(root.glob("sdk/*/Roslyn/bincore/csc.dll"))
    references = sorted(root.glob(f"packs/{FRAMEWORK}.Ref/*/ref/net*/*.dll"))
    if len(compilers) != 1 or not references:
        raise PrimitiveError(
            "the locked dotnet has no one C# compiler and its references"
        )
    return [
        host(folder),
        "exec",
        env_path(folder, compilers[0]),
        "-nologo",
        "-noconfig",
        "-optimize+",
        "-target:exe",
        f"-out:{out}",
        *([f"-main:{sources.main_class}"] if sources.main_class else []),
        *(f"-r:{env_path(folder, each)}" for each in references),
        *map(argument, sources.sources),
    ]


def runtime_config(folder: Path) -> bytes:
    """The runtime configuration of a program of the locked dotnet: its
    one runtime, at its version, for the framework its references are
    of."""
    root = dotnet_folder(folder)
    runtimes = sorted(path.name for path in (root / "shared" / FRAMEWORK).iterdir())
    frameworks = sorted(
        {path.name for path in root.glob(f"packs/{FRAMEWORK}.Ref/*/ref/net*")}
    )
    if len(runtimes) != 1 or len(frameworks) != 1:
        raise PrimitiveError("the locked dotnet has no one runtime")
    options = {
        "tfm": frameworks[0],
        "framework": {"name": FRAMEWORK, "version": runtimes[0]},
    }
    return json.dumps({"runtimeOptions": options}, indent=2).encode()


def packed(out: Path, folder: Path, target: Path) -> None:
    """The folder program of a C# compile: the assembly the compiler wrote
    to `out`, the compile box's out folder as exec reaches it, and its
    runtime configuration."""
    files = dict(plain_files(out))
    if ASSEMBLY not in files:
        raise CompileError("the compiler wrote no program")
    config = runtime_config(folder)
    write_folder([(ASSEMBLY, files[ASSEMBLY])], target, ASSEMBLY, {CONFIG: config})
