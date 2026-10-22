"""What the tests share: the declaration, the sandbox flags and test inputs."""

import ast
import inspect
import json
import math
import os
import subprocess
import textwrap
import zipapp
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
NAME = "primitive-exec"
SCHEMA_VERSION = 6
SIBLING_SCHEMA = ROOT.parent / "runner" / "schemas" / "primitive.schema.json"
SIBLING_RUNNER = ROOT.parent / "runner"
SIBLING_COMPILE = ROOT.parent / "primitive-compile"
SIBLING_DIFF = ROOT.parent / "deploy" / "workflows-next" / "classic" / "v3" / "diff"
USERNS = Path(__file__).resolve().parent / "userns.py"
BOX_USERS = "60000-60009"
"""The box users the tests give exec: one step's range."""

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


def step_flags(limits: dict[str, int]) -> list[str]:
    """The docker run flags an exec step container gets: privileged, so exec
    can make its boxes, with the declared memory, pids and CPU-time limits,
    one CPU, a `/tmp` mounted `noexec`, `nosuid` and `nodev`, as Docker
    mounts the harness's, the tests' box users, and the network Docker
    gives it, as a Woodpecker step has one. Under root Docker, as on Docker
    Desktop and a CI machine, exec refuses to start, so the tests start it
    through `USERNS`.
    """
    memory = f"{limits['memory_mb']}m"
    cpu_seconds = math.ceil(limits["cpu_ms"] / 1000)
    return [
        "--privileged",
        f"--memory={memory}",
        f"--memory-swap={memory}",
        f"--pids-limit={limits['pids']}",
        f"--ulimit=cpu={cpu_seconds}:{cpu_seconds}",
        "--cpus=1",
        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m",
        f"--env=UNICON_BOX_USERS={BOX_USERS}",
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
    files and directories, the container's own `/etc`, its own /tmp, and one
    system file.
    """
    paths = [
        f"{work}/out/1/output",
        f"{work}/in/2/input",
        f"{work}/inputs.json",
        f"{work}/out",
        work,
        "/etc/resolv.conf",
        "/tmp",
        "/usr/lib/os-release",
    ]
    return "\n".join(["peek", *paths, ""])


def peeked(work: str) -> list[str]:
    """What a `peek` run prints in a box: none of the step's paths is there to
    name, nor the container's `/etc`, and it lists its own /tmp and reads the
    system file."""
    return [
        f"refused {work}/out/1/output 2",
        f"refused {work}/in/2/input 2",
        f"refused {work}/inputs.json 2",
        f"refused {work}/out 2",
        f"refused {work} 2",
        "refused /etc/resolv.conf 2",
        "read /tmp",
        "read /usr/lib/os-release",
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
    try:
        os.fchmod(1, 0)
    except OSError:
        pass
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
elif case == "lock-scratch":
    for path, mode in (
        (os.path.dirname(os.path.abspath(sys.argv[0])), 0o500),
        (sys.argv[0], 0),
        ("/work/in/1/program", 0),
    ):
        try:
            os.chmod(path, mode)
        except OSError:
            pass
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
    for name, family, address in (
        ("AF_INET", socket.AF_INET, ("1.1.1.1", 443)),
        ("AF_INET6", socket.AF_INET6, ("2606:4700:4700::1111", 443)),
    ):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as made:
                made.settimeout(2)
                made.connect(address)
            print("connect", name, True)
        except OSError as error:
            print("connect", name, error.errno)
    one, two = socket.socketpair()
    one.sendall(b"x")
    print("socketpair AF_UNIX", two.recv(1) == b"x")
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
elif case == "find-key":
    import ctypes
    import platform
    libc = ctypes.CDLL(None, use_errno=True)
    request_key = {"x86_64": 249, "aarch64": 218}[platform.machine()]
    found = libc.syscall(request_key, b"user", b"left", None, 0)
    print("request_key", found >= 0 or ctypes.get_errno())
    with open("/proc/keys") as keys:
        print("listed", "left" in keys.read())
elif case == "devices":
    print("dev", sorted(os.listdir("/dev")))
    for path in sys.stdin.read().split():
        try:
            os.close(os.open(path, os.O_RDONLY))
            print("opened", path)
        except OSError as error:
            print("refused", path, error.errno)
elif case == "leave-files":
    for path in ("left", "/tmp/left", "/dev/shm/left"):
        with open(path, "w") as stream:
            stream.write("hidden answer")
    print("left")
elif case == "find-files":
    for path in ("left", "/tmp/left", "/dev/shm/left"):
        print(path, os.path.exists(path))
    print("box", sorted(os.listdir("/box")))
elif case == "fill":
    folder = sys.stdin.readline().strip()
    written = 0
    with open(os.path.join(folder, "fill"), "wb") as stream:
        while True:
            stream.write(b"z" * 2**20)
            stream.flush()
            written += 1
elif case == "files":
    folder = sys.stdin.readline().strip() or "."
    made = 0
    try:
        while True:
            open(os.path.join(folder, f"f{made}"), "w").close()
            made += 1
    except OSError as error:
        print("stopped", error.errno, made > 90000)
elif case == "nest":
    made = 0
    try:
        while True:
            os.mkdir("d")
            os.chdir("d")
            made += 1
    except OSError as error:
        print("nested", error.errno, made > 90000)
elif case == "append-stdin":
    try:
        with open("/proc/self/fd/0", "a") as again:
            again.write("changed")
        print("appended")
    except OSError as error:
        print("refused", error.errno)
elif case == "stdin-path":
    print(os.readlink("/proc/self/fd/0"), sys.stdin.read().strip())
elif case == "child-memory":
    child = os.fork()
    if child == 0:
        block = b"x" * (40 * 1024 * 1024)
        time.sleep(0.5)
        os._exit(0)
    os.waitpid(child, 0)
    print("waited")
elif case == "read-input":
    read = 0
    while chunk := sys.stdin.buffer.read(2**20):
        read += len(chunk)
    print("read", round(read / 2**20))
elif case == "fork-hold":
    children = 0
    try:
        while children < 200:
            if os.fork() == 0:
                time.sleep(3)
                os._exit(0)
            children += 1
    except OSError:
        pass
    print("forked", children, flush=True)
    time.sleep(3)
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
elif case == "report-swap":
    target = sys.stdin.readline().strip()
    folder = os.environ["UNICON_REPORT"]
    os.rename(folder, folder + "-gone")
    os.symlink(target, folder)
    print("swapped")
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


@cache
def base_build_args() -> tuple[str, ...]:
    """The `docker build` arguments naming the base image this repo's image
    builds on: `BASE_IMAGE`, or primitive-base built from a runner checkout
    beside this one, or none, which leaves the Dockerfile's own, the runner
    release's."""
    named = os.environ.get("BASE_IMAGE")
    if not named:
        dockerfile = SIBLING_RUNNER / "images" / "primitive-base" / "Dockerfile"
        if not dockerfile.is_file():
            return ()
        named = "primitive-base:test"
        command = ["docker", "build", "--quiet", "--tag", named]
        command += ["--file", str(dockerfile), str(SIBLING_RUNNER)]
        subprocess.run(command, check=True, capture_output=True)
    return ("--build-arg", f"BASE_IMAGE={named}")
