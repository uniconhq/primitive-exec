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
from dataclasses import dataclass, field
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
SIBLING_DIFF = ROOT.parent / "deploy" / "workflows" / "classic" / "v3" / "diff"
USERNS = Path(__file__).resolve().parent / "userns.py"
BOX_USERS = "60000-60009"
"""The box users the tests give exec: one step's range."""
STEP_MEMORY_MB = "1024"
"""The memory the in-process tests give exec's step, as the planner would
give a run of 512 MB, the most a test gives one, with its copies and the
256 MB room."""

RunImage = Callable[..., dict[str, Any]]
Check = Callable[..., None]
Compiled = Callable[[str, str, str], bytes]

SCORE = {"reports": {"score": {"better": "higher", "at_least": 0, "at_most": 1}}}
"""A manifest as the diff script's: a score between 0 and 1, higher better."""


def declaration(form: str = "one") -> dict[str, Any]:
    """This repo's primitive.yaml, the shared keys with one form's keys on top.

    `form` is `one` or `programs`. Readers that want today's exec read `one`.
    """
    document: dict[str, Any] = yaml.safe_load((ROOT / "primitive.yaml").read_text())
    forms = document.pop("forms")
    chosen = forms[form]
    document.pop("description", None)
    if "description" in chosen:
        document["description"] = chosen.pop("description")
    document.update(chosen)
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
    mounts the harness's, the tests' box users, that memory as the step's,
    and the network Docker gives it, as a Woodpecker step has one. Under
    root Docker, as on Docker Desktop and a CI machine, exec refuses to
    start, so the tests start it through `USERNS`.
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
        f"--env=UNICON_STEP_MEMORY_MB={limits['memory_mb']}",
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
elif case == "notify":
    import ctypes, errno

    libc = ctypes.CDLL(None, use_errno=True)
    for name, args in (
        ("inotify_init", ()),
        ("inotify_init1", (0,)),
        ("fanotify_init", (0x200, 0)),
    ):
        made = getattr(libc, name)(*args)
        print(name, "made" if made >= 0 else errno.errorcode[ctypes.get_errno()])
elif case == "filter":
    import ctypes, errno, platform

    libc = ctypes.CDLL(None, use_errno=True)

    def said(result):
        return "done" if result >= 0 else errno.errorcode[ctypes.get_errno()]

    class Rule(ctypes.Structure):
        _fields_ = [
            ("code", ctypes.c_ushort),
            ("jt", ctypes.c_ubyte),
            ("jf", ctypes.c_ubyte),
            ("k", ctypes.c_uint),
        ]

    class Filter(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("rules", ctypes.POINTER(Rule))]

    # PR_SET_NO_NEW_PRIVS is 38, PR_SET_SECCOMP 22, SECCOMP_MODE_FILTER 2,
    # and the one rule returns SECCOMP_RET_ALLOW for every call.
    print("clear no_new_privs", said(libc.prctl(38, 0, 0, 0, 0)))
    allow = (Rule * 1)(Rule(0x06, 0, 0, 0x7FFF0000))
    print("allow all", said(libc.prctl(22, 2, ctypes.byref(Filter(1, allow)), 0, 0)))
    add_key = {"x86_64": 248, "aarch64": 217}[platform.machine()]
    print("add_key", said(libc.syscall(add_key, b"user", b"left", b"secret", 6, -3)))
    print("inotify_init1", said(libc.inotify_init1(0)))
    child = os.fork()
    if child == 0:
        os._exit(0 if libc.inotify_init1(0) >= 0 else ctypes.get_errno())
    code = os.waitstatus_to_exitcode(os.waitpid(child, 0)[1])
    print("child inotify_init1", errno.errorcode.get(code, code))
    with open("/proc/self/status") as stream:
        status = dict(line.split(":", 1) for line in stream)
    for name in ("NoNewPrivs", "Seccomp", "Seccomp_filters", "CapPrm", "CapEff"):
        print(name, status[name].strip())
