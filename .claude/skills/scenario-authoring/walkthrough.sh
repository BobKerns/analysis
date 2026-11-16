#!/usr/bin/env bash
# Replay the skill's commands on a throwaway copy of prototype_data/ (Alice
# and Bill), to check they still work as SKILL.md says. Never touches the
# real data repo.
#
#   .claude/skills/scenario-authoring/walkthrough.sh [OUT_DIR]
#
# OUT_DIR (default: $TMPDIR/analysis-authoring) is rebuilt each time. Exits
# non-zero at the first command that fails. Afterwards, in your own shell:
#   source OUT_DIR/datarepo/sim/activate-analysis
# to look around. Takes about a minute (two 6-member ensembles and a pool).
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
out="${1:-${TMPDIR:-/tmp}/analysis-authoring}"
data="$out/datarepo"

unset VIRTUAL_ENV  # an editor terminal's may name another checkout's venv
rm -rf "$data"
mkdir -p "$data"
cp -R "$repo/prototype_data" "$data/sim"
printf '%s\n' "$repo" > "$data/sim/.analysis"
printf 'alice\n' > "$data/sim/.analysis_user"
printf 'state/\n' > "$data/.gitignore"
# Runs record the data's git commit, so the data must be a repo of its own.
git -C "$data" init -q -b main
# shellcheck disable=SC1091
source "$data/sim/activate-analysis" </dev/null >/dev/null
commit() {
    git -C "$data" add -A
    git -C "$data" -c user.name=walkthrough -c user.email=walkthrough@example.invalid \
        -c commit.gpgsign=false commit -qm "$1"
}
commit "prototype data"
export ANALYSIS_KEEP_SCENARIO=1 COLUMNS=160

step() { printf '\n== %s\n' "$*"; "$@" </dev/null; }
expect_in() {  # FILE PATTERN: fail unless FILE has a line matching PATTERN
    grep -q -- "$2" "$1" || { echo "walkthrough: $1 lacks $2" >&2; exit 1; }
}
s="$ANALYSIS_DATA_DIR/scenarios"

# A derived scenario, with changes now and at future dates.
X=downsize_2032
step sim scenario create $X retire_alice --description "Sell the house in 2032 and rent"
step sim calendar add set-amount 2032-07 housing 2400 --scenario $X --description "rent"
step sim scheduled edit living --amount 5000 --scenario $X
step sim scheduled add 2031-01 Asset_Checking Expense_Living 400 --name travel \
    --end 2036-12 --series cpi --scenario $X
step sim account add Asset_Savings_Emergency asset cash --owner joint \
    --opening-balance 10000 --scenario $X
step sim transfer add 2032-06 Income_Inheritance Asset_Savings_Emergency 25000 \
    --tax-character none --scenario $X
step sim invest Asset_Savings Asset_Brokerage_Joint bonds --symbol ABCDX \
    --symbol-name "Test bond fund" --weight bond=100 --no-fetch \
    --amount 10000 --date 2027-03 --scenario $X
step sim person lifespan set alice 92 --scenario $X
step sim guard-rail edit budget --threshold 95000 --scenario $X
expect_in "$s/$X/scheduled.csv" '^living,'
expect_in "$s/$X/calendar.csv" '^2032-07,set_amount,,housing'
expect_in "$ANALYSIS_DATA_DIR/securities.csv" '^ABCDX,'  # shared, not per-scenario

# A household mix-in, built on the scenario holding the accounts it names.
step sim scenario create m_high_healthcare retire_alice --description "Mix-in: higher healthcare"
step sim calendar add set-amount 2030-01 healthcare 1800 --name high_healthcare \
    --scenario m_high_healthcare
step sim scenario disable m_high_healthcare
step sim scenario create downsize_high_hc $X m_high_healthcare
# A template episode as a mix-in; an inherited rule ended there.
step sim scenario create downsize_gi $X t_great_inflation
step sim scheduled delete housing --scenario downsize_gi --yes
# A sibling with other parents.
step sim scenario copy $X downsize_2033
step sim scenario edit downsize_2033 --description "A year later" \
    --parents retire_alice:m_high_healthcare
step sim account lot add Asset_Brokerage_Joint cash_reserve --opening-balance 5000 \
    --scenario downsize_2033

# Ensembles: a varying row, episodes, a marker moved by a piped guided edit,
# and a pool.
step sim ensemble create e0 $X t_inflation_ensemble t_market_ensemble --size 6
step sim calendar add set-amount 2031-01 travel 400 --name travel_vary --scenario e0 \
    --vary-target amount --vary-distribution triangular \
    --vary-low 200 --vary-high 900 --vary-mode 400
step sim ensemble create e_gi e0 t_great_inflation
step sim ensemble create e_fc e0 t_financial_crisis
printf '\n== sim calendar edit great_inflation_start (piped answers)\n'
printf 'vary-high\n2040-06-30\ndate\n2035-12-31\nwrite\n' |
    sim calendar edit great_inflation_start --scenario e_gi >/dev/null
expect_in "$s/e_gi/calendar.csv" '^2035-12-31,date_marker,great_inflation_start'
step sim ensemble pool create trouble e_gi=40% e_fc=60%
step sim ensemble pool edit trouble e_gi=30% e_fc=70%

# Check, trial-run dirty, commit, run, compare.
step sim check --all
step sim run $X --allow-dirty-data
commit "walkthrough scenarios"
step sim run retire_alice
step sim run $X
step sim compare retire_alice $X --section net-worth
step sim run trouble
step sim run e0
step sim ensemble compare e0 trouble
step sim switch $X
sim switch </dev/null | grep -q "Current scenario: $X\$" ||
    { echo "walkthrough: sim switch didn't make $X current" >&2; exit 1; }
printf '\nwalkthrough: ok (%s)\n' "$data"
