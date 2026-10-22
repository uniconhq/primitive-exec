"""exec and interact each keep a copy of the same box and report handling,
which must not drift apart: a change to one is made to the other.

Each function in `SHARED` is compared as its syntax tree printed again,
with its docstring left out, the parameter one names `item` and the other
`entry` called `item` in both, and the primitive's own name in its texts,
`exec` or `interact`, called `PRIMITIVE`, so only what they do is compared.
The box launcher, `src/box-launch.c`, is kept byte for byte the same.
Both repos pin the same hash of those functions and the launcher in
`tests/twin.sha256`, so
each checks its own copy wherever it runs, its CI included. The pin shows
that a change was meant for both, not that both have it: a change to one
passes once that one's pin is changed. With the other repo checked out
beside this one, the two are compared function by function too, which
shows they agree and says which one differs. The tests read the files and
need no box.
"""

import ast
import hashlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
OWN = ROOT / "src" / "exec.py"
SIBLING = ROOT.parent / "primitive-interact" / "src" / "interact.py"
PINNED = ROOT / "tests" / "twin.sha256"
LAUNCHER = ROOT / "src" / "box-launch.c"
SIBLING_LAUNCHER = ROOT.parent / "primitive-interact" / "src" / "box-launch.c"
SHARED = (
    "in_file",
    "cached_folder",
    "cache_binding",
    "read_manifest",
    "place_files",
    "shown_bytes",
    "check_shape",
    "set_up_step",
    "step_memory_kb",
    "check_launcher",
    "box_users",
    "refuse_constant",
    "check_memory",
    "isolate_command",
    "isolate_said",
    "pump",
    "read_meta",
    "read_report",
    "report_files",
    "folder_files",
    "read_capped",
    "report_text",
    "report_number",
    "judge",
    "raise_limits",
    "set_up_cgroup",
    "set_up_devices",
    "fresh_box",
    "close_box",
    "isolate_box",
    "mount",
    "unmount",
    "libc",
)
"""The functions exec and interact share. The others name a run or a
program, and read differently in the two."""
CONSTANTS = (
    "STEP_MEMORY_KB",
    "STEP_MEMORY_VARIABLE",
    "CACHE_MOUNT",
    "LAUNCHER",
    "LAUNCHER_CHECK_KB",
)
"""The module constants exec and interact share, compared as their
assignments: the room the step keeps beside the boxes, which the forge's
planner keeps a copy of too, the variable the step's memory is given in,
where a box sees its cached files, where the image installs the box
launcher, and the box its self-test runs in."""
RENAMED = {"entry": "item", "Entry": "Item"}
"""interact's names for what exec calls an item, one program of an item."""
PRIMITIVE = re.compile(r"\b(exec|interact)\b")
"""The primitive's own name, as its messages give it."""


class Normalize(ast.NodeTransformer):
    """Rename interact's `entry` to exec's `item`, as a name and as a
    parameter, and the primitive's name in a text to `PRIMITIVE`."""

    def visit_Name(self, node: ast.Name) -> ast.Name:
        node.id = RENAMED.get(node.id, node.id)
        return node

    def visit_arg(self, node: ast.arg) -> ast.arg:
        node.arg = RENAMED.get(node.arg, node.arg)
        self.generic_visit(node)
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.Constant:
        if isinstance(node.value, str):
            node.value = PRIMITIVE.sub("PRIMITIVE", node.value)
        return node


def shared(path: Path) -> dict[str, str]:
    """Each function of `SHARED` in a source file, by name, normalized: its
    docstring left out and `Normalize` applied, printed from its tree; and
    each assignment of `CONSTANTS`, printed as it is."""
    found = {}
    for node in ast.parse(path.read_text()).body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in CONSTANTS
        ):
            found[node.targets[0].id] = ast.unparse(node)
        if isinstance(node, ast.FunctionDef) and node.name in SHARED:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                node.body = node.body[1:] or [ast.Pass()]
            found[node.name] = ast.unparse(Normalize().visit(node))
    return found


def digest(functions: dict[str, str], launcher: bytes) -> str:
    """The hash of the shared functions and constants, in `SHARED`'s order
    and then `CONSTANTS`', and then of the launcher's source."""
    joined = "\n\n".join(functions[name] for name in SHARED + CONSTANTS)
    return hashlib.sha256(joined.encode() + b"\n\n" + launcher).hexdigest()


def test_every_shared_function_is_here() -> None:
    assert sorted(shared(OWN)) == sorted(SHARED + CONSTANTS)


def test_the_shared_functions_are_the_pinned_ones() -> None:
    """A change to a shared function is made to both repos, and the new
    hash, this test's message, pinned in both."""
    found = digest(shared(OWN), LAUNCHER.read_bytes())
    assert found == PINNED.read_text().strip(), f"the shared functions are {found}"


def test_the_shared_functions_are_the_siblings() -> None:
    """Skipped without the other repo checked out beside this one."""
    if not SIBLING.is_file():
        pytest.skip("no primitive-interact checkout beside this one")
    ours, theirs = shared(OWN), shared(SIBLING)
    for name in SHARED + CONSTANTS:
        assert ours[name] == theirs[name], name
    assert LAUNCHER.read_bytes() == SIBLING_LAUNCHER.read_bytes(), "box-launch.c"
