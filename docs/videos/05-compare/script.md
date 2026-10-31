+++
series = "Planning with the simulator"
episode = "Video 5"
title = "Comparing what-ifs"
subtitle = "Scenarios that build on base, and which one comes out ahead"
data = "prototype_data"

[youtube]
title = "Comparing what-ifs"
summary = """
What if Bill works two more years? What if he claims Social Security at 67 \
instead of 70? Each what-if is a small scenario built on Alice and Bill's \
base, holding only what's different. Then sim compare and the comparison \
notebook show what differs, and which one comes out ahead."""
contents = [
  "A what-if builds on another scenario: parents, mix-ins, only the rows that differ",
  "Making one: sim scenario create, then editing an inherited rule (sim scheduled edit)",
  "Combining two what-ifs with a mix-in, and running them all (sim all)",
  "sim compare: what differs, net worth by year, break-even, one account (--account, --lot)",
  "Why working longer looks a little worse at first, after tax",
  "The scenario comparison notebook",
  "Asking an AI to compare",
]
+++

The fifth video of #366 (issue #376): what-ifs as scenarios built on base,
and comparing them. Alice and Bill ask two questions: what if Bill works two
more years, and what if he claims Social Security at 67 instead of 70? A
third scenario mixes the two.

`setup.sh` beside this script builds the base scenario with video 3's
commands, as video 4's does, then makes the what-ifs the way the scenes do,
tagging the data repo before each scene (`stage/<scene>`) so a tape can make
on camera what later scenes already have. Every scenario is run once at the
end, with fixed times. The tapes work in `~/retirement/data/sim`, as in
videos 3 and 4.

## Scene `title`: Title card

```slide
template = "title"
```

Planning with the simulator. Video five: comparing what-ifs.

Alice and Bill have a base plan. Now they have questions. What if Bill
works two more years? What if he claims Social Security sooner? This time,
each question becomes a scenario of its own, and we compare them.

## Scene `contents`: What-ifs, side by side

```slide
template = "card"
heading = "What-ifs, side by side"
body = [
  "A what-if builds on base, and holds only what's different",
  "Making one: create it, then edit what it inherits",
  "Mixing two what-ifs together",
  "sim compare: what differs, and who comes out ahead",
  "The comparison notebook",
  "Asking an AI",
]
speaker = "bill"
```

First, what a what-if is: a scenario that builds on base, and holds only
what's different. Then making two of them, and mixing them together. Then
sim compare, which shows what differs between them and which comes out
ahead, and the notebook that draws it. And last, asking an AI.

## Scene `builds-on`: A scenario builds on another

```slide
template = "card"
heading = "A scenario builds on another"
body = [
  "work-longer: base, with Bill's salary to the end of 2032",
  "claim-early: base, with Bill's Social Security at 67",
  "both: work-longer, with claim-early mixed in",
  "Everything else comes from base",
  "Fix base, and every what-if follows",
]
speaker = "alice"
```

A scenario doesn't have to start from scratch. Work longer builds on base:
it holds one changed row, Bill's salary running two more years, and
everything else comes from base. Claim early does the same with Bill's
Social Security. Both takes work longer as its parent and mixes in claim
early, so it gets both changes. And when Alice fixes something in base,
every what-if picks it up the next time it runs.

## Scene `create`: Making a what-if

```tape
Set FontSize 24
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/create && cd ~/retirement/data/sim && clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim scenario create work-longer base"
Sleep 400ms
Enter
Wait
Sleep 5s
Type "clear && sim scheduled edit salary_bill --end 2032-12"
Sleep 400ms
Enter
Wait
Sleep 4s
Type "cut -d, -f1,20 scenarios/work-longer/scheduled.csv"
Sleep 400ms
Enter
Wait
Sleep 7s
```

Sim scenario create makes work longer, building on base, and makes it the
current scenario. Its files start out empty. Then sim scheduled edit
changes Bill's salary to end in December 2032. The salary rule comes from
base, so the edit doesn't touch base: it writes a copy of the row into work
longer, with the new end date. That one row is the whole scenario.

> Production note: the cut keeps the row's name and end columns; the file
> has every scheduled column.

## Scene `claim-early`: A second what-if

```tape
Set FontSize 24
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/claim-early && cd ~/retirement/data/sim && clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim scenario create claim-early base > /dev/null"
Sleep 400ms
Enter
Wait
Sleep 1s
Type "sim scheduled edit ss_bill --claim-age 67"
Sleep 400ms
Enter
Wait
Sleep 7s
```

Claim early works the same way. Bill's Social Security rule comes from
base, with a claim age of 70; this scenario's copy says 67. Claiming
sooner means smaller checks, but three more years of them.

## Scene `mix-in`: Mixing two together

```tape
Set FontSize 22
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/mix-in && cd ~/retirement/data/sim && clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim scenario create both work-longer claim-early > /dev/null"
Sleep 400ms
Enter
Wait
Sleep 1s
Type "sim scenario list -v"
Sleep 400ms
Enter
Wait
Sleep 10s
```

To try both changes together, Alice doesn't copy anything. Both names work
longer as its parent and claim early as a mix-in, which adds its own
changes on top. Sim scenario list dash v shows each scenario's parents,
and the values in brackets are inherited, here the start, the end and the
seed, all from base.

## Scene `run-all`: Running them all

