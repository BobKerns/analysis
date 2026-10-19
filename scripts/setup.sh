#!/bin/sh
# One-time repo setup: uv, pinned Python, git hooks, deps, notebook filter,
# lint tools, and the link to the data repo.
# Safe to re-run; every step is idempotent.
set -eu

cd "$(dirname "$0")/.."

# Append the PATH line to startup file $1, unless it already mentions
# ~/.local/bin or, for a login file, sources the rc file named by $2 (which
# gets the line itself).
add_local_bin() {
    if grep -qs '\.local/bin' "$1"; then
        return 0
    fi
    if [ -n "${2:-}" ] && grep -qs "$2" "$1"; then
        return 0
    fi
    cat >> "$1" <<'EOF'

# Added by the analysis repo's scripts/setup.sh: uv and ripwire live here.
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac
EOF
    echo "Added ~/.local/bin to PATH in $1; new shells will pick it up."
}

# uv's and ripwire's installers put their binaries in ~/.local/bin. If it
# isn't on PATH, add it for this run and in the shell's startup files.
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *)
        export PATH="$HOME/.local/bin:$PATH"
        shell_name=$(basename "${SHELL:-}")
        zsh_dir=${ZDOTDIR:-$HOME}
        if [ "$shell_name" = zsh ] || [ -f "$zsh_dir/.zshrc" ]; then
            # Every interactive zsh reads .zshenv, then .zshrc.
            if ! grep -qs '\.local/bin' "$zsh_dir/.zshenv"; then
                add_local_bin "$zsh_dir/.zshrc"
            fi
        fi
        if [ "$shell_name" = bash ] || [ -f "$HOME/.bashrc" ]; then
            # Interactive shells read .bashrc; a login shell reads only the
            # first of these that exists.
            add_local_bin "$HOME/.bashrc"
            for login_file in "$HOME/.bash_profile" "$HOME/.bash_login" "$HOME/.profile"; do
                if [ -f "$login_file" ]; then
                    add_local_bin "$login_file" .bashrc
                    break
                fi
            done
        fi
        case $shell_name in
            zsh|bash|"") ;;
            *) echo "Add ~/.local/bin to PATH in your $shell_name startup file." >&2 ;;
        esac
        ;;
esac

if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found; installing..."
    # The step above owns the startup files; keep uv's installer out of them.
    curl -LsSf https://astral.sh/uv/install.sh | UV_NO_MODIFY_PATH=1 sh
fi

if ! command -v ripwire >/dev/null 2>&1; then
    echo "ripwire not found; installing..."
    RIPWIRE_REPO=redhat-et/ripwire bash -c \
        "$(curl -fsSL https://raw.githubusercontent.com/redhat-et/ripwire/main/scripts/install.sh)"
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
# shellcheck disable=SC2016  # single-quoted: git expands $(...) later, not this script
git config diff.ipynb.textconv '"$(git rev-parse --show-toplevel)/.venv/bin/python" -m nbstripout -t --extra-keys "metadata.kernelspec.display_name metadata.language_info.version"'

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

# Last, so the venv is there for the user prompt. Asks for the data repo's
# path; a blank answer (or no terminal) builds template data instead.
echo "Linking the data repo at data/shared..."
scripts/link-data.sh

echo "Setup complete."
