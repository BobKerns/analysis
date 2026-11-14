# cspell:ignore BALX
# Prepares what video 11 shows, before any scene records (docs/videos/README.md,
# "Setting up the demo"). Video 11 brings Alice and Bill's plan up to date in
# January 2027, so this builds video 7's base with BALX's shares added, and
# 10a's rebalance and 10b's travel on it, then what the scenes make, tagging
# the data repo before each scene's commands (stage/<scene>): a tape resets to
# its tag, so it can make on camera what a later scene already has. Runs are
# renamed to fixed times, so every recording reads the same run.

export HOME="$(dirname "$ANALYSIS_DATA_DIR")/home"
mkdir -p "$HOME"
unset ANALYSIS_DATA_DIR
sim init ~/retirement/data --id alice --display "Alice Example" --birthdate 1962-04-15 \
    --sex f --filing-status MFJ
source ~/retirement/data/sim/activate-analysis > /dev/null
cd ~/retirement/data

stage() {  # commit what the commands before made, and mark where a scene starts
    git add -A
    git diff --cached --quiet || git commit -q -m "Before $1"
    git tag "stage/$1"
}

# Run a scenario and give the run a fixed time (its file name and its record).
demo_run() {
    sim run "$1" > /dev/null
    local run
    run="$(ls -t "state/runs/"*"_$1_"*.sqlite3 | head -1)"
    local stamp="${2:0:10} ${2:11:2}:${2:13:2}:${2:15:2}"
    python - "$run" "$stamp" <<'PY'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute("UPDATE simulation_run SET started_at = ?, finished_at = ?",
           (sys.argv[2].replace(" ", "T") + "Z", sys.argv[2].replace(" ", "T") + "Z"))
db.commit()
PY
    mv "$run" "state/runs/$2_$1_${run##*_"$1"_}"
}

sim person add bill --display "Bill Example" --birthdate 1964-08-20 --sex m --role primary

sim account add Asset_Checking asset cash --owner joint --opening-balance 15000
sim account add Asset_Savings asset cash --owner joint --series cash --tax-character ordinary \
    --opening-balance 60000
sim account add Asset_TraditionalIRA_Alice asset retirement --owner alice \
    --lot-subkind traditional_ira --series stock --opening-balance 500000

echo n | sim account add Asset_Brokerage_Joint asset brokerage_taxable --owner joint
sim account lot add Asset_Brokerage_Joint stocks --series stock_us --tax-character gains \
    --opening-balance 240000 --opening-cost-basis 150000
sim account lot add Asset_Brokerage_Joint bonds --series bond --opening-balance 160000 \
    --opening-cost-basis 155000

sim account add Asset_Roth_Bill asset retirement --owner bill --lot-subkind roth_ira --series stock \
    --opening-balance 120000
sim account add Income_Salary_Alice income salary --owner alice
sim account add Income_Salary_Bill income salary --owner bill
sim account add Income_SS_Alice income ss_benefit --owner alice
sim account add Income_SS_Bill income ss_benefit --owner bill
sim account add Expense_Living expense living --owner joint
sim account add Expense_Healthcare expense healthcare --owner joint
sim account add Expense_Taxes_Federal expense taxes_federal --owner joint

sim scheduled add 2026-01 Income_Salary_Alice Asset_Checking 8000 --name salary_alice \
    --person alice --series cpi --end 2028-06
sim scheduled add 2026-01 Income_Salary_Bill Asset_Checking 7000 --name salary_bill \
    --person bill --series cpi --end 2030-12
sim scheduled add 2026-01 Asset_Checking Expense_Living 7500 --name living --person joint --series cpi

sim scheduled add 2026-01 Asset_Checking Expense_Healthcare 900 --name healthcare --person joint \
    --series cpi
sim scheduled add 2026-01 Income_SS_Alice Asset_Checking 3000 --kind ss_benefit --name ss_alice \
    --person alice --claim-age 67 --series cpi
sim scheduled add 2026-01 Income_SS_Bill Asset_Checking 3200 --kind ss_benefit --name ss_bill \
    --person bill --claim-age 70 --series cpi
sim scheduled add 2026-01 Asset_TraditionalIRA_Alice Asset_Checking --kind rmd --name rmd_alice --from-lot default \
    --person alice
sim tax add federal --source-chain Asset_Checking --destination Expense_Taxes_Federal --start 2027-04


sim chain add spending Asset_Savings --role source
sim chain step add spending Asset_Brokerage_Joint --lot bonds
sim chain step add spending Asset_Brokerage_Joint --lot stocks
sim chain step add spending Asset_TraditionalIRA_Alice --lot default
sim account lot edit Asset_Checking --minimum-balance 5000 --refill-chain spending

sim calendar add transfer 2031-06 Asset_Checking Expense_Living 40000 --description "a new car"
sim calendar add set-amount 2036-01 living 6500 --description "less travel"

