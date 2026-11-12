+++
series = "Planning with the simulator"
episode = "Video 13"
title = "Tax plugins"
subtitle = "Writing your own tax calculator"
data = "prototype_data"

[youtube]
title = "Tax plugins"
summary = """
What if Alice and Bill retired in a DC rowhouse? DC's property tax credit \
pays back the property tax above a share of their income, even when they \
owe no tax, and no table row can say that. So they write a tax plugin: forty \
lines of Python that build on the built-in state tax. They check it, use it, \
and see what it's worth, and where its cliff is."""
contents = [
  "A rule no table can say",
  "A what-if in DC (sim tax state DC --here, a tagged property tax)",
  "DC's property tax credit (Schedule H)",
  "Where a tax plugin lives (sim/taxes/)",
  "Building on the built-in state tax: the class, its parameters, its year",
  "What a tax calculator may read",
  "Checking it (sim tax kinds, sim tax edit --kind, sim tax check)",
  "Using it (sim report tax --return)",
  "What it's worth (sim compare --section tax)",
  "The trade-off: the bridge years, age, and a cliff",
  "The tax chart in the notebook",
  "Asking an AI",
]
+++

Video 13 (issue #568): tax plugins, the companion to video 12's strategy
plugins. Alice and Bill wonder what retiring in a DC rowhouse would mean,
and write `property_credit`, a state tax calculator that builds on the
built-in `state_tables` and adds DC's property tax credit (Schedule H): a
circuit breaker, which no row of `state_adjustments` can say.

`setup.sh` beside this script builds the base scenario videos 3 to 7 make,
then what the scenes make, tagging the data repo before each scene
(`stage/<scene>`) so a tape can make on camera what later scenes already
have. The plugin is `property_credit.py` beside this script, the same file
as the guide's example (`docs/guides/tax-calculators.md`, and a test keeps
the two the same): setup copies it into the data repo, and the code slides
read their lines from it (`code_file`, `code_lines`). After editing it,
keep each slide's `code_lines` and `marks` on the lines they mean. Each
scenario is run once at the end and its run renamed to a fixed time. The
tapes work in `~/retirement/data/sim`, as in videos 3 to 12.

The comparison stays with tax and pre-tax figures: the after-tax net worth
prices each year's share of the IRA on top of that year's income without
its own IRA draws, which qualifies for the credit, so it charges the
credit's loss against the IRA in every drawdown year (an open question, not
for this video).

> Numbers in the narration were checked on 2026-11-11 against `setup.sh`.

<!-- cspell:ignore rnet -->

## Scene `title`: Title card

```slide
template = "title"
```

Planning with the simulator. Video thirteen: tax plugins.

Video twelve wrote a strategy plugin. This time, a tax rule the simulator
doesn't know.

## Scene `contents`: Writing your own tax calculator

```slide
template = "card"
heading = "Writing your own tax calculator"
body = [
  "A rule no table can say",
  "A plugin that builds on the state tax",
  "Checking it, and using it",
  "What it's worth, and its cliff",
]
speaker = "bill"
```

First, a tax rule no table can say. Then a tax plugin, built on the
built-in state tax. Then checking it, using it, and what it's worth to
Alice and Bill, including the cliff it hides.

## Scene `recap`: Strategies decide, calculators compute

```slide
template = "card"
heading = "Two kinds of plugin"
body = [
  "A strategy plugin decides what the household does (video 12)",
  "A tax plugin computes what a return says",
  "The same rules: a file in the data repo, checked, copied into each run",
  "Each can build on a built-in",
]
speaker = "alice"
```

Video twelve's strategy plugin decided what the household does: which
trips to take. A tax plugin doesn't decide anything. It computes what a
return says, from the year's income and spending. The rest is the same: a
file in the data repo, checked the same way, copied into each run, and
built on a built-in.

## Scene `table-first`: Check the table first

```slide
template = "card"
heading = "Most state rules are a row"
body = [
  "state_adjustments: subtractions, deductions and credits",
  "A renter's credit, a 529 subtraction, a pension exclusion: one row each",
  "Look there first",
  "A credit that's a share of income, with limits by age, refunded: not a row",
]
speaker = "bill"
```

