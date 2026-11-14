+++
series = "Planning with the simulator"
episode = "Video 11a"
title = "Importing your data"
subtitle = "Balances and spending, from the files you already have"
data = "prototype_data"

[youtube]
title = "Importing your data"
summary = """
Alice and Bill keep their accounts in Quicken, and their brokerages download \
what each account holds, so they stop typing numbers into the plan. They \
export two reports and two downloads, tell the simulator once which of their \
accounts, lots and categories each outside name means, and from then on an \
import is one command. Guide: \
https://github.com/BobKerns/analysis/blob/main/docs/guides/importing-data.md"""
contents = [
  "Match each name once",
  "Exporting the reports and downloads",
  "Which layout a file is in (sim import sources)",
  "Balances, and the questions asked once (sim import holdings)",
  "A brokerage file, fund by fund: lots by symbol",
  "Which account is this number?",
  "Running it again, and --dry-run",
  "When something changes (--prune, --new-lots)",
  "Gathering the categories (imports/quicken/categories.csv)",
  "Spending becomes expense rules (sim import transactions)",
  "What is saved, and other sources",
]
tags = ["Quicken", "Fidelity", "Vanguard", "brokerage", "import"]
# A side trip from video 11, for viewers with files to import: it leads on
# to video 12, and video 11 goes there without it.
previous = "11-update"
next = "12-strategy-plugins"
+++

