# Prepares what video 6 shows, before any scene records (docs/videos/README.md,
# "Setting up the demo"). Video 6 runs Alice and Bill's plan across many
# futures, so this builds base and video 5's work-longer what-if with the
# same commands those videos type, then the ensembles the scenes make,
# tagging the data repo before each scene's commands (stage/<scene>): a tape
# resets to its tag, so it can make on camera what a later scene already has.
# Each ensemble is run once at the end and its batch renamed to a fixed time,
# so every recording reads the same batch.

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

sim scenario create work-longer base > /dev/null
sim scheduled edit salary_bill --end 2032-12 > /dev/null
sim switch base > /dev/null
git add -A && git commit -q -m "Video 5's what-if"
demo_run base 2026-10-31T090000Z
demo_run work-longer 2026-10-31T090100Z

# The scenes' commands, in order.
stage create
sim ensemble create markets base t_inflation_current t_market_current > /dev/null
stage varying
sim calendar edit 2031-06 --name car --vary-target amount --vary-distribution triangular \
    --vary-low 30000 --vary-mode 40000 --vary-high 55000 > /dev/null
stage run
sim ensemble create work-longer-mc markets work-longer > /dev/null
stage compared

# Run an ensemble and give its batch a fixed time (its folder names).
demo_batch() {
    sim run "$1" > /dev/null
    local batch
    batch="$(ls -t "state/runs/$1" | head -1)"
    mv "state/runs/$1/$batch" "state/runs/$1/$2"
    mv "state/intent/$1/$batch" "state/intent/$1/$2"
}

demo_batch markets 2026-10-31T091000Z
demo_batch work-longer-mc 2026-10-31T091100Z
# The tapes' prompt shows the current scenario; start them on markets.
sim switch markets > /dev/null
