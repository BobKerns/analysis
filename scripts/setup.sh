#!/bin/sh
# One-time repo setup: uv, pinned Python, git hooks, deps, notebook filter,
# lint tools.
# Safe to re-run; every step is idempotent.
set -eu

cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found; installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # uv's installer puts the binary in ~/.local/bin, which may not be on PATH yet.
    export PATH="$HOME/.local/bin:$PATH"
fi

if ! command -v ripwire >/dev/null 2>&1; then
    echo "ripwire not found; installing..."
    RIPWIRE_REPO=redhat-et/ripwire bash -c \
        "$(curl -fsSL https://raw.githubusercontent.com/redhat-et/ripwire/main/scripts/install.sh)"
    # ripwire's installer puts the binary in ~/.local/bin, which may not be on PATH yet.
    export PATH="$HOME/.local/bin:$PATH"
fi

if ! command -v ripwire >/dev/null 2>&1; then
    echo "ripwire installation failed or is not on PATH." >&2
    exit 1
fi
echo "Using $(ripwire --version)"

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
# shellcheck disable=SC2016  # single-quoted: git expands $(...) later, not this script
git config filter.nbstripout.clean '"$(git rev-parse --show-toplevel)/.venv/bin/python" -m nbstripout --extra-keys "metadata.kernelspec.display_name metadata.language_info.version"'

# The pinned lint tools need a recent Node. Keep this test in step with
# "engines" in package.json, the strictest range among the locked packages.
node_supported() {
    node -e '
        const [major, minor, patch] = process.versions.node.split(".").map(Number);
        const ok = (major === 22 && (minor > 22 || (minor === 22 && patch >= 2)))
            || (major === 24 && minor >= 15)
            || major >= 26;
        process.exit(ok ? 0 : 1);
    '
}

# Neither case below is fatal: running the simulator and notebooks needs no
# Node. Only the spelling and markdown gates do.
if ! command -v npm >/dev/null 2>&1 || ! command -v node >/dev/null 2>&1; then
    echo "Node.js not found; skipping cspell/markdownlint. Install Node.js and re-run" >&2
    echo "this script before using 'npm run spell' / 'npm run markdownlint'." >&2
elif ! node_supported; then
    echo "Node.js $(node --version) is too old for the pinned cspell/markdownlint, which" >&2
    echo "need 22.22.2+, 24.15+, or 26+; skipping them. Upgrade Node.js and re-run" >&2
    echo "this script before using 'npm run spell' / 'npm run markdownlint'." >&2
else
    echo "Installing pinned lint tools (cspell, markdownlint)..."
    npm ci --no-audit --no-fund
fi

echo "Setup complete."
