# The exec primitive, ghcr.io/uniconhq/primitive-exec. One container per batch
# of runs: it reads /work/inputs.json and the program and inputs under
# /work/in/, and writes /work/outputs.json and each run's output under
# /work/out/. Each run goes in an isolate box of its own, so the container
# runs as root and privileged (see "Where it runs" in the README).

# The image builds on the primitives' base, the runner's
# ghcr.io/uniconhq/primitive-base: the same python:3.14-slim, with the step
# wrapper as its entrypoint, which runs this image's command in a grading
# step and runs it in its own place anywhere else. BASE_IMAGE names the
# runner release's image; a build against a runner checkout passes the one
# built from it, as the tests and the shared CI do.
ARG BASE_IMAGE=ghcr.io/uniconhq/primitive-base:v0.12.0

# The stage that builds isolate is pinned by digest, python:3.14-slim
# (Debian 13), pulled 2026-09-13, the base image's own pin, so isolate
# links the libraries the final stage has. Change it with the runner's
# images/primitive-base/Dockerfile, from
# `docker image inspect python:3.14-slim --format '{{index .RepoDigests 0}}'`.
FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6 AS isolate

# isolate 2.7 (https://github.com/ioi/isolate), built unpatched from its tag's
# commit and refused at any other, installed as its own `make install` does:
# the setuid /usr/local/bin/isolate and its configuration file, which exec
# writes at the step's start. Beside it, our box launcher,
# src/box-launch.c, linked statically: exec runs every program in a box
# through it, and it refuses the keyring, inotify and fanotify calls isolate's
# own filter leaves, before it runs the program (see the file).
ARG ISOLATE_COMMIT=8f185bb37f3f23e29b33b0c7727c91c13429abe3
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates git gcc make \
       libc6-dev libcap-dev libseccomp-dev libsystemd-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --quiet https://github.com/ioi/isolate /src \
    && cd /src \
    && git checkout --quiet "$ISOLATE_COMMIT" \
    && test "$(git rev-parse HEAD)" = "$ISOLATE_COMMIT" \
    && make install DESTDIR=/out
COPY src/box-launch.c /box-launch.c
RUN gcc -O2 -Wall -Wextra -Werror -static -o /out/usr/local/bin/box-launch \
    /box-launch.c -lseccomp


FROM isolate AS installer
COPY requirements.txt /tmp/exec-requirements.txt
RUN python3 -m pip install --no-cache-dir --no-compile --only-binary=:all: --require-hashes \
        --target /rattler --requirement /tmp/exec-requirements.txt \
    && strip --strip-unneeded /rattler/rattler/*.so


FROM ${BASE_IMAGE}

LABEL org.opencontainers.image.title="primitive-exec" \
      org.opencontainers.image.description="The Unicon exec primitive: compile a program, run it under limits and measure it." \
      org.opencontainers.image.source="https://github.com/uniconhq/primitive-exec" \
      org.opencontainers.image.licenses="MIT"

# The installer is private to exec; every program toolchain comes from its lock.
COPY --from=installer /rattler/ /usr/local/lib/unicon-exec/

# isolate links libcap2 and libseccomp2, which the base image has; box-launch
# links nothing. Every box sees it, under the /usr isolate binds into each.
COPY --from=isolate /out/ /

# exec's package goes in a folder of its own outside site-packages, so nothing
# else imports it, compiled at build so no step writes bytecode beside it;
# /usr/local/bin/exec puts that folder on its path and runs it.
COPY src/exec/ /usr/local/lib/unicon-exec/exec/
RUN python3 -m compileall -q /usr/local/lib/unicon-exec
COPY --chmod=0755 src/command.py /usr/local/bin/exec

# The toolchain line, part of every compiled program's key and every
# environment's folder name in the machine's cache: the installed packages
# and their versions, the interpreter, and every file under
# /usr/local/lib/unicon-exec, exec's own, rattler's and uv's, so two images
# that would compile or assemble differently never share a program or an
# environment. The bytecode compileall wrote holds the time it was written,
# and the line's own file is made as the line is written, so both are left
# out.
RUN { dpkg-query -W; python3 -VV; \
      find /usr/local/lib/unicon-exec -type f ! -path '*/__pycache__/*' \
        ! -path /usr/local/lib/unicon-exec/toolchain -print0 \
        | LC_ALL=C sort -z | xargs -0 sha256sum; } \
    | sha256sum | cut -d' ' -f1 > /usr/local/lib/unicon-exec/toolchain

# exec runs as root: it sets up the step's cgroup, mounts each box's tmpfs and
# runs isolate, and each run is a box user of its own, never root.
WORKDIR /work
CMD ["/usr/local/bin/exec"]
