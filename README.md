# primitive-exec

The `exec` primitive, `unicon/exec@v4` at the forge. It compiles entrant
and organiser code and runs it under limits, each run in an
[isolate](https://github.com/ioi/isolate) box of its own. It has three
forms: `one` runs one program, `programs` runs up to 8 programs linked by
pipes, and `build` only compiles.

Grading workflows use it as a step. Woodpecker runs that step on a grading
machine. The image builds on the runner's `primitive-base`, and exec speaks
the runner's primitive contract. Toolchains are not in the image: each one
is assembled from the task's locked environment and kept in the machine's
cache.

What is here:

- `src/exec/`: the Python package the image runs.
- `src/box-launch.c`: the box launcher every program starts through.
- `Dockerfile`: the image, `ghcr.io/uniconhq/primitive-exec`.
- `primitive.yaml`: the declaration the forge reads to check workflows.
- `tests/`: unit tests, and image tests that run the image on Docker.

## Running it locally

You need Python 3.14, [uv](https://docs.astral.sh/uv/) and Docker. Some
checks read from a `runner` checkout next to this one, at the release named
in `.github/workflows/ci.yaml`.

```
uv sync --locked
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run --with ruamel-yaml==0.19.1 python ../runner/scripts/check_declaration.py ../runner/schemas/primitive.schema.json .
uv run pytest -m "not image"
uv run pytest -m image
```

The image tests build the image from this checkout, on a `primitive-base`
built from `../runner`. Set `PRIMITIVE_IMAGE` to test a built image, or
`BASE_IMAGE` to pick the base. They download toolchain pieces the first
time, into `UNICON_EXEC_TEST_PIECES` or a temp folder. Without Docker they
are skipped.

Tests that run programs need Linux, root and isolate, so on a laptop they
are skipped. An image test runs them inside the test container
(`tests/Dockerfile`), where none may skip.

To build the image by hand:

```
docker build -t primitive-base:test -f ../runner/images/primitive-base/Dockerfile ../runner
docker build -t primitive-exec:test --build-arg BASE_IMAGE=primitive-base:test .
```

## Releasing

CI (`.github/workflows/ci.yaml`) runs on pushes to `main` and on pull
requests. It calls the runner's shared `primitive-ci.yaml`.

To release, bump `version` in `pyproject.toml`, merge to `main`, and push a
tag such as `v4.0.1`. The tag runs `.github/workflows/release.yaml`, which
calls the runner's `primitive-release.yaml`. That workflow:

- refuses a tag that is not on `main` or does not match `pyproject.toml`;
- runs the same checks as CI;
- pushes `ghcr.io/uniconhq/primitive-exec:<tag>`;
- makes a GitHub release with `images.json` (the image by digest) and
  `primitive.yaml`.

Then pin the new digest in the deploy repo's `images.json`.

## Before you change things

- The tag's major is the forge version. A change to the ports needs a new
  major.
- To move to a new runner release, change both `uses:` lines and
  `runner-ref` in the workflows, and `BASE_IMAGE` in the `Dockerfile`,
  together.
- `primitive.yaml` has no `image` line. Bootstrap writes it from the
  release.
- exec refuses to start under root Docker, and needs `UNICON_BOX_USERS`
  and `UNICON_STEP_MEMORY_MB`. A dev stack sets `UNICON_DEV_ROOT_DOCKER=1`;
  a grading machine never does.
