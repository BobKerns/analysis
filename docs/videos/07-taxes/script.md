+++
series = "Planning with the simulator"
episode = "Video 7"
title = "Taxes"
subtitle = "What the plan pays in tax, Roth conversions, Medicare's surcharge, and deductions"
data = "prototype_data"

[youtube]
title = "Taxes"
summary = """
Videos 3 to 6 showed what Alice and Bill's plan does. This one shows what it \
pays in tax: how the simulator figures each year's tax, the report that shows \
it, and net worth after the tax still owed. Then the four years in the middle \
of the plan that pay no tax at all, and how a Roth conversion uses them; \
Medicare's income surcharge, and how to stay under it; and a big medical year, \
with itemized deductions."""
contents = [
  "How the simulator figures tax, once a year",
  "The supplied tax tables, indexed for inflation",
  "The tax report (sim report tax)",
  "Net worth after tax (sim networth)",
  "The empty years, and a Roth conversion (fill_bracket)",
  "Comparing it with the plan (sim compare --section tax)",
  "Medicare's surcharge, IRMAA, and staying under it",
  "A big medical year: itemized deductions (sim deduction)",
  "The notebooks",
  "Asking an AI",
]
+++

The seventh video of #366 (issue #370): taxes. Alice and Bill's base
scenario, from videos 3 to 6, with one change: checking keeps up to
$30,000 and sweeps the rest into the brokerage, so the money a conversion
or an RMD brings in is invested rather than idle.

`setup.sh` beside this script builds base with the commands videos 3 to 6
type, then the what-ifs the scenes make, tagging the data repo before each
scene (`stage/<scene>`) so a tape can make on camera what later scenes
already have. Each scenario is run once, with a fixed time before any
recording, so a run made on camera is the latest. The tapes work in
`~/retirement/data/sim`, as in videos 3 to 6.

## Scene `title`: Title card

```slide
template = "title"
```

Planning with the simulator. Video seven: taxes.

So far, Alice and Bill have seen what their plan does. This time: what it
pays in tax, and why some years pay none at all. Then a Roth conversion to
put those years to use, Medicare's income surcharge, and a big medical
year.

## Scene `contents`: Taxes

```slide
template = "card"
heading = "Taxes"
body = [
  "How tax is figured, and the tables it uses",
  "The year's tax, and net worth after tax",
  "The empty years, and a Roth conversion",
  "Medicare's surcharge",
  "A big medical year",
  "The notebooks, and asking an AI",
]
speaker = "bill"
```

First, how the simulator figures tax, and the tables it uses. Then the
tax report, and net worth after tax. Then four years that pay no tax, and
a Roth conversion that uses them. Then Medicare's income surcharge, and a
big medical year. And last, the notebooks, and asking an AI.

## Scene `how`: How tax is figured

```slide
template = "card"
heading = "How tax is figured"
body = [
  "Once per tax year, from everything the year posted; paid the next April",
  "Ordinary income: wages, IRA withdrawals, interest",
  "Long-term gains, stacked on top, at their own rates",
  "The taxable part of Social Security",
  "Less the larger of two deductions: standard, or itemized",
  "Filing status: Single from the year after a death",
]
speaker = "alice"
```

Tax is figured once a year, from everything the year posted, and paid the
following April, as in real life. Ordinary income is wages, withdrawals
from a traditional IRA, and interest. Long-term gains stack on top, at
their own lower rates. Part of Social Security is taxable, depending on
the rest of the income. Then comes the deduction: the standard one, or an
itemized total, whichever is larger. And the filing status follows the
household: after a death, the survivor files as single.

## Scene `tables`: The tax tables

```slide
template = "card"
heading = "The tax tables"
body = [
  "Supplied: brackets, standard deduction, gains bands, Social Security thresholds",
  "And RMD factors, Medicare's IRMAA tiers, and California's tables",
  "Brackets and deductions grow with inflation each year, as the law says",
  "Yours: filing_status.csv",
  "A scenario can replace a table, to try a change in the law",
]
speaker = "bill"
```

