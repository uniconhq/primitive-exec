"""isolate and the step: setting the step container up for boxes, making
each box fresh and throwing it away, filling it, and the isolate command
line a program runs by.
"""

import contextlib
import ctypes
import math
import os
import re
import resource
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from stat import S_IMODE

from exec.inputs import Entry
from exec.shared import RUN_PATH, PrimitiveError

PROCESS_LIMIT = 128
"""The most processes and threads a run's box user may have at once
(`RLIMIT_NPROC`), below the container's own pids limit in `primitive.yaml`,
so a fork bomb stops at the box's limit and leaves exec and isolate room."""
NAMED_FILE_MODE = 0o444
ISOLATE = "/usr/local/bin/isolate"
LAUNCHER = "/usr/local/bin/box-launch"
"""The image's box launcher, from `src/box-launch.c`, which every program
starts through, in the box, under a seccomp filter of ours beside
isolate's: it refuses the keyring, inotify and fanotify calls, whose
kernel objects boxes share. Every box sees it under `/usr`."""
LAUNCHER_CHECK_KB = 64 * 1024
"""The memory, and the tmpfs, of the box the launcher's self-test runs in."""
ISOLATE_CONFIG = Path("/usr/local/etc/isolate")
"""Where the image's isolate reads its configuration, fixed when it is built.
exec writes it at the step's start, with the step's own box users."""
BOX_ROOT = Path("/var/local/lib/isolate")
LOCK_ROOT = Path("/run/isolate/locks")
CGROUP = Path("/sys/fs/cgroup")
"""The step container's cgroup, the root of its own cgroup namespace: a
privileged container on cgroup v2 sees its own cgroup there, writable."""
STEP_CGROUP = CGROUP / "step"
"""Everything the step runs, capped together at the memory the planner
gave the step (`set_up_cgroup`): exec's own processes in `main`, the
boxes in `isolate`. The container's own cgroup is the namespace's root,
whose limits only the machine sets."""
MAIN_CGROUP = STEP_CGROUP / "main"
BOX_CGROUPS = STEP_CGROUP / "isolate"
CONTAINER_MEMORY = CGROUP / "memory.max"
"""The container's memory limit in bytes, or `max` for none: the machine's
slot, which the step's own memory is never more than outside the tests."""
STEP_MEMORY_KB = 112 * 1024
"""What the step needs of its memory beside the boxes and the copies, at
the least: the image's step wrapper, unicon-step, the container's first
process, whose peak was measured at about 35 MB with 48 MB allowed for it,
then the primitive itself, isolate, and the kernel's own use, 64 MB."""
STEP_MEMORY_VARIABLE = "UNICON_STEP_MEMORY_MB"
CACHE_MOUNT = "/cache"
"""Where a box sees the item folder of the step's cache that its cached
named files are in, bound read-only."""
CONTROLLERS = "+cpu +memory"
BOX_DEV = Path("/run/boxdev")
"""The `/dev` every box gets in place of the container's, which in a
privileged container holds the machine's devices, its disks among them."""
BOX_DEVICES = ("null", "zero", "random", "urandom")
STDIN_MOUNT = Path("/run/boxstdin")
"""Where exec opens a run's `stdin` file: bound there, opened, and unbound
at once, so the path of the open file is `/`, never its own."""
BOX_LINKS = {
    "fd": "/proc/self/fd",
    "stdin": "/proc/self/fd/0",
    "stdout": "/proc/self/fd/1",
    "stderr": "/proc/self/fd/2",
}
BOX_USERS_VARIABLE = "UNICON_BOX_USERS"
BOX_USERS = re.compile(r"([0-9]{1,9})-([0-9]{1,9})")
FIRST_BOX_USER = 10_000
LAST_BOX_USER = 65_000
"""The range every step's box users lie in: above the image's system users
and below `nobody`, and inside the 65,536 IDs a rootless Docker's user maps."""
UID_MAP = Path("/proc/self/uid_map")
ROOT_DOCKER = ["0", "0", "4294967295"]
"""The first line of `/proc/self/uid_map` where the container's root is the
machine's own, as under root Docker: every user ID maps to itself."""
DEV_ROOT_DOCKER_VARIABLE = "UNICON_DEV_ROOT_DOCKER"
"""Set to `1` by a development stack alone, so exec runs under root Docker."""
BOX_INODES = 60_000
"""The most files and folders a box's tmpfs holds. Removing them takes
several times what making them did, on no run's clock, so a run that fills
them is stopped for its memory, which every entry in a tmpfs takes."""
BOX_ERROR_LIMIT = 4096
"""The most of isolate's own standard error an error of the step quotes."""
ISOLATE_STACK = 256 * 1024 * 1024
ISOLATE_OPEN_FILES = BOX_INODES + 1024
"""The stack and open files isolate needs: after a run it gives the box's
tree back to root by recursing into it with a folder open at each level,
and a run may nest folders as deep as the box's inode cap. A tree of about
100,000 folders took more than an 8 MB stack. The open-file count, the cap
plus a margin, stays under 65,536, the hard limit a container is given
where Docker sets none."""
BOX_FOLDERS = ("tmp", "shm")
"""The folders on the box's tmpfs that are the run's `/tmp` and `/dev/shm`,
so they share its size and inode cap: isolate's own tmpfs for them takes
no size or inode options, and held over a million files."""
JAVA_CONFIG = "/etc/java-21-openjdk"
"""The one folder of `/etc` a box sees: the Java runtime's own, where the
links in its installation lead. The rest of the container's `/etc` names
the container, its address and its name server."""
WRITE_BY_OTHERS = 0o022
MS_NOSUID = 2
MS_NODEV = 4
MS_BIND = 4096
MNT_DETACH = 2


