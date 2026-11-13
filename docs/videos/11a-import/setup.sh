# Prepares what video 11a shows, before any scene records (docs/videos/README.md,
# "Setting up the demo"). Video 11a brings Alice and Bill's balances and
# spending in from Quicken, so this builds video 3's base, writes the two
# reports they exported into ~/Downloads, and then does what the scenes do,
# tagging the data repo before each scene's commands (stage/<scene>): a tape
# resets to its tag, so it can make on camera what a later scene already has.

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

# Video 3's base: the household, its accounts, and the plan as typed in.
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
sim scheduled add 2026-01 Asset_TraditionalIRA_Alice Asset_Checking --kind rmd --name rmd_alice \
    --from-lot default --person alice
sim tax add federal --source-chain Asset_Checking --destination Expense_Taxes_Federal --start 2027-04

sim chain add spending Asset_Savings --role source
sim chain step add spending Asset_Brokerage_Joint --lot bonds
sim chain step add spending Asset_Brokerage_Joint --lot stocks
sim chain step add spending Asset_TraditionalIRA_Alice --lot default
sim account lot edit Asset_Checking --minimum-balance 5000 --refill-chain spending

# What Quicken exported: the accounts under Quicken's own names, as of the
# plan's first day, and 2025's spending.
mkdir -p ~/Downloads
cat > ~/Downloads/net-worth.csv <<'CSV'
"",,"2026-01-01"
"Assets",,
,"Bank Accounts",
,"  - Joint Checking","14,820.55"
,"  - Online Savings","61,240.10"
,"  - Total Bank Accounts","76,060.65"
,"Investments",
,"  - Brokerage","402,300.00"
,"  - Alice Rollover IRA","503,610.42"
,"  - Bill Roth IRA","121,075.00"
,"  - Total Investments","1,026,985.42"
,"Property",
,"  - House","650,000.00"
,"  - Total Property","650,000.00"
"Liabilities",,
,"Credit Cards",
,"  - Rewards Card","-1,240.33"
,"  - Total Credit Cards","-1,240.33"
Net Worth,,"1,751,805.74"
CSV
python - > ~/Downloads/transactions-2025.csv <<'PY'
# Twelve months of the same handful of payees, a little different each month.
monthly = [  # day, payee, category, dollars, change a month
    (3, "Corner Market", "Groceries", 780, 9),
    (9, "Luigi's", "Restaurants", 240, -4),
    (12, "City Power", "Utilities", 310, 6),
    (15, "Fuel Stop", "Gas & Fuel", 160, 3),
    (18, "Main St Pharmacy", "Pharmacy", 95, 2),
    (21, "Dr. Example", "Doctor", 180, 0),
    (24, "Hardware Barn", "Home Repair", 420, -11),
    (27, "Everything Else", "Shopping", 4200, 17),
    (1, "Shield Health Plan", "Health Insurance", 520, 0),
]
yearly = [(4, 20, "Fly Away Air", "Travel", 3200), (9, 5, "Seaside Inn", "Travel", 2800),
          (6, 10, "Sure Insurance", "Auto Insurance", 1450)]
print('"Account",,Date,Entered,Posted,Action,Payee,Category,Tags,Transfer,Memo/Notes,Amount')
print('"Joint Checking",,,,,,,,,,,')
total = 0.0
rows = [(month, day, payee, category, dollars + change * month)
        for month in range(1, 13) for day, payee, category, dollars, change in monthly]
for month, day, payee, category, dollars in sorted(rows + yearly):
    date = f"2025-{month:02d}-{day:02d}"
    total += dollars
    print(f',,"{date}","{date}","{date}","","{payee}","{category}","","","","-{dollars:,.2f}"')
print(f'"Total Joint Checking",,,,,,,,,,,"-{total:,.2f}"')
PY

# The one mapping file written by hand: Quicken's categories, gathered into
# the few the plan keeps.
mkdir -p sim/imports/quicken
cat > sim/imports/quicken/categories.csv <<'CSV'
outside_category,category
Groceries,Food
Restaurants,Food
Utilities,Home
Home Repair,Home
Gas & Fuel,Car
Auto Insurance,Car
Pharmacy,Health
Doctor,Health
Health Insurance,Health
Travel,Travel
Shopping,Everyday
CSV

# The scenes' commands, in order. Setup has no terminal to be asked at, so it
# writes the answers Alice gives on camera as the files the import saves.
stage sources
stage holdings
cat > sim/imports/quicken/accounts.csv <<'CSV'
outside_account,asset,action,account,lot,note
Joint Checking,,map,Asset_Checking,,
Online Savings,,map,Asset_Savings,,
Brokerage,,skip,,,
Alice Rollover IRA,,map,Asset_TraditionalIRA_Alice,,
Bill Roth IRA,,map,Asset_Roth_Bill,,
House,,skip,,,
Rewards Card,,skip,,,
CSV
sim import holdings ~/Downloads/net-worth.csv
stage again
stage estimates
sim scheduled delete living --yes
sim scheduled delete healthcare --yes
stage transactions
cat > sim/imports/expense_accounts.csv <<'CSV'
category,account
Car,Expense_Living
Everyday,Expense_Living
Food,Expense_Living
Health,Expense_Healthcare
Home,Expense_Living
Travel,Expense_Living
CSV
sim import transactions ~/Downloads/transactions-2025.csv --from-account Asset_Checking
stage done