elif case == "cached":
    name = sys.stdin.readline().strip()
    print(os.readlink(name), open(name).read().strip())
    for change in (
        lambda: open(name, "w"),
        lambda: os.chmod(name, 0o666),
        lambda: open("/cache/new", "w"),
    ):
        try:
            change()
            print("changed")
        except OSError as error:
            print("refused", error.errno)
    for line in open("/proc/self/mountinfo"):
        fields = line.split()
        if fields[4] == "/cache":
            print("mount", fields[3], fields[5])
    print("listing", *sorted(os.listdir("/cache")))
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


SIBLING_VERDICT = (
    ROOT.parent / "deploy" / "workflows" / "interactive" / "v1" / "verdict"
)

GUESSES = {"reports": {"guesses": {"better": "lower", "at_least": 1}}}
"""The manifest of `JUDGE`'s guessing game: how many guesses the entrant
took."""

NOTES: dict[str, Any] = {"reports": {}}
"""A manifest declaring no report, for a test program that says what it saw
in its `message`."""

SECRET = {"secret": b"737\n"}
"""The judge's hidden input for the guessing game: the number to guess."""

GAME_LINKS = ["entrant -> judge", "judge -> entrant"]


@dataclass(frozen=True)
class Spec:
    """One program of a test item: its source, built as a Python zip
    application, its arguments, limits and named files, by name and
    content, the manifest it is given, and whether it stops the others."""

    source: str
    args: str
    time_limit: float = 1.0
    memory_limit: float = 64
    files: dict[str, bytes] = field(default_factory=dict)
    manifest: dict[str, Any] | None = None
    stops_others: bool = False


Programs = dict[str, Spec]
TestItem = tuple[str, Programs, list[str]]
"""A test item: its run, its programs by name, and its links."""


def programs_container_limits(items: list[TestItem]) -> dict[str, int]:
    """The container limits the harness gives a batch, from the declaration.

    Each limit is raised by `limits_from` for each item's programs, the sum
    or the largest of a number each takes, rounded up; a batch's time and
    CPU are summed over its items, and the others are the largest.
    """
    document = declaration("programs")
    limits: dict[str, int] = {}
    for name, floor in document["limits"].items():
        rule = document.get("limits_from", {}).get(name)
        values = []
        for _, programs, _ in items:
            value = floor
            if rule:
                over = rule.get("sum") or rule.get("max")
                numbers = [
                    getattr(spec, over.split(".")[1]) for spec in programs.values()
                ]
                total = sum(numbers) if "sum" in rule else max(numbers)
                raised = total * rule.get("scale", 1) + rule.get("add", 0)
                value = max(floor, math.ceil(raised))
            values.append(value)
        limits[name] = sum(values) if name in ("time_ms", "cpu_ms") else max(values)
    return limits


def programs_batch_inputs(
    work: Path, items: list[TestItem], binaries: dict[str, bytes] | None = None
) -> None:
    """Write inputs.json and every program and file under `in/`, as the
    harness does: each source once, as `in/programs/<k>`, and the `n`th
    item's program `p`'s named files and manifest under `in/<n>/<p>/`.
    `binaries` gives a program already built for a source, such as one from
    compile; any other source is built here as a zip application.
    """
    built = dict(binaries or {})
    stored: dict[str, str] = {}
    (work / "in" / "programs").mkdir(parents=True)
    batch = []
    for number, (run, programs, links) in enumerate(items, 1):
        given: dict[str, Any] = {}
        for name, spec in programs.items():
            if spec.source not in stored:
                if spec.source not in built:
                    build = work.parent / "build" / f"program-{len(built)}"
                    built[spec.source] = python_binary(build, spec.source).read_bytes()
                path = f"in/programs/{len(stored)}"
                (work / path).write_bytes(built[spec.source])
                stored[spec.source] = path
            folder = work / "in" / str(number) / name
            folder.mkdir(parents=True)
            inputs: dict[str, Any] = {
                "program": {"file": stored[spec.source]},
                "time_limit": spec.time_limit,
                "memory_limit": spec.memory_limit,
            }
            if spec.args:
                inputs["args"] = spec.args
            if spec.files:
                (folder / "files").mkdir()
                for file_name, content in spec.files.items():
                    (folder / "files" / file_name).write_bytes(content)
                inputs["files"] = {
                    "files": {
                        file_name: {"file": f"in/{number}/{name}/files/{file_name}"}
                        for file_name in spec.files
                    }
                }
            if spec.manifest is not None:
                (folder / "manifest.json").write_text(json.dumps(spec.manifest))
                inputs["manifest"] = {"file": f"in/{number}/{name}/manifest.json"}
            if spec.stops_others:
                inputs["stops_others"] = True
            given[name] = inputs
        entry: dict[str, Any] = {"programs": {"programs": given}}
        if links:
            entry["links"] = links
        batch.append({"run": run, "inputs": entry})
    document = {"schema_version": SCHEMA_VERSION, "batch": batch}
    (work / "inputs.json").write_text(json.dumps(document))


