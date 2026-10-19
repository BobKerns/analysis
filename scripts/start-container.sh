#!/bin/sh
# Build the setup-test container (docker/setup-test) and start it: a clean
# Ubuntu for running scripts/setup.sh from scratch, with the code and data
# repos cloned into the analysis-test volume on first start.
#
#   scripts/start-container.sh [--fresh] [command ...]
#
# --fresh deletes the volume first, so the clones (and anything setup.sh
# left in them) start over. The command defaults to an interactive bash.
# CODE_REPO_URL and DATA_REPO_URL, if set, are passed through to override
# the clone URLs.
set -eu

cd "$(dirname "$0")/.."

image=analysis-setup-test
volume=analysis-test

if [ "${1:-}" = --fresh ]; then
    shift
    echo "Removing volume $volume..."
    docker volume rm -f "$volume" >/dev/null
fi

docker build -q -t "$image" docker/setup-test >/dev/null

env_args=
for v in CODE_REPO_URL DATA_REPO_URL; do
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
exec docker run --rm $tty_args $env_args \
    -v "$volume":/work \
    -v "$HOME/.ssh":/root/.ssh-host:ro \
    "$image" "$@"
