#!/usr/bin/env bash
# Open a notebook in the user's browser, starting JupyterLab first if it
# isn't running.
#
#   .claude/skills/analysis-results/notebook.sh NOTEBOOK.ipynb
#
# Needs env.sh sourced (the real data activated). JupyterLab is started the
# way `analysis jupyter` starts it, from the main checkout ($ANALYSIS_REPO_DIR),
# in the background with its log in state/jupyter.log, and keeps running for
# the user afterwards (`jupyter lab stop <port>` stops it).
set -euo pipefail

notebook="${1:?usage: notebook.sh NOTEBOOK.ipynb}"
repo="${ANALYSIS_REPO_DIR:?source env.sh first}"
[ -f "$repo/notebooks/$notebook" ] || { echo "notebook.sh: no notebooks/$notebook" >&2; exit 2; }

running() {
    # `jupyter lab list` prints "http://localhost:PORT/?token=T :: DIR".
    jupyter lab list 2>/dev/null | sed -n "s|^\(http://[^/]*/\)?token=\([0-9a-f]*\) :: $repo/notebooks\$|\1 \2|p" | head -1
}

server="$(running)"
if [ -z "$server" ]; then
    mkdir -p "$repo/state"
    (cd "$repo" && nohup analysis jupyter --no-browser >"$repo/state/jupyter.log" 2>&1 &)
    for _ in $(seq 1 60); do
        server="$(running)"
        [ -n "$server" ] && break
        sleep 1
    done
    [ -n "$server" ] || { echo "notebook.sh: JupyterLab didn't start; see $repo/state/jupyter.log" >&2; exit 1; }
    echo "notebook.sh: started JupyterLab (log: $repo/state/jupyter.log)"
fi
base="${server% *}"
token="${server#* }"
python -m webbrowser -t "${base}lab/tree/${notebook}?token=${token}" >/dev/null
echo "notebook.sh: opened $notebook (${base}lab/tree/$notebook)"
