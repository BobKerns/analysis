#!/bin/sh
# One-time repo setup: uv, pinned Python, git hooks, deps, notebook filter.
# Safe to re-run; every step is idempotent.
set -eu

cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found; installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # uv's installer puts the binary in ~/.local/bin, which may not be on PATH yet.
    export PATH="$HOME/.local/bin:$PATH"
fi

echo "Configuring git hooks path..."
git config core.hooksPath .githooks

echo "Syncing dependencies..."
if ! uv sync --group dev; then
    echo "uv sync failed; installing pinned Python ($(cat .python-version)) and retrying..."
    uv python install
    uv sync --group dev
fi

echo "Installing nbstripout notebook filter..."
uv run nbstripout --install
git config filter.nbstripout.clean '"$(git rev-parse --show-toplevel)/.venv/bin/python" -m nbstripout --extra-keys "metadata.kernelspec.display_name metadata.language_info.version"'

echo "Setup complete."
