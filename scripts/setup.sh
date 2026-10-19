#!/bin/sh
# One-time repo setup: a C toolchain, uv, pinned Python, git hooks, deps,
# notebook filter, Node.js and the lint tools, and the link to the data repo.
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
            # first of these that exists, or none, so .profile is the default.
            add_local_bin "$HOME/.bashrc"
            login_file=$HOME/.profile
            for f in "$HOME/.bash_profile" "$HOME/.bash_login"; do
                if [ -f "$f" ]; then
                    login_file=$f
                    break
                fi
            done
            add_local_bin "$login_file" .bashrc
        fi
        case $shell_name in
            zsh|bash|"") ;;
            *) echo "Add ~/.local/bin to PATH in your $shell_name startup file." >&2 ;;
        esac
        ;;
esac

# Some dependencies build C extensions where no wheel fits, so a C and C++
# compiler and make must be there. On Linux, install them with the system's
# package manager: apt (Ubuntu and Debian, WSL included), or on a best-effort
# basis dnf, zypper, pacman or apk. If that can't be done, say what to
# install and stop.
toolchain_missing() {
    echo "setup.sh needs a C compiler, a C++ compiler and make to build some" >&2
    echo "dependencies. Install them, then re-run this script:" >&2
    echo "    $1" >&2
    exit 1
}
have_toolchain() {
    command -v cc >/dev/null 2>&1 && command -v c++ >/dev/null 2>&1 \
        && command -v make >/dev/null 2>&1
}
if [ "$(uname -s)" = Darwin ]; then
    # /usr/bin/cc exists even without the command-line tools; it just offers
    # to install them, so ask xcode-select instead.
    if ! xcode-select -p >/dev/null 2>&1; then
        toolchain_missing "xcode-select --install"
    fi
elif ! have_toolchain; then
    if [ "$(id -u)" -eq 0 ]; then
        sudo=
    else
        sudo="sudo "
    fi
    if command -v apt-get >/dev/null 2>&1; then
        install="${sudo}apt-get update && ${sudo}apt-get install -y build-essential"
    elif command -v dnf >/dev/null 2>&1; then
        install="${sudo}dnf install -y gcc gcc-c++ make"
    elif command -v zypper >/dev/null 2>&1; then
        install="${sudo}zypper install -y gcc gcc-c++ make"
    elif command -v pacman >/dev/null 2>&1; then
        install="${sudo}pacman -S --needed --noconfirm base-devel"
    elif command -v apk >/dev/null 2>&1; then
        install="${sudo}apk add build-base"
    else
        toolchain_missing "your distribution's C compiler, C++ compiler and make"
    fi
    if [ -n "$sudo" ] && ! command -v sudo >/dev/null 2>&1; then
        toolchain_missing "$install"
    fi
    echo "C compiler or make not found; installing them with: $install"
    if ! sh -c "$install" || ! have_toolchain; then
        toolchain_missing "$install"
    fi
fi

if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found; installing..."
    # The step above owns the startup files; keep uv's installer out of them.
    curl -LsSf https://astral.sh/uv/install.sh | UV_NO_MODIFY_PATH=1 sh
fi

if ! command -v ripwire >/dev/null 2>&1; then
    echo "ripwire not found; installing..."
    RIPWIRE_REPO=redhat-et/ripwire \
    RIPWIRE_INSTALL_YES=1 bash -c \
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
# "engines" in package.json, the strictest range among the locked packages,
# and .node-version (the version installed below) inside it.
node_supported() {
    node -e '
        const [major, minor, patch] = process.versions.node.split(".").map(Number);
        const ok = (major === 22 && (minor > 22 || (minor === 22 && patch >= 2)))
            || (major === 24 && minor >= 15)
            || major >= 26;
        process.exit(ok ? 0 : 1);
    '
}

# Install the Node.js pinned in .node-version from nodejs.org, unpacked under
# ~/.local/share/node with node, npm and npx linked into ~/.local/bin, after
# checking it against nodejs.org's published SHA-256 sums.
install_node() {
    version=$(cat .node-version)
    case $(uname -s) in
        Darwin) os=darwin ;;
        Linux) os=linux ;;
        *) echo "No Node.js download for $(uname -s); install Node.js yourself." >&2; return 1 ;;
    esac
    case $(uname -m) in
        x86_64|amd64) arch=x64 ;;
        arm64|aarch64) arch=arm64 ;;
        *) echo "No Node.js download for $(uname -m); install Node.js yourself." >&2; return 1 ;;
    esac
    name=node-v$version-$os-$arch
    dest=$HOME/.local/share/node/$name
    if [ ! -x "$dest/bin/node" ]; then
        echo "Installing Node.js $version into $dest..."
        url=https://nodejs.org/dist/v$version
        tmp=$(mktemp -d) || return 1
        if ! curl -fsSL -o "$tmp/$name.tar.gz" "$url/$name.tar.gz" \
            || ! curl -fsSL -o "$tmp/SHASUMS256.txt" "$url/SHASUMS256.txt"; then
            rm -rf "$tmp"
            return 1
        fi
        want=$(awk -v f="$name.tar.gz" '$2 == f { print $1 }' "$tmp/SHASUMS256.txt")
        if command -v sha256sum >/dev/null 2>&1; then
            got=$(sha256sum "$tmp/$name.tar.gz" | cut -d' ' -f1)
        else
            got=$(shasum -a 256 "$tmp/$name.tar.gz" | cut -d' ' -f1)
        fi
        if [ -z "$want" ] || [ "$got" != "$want" ]; then
            echo "$name.tar.gz doesn't match nodejs.org's SHA-256 sum; not installing it." >&2
            rm -rf "$tmp"
            return 1
        fi
        if ! mkdir -p "$HOME/.local/share/node" \
            || ! tar -xzf "$tmp/$name.tar.gz" -C "$HOME/.local/share/node"; then
            rm -rf "$tmp"
            return 1
        fi
        rm -rf "$tmp"
    fi
    mkdir -p "$HOME/.local/bin" || return 1
    for tool in node npm npx; do
        ln -sf "../share/node/$name/bin/$tool" "$HOME/.local/bin/$tool" || return 1
    done
}

# Neither failure below is fatal: running the simulator and notebooks needs
# no Node. Only the spelling and markdown gates do.
if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1 || ! node_supported; then
    if command -v node >/dev/null 2>&1; then
        echo "Node.js $(node --version) is too old for the pinned cspell/markdownlint."
    else
        echo "Node.js not found."
    fi
    if install_node; then
        hash -r 2>/dev/null || true
        if ! node_supported; then
            # An older node earlier on PATH than ~/.local/bin hides the new one.
            echo "An older node ($(command -v node)) comes before ~/.local/bin on PATH;" >&2
            echo "using ~/.local/bin/node for this run. Put ~/.local/bin first to use it everywhere." >&2
            export PATH="$HOME/.local/bin:$PATH"
        fi
    fi
fi
if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1 || ! node_supported; then
    echo "Couldn't get a Node.js new enough for cspell/markdownlint (22.22.2+, 24.15+," >&2
    echo "or 26+); skipping them. Install one and re-run this script before using" >&2
    echo "'npm run spell' / 'npm run markdownlint'." >&2
else
    echo "Installing pinned lint tools (cspell, markdownlint)..."
    npm ci --no-audit --no-fund
fi

# Last, so the venv is there for the user prompt. Asks for the data repo's
# path; a blank answer (or no terminal) builds template data instead.
echo "Linking the data repo at data/shared..."
scripts/link-data.sh

echo "Setup complete."
