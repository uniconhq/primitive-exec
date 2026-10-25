"""What the tests share: the declaration, the sandbox flags and test inputs."""

import ast
import inspect
import json
import math
import textwrap
import zipapp
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
NAME = "primitive-exec"
SCHEMA_VERSION = 6
SIBLING_SCHEMA = ROOT.parent / "runner" / "schemas" / "primitive.schema.json"
SIBLING_COMPILE = ROOT.parent / "primitive-compile"
SIBLING_DIFF = ROOT.parent / "deploy" / "workflows-next" / "classic" / "v3" / "diff"

RunImage = Callable[..., dict[str, Any]]
Check = Callable[..., None]
Compiled = Callable[[str, str, str], bytes]

SCORE = {"reports": {"score": {"better": "higher", "at_least": 0, "at_most": 1}}}
"""A manifest as the diff script's: a score between 0 and 1, higher better."""


def declaration() -> dict[str, Any]:
    """This repo's primitive.yaml."""
    document: dict[str, Any] = yaml.safe_load((ROOT / "primitive.yaml").read_text())
    return document


def returned_outcomes(function: Callable[..., object]) -> set[str]:
    """Every outcome `function` can give, read from its source rather than
    from runs: each word a `return` gives, or, where it returns a mapping,
    the word under its "outcome", a lookup's keys left out. So a new way to
    an outcome is found though no test takes it.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        value: ast.expr | None = node.value
        if isinstance(value, ast.Dict):
            value = next(
                (
                    word
                    for key, word in zip(value.keys, value.values, strict=True)
                    if isinstance(key, ast.Constant) and key.value == "outcome"
                ),
                None,
            )
        if value is None:
            continue
        keys = {
            id(key)
            for part in ast.walk(value)
            if isinstance(part, ast.Dict)
            for key in part.keys
            if key is not None
        }
        found |= {
            part.value
            for part in ast.walk(value)
            if isinstance(part, ast.Constant)
            and isinstance(part.value, str)
            and id(part) not in keys
        }
    return found


def container_limits(
    time_limit: float, memory_limit: float, items: int
) -> dict[str, int]:
    """The container limits the harness gives a batch, from the declaration.

    Each limit is raised by `limits_from` for the item's inputs, rounded up;
    a batch's time and CPU are the per-item value times the number of items.
    """
    document = declaration()
    inputs = {"time_limit": time_limit, "memory_limit": memory_limit}
    limits: dict[str, int] = {}
    for name, value in document["limits"].items():
        rule = document.get("limits_from", {}).get(name)
        if rule:
            raised = inputs[rule["input"]] * rule.get("scale", 1) + rule.get("add", 0)
            value = max(value, math.ceil(raised))
        limits[name] = value * items if name in ("time_ms", "cpu_ms") else value
    return limits


def sandbox_flags(limits: dict[str, int]) -> list[str]:
    """The docker run flags the harness gives a step container."""
    memory = f"{limits['memory_mb']}m"
    cpu_seconds = math.ceil(limits["cpu_ms"] / 1000)
    file_bytes = limits["output_mb"] * 1024 * 1024
    return [
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--security-opt=seccomp=builtin",
        "--user=65532:65532",
        f"--memory={memory}",
        f"--memory-swap={memory}",
        f"--pids-limit={limits['pids']}",
        f"--ulimit=cpu={cpu_seconds}:{cpu_seconds}",
        f"--ulimit=fsize={file_bytes}:{file_bytes}",
        "--cpus=1",
        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m",
    ]


def open_up(work: Path) -> None:
    """Let user 65532 in the container write the working directory and read `in/`."""
    for path in [work, *work.rglob("*")]:
        path.chmod(0o777 if path.is_dir() else 0o666)


def python_binary(path: Path, source: str) -> Path:
    """Build a Python binary in compile's format at its simplest: a zip
    application whose root `__main__.py` is the program itself, where compile
    puts a launcher that runs the entry from the folder under `source/`.
    """
    app = path.parent / (path.name + ".app")
    app.mkdir(parents=True)
    (app / "__main__.py").write_text(source)
    zipapp.create_archive(app, path, interpreter="/usr/bin/env python3")
    return path


def batch_inputs(
    work: Path,
    binary: bytes,
    items: list[tuple[str, str]],
    time_limit: float = 1.0,
    memory_limit: float = 64,
    args: dict[str, str] | None = None,
    files: dict[str, dict[str, bytes]] | None = None,
    loads: dict[str, dict[str, bytes]] | None = None,
    manifest: dict[str, Any] | None = None,
    self_reported: bool = False,
) -> None:
    """Write inputs.json, the program and one input per item under `in/`, as the
    harness does: the program is `in/1/program` and the nth item's standard
    input `in/<n + 1>/input`. A `manifest`, given every item, is
    `in/manifest.json`, and `self_reported` is set on every item.

    `items` are (run, standard input) pairs, all run under the same limits;
    `args` gives the `args` input of the runs it names, and the others have
    none; `files` and `loads` give each run they name its named files, by
    name and content, under `in/<n + 1>/files/` and `in/<n + 1>/loads/`.
    """
    (work / "in" / "1").mkdir(parents=True)
    (work / "in" / "1" / "program").write_bytes(binary)
    if manifest is not None:
        (work / "in" / "manifest.json").write_text(json.dumps(manifest))
    batch = []
    for number, (run, stdin) in enumerate(items, 2):
        (work / "in" / str(number)).mkdir()
        (work / "in" / str(number) / "input").write_text(stdin)
        inputs: dict[str, Any] = {
            "program": {"file": "in/1/program"},
            "stdin": {"file": f"in/{number}/input"},
            "time_limit": time_limit,
            "memory_limit": memory_limit,
        }
        if args and run in args:
            inputs["args"] = args[run]
        if manifest is not None:
            inputs["manifest"] = {"file": "in/manifest.json"}
        if self_reported:
            inputs["self_reported"] = True
        for port, given in (("files", files), ("loads", loads)):
            if given and run in given:
                folder = work / "in" / str(number) / port
                folder.mkdir()
                for name, content in given[run].items():
                    (folder / name).write_bytes(content)
                inputs[port] = {
                    "files": {
                        name: {"file": f"in/{number}/{port}/{name}"}
                        for name in given[run]
                    }
                }
        batch.append({"run": run, "inputs": inputs})
    document = {"schema_version": SCHEMA_VERSION, "batch": batch}
    (work / "inputs.json").write_text(json.dumps(document))


def peek_input(work: str) -> str:
    """The standard input of a `peek` run after a first run: the paths it
    tries to read, which are the other run's output and input, the step's own
    files and directories, the rest of /tmp, and one system file.
    """
    paths = [
        f"{work}/out/1/output",
        f"{work}/in/2/input",
        f"{work}/inputs.json",
        f"{work}/out",
        work,
        "/tmp",
        "/etc/os-release",
    ]
    return "\n".join(["peek", *paths, ""])


def peeked(work: str) -> list[str]:
    """What a `peek` run prints when only the system file can be read."""
    return [
        f"refused {work}/out/1/output 13",
        f"refused {work}/in/2/input 13",
        f"refused {work}/inputs.json 13",
        f"refused {work}/out 13",
        f"refused {work} 13",
        "refused /tmp 13",
        "read /etc/os-release",
    ]


def by_run(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """A batch's outputs keyed by run."""
    return {entry["run"]: entry["outputs"] for entry in result["batch"]}


