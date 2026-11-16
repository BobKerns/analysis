+++
series = "Planning with the simulator"
episode = "Video 10b"
title = "Spending strategies"
subtitle = "Spending that follows the markets"
data = "prototype_data"

[youtube]
title = "Spending strategies"
summary = """
Alice and Bill want to keep traveling for as long as the markets allow. \
They tag their travel as optional spending, see what happens to it as \
written across eighty futures, and try four built-in strategies that decide \
each January how much of it to spend: a threshold, Guyton-Klinger \
guardrails, a ceiling and floor, and VPW."""
contents = [
  "Optional spending, with tags (sim scheduled add, sim scheduled tag add)",
  "Spending by category (sim report expenses --by tag:category)",
  "The plan as written across 80 futures (sim ensemble summary)",
  "A threshold: travel only above a minimum (sim strategy add)",
  "Guardrails: Guyton-Klinger's cuts and raises (sim strategy log)",
  "A ceiling and floor on each year's change",
  "VPW: spending the portfolio down (sim ensemble spending)",
  "The four side by side (sim ensemble compare)",
  "The ensemble notebook",
  "Asking an AI",
]
+++

The second of video 10's two parts (issue #410): spending strategies.
Alice and Bill's rebalance scenario from video 10a, which is video 7's base
with a portfolio, a target and two rebalance strategies.

`setup.sh` beside this script builds rebalance with the commands videos 3
to 10a type, then what the scenes make, tagging the data repo before each
scene (`stage/<scene>`) so a tape can make on camera what later scenes
already have. Each scenario and ensemble is run once at the end and its run
renamed to a fixed time. The tapes work in `~/retirement/data/sim`, as in
videos 3 to 10a.

> Numbers in the narration were checked on 2026-11-10 against `setup.sh`.

## Scene `title`: Title card

```slide
template = "title"
```

Planning with the simulator. Video ten, part two: spending strategies.

Last time, strategies decided where the money comes from. This time, they
decide how much of it Alice and Bill spend.

## Scene `contents`: Spending that follows the markets

```slide
template = "card"
heading = "Spending that follows the markets"
body = [
  "Which spending is optional",
  "The plan as written, across 80 futures",
  "Four ways to decide each January",
  "Side by side",
]
speaker = "bill"
```

First, which of Alice and Bill's spending is optional. Then what happens
to it, as written, across eighty futures. Then four strategies that each
decide, every January, how much of it to spend, and how they compare.

## Scene `recap`: Strategies that own rules

```slide
template = "card"
heading = "A spending strategy owns the rules with a tag"
body = [
  "Video 10a: strategies that own a chain decide where money comes from",
  "These own scheduled rules, and decide how much of each is spent",
  "Only rules carrying their tag: never a tax or a premium by surprise",
  "Each rule still says the plan; the run keeps both",
]
speaker = "alice"
```

In part one, the strategies owned a chain, and decided where each dollar
came from. A spending strategy owns scheduled rules instead, and decides
how much of each to spend. It owns only the rules carrying its tag, so a
Medicare premium or a tax bill is never cut by surprise. Each rule still
says what they planned, and the run keeps both: the plan, and what was
spent.

## Scene `travel`: Optional spending

```tape
Set FontSize 16
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/travel && cd ~/retirement/data/sim && sim switch rebalance > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim scenario create travel rebalance > /dev/null"
Sleep 400ms
Enter
Wait
Sleep 2s
Type "clear && sim scheduled edit living --amount 6500"
Sleep 400ms
Enter
Wait
Sleep 4s
Type "clear && sim calendar edit 2036-01 --description 'Living without travel, now its own rule'"
Sleep 400ms
Enter
Wait
Sleep 4s
Type "clear && sim scheduled add 2026-01 Asset_Checking Expense_Living 1000 --name travel --person joint --series cpi"
Sleep 400ms
Enter
Wait
Sleep 5s
Type "clear && sim scheduled tag add living category:living"
Sleep 400ms
Enter
Wait
Sleep 2s
Type "clear && sim scheduled tag add healthcare category:health"
Sleep 400ms
Enter
Wait
Sleep 2s
Type "clear && sim scheduled tag add travel category:travel flex:optional"
Sleep 400ms
Enter
Wait
Sleep 5s
Type "clear && sim run travel --commit > /dev/null && sim report expenses --by tag:category --year 2040"
Sleep 400ms
Enter
Wait
Sleep 9s
```

