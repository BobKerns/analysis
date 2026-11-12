+++
series = "Planning with the simulator"
episode = "Video 11a"
title = "Importing your data"
subtitle = "Balances and spending, from the files you already have"
data = "prototype_data"

[youtube]
title = "Importing your data"
summary = """
Alice and Bill keep their accounts in Quicken, so they stop typing numbers \
into the plan. They export two reports, tell the simulator once which of \
their accounts and categories each outside name means, and from then on an \
import is one command. Guide: \
https://github.com/BobKerns/analysis/blob/main/docs/guides/importing-data.md"""
contents = [
  "Match each name once",
  "Exporting the two reports",
  "Which layout a file is in (sim import sources)",
  "Balances, and the questions asked once (sim import holdings)",
  "Running it again, and --dry-run",
  "Gathering the categories (imports/quicken/categories.csv)",
  "Spending becomes expense rules (sim import transactions)",
  "What is saved, and other sources",
]
tags = ["Quicken", "import"]
# A side trip from video 11, for viewers with files to import: it leads on
# to video 12, and video 11 goes there without it.
previous = "11-update"
next = "12-strategy-plugins"
+++

Video 11a (issue #590, part of #366): importing balances and spending with
`sim import` (#527). A side trip from video 11, as 2a is from video 2: a
viewer who types a handful of balances once a year never needs it.

`setup.sh` beside this script builds video 3's base, writes the two Quicken
reports Alice exported into `~/Downloads`, and the category file she wrote by
hand, then does what the scenes do, tagging the data repo before each
(`stage/<scene>`). The questions are only asked at a terminal, so setup
writes Alice's answers as the files the import would save. The tapes work
in `~/retirement/data/sim`, as in videos 3 to 11.

Quicken is the one source there is today. When brokerage and bank files
arrive (#527's next phase), the `holdings` scenes change to show one, and
the `others` card loses its "coming" line.

> Numbers in the narration were checked on 2026-11-12 against `setup.sh`.

## Scene `title`: Title card

```slide
template = "title"
```

Planning with the simulator. Video eleven A: importing your data.

Alice and Bill already keep their accounts in Quicken. So why type the
numbers into the plan a second time?

## Scene `contents`: From the files you already have

```slide
template = "card"
heading = "From the files you already have"
body = [
  "Balances: what each account holds",
  "Spending: a year of it, as expense rules",
  "Each outside name is matched once, and remembered",
  "Today from Quicken; other sources work the same way",
]
speaker = "alice"
```

This video is a side trip from video eleven, for anyone with more than a
few accounts. An import brings in two things: balances, what each account
holds, and spending, a year of it turned into expense rules. Today it
reads Quicken's reports. Other sources are on the way, and they'll work the
same way.

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

## Scene `export`: Exporting the two reports

```slide
template = "window"
heading = "Two reports, exported as CSV"
title = "File Explorer"
look = "files"
address = "Downloads"
lines = ["net-worth.csv", "transactions-2025.csv"]
note = "In Quicken: a net worth report as of the plan's first day, and last year's transactions"
```

Alice exports two reports from Quicken, as CSV files. One is a net worth
report, as of the day the plan starts: every account, with its balance.
The other is last year's transactions. Both land in her downloads folder.

## Scene `sources`: Which layout a file is in

```tape
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
Sleep 6s
Type "sim import sources ~/Downloads/*.csv"
Sleep 400ms
Enter
Wait
Sleep 9s
```

Sim import sources lists the layouts the simulator can read: today,
Quicken's, for holdings and for transactions. Given files, it says which
one each is in. So Alice knows, before importing anything, that both of
hers will be understood.

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
Sleep 4s
Type "skip"
Sleep 600ms
Enter
Sleep 14s
Type "Asset_TraditionalIRA_Alice"
Sleep 600ms
Enter
Sleep 2s
Type "Asset_Roth_Bill"
Sleep 600ms
Enter
Sleep 3s
Type "skip"
Sleep 600ms
Enter
Sleep 4s
Type "skip"
Sleep 600ms
Enter
Wait
Sleep 30s
```

Sim import holdings reads the net worth report. It hasn't seen any of
these names before, so it asks about each, and lists the plan's accounts
to choose from. Joint Checking is asset checking. Online Savings is asset
savings. The brokerage is different. Quicken's report has one number for
it, and the plan keeps it as two lots, stocks and bonds. One number can't
fill two lots, so Alice answers skip, and keeps typing those two herself.
The two retirement accounts match the plan's. The house and the credit
card aren't in the plan at all, so they're skipped too. Skip is an answer
like any other: it's saved, and the import won't ask again. Then the
import shows each balance before and after. Checking was typed as fifteen
thousand; the report says fourteen thousand eight hundred and twenty. For
the two retirement accounts it also fills in last year's closing balance,
which required withdrawals are figured from. Four lots are updated, and
the answers are saved.

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
Type "cat imports/quicken/accounts.csv"
Sleep 400ms
Enter
Wait
Sleep 13s
Type "sim import holdings ~/Downloads/net-worth.csv --dry-run"
Sleep 400ms
Enter
Wait
Sleep 14s
```

The answers are an ordinary file, one row for each outside account: map,
with the plan's account, or skip. To change an answer, Alice edits its row.
Now she runs the import again, this time with dry run, which shows what
would change and writes nothing. It asks no questions, because every name
has its answer. And nothing would change, because the plan already holds
what the report says. Next year, with a new report, this is the whole job:
one command.

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
Sleep 22s
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
Sleep 12s
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
Sleep 12s
Type "Expense_Living"
Sleep 600ms
Enter
Sleep 2s
Type "Expense_Living"
Sleep 600ms
Enter
Sleep 2s
Type "Expense_Living"
Sleep 600ms
Enter
Sleep 2s
Type "Expense_Healthcare"
Sleep 600ms
Enter
Sleep 2s
Type "Expense_Living"
Sleep 600ms
Enter
Sleep 2s
Type "Expense_Living"
Sleep 600ms
Enter
Wait
Sleep 30s
```

Sim import transactions reads the year, and works out what each category
cost in an average month. From account says where the money comes from:
checking. Then, for each category, it asks which expense account the
spending belongs to. Health goes to healthcare, and the rest to living.
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
  "accounts.csv: each outside account, mapped or skipped",
  "categories.csv: outside categories, gathered into yours",
  "expense_accounts.csv: each category's expense account",
  "Shared by every scenario, and renames follow",
]
speaker = "bill"
```

Everything the import learned is in one folder, imports, beside the
scenarios. Every scenario shares it, so a what-if never asks again. When
an account in the plan is renamed, the saved answers follow. And when a
scenario doesn't have an account that an answer names, the import asks
for that scenario alone, and keeps the saved answer for the others.

## Scene `others`: Other sources

```slide
template = "card"
heading = "Other sources"
body = [
  "Today: Quicken's reports",
  "Coming: brokerage, bank and OFX files",
  "A brokerage file carries each holding, so it can fill every lot",
  "The guide lists each source, and what to export from it",
]
speaker = "alice"
```

Quicken is the first source. Files from brokerages and banks, and OFX
files, are coming. A brokerage file lists each holding, so it can fill the
lots that Quicken's one number couldn't. Whatever the source, the steps
are these: export, answer once, and import. The written guide lists each
source, and what to export from it.

## Scene `end`: Next time

```slide
template = "card"
heading = "Next: strategy plugins"
body = "When no built-in strategy does what you want, write your own."
speaker = "bill"
```

That's the side trip. Next: strategy plugins, video twelve.
