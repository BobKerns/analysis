#!/bin/sh
# Prepare the container, then run the command it was given (bash by default).
#
# The host's ~/.ssh arrives read-only at /root/.ssh-host. It is copied into
# /root/.ssh rather than used in place, so ssh sees the file modes it insists
# on and can add to known_hosts.
#
# On a fresh /work volume, clone the code repo (CODE_REPO_URL, at
# CODE_REPO_REF if set) into /work/analysis and, if DATA_REPO_URL is set, the
# data repo into /work/finance-data. A clone already in the volume is left as
# it is, so a half-finished setup survives a restart. Delete the volume to
# start over. scripts/start-container.sh sets the code repo's URL and ref
# from the checkout it runs in.
#
# With RUN_SETUP=1, run scripts/setup.sh in the code repo after that. A
# failure is reported but still leaves you in the container to look around.
set -eu

if [ -z "${CODE_REPO_URL:-}" ]; then
    echo "entrypoint: CODE_REPO_URL is not set; start the container with scripts/start-container.sh." >&2
    exit 2
fi

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

# Clone $1 into $2, at branch $3 if given and the remote has it.
clone_if_missing() {
    url=$1
    dir=$2
    ref=${3:-}
    if [ -d "$dir/.git" ]; then
        current=$(git -C "$dir" branch --show-current)
        if [ -n "$ref" ] && [ "$current" != "$ref" ]; then
            echo "$dir already cloned, on ${current:-a detached HEAD} rather than $ref; leaving it." >&2
        else
            echo "$dir already cloned; leaving it."
        fi
        return 0
    fi
    if [ -n "$ref" ]; then
        echo "Cloning ($ref) into $dir..."
        if git ls-remote --exit-code "$url" "refs/heads/$ref" "refs/tags/$ref" >/dev/null; then
            git clone --branch "$ref" "$url" "$dir"
            return $?
        else
            status=$?
            if [ "$status" -ne 2 ]; then
                return "$status"
            fi
        fi
        echo "No $ref at the remote (not pushed yet?); cloning its default branch instead." >&2
    else
        echo "Cloning into $dir..."
    fi
    git clone "$url" "$dir"
}

clone_if_missing "$CODE_REPO_URL" /work/analysis "${CODE_REPO_REF:-}"
if [ -n "${DATA_REPO_URL:-}" ]; then
    clone_if_missing "$DATA_REPO_URL" /work/finance-data
else
    echo "DATA_REPO_URL is not set; no data repo cloned (setup.sh can build template data)."
fi

cd /work/analysis
if [ "${RUN_SETUP:-}" = 1 ]; then
    if ./scripts/setup.sh; then
        :
    else
        echo "scripts/setup.sh failed (exit $?)." >&2
    fi
fi
exec "$@"
