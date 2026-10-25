"""Run a command in a user namespace that maps user and group IDs 0 to
65,535 onto themselves, and exit as it exits.

    python3 userns.py <command> [arguments...]

exec refuses to start where `/proc/self/uid_map` is the identity, as it is
in a privileged container under root Docker, the only Docker that Docker
Desktop and a CI machine have. Under rootless Docker, the shape exec is
built for, a step's root is a user namespace's: uid 0 maps to the daemon's
user and the rest to its 65,536 subordinate IDs. This gives the tests a map
that is not the identity and holds every box user, so exec's own checks run
as they would there. It is not that shape: root here is still the machine's
root, so the tests show what isolate and exec do, not what an escape from
them would reach. Run it as root in a privileged container.
"""

import os
import sys
from pathlib import Path

IDS = 65536


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: userns.py <command> [arguments...]", file=sys.stderr)
        return 2
    ready, go = os.pipe(), os.pipe()
    child = os.fork()
    if child == 0:
        os.close(ready[0])
        os.close(go[1])
        os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNS)
        os.write(ready[1], b"x")
        os.close(ready[1])
        if os.read(go[0], 1) != b"x":
            os._exit(125)
        os.close(go[0])
        os.setresgid(0, 0, 0)
        os.setresuid(0, 0, 0)
        os.execvp(argv[1], argv[1:])
    os.close(ready[1])
    os.close(go[0])
    if os.read(ready[0], 1) != b"x":
        return 125
    for name in ("uid_map", "gid_map"):
        Path(f"/proc/{child}/{name}").write_text(f"0 0 {IDS}\n")
    os.write(go[1], b"x")
    os.close(go[1])
    _, status = os.waitpid(child, 0)
    return os.waitstatus_to_exitcode(status)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
