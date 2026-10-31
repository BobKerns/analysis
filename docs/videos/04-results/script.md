+++
series = "Planning with the simulator"
episode = "Video 4"
title = "Looking at the results"
subtitle = "Runs, net worth after tax, reports, and the notebooks"
data = "prototype_data"
+++

The fourth video of #366 (issue #375): what a run leaves behind, and the
ways to look at it. The terminal's quick look and reports first, then the
same views in the notebooks, and last, asking an AI.

`setup.sh` beside this script builds Alice and Bill's base scenario with
video 3's commands and runs it where video 3 did: before the refill chain,
after it, and after the calendar events. Each run gets a fixed time, so the
run files read the same in every recording and a tape can name the oldest.

The tapes work in `~/retirement/data/sim`, as in video 3. The notebook
scenes open this checkout's notebooks on the same data.

## Scene `title`: Title card

```slide
template = "title"
```

Planning with the simulator. Video four: looking at the results.

Alice and Bill's base scenario runs. Now, what did it say? This time, the
ways to look at a run: a quick summary in the terminal, net worth after
tax, reports on income, spending and tax, and the notebooks' charts.

## Scene `contents`: Ways to look at a run

```slide
template = "card"
heading = "Ways to look at a run"
body = [
  "Runs: one file each, the latest unless you say",
  "sim explain: did it do what you expected?",
  "Net worth, after tax and in today's dollars",
  "Reports: an account, income, spending, gains, tax",
  "The notebooks: the same views, as charts",
  "Asking an AI",
]
speaker = "bill"
```

Every run is saved, so you can look at it as often as you like. Sim
explain gives the quick answer to "did it do what I expected?". Net worth
comes after tax and in today's dollars. Reports break it down: one account,
income, spending, gains and tax. The notebooks show the same views as
charts you can change. And you can ask an AI about a run, in plain words.

## Scene `runs`: Every run is a file

```tape
Set FontSize 24
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "cd ~/retirement/data/sim && clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim list"
Sleep 400ms
Enter
Wait
Sleep 14s
```

A run plays the scenario forward, month by month, and saves everything in
one file: every balance, every dollar that moved, every year's tax. Sim
list shows the runs so far. These three are the runs from video three:
before the refill chain, after it, and after the calendar events. Each file
is named for when it ran, and the scenario. Every view of a run reads the
scenario's latest run unless you name another, so you rarely need these
names. Old runs are cleared out after a while, keeping the newest few.

## Scene `explain`: A quick look, and an older run

```tape
Set FontSize 22
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "cd ~/retirement/data/sim && clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim explain ../state/runs/2026-10-30T0905*"
Sleep 400ms
Enter
Wait
Sleep 12s
Type "clear && sim explain | tail -4"
Sleep 400ms
Enter
Wait
Sleep 9s
```

To look at an older run, name its file. Here's the first one, from before
the refill chain. Sim explain starts with where the run came from: the
code and the data it ran, and how many months. Then each lot at the end,
net worth each year, and at the bottom, the shortfall: in November 2032,
checking went below zero, and there was no refill chain to cover it. When
there is a chain, this line says what it did. A scenario with guard rails
also gets each stretch of years past one; video nine is about those. The
latest run, with no name given, has no negative balances at all.

> Production note: the tail keeps the second explain to its last lines;
> the first one showed the rest.

## Scene `after-tax`: What net worth means here

```slide
template = "card"
heading = "Net worth, as you can spend it"
body = [
  "After tax: less the tax the money still owes",
  "IRA withdrawals and unrealized gains are taxed when spent",
  "In today's dollars: inflation taken out",
  "--pre-tax and --dollars nominal show the raw totals",
]
speaker = "alice"
```

A dollar in Alice's traditional IRA isn't a dollar she can spend: tax
comes out when she takes it. The same goes for the gain in their brokerage
stocks. So the simulator shows net worth after the tax it still owes,
priced as if the money were drawn down over ten years at that year's
brackets. And it shows today's dollars, with inflation taken out, so 2060
compares fairly with 2026. Two options show the raw totals instead.