Nobody has to type in tax tables. The simulator supplies them: the
brackets, the standard deduction, the bands for long-term gains, the
Social Security thresholds, the factors for required minimum
distributions, and Medicare's income tiers. The brackets and deductions
grow with inflation each year, as the law has them do. The one table that's
yours is your filing status. And a scenario can replace any table with its
own, to see what a change in the law would do.

## Scene `report`: The year's tax

```tape
Set FontSize 18
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "cd ~/retirement/data/sim && sim switch base > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim report tax --end 2040"
Sleep 400ms
Enter
Wait
Sleep 22s
Type "clear && sim report tax --chart"
Sleep 400ms
Enter
Wait
Sleep 14s
```

Sim report tax shows each year's tax: the adjusted gross income, the
deduction, the taxable income that's left, and the tax. All in today's
dollars. While both work, about twenty five thousand dollars a year.
Then Alice retires, and it falls to about ten thousand. And then, from 2031 to
2034: nothing. Both are retired, Bill's Social Security hasn't started,
and Alice's required distributions don't start until 2037. The little
income they have is less than the standard deduction, or, in 2034, taxed
at the zero rate for long-term gains and dividends. From 2035, tax comes
back.

With dash dash chart, the same years are drawn: tax as bars, and the
effective rate as a line.

## Scene `after-tax`: Net worth, after tax

```tape
Set FontSize 20
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "cd ~/retirement/data/sim && sim switch base > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim networth --end 2034"
Sleep 400ms
Enter
Wait
Sleep 18s
```

Tax also changes what net worth means. The half million dollars in Alice's
traditional IRA isn't all theirs: tax is still owed on it, whenever it
comes out. So sim networth takes it off. Deferred tax is what the IRA and
the brokerage's gains would owe if drawn down over ten years; unpaid tax is
this year's bill, due in April. After tax is what's left, and it's the
default, because it's the fair way to compare plans that put money in
different kinds of accounts. Dash dash pre tax shows the other view.

## Scene `gap`: The empty years

```slide
template = "card"
heading = "The empty years"
body = [
  "2031 to 2034: no tax at all",
  "The 10% and 12% brackets go unused",
  "Later, RMDs and Social Security push income up",
  "Convert some of the IRA to a Roth now: tax at 12%",
  "The Roth grows tax-free, and has no RMDs",
]
speaker = "alice"
```

Those empty years are an opportunity. The deduction and the lowest
brackets go unused, and later, required distributions and Social Security
will push income into higher ones. So move some of the IRA into a Roth
IRA now, while it's cheap. The amount moved is taxed as income this year,
at ten or twelve percent. After that, it grows tax free, comes out tax
free, and has no required distributions.

## Scene `convert`: A Roth conversion

```tape
Set FontSize 18
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/convert && cd ~/retirement/data/sim && sim switch base > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim scenario create convert base"
Sleep 400ms
Enter
Wait
Sleep 3s
Type "sim account add Asset_Roth_Alice asset retirement --owner alice --lot-subkind roth_ira --series stock --opening-balance 0"
Sleep 400ms
Enter
Wait
Sleep 6s
Type "sim scheduled add 2031-01 Asset_TraditionalIRA_Alice Asset_Roth_Alice --kind fill_bracket --name convert --from-lot default --to-lot default --rate 12% --end 2036-12"
Sleep 400ms
Enter
Wait
Sleep 10s
Type "sim chain step add spending Asset_Roth_Alice --lot default"
Sleep 400ms
Enter
Wait
Sleep 6s
Type "clear && sim run convert --commit"
Sleep 400ms
Enter
Wait
Sleep 4s
```

A new scenario, convert, builds on base. It needs somewhere to convert to:
a Roth IRA for Alice, starting empty.