@dataclass(frozen=True)
class Run:
    """What one program did, as isolate's meta file and the box's tmpfs tell
    it.

    `status` is isolate's: empty when the program exited 0, `RE` for another
    exit code, `SG` for a signal, `TO` for a time limit. `returncode` is the
    exit code, or minus the signal that killed it. `memory_kb` is the peak of
    the box's cgroup (`cg-mem`). `out_of_memory` says the kernel killed a
    process of the box for the box's memory, and `no_inodes` that the box's
    tmpfs had no file or folder left to give. `interrupted` says isolate
    stopped the box because exec asked, and `wall_clock` that the wall
    clock stopped it, which only the several-programs form tells.
    """

    status: str
    returncode: int
    cpu_seconds: float
    memory_kb: int
    output_bytes: int
    out_of_memory: bool = False
    no_inodes: bool = False
    interrupted: bool = False
    wall_clock: bool = False


NOT_STARTED = Run("RE", 127, 0.0, 0, 0)
"""A run that could not start, for its command line: a `runtime_error`."""


def cache_binding(folder: Path | None) -> list[str]:
    """isolate's option binding an item folder of the step's cache into the
    box as `CACHE_MOUNT`, read-only, as isolate binds by default, and never
    executable, or none for no folder."""
    if folder is None:
        return []
    return [f"--dir={CACHE_MOUNT.lstrip('/')}={folder}:noexec"]


