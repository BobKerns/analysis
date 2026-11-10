# Prepares what video 10b shows, before any scene records (docs/videos/README.md,
# "Setting up the demo"). Video 10b lets a spending strategy decide how much of
# Alice and Bill's travel to spend each year, so this builds video 10a's
# rebalance scenario with the commands videos 3 to 10a type, then what the
# scenes make, tagging the data repo before each scene's commands
# (stage/<scene>): a tape resets to its tag, so it can make on camera what a
# later scene already has. Each scenario and ensemble is run once at the end and
# its run renamed to a fixed time, so every recording reads the same run.


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
git add -A && git commit -q -m "Video 7's base"

# Run an ensemble and give its batch a fixed time (its folder names).
demo_batch() {
    sim run "$1" > /dev/null
    local batch
    batch="$(ls -t "state/runs/$1/monte-carlo" | head -1)"
    mv "state/runs/$1/monte-carlo/$batch" "state/runs/$1/monte-carlo/$2"
    mv "state/intent/$1/$batch" "state/intent/$1/$2" 2>/dev/null || true
}

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

# The scenes' commands, in order.
stage travel
sim scenario create travel rebalance > /dev/null
sim scheduled edit living --amount 6500 > /dev/null
sim scheduled add 2026-01 Asset_Checking Expense_Living 1000 --name travel --person joint \
    --series cpi > /dev/null
sim scheduled tag add living category:living > /dev/null
sim scheduled tag add healthcare category:health > /dev/null
sim scheduled tag add travel category:travel flex:optional > /dev/null
stage as-written
sim ensemble create travel-mc travel t_inflation_current t_market_current > /dev/null
stage threshold
sim scenario create threshold travel > /dev/null
sim strategy add spend --kind threshold --rules flex:optional --param portfolio=portfolio:invest \
    --param minimum=800000 --description "Travel only while the portfolio is above \$800,000" > /dev/null
sim ensemble create threshold-mc threshold t_inflation_current t_market_current > /dev/null
stage guardrail
sim scenario create guardrail travel > /dev/null
sim strategy add spend --kind guardrail --rules flex:optional --param portfolio=portfolio:invest \
    --description "Guyton-Klinger guardrails on travel" > /dev/null
sim ensemble create guardrail-mc guardrail t_inflation_current t_market_current > /dev/null
stage ceiling
sim scenario create ceiling-floor travel > /dev/null
sim strategy add spend --kind ceiling_floor --rules flex:optional --param portfolio=portfolio:invest \
    --description "Travel moves with the portfolio, within limits" > /dev/null
sim ensemble create ceiling-floor-mc ceiling-floor t_inflation_current t_market_current > /dev/null
stage vpw
sim scenario create vpw travel > /dev/null
sim strategy add spend --kind amortize --rules flex:optional --param portfolio=portfolio:invest \
    --description "VPW: spend the portfolio down by 100" > /dev/null
sim ensemble create vpw-mc vpw t_inflation_current t_market_current > /dev/null
stage compare
stage done

demo_run base 2026-11-11T090000Z
demo_run rebalance 2026-11-11T090100Z
demo_run travel 2026-11-11T090200Z
demo_run threshold 2026-11-11T090300Z
demo_run guardrail 2026-11-11T090400Z
demo_run ceiling-floor 2026-11-11T090500Z
demo_run vpw 2026-11-11T090600Z
demo_batch travel-mc 2026-11-11T091000Z
demo_batch threshold-mc 2026-11-11T091100Z
demo_batch guardrail-mc 2026-11-11T091200Z
demo_batch ceiling-floor-mc 2026-11-11T091300Z
demo_batch vpw-mc 2026-11-11T091400Z
# Build each batch's series store now, so no scene waits for it.
for batch in travel-mc threshold-mc guardrail-mc ceiling-floor-mc vpw-mc; do
    sim ensemble spending "$batch" > /dev/null
done
sim switch travel > /dev/null
