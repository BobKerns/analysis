# Prepares what video 9 shows, before any scene records (docs/videos/README.md,
# "Setting up the demo"). Video 9 asks how long Alice and Bill live, whether
# their spending stays inside a line, and how the plan can respond, so this
# builds base with the commands videos 3 to 5 type and video 7's checking
# sweep, then what the scenes make, tagging the data repo before each scene's
# commands (stage/<scene>): a tape resets to its tag, so it can make on camera
# what a later scene already has. Each scenario is run once at the end and its
# run renamed to a fixed time, so every recording reads the same run.


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
sim scheduled add 2027-04 Asset_Checking Expense_Taxes_Federal --kind tax_accumulator \
    --name federal_tax --cadence annual --month-of-year 4 --params-json '{"year": "previous"}'


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
git add -A && git commit -q -m "Video 7's base"

# Run an ensemble and give its batch a fixed time (its folder names).
demo_batch() {
    sim run "$1" > /dev/null
    local batch
    batch="$(ls -t "state/runs/$1/monte-carlo" | head -1)"
    mv "state/runs/$1/monte-carlo/$batch" "state/runs/$1/monte-carlo/$2"
    mv "state/intent/$1/$batch" "state/intent/$1/$2" 2>/dev/null || true
}

# The scenes' commands, in order.
stage table
stage set
sim scenario create lives base > /dev/null
sim person lifespan set alice 88 --vary life_table > /dev/null
sim person lifespan set bill 82 --vary life_table > /dev/null
stage death
stage rail-add
sim guard-rail add four_percent --measure withdrawals --side above --basis initial_portfolio \
    --threshold 4% --start 2032-01 > /dev/null
sim guard-rail add gk_upper --measure withdrawals --side above --basis portfolio \
    --threshold 6% --start 2032-01 > /dev/null
sim guard-rail add gk_lower --measure withdrawals --side below --basis portfolio \
    --threshold 4% --sense positive --start 2032-01 > /dev/null
stage rail-report
stage rail-mc
sim ensemble create lives-mc lives t_inflation_current t_market_current > /dev/null
stage respond
sim ensemble create respond-mc lives-mc > /dev/null
sim scheduled tag add living flex:optional > /dev/null
sim strategy add respond --kind cut_on_breach --rules flex:optional --trigger gk_upper:annual \
    --param cut=10% --param increase=0% > /dev/null
stage done

demo_run base 2026-11-08T090000Z
demo_run lives 2026-11-08T090100Z
demo_batch lives-mc 2026-11-08T091000Z
demo_batch respond-mc 2026-11-08T091100Z
sim switch lives > /dev/null
# Build each batch's series store now, so no scene waits for it.
sim ensemble guard-rails lives-mc > /dev/null
sim ensemble guard-rails respond-mc > /dev/null
