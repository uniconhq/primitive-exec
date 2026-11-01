"""Assemble verified wheels once, copying into a private read-only environment."""

import contextlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from exec.compile import cache
from exec.shared import PrimitiveError

DIGEST = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class Environment:
    folder: Path
    fingerprint: str


@contextlib.contextmanager
def assembled(
    root: Path, lock_file: Path, language: str
) -> Iterator[Environment | None]:
    try:
        document = json.loads(lock_file.read_bytes())
        entry = document["languages"].get("python" if language == "ipynb" else language)
        if entry is None:
            yield None
            return
        fingerprint = entry["fingerprint"]
        if (
            document["version"] != 2
            or document["prefix"] != "/env"
            or not DIGEST.fullmatch(fingerprint)
        ):
            raise ValueError("invalid environment lock")
        pieces = document["pieces"]
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise PrimitiveError("the environment lock could not be read") from error
    if not cache.in_use():
        raise PrimitiveError("the environment needs the machine's /cache volume")
    folder = cache.CACHE / "envs" / fingerprint
    folder.parent.mkdir(exist_ok=True)
    with cache.locked(fingerprint, kind="envs"):
        if not (folder / ".ready").is_file():
            temporary = Path(
                tempfile.mkdtemp(prefix=f"{fingerprint}.", dir=folder.parent)
            )
            try:
                for wheel in entry.get("pypi", []):
                    digest = wheel["sha256"]
                    if not DIGEST.fullmatch(digest) or digest not in pieces:
                        raise PrimitiveError("the environment is missing a wheel piece")
                    path = root / pieces[digest]
                    if (
                        not path.is_file()
                        or path.stat().st_size != wheel["size"]
                        or cache.file_sha256(path) != digest
                    ):
                        raise PrimitiveError(
                            "an environment piece does not match its SHA-256 and size"
                        )
                    unpack_wheel(path, temporary)
                (temporary / ".ready").write_text(fingerprint)
                if folder.exists():
                    shutil.rmtree(folder)
                temporary.rename(folder)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
            print(f"exec: assembled environment {fingerprint} at /env", file=sys.stderr)
        else:
            print(f"exec: reused environment {fingerprint} at /env", file=sys.stderr)
        folder.chmod(0o755)
        os.utime(folder)
        yield Environment(folder, fingerprint)


def unpack_wheel(wheel: Path, target: Path) -> None:
    try:
        with zipfile.ZipFile(wheel) as archive:
            for entry in archive.infolist():
                path = PurePosixPath(entry.filename)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in entry.filename
                    or stat.S_ISLNK(entry.external_attr >> 16)
                ):
                    raise PrimitiveError("a wheel holds an unsafe path or link")
                if not path.parts:
                    continue
                parts = path.parts
                scheme = "site-packages"
                if parts[0].endswith(".data"):
                    if len(parts) < 3:
                        continue
                    mapped = {
                        "purelib": "site-packages",
                        "platlib": "site-packages",
                        "scripts": "bin",
                        "data": "",
                        "headers": "include",
                    }.get(parts[1])
                    if mapped is None:
                        raise PrimitiveError("a wheel names an unknown install folder")
                    scheme = mapped
                    parts = parts[2:]
                output = target / scheme / Path(*parts)
                if entry.is_dir():
                    output.mkdir(parents=True, exist_ok=True)
                    continue
                output.parent.mkdir(parents=True, exist_ok=True)
                if output.exists():
                    raise PrimitiveError("two wheels install the same file")
                with archive.open(entry) as source, output.open("xb") as destination:
                    shutil.copyfileobj(source, destination, 1024 * 1024)
                output.chmod(0o555 if entry.external_attr >> 16 & 0o111 else 0o444)
    except (OSError, zipfile.BadZipFile) as error:
        raise PrimitiveError("an environment wheel could not be unpacked") from error