def box_size_kb(entry: Entry) -> int:
    """The size of a program's box tmpfs, in kilobytes: its memory limit,
    which holds what the program writes there before the size does, as every
    page it writes is charged to the box's memory, plus the program and the
    named files exec copies in, which are charged to exec's, each to whole
    4 KB pages. A cached named file is not copied but bound in."""
    named = [*entry.files.values(), *entry.loads.values()]
    copied = [entry.program, *(path for path in named if path.parent != entry.cached)]
    return entry.memory_limit_kb + sum(
        4 * -(-path.stat().st_size // 4096) for path in copied
    )


def check_memory(needed_kb: int, memory_kb: int, run: str) -> None:
    """Refuse run `run`, before any program of the batch runs, when
    `needed_kb`, its boxes' memory limits and the files copied into their
    tmpfs, and `STEP_MEMORY_KB` are more than the step's memory,
    `memory_kb`, or the container's, when that is less. The limit would
    otherwise kill first: a program, which isolate reads as the box's own
    kill, a `memory_limit` that was not the program's, or the step itself,
    which then writes no outputs at all."""
    held_kb = memory_kb
    with contextlib.suppress(FileNotFoundError):
        text = CONTAINER_MEMORY.read_text().strip()
        if text != "max":
            held_kb = min(held_kb, int(text) // 1024)
    if needed_kb + STEP_MEMORY_KB > held_kb:
        raise PrimitiveError(
            f"the run {run} needs {math.ceil(needed_kb / 1024)} MB for its "
            "memory limits and the files copied in beside them, and the "
            f"step's {held_kb // 1024} MB leaves "
            f"{max(0, held_kb - STEP_MEMORY_KB) // 1024} MB for them"
        )


def place_files(cwd: Path, files: dict[str, Path], cached: Path | None) -> None:
    """Copy each named file into the run's working directory under its name,
    readable and not writable, before the run's clock starts, so the program
    opens `answer` or `input` as a plain relative path.

    Each run gets copies of its own, which go with its box, and nothing is
    read back from them, so a run that makes its copy writable again, as the
    copy's owner may, changes only what it reads itself.

    A file in `cached`, the item folder of the step's cache bound into the
    box, is not copied: its name is a link to it there, `CACHE_MOUNT`/<its
    name>, which the run reads and cannot change.
    """
    cwd.mkdir(mode=0o755)
    for name, source in files.items():
        placed = cwd / name
        if source.parent == cached:
            placed.symlink_to(Path(CACHE_MOUNT) / source.name)
            continue
        try:
            shutil.copyfile(source, placed)
        except OSError as error:
            raise PrimitiveError(
                f"the named file {name} could not be placed: {error.strerror}"
            ) from error
        placed.chmod(NAMED_FILE_MODE)


def isolate_command(
    number: int,
    meta: Path,
    entry: Entry,
    reports: bool,
    more: list[str],
    run: list[str],
) -> list[str]:
    """The isolate command line that runs `run`, one program, in box
    `number`, already made and filled, writing its meta file to `meta`.

    isolate holds every limit: CPU time over all the box's processes
    (`--time`), the wall clock (`--wall-time`), the box's memory in its
    cgroup, without swap (`--cg-mem`), the stack at the memory limit, and the
    box user's processes (`--processes`). The box gets `/tmp` and `/dev/shm`
    from its own tmpfs, the step's `/dev` of four devices, and of `/etc` only
    `JAVA_CONFIG`, read-only, then the options in `more`, the caller's own.
    The environment holds only `PATH`, `HOME` and `TMPDIR`, both the working
    directory, `LANG=C.UTF-8`, and for a program given a manifest
    (`reports`) `UNICON_REPORT`. Standard error goes nowhere. The program
    starts through `LAUNCHER`, which runs it with that command line and
    environment once its filter is on.
    """
    env = {"PATH": RUN_PATH, "HOME": "/box/run", "TMPDIR": "/box/run"}
    env["LANG"] = "C.UTF-8"
    if reports:
        env["UNICON_REPORT"] = "/box/report"
    box = BOX_ROOT / str(number) / "box"
    return [
        ISOLATE,
        "--cg",
        f"--box-id={number}",
        "--silent",
        f"--meta={meta}",
        f"--time={entry.time_limit}",
        f"--wall-time={entry.wall_limit}",
        f"--cg-mem={entry.memory_limit_kb}",
        f"--stack={entry.memory_limit_kb}",
        f"--processes={PROCESS_LIMIT}",
        f"--dir=tmp={box / 'tmp'}:rw",
        f"--dir=dev={BOX_DEV}:dev",
        f"--dir=dev/shm={box / 'shm'}:rw",
        f"--dir={JAVA_CONFIG.lstrip('/')}",
        *more,
        "--chdir=/box/run",
        "--stderr=/dev/null",
        *(f"--env={name}={value}" for name, value in env.items()),
        "--run",
        "--",
        LAUNCHER,
        *run,
    ]


def open_stdin(path: Path) -> int:
    """Open a run's `stdin` file through `STDIN_MOUNT`, bound there and
    unbound once open. A box reads the path its standard input was opened
    at through `/proc/self/fd/0`, and the file's own path under `/work/in`
    may say which test it is; the path of a file opened through a mount
    that is gone is `/`."""
    STDIN_MOUNT.touch()
    mount(path, STDIN_MOUNT, None, MS_BIND, None)
    try:
        return os.open(STDIN_MOUNT, os.O_RDONLY)
    finally:
        unmount(STDIN_MOUNT, MNT_DETACH)


def hand_over(descriptor: int, run: str, users: range) -> None:
    """Make run `run`'s `stdin` file, open on `descriptor`, safe to hand to
    the box: the box can open it again through `/proc/self/fd/0`, by the
    file's own mode and owner, so write for its group and others is taken
    away, and a file one of the step's box users owns, who could write it
    whatever its mode, is an error of the step."""
    found = os.fstat(descriptor)
    if found.st_uid in users:
        raise PrimitiveError(
            f"the stdin file of run {run} is owned by user {found.st_uid}, "
            "a box user, who could change it from a box"
        )
    if found.st_mode & WRITE_BY_OTHERS:
        os.fchmod(descriptor, S_IMODE(found.st_mode) & ~WRITE_BY_OTHERS)


def isolate_said(code: int, said: str | bytes) -> str:
    """What isolate said when it failed, `said` its message or its standard
    error, or failing that how it ended: exit `code`, or a signal when
    negative."""
    if isinstance(said, bytes):
        said = said[:BOX_ERROR_LIMIT].decode("utf-8", "replace")
    said = said.strip()
    if said:
        return said
    if code < 0:
        return f"it was killed by signal {-code} and said nothing"
    return f"it exited with code {code} and said nothing"


def read_meta(path: Path) -> dict[str, str]:
    """isolate's meta file, `key:value` a line, as keys and values; empty
    where isolate wrote none, as when it failed before the run."""
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return {}
    return dict(line.partition(":")[::2] for line in lines if ":" in line)


def exit_code(found: dict[str, str], otherwise: int) -> int:
    """How a program ended, from isolate's meta file read as `found`: its
    exit code, or minus the signal that killed it, or `otherwise` where the
    meta file says neither."""
    if "exitcode" in found:
        return int(found["exitcode"])
    if "exitsig" in found:
        return -int(found["exitsig"])
    return otherwise


def quietly(cleanup: Callable[[], None]) -> None:
    """Run `cleanup` while an error that ends the batch is on its way out: an
    error of its own goes to standard error, so it does not hide the first.
    The container goes with the batch, so what it could not undo stays with
    no later run."""
    try:
        cleanup()
    except Exception as error:
        print(f"exec: cleaning up after the error failed too: {error}", file=sys.stderr)


def box_users() -> range:
    """The step's box users, from `UNICON_BOX_USERS`, `<first>-<last>`, both
    included, which whoever starts the step sets: users no other step that
    can run at the same time on the machine has, and no container's user,
    all within 10,000 to 65,000. exec runs one box at a time, so it needs
    one; it is given them all so a step that runs boxes side by side can
    have more."""
    text = os.environ.get(BOX_USERS_VARIABLE)
    if text is None:
        raise PrimitiveError(
            f"{BOX_USERS_VARIABLE} is not set, so exec has no box users of the "
            "step's own to run the programs as"
        )
    matched = BOX_USERS.fullmatch(text)
    if matched is None:
        raise PrimitiveError(f"{BOX_USERS_VARIABLE} is not <first>-<last>: {text!r}")
    first, last = int(matched.group(1)), int(matched.group(2))
    if not FIRST_BOX_USER <= first <= last <= LAST_BOX_USER:
        raise PrimitiveError(
            f"{BOX_USERS_VARIABLE} is {text}, not a range within "
            f"{FIRST_BOX_USER} to {LAST_BOX_USER}"
        )
    return range(first, last + 1)


def step_memory_kb() -> int:
    """The step's memory in kilobytes, from `UNICON_STEP_MEMORY_MB`, which
    whoever starts the step sets, from the step's job: what the forge's
    planner gave the step, its largest item's boxes' memory limits, the
    files copied in beside them, and the room `primitive.yaml` adds for
    what the step does outside its boxes. The step is capped at it, so
    steps side by side in one slot of a machine stay within what each was
    planned; exec does not work it out again."""
    text = os.environ.get(STEP_MEMORY_VARIABLE)
    if text is None:
        raise PrimitiveError(
            f"{STEP_MEMORY_VARIABLE} is not set, so exec does not know the "
            "memory the step was given"
        )
    if re.fullmatch(r"[1-9][0-9]{0,6}", text) is None:
        raise PrimitiveError(
            f"{STEP_MEMORY_VARIABLE} is not a whole number of MB: {text!r}"
        )
    return int(text) * 1024


def set_up_step(users: range, memory_kb: int) -> None:
    """Set the step container up for isolate's boxes, as root, in this order:
    refuse root Docker's identity map (`check_shape`), raise the limits
    isolate inherits (`raise_limits`), give the step a cgroup capped at
    `memory_kb` and the boxes theirs under it
    (`set_up_cgroup`), a `/dev` (`set_up_devices`), write isolate's
    configuration with the step's box users, and run the box launcher's
    self-test (`check_launcher`). Any failure is an error of the step, and
    nothing runs. Each step is safe to take again, so a step that
    runs exec twice, as the tests do, sets up once.
    """
    check_shape()
    raise_limits()
    try:
        set_up_cgroup(memory_kb)
        set_up_devices()
        ISOLATE_CONFIG.write_text(
            f"box_root = {BOX_ROOT}\n"
            f"lock_root = {LOCK_ROOT}\n"
            f"cg_root = {BOX_CGROUPS}\n"
            f"first_uid = {users.start}\n"
            f"first_gid = {users.start}\n"
            f"num_boxes = {len(users)}\n"
        )
        BOX_ROOT.mkdir(parents=True, exist_ok=True)
        LOCK_ROOT.mkdir(parents=True, exist_ok=True)
        check_launcher()
    except OSError as error:
        raise PrimitiveError(
            f"the step could not be set up for its boxes, {error.filename}: "
            f"{error.strerror}; exec runs as root in a privileged container"
        ) from None


def check_launcher() -> None:
    """Run `LAUNCHER --self-test` once in box 0, as a run starts: it sets
    its filter up, checks that a call the filter refuses is refused, and
    exits 0. A run's box sends its standard error nowhere, so a launcher
    that could not set its filter up would end every run as the program's
    `runtime_error`; here it is an error of the step, with what the
    launcher said."""
    with tempfile.TemporaryDirectory(prefix="launcher-") as scratch:
        meta = Path(scratch) / "meta"
        with fresh_box(0, LAUNCHER_CHECK_KB):
            done = subprocess.run(
                [
                    ISOLATE,
                    "--cg",
                    "--box-id=0",
                    "--silent",
                    f"--meta={meta}",
                    "--time=5",
                    "--wall-time=10",
                    f"--cg-mem={LAUNCHER_CHECK_KB}",
                    f"--dir=dev={BOX_DEV}:dev",
                    "--run",
                    "--",
                    LAUNCHER,
                    "--self-test",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
            )
        found = read_meta(meta)
    if done.returncode == 0:
        return
    said = done.stderr or found.get("message", "")
    raise PrimitiveError(
        "the box launcher's self-test failed, so no program can run under its "
        f"filter: {isolate_said(exit_code(found, done.returncode), said)}"
    )


def check_shape() -> None:
    """Refuse root Docker's identity map: where the first line of
    `/proc/self/uid_map` is exactly `0 0 4294967295`, as under root Docker,
    a privileged step would be the machine's root. exec is meant for a
    container whose root is a user namespace's, as under rootless Docker;
    this check catches root Docker, not every map whose root is the
    machine's. Without a map to read, it does not start either. A
    development stack, whose laptop has root Docker alone, lets it through
    with `UNICON_DEV_ROOT_DOCKER=1`, saying so on standard error."""
    try:
        first = UID_MAP.read_text().splitlines()[0].split()
    except OSError, IndexError:
        first = None
    if first == ROOT_DOCKER and os.environ.get(DEV_ROOT_DOCKER_VARIABLE) == "1":
        print(
            f"exec: WARNING: {DEV_ROOT_DOCKER_VARIABLE} is set, so this step runs "
            "under root Docker, where a box that breaks out is the machine's root. "
            "Only a development stack may set it.",
            file=sys.stderr,
        )
    elif first is None or first == ROOT_DOCKER:
        raise PrimitiveError(
            "this container has root Docker's identity user ID map, where its "
            "root is the machine's own root; exec runs only where it is a user "
            "namespace's, as under rootless Docker"
        )
    if os.geteuid() != 0:
        raise PrimitiveError(
            "exec runs as root in a privileged container, to make its boxes"
        )


def raise_limits() -> None:
    """Raise exec's own stack and open-files limits to what isolate needs,
    `ISOLATE_STACK` and `ISOLATE_OPEN_FILES`, for isolate inherits them; a
    box gets isolate's own limits, not these. Where the container's hard
    limit is below that, exec does not start, rather than have isolate fail
    on a run's deep tree and end the batch in an error."""
    for kind, name, need in (
        (resource.RLIMIT_STACK, "stack", ISOLATE_STACK),
        (resource.RLIMIT_NOFILE, "open files", ISOLATE_OPEN_FILES),
    ):
        soft, hard = resource.getrlimit(kind)
        if hard != resource.RLIM_INFINITY and hard < need:
            raise PrimitiveError(
                f"the container's hard limit on {name}, {hard}, is below the "
                f"{need} isolate needs to give back a box's deepest tree"
            )
        if soft != resource.RLIM_INFINITY and soft < need:
            resource.setrlimit(kind, (need, hard))


def set_up_cgroup(memory_kb: int) -> None:
    """Make the step's cgroup, `step`, with the CPU and memory controllers
    on, and under it `main`, which every process in the container's cgroup
    moves to, and `isolate`, which isolate puts each box's cgroup under. A
    cgroup with processes in it cannot hand controllers to its children,
    hence `main`; a process that will not move, gone already, is passed
    over. `step` is capped, with no swap, at `memory_kb`, what the forge's
    planner gave the step (`step_memory_kb`), so steps side by side in one
    machine's slot stay within what the planner gave each."""
    MAIN_CGROUP.mkdir(parents=True, exist_ok=True)
    for pid in (CGROUP / "cgroup.procs").read_text().split():
        with contextlib.suppress(OSError):
            (MAIN_CGROUP / "cgroup.procs").write_text(pid)
    (CGROUP / "cgroup.subtree_control").write_text(CONTROLLERS)
    (STEP_CGROUP / "cgroup.subtree_control").write_text(CONTROLLERS)
    (STEP_CGROUP / "memory.max").write_text(str(memory_kb * 1024))
    with contextlib.suppress(FileNotFoundError):
        (STEP_CGROUP / "memory.swap.max").write_text("0")
    BOX_CGROUPS.mkdir(exist_ok=True)
    (BOX_CGROUPS / "cgroup.subtree_control").write_text(CONTROLLERS)


def set_up_devices() -> None:
    """Make `BOX_DEV`, the `/dev` every box gets: the container's null,
    zero, random and urandom bound in, the `fd` and standard stream links,
    and an empty `shm`, where each box's own `/box/shm` is bound. A box
    then cannot even name the machine's devices."""
    (BOX_DEV / "shm").mkdir(parents=True, exist_ok=True)
    for name in BOX_DEVICES:
        node = BOX_DEV / name
        if not node.exists():
            node.touch()
        if not node.is_mount():
            mount(Path("/dev") / name, node, None, MS_BIND, None)
    for name, target in BOX_LINKS.items():
        if not (BOX_DEV / name).is_symlink():
            (BOX_DEV / name).symlink_to(target)


@contextlib.contextmanager
def fresh_box(number: int, size_kb: int) -> Iterator[Path]:
    """Make box `number` afresh and give its folder, a tmpfs of its own of
    `size_kb` kilobytes and `BOX_INODES` files and folders; on every way out,
    unmount the tmpfs and clean the box up.

    A tmpfs an earlier item left mounted, should its clean-up not have run,
    is unmounted first: isolate refuses to clean a box with a mount in it,
    and `--init` cleans first. A fresh box every item is required, not only
    tidy: a box run twice reads the first run's peak memory and kill.
    """
    folder = BOX_ROOT / str(number) / "box"
    if folder.is_mount():
        unmount(folder)
    isolate_box(number, "--init")
    try:
        mount(
            f"box-{number}",
            folder,
            "tmpfs",
            MS_NOSUID | MS_NODEV,
            f"size={size_kb}k,nr_inodes={BOX_INODES},mode=0700",
        )
    except BaseException:
        quietly(lambda: isolate_box(number, "--cleanup"))
        raise
    try:
        yield folder
    except BaseException:
        quietly(lambda: close_box(number, folder))
        raise
    close_box(number, folder)


def close_box(number: int, folder: Path) -> None:
    """Unmount box `number`'s tmpfs, which frees all it held, and clean the
    box up, its cgroup with it."""
    unmount(folder)
    isolate_box(number, "--cleanup")


def isolate_box(number: int, action: str) -> None:
    """Run `isolate --cg` on box `number` for `action`, `--init` or
    `--cleanup`, raising on failure."""
    done = subprocess.run(
        [ISOLATE, "--cg", f"--box-id={number}", action],
        capture_output=True,
        check=False,
    )
    if done.returncode != 0:
        said = isolate_said(done.returncode, done.stderr)
        raise PrimitiveError(f"isolate {action} of box {number} failed: {said}")


def mount(
    source: Path | str, target: Path, kind: str | None, flags: int, data: str | None
) -> None:
    """Mount `source` on `target`, as mount(2), raising on failure."""
    result = libc().mount(
        os.fsencode(source),
        os.fsencode(target),
        None if kind is None else kind.encode(),
        ctypes.c_ulong(flags),
        None if data is None else data.encode(),
    )
    if result != 0:
        code = ctypes.get_errno()
        raise PrimitiveError(f"{target} could not be mounted: {os.strerror(code)}")


def unmount(target: Path, flags: int = 0) -> None:
    """Unmount what is mounted on `target`, as umount2(2) with `flags`,
    raising on failure."""
    if libc().umount2(os.fsencode(target), flags) != 0:
        code = ctypes.get_errno()
        raise PrimitiveError(f"{target} could not be unmounted: {os.strerror(code)}")


def libc() -> ctypes.CDLL:
    """The C library, for mount and umount2."""
    return ctypes.CDLL(None, use_errno=True)
