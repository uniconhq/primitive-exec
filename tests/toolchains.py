"""Reproducible public toolchains used by the native image tests only."""

import concurrent.futures
import hashlib
import json
import os
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

FIXTURE = Path(__file__).parent / "fixtures/environment.lock"


def document(platform: str) -> dict[str, Any]:
    lock = json.loads(FIXTURE.read_bytes())
    return {
        "version": 2,
        "prefix": "/env",
        "platform": platform,
        "languages": lock["platforms"][platform],
        "pieces": {
            piece["sha256"]: "pieces/" + piece["sha256"]
            for entry in lock["platforms"][platform].values()
            for piece in entry.get("conda", []) + entry.get("pypi", [])
        },
    }


def downloaded(platform: str) -> Path:
    root = Path(
        os.environ.get(
            "UNICON_EXEC_TEST_PIECES",
            str(Path(tempfile.gettempdir()) / "unicon-exec-test-pieces"),
        )
    )
    root.mkdir(exist_ok=True, parents=True)
    lock = document(platform)
    records = {
        piece["sha256"]: piece
        for entry in lock["languages"].values()
        for piece in entry.get("conda", []) + entry.get("pypi", [])
    }

    def fetch(piece: dict[str, Any]) -> None:
        path = root / piece["sha256"]
        if path.is_file() and path.stat().st_size == piece["size"]:
            with path.open("rb") as source:
                if hashlib.file_digest(source, "sha256").hexdigest() == piece["sha256"]:
                    return
        url = (
            piece.get("url")
            or "https://files.pythonhosted.org/packages/" + piece["file_name"]
        )
        # Wheel fixtures keep the registry URL beside their locked size and digest.
        if not piece.get("url"):
            package = json.loads(
                urllib.request.urlopen(
                    f"https://pypi.org/pypi/{piece['name']}/{piece['version']}/json",
                    timeout=60,
                ).read()
            )
            url = next(
                row["url"]
                for row in package["urls"]
                if row["digests"]["sha256"] == piece["sha256"]
            )
        with (
            urllib.request.urlopen(url, timeout=120) as source,
            tempfile.NamedTemporaryFile(dir=root, delete=False) as target,
        ):
            temporary = Path(target.name)
            try:
                digest, size = hashlib.sha256(), 0
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    assert size <= piece["size"], (
                        "a fixture archive is larger than its lock"
                    )
                    digest.update(chunk)
                    target.write(chunk)
                assert size == piece["size"] and digest.hexdigest() == piece["sha256"]
                target.close()
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(fetch, records.values()))
    return root


def attach(work: Path, inputs: dict[str, Any]) -> None:
    """Give a fixture the required environment input."""
    template = Path(os.environ.get("UNICON_TEST_LOCK", "/nonexistent-test-lock"))
    value = (
        json.loads(template.read_bytes())
        if template.is_file()
        else {"version": 2, "prefix": "/env", "languages": {}}
    )
    path = work / "in/environment.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    entries = (
        [each["inputs"] for each in inputs["batch"]]
        if "batch" in inputs
        else [inputs["inputs"]]
    )
    for ports in entries:
        ports.setdefault("environment", {"file": "in/environment.json"})


def folder_for(language: str) -> Path:
    """The folder the image under test assembled `language`'s toolchain in,
    named by the lock's fingerprint and the image's toolchain line."""
    template = Path(os.environ["UNICON_TEST_LOCK"])
    lock = json.loads(template.read_bytes())
    fingerprint = str(lock["languages"][language]["fingerprint"])
    tools = Path("/usr/local/lib/unicon-exec/toolchain").read_text().strip()
    return Path("/cache/envs") / f"{fingerprint}-{tools}"