def facts_of(work: Path, outputs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """One item's `facts.json`, every program's facts by name."""
    document: dict[str, dict[str, Any]] = json.loads(
        (work / str(outputs["facts"]["file"])).read_text()
    )
    return document


def game(entrant: str, judge: str = "guess", **limits: float) -> Programs:
    """The guessing game: `ENTRANT` playing `entrant` against `JUDGE`
    playing `judge`, with the secret and `GUESSES`. `limits` gives either's
    time or memory limit as `entrant_time_limit` and the like; the judge's
    time limit is 2 s and the entrant's 1 s, so the entrant's wall clock
    runs out first, as `unicon/interactive@v1` asks."""
    return {
        "entrant": Spec(
            ENTRANT,
            entrant,
            time_limit=limits.get("entrant_time_limit", 1.0),
            memory_limit=limits.get("entrant_memory_limit", 64),
        ),
        "judge": Spec(
            JUDGE,
            judge,
            time_limit=limits.get("judge_time_limit", 2.0),
            memory_limit=limits.get("judge_memory_limit", 64),
            files=SECRET,
            manifest=GUESSES,
        ),
    }


ENTRANT = r"""
import os, sys, time

case = sys.argv[1]


def ask(guess):
    print(guess, flush=True)
    return sys.stdin.readline().strip()


def search(spin=0.0):
    low, high = 1, 1000
    while True:
        guess = (low + high) // 2
        end = time.process_time() + spin
        while time.process_time() < end:
            pass
        answer = ask(guess)
        if answer == "higher":
            low = guess + 1
        elif answer == "lower":
            high = guess - 1
        else:
            return


if case == "guess":
    search()
elif case == "slow":
    search(spin=0.4)
elif case == "wrong":
    while True:
        ask(1)
elif case == "leave":
    pass
elif case == "fail":
    sys.exit(3)
elif case == "crash":
    search()
    sys.exit(3)
elif case == "sleep":
    time.sleep(60)
elif case == "grow":
    blocks = []
    while True:
        blocks.append(b"x" * (8 * 1024 * 1024))
elif case == "unflushed":
    sys.stdout.write("500\n")
    sys.stdin.readline()
"""
"""The entrant, the case it plays its first argument: `guess` finds
`JUDGE`'s number by halving; `slow` does too, too slowly; `wrong` guesses 1
for ever; `leave` exits at once and `fail` exits 3 at once; `crash`
guesses right and exits 3; `sleep` waits for nothing; `grow` takes memory
without end; `unflushed` writes its guess without flushing it and waits for
an answer that never comes."""

JUDGE = r'''
import os, signal, sys, time

case = sys.argv[1]
report = os.environ["UNICON_REPORT"]


def put(name, value):
    with open(os.path.join(report, name), "w") as stream:
        stream.write(f"{value}\n")


def judge(verdict, message, **reports):
    put("verdict", verdict)
    put("message", message)
    for name, value in reports.items():
        put(name, value)


def tell(text):
    """Answer the entrant, which judges it on a closed pipe: it left."""
    try:
        print(text, flush=True)
    except BrokenPipeError:
        judge(1, "the program left")
        os._exit(0)


if case == "guess":
    secret = int(open("secret").read())
    for guesses in range(1, 21):
        line = sys.stdin.readline()
        if not line.strip():
            judge(1, "the program stopped guessing")
            break
        guess = int(line)
        if guess == secret:
            judge(0, "found", guesses=guesses)
            tell("correct")
            break
        tell("higher" if guess < secret else "lower")
    else:
        judge(1, "too many guesses")
    os._exit(0)
elif case == "crash":
    sys.exit(3)
elif case == "grow":
    blocks = []
    while True:
        blocks.append(b"x" * (8 * 1024 * 1024))
elif case == "pipe":
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    time.sleep(0.3)
    sys.stdout.write("y" * (1 << 20))
    sys.stdout.flush()
    judge(0, "it read it all", guesses=1)
elif case == "pipe-python":
    time.sleep(0.3)
    sys.stdout.write("y" * (1 << 20))
    sys.stdout.flush()
elif case in ("farewell", "farewell-native"):
    if case == "farewell-native":
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    secret = int(open("secret").read())
    guesses = 1
    while (guess := int(sys.stdin.readline())) != secret:
        print("higher" if guess < secret else "lower", flush=True)
        guesses += 1
    judge(0, "found", guesses=guesses)
    print("correct", flush=True)
    time.sleep(0.3)
    sys.stdout.write("bye " * (1 << 18))
    sys.stdout.flush()
elif case == "judge-pipe":
    judge(1, "no")
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    time.sleep(0.3)
    sys.stdout.write("y" * (1 << 20))
    sys.stdout.flush()
elif case == "judge-wait":
    judge(1, "no")
    time.sleep(60)
elif case == "accept-now":
    judge(0, "yes", guesses=1)
elif case == "accept-wait":
    judge(0, "yes", guesses=1)
    time.sleep(60)
elif case == "no-verdict":
    put("message", "forgot")
elif case == "bad-number":
    judge(0, "counted", guesses=0)
elif case == "word":
    sys.stdin.readline()
    judge("time_limit", "too slow")
'''
"""The judge, the case it plays its first argument: `guess` holds the
number in its `secret` file and answers each guess `higher`, `lower` or
`correct`, reporting how many it took, and a wrong answer after 20 or when
the entrant stops; `pipe` sleeps, then writes 1 MB with `SIGPIPE` as a
native program has it, and judges `0` if it lives; `pipe-python` does
the same as Python has it, so the write raises and it exits 1, with no
verdict; `farewell` plays `guess`, then after its `0` writes 1 MB of
goodbye to the entrant that has gone, and raises, and `farewell-native`
dies of `SIGPIPE` doing so; `judge-pipe` does as `pipe` after a `1`;
`crash` exits 3; `grow` takes memory without end; `judge-wait` writes a
`1` and waits; `accept-now` judges `0` and exits; `accept-wait` judges `0`
and waits; `no-verdict` and `bad-number` write a report that does not
hold; `word` reads a line and judges `time_limit`."""

PROGRAMS_PROGRAM = r'''
import errno, json, os, sys, time

case = sys.argv[1]
words = sys.argv[2:]


def note(text):
    """Say what this program saw, as its report's message."""
    folder = os.environ["UNICON_REPORT"]
    with open(os.path.join(folder, "message"), "w") as stream:
        stream.write(text)
    with open(os.path.join(folder, "verdict"), "w") as stream:
        stream.write("0")


def attempt(action):
    try:
        action()
        return "done"
    except OSError as error:
        return errno.errorcode[error.errno]


if case == "say":
    print(words[0], flush=True)
elif case == "add":
    print(sys.stdin.readline().strip(), words[0], flush=True)
elif case == "end":
    note(sys.stdin.readline().strip())
elif case == "ring":
    print(words[0], flush=True)
    note(sys.stdin.readline().strip())
elif case == "hub":
    to_y, from_y = open("to_y", "w"), open("from_y")
    print("a", flush=True)
    to_y.write("b\n")
    to_y.flush()
    got = [sys.stdin.readline().strip(), from_y.readline().strip()]
    note(f"{sorted(os.listdir('.'))} {sorted(os.listdir('/pipes'))} {got}")
elif case == "fill":
    with open("/tmp/fill", "wb") as stream:
        for _ in range(int(words[0])):
            stream.write(b"x" * (1024 * 1024))
            stream.flush()
    print("full", flush=True)
    time.sleep(60)
elif case == "free":
    sys.stdin.readline()
    found = os.statvfs("/tmp")
    free = found.f_bavail * found.f_frsize // (1024 * 1024)
    with open("/tmp/mine", "wb") as stream:
        for _ in range(int(words[0])):
            stream.write(b"x" * (1024 * 1024))
    note(f"free {free} wrote {words[0]}")
elif case == "hold":
    kept = bytearray(int(words[0]) * 1024 * 1024)
    for start in range(0, len(kept), 4096):
        kept[start] = 1
    print("held", flush=True)
    time.sleep(60)
elif case == "small":
    sys.stdin.readline()
    note("small")
elif case == "grow":
    if words:
        sys.stdin.readline()
    blocks = []
    while True:
        blocks.append(b"x" * (8 * 1024 * 1024))
elif case == "sleep":
    time.sleep(60)
elif case == "spin":
    while True:
        pass
elif case == "flood":
    line = "y" * 1023 + "\n"
    while True:
        sys.stdout.write(line)
elif case == "leave":
    pass
elif case == "secret":
    print("ready", flush=True)
    time.sleep(60)
elif case == "pry":
    sys.stdin.readline()
    other = words[0]
    found = [
        attempt(lambda: open(f"/var/local/lib/isolate/{other}/box/run/secret").close()),
        attempt(lambda: open("/proc/self/fd/0", "w").close()),
        attempt(lambda: open("/proc/self/fd/1").close()),
    ]
    pids = sorted(int(entry) for entry in os.listdir("/proc") if entry.isdigit())
    others = [pid for pid in pids if pid != os.getpid()]
    tried = []
    for pid in others:
        tried.append(attempt(lambda: open(f"/proc/{pid}/root/box/run/secret").close()))
        tried.append(attempt(lambda: os.listdir(f"/proc/{pid}/fd")))
    note(json.dumps({"found": found, "seen": len(others), "tried": sorted(set(tried))}))
elif case == "streams":
    found = [
        attempt(lambda: os.fchmod(1, 0o640)),
        attempt(lambda: open("/proc/self/fd/1").close()),
        attempt(lambda: open("/proc/self/fd/0", "w").close()),
    ]
    note(json.dumps(found))
elif case == "pipe-ways":
    found = [
        attempt(lambda: open("to_b").close()),
        attempt(lambda: open("from_b", "w").close()),
        attempt(lambda: os.chmod("to_b", 0o666)),
    ]
    note(f"{found}")
elif case == "swap-report":
    folder = os.environ["UNICON_REPORT"]
    os.rename(folder, folder + "-gone")
    os.symlink(words[0], folder)
elif case == "link-report":
    note("linked")
    os.symlink("/etc/hostname", os.path.join(os.environ["UNICON_REPORT"], "label"))
elif case == "leave-tmp":
    open("/tmp/left", "w").write("left")
    note("left")
elif case == "find-tmp":
    note(f"{sorted(os.listdir('/tmp'))} {sorted(os.listdir('.'))}")
elif case == "fill-at":
    folder = words[0]
    with open(os.path.join(folder, "fill"), "wb") as stream:
        while True:
            stream.write(b"x" * (1024 * 1024))
            stream.flush()
elif case == "many-at":
    folder = words[0]
    try:
        for made in range(200000):
            open(os.path.join(folder, str(made)), "w").close()
    except OSError:
        pass
elif case == "nest":
    try:
        while True:
            os.mkdir("d")
            os.chdir("d")
    except OSError:
        pass
elif case == "fork-bomb":
    made = 0
    try:
        while True:
            pid = os.fork()
            if pid == 0:
                time.sleep(60)
                os._exit(0)
            made += 1
    except OSError:
        note(f"stopped {made < 128}")
    time.sleep(60)
elif case == "dev":
    blocked = []
    for path in words:
        blocked.append(attempt(lambda: open(path).close()))
    note(json.dumps({"dev": sorted(os.listdir("/dev")), "blocked": blocked}))
elif case == "etc":
    note(json.dumps(sorted(os.listdir("/etc"))))
elif case == "net":
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2)
    note(attempt(lambda: sock.connect(("8.8.8.8", 53))))
elif case == "keyring":
    import ctypes
    import platform

    libc = ctypes.CDLL(None, use_errno=True)
    keyring = {"x86_64": (248, 249, 250), "aarch64": (217, 218, 219)}
    add_key, request_key, keyctl = keyring[platform.machine()]
    found = {}
    for name, made in (
        ("add_key", libc.syscall(add_key, b"user", b"leak", b"secret", 6, -3)),
        ("request_key", libc.syscall(request_key, b"user", b"leak", None, 0)),
        ("keyctl", libc.syscall(keyctl, 0, -3, 0)),
    ):
        found[name] = "made" if made >= 0 else errno.errorcode[ctypes.get_errno()]
    note(json.dumps(found))
elif case == "notify":
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    found = {}
    for name, args in (
        ("inotify_init", ()),
        ("inotify_init1", (0,)),
        ("fanotify_init", (0x200, 0)),
    ):
        made = getattr(libc, name)(*args)
        found[name] = "made" if made >= 0 else errno.errorcode[ctypes.get_errno()]
    note(json.dumps(found))
elif case == "filter":
    import ctypes
    import platform

    libc = ctypes.CDLL(None, use_errno=True)

    def said(result):
        return "done" if result >= 0 else errno.errorcode[ctypes.get_errno()]

    class Rule(ctypes.Structure):
        _fields_ = [
            ("code", ctypes.c_ushort),
            ("jt", ctypes.c_ubyte),
            ("jf", ctypes.c_ubyte),
            ("k", ctypes.c_uint),
        ]

    class Filter(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("rules", ctypes.POINTER(Rule))]

    # PR_SET_NO_NEW_PRIVS is 38, PR_SET_SECCOMP 22, SECCOMP_MODE_FILTER 2,
    # and the one rule returns SECCOMP_RET_ALLOW for every call.
    found = {"clear no_new_privs": said(libc.prctl(38, 0, 0, 0, 0))}
    allow = (Rule * 1)(Rule(0x06, 0, 0, 0x7FFF0000))
    found["allow all"] = said(libc.prctl(22, 2, ctypes.byref(Filter(1, allow)), 0, 0))
    add_key = {"x86_64": 248, "aarch64": 217}[platform.machine()]
    found["add_key"] = said(libc.syscall(add_key, b"user", b"leak", b"secret", 6, -3))
    found["inotify_init1"] = said(libc.inotify_init1(0))
    with open("/proc/self/status") as stream:
        status = dict(line.split(":", 1) for line in stream)
    for name in ("NoNewPrivs", "Seccomp", "Seccomp_filters", "CapPrm", "CapEff"):
        found[name] = status[name].strip()
    note(json.dumps(found))
elif case == "list":
    note(json.dumps(sorted(os.listdir(words[0]))))
'''
"""The general test program, the case it plays its first argument and the
rest its words: `say` prints a word; `add` reads a line and prints it with
a word added; `end` notes the line it reads; `ring` prints a word and notes
the line that comes back; `hub` opens `to_y` and `from_y`, writes to its
standard output and `to_y`, reads its standard input and `from_y`, and
notes them with its folders; `fill` writes megabytes into `/tmp`, says so
and waits; `free` reads a line, then notes the room its own `/tmp` has and
writes megabytes there; `hold` holds megabytes and waits; `small` reads a
line and notes it; `grow`, after reading a line if it has a word, `sleep`,
`spin`, `flood` and `leave` take memory, time or output, or leave; `secret`
says it is ready and waits; `pry` reads a line, then tries another box's
files, its own standard streams the wrong way, and each process it can
see, its root and its open files, noting the errno of each and how many it
saw; `pipe-ways` tries its named pipes the
wrong way and changing one's mode; `link-report` leaves a link in its
report; `streams` reads a line, then tries to change the mode of its
standard output and to reopen its standard streams the wrong way, noting
each errno; `swap-report` puts a link to its word where its report folder
was; `leave-tmp` and `find-tmp` leave a file in `/tmp` and look for one;
`fill-at` and `many-at` fill a folder with bytes or files until they
cannot; `nest` nests folders until it cannot; `fork-bomb` forks until it
cannot and notes whether it stopped under 128; `dev` lists
`/dev` and tries to open each path given; `etc` lists `/etc`; `net` tries
to connect out; `keyring` tries to add, find and use a key of its box
user's keyrings; `notify` tries to make an inotify and a fanotify
instance; `filter` tries to clear `no_new_privs` and to load a filter
allowing every call, then to add a key and make an inotify instance, and
notes its standing; `list` lists the folder given."""
