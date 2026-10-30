# Prepares what video 1 shows, before any scene records (docs/videos/README.md,
# "Setting up the demo"). It runs inside a fresh, committed copy of the
# prototype data, with `sim` on PATH; whatever it leaves uncommitted is
# committed after it. Every scene starts from the result, so a scene can show
# a run without recording one first.

sim switch retire_alice

# The prototype's savings grow at fixed rates and it pays no income tax. Give
# Alice and Bill a stock and bond mix, and a federal income tax settled each
# April, so the ensemble has markets to vary and "after tax" means something.
sim account lot edit Asset_Brokerage_Joint default --series stock_us
sim account lot edit Asset_TraditionalIRA_Alice default --series bond
sim account lot edit Asset_Roth_Alice default --series stock
sim account lot edit Asset_Roth_Bill default --series stock
sim scheduled add 2027-04-30 Asset_Checking Expense_Taxes_Federal --kind tax_accumulator \
    --name federal_tax --cadence annual --month-of-year 4 --params-json '{"year": "previous"}' \
    --description "federal income tax for the year before"
git add -A && git commit -q -m "Invest the savings; add federal income tax"
sim run

# The ensemble at the end: eighty futures of markets and inflation, with a
# 2008-style crash placed some time from mid-2028 to 2034, around Alice's and
# Bill's retirements, so a bad decade lands where it hurts most.
sim ensemble create alice_crisis retire_alice t_market_ensemble t_inflation_ensemble t_financial_crisis \
    --size 80 --description "retire_alice through eighty futures, each with a crash near retirement"
sim calendar edit financial_crisis_start --date 2031-06-30 --vary-low 2028-06-30 --vary-high 2034-12-31
git add -A && git commit -q -m "Ensemble with a crash near retirement"
sim run alice_crisis
# The first summary builds a cache and says so; get that out of the way.
sim ensemble summary alice_crisis > /dev/null

sim switch retire_alice
