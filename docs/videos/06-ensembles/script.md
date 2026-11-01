+++
series = "Planning with the simulator"
episode = "Video 6"
title = "Ensembles"
subtitle = "Running the plan in many futures, not one"
data = "prototype_data"

[youtube]
title = "Ensembles"
summary = """
So far every run has given one answer, with markets and inflation at their \
long-run rates every year. An ensemble runs Alice and Bill's plan in eighty \
futures, each with its own markets and inflation, and shows the range: how \
good it might get, and how bad. Then the same eighty futures answer video \
5's question again: does working longer help in the bad ones?"""
contents = [
  "Monte Carlo in plain words: one plan, many futures",
  "The supplied templates: long-run rates, year-by-year paths, today's rates",
  "Making an ensemble: sim ensemble create",
  "Making a row vary: a range for the new car (sim calendar edit)",
  "Factors: why inflation and interest rates move together (sim ensemble draws --pair)",
  "Running it, and reading the fan chart (sim ensemble summary)",
  "Comparing a what-if across the same futures (sim ensemble compare)",
  "The ensemble notebook",
  "Asking an AI",
]
+++

The sixth video of #366 (issue #369): ensembles. Alice and Bill's base
scenario, from video 3, and video 5's work-longer what-if, run across eighty
futures drawn from the supplied templates.

`setup.sh` beside this script builds base and work-longer with the commands
videos 3 and 5 type, then the ensembles the scenes make, tagging the data
repo before each scene (`stage/<scene>`) so a tape can make on camera what
later scenes already have. Both ensembles are run once at the end and their
batches renamed to fixed times. The tapes work in `~/retirement/data/sim`,
as in videos 3 to 5.

## Scene `title`: Title card

```slide
template = "title"
```

Planning with the simulator. Video six: ensembles.

Every run so far has given one answer, as if the markets earned exactly
their long-run rates, every single year. They won't. This time, Alice and
Bill run their plan in eighty different futures, and see the range.

## Scene `contents`: Many futures

```slide
template = "card"
heading = "Many futures"
body = [
  "One plan, many futures: Monte Carlo",
  "The templates that supply the futures",
  "Making an ensemble, and a row that varies",
  "Factors: what moves together",
  "Reading the fan chart",
  "A what-if, across the same futures",
  "The notebook, and asking an AI",
]
speaker = "bill"
```

First, what an ensemble is. Then the templates that supply its futures,
making one, and making a row of their own vary. Then factors, which keep
things that move together moving together. Then running it, and reading
the result. Then video five's what-if, across the same futures. And last,
the notebook, and asking an AI.

## Scene `monte-carlo`: One plan, many futures

```slide
template = "card"
heading = "One plan, many futures"
body = [
  "Each member is a whole run of the plan",
  "Each draws its own markets and inflation, year by year",
  "Member 0 is the middle case: every value at its median",
  "Eighty members: a quick look. Four hundred: to be sure",
  "The answer is the range, not one line",
]
speaker = "alice"
```

The idea has a grand name, Monte Carlo, but it's simple. Instead of
running the plan once, run it many times. Each run, called a member,
draws its own future: a good decade for stocks, or a bad one; inflation
that runs hot, or cool. Member zero is the exception: it takes the middle
value of everything, so there's always one ordinary case to check. Eighty
members is enough for a first look; four hundred, to be sure of a number.
And the answer is no longer one line, but the spread of all of them.

## Scene `templates`: The supplied templates

```slide
template = "card"
heading = "Futures, supplied"
body = [
  "t_inflation_series, t_market_series: long-run rates (what base uses)",
  "…_ensemble: a new path every year, around a drawn level",
  "…_current: today's rates, held a while, then back to the long run",
  "Episodes to mix in: great inflation, financial crisis, pandemic spike",
  "Each is a scenario: build on it like any other",
]
speaker = "bill"
```

Nobody has to invent those futures. The simulator ships template
scenarios, whose names start with t underscore. Base already builds on two
of them: long-run inflation and market rates, the same every year. The
ensemble templates add a path that moves every year, around a long-run
level that each member draws. The current templates start from today's
rates, hold them for a while, then let them drift back. And there are
historical episodes to mix in, like the great inflation, or the financial
crisis. Each one is just a scenario, so you build on it like any other.

