#!/bin/sh
# Prepare the container, then run the command it was given (bash by default).
#
# The host's ~/.ssh arrives read-only at /root/.ssh-host. It is copied into
# /root/.ssh rather than used in place, so ssh sees the file modes it insists
# on and can add to known_hosts.
#
# On a fresh /work volume, clone the code and data repos into it; a volume
# that already has them is left as it is, so a half-finished setup survives a
# restart. Delete the volume to start over.
#
# With RUN_SETUP=1, run scripts/setup.sh in the code repo after that. A
# failure is reported but still leaves you in the container to look around.
set -eu

CODE_REPO_URL=${CODE_REPO_URL:-git@github.com:BobKerns/analysis.git}
DATA_REPO_URL=${DATA_REPO_URL:-git:finance-data.git}

if [ -d /root/.ssh-host ]; then
    # Regular files only: agent and ControlMaster sockets can't be copied,
    # and one can vanish mid-copy.
    rm -rf /root/.ssh
    (cd /root/.ssh-host && find . -type d) | while read -r d; do
        mkdir -p "/root/.ssh/$d"
    done
    (cd /root/.ssh-host && find . -type f) | while read -r f; do
        cp "/root/.ssh-host/$f" "/root/.ssh/$f" 2>/dev/null || true
    done
    chmod -R go= /root/.ssh
fi

clone_if_missing() {
    url=$1
    dir=$2
    if [ -d "$dir/.git" ]; then
        echo "$dir already cloned; leaving it."
    else
        echo "Cloning $url into $dir..."
        git clone "$url" "$dir"
    fi
}

clone_if_missing "$CODE_REPO_URL" /work/analysis
clone_if_missing "$DATA_REPO_URL" /work/finance-data

cd /work/analysis
if [ "${RUN_SETUP:-}" = 1 ]; then
    if ./scripts/setup.sh; then
        :
    else
        echo "scripts/setup.sh failed (exit $?)." >&2
    fi
fi
exec "$@"
