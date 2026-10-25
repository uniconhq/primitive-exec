# The exec primitive, ghcr.io/uniconhq/primitive-exec. One container per batch
# of runs: it reads /work/inputs.json and the program and inputs under
# /work/in/, and writes /work/outputs.json and each run's output under
# /work/out/. Each run goes in an isolate box of its own, so the container
# runs as root and privileged (see "Where it runs" in the README).

# Pinned by digest so a release rebuilds from the same base. python:3.14-slim
# (Debian 13), pulled 2026-09-13, the same pin as the runner's images and the
# compile primitive, whose Python and Java versions this image must match to
# run what compile builds. Change it in both primitives together, from
# `docker image inspect python:3.14-slim --format '{{index .RepoDigests 0}}'`.
FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6 AS isolate

# isolate 2.7 (https://github.com/ioi/isolate), built from its tag's commit and
# refused at any other, installed as its own `make install` does: the setuid
# /usr/local/bin/isolate and its configuration file, which exec writes at
# the step's start. One patch of ours, src/isolate-keyrings.patch, refuses
# add_key and request_key where isolate refuses keyctl alone: a key one box
# adds outlives it, and the next box of its user lists it in /proc/keys.
ARG ISOLATE_COMMIT=8f185bb37f3f23e29b33b0c7727c91c13429abe3
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates git gcc make \
       libc6-dev libcap-dev libseccomp-dev libsystemd-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*
COPY src/isolate-keyrings.patch /keyrings.patch
RUN git clone --quiet https://github.com/ioi/isolate /src \
    && cd /src \
    && git checkout --quiet "$ISOLATE_COMMIT" \
    && test "$(git rev-parse HEAD)" = "$ISOLATE_COMMIT" \
    && git apply /keyrings.patch \
    && make install DESTDIR=/out


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

# isolate links libcap2 and libseccomp2, which the base image has.
COPY --from=isolate /out/ /
COPY --chmod=0755 src/exec.py /usr/local/bin/exec

# exec runs as root: it sets up the step's cgroup, mounts each box's tmpfs and
# runs isolate, and each run is a box user of its own, never root.
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/exec"]
