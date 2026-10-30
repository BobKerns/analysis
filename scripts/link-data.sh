#!/bin/sh
# Link this code repo with a data repo at data/shared (ADR-004 §4).
#
#   scripts/link-data.sh PATH        symlink data/shared to the data repo at PATH;
#                                    at a terminal, offer to create one if PATH
#                                    has none
#   scripts/link-data.sh --new PATH [OPTION...]
#                                    create a data repo at PATH with `sim init`
#                                    (OPTIONs are its: --id, --display, ...),
#                                    then link it
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
    sed -n '2,16s/^# \{0,1\}//p' "$0" >&2
    exit 2
}

mode=""
target=""
case "${1-}" in
    --template) mode="template" ;;
    --new)
        [ $# -ge 2 ] || usage
        mode="new"; target="$2"; shift 2
        ;;
    -h|--help) usage ;;
    -*) usage ;;
    "") ;;
    *) mode="link"; target="$1" ;;
esac
if [ "$mode" = new ]; then
    : # the rest of the arguments are sim init's options
elif [ $# -gt 1 ]; then
    usage
fi

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
        printf "Path to your data repo, or where to create one (blank to use template data): " >&2
        read -r target || target=""
    fi
    if [ -n "$target" ]; then mode="link"; else mode="template"; fi
fi

if [ "$mode" = template ]; then
    mkdir -p "$SHARED/state/intent" "$SHARED/state/runs" "$SHARED/.cspell"
    # cspell.json loads this dictionary; the template data adds no words.
    : > "$SHARED/.cspell/data-words.txt"
    cp -R templates "$SHARED/sim"
    # The t_ template scenarios are read from this repo's templates/ (ADR-003
    # §4.5a); a copy in the data would replace them and go stale.
    find "$SHARED/sim/scenarios" -mindepth 1 -maxdepth 1 -name 't_*' -exec rm -r {} +
    # So are the aux tables (#317), all but the household's filing status.
    find "$SHARED/sim/aux" -name '*.csv' ! -name filing_status.csv -delete
    printf '%s\n' "$REPO" > "$SHARED/sim/.analysis"
    echo "Built template data in $SHARED, used as 'test'."
    echo "Activate it with: source $SHARED/sim/activate-analysis"
    exit 0
fi

case "$target" in
    \~) target="$HOME" ;;
    \~/*) target="$HOME/${target#\~/}" ;;
esac

# The user prompt, and `sim init`, need the venv; setup.sh creates it first.
PYTHON="$REPO/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    echo "link-data: $PYTHON not found; run scripts/setup.sh first." >&2
    exit 1
fi

# No data repo there yet: at a terminal, offer to create one (#308).
if [ "$mode" = link ] && [ ! -f "$target/sim/persons.csv" ] && [ -t 0 ]; then
    printf "There's no data repo at %s. Create a new one there? [Y/n] " "$target" >&2
    read -r answer || answer=""
    case $answer in
        [Nn]*) ;;
        *) mode="new" ;;
    esac
fi
if [ "$mode" = new ]; then
    # Background builds are setup.sh's to turn on (never under CI or an
    # agent), once data/shared links here.
    "$REPO/.venv/bin/sim" init "$target" --no-builder "$@"
fi
if [ ! -f "$target/sim/persons.csv" ]; then
    echo "link-data: $target is not a data repo (no sim/persons.csv)." >&2
    echo "  To create one there: scripts/link-data.sh --new $target" >&2
    exit 1
fi
target="$(cd "$target" && pwd -P)"

ln -s "$target" "$SHARED"
printf '%s\n' "$REPO" > "$SHARED/sim/.analysis"

# Keep the per-machine files out of the data repo, as activate-analysis does.
for ignored in .analysis .current_scenario .analysis_user; do
    if [ ! -f "$SHARED/sim/.gitignore" ] || ! grep -qxF "$ignored" "$SHARED/sim/.gitignore"; then
        printf '%s\n' "$ignored" >> "$SHARED/sim/.gitignore"
    fi
done

if [ -s "$SHARED/sim/.analysis_user" ]; then
    # sim init (or an earlier link from this machine) already chose.
    IFS= read -r user < "$SHARED/sim/.analysis_user"
    echo "Linked $SHARED to $target, used as '$user'."
elif user="$("$PYTHON" -m analysis.sim.choose_user "$SHARED/sim")"; then
    printf '%s\n' "$user" > "$SHARED/sim/.analysis_user"
    echo "Linked $SHARED to $target, used as '$user'."
else
    echo "Linked $SHARED to $target; no user chosen, so activate-analysis will ask." >&2
fi
echo "Activate it with: source $SHARED/sim/activate-analysis"