Then the conversion itself: a fill bracket rule. Each December from 2031
through 2036, it moves just enough from the traditional IRA to the Roth to
fill the year's taxable income up to the top of the twelve percent
bracket, whatever else the year brought in. Nobody has to work out the
amount.

And the Roth joins the spending chain, last, so its money can be spent
when the rest runs low; otherwise it would sit there untouched. Then run
it.

## Scene `convert-results`: What it costs, and what it buys

```tape
Set FontSize 16
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/irmaa-demo && cd ~/retirement/data/sim && sim switch base > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim compare base convert --section tax --start 2029 --end 2045"
Sleep 400ms
Enter
Wait
Sleep 24s
Type "clear && sim compare base convert --section net-worth --start 2050"
Sleep 400ms
Enter
Wait
Sleep 12s
```

Sim compare, with dash dash section tax, compares the two year by year.
In the conversion years, convert pays nine to seventeen thousand dollars
of tax that base doesn't. From 2037, when the required distributions
would have started, it pays four to six thousand dollars a year less, and
keeps paying less for the rest of their lives. Over the whole run, about
seventy five thousand dollars less tax, in today's dollars.

And net worth, after tax: convert pulls ahead and keeps pulling away. By
2050 it's eighty five thousand dollars ahead of base, and by 2060 a
hundred and sixty thousand. Part of that is the lower tax,
and part is that the Roth carries no tax still owed.

## Scene `irmaa`: Medicare's surcharge

```slide
template = "card"
heading = "Medicare's surcharge: IRMAA"
body = [
  "Higher income means higher Medicare premiums",
  "Set by income two years earlier",
  "Cliffs, not brackets: one dollar over costs the whole step",
  "Per person on Medicare",
  "Tiers supplied, growing with inflation",
]
speaker = "bill"
```

Why stop at twelve percent? One reason is Medicare. Once you're on it,
higher income means higher premiums: the income related monthly
adjustment amount, or IRMAA. It's set by your income two years earlier,
and it works in cliffs, not brackets: one dollar over a tier, and you pay
that tier's whole surcharge, for each person on Medicare. The simulator
supplies the tiers, growing with inflation.

## Scene `irmaa-demo`: Converting more

```tape
Set FontSize 16
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/irmaa-demo && cd ~/retirement/data/sim && sim switch convert > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim scenario create convert-22 convert"
Sleep 400ms
Enter
Wait
Sleep 2s
Type "sim scheduled edit convert --rate 22%"
Sleep 400ms
Enter
Wait
Sleep 4s
Type "sim scheduled add 2028-01 Asset_Checking Expense_Healthcare --kind irmaa --name irmaa_alice --person alice"
Sleep 400ms
Enter
Wait
Sleep 3s
Type "sim scheduled add 2029-08 Asset_Checking Expense_Healthcare --kind irmaa --name irmaa_bill --person bill"
Sleep 400ms
Enter
Wait
Sleep 3s
Hide
Type "git -C ~/retirement/data reset -q --hard stage/medical && sim switch convert-22 > /dev/null 2>&1; clear"
Enter
Sleep 1s
Show
Type "sim report tax --start 2030 --end 2036"
Sleep 400ms
Enter
Wait
Sleep 16s
Type `clear && sim scheduled edit convert --params-json '{"irmaa_tier": 0}' && sim run convert-22 --commit > /dev/null && sim report tax --start 2030 --end 2036`
Sleep 400ms
Enter
Wait
Sleep 16s
```

Convert 22 builds on convert, and fills the twenty two percent bracket
instead. Two irmaa rules charge the surcharge, paid from checking, each
from the month its person is on Medicare.

The report now shows the income Medicare looks at, and the IRMAA tier it
sets, for the premiums two years later. Converting this much crosses the
first tier in 2031 and 2032, and the second in 2033.