```tape
Set FontSize 22
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/run-all && rm ../state/runs/*_work-longer_* ../state/runs/*_claim-early_* ../state/runs/*_both_* && cd ~/retirement/data/sim && clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim all"
Sleep 400ms
Enter
Wait
Sleep 6s
```

Each what-if needs a run before it can be compared. Sim all runs every
scenario in the data repo, skipping any whose last run is still up to
date, and says how each one went.

## Scene `compare-graph`: Who comes out ahead

```slide
template = "figure"
heading = "sim compare base work-longer claim-early --graph"
demo_image = "home/compare.png"
```

Sim compare names a baseline, here base, and the what-ifs to set beside
it. With dash dash graph, it draws net worth after tax for each one, and
below, each what-if's difference from base. Working longer pulls ahead in
2031 and stays about a hundred and fifty thousand dollars ahead. Claiming
early pulls ahead too, but its lead shrinks every year after Bill turns
70: base's bigger checks are catching up.

## Scene `compare`: What differs, and by how much

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
Type "sim compare base work-longer claim-early both --section differences"
Sleep 400ms
Enter
Wait
Sleep 8s
Type "clear && sim compare base work-longer claim-early both --section net-worth"
Sleep 400ms
Enter
Wait
Sleep 14s
```

Without the graph, sim compare starts with what differs. Here that's just
the two rows the what-ifs changed: the salary's end, and the claim age.
If a row you meant to change isn't listed, the change didn't take. Then
net worth at each year end, with each what-if's difference from base.

## Scene `after-tax-dip`: Why working longer starts behind

```slide
template = "card"
heading = "Why working longer starts behind"
body = [
  "After tax, net worth takes off the IRA's future tax",
  "That tax depends on the years the money is spent",
  "Base can spend it in low-tax years, 2031 to 2033",
  "Working longer fills those years with salary",
  "A spouse's death does it too: single filing",
]
speaker = "bill"
```

Look at the first few years: before Bill's extra paychecks even start,
working longer is a little behind. That's real, and it's about tax.
Net worth here is after tax, so it takes off the tax the IRA still owes
when it's spent, and that depends on the years it's spent in. In base,
there are a few quiet years after the paychecks stop and before Social
Security and the required withdrawals start, when IRA money is taxed
lightly. Working longer fills those years with salary, and claiming early
fills them with Social Security, so the same IRA is worth a little less.
The death of a spouse does the same thing, since the survivor files
single.

## Scene `break-even`: When it pulls ahead for good

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
Type "sim compare base work-longer claim-early both --section break-even"
Sleep 400ms
Enter
Wait
Sleep 12s
```

Break even is the month after which a what-if stays ahead for good. Work
longer gets there in February 2031, after a deepest gap of about twenty
five thousand dollars, and ends 2060 about a hundred and sixty thousand
ahead. Claim early ends just forty thousand ahead. And both together come
out slightly behind working longer alone: claiming early adds little once
Bill is working anyway.

## Scene `account`: One account, side by side

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
Type "sim compare base work-longer --account Asset_Brokerage_Joint --lot bonds --section account | head -22"
Sleep 400ms
Enter
Wait
Sleep 12s
```

Dash dash account compares one account's balance, and dash dash lot one
lot of it. The refill chain sells the brokerage bonds once savings run
out. In base, that starts in 2033, and by 2035 most of the bonds are gone.
With two more years of salary, they're never touched.

## Scene `nb-compare`: The comparison notebook

```notebook
Set DataDir "home/retirement/data/sim"
Hide
Open "scenario_compare.ipynb"
RunAll
Scroll "Baseline:"
Show
Sleep 6s
Scroll "Net worth:"
Sleep 8s
Drag "Years:" 2026 2036
Sleep 7s
```

The scenario comparison notebook does the same, as charts. Pick a
baseline, and up to four what-ifs; here, it picked base and the other
three. Below are the rows that differ, then net worth for each, with the
differences under it. The years slider zooms in: here, the first ten
years, where the lines cross.

## Scene `nb-account`: One account, as a chart

```notebook
Set DataDir "home/retirement/data/sim"
Set Zoom 1.25
Hide
Open "scenario_compare.ipynb"
RunAll
Scroll "Account:"
Show
Sleep 3s
Select "Account:" "Asset_Brokerage_Joint"
Sleep 3s
Select "Lot:" "bonds"
Sleep 7s
```

Further down, pick an account to compare its balance: the brokerage
account, then its bonds. Base sells them off from 2033; the what-ifs with
Bill's extra salary keep them.

## Scene `ai`: Asking an AI to compare

```slide
template = "card"
heading = "Asking an AI to compare"
body = [
  "\"Compare work-longer with base: when does it pull ahead, and why?\"",
  "\"Why is claim-early behind base before 2031?\"",
  "Name both scenarios, and the account or year",
]
speaker = "alice"
```

An AI assistant can compare runs too. Name both scenarios, and what you
want to know: when one pulls ahead, or why it's behind in a given year.
It can read the same runs sim compare does, and you can check its answer
against the tables.

## Scene `end`: Next time

```slide
template = "card"
heading = "Next: ensembles"
body = "Not one future, but hundreds: a range of outcomes, and how likely each is."
speaker = "bill"
```

Next time: ensembles. Not one future, but hundreds of them, so the plan
shows a range of outcomes, and how likely each one is.
