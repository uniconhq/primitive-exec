"""The image assembling the fixture lock into a cache of its own, as a
machine's cache is: each folder made of hard links into the store, offline,
file for file what exec 3.0.0 assembled but for what uv adds, and a second
folder adding only its own files."""

import hashlib
import json
import os
import secrets
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from conftest import docker
from support import (
    ROOT,
    RunImage,
    a_wheel,
    base_build_args,
    batch_inputs,
)
from toolchains import document, downloaded

pytestmark = pytest.mark.image

REFERENCE = "v3.0.0"
"""The release whose assembly a folder is compared with: the last to
unpack wheels itself, with no store."""
UV_FILES = {"INSTALLER", "REQUESTED", "direct_url.json", "uv_cache.json", "RECORD"}
"""What uv writes into each `dist-info` folder itself, which exec 3 left
as the wheel had it or did not write."""
BOOKKEEPING = 1 << 20
"""The most the tools' own bookkeeping may add to the cache beside a
folder's files and its new store entries: uv's pointers to the wheels it
unpacked, and the locks both keep."""


class Cache:
    """A machine's cache in a Docker volume of its own, the fixture lock's
    pieces at hand, assembled by `image`, each time with no network."""

    def __init__(self, image: str, root: Path) -> None:
        architecture = docker(
            "image", "inspect", image, "--format", "{{.Architecture}}"
        ).strip()
        self.image = image
        self.root = root
        self.platform = {"arm64": "linux-aarch64", "amd64": "linux-64"}[architecture]
        self.lock = document(self.platform)
        self.archives = downloaded(self.platform)
        self.volume = "primitive-exec-store-" + secrets.token_hex(4)
        docker("volume", "create", self.volume)

    def assemble(
        self,
        lock: dict[str, Any],
        archives: Path | None = None,
        line: str | None = None,
        image: str | None = None,
        volume: str | None = None,
    ) -> None:
        """Assemble every language of `lock`, its pieces in `archives`, in
        `image`, its toolchain line `line` when one is given."""
        file = self.root / f"lock-{secrets.token_hex(4)}.json"
        file.write_text(json.dumps(lock))
        script = "python3 -I /prepare.py"
        if line is not None:
            script = (
                f"printf '%s\\n' {line} > /usr/local/lib/unicon-exec/toolchain"
                f" && {script}"
            )
        docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--volume",
            f"{volume or self.volume}:/cache",
            "--volume",
            f"{archives or self.archives}:/archives:ro",
            "--volume",
            f"{file}:/fixture.json:ro",
            "--volume",
            f"{ROOT / 'tests/prepare_environments.py'}:/prepare.py:ro",
            "--entrypoint",
            "sh",
            image or self.image,
            "-c",
            script,
        )

    def manifest(self, volume: str | None = None) -> dict[str, Any]:
        """What the cache holds, as `environment_manifest.py` prints it."""
        printed = docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--volume",
            f"{volume or self.volume}:/cache:ro",
            "--volume",
            f"{ROOT / 'tests/environment_manifest.py'}:/manifest.py:ro",
            "--entrypoint",
            "python3",
            self.image,
            "-I",
            "/manifest.py",
        )
        found: dict[str, Any] = json.loads(printed)
        return found


@pytest.fixture(scope="module")
def store_cache(
    image: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Cache]:
    """The fixture lock's four languages assembled into a cache of their
    own, offline."""
    cache = Cache(image, tmp_path_factory.mktemp("store"))
    try:
        cache.assemble(cache.lock)
        yield cache
    finally:
        docker("volume", "rm", "--force", cache.volume)


@pytest.fixture(scope="module")
def reference_image(tmp_path_factory: pytest.TempPathFactory) -> str:
    """exec 3.0.0's image, built from its tag on the base the image under
    test builds on, for this machine's architecture. A checkout without
    the tag, as CI's shallow one, fetches that tag alone, as shallow as the
    checkout; in CI one that still has none fails rather than skip the
    comparison."""
    if not tagged():
        git = ["git", "-C", str(ROOT)]
        shallow = subprocess.run(
            [*git, "rev-parse", "--is-shallow-repository"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        depth = ["--depth=1"] if shallow == "true" else []
        fetch = [*git, "fetch", "--quiet", *depth, "origin", "tag", REFERENCE]
        subprocess.run(fetch, capture_output=True)
    if not tagged():
        missing = f"this checkout has no tag {REFERENCE} and could not fetch it"
        if os.environ.get("CI"):
            pytest.fail(missing)
        pytest.skip(missing)
    source = tmp_path_factory.mktemp("reference")
    archive = subprocess.run(
        ["git", "-C", str(ROOT), "archive", "--format=tar", REFERENCE],
        capture_output=True,
        check=True,
    ).stdout
    subprocess.run(["tar", "-x", "-C", str(source)], input=archive, check=True)
    tag = f"primitive-exec:reference-{REFERENCE}"
    command = ["docker", "build", "--quiet", *base_build_args(), "--tag", tag]
    subprocess.run([*command, str(source)], check=True, capture_output=True)
    return tag


def tagged() -> bool:
    """Whether this checkout has `REFERENCE`'s commit."""
    found = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "rev-parse",
            "--verify",
            "-q",
            f"{REFERENCE}^{{commit}}",
        ],
        capture_output=True,
    )
    return found.returncode == 0


