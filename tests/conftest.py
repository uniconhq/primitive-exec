"""Fixtures for running the image the way the harness does, and the contract checks.

The image tests build the image from this checkout (or use the one named by
`PRIMITIVE_IMAGE`) and start it as an exec step container is started:
privileged, as root, with no swap, the declared memory and pids limits, the
CPU-time limit as `RLIMIT_CPU`, one CPU, the step's box users, and the
working directory at /work. They are skipped when Docker is not reachable.

The contract checks use `primitive.schema.json` from `PRIMITIVE_SCHEMA`, or
from a runner checkout beside this one, when it has the version 6 contract.

Every test gives exec a program's source, which exec compiles itself. The
in-process tests import the program, which needs Linux, so elsewhere only the image
tests are collected; those that run programs need root and isolate too, so
they are skipped elsewhere and run in the privileged test container
(`tests/Dockerfile`), which one image test starts. The declaration is
checked by the runner's shared primitive CI.
"""

import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from support import (
    NAME,
    ROOT,
    SCHEMA_VERSION,
    SIBLING_SCHEMA,
    USERNS,
    Check,
    RunImage,
    base_build_args,
    declaration,
    step_flags,
)

collect_ignore = [] if sys.platform == "linux" else ["test_exec.py", "test_programs.py"]


@pytest.fixture(scope="session")
def image() -> str:
    """The image under test: `PRIMITIVE_IMAGE`, or built from this checkout."""
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed")
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.skip("docker is not reachable")
    named = os.environ.get("PRIMITIVE_IMAGE")
    if named:
        return named
    tag = f"{NAME}:test"
    command = ["docker", "build", "--quiet", *base_build_args(), "--tag", tag]
    subprocess.run([*command, str(ROOT)], check=True)
    return tag


@pytest.fixture(scope="session")
def rootless(image: str) -> bool:
    """Whether the daemon is rootless Docker, where a step's root is
    already a user namespace's, so exec starts as the image's entrypoint
    starts it rather than through `USERNS`, and there is no root Docker to
    refuse."""
    return "rootless" in docker("info", "--format", "{{json .SecurityOptions}}")