Before writing any code, look at the state adjustments table. Most state
rules are one row of it: a renter's credit, a subtraction for 529 savings,
a pension exclusion. But some rules aren't a row. A credit for the tax
above a share of your income, with limits that depend on your age, paid
even when you owe nothing, is one of those. That's DC's property tax
credit.

## Scene `dc`: A what-if in DC

```tape
Set FontSize 14
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/dc && cd ~/retirement/data/sim && sim switch base > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim scenario create dc base > /dev/null"
Sleep 400ms
Enter
Wait
Sleep 2s
Type "clear && sim tax state DC --here --yes"
Sleep 400ms
Enter
Wait
Sleep 6s
Type "clear && sim account add Expense_Property_Tax expense taxes_property --owner joint"
Sleep 400ms
Enter
Wait
Sleep 3s
Type "clear && sim scheduled add 2026-03 Asset_Checking Expense_Property_Tax 2550 --name property_tax_march --person joint --series cpi --cadence annual"
Sleep 400ms
Enter
Wait
Sleep 3s
Type "clear && sim scheduled add 2026-09 Asset_Checking Expense_Property_Tax 2550 --name property_tax_september --person joint --series cpi --cadence annual"
Sleep 400ms
Enter
Wait
Sleep 3s
Type `clear && for half in property_tax_march property_tax_september; do sim calendar add set-amount 2027-09 "$half" 1275 --description "DC's senior relief halves the bill"; done`
Sleep 400ms
Enter
Wait
Sleep 6s
Type "clear && sim account tag add Expense_Property_Tax tax:property"
Sleep 400ms
Enter
Wait
Sleep 4s
```

What if Alice and Bill retired in a DC rowhouse? A new scenario, dc, built
on base, says they live in DC, here, in this scenario only, so base and
the others keep their own state. DC's income tax comes with the
simulator. Then the property tax: a seven hundred thousand dollar home,
less DC's homestead deduction, at eighty five cents per hundred dollars,
is fifty one hundred dollars a year, paid in two halves, in March and
September, raised with inflation. From the second half of 2027, when Alice
is sixty five, DC's senior relief halves it. Last, a tag, tax property,
on the property tax account. That's how a calculator finds what was paid.

## Scene `schedule-h`: DC's property tax credit

```slide
template = "card"
heading = "DC's property tax credit (Schedule H, 2025)"
body = [
  "The property tax above 3% to 5% of federal AGI (3% at 70 or older)",
  "Up to $1,425",
  "Only below $66,000 of AGI, or $90,000 at 70 or older",
  "Refundable: paid even when no tax is due",
]
speaker = "alice"
```

DC's credit, Schedule H, works like this. It pays back the property tax
above a share of federal adjusted gross income: three to five percent,
rising with income, or three percent at seventy or older. It's at most
fourteen hundred and twenty five dollars, for 2025. Only households below
sixty six thousand dollars of income get it, or ninety thousand at
seventy or older. And it's refundable: DC pays it even when no tax is due.
A renter counts a fifth of the rent instead.

## Scene `where`: Where a tax plugin lives

```slide
template = "window"
look = "files"
heading = "A tax plugin is a file in the data repo"
title = "sim"
address = "~/retirement/data/sim"
lines = ["taxes", "scenarios", "aux", "allocations.csv", "persons.csv"]
note = "taxes/property_credit.py, for every scenario: not a scenario's own taxes/ folder"
speaker = "bill"
```

A tax plugin is a Python file in the data repo's taxes folder, beside the
strategies folder from video twelve, with the same rules: every scenario
can use it, it's versioned with the data, and each run keeps a copy of
the code that computed it. Alice and Bill's is property credit dot p y. One
thing to keep apart: a scenario's own taxes folder is different. It holds
the scenario's federal and state dot c s v files, which say which
calculator each jurisdiction uses.

## Scene `class`: The class

```slide
template = "window"
look = "code"
heading = "Building on the state tax"
title = "taxes/property_credit.py"
status = "Python"
code_file = "property_credit.py"
code_lines = [56, 64]
marks = [56, 57, 61, 62, 63]
speaker = "alice"
```