# Video 7's sweep (not its Roth conversion): RMD cash beyond $30,000 in
# checking is swept into the brokerage's stocks.
sim account lot edit Asset_Checking --maximum-balance 30000 \
    --overflow-chain Asset_Brokerage_Joint --overflow-lot stocks > /dev/null
# The balanced fund from video 8, which Alice and Bill hold by shares here:
# 1,000 of them at the start. Its prices are made up, so they're written in
# rather than fetched.
sim symbol add BALX --name "Example Balanced Fund" --weight stock_us=60% --weight bond=40% \
    --no-fetch > /dev/null
mkdir -p sim/prices
cat > sim/prices/security_price__BALX.csv <<'CSV'
effective_date,price
2025-12-31,50.00
2026-06-30,51.30
2026-12-31,47.80
CSV
sim account lot add Asset_Brokerage_Joint balanced --symbol BALX --quantity 1000 \
    --opening-cost-basis 42000 > /dev/null
git add -A && git commit -q -m "Video 7's base"

# 10a's rebalance scenario, as that video makes it.
sim scenario create rebalance base > /dev/null
for account in Asset_Brokerage_Joint Asset_TraditionalIRA_Alice Asset_Roth_Bill; do
    sim account tag add "$account" portfolio:invest > /dev/null
done
# Base grows the IRA and the Roth at `stock`, the brokerage's stocks at
# `stock_us`; one class for all three keeps the target to two numbers.
sim symbol add Asset_TraditionalIRA_Alice:default --weight stock_us=100% > /dev/null
sim symbol add Asset_Roth_Bill:default --weight stock_us=100% > /dev/null
sim target set main stock_us=80% bond=20% > /dev/null
sim strategy add draw --kind rebalance --chain spending --param portfolio=portfolio:invest \
    --param targets=main --description "Draw from whatever is overweight" > /dev/null
sim chain add invest Asset_Brokerage_Joint --role destination --lot stocks > /dev/null
sim chain step add invest Asset_Brokerage_Joint --lot bonds > /dev/null
sim account lot edit Asset_Checking --overflow-chain invest --overflow-lot '' > /dev/null
sim strategy add place --kind rebalance --chain invest --param portfolio=portfolio:invest \
    --param targets=main --description "Invest in whatever is underweight" > /dev/null
git add -A && git commit -q -m "Video 10a's rebalance"

# 10b's travel scenario, as that video makes it.
sim scenario create travel rebalance > /dev/null
sim scheduled edit living --amount 6500 > /dev/null
sim calendar edit 2036-01 --description "Living without travel, now its own rule" > /dev/null
sim scheduled add 2026-01 Asset_Checking Expense_Living 1000 --name travel --person joint \
    --series cpi > /dev/null
sim scheduled tag add travel category:travel flex:optional > /dev/null
git add -A && git commit -q -m "Video 10b's travel"

# The scenes' commands, in order. Base's and rebalance's runs as they stood
# in November, before the roll: the roll tape reads their projections for
# December 2026.
demo_run base 2026-11-10T090000Z
demo_run rebalance 2026-11-10T090100Z
stage roll
# The roll, answered with Alice and Bill's January statements: the balances,
# BALX's shares, and Enter for each cost basis the plan projected, and for
# 2026's projected year-to-date rows.
printf '%s\n' 22400 58200 498700 296500 '' 163900 '' 119800 '' 1024.8 '' '' \
    | sim scenario roll base 2027-01 --keep plan-2026 > /dev/null
# Rebalance's checking is its own lot (its overflow goes to invest), so it
# rolls on its own, to the same statement.
echo 22400 | sim scenario roll rebalance 2027-01 --own > /dev/null
stage taxes
# Last year's plan, run as it stood: the spending scene reads its 2026.
demo_run plan-2026 2026-11-10T090500Z
sim switch base > /dev/null
# The roll's projected 2026 rows, numbered in the order `sim ytd list` shows.
sim ytd edit projected_2026_4 --amount 96412 --description "Alice's W-2" > /dev/null
sim ytd edit projected_2026_5 --amount 84780 --description "Bill's W-2" > /dev/null
sim ytd edit projected_2026_10 --amount 4380 --description 1099-DIV > /dev/null
sim ytd edit projected_2026_11 --amount 1790 --description 1099-INT > /dev/null
sim ytd add gains_2026 capital_gains capital_gains 15900 --tax-character ltcg --year 2026 \
    --description 1099-B > /dev/null
stage spending
sim scheduled edit living --amount 8000 --base-date 2027-01 > /dev/null
# Travel's living row is its own (video 10b's override), so base's edit
# doesn't reach it.
sim scheduled edit living --amount 7000 --base-date 2027-01 --scenario travel > /dev/null
stage compare
stage done
# The compare scene reruns everything on camera (`sim all`), so nothing after
# the roll is run here: a run made now would leave `sim all` nothing to do.
sim switch base > /dev/null
