+++
series = "Planning with the simulator"
episode = "Video 9"
title = "Advanced"
subtitle = "Lifespans, guard rails and strategies"
data = "prototype_data"

[youtube]
title = "Advanced"
summary = """
How long will Alice and Bill live, and how do they know when they're \
spending too much? They give each of them a lifespan drawn from a life \
table, see what a death changes, draw guard rails around their \
withdrawals, and add a strategy that cuts spending when a rail is \
crossed, across a whole ensemble of futures."""
contents = [
  "Why a plan needs lifespans",
  "The life table: sim person show",
  "Setting a lifespan: sim person lifespan set",
  "What a death changes: filing status, Social Security, basis step-up (sim explain, sim compare)",
  "Guard rails: the 4% rule and Guyton-Klinger's guardrails (sim guard-rail add)",
  "Margins and cumulative margins (sim report guard-rails)",
  "Guard rails across an ensemble (sim ensemble guard-rails)",
  "A strategy that responds: cut_on_breach (sim strategy add --trigger)",
  "What the response bought, and what it cost (sim ensemble compare, sim ensemble spending)",
  "The lifespan and ensemble notebooks",
  "Asking an AI",
]
+++

The ninth video of #366 (issue #377): lifespans, guard rails and
strategies. Alice and Bill's base scenario from video 3, with video 7's
checking sweep.

`setup.sh` beside this script builds base with the commands videos 3 to 7
type, then what the scenes make, tagging the data repo before each scene
(`stage/<scene>`) so a tape can make on camera what later scenes already
have. Each scenario is run once at the end and its run renamed to a fixed
time. The tapes work in `~/retirement/data/sim`, as in videos 3 to 8.

> Numbers in the narration were checked on 2026-11-08 against `setup.sh`.

## Scene `title`: Title card

```slide
template = "title"
```

Planning with the simulator. Video nine: advanced topics.

So far, Alice and Bill have both lived to the end of the plan, and the
plan has never changed course. This time, neither is true.

## Scene `contents`: Plans that respond

```slide
template = "card"
heading = "Plans that respond"
body = [
  "How long they live",
  "What a death changes",
  "Guard rails: is spending too high?",
  "A strategy that responds when it is",
  "The notebooks, and asking an AI",
]
speaker = "bill"
```

First, how long Alice and Bill might live, and what a death changes. Then
guard rails: lines that tell them when they're spending too much. Then a
strategy that responds when they are. And last, the notebooks, and asking
an AI.

## Scene `why-lifespan`: Nobody knows their age at death

```slide
template = "card"
heading = "A plan has to last as long as they do"
body = [
  "Until now: both alive to the end of the plan",
  "A death ends a person's income and rules",
  "The survivor files single: higher tax",
  "Inherited investments' basis steps up",
  "The plan has to last until the second death",
]
speaker = "alice"
```

Until now, the plan has run to 2060 with both of them alive. Real plans
don't work that way. A death ends that person's income, such as a Social
Security check, and the rules that belong to them. The survivor files
taxes as single, which usually costs more. Investments the survivor
inherits get a new cost basis. And the money has to last until the
second death, which nobody knows in advance.

## Scene `table`: The life table

```tape
Set FontSize 20
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/table && cd ~/retirement/data/sim && sim switch base > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim person show alice"
Sleep 400ms
Enter
Wait
Sleep 14s
```

Sim person show reads the Social Security life table for Alice. She's
sixty three now. Half of women her age live past eighty six; one in
twenty dies before sixty nine, and one in twenty lives past ninety eight.
Below, every five years, the chance she has died by then. That spread is
the point: a plan built on one age is built on a guess.

## Scene `set`: Setting a lifespan

```tape
Set FontSize 20
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/set && cd ~/retirement/data/sim && sim switch base > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim scenario create lives base > /dev/null"
Sleep 400ms
Enter
Wait
Sleep 3s
Type "clear && sim person lifespan set alice 88 --vary life_table"
Sleep 400ms
Enter
Wait
Sleep 9s
Type "clear && sim person lifespan set bill 82 --vary life_table"
Sleep 400ms
Enter
Wait
Sleep 8s
Type "clear && sim person show --graph"
Sleep 400ms
Enter
Wait
Sleep 16s
```

Alice and Bill try it in a new scenario, lives. Sim person lifespan set
gives Alice eighty eight, a little above the table, since she's healthy,
and dash dash vary life table: in an ensemble, each future draws her age
from the table, shifted so eighty eight is the middle. Bill gets eighty
two, a little below his. Sim person show dash dash graph draws both: each
one's chance of dying at each age, and at the bottom, the household. The
last of them most likely dies around 2053, and one time in twenty, after
2063: past the end of the plan.

## Scene `death`: What a death changes