The calculator builds on state tables, the built-in state tax: everything
it does, DC's brackets and deduction, plus the credit. The register line
makes it a calculator kind, and its docstring is what the list of kinds
shows. Its kind is property credit, it's a state tax, and its year class,
further up, holds the credit.

## Scene `params`: Its parameters

```slide
template = "window"
look = "code"
heading = "Its parameters, with their units"
title = "taxes/property_credit.py"
status = "Python"
code_file = "property_credit.py"
code_lines = [65, 74]
marks = [66, 67, 69, 71, 73]
speaker = "bill"
```

Its parameters are state tables', plus four, each with a unit, so a
mistyped one is caught before anything runs: the tag on the property tax
paid, a tag on rent instead for a renter, the largest credit, in dollars,
and the inflation series its 2025 figures grow by.

## Scene `setup`: Once a year

```slide
template = "window"
look = "code"
heading = "Once a year: what doesn't change"
title = "taxes/property_credit.py"
status = "Python"
code_file = "property_credit.py"
code_lines = [21, 35]
marks = [24, 26, 28, 29, 31, 32, 34, 35]
speaker = "alice"
```

The year class is one tax year of the state's tax. Its setup runs once
for the year, before any return. It picks the worksheet's rows, from the
top of the file: the rows for seventy or older if either of them is,
counting only a spouse still living. It grows each row's limit, and the
cap, from 2025 with inflation. And it adds up what was spent with the
property tax tag, or a fifth of the rent with a rent tag. None of that
changes with a what-if's extra income, so it's worked out once.

## Scene `year`: Each return

```slide
template = "window"
look = "code"
heading = "Each return: the credit"
title = "taxes/property_credit.py"
status = "Python"
code_file = "property_credit.py"
code_lines = [37, 44]
marks = [37, 39, 40, 43, 44]
speaker = "bill"
```

Then, for each return, one step added to the state's: refundable credits,
which come off last and may take the tax below zero. It reads federal AGI
from the federal return the state starts from, and finds the first row
it fits under. Past the last row, there's no credit. The credit is what
was paid less that share of AGI, up to the cap. A strategy may ask for a
return thousands of times a year, so this part is kept small.

## Scene `rules`: What a calculator may read

```slide
template = "card"
heading = "What a tax calculator may read"
body = [
  "The year's income, by character, and who received it",
  "What was spent or received with a tag",
  "The return it builds on: the state reads the federal one",
  "Quick and pure: same year, same return, nothing kept between years",
]
speaker = "alice"
```

A calculator sees one tax year: its income by character, like wages,
dividends and gains, and who received it; what was spent or received with
a tag; and the return it builds on, so a state reads the federal return.
It must be quick, since a strategy can ask for a return many times a
month, and pure: the same year always gives the same return, with nothing
kept from one year to the next. Anything carried over, like a loss, comes
from last year's return.

## Scene `check`: Checking it

```tape
Set FontSize 14
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/check && cd ~/retirement/data/sim && sim switch dc > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim tax kinds property_credit"
Sleep 400ms
Enter
Wait
Sleep 10s
Type "clear && sim scenario create dc-credit dc > /dev/null"
Sleep 400ms
Enter
Wait
Sleep 2s
Type `clear && sim tax edit state --kind property_credit --params-json '{"rnet": "tax:rent"}'`
Sleep 400ms
Enter
Wait
Sleep 6s
Type "clear && sim tax edit state --kind property_credit"
Sleep 400ms
Enter
Wait
Sleep 4s
Type "clear && sim tax check 2> /dev/null"
Sleep 400ms
Enter
Wait
Sleep 6s
```

Sim tax kinds lists property credit, with its docstring, state tables'
parameters, and then its own four, with their units and defaults. In a
new scenario, dc credit, the state tax uses it. A mistyped parameter is
refused at once, with the ones it takes, and nothing is written. Without
it, the defaults fit: the tag is tax property, and they own their home.
Then sim tax check computes every return twice, each in a fresh Python,
and compares them: nineteen hundred lines over thirty five years, the
same in both runs.

