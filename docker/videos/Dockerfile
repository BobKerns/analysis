# Everything a video build needs (#382), so a build doesn't depend on what
# the host has installed: ffmpeg, VHS and ttyd for tapes, the libraries
# Playwright's Chromium needs, the Kokoro voice model, uv and Node.js, and
# the fonts the theme names.
#
# Don't run it by hand: scripts/videos/docker.sh builds it, with this
# folder as the whole build context, and runs it with (#612)
#
#   /repo/...          the checkout, read-only: src, pyproject.toml, uv.lock,
#                      README.md, templates, historical_data, prototype_data,
#                      notebooks, scripts/videos, docs/videos
#   /opt/env           the named volume analysis-videos-env: the project's
#                      venv, uv's cache and Pythons, Playwright's Chromium
#   /out               where the videos land (and the build cache, .cache)
#   /data              the video's own data repo (`sim init /data` makes
#                      /data/sim): a named volume per video
#
# The entrypoint runs `uv sync` into the volume before each command (a
# quick check once it's done), so the CLI a tape records is the checkout's
# as it stands, with the checkout's dependencies, and neither a code nor a
# dependency change needs a new image. The host's .venv isn't used: it may
# be for another OS or architecture. The image changes only when this
# folder does: docker.sh rebuilds it then, and `analysis video image`
# rebuilds it from scratch for newer system packages.
#
# Works on amd64 and arm64 (Docker Desktop on Apple silicon). Needs
# BuildKit (docker.sh turns it on) for TARGETARCH.

FROM ubuntu:24.04

# Keep VHS_VERSION, TTYD_VERSION and KOKORO_RELEASE in step with
# src/analysis/videos, and PLAYWRIGHT_VERSION with uv.lock (tests/test_videos.py
# checks): it chooses the system libraries Chromium is given. VHS comes from Bob's
# fork, which records terminal images (render.VHS_VERSION says why).
ARG NODE_VERSION=22.22.2
ARG UV_VERSION=0.11.32
ARG VHS_VERSION=0.12.1-analysis.2
ARG TTYD_VERSION=1.7.7
ARG PLAYWRIGHT_VERSION=1.64.0
ARG KOKORO_RELEASE=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0
ARG TARGETARCH

# VHS_NO_SANDBOX: Chromium can't sandbox itself inside a container.
# ANALYSIS_BUILDER=off: no background builds (#300); a tape runs what it shows.
# PYTHONPYCACHEPREFIX: the code is mounted read-only, so compiled modules go
# here, once per container run.
ENV LANG=C.UTF-8 \
    UV_CACHE_DIR=/opt/env/uv-cache \
    UV_PYTHON_INSTALL_DIR=/opt/env/uv-python \
    UV_PROJECT_ENVIRONMENT=/opt/env/venv \
    UV_NO_PROGRESS=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/env/pw-browsers \
    ANALYSIS_VIDEO_CACHE=/opt/analysis-videos \
    ANALYSIS_VIDEO_DATA=/data/sim \
    ANALYSIS_DATA_DIR=/data/sim \
    VHS_NO_SANDBOX=true \
    ANALYSIS_BUILDER=off \
    PYTHONPYCACHEPREFIX=/tmp/pycache \
    PATH=/opt/env/venv/bin:/opt/node/bin:$PATH

# git: the tape sandbox commits its copy of the data. build-essential and
# cmake: the narration worker's Japanese packages (misaki[ja]) build from
# source: pyopenjtalk has no wheels at all, and mojimoji none for arm64 or
# Python 3.13. The fonts are the theme's (Inter, JetBrains Mono) plus CJK
# for translated slides.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        cmake \
        curl \
        ffmpeg \
        fonts-dejavu-core \
        fonts-inter \
        fonts-jetbrains-mono \
        fonts-noto-cjk \
        git \
        xz-utils \
 && rm -rf /var/lib/apt/lists/*

RUN set -eu; \
    case "${TARGETARCH:-$(uname -m)}" in \
        amd64|x86_64) node=x64; uv=x86_64; vhs=x86_64; ttyd=x86_64 ;; \
        arm64|aarch64) node=arm64; uv=aarch64; vhs=arm64; ttyd=aarch64 ;; \
        *) echo "unsupported architecture: ${TARGETARCH:-$(uname -m)}" >&2; exit 1 ;; \
    esac; \
    gh=https://github.com; \
    mkdir /opt/node; \
    curl -fsSL "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-$node.tar.xz" \
        | tar -xJ -C /opt/node --strip-components=1; \
    curl -fsSL "$gh/astral-sh/uv/releases/download/$UV_VERSION/uv-$uv-unknown-linux-gnu.tar.gz" \
        | tar -xz -C /usr/local/bin --strip-components=1; \
    curl -fsSL "$gh/BobKerns/vhs/releases/download/v$VHS_VERSION/vhs_${VHS_VERSION}_Linux_$vhs.tar.gz" \
        | tar -xz -C /usr/local/bin --strip-components=1 --wildcards '*/vhs'; \
    curl -fsSL -o /usr/local/bin/ttyd "$gh/tsl0922/ttyd/releases/download/$TTYD_VERSION/ttyd.$ttyd"; \
    chmod 755 /usr/local/bin/ttyd /usr/local/bin/vhs; \
    mkdir -p "$ANALYSIS_VIDEO_CACHE"; \
    for f in kokoro-v1.0.onnx voices-v1.0.bin; do \
        curl -fsSL -o "$ANALYSIS_VIDEO_CACHE/$f" "$KOKORO_RELEASE/$f"; \
    done

# Chromium's system libraries, from the npm package of the Playwright the
# project uses; Chromium itself goes in the volume (the entrypoint installs
# it). /opt/pw-browsers/chromium is where the builder looks for it, and VHS
# finds `chromium` on PATH: both point into the volume.
RUN npx --yes "playwright@$PLAYWRIGHT_VERSION" install-deps chromium \
 && rm -rf /var/lib/apt/lists/* /root/.npm \
 && mkdir -p /opt/pw-browsers \
 && ln -s /opt/env/pw-browsers/chromium /opt/pw-browsers/chromium \
 && ln -s /opt/env/pw-browsers/chromium /usr/local/bin/chromium

# /repo/.venv: activate-analysis, which video 2 sources, looks for the
# code's venv there. The checkout is mounted piece by piece, so the host's
# own .venv never shadows it.
WORKDIR /repo
RUN ln -s /opt/env/venv .venv

# An identity for commits made in the container (`sim init`, the CLI's
# data commits), which has no user of its own.
RUN git config --system user.name "Video builder" \
 && git config --system user.email "videos@example.com"

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 755 /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "analysis.videos", "check"]