```tape
Set FontSize 18
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/death && cd ~/retirement/data/sim && sim switch lives > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim run lives"
Sleep 400ms
Enter
Wait
Sleep 4s
Type "clear && sim explain lives | tail -n 3"
Sleep 400ms
Enter
Wait
Sleep 12s
Type "clear && sim compare base lives --section tax"
Sleep 400ms
Enter
Wait
Sleep 16s
```

A single run uses the ages they typed: Bill dies in August 2046, Alice in
April 2050. Sim explain lists what the deaths did to their brokerage: at
Bill's death, his half of the stocks' cost basis steps up to their value,
and at Alice's, the rest. Sim compare shows the tax. From 2047, Alice
files single, and on about the same income pays about three thousand
dollars more a year. After 2050 there's no one left to tax. Over the
run, one Social Security check stopping costs them about eight hundred
and sixty thousand dollars, in today's dollars.

## Scene `rails`: Guard rails

```slide
template = "card"
heading = "A guard rail is a line to stay inside"
body = [
  "The 4% rule: withdraw 4% of the starting portfolio, plus inflation",
  "Guyton-Klinger: above 6% of this year's portfolio, cut",
  "... and below 4%, there's room to spend more",
  "A rail reads a run; it doesn't change it",
]
speaker = "bill"
```

How do Alice and Bill know when they're spending too much? A guard rail
is a line their withdrawals or spending should stay on one side of. The
best known is the four percent rule: withdraw four percent of the
portfolio you retire with, raised each year with inflation. Guyton and
Klinger's guardrails use this year's portfolio instead: above six
percent, cut back; below four percent, there's room to spend more. A
rail only reads a run. It never changes one.

## Scene `rail-add`: Adding rails

```tape
Set FontSize 16
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/rail-add && cd ~/retirement/data/sim && sim switch lives > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim guard-rail add four_percent --measure withdrawals --side above --basis initial_portfolio --threshold 4% --start 2032-01"
Sleep 400ms
Enter
Wait
Sleep 6s
Type "clear && sim guard-rail add gk_upper --measure withdrawals --side above --basis portfolio --threshold 6% --start 2032-01"
Sleep 400ms
Enter
Wait
Sleep 5s
Type "clear && sim guard-rail add gk_lower --measure withdrawals --side below --basis portfolio --threshold 4% --sense positive --start 2032-01"
Sleep 400ms
Enter
Wait
Sleep 6s
Type "clear && sim guard-rail list"
Sleep 400ms
Enter
Wait
Sleep 12s
```

Sim guard-rail add writes each one. Four percent: withdrawals above four
percent of the portfolio when the rail starts. GK upper: above six
percent of each year's portfolio. GK lower: below four percent, with
dash dash sense positive, since being past it is good news. They all
start in 2032, after the year they buy a new car, which would cross
every rail. Sim guard-rail list shows all three.

## Scene `margins`: Margins

```slide
template = "card"
heading = "How much room is left"
body = [
  "Margin: this year's room before the rail",
  "Negative: past it",
  "Cumulative margin: room banked, or overdrawn, so far",
  "A positive rail: past it means room to spend more",
]
speaker = "alice"
```

For each year, a rail has a margin: how much room is left before the
line. A negative margin means past it. The cumulative margin adds the
years up: a year under the line banks room, and a year over it draws
some down. For a positive rail, like GK lower, being past it means there
was money they could have spent.

## Scene `rail-report`: Where the run stands

```tape
Set FontSize 16
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/rail-report && cd ~/retirement/data/sim && sim switch lives > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim report guard-rails --rail four_percent --rail gk_lower"
Sleep 400ms
Enter
Wait
Sleep 18s
Type "clear && sim report guard-rails --chart"
Sleep 400ms
Enter
Wait
Sleep 16s
```

Sim report guard-rails shows the run year by year. Withdrawals are past
the four percent rule in 2032 and 2033, by about twelve thousand dollars
a year, before Bill's Social Security starts in 2034. Then they're well inside,
until Bill's death in 2046, when one check stops. GK lower says there was
room to spend more from 2034 on, up to forty thousand dollars a year.
Dash dash chart draws the same thing.

## Scene `rail-mc`: Rails across an ensemble

```tape
Set FontSize 18
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/rail-mc && cd ~/retirement/data/sim && sim switch lives > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim ensemble create lives-mc lives t_inflation_current t_market_current > /dev/null"
Sleep 400ms
Enter
Wait
Sleep 3s
Type "clear && sim run lives-mc"
Sleep 400ms
Enter
Wait
Sleep 3s
Type "clear && sim ensemble guard-rails lives-mc --chart"
Sleep 400ms
Enter
Wait
Sleep 22s
```

One run is one future. Lives MC is an ensemble of eighty, with inflation
and the markets varying as in video six, and now each person's lifespan
drawn from the life table too. Sim ensemble guard-rails dash dash chart
shows the share of futures past each rail each year. In 2032, the four
percent rule is past in most of them, and GK upper, the one that says
cut, in four futures out of ten. The red dashed lines are the deaths in
the base case. In the late years, only the households still alive count,
so the lines jump around.