## Scene `use`: Using it

```tape
Set FontSize 14
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/use && cd ~/retirement/data/sim && sim switch dc-credit > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim run dc-credit --commit > /dev/null && sim report tax --return federal --start 2030 --end 2036 | grep -E ' (line|agi) '"
Sleep 400ms
Enter
Wait
Sleep 6s
Type "sim report tax --return state --start 2030 --end 2036 | grep -E ' (line|property_credit|tax|tax_overpaid) '"
Sleep 400ms
Enter
Wait
Sleep 16s
```

Sim run commits the change and runs the scenario. Sim report tax, return
federal, shows their federal AGI, in today's dollars: a hundred and
twenty three thousand in 2030, the last year of salary, then fifteen
thousand and eleven thousand, before more Social Security and the IRA
fill it back up. The state return shows the credit: the whole fourteen hundred
dollars in 2031 and 2032, with no tax due, so the tax is below zero and DC
pays it back. In 2033 and 2034 it's a share of a higher income: nine
hundred and eighty, then seven hundred and fifty. In 2035, their AGI is
over the limit, and there's no credit. In 2036 it's under again, but
three percent of it nearly covers the bill, leaving thirty three dollars.

## Scene `worth`: What it's worth

```tape
Set FontSize 14
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/worth && cd ~/retirement/data/sim && sim switch dc-credit > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim compare dc dc-credit --section tax 2> /dev/null | grep -vE '^  20(3[6-9]|[45][0-9]|60) '"
Sleep 400ms
Enter
Wait
Sleep 16s
```

Sim compare, section tax, sets dc credit beside dc, which taxes the same
household without the credit. The years before 2035 are where it pays:
about forty six hundred dollars, most of it as refunds. Over the whole
run, they owe about thirty nine hundred dollars less, since the refunds,
saved, earn a little more taxable income later.

## Scene `tradeoff`: The trade-off

```slide
template = "card"
heading = "What the credit says about their plan"
body = [
  "It pays in the bridge years, between salary and Social Security",
  "Age matters: at 69, 5% of their 2034 AGI would leave no credit",
  "It's a cliff: a dollar over the limit costs all of it",
  "A Roth conversion in those years has to price that too",
]
speaker = "alice"
```

The credit pays in the bridge years, after their salaries end and before
Social Security and the IRA fill their income back up. Age matters: in
2034, under seventy, five percent of their income would be more than the
bill, and there'd be no credit. Alice turned seventy in 2032, so it's
three percent. And it's a cliff: one dollar over the limit costs the whole
credit. The bridge years are also when a Roth conversion is cheapest, and
a conversion that crosses the limit costs the credit as well as its tax.
The credit is on the state's return, so a what-if that adds income sees
it go.

## Scene `nb`: The tax chart

```notebook
Set DataDir "home/retirement/data/sim"
Hide
Open "net_worth.ipynb"
RunAll
Scroll "Tax"
Show
Sleep 3s
ScrollBy 450
Sleep 8s
```

The net worth notebook's tax chart draws a plugin's tax like the
built-in's: federal and state tax stacked by year, with the effective rate
on the right. In the bridge years, the state's bar dips below zero: DC
paying the credit back.

## Scene `ai`: Asking an AI

```slide
template = "card"
heading = "Asking an AI about a tax plugin"
body = [
  "\"Write a tax plugin for my state's property tax credit\"",
  "\"Is this a row of state_adjustments, or does it need a plugin?\"",
  "\"Why did the credit stop in 2035?\"",
]
speaker = "bill"
```

An AI assistant is good at this too. It can say whether a rule is a row
of state adjustments or needs a plugin, write a first version from the
state's form, and read a return to explain why a credit stopped in a
particular year. Check what it writes against the form: tax rules are
precise, and the form has the last word.

## Scene `end`: The series so far

```slide
template = "card"
heading = "That's the series so far"
body = "Strategies decide; tax calculators compute. Both can be your own."
speaker = "alice"
```

That's the series so far. Strategies decide what the household does, and
tax calculators compute what each return says. When the built-ins don't
fit, either can be your own.