## Scene `create`: Making an ensemble

```tape
Set FontSize 24
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/create && cd ~/retirement/data/sim && sim switch base > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim ensemble create markets base t_inflation_current t_market_current"
Sleep 400ms
Enter
Wait
Sleep 9s
```

Sim ensemble create works like sim scenario create: a name, a parent,
and mix-ins. Markets builds on base, with today's inflation and market
templates mixed in. It inherits a seed, which fixes which futures it
draws, and a size of eighty members, both from the templates. Base itself
is unchanged.

## Scene `varying`: A row of their own

```tape
Set FontSize 18
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/varying && cd ~/retirement/data/sim && clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim calendar edit 2031-06 --name car --vary-target amount --vary-distribution triangular --vary-low 30000 --vary-mode 40000 --vary-high 55000"
Sleep 400ms
Enter
Wait
Sleep 4s
Type "clear && sim calendar list --kind one_off_transfer"
Sleep 400ms
Enter
Wait
Sleep 8s
```

The templates vary the markets. Alice and Bill can vary their own rows
too. The new car in 2031 was forty thousand dollars, but who knows?
Sim calendar edit gives the row a name, and a range: triangular, at least
thirty thousand, most likely forty, maybe fifty five. The row comes from
base, so the edit writes an override into markets, and base keeps its
plain forty thousand.

## Scene `factors`: What moves together

```slide
template = "card"
heading = "Factors: what moves together"
body = [
  "A factor is a shared influence, not a value of its own",
  "core inflation → interest rates → the cash yield",
  "core inflation → the CPI → prices",
  "So high inflation tends to come with high rates",
  "The templates' tree comes from a century of history",
]
speaker = "alice"
```

If every value were drawn on its own, the futures would be strange: high
inflation with rock bottom interest rates, year after year. In real life,
they tend to move together. The templates tie them with factors: shared
influences that aren't values themselves. Core inflation sits at the top;
the consumer price index hangs under it, and so do interest rates, with
the cash yield under those. How tightly each one follows was measured
from a century of history.

## Scene `pair`: Seeing it in the draws

```tape
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "cd ~/retirement/data/sim && clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim ensemble draws markets --pair cpi_path,cash_path --chart"
Sleep 400ms
Enter
Wait
Sleep 14s
```

Sim ensemble draws shows what each member drew. With dash dash pair, it
plots two values against each other: here, each future's long-run
inflation across, and its cash yield up. They rise together: the tree
implies a correlation of about point eight, and the draws came out close
to that. A future with hot inflation usually pays more on cash, too.

## Scene `run`: Running it

```tape
Set FontSize 24
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/run && cd ~/retirement/data/sim && clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim run markets"
Sleep 400ms
Enter
Wait
Sleep 5s
```

Sim run sees that markets is an ensemble, and runs every member, as many
at once as the computer has processors. Eighty whole runs of the plan take
about half a minute here. Each member keeps its own run file, in one
folder for the batch. And look: in five of the eighty futures, an account
went negative. In those, the money ran out.

> Production note: the count is live; how long it takes depends on the
> machine that records it.

## Scene `fan`: The fan chart

```tape
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "cd ~/retirement/data/sim && clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim ensemble summary markets --chart"
Sleep 400ms
Enter
Wait
Sleep 22s
```

Sim ensemble summary, with dash dash chart, draws a fan chart: net worth
after tax, in today's dollars. The darkest band is the middle fifth of the
futures; the lighter ones reach out to the luckiest and unluckiest five
percent. In 2060, the median future ends with about one point six
million dollars, a little under base's single run. But the top of the fan
reaches past ten million, and the bottom comes down close to zero.

## Scene `table`: The numbers behind it

```tape
Set FontSize 18
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "cd ~/retirement/data/sim && clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim ensemble summary markets --start 2050"
Sleep 400ms
Enter
Wait
Sleep 14s
```