@pytest.fixture
def run_image(image: str, rootless: bool) -> RunImage:
    """Run the image once over a working directory and return its outputs.json.

    The directory is copied into a Docker volume, run there and copied back,
    because the harness gives a step a volume, and so the files exec writes
    as root come back as the tester's own.
    """

    def run(
        work: Path,
        limits: dict[str, int] | None = None,
        seconds: list[float] | None = None,
        root_docker: bool = False,
        flags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run the batch in `work`; `seconds`, when given, gets how long the
        step container took, from `docker run` to its exit. exec is started
        through `USERNS` unless `root_docker`, when it meets root Docker's
        identity user ID map as the image's entrypoint does, or the daemon
        is `rootless`, when the entrypoint starts it as a step's is.
        `flags` are more `docker run` flags."""
        limits = limits or declaration()["limits"]
        volume = f"{NAME}-test-{secrets.token_hex(4)}"
        docker("volume", "create", volume)
        try:
            holder = docker("create", "--volume", f"{volume}:/work", image).strip()
            try:
                docker("cp", f"{work}/.", f"{holder}:/work")
                command = ["docker", "run", "--rm", *step_flags(limits)]
                command += flags or []
                command += ["--volume", f"{volume}:/work"]
                if root_docker or rootless:
                    command += [image]
                else:
                    command += ["--volume", f"{USERNS}:/userns.py:ro"]
                    command += ["--entrypoint=python3", image]
                    command += ["/userns.py", "/usr/local/bin/exec"]
                started = time.monotonic()
                subprocess.run(
                    command, check=True, timeout=limits["time_ms"] / 1000 + 60
                )
                if seconds is not None:
                    seconds.append(time.monotonic() - started)
                docker("cp", f"{holder}:/work/.", str(work))
            finally:
                docker("rm", "--force", holder)
        finally:
            docker("volume", "rm", "--force", volume)
        document: dict[str, Any] = json.loads(
            (work / "outputs.json").read_text(encoding="utf-8")
        )
        return document

    return run


def docker(*arguments: str) -> str:
    """Run one docker command, raising with its error output when it fails."""
    done = subprocess.run(["docker", *arguments], capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"docker {arguments[0]} failed: {done.stderr.strip()}")
    return done.stdout


@pytest.fixture(scope="session")
def schema() -> dict[str, Any]:
    """The runner's primitive.schema.json, when it has contract version 6."""
    named = os.environ.get("PRIMITIVE_SCHEMA")
    path = Path(named) if named else SIBLING_SCHEMA
    if not path.is_file():
        pytest.skip("no primitive.schema.json; set PRIMITIVE_SCHEMA")
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    inputs = document.get("$defs", {}).get("inputs_file", {})
    version = inputs.get("properties", {}).get("schema_version", {})
    if SCHEMA_VERSION not in version.get("enum", [version.get("const")]):
        pytest.skip(f"{path} has no version {SCHEMA_VERSION} contract")
    return document


@pytest.fixture
def check(schema: dict[str, Any]) -> Check:
    """Validate a document against one part of the contract.

    For `outputs_file`, every entry must also carry each output the
    declaration names as required, and nothing it does not name but the
    `reports` of the manifest it was given.
    """
    registry: Registry[Any] = Registry().with_resource(
        schema["$id"], Resource.from_contents(schema)
    )

    def validate(
        document: dict[str, Any], part: str, reports: frozenset[str] = frozenset()
    ) -> None:
        reference = {"$ref": f"{schema['$id']}#/$defs/{part}"}
        Draft202012Validator(reference, registry=registry).validate(document)
        if part != "outputs_file" or "error" in document:
            return
        declared = declaration()["outputs"]
        required = {name for name, port in declared.items() if not port.get("optional")}
        if "batch" in document:
            entries = [entry["outputs"] for entry in document["batch"]]
        else:
            entries = [document["outputs"]]
        for outputs in entries:
            if outputs["outcome"] == "compile_error":
                assert set(outputs) == {"log", "outcome"}, outputs
                continue
            assert required <= set(outputs) <= set(declared) | reports, outputs

    return validate


@pytest.fixture
def programs_check(schema: dict[str, Any]) -> Check:
    """Validate a document against one part of the contract.

    For `outputs_file`, every entry must also carry `facts`, `outcome` and,
    for each program named in `programs`, each fact the several-programs
    form names as required, and nothing else but the optional facts and
    that program's `reports`, as `<name>.<report>`.
    """
    registry: Registry[Any] = Registry().with_resource(
        schema["$id"], Resource.from_contents(schema)
    )

    def validate(
        document: dict[str, Any],
        part: str,
        programs: dict[str, frozenset[str]] | None = None,
    ) -> None:
        reference = {"$ref": f"{schema['$id']}#/$defs/{part}"}
        Draft202012Validator(reference, registry=registry).validate(document)
        if part != "outputs_file" or "error" in document:
            return
        declared = declaration("programs")
        facts = declared["inputs"]["programs"]["outputs"]
        required = {name for name, port in facts.items() if not port.get("optional")}
        for entry in document["batch"]:
            outputs = entry["outputs"]
            if outputs["outcome"] == "compile_error":
                assert {key for key in outputs if not key.endswith(".log")} == {
                    "outcome"
                }, outputs
                continue
            assert set(declared["outputs"]) == {
                name for name in outputs if "." not in name
            }, outputs
            for name, reports in (programs or {}).items():
                given = {
                    key.partition(".")[2]
                    for key in outputs
                    if key.partition(".")[0] == name
                }
                assert required <= given <= set(facts) | reports, outputs

    return validate
