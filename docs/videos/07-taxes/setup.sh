# Prepares what video 7 shows, before any scene records (docs/videos/README.md,
# "Setting up the demo"). Video 7 is about tax, on Alice and Bill's base
# scenario, so this builds base with the commands videos 3 to 6 typed (one
# change: checking sweeps its surplus into the brokerage), then the what-ifs
# the scenes make: a Roth conversion to the 12% bracket, one to 22% with
# Medicare's surcharge, and a big medical year. It tags the data repo before
# each scene's commands (stage/<scene>): a tape resets to its tag, so it can
# make on camera what a later scene already has. Each scenario is run once,
# with a fixed time, so every recording reads the same runs.

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
# Video 7's one change to base: checking keeps up to $30k and sweeps the
# rest into the brokerage stocks each month, so RMDs and surplus income are
# invested rather than idling in a 0% account (which would make anything
# that adds income, like a Roth conversion, look worse than it is).
sim account lot edit Asset_Checking --maximum-balance 30000 --overflow-chain Asset_Brokerage_Joint \
    --overflow-lot stocks > /dev/null
git add -A && git commit -q -m "Base, with checking's sweep"
demo_run base 2026-11-01T090000Z

# The scenes' commands, in order.
stage convert
sim scenario create convert base > /dev/null
sim account add Asset_Roth_Alice asset retirement --owner alice --lot-subkind roth_ira \
    --series stock --opening-balance 0 > /dev/null
sim scheduled add 2031-01 Asset_TraditionalIRA_Alice Asset_Roth_Alice --kind fill_bracket \
    --name convert --from-lot default --to-lot default --rate 12% --end 2036-12 > /dev/null
sim chain step add spending Asset_Roth_Alice --lot default > /dev/null
stage convert-results
demo_run convert 2026-11-01T090100Z
stage irmaa-demo
sim scenario create convert-22 convert > /dev/null
sim scheduled edit convert --rate 22% > /dev/null
sim scheduled add 2028-01 Asset_Checking Expense_Healthcare --kind irmaa --name irmaa_alice \
    --person alice > /dev/null
sim scheduled add 2029-08 Asset_Checking Expense_Healthcare --kind irmaa --name irmaa_bill \
    --person bill > /dev/null
git add -A && git commit -q -m convert-22
demo_run convert-22 2026-11-01T090200Z
stage medical
sim scenario create medical base > /dev/null
sim deduction add 2028 --item "medical over 7.5% of AGI=39500" --item "state and local taxes=10000" \
    --item charity=3000 > /dev/null
sim calendar add transfer 2028-05 Asset_Checking Expense_Healthcare 50000 --description surgery > /dev/null
git add -A && git commit -q -m medical
demo_run medical 2026-11-01T090300Z
stage end
# The tapes' prompt shows the current scenario; start them on base.
sim switch base > /dev/null