A fill bracket rule can stay under a tier. Irmaa tier zero means: stop
short of the first tier, even if the bracket has more room. Now every
year stays at tier zero, and the conversion is spread over five years
instead of four.

Is converting more better? Not here. Filling the twenty two percent
bracket all the way ends about sixty thousand dollars behind the twelve
percent plan. Staying under the first tier cuts that to about twenty
thousand, but it's still behind: the extra tax now buys less than it
saves later. A conversion pays when it's taxed at a lower rate than the
money would be later.

## Scene `medical`: A big medical year

```tape
Set FontSize 18
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/medical && cd ~/retirement/data/sim && sim switch base > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim scenario create medical base"
Sleep 400ms
Enter
Wait
Sleep 2s
Type "sim calendar add transfer 2028-05 Asset_Checking Expense_Healthcare 50000 --description surgery"
Sleep 400ms
Enter
Wait
Sleep 5s
Type "sim deduction add 2028 --item 'medical over 7.5% of AGI=39500' --item 'state and local taxes=10000' --item charity=3000"
Sleep 400ms
Enter
Wait
Sleep 10s
Type "clear && sim run medical --commit > /dev/null && sim report tax --start 2027 --end 2030"
Sleep 400ms
Enter
Wait
Sleep 16s
```

Last, a different kind of year. Suppose Alice has surgery in 2028, and
fifty thousand dollars of it isn't covered. Medical is a new scenario
with the payment, as a one time transfer.

Medical expenses are deductible, if you itemize, above seven and a half
percent of income. Sim deduction add gives 2028 its itemized deductions,
listing what's deductible: the medical expenses above that floor, state
and local taxes, and charity. The simulator adds them up, and uses them
only if they beat the standard deduction, and only in 2028.

The report shows it: 2028's deduction rises from thirty thousand to forty
eight thousand, and the tax falls by over four thousand dollars. The
years around it barely change.

## Scene `nb-tax`: The notebooks

```notebook
Set DataDir "home/retirement/data/sim"
Hide
Open "net_worth.ipynb"
RunAll
Scroll "Tax"
Show
Sleep 9s
Hide
Open "scenario_compare.ipynb"
RunAll
Scroll "Tax"
Show
Sleep 10s
ScrollBy 500
Sleep 8s
```

The notebooks show the same. Net worth's tax section stacks each year's
tax, with the effective rate on the right. The scenario comparison
notebook's tax section draws every run's tax, and below it, each one's
difference from base: convert and convert 22 pay more early, and less
for decades after. Tables under the chart give the totals, and each
run's final net worth after tax.

## Scene `more`: Also in the guide

```slide
template = "card"
heading = "Also in the guide"
body = [
  "State tax: California's tables are supplied",
  "Year-to-date: what the first year already paid (sim ytd)",
  "Harvesting gains and giving shares each December",
  "Medicare's surcharge for the years before the plan starts",
]
speaker = "alice"
```

There's more in the user guide. State tax, with California's tables
supplied. Year to date rows, for what the first year already earned and
paid before the plan starts. Harvesting gains and giving shares each
December, which comes up again with investments. And the incomes Medicare
needs from the years before the plan starts.

## Scene `ai`: Asking an AI about tax

```slide
template = "card"
heading = "Asking an AI about tax"
body = [
  "\"Which years have room in the 12% bracket?\"",
  "\"How much would converting $30,000 in 2032 cost in tax?\"",
  "\"Why did 2028's tax fall in medical?\"",
]
speaker = "bill"
```

An AI assistant can read all of this too. Ask which years have room in
the twelve percent bracket, or what a particular conversion would cost.
Or ask why a year's tax changed; it can read both runs and find out.

## Scene `end`: Next time

```slide
template = "card"
heading = "Next: investments"
body = "Lots, asset classes, and what the portfolio holds."
speaker = "alice"
```

Next time: investments. Lots, asset classes, and what the portfolio
holds.
