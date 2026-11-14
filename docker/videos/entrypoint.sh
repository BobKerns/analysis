#!/bin/sh
# Bring the environment volume up to date with the checkout, run the
# command, then give what it wrote under /out to whoever owns /out.
#
# The project's venv, uv's cache and Playwright's Chromium live in the
# volume at /opt/env (#612), synced from the mounted pyproject.toml and
# uv.lock: the first run fills it (several minutes), later ones find it
# current in a moment, and a dependency change is picked up here, with no
# new image.
#
# The container runs as root, so on a Linux host the videos would otherwise
# be root's. (Docker Desktop maps ownership itself; there this does nothing.)
# docker.sh runs the container with --init: as PID 1 this shell would
# ignore `docker stop` and Ctrl-C without a terminal, so the build ran on
# until Docker killed it. A stopped run skips the chown below.
set -u

if [ ! -x "$UV_PROJECT_ENVIRONMENT/bin/python" ]; then
    echo "Setting up the video environment volume (first time: several minutes)..." >&2
fi
# Output to stderr: `check` and the build print their own results on stdout.
uv sync --quiet --frozen --no-dev >&2 || exit
python -m playwright install chromium >&2 || exit
# The builder and VHS find Chromium through this link (see the Dockerfile).
chrome=$(python -c 'from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    print(p.chromium.executable_path)') || exit
ln -sfn "$chrome" "$PLAYWRIGHT_BROWSERS_PATH/chromium"

"$@"
status=$?
owner=$(stat -c %u:%g /out 2>/dev/null) || exit "$status"
if [ "$owner" != "0:0" ]; then
    # Only what this run made: a full chown -R would walk every cached clip.
    find /out \( ! -uid "${owner%:*}" -o ! -gid "${owner#*:}" \) -exec chown -h "$owner" {} +
fi
exit "$status"
