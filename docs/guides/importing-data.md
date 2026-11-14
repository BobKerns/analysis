# Importing your data

Balances and spending you already keep somewhere else don't have to be typed
into a scenario. `sim import` reads a file another program exported and
writes the scenario's own files from it.

**Video.** *Importing your data* (video 11a) walks through this guide with
Alice and Bill and a Quicken export:
[English](https://www.youtube.com/watch?v=KHS9dpUaHFc),
[Japanese (日本語)](https://www.youtube.com/watch?v=Dby3vYmfFPA).

## Which path is yours

| You keep your accounts in | What to do |
| --- | --- |
| Quicken | Export two reports, then [balances](#balances-sim-import-holdings) and [spending](#spending-sim-import-transactions) below |
| Vanguard or Fidelity | Download each account's holdings, then [brokerage files](#brokerage-files) below |
| Another brokerage or bank whose website downloads a table of balances | Describe its columns in a [profile](#a-source-of-your-own), then import as above |
| A bank's transactions download | Not read yet ([#527](https://github.com/BobKerns/analysis/issues/527)) |
| An HSA custodian with no usable download | Type the balances when `sim scenario roll` asks ([user guide](../user-guide.md), §4.21) |
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
    profile.yaml           a source of your own (see below)
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
  filled from a [brokerage file](#brokerage-files) or typed.
- A debt is stored as a positive amount owed, whatever sign the source uses.
- Two outside accounts mapped to one lot are added together.
- A lot the scenario inherits from its parent is changed by writing an
  override into the scenario itself; the parent is left alone.
- For investment accounts, a blank `prior_year_end_balance` (what required
  withdrawals are figured from) is filled with the same number.
  `--year-end` sets that column alone, from a snapshot as of December 31.

### Brokerage files

A brokerage's download lists what each account holds, fund by fund, so it
can fill an account kept as several lots, which one number can't.

```sh
sim import holdings ~/Downloads/Portfolio_Positions_Jan-05-2026.csv --dry-run
sim import holdings ~/Downloads/Portfolio_Positions_Jan-05-2026.csv
```

1. **Each outside account is matched to an account**, once, as above. No
   lot is asked for.
2. **Each holding goes to the account's lot with its symbol.** The lot's
   shares and balance are set, and in a taxable brokerage account its cost
   basis.
3. **A holding the account has no lot of is asked about.** Press Enter for a
   new lot named for the symbol, type another name, name a lot the account
   has to add the holding into it, or `skip` it. `--new-lots` adds a lot for
   each without asking.
4. **An account with one lot and no symbol is asked about first:** add the
   holdings together into that lot, or keep a lot for each? A lot for each
   lets each grow as its own fund does, which matters to the plan's
   allocation and to an ensemble's range of outcomes. One lot is simpler.
   If you split, the old lot is emptied, since the new lots hold its total.
   Rules that name it still do; `sim account lot delete` removes it and
   lists them.

Things to know:

- **A Roth lot's basis is yours to type.** In the plan a Roth lot's basis is
  what was contributed and converted, which comes out untaxed at any age.
  A broker's "cost basis" is what the holdings cost, which is something
  else, so an import never sets it. A Roth lot with no basis counts its
  whole balance as contributions, and the import reminds you:
  `sim account lot edit ACCOUNT LOT --opening-cost-basis AMOUNT`. The
  custodian's Form 5498s add up to it.
- **A new symbol is only a label** until the plan knows it. `sim symbol add`
  gives it a name, its asset classes and its prices; until then its lot
  grows as its account does.
- **A lot the file doesn't mention is kept**, with a warning: it may have
  been sold. `--prune` empties every such lot (balance, shares and basis to
  zero), and `sim account lot delete` removes one for good. When two
  outside accounts go into one of the plan's accounts, import their files
  together: a file with only one of them leaves the account's other lots
  alone.
- **A Roth account's basis follows its money.** When a Roth lot is emptied
  (split into a lot for each fund, or by `--prune`), what was contributed
  to it is shared among the account's Roth lots that hold a balance, by
  their balances, so the account's contributions add up as before.
- **A retirement account with a required-withdrawal rule** draws from one
  lot. Before it has several, its rule needs a chain naming them in order;
  the import is refused, with that message, until it has one.
- **Roll first, then import.** At a new year, `sim scenario roll` moves the
  plan forward on its own projections, and an import then corrects the
  balances from the real files.
- **The file's date should be the scenario's first day**, give or take a
  week. The balances are written as the opening ones whatever the date, so
  an import of a file dated further away warns, naming both dates.

| Source | What to download | The account |
| --- | --- | --- |
| `fidelity` | *Positions*, *Download*: one file for every account | a column of the file |
| `vanguard` | *Holdings*, the download of one account's holdings | its number, in the file's name; or `--outside-account NAME` |

A Vanguard file doesn't say inside which account it is. The name it is
downloaded with has the account's number, and the import goes by that: the
first time, it asks which of the plan's accounts the number is, showing what
the file holds, and remembers. A file renamed without the number is asked
about at a terminal each time (any name you like, such as `Bill Roth`), or
named with `--outside-account`.

**Share counts.** A row that gives a value and a price and no share count
has its shares worked out from the two. Where the file has only a balance
for a lot that is counted in shares (a money market's row at some
brokerages, or another source's one number for an account), and the plan
has a price for the lot's symbol from the week up to the file's date, an
import at a terminal offers to set the count to the balance over that price:
the count then follows reinvested dividends. Away from a terminal it says
that it could.

### A source of your own

A download that is a plain table, one row for each holding or account, needs
no code. Describe it in `imports/<source>/profile.yaml`, with whatever name
you like for the source:

```yaml
description: Credit union balances
holdings:
  layout: 'Accounts: Export'
  columns:
    account: Share ID
    asset: Symbol
    value: Balance
```

`columns` says which heading holds what; `asset` and `value` are required,
and `quantity`, `price`, `cost_basis`, `account_name` and a few more are
optional.
The keys for a date, for an account that is only in the file's name, and for
cells that mean "nothing" are in
[ADR-003](../architecture/ADR-003-registries-and-csv-schemas.md), §4.17.
`sim import sources` then lists the source, and recognizes its files.

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
can't fill an account kept as several lots: import those from the
[brokerage's own file](#brokerage-files).

A data repo with Quicken mappings from before the `imports/` folder moves
them with `scripts/migrate_527.py` (it reports first; `--write` does it).
