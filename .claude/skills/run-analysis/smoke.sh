#!/usr/bin/env bash
# Build a throwaway data repo from prototype_data/ (Alice and Bill), then load,
# run, report, chart, and execute notebooks against it.
#
#   .claude/skills/run-analysis/smoke.sh [OUT_DIR] [--fresh] [--no-notebooks]
#
# OUT_DIR (default: $TMPDIR/analysis-smoke) gets:
#   datarepo/           a git repo shaped like the real data repo: sim/ + state/
#   png/                charts written by the CLI's --graph-file
#   nb/                 executed notebooks as HTML (see nbrun.py)
# --fresh rebuilds datarepo/ from prototype_data/ (it is otherwise reused).
#
# Afterwards, in your own shell:
#   source OUT_DIR/datarepo/sim/activate-analysis
# and any `sim ...` command works against the same data.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
out="${TMPDIR:-/tmp}/analysis-smoke"
fresh=0
notebooks=1
for arg in "$@"; do
    case "$arg" in
        --fresh) fresh=1 ;;
        --no-notebooks) notebooks=0 ;;
        -*) sed -n '2,16s/^# \{0,1\}//p' "$0" >&2; exit 2 ;;
        *) out="$arg" ;;
    esac
done
mkdir -p "$out"
out="$(cd "$out" && pwd)"
data="$out/datarepo"

# An editor terminal's VIRTUAL_ENV may name another checkout's venv.
unset VIRTUAL_ENV
[ -x "$repo/.venv/bin/python" ] || (cd "$repo" && uv sync --group dev)

if [ "$fresh" = 1 ]; then rm -rf "$data"; fi
if [ ! -d "$data/.git" ]; then
    echo "== building $data from prototype_data/"
    mkdir -p "$data"
    cp -R "$repo/prototype_data" "$data/sim"
    # prototype_data's base has no accounts, unlike a real household's, and
    # some notebook views fail on an empty run (net_worth.ipynb always opens
    # base). Give base the household; retire_alice ships its own copies of
    # these files, so its results don't change.
    s="$data/sim/scenarios"
    cp "$s"/retire_alice/{accounts,account_lots,scheduled,calendar}.csv "$s/base/"
    cp -R "$s/retire_alice/chains" "$s/base/"
    cp "$s"/retire_alice/series/rate__*.csv "$s/base/series/"
    # activate-analysis reads these instead of prompting.
    printf '%s\n' "$repo" > "$data/sim/.analysis"
    printf 'alice\n' > "$data/sim/.analysis_user"
    printf 'state/\n' > "$data/.gitignore"
    # The run's provenance needs a git repo of its own around the data; a
    # dir inside the code repo (data/shared from link-data.sh --template)
    # fails in `git add` because data/ is ignored there.
    git -C "$data" init -q -b main
fi

# shellcheck disable=SC1091
source "$data/sim/activate-analysis" </dev/null
# Activation adds sim/.gitignore; commit it so the data reads as clean.
if [ -n "$(git -C "$data" status --porcelain)" ]; then
    git -C "$data" add -A
    # No signing: a global commit.gpgsign would need gpg (and a key) here.
    git -C "$data" -c user.name=smoke -c user.email=smoke@example.invalid \
        -c commit.gpgsign=false commit -qm "smoke data"
fi

# Background scenario builds (`sim builder`, git hooks) would queue work this
# test never asked for, and opening a run waits for a pending build of it. A
# fresh repo has none; stop if someone installed it here.
if ! sim builder status | grep -q "aren't installed"; then
    echo "smoke: sim builder is installed in $data; run: sim builder uninstall" >&2
    exit 1
fi

echo "== sim check --all"
sim check --all
for s in base retire_alice; do
    echo "== sim run $s"
    sim run "$s"
done
echo "== sim list"
sim list
echo "== sim explain retire_alice"
sim explain retire_alice | sed -n 1,20p
echo "== sim networth retire_alice"
sim networth retire_alice | sed -n 1,8p

mkdir -p "$out/png"
echo "== charts -> $out/png"
sim compare base retire_alice --graph-file "$out/png/compare.png" >/dev/null
sim person show alice --graph-file "$out/png/alice.png" >/dev/null
ls "$out/png"

if [ "$notebooks" = 1 ]; then
    # Notebooks open the current scenario, and compare just made it `base`
    # (empty in prototype_data), so point them back at the household.
    sim switch retire_alice
    echo "== notebooks -> $out/nb"
    # Kernel chatter (TCP warnings) goes to stderr; the summary to stdout.
    python "$here/nbrun.py" "$out/nb" \
        "$repo/notebooks/net_worth.ipynb" \
        "$repo/notebooks/accounts.ipynb" \
        "$repo/notebooks/scenario_overview.ipynb" 2>/dev/null
fi

echo
echo "smoke ok. For more commands:  source $data/sim/activate-analysis"