Video 11a (issue #590, part of #366): importing balances and spending with
`sim import` (#527). A side trip from video 11, as 2a is from video 2: a
viewer who types a handful of balances once a year never needs it.

`setup.sh` beside this script builds video 3's base (with Alice's IRA in
shares of a made-up fund, and Bill's Roth contributions typed), writes the
two Quicken reports and two brokerage downloads Alice saved into
`~/Downloads`, and the category file she wrote by hand, then does what the
scenes do, tagging the data repo before each
(`stage/<scene>`). The questions are only asked at a terminal, so setup
writes Alice's answers as the files the import would save. The tapes work
in `~/retirement/data/sim`, as in videos 3 to 11.

Quicken's reports fill the cash accounts and Alice's IRA, and bring the
spending; the brokerages' downloads (#527's second phase) fill the joint
account and Bill's Roth fund by fund. The tickers are made up. When bank
transactions and OFX files can be read, the `others` card loses its
"coming" line.

> Numbers in the narration were checked on 2026-11-14 against `setup.sh`.

## Scene `title`: Title card

```slide
template = "title"
```

Planning with the simulator. Video eleven A: importing your data.

Alice and Bill already keep their accounts in Quicken, and their brokerages
will download what each account holds. So why type the numbers into the
plan a second time?

## Scene `contents`: From the files you already have

```slide
template = "card"
heading = "From the files you already have"
body = [
  "Balances: what each account holds, fund by fund",
  "Spending: a year of it, as expense rules",
  "Each outside name is matched once, and remembered",
  "From Quicken, and from brokerage downloads",
]
speaker = "alice"
```

This video is a side trip from video eleven, for anyone with more than a
few accounts. An import brings in two things: balances, what each account
holds, and spending, a year of it turned into expense rules. Alice and Bill
use two kinds of file: Quicken's reports, and the downloads their
brokerages offer, which list each account's funds one by one.

## Scene `idea`: Match each name once

```slide
template = "card"
heading = "Match each name once"
body = [
  "Quicken says: Joint Checking, Groceries",
  "The plan says: Asset_Checking, Expense_Living",
  "The first import asks which is which",
  "The answers are saved, for every scenario",
]
speaker = "bill"
```

The trouble with outside files is names. Quicken calls an account Joint
Checking. The plan calls it asset checking. So the first import asks, for
each outside name, which of the plan's accounts it means. It saves the
answers, and every scenario shares them. After that, an import is one
command.

## Scene `export`: Exporting the reports and downloads

```slide
template = "window"
heading = "Two reports and two downloads"
title = "File Explorer"
look = "files"
address = "Downloads"
lines = [
  "net-worth.csv",
  "transactions-2025.csv",
  "Portfolio_Positions_Jan-02-2026.csv",
  "holdings-88812345.csv",
]
note = "From Quicken: net worth and last year's transactions. From each brokerage: what an account holds"
```

Alice exports two reports from Quicken, as CSV files. One is a net worth
report, as of the day the plan starts: every account, with its balance.
The other is last year's transactions. Then she downloads two more files
from the brokerages' websites, on the first business day of the year: the
joint account's positions, and the holdings of Bill's Roth. All four land
in her downloads folder.

## Scene `sources`: Which layout a file is in

```tape
Set FontSize 28
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/sources && cd ~/retirement/data/sim && sim switch base > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim import sources"
Sleep 400ms
Enter
Wait
Sleep 9s
Type "sim import sources ~/Downloads/*.csv"
Sleep 400ms
Enter
Wait
Sleep 8s
```

Sim import sources lists what the simulator can read: Quicken's two
reports, and the downloads from two brokerages, Fidelity and Vanguard.
Each is a profile, a short description of a file's columns, and a
household can write its own. Given files, it says which one each is in. So
Alice knows, before importing anything, that all four will be understood.

## Scene `holdings`: Balances, and the questions asked once

```tape
Set FontSize 20
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/holdings && cd ~/retirement/data/sim && sim switch base > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim import holdings ~/Downloads/net-worth.csv"
Sleep 400ms
Enter
Sleep 9s
Type "Asset_Checking"
Sleep 600ms
Enter
Sleep 2s
Type "Asset_Savings"
Sleep 600ms
Enter
Sleep 3s
Type "skip"
Sleep 600ms
Enter
Sleep 4s
Type "Asset_TraditionalIRA_Alice"
Sleep 600ms
Enter
Sleep 2s
Type "skip"
Sleep 600ms
Enter
Sleep 3s
Type "skip"
Sleep 600ms
Enter
Sleep 2s
Type "skip"
Sleep 600ms
Enter
Sleep 8s
Type "y"
Sleep 600ms
Enter
Sleep 1s
Wait
Sleep 20s
```

Sim import holdings reads the net worth report. It hasn't seen any of
these names before, so it asks about each, and lists the plan's accounts
to choose from. Joint Checking is asset checking. Online Savings is asset
savings. The brokerage is different. Quicken's report has one number for
it, and the plan keeps it as two lots, stocks and bonds. The brokerage's
own download lists each fund, so Alice answers skip here, and imports that
file next. Alice's IRA matches the plan's. Bill's Roth is skipped, for the
same reason as the brokerage. The house and the credit card aren't in the
plan at all, so they're skipped too. Skip is an answer like any other:
it's saved, and the import won't ask again.

Alice's IRA holds one fund, and the plan counts it in shares. The report
gives only a balance, so at the fund's latest price the import works out
the shares, and offers to update the count, since reinvested dividends
may have added to it. Alice says yes. Then the import shows each balance
before and after. Checking was typed as fifteen thousand; the report says
fourteen thousand eight hundred and twenty. Three lots are updated, and
the answers are saved.

## Scene `positions`: A brokerage file, fund by fund

```tape
Set FontSize 20
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/positions && cd ~/retirement/data/sim && sim switch base > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim import holdings ~/Downloads/Portfolio_Positions_Jan-02-2026.csv"
Sleep 400ms
Enter
Sleep 9s
Type "Asset_Brokerage_Joint"
Sleep 600ms
Enter
Sleep 9s
Type "stocks"
Sleep 600ms
Enter
Sleep 4s
Type "bonds"
Sleep 600ms
Enter
Sleep 4s
Sleep 600ms
Enter
Sleep 1s
Wait
Sleep 20s
```

Next, the joint account's positions. This file says which account each row
is in, by its number, so the import asks once which of the plan's accounts
that is: the joint brokerage. Then each fund goes to the account's lot
with its symbol. The plan's two lots, stocks and bonds, were typed without
symbols, so for each fund the import asks where it goes: a new name adds a
lot, a lot the account has takes it in, or skip. The stock index fund goes
into stocks, and the bond fund into bonds. For the money market, Alice
presses Enter, and gets a new lot named for its symbol. Each lot gets its
balance and, since this is a taxable account, its cost basis from the
file. The plan doesn't know the money market's symbol yet, and the import
says how to add it.

## Scene `roth`: Which account is this number?

```tape
Set FontSize 20
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/roth && cd ~/retirement/data/sim && sim switch base > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim import holdings ~/Downloads/holdings-88812345.csv"
Sleep 400ms
Enter
Sleep 9s
Type "Asset_Roth_Bill"
Sleep 600ms
Enter
Sleep 9s
Type "n"
Sleep 600ms
Enter
Sleep 4s
Sleep 600ms
Enter
Sleep 3s
Sleep 600ms
Enter
Sleep 3s
Sleep 600ms
Enter
Sleep 1s
Wait
Sleep 24s
```

Bill's Roth is at the other brokerage, whose download doesn't say inside
which account it is. Only its name does, with the account's number. So the
first import asks which of the plan's accounts that number is, and shows
the funds the file holds, to help. It's Bill's Roth. The plan keeps the
Roth as one lot, and the file lists three funds. One lot keeps things
simple. A lot for each lets each grow as its own fund does, which matters
to the plan's mix of stocks and bonds, and to an ensemble's range of
outcomes. Bill wants a lot for each, so he answers no, and presses Enter
to name each lot for its symbol. The new lots option would do all of this
without asking. The old lot is emptied, since the new ones hold its total.
And what Bill put into the Roth, its basis, follows the money: it's shared
among the new lots by their balances. A broker's cost basis is what the
funds cost, which is something else, so it's never taken from the file.

## Scene `again`: Running it again

```tape
Set FontSize 24
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/again && cd ~/retirement/data/sim && sim switch base > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "head imports/*/accounts.csv"
Sleep 400ms
Enter
Wait
Sleep 16s
Type "sim import holdings ~/Downloads/net-worth.csv --dry-run"
Sleep 400ms
Enter
Wait
Sleep 10s
```

The answers are ordinary files, in a folder for each source. Quicken's
has one row for each outside account: map, with the plan's account, or
skip. The joint brokerage's has its number, and a row for each fund that
went into a lot of another name. The Roth's is one row: its number. To
change an answer, Alice edits its row. Now she runs the Quicken import
again, this time with dry run, which shows what would change and writes
nothing. It asks no questions, because every name has its answer. And
nothing would change, because the plan already holds what the report
says. Next year, with new files, this is the whole job: one command for
each source.

## Scene `sold`: When something changes

```slide
template = "card"
heading = "When something changes"
body = [
  "A fund sold: its lot is kept, with a warning",
  "--prune empties each such lot: balance, shares and basis",
  "--new-lots adds a lot for each new fund, without asking",
  "A file dated more than a week from the plan's start: a warning",
]
speaker = "bill"
```

A few things for later years. When a fund is sold, the next file won't
list it. The import keeps its lot, and warns that it may have been sold.
Prune empties every such lot, its balance, shares and basis, and sim
account lot delete removes one for good. New lots adds a lot for each new
fund without asking. And the balances are written as the plan's opening
ones, whatever the file's date, so a file dated more than a week from the
plan's start gets a warning.

## Scene `categories`: Gathering the categories

```tape
Set FontSize 24
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/estimates && cd ~/retirement/data/sim && sim switch base > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "cat imports/quicken/categories.csv"
Sleep 400ms
Enter
Wait
Sleep 18s
```

Spending takes one file that Alice writes herself. Quicken has dozens of
categories, and a plan wants a handful. So this file gathers them:
groceries and restaurants become food. Utilities and home repair become
home. Shopping becomes everyday. A category she leaves out goes into one
called other.

## Scene `estimates`: The estimates make way

```tape
Set FontSize 24
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/estimates && cd ~/retirement/data/sim && sim switch base > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim scheduled delete living --yes"
Sleep 400ms
Enter
Wait
Sleep 5s
Type "sim scheduled delete healthcare --yes"
Sleep 400ms
Enter
Wait
Sleep 6s
```

In video three, Alice and Bill typed two estimates: living, at seventy
five hundred dollars a month, and healthcare, at nine hundred. The import
is about to write rules from what they really spent. Both would count, so
the two estimates go first.

## Scene `transactions`: Spending becomes expense rules

```tape
Set FontSize 20
Hide
Type "export HOME=$PWD/home && unset ANALYSIS_DATA_DIR && source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "git -C ~/retirement/data reset -q --hard stage/transactions && cd ~/retirement/data/sim && sim switch base > /dev/null 2>&1; clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim import transactions ~/Downloads/transactions-2025.csv --from-account Asset_Checking"
Sleep 400ms
Enter
Sleep 7s
Type "Expense_Living"
Sleep 200ms
Enter
Sleep 1s
Type "Expense_Living"
Sleep 200ms
Enter
Sleep 1s
Type "Expense_Living"
Sleep 200ms
Enter
Sleep 1s
Type "Expense_Healthcare"
Sleep 200ms
Enter
Sleep 1s
Type "Expense_Living"
Sleep 200ms
Enter
Sleep 1s
Type "Expense_Living"
Sleep 600ms
Enter
Wait
Sleep 22s
```

Sim import transactions reads the year, and works out what each category
cost in an average month. From account says where the money comes from:
checking. Then, for each category, it asks which expense account the
spending belongs to. Alice and Bill keep two, living and healthcare.
Health goes to healthcare, and the rest to living.
It writes one rule for each category, every month, rising with
inflation. Everyday spending is forty three hundred dollars a month, and
food a thousand and fifty. Together they come to about seventy seven
hundred dollars a month, where Alice and Bill had guessed eighty four
hundred. Each rule is marked as generated. So next year's import replaces
these six, and leaves alone every rule they wrote by hand.

## Scene `saved`: What is saved

```slide
template = "card"
heading = "The imports folder"
body = [
  "accounts.csv, one for each source: outside accounts, and funds' lots",
  "categories.csv: outside categories, gathered into yours",
  "expense_accounts.csv: each category's expense account",
  "Shared by every scenario, and renames follow",
]
speaker = "alice"
```

Everything the import learned is in one folder, imports, beside the
scenarios, with a folder inside for each source. Every scenario shares it,
so a what-if never asks again. When an account in the plan is renamed, the
saved answers follow. And when a scenario doesn't have an account that an
answer names, the import asks for that scenario alone, and keeps the saved
answer for the others.

## Scene `others`: Other sources

```slide
template = "card"
heading = "Other sources"
body = [
  "Built in: Quicken, Fidelity and Vanguard",
  "Your own: describe a download's columns in profile.yaml",
  "Coming: bank transactions, and OFX files",
  "The guide lists each source, and what to download",
]
speaker = "bill"
```

Quicken, Fidelity and Vanguard are built in. A brokerage or bank whose
website downloads a plain table needs no code: describe its columns in a
short profile, in that source's folder under imports, and sim import
sources lists it. Bank transactions and OFX files are coming. Whatever the
source, the steps are these: export, answer once, and import. The written
guide lists each source, and what to download from it.

## Scene `end`: Next time

```slide
template = "card"
heading = "Next: strategy plugins"
body = "When no built-in strategy does what you want, write your own."
speaker = "bill"
```

That's the side trip. Next: strategy plugins, video twelve.
