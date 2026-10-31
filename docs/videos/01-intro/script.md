+++
series = "Planning with the simulator"
episode = "Video 1"
title = "What it does, and how it works"
subtitle = "A tour with Alice and Bill"
data = "prototype_data"

[youtube]
summary = """
A tour of the retirement simulator with Alice and Bill: the questions it \
helps answer, how it models a household month by month, and how one plan \
becomes many possible futures."""
contents = [
  "The questions it helps answer: when to retire, how much to spend, which investments, when to sell the house",
  "A month-by-month model of the household, with each year's taxes",
  "Your data stays yours: plain text files on your own computer, with their history",
  "Scenarios that build on each other",
  "Running a plan, and a first look at the result (sim run, sim explain)",
  "Net worth, after tax (sim networth)",
  "Reports, comparisons and notebooks",
  "Ensembles: the same plan through many futures (sim ensemble summary)",
]
+++

The first video of #366 (issue #367): the questions the simulator answers,
then how it gets there: scenarios that build on each other, a monthly run,
the views on a run, and an ensemble of many runs.

`setup.sh` beside this script prepares the demo before any scene records:
it invests the prototype's savings in stocks and bonds, adds a federal
income tax, runs `retire_alice`, creates the `bill_early` what-if, and
builds and runs the `alice_crisis` ensemble. So each terminal scene shows its own command without first
recording the ones before it.

Notebooks are left to video 4, until the pipeline can record them.

## Scene `title`: Title card

```slide
template = "title"
```

Planning with the simulator. Video one: what it does, and how it works.

Meet Alice and Bill. Alice plans to retire in 2028, and Bill two years
later. Like most people at that point, they have savings in a few different
kinds of accounts, and a lot of questions.

## Scene `questions`: The questions it helps answer

```slide
template = "card"
heading = "Questions it helps answer"
body = [
  "When can we retire, and how much can we spend?",
  "Which mix of investments suits us best?",
  "Sell the house now, or in five years?",
  "What does a Roth conversion buy us?",
  "How much room does the plan have?",
]
speaker = "bill"
```

When can we retire, and how much can we spend once we do? Which mix of
investments suits us best? Should we sell the house now, or in five years,
and buy something smaller? What does converting part of an IRA to a Roth
actually buy us? And how much room does the plan have if markets disappoint?

This simulator is built to answer questions like these, for your own
household, by playing each choice forward and putting them side by side.

## Scene `model`: A model of the household

```slide
template = "card"
heading = "A model of your household"
body = "Every account, month by month: income in, spending out, money moved by rules you write, and each year's taxes."
```

It isn't a calculator with one growth rate. It models your accounts month by
month: paychecks and Social Security coming in, spending going out, and money
moving between accounts by rules you write. It works out the taxes each
year, including required minimum distributions and Medicare's income
surcharge, so the answer is what you could actually spend.

## Scene `data`: Your data stays yours

```slide
template = "card"
heading = "Your data stays yours"
body = "Plain text files in a folder of your own, under version control. Nothing is uploaded."
speaker = "alice"
```

Your numbers live in plain text files, in a folder of your own, kept under
version control. The code is separate. Nothing is uploaded anywhere, and
every run {records|/ɹᵻkˈɔːɹdz/} exactly which version of your data and of the code
produced it.

## Scene `scenarios`: Scenarios build on each other

```tape
Set FontSize 23
Sleep 500ms
Type "sim scenario list -v"
Sleep 400ms
Enter
Wait
Sleep 13s
```

A plan is called a scenario, and most scenarios build on another one. Here,
base holds the general assumptions: long-run inflation and market rates.
Alice's plan, retire alice, builds on base and adds the household: their
accounts, income and spending. Bill early is a what-if: Bill retiring a
year early. It builds on retire alice, and all it holds is the one change,
his salary ending a year sooner. Fix something in base, and every scenario
below it picks up the fix. Alice crisis, at the top, we'll come back to at
the end.

> Production note: this tape and the explain and ensemble tapes use a
> smaller font, so their wide tables fit.
>
> Production note: "retire alice", "bill early" and "alice crisis" are
> spelled with a space so the voice doesn't say "underscore".

## Scene `run`: Run it

```tape
Sleep 500ms
Type "sim run"
Sleep 400ms
Enter
Wait
Sleep 9s
```

To see where a plan leads, you run it. The simulator steps forward one month
at a time, from the start date to the end, and writes everything that
happened to a run file: every balance, every transfer, every year's tax. A
run is deterministic: the same inputs always give the same result.

## Scene `explain`: A quick first look

```tape
Set FontSize 23
Sleep 500ms
Type "sim explain"
Sleep 400ms
Enter
Wait
Sleep 17s
```

Sim explain gives a quick first look at the latest run: what's left in each
account at the end, and net worth each year. Then come
the warnings. If an account would have gone negative, it says when. A
shortfall doesn't stop the run; it's recorded, so you can see it and fix the
plan. And these lines show the years when spending crossed guard rails
Alice and Bill set for themselves.

## Scene `networth`: Net worth, after tax

```tape
Sleep 500ms
Type "sim networth"
Sleep 400ms
Enter
Wait
Sleep 15s
```

Sim networth shows net worth by year, in today's dollars, so 2040 compares
fairly with 2026. It's also after tax. A traditional IRA still owes income
tax when it's spent, and so do gains in a brokerage account. That deferred
tax comes off, along with tax for last year that isn't paid until April.

## Scene `views`: Many views of one run

```slide
template = "card"
heading = "Many views of one run"
body = "Reports for income, expenses and tax. Scenarios side by side. Notebooks with charts, no code needed."
```

Every report reads the same run file. There are reports for income,
expenses and tax, a side by side comparison of two scenarios, and Jupyter
notebooks with charts you can explore, without writing any code. Later
videos go through them one at a time.

## Scene `ensemble`: Many futures

```tape
Set FontSize 23
Sleep 500ms
Type "sim ensemble summary alice_crisis"
Sleep 400ms
Enter
Wait
Sleep 23s
```

One run is one possible future, with average markets and average inflation.
The real future won't be average. An ensemble runs the same plan many
times, each with its own market returns and inflation, drawn at random.
Alice crisis is retire alice run through eighty futures, each with a crash
like 2008's, landing some time between 2028 and 2034, right around their
retirements. The summary shows the spread, year by year: the median, the
percentiles, and the average of the worst five percent. By 2040, the worst
five percent of these futures leave them a little over half of what the middle one
does. That's how much room the plan has, and the ensemble shows it for good
futures as well as bad.

## Scene `end`: Next time

```slide
template = "card"
heading = "Next: setup and install"
body = "Getting the simulator running on your own computer, and making your data folder."
speaker = "alice"
```

Next time: getting the simulator running on your own computer, and making
your data folder.