## Scene `networth`: Net worth by year

```tape
Set FontSize 20
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "cd ~/retirement/data/sim && clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim networth"
Sleep 400ms
Enter
Wait
Sleep 14s
Type "clear && sim networth --pre-tax --dollars nominal"
Sleep 400ms
Enter
Wait
Sleep 9s
```

Sim networth shows each year end. Net worth is everything they own, less
what they owe. Deferred tax is what the IRA and the gains still owe, and
unpaid tax is a finished year's bill, due the next April. What's left is
after tax: about one point eight million dollars in 2060, in today's money.
With dash dash pre tax and nominal dollars, the same run shows the plain
total in future dollars: more than five point eight million. Same plan;
the first number is the one to plan with.

## Scene `history`: One account over time

```tape
Set FontSize 19
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "cd ~/retirement/data/sim && clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim account history Asset_Checking --yearly"
Sleep 400ms
Enter
Wait
Sleep 11s
Type "clear && sim account history Asset_Brokerage_Joint --lot stocks --yearly"
Sleep 400ms
Enter
Wait
Sleep 11s
```

Sim account history follows one account. Checking climbs while both
paychecks come in, and falls once they stop and the car is bought. In 2032
and 2033 it sits near its five thousand dollar minimum, with the refill
chain topping it up, then climbs again once Bill's Social Security and
Alice's required distributions start. Name a lot, and you also see its cost basis and unrealized
gain. The brokerage stocks grow every year, and as the chain sells some,
the basis goes down with them.

## Scene `income`: Where the money came from

```tape
Set FontSize 26
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "cd ~/retirement/data/sim && clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim report income --year 2030"
Sleep 400ms
Enter
Wait
Sleep 6s
Type "sim report income --year 2031"
Sleep 400ms
Enter
Wait
Sleep 9s
```

Sim report income shows a year's income by kind, with each kind's share.
In 2030, Bill's last year at work, salary is still the biggest part. In
2031 there's no salary: Social Security and investments carry it, and
investment counts the growth of what they hold, sold or not. Without dash
dash year, it's a table of every year that follows each one from income,
through tax and spending, down to the change in net worth. It's wide, so
we'll see it in a notebook.

## Scene `expenses`: Spending and gains

```tape
Set FontSize 19
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "cd ~/retirement/data/sim && clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim report expenses"
Sleep 400ms
Enter
Wait
Sleep 10s
Type "clear && sim report gains"
Sleep 400ms
Enter
Wait
Sleep 11s
```

Sim report expenses shows the spending by category: living costs, health
care and taxes, each year. 2031 has the car, and from 2036 living costs
are lower. Sim report gains splits the investment side: gains that were
realized and taxed, ones that weren't taxed, and growth still on paper.
Its last column says whether the cash that came in covered the spending,
and when it didn't, the refill chain sold investments for the rest.

## Scene `tax`: Tax by year

```tape
Set FontSize 19
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "cd ~/retirement/data/sim && clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim report tax"
Sleep 400ms
Enter
Wait
Sleep 16s
```

Sim report tax shows each year's federal income tax as a tax form adds it
up: adjusted gross income, the deduction, the taxable income that's left,
the tax, and the effective rate on the whole. While the salaries come in,
it's about thirteen percent. Then a few years with almost nothing to tax,
until Bill's Social Security starts, and in 2035, Alice's required
distributions. A state with an income tax rule gets its own columns. Video seven
goes into the taxes.

## Scene `notebooks-card`: The notebooks

```slide
template = "card"
heading = "The notebooks"
body = [
  "net_worth: net worth, income, spending, gains, tax",
  "accounts: one account or lot over time",
  "events: every dollar that moved, and why",
  "scenario_overview: the setup, no run needed",
  "Open them with: analysis jupyter",
]
speaker = "bill"
```

The same views, and a few more, come as notebooks: pages in your browser
with charts and controls. Analysis jupyter opens them. You don't write any
code: run the notebook, pick from the menus, and the charts follow.

