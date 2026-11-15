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
| A budget in Quicken | Export it, then [a budget](#a-budget-sim-import-budget) below |
| Vanguard or Fidelity | Download each account's holdings, then [brokerage files](#brokerage-files) below |
| Another brokerage or bank whose website downloads a table of balances | Describe its columns in a [profile](#a-source-of-your-own), then import as above |
| A bank's or a card's transactions download | [Bank and card files](#bank-and-card-files) below; a layout not built in needs a [profile](#a-source-of-your-own) |
| An HSA custodian with no usable download | Type the balances when `sim scenario roll` asks ([user guide](../user-guide.md), §4.21) |
| An OFX or QFX download | Not read yet ([#618](https://github.com/BobKerns/analysis/issues/618)) |
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

**What an import writes is a first draft.** A file says what was paid or
budgeted, not what it meant. The import reads it by the odds: a category
budgeted in two months is taken as a yearly bill, a payee paid once in a
year as one that may come round, a paycheck as take-home pay. Some of those
readings will be wrong for your household, and no file can settle them: a
new monthly payment and a one-time purchase look the same at first. So
read what was written (`sim scheduled list`), and change what doesn't fit
with `sim scheduled edit`, or by correcting a row in `imports/` and running
the import again.

When a saved answer names an account the scenario doesn't have, it may still
be right for another scenario. The import asks again, uses the new answer for
that run, and replaces the saved one only if you say so.

To see the saved answers, or change one, use `sim import map`:

```sh
sim import map              # what every source shares, then each source's own
sim import map quicken      # one source's
sim import map --list       # print only
```

Every row has a number. At a terminal it asks for a row's number, then
whether to change it (each thing the row says is asked for, with TAB
completing the scenario's names and what it says now as the default) or
remove it. A removed row is asked about again by the next import that needs
it, which is also how a row is added: an import asks for what has no row.
You can still edit the files by hand. `sim account rename` and
`sim account lot rename` follow into these files by themselves.

```text
imports/
  expense_accounts.csv     category -> expense account ('-' for no rule)
  budget_choices.csv       category -> budget or actual, where you have both
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
- An outside account the scenario has no account for can be added from
  the question: answer with a new name, and it asks what `sim account add`
  asks (kind, subkind, owner) and adds the account before going on. A dry
  run adds nothing, so it offers only the accounts there are.
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

A bank's or a card's download of transactions is described the same way,
under `transactions`:

```yaml
description: Credit union card activity
transactions:
  layout: 'Card: Download transactions'
  columns:
    date: Posted Date
    payee: Description
    amount: Amount
  spending: positive
```

`date` and `payee` are required, and the amount is either one `amount`
column or a `debit` and a `credit` column. `spending: positive` is for a
file whose charges are positive numbers; a bank's are negative, which is the
default. `category`, `account` and `reference` columns are used if named,
and `optional: [category, reference]` lets one profile read both a short
download and a detailed one with more columns.

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

### Look before writing: `--summary`

```sh
sim import transactions transactions.csv --summary
```

Shows what the rules would be made from, and writes nothing: the period
covered, each category's total and what it comes to a month (over the
months that had any spending, as the rules are worked out), what was left
out and why (money in, moves between your own accounts, what the scenario
models itself), and the outside categories that fell to `Other`. It needs
no `--from-account` and no expense accounts yet, so it is a good first
command with a new file. It asks nothing: a payee with no category yet is
counted in `Other`, with a note of the most paid, and a file that doesn't
name its account is taken as its own name's.

Give it a year or more of transactions and it also lists the large payments
made in only a few months of each year: an insurance premium, a property
tax paid in two installments, a utility billed every quarter.

```text
paid in only a few months of a year, and spread over every month by the rules:
  Mutual Insurance (Insurance): Mar 2026 900.00, Sep 2026 900.00  (not seen to come round again)
These are candidates: one not seen to come round again may be a yearly bill or a one-time expense, which no rule should repeat. A bill that does come round wants a yearly rule for each month of the year it is paid in, each for that month's amount, and its payee left out of the generated rules (a row in excluded_payees.csv); one you already have rules for wants only its payee left out. --yearly-script FILE writes both as a script to edit and run.
```

The list is of candidates. One year can't tell a yearly premium from a
one-time purchase, so a payment is marked until a later year shows it
again. A candidate is paid in at most four months of a year, no two of
them closer than three months, so a quarterly bill is one and a bill paid
every other month is not. A monthly bill that began or ended in the period
is not listed, nor is a season's.

A monthly rule gets the year's total right and the months wrong. To pay
such a bill when it falls, it wants a yearly rule for each month of the
year it is paid in (two rules for the premium above, 900 in March and 900
in September; four for a quarterly bill, each for its own quarter's
amount), and its payee left out of the generated rules. Have both written
for you as a script:

```sh
sim import transactions transactions.csv --yearly-script yearly-rules.sh --from-account Asset_Checking
```

`--yearly-script` implies `--summary`: it shows the same summary, writes
no rules, and puts the candidates in the file as a shell script.

```sh
# Mutual Insurance (Insurance): Mar 2026 900.00, Sep 2026 900.00
# Not seen to come round again: a bill, or a one-time expense? Choose, and uncomment:
#   every line, for a bill that comes round;
#   only `leave_out`, for a one-time expense or a bill you already have rules for;
#   none, to keep it spread over the months.
# yearly yearly_mutual_insurance_mar Expense_Insurance 900.00 3 2026-03 'Yearly: Mutual Insurance (Insurance)'
# yearly yearly_mutual_insurance_sep Expense_Insurance 900.00 9 2026-09 'Yearly: Mutual Insurance (Insurance)'
# leave_out quicken '^\s*(?:\S*\d\S*\s+)*mutual\s+(?:\S*\d\S*\s+)*insurance(?:\s+\S*\d\S*)*\s*$'
```

**Edit it before you run it.** The script is a best-effort reading, and
each payee in it is a choice that only you can make:

- **A bill that comes round** wants all of its lines: the `yearly` rules
  and the `leave_out` row.
- **A one-time expense** (a repair, a trip), **or a bill you already have
  rules for**, wants only its `leave_out` line. No rule should repeat the
  first, and the second would be counted twice.
- **To keep it spread over the months** as it is, it wants none of them.

A payee seen again about a year later has its lines ready to run, since
the odds favor a bill. Any other has them commented out, as above, and
does nothing until you remove the `#` from the lines you choose. With only
one year of transactions that is every payee. The command says how many
are waiting for a choice.

Then:

- Correct an amount, or a month, that will be different next time. Each
  amount is as it was paid, and grows with inflation from the month it was
  paid in. Where a bill was seen over several years, the latest year's
  payments are used.
- Fill in each `EDIT_ME`: the account that pays, when `--from-account` was
  left out, and the expense account of a category that has none in this
  scenario (neither a row in `imports/expense_accounts.csv` that names one
  of its accounts, nor an account of its own name).

Two kinds of candidate are noted in the script and not written as rules:

- A category you gave no rule (`-` in `expense_accounts.csv`): nothing
  spreads it over the months, so there is nothing for a yearly rule to
  replace.
- A payee that is also paid in other categories (a store paid weekly for
  food, and twice for something large). Its lines are there, commented
  out: leaving the payee out would drop its other payments from the
  monthly rules too.

The script is yours once written. Running the command again refuses to
replace it (at a terminal, it asks) unless `--overwrite` is given.

Then run it once with `sh yearly-rules.sh`. Each `yearly` line is a
`sim scheduled add`, and each `leave_out` line adds a row to the
`excluded_payees.csv` of the source the payments came from, so that the
next `sim import transactions` no longer spreads that payee over the
months. The row finds that payee and no other: its words in order,
whatever dates and reference numbers come before, between and after them,
and no other word. Should the script stop partway (an `EDIT_ME` left in,
say), correct it and run it again: it skips what it has already added.
Run the import again afterwards. The yearly rules are ordinary rules of yours from then on:
an import never replaces them, and `sim scheduled edit` changes them.

With less than a year of transactions, first day to last, a yearly bill
can't be told from a monthly one that just began, so nothing is listed.

### Bank and card files

A bank's download of an account's activity has a date, an amount and the
bank's text for who was paid, and no category. `sim import transactions`
reads one as it reads any other transactions file, and finds each payment's
category by its payee. Wells Fargo's *Download Account Activity* (comma
delimited) is built in (`wells_fargo`); another bank's needs a
[profile](#a-source-of-your-own).

```sh
sim import transactions ~/Downloads/Checking1.csv --from-account Asset_Checking --dry-run
```

At a terminal it asks about each payee it has no category for, the most paid
first, so the first few answers cover most of the money:

```text
wells_fargo: 'PURCHASE AUTHORIZED ON 03/14 CORNER GROCERY S123456 CARD 9876', 41 payment(s), 6150.00 in all, has no category (212 payee(s) to go).
category: Food
  the words of the payee that say so [purchase authorized on corner grocery card]: corner grocery
```

The answer is a category of yours, new or one in use, or one of three words:

- `transfer`: a move between your own accounts, or a card's payment. It
  isn't spending, and makes no rule.
- `-`: leave these payments out.
- `rest`: stop asking, and put every payee not yet asked about into `Other`
  for this run. Nothing is saved for them, so the next run asks again.

The second question is which words of the bank's text name the payee. Words
with a digit in them (dates, store and card numbers) are already gone; trim
the rest to the fewest that are still this payee's alone, and its other
spellings are covered too. The answers are saved in
`imports/<source>/payees.csv`, which can be edited by hand as well:

```csv
payee_pattern,category
corner grocery,Food
city water,Home
card payment,transfer
```

A pattern matches whole words, together and in their order, so `art` is
not found in `walmart`. The first row found in a payee wins, so put the
longer pattern above a shorter one it contains.

**Several files, several sources.** Each run replaces all the generated
rules, so give it every file the rules should come from: each account's
download, each card's, a Quicken export. Each file's layout is recognized by
itself, and the rules are averaged over all of them together:

```sh
sim import transactions Checking1.csv Card.csv --from-account Asset_Checking
```

A card's purchases come from the card's own file, so mark its payment
`transfer` in the bank's `payees.csv`, or the money is counted twice.

A bank names its downloads alike whatever the account, so at a terminal the
import asks which account each such file is, offering the file's name. Away
from a terminal, name them with `--outside-account`, once for each such
file in the files' order; a single file may go without, as the account of
its own name.

**A card's file.** American Express's CSV download is built in (`amex`),
with or without its additional details; the detailed one has the card's
category and a reference for each charge. Describe another card's in a
profile, with `spending: positive` if its charges are positive numbers; see
[A source of your own](#a-source-of-your-own). If the card gives its own
category for each charge, map those in `imports/<source>/categories.csv` as
for any other source. Where the card's category isn't yours, a row of
`imports/<source>/payees.csv` for that payee wins over it. If the file has a
reference for each transaction, name that column too (`reference`):
downloads that all have it may then overlap, and a transaction in two of
them is counted once. A download without references can't be matched that
way, so it mustn't cover the same days as another.

## A budget: `sim import budget`

```sh
sim import budget budget-full.csv --from-account Asset_Checking --dry-run
sim import budget budget-full.csv --from-account Asset_Checking
```

Writes the same expense rules from what you plan to spend instead of what
you spent. Transactions miss plans not yet carried out, and a budget may be
out of date, so which to use is yours to choose: each import replaces all
the generated rules, whichever wrote them. To choose for each category,
give the budget to the transactions import
([below](#budget-or-actual-for-each-category)).

Quicken exports a budget in two forms. Use **Full**, which has each month:

- A category budgeted in most months becomes one monthly rule, at its
  average month.
- A category budgeted in only one or two months of the year (property tax,
  an insurance premium) becomes a yearly rule in each of those months.

The **Summary** form has only the year's totals, so everything is spread
over twelve months.

A budget's categories are already yours, so no category file is needed:
everything under a top-level category is added into it. To divide one, or
leave one out, add rows to `imports/quicken/categories.csv`. A row names a
category by its whole path, and covers what is under it.

```csv
outside_category,category
Food:Restaurants,Dining
Gifts,-
```

The import asks which expense account each category belongs to. Type a new
name there and it offers to add the account. A mortgage category is left
out, since the scenario models the loan, and so are the budget's transfer
rows. Income tax categories (`Tax:Fed`, `Tax:State`) are left out as well:
the scenario works out and pays that tax itself, so a rule would pay it
twice. A row for such a category's own path keeps it. Property tax is
spending and stays.

**Income.** If the budget has income, the import says so and asks whether
to write income rules from it. A plan usually models its own income (a
salary that ends at retirement, Social Security by claim age), so the
default is no. To stop being asked, copy the budget in Quicken, exclude its
income, and export the copy. Away from a terminal income is left out unless
`--income` is given.

A budget's paycheck is usually take-home pay, with its tax already
withheld, so income rules are written untaxed. If your budget has pay
before tax, say so when asked, or with `--gross-income`, and the scenario
taxes it. Income goes into `--from-account` unless `--income-to` names
another account, which it must when the spending comes from a chain.

Treat a budget's income as a convenient start, not the finished model. It
has no withholding and no 401(k) contribution for the plan's taxes to see.
For those to come out right, set the income up from a pay stub: the gross
pay as the income rule, with a rule for each thing taken out of it (the
401(k) contribution, other deductions), and leave the income tax to the
scenario's tax rules. `sim scheduled add` writes each rule, and
`--no-income` keeps the budget's income out once you have.

The scenario does not work out payroll tax (Social Security and Medicare).
Enter the pay stub's amounts for them as an expense rule of their own, taken
from the gross pay with the other deductions. A business's profit is another
matter: the federal return works out its self-employment tax
([#623](https://github.com/BobKerns/analysis/issues/623)), since it depends on
a profit the run works out. It counts each spouse's gross wages (`ordinary`
income out of their own `salary` account) toward their Social Security wage
base, so gross pay matters there too.

Amounts are taken as budgeted. A budget more than a year older than the
scenario's start is warned about, since its dollars are old ones.

### Budget or actual, for each category

```sh
sim import transactions transactions.csv --budget budget-full.csv --from-account Asset_Checking
```

Reads both, gathers each into the same categories, and shows a month of
each side by side:

```text
a month of each category, budgeted and as the transactions have it:
  category      budget  actual  difference                    use
  Food          710.00  420.00     -290.00                      ?
  Gifts                  30.00              actual (the only one)
  Property Tax  500.00                      budget (the only one)
  Utilities     165.00                      budget (the only one)
Food: budget 710.00 or actual 420.00 a month? (budget, actual, all-budget, all-actual):
```

- A category both have is asked about. `all-budget` or `all-actual`
  answers for every one still to be asked.
- A category only one has is made from that one.
- A category made from the budget keeps the budget's yearly payments (the
  property tax above is two yearly rules, not 500 a month).

Your answers are saved in `imports/budget_choices.csv`, with the two
amounts each was chosen between. The next run uses them and asks nothing,
until one of a category's amounts has moved by more than 5%: then it asks
again, offering what you chose before. Edit the file, or delete a row, to
change your mind.

Away from a terminal the saved choices are used. A category with none
needs `--prefer budget` or `--prefer actual`, which is also how to skip
the questions at a terminal; a preference is not saved. With `--summary`
the table is shown and nothing is asked or written.

Neither side is the truth: a budget is a plan and the transactions are one
period's history. The choice is a starting point to review, like the rules
it makes. The budget's income is not used here; `sim import budget
--income` writes income rules.

## Quicken

Save two reports under *My Reports*, and export each as CSV:

| Report | For | As of |
| --- | --- | --- |
| Net worth (every account, with its balance) | `sim import holdings` | the scenario's first day; or December 31 for `--year-end` |
| Transactions (every account, itemized, with payee and category) | `sim import transactions` | the year to average |
| A budget's export, Full | `sim import budget` | the year planned |

With no file named, `sim import transactions` reads
`quicken/Transaction Export.csv` at the data repo's top level, along with the
Amazon categorizer's monthly summaries in `state/`, which split Amazon
orders into real categories.

Quicken's net worth report has one number for a brokerage account, so it
can't fill an account kept as several lots: import those from the
[brokerage's own file](#brokerage-files).

A data repo with Quicken mappings from before the `imports/` folder moves
them with `scripts/migrate_527.py` (it reports first; `--write` does it).