Their base trims living expenses by a thousand dollars a month in 2036:
less travel. They'd rather keep traveling while the markets allow. In a
new scenario, travel, living drops to sixty five hundred dollars now, so
the 2036 cut changes nothing, and its description says so. Travel becomes
a rule of its own: a thousand dollars a month, raised with
inflation, into the same living account. Tags say what it is. Category
colon living, health and travel group the spending in reports, and flex
colon optional marks what a strategy may change. Sim report expenses by
category shows 2040: seventy eight thousand for living, twelve thousand
for travel, and eleven thousand for health, in today's dollars.

## Scene `as-written`: The plan as written

```tape
Set FontSize 14
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/as-written && cd ~/retirement/data/sim && sim switch travel > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim ensemble create travel-mc travel t_inflation_current t_market_current > /dev/null"
Sleep 400ms
Enter
Wait
Sleep 2s
Type "clear && sim run travel-mc --commit"
Sleep 400ms
Enter
Wait
Sleep 3s
Type "clear && sim ensemble summary travel-mc --start 2045 --end 2060"
Sleep 400ms
Enter
Wait
Sleep 14s
```

On one future, traveling for good looks fine. As in video six, they try
eighty futures with inflation and the markets varying: travel MC. Sim
ensemble summary shows net worth after tax, from 2045. The middle is
comfortable. But look at CVaR 5, the average of the worst five percent of
futures: from 2051 they average below zero, and by 2060 about two hundred
thousand dollars in the hole. Traveling as written, in a bad future,
runs them out of money.

## Scene `kinds`: Four ways to decide

```slide
template = "card"
heading = "Four ways to decide, each January"
body = [
  "threshold: travel only while the portfolio is above a minimum",
  "guardrail: cut or raise 10% as the withdrawal rate moves",
  "ceiling_floor: follow the portfolio, a few percent a year at most",
  "amortize (VPW): spend what would run the portfolio down by 100",
  "cut_on_breach answers a guard rail (video 9)",
]
speaker = "bill"
```

Four built-in strategies decide, each January, how much of the optional
spending to spend. Threshold skips it whole in a year that starts with the
portfolio below a minimum. Guardrail, from Guyton and Klinger, cuts or
raises it ten percent when the withdrawal rate moves too far. Ceiling and
floor follows the portfolio, but limits how far spending moves in a year.
And amortize, or VPW, spends what the portfolio could pay every year until
the younger of them is a hundred. A fifth, cut on breach, answers a guard
rail; that was video nine.

## Scene `threshold`: Travel above a minimum

```tape
Set FontSize 14
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/threshold && cd ~/retirement/data/sim && sim switch travel > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim scenario create threshold travel > /dev/null"
Sleep 400ms
Enter
Wait
Sleep 2s
Type "clear && sim strategy add spend --kind threshold --rules flex:optional --param portfolio=portfolio:invest --param minimum=800000"
Sleep 400ms
Enter
Wait
Sleep 5s
Type "clear && sim run threshold --commit > /dev/null && sim strategy log --strategy spend | head -n 4"
Sleep 400ms
Enter
Wait
Sleep 7s
Type "clear && sim ensemble create threshold-mc threshold t_inflation_current t_market_current > /dev/null"
Sleep 400ms
Enter
Wait
Sleep 2s
Type "clear && sim run threshold-mc --commit"
Sleep 400ms
Enter
Wait
Sleep 3s
Type "clear && sim ensemble spending threshold-mc | grep -E 'p95|^2060|worst|over the run'"
Sleep 400ms
Enter
Wait
Sleep 10s
```