Without dash dash chart, the same numbers come as a table, one row a
year. Mean, median, and the percentiles that mark the bands. The last two
columns are the ones to watch: cvar zero five is the average of the worst
five percent of futures, and in 2060 it's under twenty thousand dollars.
In those futures, Alice and Bill all but run out.

## Scene `reading`: How to read it

```slide
template = "card"
heading = "How to read it"
body = [
  "The median is the middle future; the mean is pulled up by a lucky few",
  "The bands: where most futures land",
  "cvar05, cvar20: the average of the worst 5% and 20%",
  "A plan is safer when its bad futures are better",
  "Small differences? Run more members before trusting them",
]
speaker = "bill"
```

A few things to keep in mind. The median is the middle future; the mean
sits higher, because a few very lucky futures pull it up. The bands show
where most futures land. The tail averages, cvar five and cvar twenty,
show how bad the bad ones are. That's the question that matters for
safety: not how good the average is, but how bad things get. And with
eighty members, small differences are noise; run more before trusting
them.

## Scene `compare`: A what-if, across the same futures

```tape
Set FontSize 20
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/run && cd ~/retirement/data/sim && clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim ensemble create work-longer-mc markets work-longer"
Sleep 400ms
Enter
Wait
Sleep 6s
Hide
Type "git -C ~/retirement/data reset -q --hard stage/compared && clear"
Enter
Sleep 1s
Show
Type "sim ensemble compare markets work-longer-mc --start 2029 --end 2036 | head -12"
Sleep 400ms
Enter
Wait
Sleep 14s
```

Now video five's question again: what if Bill works two more years?
Work longer mc builds on markets and mixes in the work longer what-if. It
inherits markets' seed, so its member one draws the same future as
markets' member one, and so on. Sim ensemble compare can then compare them
future by future. From 2031 on, working longer comes out ahead in every
one of the eighty futures, and the average of the worst five percent
improves by over a hundred thousand dollars. So yes: it helps most where
help is needed.

## Scene `compare-chart`: The comparison, drawn

```tape
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "cd ~/retirement/data/sim && clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim ensemble compare markets work-longer-mc --chart"
Sleep 400ms
Enter
Wait
Sleep 18s
```

With dash dash chart, the comparison is drawn. On top, the difference in
each future, as a fan: a little behind before 2031, for the tax reasons
video five explained, then well ahead. Below, the change in the worst
five and twenty percent, and the change at each percentile.

## Scene `nb-ensemble`: The ensemble notebook

```notebook
Set DataDir "home/retirement/data/sim"
Hide
Open "ensemble.ipynb"
RunAll
Scroll "Recipe:"
Show
Sleep 7s
Drag "Years:" 2026 2040
Sleep 2s
ScrollBy 450
Sleep 5s
Scroll "Baseline:"
Sleep 2s
Select "Compare:" "work-longer-mc"
Sleep 3s
ScrollBy 350
Sleep 7s
```

The ensemble notebook shows the same, with controls. Pick a recipe, and
an account if you like, and it draws the fan chart over the year-end
table. The years slider zooms in: here, the first fifteen years. Further
down, a baseline and a recipe to compare it with, head to head; and below
that, guard rails, spending, and a table for several recipes at once.

## Scene `ai`: Asking an AI

```slide
template = "card"
heading = "Asking an AI about an ensemble"
body = [
  "\"In markets, what do the worst futures have in common?\"",
  "\"How likely is net worth to fall below $250,000?\"",
  "\"Which varying rows matter most?\" (sim ensemble sensitivity)",
]
speaker = "alice"
```

An AI assistant can read ensembles too. Ask what the worst futures have in
common, or how likely net worth is to fall below some amount. And to find
which varying rows matter most, sim ensemble sensitivity measures each
one's effect; the ensembles guide explains how.

## Scene `end`: Next time

```slide
template = "card"
heading = "Next: taxes"
body = "What the simulator taxes, how, and how to see where the money goes."
speaker = "bill"
```

Next time: taxes. What the simulator taxes, and how, and how to see where
the money goes.
