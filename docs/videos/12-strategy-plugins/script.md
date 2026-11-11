+++
series = "Planning with the simulator"
episode = "Video 12"
title = "Strategy plugins"
subtitle = "Writing your own strategy"
data = "prototype_data"

[youtube]
title = "Strategy plugins"
summary = """
Alice and Bill would rather skip a trip than shorten every one, and no \
built-in strategy does that. So they write their own, a strategy plugin: \
forty lines of Python that build on the built-in guardrail. They check it, \
use it like any other strategy, and see how it does across eighty futures."""
contents = [
  "Trips, not percentages (sim scheduled add --cadence annual)",
  "Where a strategy plugin lives (sim/strategies/)",
  "Building on a built-in: the class, its parameters, its hooks",
  "What a strategy may see and do",
  "Checking it (sim strategy kinds, sim strategy check)",
  "Using it (sim strategy add, sim strategy log)",
  "Across 80 futures (sim ensemble spending, sim ensemble compare)",
  "One future, up close (sim strategy log --member)",
  "The ensemble notebook",
  "Asking an AI",
]
+++

Video 12 (issue #411): strategy plugins. Alice and Bill split video 10b's
travel into three trips and write `whole_trips`, a strategy of their own
that builds on the built-in `guardrail`: guardrail decides each January how
much travel the year can afford, and their code decides which trips fit,
taken whole.

`setup.sh` beside this script builds 10b's `travel` scenario with the
commands videos 3 to 10b type, then what the scenes make, tagging the data
repo before each scene (`stage/<scene>`) so a tape can make on camera what
later scenes already have. The plugin is `whole_trips.py` beside this
script: setup copies it into the data repo, and the code slides read
their lines from it (`code_file`, `code_lines`), so the two can't drift.
After editing it, keep each slide's `code_lines` and `marks` on the lines
they mean. The trips as written (`trips-mc`) and the built-in
guardrail on them (`trips-guardrail-mc`) are made and run off camera, for
the compare scene. Each scenario and ensemble is run once at the end and
its run renamed to a fixed time. The tapes work in `~/retirement/data/sim`,
as in videos 3 to 11.

> Numbers in the narration were checked on 2026-11-11 against `setup.sh`.

<!-- cspell:ignore ordr -->

## Scene `title`: Title card

```slide
template = "title"
```

Planning with the simulator. Video twelve: strategy plugins.

The built-in strategies cover a lot. When none of them does what Alice and
Bill want, they can write their own.

## Scene `contents`: Writing your own strategy

```slide
template = "card"
heading = "Writing your own strategy"
body = [
  "Trips, not percentages",
  "A plugin that builds on a built-in",
  "Checking it, and using it",
  "Across 80 futures",
]
speaker = "bill"
```

First, why the built-ins don't quite fit Alice and Bill's travel. Then a
strategy plugin of their own, built on one of them. Then checking it,
using it, and seeing how it does across eighty futures.

## Scene `recap`: Trips, not percentages

```slide
template = "card"
heading = "A trip is taken, or it waits"
body = [
  "Video 10b: guardrail trims all the travel by the same share",
  "A 10% cut makes a $7,000 trip a $6,300 one",
  "Alice and Bill would rather skip a trip than shorten every one",
  "No built-in does that, so they write it",
]
speaker = "alice"
```

In video ten, part two, guardrail decided each January how much travel
the year could afford, and trimmed it all by the same share. A ten percent
cut makes a seven thousand dollar trip a sixty three hundred dollar one.
That isn't how Alice and Bill travel. A trip is taken, or it waits a year.
No built-in strategy does that, so they write one that does.

## Scene `trips`: Three trips

```tape
Set FontSize 14
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/trips && cd ~/retirement/data/sim && sim switch travel > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim scenario create trips travel > /dev/null"
Sleep 400ms
Enter
Wait
Sleep 2s
Type "clear && sim scheduled delete travel --yes"
Sleep 400ms
Enter
Wait
Sleep 6s
Type "clear && sim scheduled add 2026-06 Asset_Checking Expense_Living 7000 --name trip_abroad --person joint --series cpi --cadence annual"
Sleep 400ms
Enter
Wait
Sleep 4s
Type "clear && sim scheduled add 2026-03 Asset_Checking Expense_Living 3000 --name trip_beach --person joint --series cpi --cadence annual"
Sleep 400ms
Enter
Wait
Sleep 3s
Type "clear && sim scheduled add 2026-11 Asset_Checking Expense_Living 2000 --name visit_family --person joint --series cpi --cadence annual"
Sleep 400ms
Enter
Wait
Sleep 3s
Type "clear && for trip in trip_abroad trip_beach visit_family; do sim scheduled tag add $trip category:travel flex:optional; done"
Sleep 400ms
Enter
Wait
Sleep 6s
```

In a new scenario, trips, built on 10b's travel, the thousand dollars a
month for travel becomes three trips. The travel rule is inherited, so
deleting it here ends it before trips starts. Then the trips, once a year
each, raised with inflation: seven thousand dollars abroad every June,
three thousand at the beach every March, and two thousand to visit family
every November. Twelve thousand a year, as before. Each gets the same two
tags travel had: category travel, for reports, and flex optional, for a
strategy to own.

## Scene `where`: Where a plugin lives

```slide
template = "window"
look = "files"
heading = "A strategy plugin is a file in the data repo"
title = "sim"
address = "~/retirement/data/sim"
lines = ["strategies", "scenarios", "aux", "allocations.csv", "persons.csv", "securities.csv"]
note = "strategies/whole_trips.py: for every scenario, versioned with the data, copied into each run"
speaker = "bill"
```

A strategy plugin is a Python file in the data repo's strategies folder,
beside allocations dot c s v. Every scenario can use it. It's versioned
with the rest of the data, so editing it reruns what it affects. And each
run keeps a copy of the code that decided it, so a run always shows
exactly what it ran. Alice and Bill's is whole trips dot p y.

## Scene `class`: The class and its parameters

```slide
template = "window"
look = "code"
heading = "Building on guardrail"
title = "strategies/whole_trips.py"
status = "Python"
code_file = "whole_trips.py"
code_lines = [5, 19]
marks = [5, 9, 10, 14, 17, 18]
speaker = "alice"
```

It starts by importing guardrail, the built-in, and building on it: whole
trips is a guardrail, with changes. The register line makes it a strategy
kind, and its docstring is what the list of kinds shows. Its kind, whole
underscore trips, is the name a strategy file uses. It must have one of
its own, so a built-in is never replaced by surprise. Its parameters are
guardrail's, plus one more: the trips, in the order they want them most.

## Scene `year-start`: Each January

```slide
template = "window"
look = "code"
heading = "Each January: which trips fit"
title = "strategies/whole_trips.py"
status = "Python"
code_file = "whole_trips.py"
code_lines = [21, 35]
marks = [22, 23, 24, 26, 27, 31]
speaker = "bill"
```

Each January, year start asks guardrail first. Super, year start, makes
guardrail's own decision, and leaves the share of the plan it allows in
memory. That share of the year's budget is what travel can spend. Rule
budgets gives each trip's cost this year. Then, in their order, each trip
that still fits beside the ones already kept is kept, and the rest are
skipped. The kept list goes in
memory, for the rest of the year, and the January note says what was
skipped.

## Scene `scale`: Each trip

```slide
template = "window"
look = "code"
heading = "Each trip: pay it, or skip it"
title = "strategies/whole_trips.py"
status = "Python"
code_file = "whole_trips.py"
code_lines = [37, 45]
note = "Or delegate: self.delegate(Guardrail) holds a guardrail rather than being one"
speaker = "alice"
```

Then, each time a trip would be paid, scale decides. A kept trip is paid
as planned. A skipped one spends nothing this year. Below it, in order
sorts the trips by the order parameter, with any it leaves out last.
That's the whole strategy. Subclassing isn't the only way to build on a built-in: a
strategy can also hold a guardrail and hand it the January decision. The
user guide shows both.

## Scene `rules`: What a strategy may do

```slide
template = "card"
heading = "What a strategy may see and do"
body = [
  "It reads the run as of this month: balances, its budgets, its memory",
  "No future: when the plan ends, but never next year's markets",
  "It answers; the simulator posts the money",
  "Same inputs, same answers: no clock, files, network or chance",
]
speaker = "bill"
```

A strategy sees the run as of the current month: balances, its rules'
budgets, and its own memory. It knows when the plan ends, but never what
next year's markets will do. It doesn't move money itself: it answers,
and the simulator posts what it asked for. And it has to give the same
answer every time, with no clock, no files, no network and no chance, or
two runs of one plan could differ.

## Scene `check`: Checking it

```tape
Set FontSize 14
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/check && cd ~/retirement/data/sim && sim switch trips > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim strategy kinds whole_trips"
Sleep 400ms
Enter
Wait
Sleep 10s
Type "clear && sim scenario create trips-whole trips > /dev/null"
Sleep 400ms
Enter
Wait
Sleep 2s
Type `clear && sim strategy add spend --kind whole_trips --rules flex:optional --param portfolio=portfolio:invest --param 'ordr=["visit_family","trip_beach","trip_abroad"]'`
Sleep 400ms
Enter
Wait
Sleep 6s
Type `clear && sim strategy add spend --kind whole_trips --rules flex:optional --param portfolio=portfolio:invest --param 'order=["visit_family","trip_beach","trip_abroad"]'`
Sleep 400ms
Enter
Wait
Sleep 4s
Type "clear && sim strategy check 2> /dev/null"
Sleep 400ms
Enter
Wait
Sleep 6s
```

Sim strategy kinds lists whole trips beside the built-ins, with its
docstring, guardrail's parameters and their defaults, and then its own:
order. In a new scenario, trips whole, a strategy named spend uses it,
owning the rules tagged flex optional. A mistyped parameter is refused at
once, with the parameters it does take. Spelled right, it's added. Then
sim strategy check runs the scenario twice, each in a fresh Python, and
compares every decision: six hundred and eight, the same in both runs.

## Scene `use`: Using it

```tape
Set FontSize 14
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/use && cd ~/retirement/data/sim && sim switch trips-whole > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim run trips-whole --commit > /dev/null && sim strategy log --hook year_start | head -n 6"
Sleep 400ms
Enter
Wait
Sleep 12s
```

From here on, it's a strategy like any other. Sim run commits the new
file and runs the scenario, and sim strategy log shows each January's
decision. On this future, guardrail raises spending ten percent in 2029.
Whole trips can't take a tenth of a trip, so it keeps all three and
spends the plan. Raises add nothing; only cuts change what they do.

## Scene `futures`: Across eighty futures

```tape
Set FontSize 14
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/futures && cd ~/retirement/data/sim && sim switch trips-whole > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim ensemble create trips-whole-mc trips-whole t_inflation_current t_market_current > /dev/null"
Sleep 400ms
Enter
Wait
Sleep 2s
Type "clear && sim run trips-whole-mc --commit"
Sleep 400ms
Enter
Wait
Sleep 3s
Type "clear && sim ensemble spending trips-whole-mc | grep -E 'p95|^20(40|50|60)|worst|over the run'"
Sleep 400ms
Enter
Wait
Sleep 14s
```

Across eighty futures, as in 10b. Sim ensemble spending shows the spread:
the plan is about a hundred thousand dollars a year in today's dollars,
and in the worst five percent of years they spend about ninety thousand.
In half the futures, at least one trip is skipped at some point, about
three years in ten on average. And the median is the plan: 99.7 percent
of it.

## Scene `compare`: Against the built-in

```tape
Set FontSize 14
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/compare && cd ~/retirement/data/sim && sim switch trips-whole-mc > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim ensemble compare trips-mc trips-guardrail-mc trips-whole-mc | grep -E 'year|^ *2060'"
Sleep 400ms
Enter
Wait
Sleep 16s
```

Sim ensemble compare sets it beside the trips as written, and beside the
built-in guardrail trimming them, made the same way. In 2060, the worst
five percent of futures end about two hundred thousand dollars in the
hole as written, and thirty thousand in the hole with guardrail. With
whole trips they end thirty eight thousand dollars ahead. And at the top,
whole trips gives up nothing, while guardrail's raises spend two million
dollars at the ninety fifth percentile. Part of that is whole trips never
spending above the plan, not only skipping whole trips.

## Scene `lumpy`: One future, up close

```tape
Set FontSize 14
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/lumpy && cd ~/retirement/data/sim && sim switch trips-whole-mc > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim strategy log trips-whole-mc --member 2 --hook year_start | grep skip"
Sleep 400ms
Enter
Wait
Sleep 14s
```

Member two of the ensemble shows the price. In 2036, guardrail cuts ten
percent, leaving 94 percent of the plan. Six percent short, the trip
abroad doesn't fit, so it's skipped: more than half the year's travel.
And it stays skipped while the plan is short. In 2058, two percent short
is enough to skip it again.

## Scene `tradeoff`: The trade-off

```slide
template = "card"
heading = "Whole trips: what it trades"
body = [
  "More left in the worst futures, and nothing given up at the top",
  "But a small cut can cost a whole trip",
  "An idea to try: a little slack before the big trip goes",
  "It's their code: they can change it",
]
speaker = "alice"
```

Whole trips leaves more in the worst futures than trimming does, and
gives up nothing at the top. But a small cut can cost a whole trip. If
that's too abrupt, one idea is a little slack: take the trip if it
overshoots by less than, say, ten percent. That's a parameter and two
lines, and a good first change to try. It's their code, so they can.

## Scene `nb`: The ensemble notebook

```notebook
Set DataDir "home/retirement/data/sim"
Hide
Open "ensemble.ipynb"
RunAll
Scroll "Recipe:"
Select "Recipe:" "trips-whole-mc"
Scroll "Spending against the plan"
Show
Sleep 3s
ScrollBy 450
Sleep 8s
```

The ensemble notebook draws the same for a plugin as for a built-in: the
spread of spending by year against the plan, dashed. With whole trips,
the top is the plan as written, and the bottom steps down a trip at a
time.

## Scene `ai`: Asking an AI

```slide
template = "card"
heading = "Asking an AI about a plugin"
body = [
  "\"Write a strategy that skips our biggest trip first\"",
  "\"Add slack, so a small cut doesn't cost the trip abroad\"",
  "\"Why was the trip skipped in 2036?\"",
]
speaker = "bill"
```

An AI assistant is good at this. It can write a first version of a
strategy from a sentence, and then change it, like adding the slack. And
it can read the strategy log and explain why a trip was skipped in a
particular year.

## Scene `end`: Next time

```slide
template = "card"
heading = "Next: tax plugins"
body = "When a state's tax rule isn't built in, a plugin can add it."
speaker = "alice"
```

Next time: tax plugins. When your state has a tax rule the simulator
doesn't know, a plugin of your own can add it.
