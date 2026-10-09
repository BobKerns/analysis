#!/bin/sh
# Link this code repo with a data repo at data/shared (ADR-004 §4).
#
#   scripts/link-data.sh PATH        symlink data/shared to the data repo at PATH
#   scripts/link-data.sh --template  build a data/shared hierarchy from templates/
#   scripts/link-data.sh             ask for PATH; a blank answer, or no terminal
#                                    (CI), means --template
#
# Either way, `source data/shared/sim/activate-analysis` then gives a working
# environment. Safe to re-run: an existing data/shared is reported and left alone.
set -eu

cd "$(dirname "$0")/.."
REPO="$(pwd -P)"
SHARED=data/shared

usage() {
    sed -n '2,10s/^# \{0,1\}//p' "$0" >&2
    exit 2
}

mode=""
target=""
case "${1-}" in
    --template) mode=template ;;
    -h|--help) usage ;;
    -*) usage ;;
    "") ;;
    *) mode=link; target="$1" ;;
esac
[ $# -le 1 ] || usage

if [ -L "$SHARED" ]; then
    echo "$SHARED already links to $(readlink "$SHARED"); leaving it alone."
    exit 0
elif [ -e "$SHARED" ]; then
    echo "$SHARED already exists (template data?); leaving it alone."
    echo "To link a data repo instead, remove $SHARED and re-run this script."
    exit 0
fi

if [ -z "$mode" ]; then
    if [ -t 0 ]; then
        printf "Path to your data repo (blank to use template data): " >&2
        read -r target || target=""
    fi
    if [ -n "$target" ]; then mode=link; else mode=template; fi
fi

if [ "$mode" = template ]; then
    mkdir -p "$SHARED/state/intent" "$SHARED/state/runs" "$SHARED/.cspell"
    # cspell.json loads this dictionary; the template data adds no words.
    : > "$SHARED/.cspell/data-words.txt"
    cp -R templates "$SHARED/sim"
    printf '%s\n' "$REPO" > "$SHARED/sim/.analysis"
    echo "Built template data in $SHARED, used as 'test'."
    echo "Activate it with: source $SHARED/sim/activate-analysis"
    exit 0
fi

case "$target" in
    "~") target="$HOME" ;;
    "~/"*) target="$HOME/${target#"~/"}" ;;
esac
if [ ! -f "$target/sim/persons.csv" ]; then
    echo "link-data: $target is not a data repo (no sim/persons.csv)." >&2
    exit 1
fi
target="$(cd "$target" && pwd -P)"

# The user prompt needs the venv's Python; setup.sh creates it first.
PYTHON="$REPO/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    echo "link-data: $PYTHON not found; run scripts/setup.sh first." >&2
    exit 1
fi

ln -s "$target" "$SHARED"
printf '%s\n' "$REPO" > "$SHARED/sim/.analysis"

# Keep the per-machine files out of the data repo, as activate-analysis does.
for ignored in .analysis .current_scenario .analysis_user; do
    if [ ! -f "$SHARED/sim/.gitignore" ] || ! grep -qxF "$ignored" "$SHARED/sim/.gitignore"; then
        printf '%s\n' "$ignored" >> "$SHARED/sim/.gitignore"
    fi
done

if user="$("$PYTHON" -m analysis.sim.choose_user "$SHARED/sim")"; then
    printf '%s\n' "$user" > "$SHARED/sim/.analysis_user"
    echo "Linked $SHARED to $target, used as '$user'."
else
    echo "Linked $SHARED to $target; no user chosen, so activate-analysis will ask." >&2
fi
echo "Activate it with: source $SHARED/sim/activate-analysis"