## Scene `strategy`: A strategy that responds

```slide
template = "card"
heading = "A strategy can respond to a rail"
body = [
  "A strategy: a decision the scenario makes as it goes",
  "A trigger: tell it when a rail is crossed",
  "Checked each January, on last year",
  "cut_on_breach: cut flexible spending while past",
]
speaker = "bill"
```

What would Alice and Bill actually do if withdrawals passed six percent?
Cut back. A strategy is a decision the scenario makes as it goes, and a
trigger tells it when a rail is crossed. Each January it looks at last
year. The built-in cut on breach strategy cuts the spending they tagged
as flexible by ten percent while the rail is past, and goes back to plan
when it isn't.

## Scene `respond`: Adding the response

```tape
Set FontSize 16
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/respond && cd ~/retirement/data/sim && sim switch lives-mc > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim ensemble create respond-mc lives-mc > /dev/null"
Sleep 400ms
Enter
Wait
Sleep 3s
Type "clear && sim scheduled tag add living flex:optional"
Sleep 400ms
Enter
Wait
Sleep 6s
Type "clear && sim strategy add respond --kind cut_on_breach --rules flex:optional --trigger gk_upper:annual --param cut=10% --param increase=0%"
Sleep 400ms
Enter
Wait
Sleep 10s
Type "clear && sim run respond-mc"
Sleep 400ms
Enter
Wait
Sleep 4s
```

A new ensemble, respond MC, builds on lives MC, so its futures are the
same ones. Alice and Bill tag their living expenses flex optional: the
spending they could trim. Sim strategy add respond, kind cut on breach,
on the rules tagged flex optional, triggered by GK upper's yearly
margin: cut ten percent, raise nothing. Then they run it.

## Scene `breaches`: What it did, in one future

```tape
Set FontSize 16
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/done && cd ~/retirement/data/sim && sim switch respond-mc > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim strategy log --breaches --runfile ../state/runs/respond-mc/monte-carlo/2026-11-08T091100Z/member_0005.sqlite3"
Sleep 400ms
Enter
Wait
Sleep 16s
```

Sim strategy log dash dash breaches shows one future's decisions. In
this one, withdrawals pass six percent in 2047; the next January the
strategy cuts. A year later they're back inside, and spending goes back
to plan. Then past again in 2049, for three years. Each line is one
January's look back at the year before.

## Scene `respond-compare`: What it bought, and what it cost

```tape
Set FontSize 18
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/done && cd ~/retirement/data/sim && sim switch respond-mc > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim ensemble compare lives-mc respond-mc --chart"
Sleep 400ms
Enter
Wait
Sleep 18s
Type "clear && sim ensemble spending respond-mc | tail -n 2"
Sleep 400ms
Enter
Wait
Sleep 14s
```

Sim ensemble compare sets the two ensembles side by side, future by
future. Before 2033 they're the same, apart from a few hundred dollars of
estimated tax. After that, the worst five percent of futures end each
year about a hundred thousand dollars better off by 2050. Sim ensemble
spending shows the cost: across the run, cuts in fifty four percent of
futures, in fifteen percent of years on average, and in the worst
futures, their worst year's spending about eight thousand dollars lower.

## Scene `nb`: The notebooks

```notebook
Set DataDir "home/retirement/data/sim"
Hide
Open "lifespan.ipynb"
RunAll
Scroll "Reading the charts"
Show
Sleep 10s
Hide
Open "ensemble.ipynb"
RunAll
Scroll "Guard rails"
Show
Sleep 4s
ScrollBy 450
Sleep 10s
```

The notebooks have the same views. The lifespan notebook draws each
person's chances, and the household's. The ensemble notebook's guard
rails section shows the shares and overshoots for any ensemble, beside
its spending and its comparisons.

## Scene `ai`: Asking an AI

```slide
template = "card"
heading = "Asking an AI about lifespans and rails"
body = [
  "\"What's a sensible age at death for us to plan for?\"",
  "\"Which guard rails suit a plan like ours?\"",
  "\"How much should we cut when withdrawals pass 6%?\"",
]
speaker = "alice"
```

An AI assistant can help here too. It can talk through what age to plan
for, given your health and family history; it's a conversation, not a
diagnosis. It can explain the different guard rails and which suit a plan
like yours. And it can read your runs and help you decide how deep a cut
to try, and compare the ensembles that result.

## Scene `end`: Next time

```slide
template = "card"
heading = "Next: strategies"
body = "Rebalancing, spending and withdrawals that decide as they go."
speaker = "bill"
```

Next time: strategies. Rebalancing, spending, and withdrawals that
decide as the plan goes.