The first is the simplest. A scenario, threshold, with a strategy, spend:
kind threshold, owning the rules tagged flex colon optional, watching the
portfolio from part one, with a minimum of eight hundred thousand dollars,
raised with inflation. Sim strategy log shows each January's decision. On
this one future the portfolio stays above it, so they travel every year.
Across eighty futures, sim ensemble spending sums it up. Half the futures
skip travel at least once, and on average one year in five has none. In
the worst years they spend eighty nine thousand instead of a hundred.

## Scene `guardrail`: Guardrails

```tape
Set FontSize 14
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/guardrail && cd ~/retirement/data/sim && sim switch travel > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim scenario create guardrail travel > /dev/null"
Sleep 400ms
Enter
Wait
Sleep 2s
Type "clear && sim strategy add spend --kind guardrail --rules flex:optional --param portfolio=portfolio:invest"
Sleep 400ms
Enter
Wait
Sleep 4s
Type "clear && sim run guardrail --commit > /dev/null && sim strategy log --strategy spend | head -n 5"
Sleep 400ms
Enter
Wait
Sleep 9s
Type "clear && sim strategy log --strategy spend --year 2029"
Sleep 400ms
Enter
Wait
Sleep 6s
Type "clear && sim ensemble create guardrail-mc guardrail t_inflation_current t_market_current > /dev/null"
Sleep 400ms
Enter
Wait
Sleep 2s
Type "clear && sim run guardrail-mc --commit"
Sleep 400ms
Enter
Wait
Sleep 3s
Type "clear && sim ensemble spending guardrail-mc | grep -E 'p95|^2060|worst|over the run'"
Sleep 400ms
Enter
Wait
Sleep 12s
```

Next, Guyton and Klinger's guardrails, with their usual settings. The first
January sets the withdrawal rate to beat: twelve thousand dollars of travel
from a portfolio of about a million, one point one eight percent. Each
January after, the log says what it did: hold, while the rate stays within
twenty percent of that. In 2029 the portfolio has grown and the rate is
more than twenty percent below where it started, so travel is raised ten
percent. The monthly lines, one for each payment it scaled, are counted at
the end; dash dash hook scale lists them. Across eighty
futures, half see a cut, and the worst years spend ninety two thousand.
But look at the top of 2060: one future in twenty spends two hundred and
fifty thousand dollars that year. Each raise is ten percent of the last,
and nothing caps them, so in the best futures travel grows more than tenfold.

## Scene `ceiling`: A ceiling and a floor

```tape
Set FontSize 14
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/ceiling && cd ~/retirement/data/sim && sim switch travel > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim scenario create ceiling-floor travel > /dev/null"
Sleep 400ms
Enter
Wait
Sleep 2s
Type "clear && sim strategy add spend --kind ceiling_floor --rules flex:optional --param portfolio=portfolio:invest"
Sleep 400ms
Enter
Wait
Sleep 4s
Type "clear && sim ensemble create ceiling-floor-mc ceiling-floor t_inflation_current t_market_current > /dev/null"
Sleep 400ms
Enter
Wait
Sleep 2s
Type "clear && sim run ceiling-floor-mc --commit"
Sleep 400ms
Enter
Wait
Sleep 3s
Type "clear && sim ensemble spending ceiling-floor-mc | grep -E 'p95|^2060|worst|over the run'"
Sleep 400ms
Enter
Wait
Sleep 12s
```

Ceiling and floor is the capped version. Spending aims at a share of the
portfolio, starting from their plan, but after inflation it may rise at
most five percent in a year and fall at most two and a half. Across eighty
futures the cuts are the gentlest of the four: the worst years still spend
ninety four thousand. The top of 2060 is lower than guardrail's, two
hundred and twenty seven thousand, because no year can jump.

## Scene `vpw`: Spending it down

