# Everything a video build needs (#382), so a build doesn't depend on what
# the host has installed: ffmpeg, VHS and ttyd for tapes, Playwright's
# Chromium for slides (and for VHS), the Kokoro voice model, the project's
# Python and Node.js, and the fonts the theme names.
#
# Don't run it by hand: scripts/videos/docker.sh builds it (from the repo
# root, with Dockerfile.dockerignore beside this file choosing what's sent)
# and runs it with
#
#   /repo/docs/videos  the checkout's video sources, read-only
#   /out               where the videos land (and the build cache, .cache)
#   /data              the video's own data repo (`sim init /data` makes
#                      /data/sim): a named volume per video
#
# The code and prototype_data are copied in, not mounted, so the CLI a tape
# records is the checkout's at the time of the build; the script rebuilds
# the image each run, which is quick when nothing changed.
#
# Works on amd64 and arm64 (Docker Desktop on Apple silicon).

FROM ubuntu:24.04

# Keep VHS_VERSION, TTYD_VERSION and KOKORO_RELEASE in step with
# src/analysis/videos (tests/test_videos.py checks).
ARG NODE_VERSION=22.22.2
ARG UV_VERSION=0.11.32
ARG VHS_VERSION=0.10.0
ARG TTYD_VERSION=1.7.7
ARG KOKORO_RELEASE=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0
ARG TARGETARCH

# UV_LINK_MODE=copy: the uv cache and the venv are on different layers.
# VHS_NO_SANDBOX: Chromium can't sandbox itself inside a container.
ENV LANG=C.UTF-8 \
    UV_LINK_MODE=copy \
    UV_CACHE_DIR=/opt/uv-cache \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_NO_PROGRESS=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
    ANALYSIS_VIDEO_CACHE=/opt/analysis-videos \
    ANALYSIS_VIDEO_DATA=/data/sim \
    ANALYSIS_DATA_DIR=/data/sim \
    VHS_NO_SANDBOX=true \
    PATH=/opt/venv/bin:/opt/node/bin:$PATH

# git: the tape sandbox commits its copy of the data. The fonts are the
# theme's (Inter, JetBrains Mono) plus CJK for translated slides.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates \
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
    case "$TARGETARCH" in \
        amd64) node=x64; uv=x86_64; vhs=x86_64; ttyd=x86_64 ;; \
        arm64) node=arm64; uv=aarch64; vhs=arm64; ttyd=aarch64 ;; \
        *) echo "unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    gh=https://github.com; \
    mkdir /opt/node; \
    curl -fsSL "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-$node.tar.xz" \
        | tar -xJ -C /opt/node --strip-components=1; \
    curl -fsSL "$gh/astral-sh/uv/releases/download/$UV_VERSION/uv-$uv-unknown-linux-gnu.tar.gz" \
        | tar -xz -C /usr/local/bin --strip-components=1; \
    curl -fsSL "$gh/charmbracelet/vhs/releases/download/v$VHS_VERSION/vhs_${VHS_VERSION}_Linux_$vhs.tar.gz" \
        | tar -xz -C /usr/local/bin --strip-components=1 --wildcards '*/vhs'; \
    curl -fsSL -o /usr/local/bin/ttyd "$gh/tsl0922/ttyd/releases/download/$TTYD_VERSION/ttyd.$ttyd"; \
    chmod 755 /usr/local/bin/ttyd /usr/local/bin/vhs; \
    mkdir -p "$ANALYSIS_VIDEO_CACHE"; \
    for f in kokoro-v1.0.onnx voices-v1.0.bin; do \
        curl -fsSL -o "$ANALYSIS_VIDEO_CACHE/$f" "$KOKORO_RELEASE/$f"; \
    done

WORKDIR /repo

# The narration worker's own environment (Python < 3.14, kokoro-onnx), made
# now so a build doesn't download it.
COPY scripts/videos/tts_kokoro.py scripts/videos/
RUN uv sync --quiet --script scripts/videos/tts_kokoro.py

# The project's dependencies, then Chromium (with the libraries it needs).
# /opt/pw-browsers/chromium is where the builder looks for it, and VHS finds
# `chromium` on PATH.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --quiet --frozen --no-dev --no-install-project \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/* \
 && chrome=$(find /opt/pw-browsers -path '*/chromium-*/chrome-linux*/chrome' -type f | head -n 1) \
 && test -n "$chrome" \
 && ln -s "$chrome" /opt/pw-browsers/chromium \
 && ln -s "$chrome" /usr/local/bin/chromium

COPY prototype_data/ prototype_data/
COPY src/ src/
RUN uv sync --quiet --frozen --no-dev

COPY docker/videos/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 755 /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "analysis.videos", "check"]
