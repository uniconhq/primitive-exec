"""Print what the machine's cache holds, for the image tests to compare.
For each assembled folder, by its name: its fingerprint; every path under
it, by kind, with a file's SHA-256, whether it is executable, its inode
and its links, or a link's target; the launchers uv wrote in its `bin/`;
and its own bytes, those of files with every link inside it. For the
cache: the bytes of its folders and its store, each file counted once
however many links it has, and each store entry's. Run in an image,
with `/cache` mounted."""

import collections
import hashlib
import json
import os
import stat
from pathlib import Path

CACHE = Path("/cache")
ENTRIES = ("conda", "uv/archive-v0", "wheels")


def main() -> None:
    folders = {}
    for folder in sorted((CACHE / "envs").iterdir()):
        if not (folder / "ready.json").is_file():
            continue
        fingerprint = json.loads((folder / "ready.json").read_text())["fingerprint"]
        paths: dict[str, list[object]] = {}
        inside: collections.Counter[int] = collections.Counter()
        sizes: dict[int, tuple[int, int]] = {}
        for parent, names, files in folder.walk():
            for name in names + files:
                path = parent / name
                relative = str(path.relative_to(folder))
                found = path.lstat()
                if stat.S_ISLNK(found.st_mode):
                    paths[relative] = ["link", str(path.readlink())]
                elif stat.S_ISDIR(found.st_mode):
                    paths[relative] = ["folder"]
                else:
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    paths[relative] = [
                        "file",
                        digest,
                        bool(found.st_mode & 0o111),
                        found.st_ino,
                        found.st_nlink,
                    ]
                    inside[found.st_ino] += 1
                    sizes[found.st_ino] = (found.st_size, found.st_nlink)
        own = sum(
            size for inode, (size, links) in sizes.items() if inside[inode] == links
        )
        folders[folder.name] = {
            "fingerprint": fingerprint,
            "paths": paths,
            "launchers": launchers(folder),
            "own": own,
        }
    store = {}
    for kind in ENTRIES:
        place = CACHE / "store" / kind
        for entry in sorted(place.iterdir()) if place.is_dir() else []:
            if entry.is_dir() and not entry.name.startswith("."):
                store[f"{kind}/{entry.name}"] = counted(entry)
    used = counted(CACHE / "envs", CACHE / "store")
    print(json.dumps({"folders": folders, "store": store, "bytes": used}))


def launchers(folder: Path) -> list[str]:
    """The files in `folder`'s `bin/` that a `dist-info` uv installed
    lists in its `RECORD`."""
    found = []
    for dist in folder.glob("lib/python*/site-packages/*.dist-info"):
        installer = dist / "INSTALLER"
        if not installer.is_file() or installer.read_text().strip() != "uv":
            continue
        for row in (dist / "RECORD").read_text().splitlines():
            installed = Path(os.path.normpath(dist.parent / row.partition(",")[0]))
            if installed.is_relative_to(folder / "bin"):
                found.append(str(installed.relative_to(folder)))
    return sorted(found)


def counted(*folders: Path) -> int:
    """The bytes of the files under `folders`, each inode once."""
    seen = set()
    total = 0
    for folder in folders:
        for parent, _, files in folder.walk():
            for name in files:
                found = (parent / name).lstat()
                if stat.S_ISREG(found.st_mode) and found.st_ino not in seen:
                    seen.add(found.st_ino)
                    total += found.st_size
    return total


if __name__ == "__main__":
    main()