```tape
Set FontSize 14
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/vpw && cd ~/retirement/data/sim && sim switch travel > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim scenario create vpw travel > /dev/null"
Sleep 400ms
Enter
Wait
Sleep 2s
Type "clear && sim strategy add spend --kind amortize --rules flex:optional --param portfolio=portfolio:invest"
Sleep 400ms
Enter
Wait
Sleep 4s
Type "clear && sim run vpw --commit > /dev/null && sim strategy log --strategy spend --year 2027"
Sleep 400ms
Enter
Wait
Sleep 12s
Type "clear && sim ensemble create vpw-mc vpw t_inflation_current t_market_current > /dev/null"
Sleep 400ms
Enter
Wait
Sleep 2s
Type "clear && sim run vpw-mc --commit"
Sleep 400ms
Enter
Wait
Sleep 3s
Type "clear && sim ensemble spending vpw-mc | grep -E 'p95|^2060|worst|over the run'"
Sleep 400ms
Enter
Wait
Sleep 12s
```

The last, amortize, is VPW: variable percentage withdrawal. Each January
it asks what the whole portfolio could pay every year, the same after
inflation, if it were spent down by the time Bill is a hundred. The first
year spends the plan. After that, travel takes the whole change in that
payment. In 2027 the portfolio has grown, the payment is up sixty three
hundred dollars, and all of it goes to travel: about eighteen thousand six
hundred, one and a half times the plan. Across eighty futures it spends
the most, a median of a fifth more than planned, and one future in twenty
spends six hundred thousand dollars in 2060. It also cuts most often,
in two futures out of three. Its ceiling and floor parameters limit the
swings, as ceiling and floor does.

## Scene `compare`: Side by side

```tape
Set FontSize 14
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/compare && cd ~/retirement/data/sim && sim switch travel-mc > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim ensemble compare travel-mc threshold-mc guardrail-mc ceiling-floor-mc vpw-mc | grep -E 'recipe|^ *2060'"
Sleep 400ms
Enter
Wait
Sleep 16s
```

Sim ensemble compare pairs each strategy's futures with the plan as
written. In 2060, every strategy is ahead in the worst five percent. The
threshold most of all, three hundred and eighteen thousand dollars, and it
is the only one never behind in any percentile, since it only ever skips.
VPW and guardrail give up a lot in the middle and top, because they spent
it. The bottom table is the floor: as written, the worst five percent end
two hundred thousand in the hole. With the threshold, they end a hundred
thousand ahead; with VPW, just above zero; with guardrail and ceiling and
floor, still below.

## Scene `tradeoff`: What each one trades

```slide
template = "card"
heading = "Each strategy trades cuts for what's left"
body = [
  "threshold: whole years without travel, the best floor",
  "guardrail: small cuts, raises with no cap",
  "ceiling_floor: the gentlest cuts, a lower floor",
  "VPW: spends the most, swings the most",
]
speaker = "alice"
```

None of them is best. The threshold protects the floor most, at the cost
of whole years without a trip. Guardrail cuts in small steps, but raises
without limit. Ceiling and floor cuts the least, and protects the least.
VPW spends the most, and its spending swings the most. Which trade suits
them is Alice and Bill's call.

## Scene `nb`: The ensemble notebook

```notebook
Set DataDir "home/retirement/data/sim"
Hide
Open "ensemble.ipynb"
RunAll
Scroll "Recipe:"
Select "Recipe:" "guardrail-mc"
Scroll "Spending against the plan"
Show
Sleep 3s
ScrollBy 450
Sleep 8s
```

The ensemble notebook draws the same for any recipe: the spread of
spending by year against the plan, dashed, with the table below. Here,
guardrail: the plan in the middle, and the top fanning out as the raises
add up.

## Scene `ai`: Asking an AI

```slide
template = "card"
heading = "Asking an AI about spending"
body = [
  "\"Would we rather skip trips, or shorten them?\"",
  "\"What minimum makes sense for us?\"",
  "\"How should we cap VPW's raises?\"",
]
speaker = "bill"
```

An AI assistant can help here too. It can talk through whether they'd
rather skip a trip or shorten it, which is the difference between a
threshold and a cut. It can help pick a minimum, and try a few in
ensembles. And it can explain how to cap the raises, with a ceiling on
VPW, or by using ceiling and floor instead.

## Scene `end`: Next time

```slide
template = "card"
heading = "Next: keeping the plan current"
body = "Updated balances, changed plans, and data from outside."
speaker = "alice"
```

Next time: keeping the plan current, as balances change and plans move
on.
