# Importing your data

Balances and spending you already keep somewhere else don't have to be typed
into a scenario. `sim import` reads a file another program exported and
writes the scenario's own files from it.

**Video.** *Importing your data* (video 11a) walks through this guide with
Alice and Bill and a Quicken export:
[English](https://www.youtube.com/watch?v=usFLQg8Lvsk),
[Japanese (日本語)](https://www.youtube.com/watch?v=x7PQfaDGhFA).

## Which path is yours

| You keep your accounts in | What to do |
| --- | --- |
| Quicken | Export two reports, then [balances](#balances-sim-import-holdings) and [spending](#spending-sim-import-transactions) below |
| A brokerage's or a bank's website (a positions or transactions download) | Not read yet ([#527](https://github.com/BobKerns/analysis/issues/527)). Until then, type the balances when `sim scenario roll` asks ([user guide](../user-guide.md), §4.21) |
| An OFX or QFX download | Not read yet (#527) |
| Nothing a program exports, or only a few accounts | Type them: `sim scenario roll` asks for each lot's balance, and `sim scheduled add` takes an estimate of spending |

`sim import sources` lists what this version reads. Given files, it says
which layout each is in:

```sh
sim import sources ~/Downloads/*.csv
```

## How every import works

An outside file has its own names for things (an account called *Joint
Checking*, a category called *Groceries*), and a scenario has its own
(`Asset_Checking`, `Expense_Living`). So:

1. **The first import asks.** For each outside name it hasn't seen, it asks
   which of the scenario's names that is, completing with TAB, or `skip` to
   leave it out for good.
2. **The answers are saved** in the data dir's `imports/` folder, beside
   `scenarios/`, and shared by every scenario. A what-if never asks again.
3. **After that an import is one command.** Run from a script, where nothing
   can be asked, an import stops and lists any name it has no answer for: a
   new or renamed outside account is never quietly left out.

`--dry-run` shows what would change, before and after, and writes nothing
(the answers given during a dry run aren't saved either).

When a saved answer names an account the scenario doesn't have, it may still
be right for another scenario. The import asks again, uses the new answer for
that run, and replaces the saved one only if you say so.

To change an answer, edit its row in the file. `sim account rename` and
`sim account lot rename` follow into these files by themselves.

```text
imports/
  expense_accounts.csv     category -> expense account ('-' for no rule)
  <source>/
    accounts.csv           outside account -> account and lot, or skip
    categories.csv         outside category -> category
    splits.csv             a category divided by the outside account that paid
    excluded_payees.csv    payees whose expenses the scenario models itself
```

The columns of each are in
[ADR-003](../architecture/ADR-003-registries-and-csv-schemas.md), §4.17.

## Balances: `sim import holdings`

```sh
sim import holdings net-worth.csv --dry-run
sim import holdings net-worth.csv
```

Sets each mapped lot's `opening_balance` from a snapshot of what the outside
accounts hold. Export the snapshot as of the scenario's first day.

- An account with several lots needs the lot named: the question asks for
  it. One outside number can't fill two lots, so an outside account that
  the scenario keeps as several lots is best answered `skip`, and its lots
  typed.
- A debt is stored as a positive amount owed, whatever sign the source uses.
- Two outside accounts mapped to one lot are added together.
- A lot the scenario inherits from its parent is changed by writing an
  override into the scenario itself; the parent is left alone.
- For investment accounts, a blank `prior_year_end_balance` (what required
  withdrawals are figured from) is filled with the same number.
  `--year-end` sets that column alone, from a snapshot as of December 31.

## Spending: `sim import transactions`

```sh
sim import transactions transactions.csv --from-account Asset_Checking --dry-run
sim import transactions transactions.csv --from-account Asset_Checking
```

Turns a period's transactions into one monthly expense rule per category,
at that category's average month, rising with inflation (`--series`).
`--from-account` is where the money comes from.

One file is written by hand first, `imports/<source>/categories.csv`: it
gathers the outside program's many categories into the few a plan wants.

```csv
outside_category,category
Groceries,Food
Restaurants,Food
Utilities,Home
```

A blank or `-` category leaves an outside category out; one with no row at
all becomes `Other`. The import then asks which expense account each
category belongs to, and saves that in `imports/expense_accounts.csv`.

The rules it writes are tagged `generated:expense_baseline`. A rerun replaces
exactly those and leaves every rule written by hand alone, so:

- **Remove the estimates the rules stand in for** (`sim scheduled delete`),
  or the spending is counted twice.
- In a scenario built on another, a generated rule the parent has, which a
  rerun no longer produces, is listed as `kept`: end it there with
  `sim scheduled delete`.

Two files that cover the same days of one outside account are refused, since
their transactions would be counted twice.

## Quicken

Save two reports under *My Reports*, and export each as CSV:

| Report | For | As of |
| --- | --- | --- |
| Net worth (every account, with its balance) | `sim import holdings` | the scenario's first day; or December 31 for `--year-end` |
| Transactions (every account, itemized, with payee and category) | `sim import transactions` | the year to average |

With no file named, `sim import transactions` reads
`quicken/Transaction Export.csv` at the data repo's top level, along with the
Amazon categorizer's monthly summaries in `state/`, which split Amazon
orders into real categories.

Quicken's net worth report has one number for a brokerage account, so it
can't fill an account kept as several lots.

A data repo with Quicken mappings from before the `imports/` folder moves
them with `scripts/migrate_527.py` (it reports first; `--write` does it).