def own(path: str) -> bool:
    """Whether `path` is what either assembly writes of its own rather than
    unpacks: the conda records, the folder's marker, and what uv writes
    into each `dist-info`."""
    parts = path.split("/")
    return (
        parts[0] == "conda-meta"
        or path == "ready.json"
        or (
            len(parts) > 1
            and parts[-2].endswith(".dist-info")
            and parts[-1] in UV_FILES
        )
    )


def test_each_folder_matches_exec_3s_file_for_file_but_for_uvs_additions(
    store_cache: Cache, reference_image: str
) -> None:
    """For the fixture lock, each folder holds what exec 3.0.0 assembled,
    path for path, each file's bytes, shebangs among them, and executable
    bit, and each link's target, but for what 6.7 names: uv's launchers in
    `bin/`, each `dist-info`'s files uv writes, and the conda records. The
    launchers name `/env`'s Python."""
    volume = "primitive-exec-reference-" + secrets.token_hex(4)
    docker("volume", "create", volume)
    try:
        store_cache.assemble(store_cache.lock, image=reference_image, volume=volume)
        reference = store_cache.manifest(volume)["folders"]
    finally:
        docker("volume", "rm", "--force", volume)
    assembled = {
        folder["fingerprint"]: folder
        for folder in store_cache.manifest()["folders"].values()
    }
    fingerprints = {
        entry["fingerprint"] for entry in store_cache.lock["languages"].values()
    }
    assert {folder["fingerprint"] for folder in reference.values()} == fingerprints
    added = []
    for old in reference.values():
        new = assembled[old["fingerprint"]]
        before = {path: shape(found) for path, found in old["paths"].items()}
        after = {path: shape(found) for path, found in new["paths"].items()}
        for path in set(before) | set(after):
            if own(path):
                continue
            if path not in before:
                assert path in new["launchers"], f"{path} is new in the folder"
                assert after[path][0] == "file"
                added.append(path)
                continue
            assert after.get(path) == before[path], f"{path} differs from exec 3's"
    assert sorted(added) == ["bin/f2py", "bin/numpy-config"]


def shape(found: list[Any]) -> list[Any]:
    """What a path of the manifest is, without the inode and links a file
    has in one cache alone."""
    return found[:3] if found[0] == "file" else found


def test_a_second_lock_sharing_python_adds_only_its_own_files(
    store_cache: Cache, tmp_path: Path
) -> None:
    """A lock of the fixture's Python with one more wheel shares every file
    the first links from the store, inode for inode, numpy's unpacked
    wheel among them, and adds to the cache only its own files, the new
    wheel's copy and its one unpacked entry. The same lock under another
    toolchain line adds its own files alone."""
    python = store_cache.lock["languages"]["python"]
    archives = tmp_path / "archives"
    archives.mkdir()
    for piece in python["conda"] + python["pypi"]:
        source = store_cache.archives / piece["sha256"]
        try:
            os.link(source, archives / piece["sha256"])
        except OSError:
            shutil.copyfile(source, archives / piece["sha256"])
    wheel = a_wheel(tmp_path, "extra", {"extra/__init__.py": "value = 1\n"})
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    wheel.rename(archives / digest)
    second = {
        **python,
        "fingerprint": hashlib.sha256(b"a second lock").hexdigest(),
        "pypi": [
            *python["pypi"],
            {
                "name": "extra",
                "version": "1.0",
                "file_name": wheel.name,
                "sha256": digest,
                "size": (archives / digest).stat().st_size,
            },
        ],
    }
    lock = store_cache.lock | {
        "languages": {"python": second},
        "pieces": {
            piece["sha256"]: "pieces/" + piece["sha256"] for piece in second["pypi"]
        }
        | {piece["sha256"]: "pieces/" + piece["sha256"] for piece in second["conda"]},
    }
    before = store_cache.manifest()
    store_cache.assemble(lock, archives)
    after = store_cache.manifest()
    (name,) = set(after["folders"]) - set(before["folders"])
    folder = after["folders"][name]
    first = next(
        found
        for found in before["folders"].values()
        if found["fingerprint"] == python["fingerprint"]
    )
    shared = 0
    for path, found in first["paths"].items():
        if found[0] == "file" and found[4] > 1:
            assert folder["paths"][path][3] == found[3], f"{path} is not shared"
            shared += 1
    entries = set(after["store"]) - set(before["store"])
    unpacked = {entry for entry in entries if entry.startswith("uv/archive-v0/")}
    assert entries == {f"wheels/{digest}", *unpacked} and len(unpacked) == 1
    added = after["bytes"] - before["bytes"]
    expected = folder["own"] + sum(after["store"][entry] for entry in entries)
    print(
        f"\nsecond lock: {added} bytes added, {folder['own']} of its own files, "
        f"{expected - folder['own']} of new store entries, {shared} files shared"
    )
    assert expected <= added <= expected + BOOKKEEPING

    line = hashlib.sha256(b"another toolchain line").hexdigest()
    store_cache.assemble(lock, archives, line=line)
    again = store_cache.manifest()
    (name,) = set(again["folders"]) - set(after["folders"])
    assert name == f"{second['fingerprint']}-{line}"
    assert set(again["store"]) == set(after["store"])
    added = again["bytes"] - after["bytes"]
    own_bytes = again["folders"][name]["own"]
    print(f"another line: {added} bytes added, {own_bytes} of its own files")
    assert own_bytes <= added <= own_bytes + BOOKKEEPING


