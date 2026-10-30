+++
series = "Planning with the simulator"
episode = "Video 1"
title = "What it does, and how it works"
subtitle = "A tour with Alice and Bill"
data = "prototype_data"
+++

The first video of #366 (issue #367): the questions the simulator answers,
then how it gets there: scenarios that build on each other, a monthly run,
the views on a run, and an ensemble of many runs.

`setup.sh` beside this script prepares the demo before any scene records:
it invests the prototype's savings in stocks and bonds, adds a federal
income tax, runs `retire_alice`, and builds and runs the `alice_crisis`
ensemble. So each terminal scene shows its own command without first
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
body = "Can we retire at 62? How long will the money last? What does a Roth conversion buy us? How bad would a bad decade be?"
speaker = "bill"
```

Can we retire at sixty-two, or should we wait? How long will the money last?
What does converting part of an IRA to a Roth actually buy us? And how bad
would it be if the next ten years were a bad decade?

This simulator is built to answer questions like these, for your own
household.

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
every run records exactly which version of your data and of the code
produced it.

## Scene `scenarios`: Scenarios build on each other

```tape
Sleep 500ms
Type "sim scenario list -v"
Sleep 400ms
Enter
Sleep 14s
```

A plan is called a scenario, and most scenarios build on another one. Here,
base holds the general assumptions: long-run inflation and market rates.
Alice's plan, retire alice, builds on base and adds the household: their
accounts, income and spending. A what-if, say Bill retiring a year early,
would build on retire alice and list only what changes. Fix something in
base, and every scenario below it picks up the fix. Alice crisis, at the
top, we'll come back to at the end.

> Production note: "retire alice" and "alice crisis" are spelled with a
> space so the voice doesn't say "underscore".

## Scene `run`: Run it

```tape
Sleep 500ms
Type "sim run"
Sleep 400ms
Enter
Sleep 10s
```

To see where a plan leads, you run it. The simulator steps forward one month
at a time, from the start date to the end, and writes everything that
happened to a run file: every balance, every transfer, every year's tax. A
run is deterministic: the same inputs always give the same result.

## Scene `explain`: A quick first look

```tape
Sleep 500ms
Type "sim explain"
Sleep 400ms
Enter
Sleep 18s
```

Sim explain gives a quick first look at the latest run: whether it finished,
what's left in each account at the end, and net worth each year. Then come
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
Sleep 16s
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
Sleep 500ms
Type "sim ensemble summary alice_crisis"
Sleep 400ms
Enter
Sleep 24s
```

One run is one possible future, with average markets and average inflation.
The real future won't be average. An ensemble runs the same plan many
times, each with its own market returns and inflation, drawn at random.
Alice crisis is retire alice run through eighty futures, each with a crash
like 2008's, landing some time between 2028 and 2034, right around their
retirements. The summary shows the spread, year by year: the median, the
percentiles, and the average of the worst five percent. By 2040, the worst
five percent of these futures leave them a little over half of what the middle one
does. That's how you answer: how bad could a bad decade be?

## Scene `end`: Next time

```slide
template = "card"
heading = "Next: setup and install"
body = "Getting the simulator running on your own computer, and making your first data folder."
speaker = "alice"
```

Next time: getting the simulator running on your own computer, and making
your first data folder.
