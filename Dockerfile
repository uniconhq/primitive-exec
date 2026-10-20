# The exec primitive, ghcr.io/uniconhq/primitive-exec. One container per batch
# of runs: it reads /work/inputs.json and the program and inputs under
# /work/in/, and writes /work/outputs.json and each run's output under
# /work/out/.

# Pinned by digest so a release rebuilds from the same base. python:3.14-slim
# (Debian 13), pulled 2026-09-13, the same pin as the runner's images and the
# compile primitive, whose Python and Java versions this image must match to
# run what compile builds. Change it in both primitives together, from
# `docker image inspect python:3.14-slim --format '{{index .RepoDigests 0}}'`.
FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6 AS helper

# sandbox-exec, the small static program every program is started through, so
# that its peak memory is its own (see src/sandbox-exec.c).
RUN apt-get update \
    && apt-get install --yes --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
COPY src/sandbox-exec.c /src/
RUN gcc -O2 -static -Wall -Werror -o /sandbox-exec /src/sandbox-exec.c


FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

LABEL org.opencontainers.image.title="primitive-exec" \
      org.opencontainers.image.description="The Unicon exec primitive: run a program under limits and measure it." \
      org.opencontainers.image.source="https://github.com/uniconhq/primitive-exec" \
      org.opencontainers.image.licenses="MIT"

# The Java runtime runs the jars compile builds, from the same Debian package
# set as compile's JDK. Native programs are statically linked and Python
# programs run on the image's own interpreter. The JRE's post-install scripts
# expect the man directory the slim image leaves out.
RUN mkdir -p /usr/share/man/man1 \
    && apt-get update \
    && apt-get install --yes --no-install-recommends openjdk-21-jre-headless \
    && rm -rf /var/lib/apt/lists/*

COPY --from=helper --chmod=0755 /sandbox-exec /usr/local/bin/sandbox-exec
COPY --chmod=0755 src/exec.py /usr/local/bin/exec

# The harness runs every step as a non-root user with a read-only root, and
# /work and /tmp as the only writable places. This program and every program
# it runs write nowhere else.
USER 65532:65532
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/exec"]
