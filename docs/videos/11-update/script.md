+++
series = "Planning with the simulator"
episode = "Video 11"
title = "Keeping the plan current"
subtitle = "A year later: the plan meets the statements"
data = "prototype_data"

[youtube]
title = "Keeping the plan current"
summary = """
A year after they built their plan, Alice and Bill bring it up to date. \
They keep last year's plan, start the plan again in January with what their \
statements say, add last year's income for the April tax bill, fix the \
spending that turned out wrong, and compare the new plan with last year's: \
where 2026 left them, and what it changes from here on."""
contents = [
  "Why bring the plan up to date",
  "Rolling the plan forward a year (sim scenario roll)",
  "Last year's income, for April's tax (sim ytd add --year)",
  "Spending that turned out wrong (sim report expenses, sim scheduled edit)",
  "Running everything again (sim all)",
  "Plan against actual (sim compare --section start)",
  "What it changes from here on (sim compare)",
  "What-ifs, strategies and imports",
  "Asking an AI",
]
+++

Video 11 (issue #502): keeping the plan current. It is January 2027, a
year after Alice and Bill built `base`, starting January 2026. They roll
it forward with `sim scenario roll`, add 2026's income for the April tax
bill, fix their spending, and compare the new plan with the old one.

`setup.sh` beside this script builds video 7's base, with 1,000 shares of
video 8's balanced fund BALX and its prices written in, then video 10a's
rebalance and 10b's travel on it, tagging the data repo before each scene
(`stage/<scene>`). Base is run once before the roll, and that run renamed
to a fixed time, so the roll tape reads the same projection every time.
Last year's plan, `plan-2026`, is run once too. Nothing after the roll is
run in setup: the compare scene reruns everything on camera with
`sim all`. The tapes work in `~/retirement/data/sim`, as in videos 3 to
10b.

<!-- cspell:ignore BALX -->

> Numbers in the narration were checked on 2026-11-11 against `setup.sh`.

## Scene `title`: Title card

```slide
template = "title"
```

Planning with the simulator. Video eleven: keeping the plan current.

A year ago, Alice and Bill built their plan. Now a year has happened.

## Scene `contents`: A year later

```slide
template = "card"
heading = "A year later: the plan meets the statements"
body = [
  "Keep last year's plan, and start again in January",
  "What the statements say, and last year's taxes",
  "Spending that turned out wrong",
  "Plan against actual, and from here on",
]
speaker = "bill"
```

First, keeping last year's plan, and starting the plan again in January
with what the statements say. Then last year's taxes, and the spending
that turned out wrong. Then how the year went against the plan, and what
it changes from here on.

## Scene `why`: Why bring it up to date

```slide
template = "card"
heading = "A plan only knows what you told it"
body = [
  "Base still opens with January 2026's balances",
  "2026 happened: the markets, the spending, the taxes",
  "Once a year, or after a big change, start again from the statements",
  "Keep the old plan, to see how it did",
]
speaker = "alice"
```

Base still opens with the balances Alice and Bill typed in January 2026,
and every year after follows from those. Since then the markets did worse
than the plan assumed, and they spent a bit more than they meant to. A
plan only knows what they told it. So once a year, or after a big change,
they start it again from the statements. And they keep the old plan, so
they can see how it did.

## Scene `roll`: Rolling forward a year

```tape
Set FontSize 16
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/roll && cd ~/retirement/data/sim && sim switch base > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim scenario roll base 2027-01 --keep plan-2026"
Sleep 400ms
Enter
Sleep 5s
Type "22400"
Sleep 600ms
Enter
Sleep 1s
Type "58200"
Sleep 600ms
Enter
Sleep 1s
Type "498700"
Sleep 600ms
Enter
Sleep 2s
Type "296500"
Sleep 600ms
Enter
Sleep 2s
Enter
Sleep 1s
Type "163900"
Sleep 600ms
Enter
Sleep 1s
Enter
Sleep 2s
Type "119800"
Sleep 600ms
Enter
Sleep 3s
Enter
Sleep 2s
Type "1024.8"
Sleep 600ms
Enter
Sleep 3s
Enter
Wait
Sleep 10s
```

Sim scenario roll moves a scenario's start, here base, to January 2027.
Keep plan 2026 copies the plan as it stood first. Then it goes through
every account and lot, and offers what last year's run projected for the
end of December. Alice and Bill type what their statements say instead:
twenty two thousand four hundred in checking, not thirty; fifty eight
thousand in savings; and four hundred ninety eight thousand in Alice's
IRA, not five hundred thirty six. The brokerage's stocks hold two hundred
ninety six thousand. Each cost basis comes from the plan, dividends
reinvested included, so Enter keeps it. Bill's Roth holds a hundred and
nineteen thousand. Its basis is his contributions, a hundred and twenty
thousand, so it stays; left blank, after a down year, it would shrink.
The balanced fund has a price, so roll asks for its shares: one thousand
twenty four point eight, worth forty eight thousand nine hundred at
December's price. Last, roll says what it can't do by itself: 2026's tax
is paid in April, from 2026's income.

## Scene `taxes`: Last year's income, for April's tax

```tape
Set FontSize 16
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/taxes && cd ~/retirement/data/sim && sim switch base > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type `sim ytd add wages_alice Income_Salary_Alice Asset_Checking 96412 --tax-character ordinary --year 2026 --description "Alice's W-2"`
Sleep 400ms
Enter
Wait
Sleep 2s
Type `sim ytd add wages_bill Income_Salary_Bill Asset_Checking 84780 --tax-character ordinary --year 2026 --description "Bill's W-2"`
Sleep 400ms
Enter
Wait
Sleep 2s
Type "sim ytd add dividends_2026 dividends Asset_Brokerage_Joint 4380 --to-lot stocks --tax-character qualified_dividend --year 2026 --description 1099-DIV"
Sleep 400ms
Enter
Wait
Sleep 2s
Type "sim ytd add interest_2026 interest_income Asset_Savings 1790 --tax-character ordinary --year 2026 --description 1099-INT"
Sleep 400ms
Enter
Wait
Sleep 2s
Type "sim ytd add gains_2026 capital_gains capital_gains 15900 --tax-character ltcg --year 2026 --description 1099-B"
Sleep 400ms
Enter
Wait
Sleep 2s
Type "clear && sim ytd list"
Sleep 400ms
Enter
Wait
Sleep 8s
```

The plan works out each year's tax in April of the next, from that year's
income. A plan that starts in January 2027 has no 2026 in it, so without
help, April's payment would be zero. Year to date rows with year 2026 put
it back: Alice's and Bill's W-2 wages, the 1099s' dividends and interest,
and the gains they realized. The last one moves no money, from capital
gains to itself, so it counts only for tax. Sim ytd list shows the five.

## Scene `spending`: Spending that turned out wrong

```tape
Set FontSize 16
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/spending && cd ~/retirement/data/sim && sim switch base > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim report expenses --year 2026 --scenario plan-2026 --dollars nominal"
Sleep 400ms
Enter
Wait
Sleep 8s
Type "clear && sim scheduled edit living --amount 8000 --base-date 2027-01 --scenario base"
Sleep 400ms
Enter
Wait
Sleep 4s
Type "clear && sim scheduled edit living --amount 7000 --base-date 2027-01 --scenario travel"
Sleep 400ms
Enter
Wait
Sleep 5s
```

Last year's plan expected ninety one thousand dollars of living expenses
in 2026. Their statements say about ninety six thousand: eight thousand a
month. So living becomes eight thousand a month in base, in January
2027's dollars, the base date. Travel, from video 10b, has a living rule
of its own, sixty five hundred with travel taken out, so base's change
doesn't reach it. It gets its own: seven thousand.

## Scene `compare`: Plan against actual

```tape
Set FontSize 16
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/compare && cd ~/retirement/data/sim && sim switch base > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim all --commit | tail -1"
Sleep 400ms
Enter
Wait
Sleep 3s
Type "clear && sim compare plan-2026 base --section start"
Sleep 400ms
Enter
Wait
Sleep 14s
```

Sim all runs everything again: base, and rebalance and travel, which are
built on it and start in January 2027 with it. Then sim compare, plan 2026
against base. The start section is plan against actual: what last year's
plan projected for December, and what base opens with. Altogether, ninety
two thousand dollars less. Most of it is the markets: thirty seven
thousand in Alice's IRA, twenty eight thousand in the stocks. The balanced
fund has the shares the plan expected; its price fell. Checking is seven
thousand six hundred short, which is the extra spending.

## Scene `after`: From here on

```tape
Set FontSize 16
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/compare && cd ~/retirement/data/sim && sim all --commit > /dev/null 2>&1; sim switch base > /dev/null 2>&1; clear"
Enter
Wait
Sleep 2s
Show
Sleep 500ms
Type "sim compare plan-2026 base --section net-worth --section break-even --start 2027 --end 2034"
Sleep 400ms
Enter
Wait
Sleep 14s
```

From here on, net worth after tax, in today's dollars. The two plans
start in different years, so sim compare puts base in plan 2026's dollars,
and the line under the title says so. Base is eighty five thousand behind
at the end of 2027, and the gap grows: a hundred and twenty four thousand
by 2034, as the lost growth compounds and the extra five hundred a month
adds up. It never catches up. That isn't bad news to hide; it's what the
next decisions start from.

## Scene `what-ifs`: Decided, and not yet

```slide
template = "card"
heading = "Decided changes go into base"
body = [
  "Base is the plan: what they've decided",
  "What-ifs stay scenarios built on it (video 5)",
  "Built on base, they move to January 2027 with it",
  "A rule a what-if overrides keeps its own value",
]
speaker = "bill"
```

The update goes into base, because base is the plan, what they've
decided. A what-if they haven't decided stays a scenario built on it, as
in video five. Those follow base by themselves: rebalance and travel now
start in January 2027, with the new balances. Only a rule a what-if
overrides keeps its own value, as travel's living did.

## Scene `strategies`: What strategies take off the list

```slide
template = "card"
heading = "What strategies take off the yearly list"
body = [
  "Rebalancing, and where withdrawals come from (video 10a)",
  "How much of the optional spending to spend (video 10b)",
  "Not the balances, the taxes, or changed plans",
]
speaker = "alice"
```

Video ten's strategies take some of this off the yearly list. A
rebalance strategy decides where withdrawals come from, and a spending
strategy how much of the optional spending to spend, every January by
themselves. They can't know what the statements say, what last year's
tax was, or that the plans changed. That's the update.

## Scene `imports`: Data from outside

```slide
template = "card"
heading = "Data from outside"
body = [
  "Today: Quicken exports (sim import holdings, sim import transactions)",
  "Coming: brokerage, bank and OFX files",
  "Prices are optional: dollars work for any lot",
]
speaker = "bill"
```

Typing a few balances is quick. With more accounts, sim import holdings
reads the balances from a Quicken export, and sim import transactions
turns its spending into expense rules. Brokerage, bank and OFX files are
coming. Shares and prices help where a fund has a price, but they're
optional: any lot works in dollars.

## Scene `ai`: Asking an AI

```slide
template = "card"
heading = "Asking an AI about the update"
body = [
  "\"What changed since last year's plan?\"",
  "\"Which of our assumptions were furthest off?\"",
  "\"Is this year's update complete?\"",
]
speaker = "alice"
```

An AI assistant can help with the update too. It can read the comparison
and say what changed since last year's plan, and which assumptions were
furthest off. And it can check the update is complete: every account,
last year's income, and the rows a new start changes.

## Scene `end`: The series so far

```slide
template = "card"
heading = "That's the series so far"
body = "The user guide has everything the videos showed, and more."
speaker = "bill"
```

That's the series so far. The user guide has everything the videos
showed, and more. See you next January.