## Scene `nb-networth`: Net worth, as a chart

```notebook
Set DataDir "home/retirement/data/sim"
Hide
Open "net_worth.ipynb"
RunAll
Scroll "Net worth:"
Show
Sleep 6s
Click "Pre-tax"
Sleep 5s
Click "After-tax"
Sleep 3s
Drag "Years:" 2028 2040
Sleep 6s
```

The net worth notebook. At the top it picks the scenario, base. The chart
is net worth after tax, in nominal and today's dollars. Click pre tax, and
it shows the plain totals; after tax brings the tax back out. The years
slider zooms in on part of the run: here, the years around retirement. It
narrows every chart in the notebook at once.

## Scene `nb-income-tax`: Income and tax, as charts

```notebook
Set DataDir "home/retirement/data/sim"
Hide
Open "net_worth.ipynb"
RunAll
Scroll "Economic income and net worth change"
Show
Sleep 4s
Scroll "Dollar basis:" 4
Sleep 9s
Scroll "Tax"
Sleep 3s
Scroll "Dollar basis:" 5
Sleep 8s
```

Further down are the reports, as charts. Economic income is the wide table
from sim report income: above zero, what each year brought in after tax;
below, what spending and inflation took; and the black line, the change in
net worth. 2031's car shows plainly. The tax chart shows each year's tax,
with the effective rate on its own scale. Each chart has its table below
it.

## Scene `nb-accounts`: One account, as a chart

```notebook
Set DataDir "home/retirement/data/sim"
Set Zoom 1.25
Hide
Open "accounts.ipynb"
RunAll
Scroll "Values:"
Show
Sleep 3s
Select "Accounts:" "Asset_Checking"
Sleep 6s
Select "Accounts:" "Asset_Brokerage_Joint"
Sleep 3s
Select "Lots:" "stocks"
Sleep 7s
```

The accounts notebook draws one account's balance over the run. Pick
checking, and there's the climb, the drop to its minimum, and the climb
again. Pick the brokerage account, and its lots appear: pick stocks to
see that lot alone.

## Scene `nb-events`: Every dollar that moved

```notebook
Set DataDir "home/retirement/data/sim"
Set Zoom 1.25
Hide
Open "events.ipynb"
RunAll
Scroll "Accounts:"
Show
Sleep 3s
Select "Reason:" "refill"
Sleep 3s
ScrollBy 260
Sleep 7s
```

The events notebook lists every dollar the run moved, and why: which rule,
or a refill. Choose refill, and you see each time checking was topped up,
from which account in the chain, linked to the payment that needed it.
When a number surprises you, this is where to find out why.

## Scene `nb-overview`: The setup at a glance

```notebook
Set DataDir "home/retirement/data/sim"
Hide
Open "scenario_overview.ipynb"
RunAll
Scroll "Persons"
Show
Sleep 3s
Scroll "Chains"
Sleep 3s
ScrollBy 440
Sleep 6s
```

And the scenario overview shows the setup itself, with no run needed:
people, accounts, chains, rules and calendar events, with the refill chain
drawn as a flowchart. It's a good check that the scenario says what you
meant, before you trust what a run says.

## Scene `ai`: Asking an AI

```slide
template = "card"
heading = "Asking an AI about a run"
body = [
  "\"What was net worth at the end of each year in base?\"",
  "\"Why did checking go negative in November 2032?\"",
  "\"How much tax did base pay in 2030, and on what?\"",
  "Name the scenario, the account and the year",
]
speaker = "alice"
```

A run file is a database, so an AI assistant can read it for you, and you
can ask in plain words. Questions that name the scenario, the account and
the year get specific answers you can check against the reports. "How's my
retirement looking?" makes it guess.

## Scene `end`: Next time

```slide
template = "card"
heading = "Next: comparing scenarios"
body = "What-ifs side by side: what differs, and which comes out ahead."
speaker = "bill"
```

Next time: comparing scenarios. What-ifs side by side, what differs between
them, and which one comes out ahead.
