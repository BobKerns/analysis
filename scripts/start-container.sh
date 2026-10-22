#!/bin/sh
# Build the setup-test container (docker/setup-test) and start it: a clean
# Ubuntu for running scripts/setup.sh from scratch, with the code and data
# repos cloned into the analysis-test volume on first start.
#
#   scripts/start-container.sh [--fresh] [--setup] [command ...]
#
# --fresh deletes the volume first, so the clones (and anything setup.sh
# left in them) start over. --setup runs scripts/setup.sh in the container's
# clone before the command, which defaults to an interactive bash.
#
# The container clones this checkout's origin at its current branch (which
# must be pushed); CODE_REPO_URL and CODE_REPO_REF override them. It clones
# a data repo only if DATA_REPO_URL is set, e.g. in your shell profile.
set -eu

cd "$(dirname "$0")/.."

image=analysis-setup-test
volume=analysis-test

fresh=
setup_args=
while [ $# -gt 0 ]; do
    case $1 in
        --fresh) fresh=1 ;;
        --setup) setup_args="-e RUN_SETUP=1" ;;
        -h|--help) sed -n '2,14s/^# \{0,1\}//p' "$0"; exit 0 ;;
        --) shift; break ;;
        -*) echo "start-container: unknown option $1" >&2; exit 2 ;;
        *) break ;;
    esac
    shift
done

if [ -n "$fresh" ]; then
    echo "Removing volume $volume..."
    docker volume rm -f "$volume" >/dev/null
fi

docker build -q -t "$image" docker/setup-test >/dev/null

CODE_REPO_URL=${CODE_REPO_URL:-$(git remote get-url origin)}
CODE_REPO_REF=${CODE_REPO_REF:-$(git branch --show-current)}
export CODE_REPO_URL CODE_REPO_REF

env_args=
for v in CODE_REPO_URL CODE_REPO_REF DATA_REPO_URL; do
    if eval "[ -n \"\${$v:-}\" ]"; then
        env_args="$env_args -e $v"
    fi
done

if [ -t 0 ] && [ -t 1 ]; then
    tty_args=-it
else
    tty_args=-i
fi

# shellcheck disable=SC2086  # the *_args are lists of words, split on purpose
exec docker run --rm $tty_args $setup_args $env_args \
    -v "$volume":/work \
    -v "$HOME/.ssh":/home/ubuntu/.ssh-host:ro \
    "$image" "$@"
