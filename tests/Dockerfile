# The test container: the image under test with the tests' own tools, to run
# tests/test_exec.py as root with isolate, as exec runs in a step. The image
# test `test_the_in_process_tests_pass_in_the_test_container` builds it on
# the image and runs it privileged, the checkout mounted at /src, through
# tests/userns.py (see "Running it locally" in the README).
ARG IMAGE=primitive-exec:test
FROM ghcr.io/astral-sh/uv:0.12.13 AS uv

FROM ${IMAGE}
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON=/usr/local/bin/python3 \
    UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock /tools/
RUN cd /tools && uv sync --locked --no-install-project
# A test that runs programs fails here rather than skipping, and nothing is
# written into the read-only checkout.
ENV PATH=/opt/venv/bin:$PATH \
    UNICON_TESTS_IN_BOXES=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /src
ENTRYPOINT []