def printed(work: Path, outputs: dict[str, Any]) -> str:
    """What one item's program printed, from the file its `output` names."""
    return (work / str(outputs["output"]["file"])).read_text()


PROGRAM = """
import os, signal, sys, time
case = sys.stdin.readline().strip()
if case == "double":
    print(int(sys.stdin.readline()) * 2)
elif case == "spin":
    while True:
        pass
elif case == "sleep":
    time.sleep(60)
elif case == "grow":
    blocks = []
    while True:
        blocks.append(b"x" * (8 * 1024 * 1024))
elif case == "flood":
    line = "y" * 1023 + "\\n"
    while True:
        sys.stdout.write(line)
elif case == "fail":
    print("partial")
    sys.exit(3)
elif case == "argv":
    print(repr(sys.argv[1:]))
elif case == "escape":
    work = sys.stdin.readline().strip()
    for path in (work + "/escaped", work + "/inputs.json", "/tmp/escaped"):
        try:
            open(path, "w").close()
            print("wrote", path)
        except OSError as error:
            print("refused", path, error.errno)
    open("here", "w").write("fine")
    print("cwd", sorted(os.listdir(".")))
elif case == "peek":
    for path in sys.stdin.read().split():
        try:
            if os.path.isdir(path):
                os.listdir(path)
            else:
                open(path, "rb").close()
            print("read", path)
        except OSError as error:
            print("refused", path, error.errno)
elif case == "linger":
    if os.fork() == 0:
        os.setsid()
        time.sleep(60)
        os._exit(0)
    print("left one behind")
elif case == "chmod":
    work = sys.stdin.readline().strip()
    print("changed")
    sys.stdout.flush()
    for path in (work + "/out/1", work + "/out", work + "/in/3/input", work):
        try:
            os.chmod(path, 0)
        except OSError:
            pass
    os.fchmod(1, 0)
elif case == "deep":
    for _ in range(17):
        os.mkdir("a" * 255)
        os.chdir("a" * 255)
    print("deep")
elif case == "census":
    print(len([p for p in os.listdir("/proc") if p.isdigit()]))
elif case == "parricide":
    try:
        os.kill(os.getppid(), 9)
    except OSError as error:
        print("refused", error.errno, flush=True)
        os._exit(1)
    print("after")
elif case == "freeze":
    try:
        os.kill(os.getppid(), signal.SIGSTOP)
    except OSError:
        pass
    while True:
        pass
elif case == "hidden-work":
    if os.fork() == 0:
        while True:
            pass
    time.sleep(30)
elif case == "hidden-memory":
    child = os.fork()
    if child == 0:
        blocks = []
        while True:
            blocks.append(b"x" * (8 * 1024 * 1024))
    os.waitpid(child, 0)
elif case in ("split-memory", "undumpable-split-memory"):
    if case.startswith("undumpable"):
        import ctypes
        ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)
    child = os.fork()
    block = b"x" * (40 * 1024 * 1024)
    time.sleep(0.5)
    if child == 0:
        os._exit(0)
    os.waitpid(child, 0)
    print("held", 2 * len(block) // 1024 // 1024)
elif case in ("hold-memory", "shared-memory"):
    block = b"x" * (40 * 1024 * 1024)
    if case == "shared-memory" and os.fork() == 0:
        time.sleep(0.5)
        os._exit(0)
    time.sleep(0.5)
    print("held", len(block) // 1024 // 1024)
elif case in ("file-memory", "unlinked-memory", "memfd-memory"):
    block = b"x" * (24 * 1024 * 1024)
    chunk = b"y" * (1024 * 1024)
    for name in ("one", "two"):
        if case == "memfd-memory":
            kept = os.memfd_create(name)
        else:
            kept = os.open(name, os.O_RDWR | os.O_CREAT)
            if case == "unlinked-memory":
                os.unlink(name)
        for _ in range(24):
            os.write(kept, chunk)
    time.sleep(0.5)
    print("held", (len(block) + 48 * len(chunk)) // 1024 // 1024)
elif case == "pipe-memory":
    import fcntl
    block = b"x" * (24 * 1024 * 1024)
    pipes = []
    for _ in range(48):
        read_end, write_end = os.pipe()
        fcntl.fcntl(write_end, fcntl.F_SETPIPE_SZ, 1024 * 1024)
        os.set_blocking(write_end, False)
        os.write(write_end, b"y" * (1024 * 1024))
        pipes.append((read_end, write_end))
    time.sleep(0.5)
    print("held", (len(block) + 48 * 1024 * 1024) // 1024 // 1024)
elif case == "socket-memory":
    import socket
    block = b"x" * (24 * 1024 * 1024)
    pairs, sent = [], 0
    while sent < 48 * 1024 * 1024 and len(pairs) < 400:
        one, two = socket.socketpair()
        one.setblocking(False)
        try:
            while True:
                sent += one.send(b"y" * 65536)
        except BlockingIOError:
            pass
        pairs.append((one, two))
    time.sleep(0.5)
    print("held", (len(block) + sent) // 1024 // 1024)
elif case == "small-file":
    block = b"x" * (40 * 1024 * 1024)
    with open("note", "wb") as note:
        note.write(b"z" * (1024 * 1024))
    time.sleep(0.5)
    print("held", len(block) // 1024 // 1024)
elif case == "prying":
    for path in ("/proc/%d/fd/3" % os.getppid(), "/proc/1/mem"):
        try:
            open(path, "rb").close()
            print("opened", path)
        except OSError as error:
            print("refused", error.errno)
elif case == "interrupt":
    import signal
    try:
        os.kill(1, signal.SIGINT)
    except OSError as error:
        print("refused", error.errno)
    print("still here")
elif case == "leave-sysv":
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    kind, count = sys.stdin.readline().split()
    made = 0
    for key in range(0x5EED0000, 0x5EED0000 + int(count)):
        if kind == "shm":
            made += libc.shmget(key, 4096, 0o1600) >= 0
        elif kind == "msg":
            made += libc.msgget(key, 0o1600) >= 0
        else:
            made += libc.semget(key, 1, 0o1600) >= 0
    print("made", kind, made)
elif case == "find-sysv":
    kind = sys.stdin.readline().strip()
    with open("/proc/sysvipc/" + kind) as table:
        print("found", kind, len(table.read().splitlines()) - 1)
elif case == "mark":
    os.setxattr(sys.argv[0], "user.leak", b"hidden input")
    os.utime(sys.argv[0], (1000000000, 1000000000))
    print("marked")
elif case == "mark-paths":
    for path in sys.stdin.read().split():
        try:
            os.setxattr(path, "user.cache", b"x" * 1000)
            print("marked", path)
        except OSError as error:
            print("refused", path, error.errno)
elif case == "read-paths":
    for path in sys.stdin.read().split():
        try:
            print("found", path, len(os.getxattr(path, "user.cache")))
        except OSError as error:
            print("none", path, error.errno)
elif case == "bury":
    megabytes = int(sys.stdin.readline())
    for _ in range(17):
        os.mkdir("a" * 255)
        os.chdir("a" * 255)
    os.mkdir("box")
    os.chdir("box")
    for number in range(megabytes):
        with open(f"part-{number}", "wb") as part:
            part.write(b"z" * 2**20)
    os.chdir("..")
    os.chmod("box", 0)
    print("buried", megabytes)
elif case == "folders":
    for number in range(int(sys.stdin.readline())):
        os.mkdir(f"f{number}")
    print("made them")
elif case == "free":
    status = os.statvfs(".")
    print("free", status.f_bavail * status.f_frsize // 2**20)
elif case == "acl":
    import struct

    owner, user, group, mask, other = 1, 2, 4, 0x10, 0x20
    entries = [(owner, 6, -1), (user, 4, 4242), (user, 4, 4343), (group, 4, -1)]
    entries += [(mask, 4, -1), (other, 4, -1)]
    acl = struct.pack("<I", 2) + b"".join(
        struct.pack("<HHI", tag, perm, ident & 0xFFFFFFFF)
        for tag, perm, ident in entries
    )
    for path in sys.stdin.read().split():
        names = ["system.posix_acl_access"]
        if os.path.isdir(path):
            names.append("system.posix_acl_default")
        try:
            for name in names:
                os.setxattr(path, name, acl)
            print("set", path)
        except OSError as error:
            print("refused", path, error.errno)
elif case == "read-acl":
    for path in sys.stdin.read().split():
        found = [name for name in os.listxattr(path) if "posix_acl" in name]
        print("found" if found else "none", path)
elif case == "mark-and-lock":
    files = ["/work/in/3/input", "/work/out/1/output"]
    for path in files + ["/work/in/3"]:
        os.setxattr(path, "user.cache", b"x" * 1000)
    for path in files:
        os.chmod(path, 0o444)
    os.chmod("/work/in/3", 0)
    os.chmod("/work/out", 0)
    print("marked and locked")
elif case == "lock-scratch":
    os.chmod(os.path.dirname(os.path.abspath(sys.argv[0])), 0o500)
    os.chmod(sys.argv[0], 0)
    os.chmod("/work/in/1/program", 0)
    print("locked")
elif case == "leave-mq":
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    made = libc.mq_open(b"/leak", os.O_CREAT | os.O_RDWR, 0o600, None)
    print("made" if made >= 0 else "refused", ctypes.get_errno() if made < 0 else 0)
elif case == "find-mq":
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    found = libc.mq_open(b"/leak", os.O_RDONLY)
    print("found" if found >= 0 else "none", ctypes.get_errno() if found < 0 else 0)
elif case == "named":
    for name in sorted(os.listdir(".")):
        with open(name, "rb") as stream:
            data = stream.read()
        mode = oct(os.stat(name).st_mode & 0o777)
        print(name, mode, len(data), data[:16].decode(errors="replace").strip())
    for name in sys.stdin.read().split():
        try:
            open(name, "w").close()
            print("wrote", name)
        except OSError as error:
            print("refused", name, error.errno)
elif case == "spoil":
    os.chmod("answer", 0o644)
    with open("answer", "w") as stream:
        stream.write("spoiled")
    os.remove("answer")
    print("spoiled")
elif case == "load":
    sys.path.insert(0, os.getcwd())
    import solution
    print(solution.answer())
elif case == "sockets":
    import ctypes
    import socket
    libc = ctypes.CDLL(None, use_errno=True)
    families = [
        ("AF_INET", socket.AF_INET),
        ("AF_INET6", socket.AF_INET6),
        ("AF_NETLINK", socket.AF_NETLINK),
        ("AF_PACKET", socket.AF_PACKET),
        ("AF_UNIX", socket.AF_UNIX),
    ]
    for name, family in families:
        kind = socket.SOCK_RAW if family == socket.AF_PACKET else socket.SOCK_DGRAM
        made = libc.socket(family, kind, 0)
        print("socket", name, made >= 0 or ctypes.get_errno())
    pair = (ctypes.c_int * 2)()
    for name, family in (("AF_UNIX", socket.AF_UNIX), ("AF_INET", socket.AF_INET)):
        made = libc.socketpair(family, socket.SOCK_STREAM, 0, pair)
        print("socketpair", name, made == 0 or ctypes.get_errno())
    params = ctypes.create_string_buffer(120)
    ring = libc.syscall(425, 8, params)
    print("io_uring_setup", ring >= 0 or ctypes.get_errno())
    import platform
    keyring = {"x86_64": (248, 249, 250), "aarch64": (217, 218, 219)}
    add_key, request_key, keyctl = keyring[platform.machine()]
    made = libc.syscall(add_key, b"user", b"left", b"secret", 6, -3)
    print("add_key", made >= 0 or ctypes.get_errno())
    found = libc.syscall(request_key, b"user", b"left", None, 0)
    print("request_key", found >= 0 or ctypes.get_errno())
    session = libc.syscall(keyctl, 0, -3, 0)
    print("keyctl", session >= 0 or ctypes.get_errno())
elif case == "fork-bomb":
    children = 0
    try:
        while True:
            pid = os.fork()
            if pid == 0:
                time.sleep(60)
                os._exit(0)
            children += 1
    except OSError as error:
        print("stopped", error.errno, children < 128)
elif case == "report":
    folder = os.environ["UNICON_REPORT"]
    for line in sys.stdin.read().splitlines():
        name, _, value = line.partition(" ")
        with open(os.path.join(folder, name), "w") as stream:
            stream.write(value + "\\n")
    print("reported")
elif case == "report-then-fail":
    with open(os.path.join(os.environ["UNICON_REPORT"], "verdict"), "w") as stream:
        stream.write("0\\n")
    sys.exit(1)
elif case == "report-link":
    target = sys.stdin.readline().strip()
    folder = os.environ["UNICON_REPORT"]
    open(os.path.join(folder, "verdict"), "w").write("0")
    os.symlink(target, os.path.join(folder, "message"))
    print("linked")
elif case == "report-bad-name":
    folder = os.environ["UNICON_REPORT"].encode()
    open(folder + b"/verdict", "w").write("0")
    open(folder + b"/\\xff", "w").write("x")
elif case == "report-hardlink":
    target = sys.stdin.readline().strip()
    folder = os.environ["UNICON_REPORT"]
    open(os.path.join(folder, "verdict"), "w").write("0")
    open(os.path.join(folder, "score"), "w").write("1")
    try:
        os.link(target, os.path.join(folder, "message"))
        print("linked")
    except OSError as error:
        print("refused", error.errno)
elif case == "report-where":
    folder = os.environ.get("UNICON_REPORT")
    print("report", folder is not None and not folder.startswith(os.getcwd()))
    print("empty", os.listdir(folder) == [] if folder else None)
    open(os.path.join(folder, "verdict"), "w").write("0")
elif case == "no-report":
    print("report", os.environ.get("UNICON_REPORT"))
elif case == "read-mark":
    try:
        print("attribute", os.getxattr(sys.argv[0], "user.leak").decode())
    except OSError:
        print("no attribute")
    marked = os.stat(sys.argv[0]).st_mtime == 1000000000
    print("time", "marked" if marked else "fresh")
"""