def test_a_store_on_another_mount_fails_the_assembly_rather_than_copy(
    store_cache: Cache, tmp_path: Path, run_image: RunImage
) -> None:
    """A store the folders cannot link from, here a volume of its own on
    `/cache/store`, as a mount is to `link()` though both lie on one disk,
    makes both tools copy, and the step fails naming the copied file,
    rather than fill the cache with copies."""
    work = tmp_path / "work"
    batch_inputs(work, "print(42)\n", [("copied", "")])
    python = store_cache.lock["languages"]["python"]
    for piece in python["conda"] + python["pypi"]:
        target = work / "pieces" / piece["sha256"]
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(store_cache.archives / piece["sha256"], target)
    path = work / "in/environment.json"
    lock = json.loads(path.read_bytes())
    lock["languages"] = {"python": python}
    path.write_text(json.dumps(lock))
    cache, store = (
        f"primitive-exec-{kind}-" + secrets.token_hex(4) for kind in ("cache", "store")
    )
    for volume in (cache, store):
        docker("volume", "create", volume)
    try:
        docker(
            "run",
            "--rm",
            "--volume",
            f"{cache}:/cache",
            "--entrypoint",
            "mkdir",
            store_cache.image,
            "/cache/programs",
            "/cache/locks",
            "/cache/envs",
        )
        flags = ["--volume", f"{cache}:/cache", "--volume", f"{store}:/cache/store"]
        answer = run_image(work, flags=flags)
    finally:
        for volume in (cache, store):
            docker("volume", "rm", "--force", volume)
    assert set(answer) == {"schema_version", "error"}
    assert "was copied rather than linked from the machine's store" in answer["error"]


def test_a_lock_whose_entries_the_wrapper_freed_assembles_whole_again(
    image: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The step wrapper of the image's base evicts a folder and frees the
    store entries it listed, uv's pointers to them with them and the
    tools' other bookkeeping, rattler's locks among it, left. The lock
    then assembles offline again, file for file as before, from entries
    unpacked anew."""
    cache = Cache(image, tmp_path_factory.mktemp("evicted"))
    try:
        python = cache.lock["languages"]["python"]
        lock = cache.lock | {"languages": {"python": python}}
        cache.assemble(lock)
        before = cache.manifest()
        (folder,) = before["folders"].values()
        docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--volume",
            f"{cache.volume}:/cache",
            "--volume",
            f"{ROOT / 'tests/evict_cache.py'}:/evict.py:ro",
            "--entrypoint",
            "python3",
            image,
            "-I",
            "/evict.py",
        )
        evicted = cache.manifest()
        assert evicted["folders"] == {} and evicted["store"] == {}
        cache.assemble(lock)
        after = cache.manifest()
    finally:
        docker("volume", "rm", "--force", cache.volume)
    (again,) = after["folders"].values()
    assert set(after["store"]) & set(before["store"]) == {
        entry for entry in before["store"] if not entry.startswith("uv/")
    }
    shapes = {
        path: shape(found) for path, found in folder["paths"].items() if not own(path)
    }
    assert {
        path: shape(found) for path, found in again["paths"].items() if not own(path)
    } == shapes


def test_the_fixture_assembles_with_no_network(store_cache: Cache) -> None:
    """Each language of the fixture lock was assembled in a container with
    no network, uv told to stay offline, from the pieces alone."""
    folders = store_cache.manifest()["folders"].values()
    assert {folder["fingerprint"] for folder in folders} >= {
        entry["fingerprint"] for entry in store_cache.lock["languages"].values()
    }
