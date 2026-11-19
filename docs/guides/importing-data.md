![Zabob Retirement](../assets/brand/banner.jpg)

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
| An OFX or QFX download (what a bank, a card or a brokerage offers for "Quicken" or "Money") | [OFX and QFX files](#ofx-and-qfx-files) below: no profile needed, whatever the institution |
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
remove it. An account the scenario doesn't have is asked about before it is
kept, since it may be a slip or another scenario's account. A removed row
is asked about again by the next import that needs it, which is also how a
row is added: an import asks for what has no row.
If an import or a rename wrote a file while `sim import map` waited at a
question, your change to that file is dropped and the files are listed
again as they are now.
Removing a row moves the numbers of the rows after it, so the whole list
is shown again: read a row's number from the latest listing.
You can still edit the files by hand. `sim account rename` and
`sim account lot rename` follow into these files by themselves.

```text
imports/
  expense_accounts.csv     category -> expense account ('-' for no rule)
  budget_choices.csv       category -> budget or actual, where you have both
  <source>/
    profile.yaml           a source of your own (see below)
    accounts.csv           outside account -> account and lot, or skip, or split
    lot_splits.csv         one outside number divided among an account's lots
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

- **Several accounts at one bank are best kept as lots of one account**:
  the checking and savings accounts you have there, say, as lots `checking`
  and `savings` of one `Asset_Bank` account. Keep one account for each
  owner and each kind of account (cash and retirement money stay apart), and
  one lot for each outside account. The account list stays
  short, every report shows the bank's total, and each lot still has its own
  balance, rate and refill rules. Map each outside account to its lot.
- The question asks for the lot too. Press Enter for an account's one lot,
  name one of its lots, or give a new name: after a yes, the lot is added
  to the account and takes the balance. So a bank's second account can be
  given its own lot as it is first imported.
- When the account's one lot is still called `default`, a new name may be
  that same lot's, so the question says what `default` holds and asks:
  `rename` gives the lot your name, and the rules that pay into or out of it
  follow; `add` keeps `default` and adds your lot beside it, for a file that
  lists only part of the account; `back` asks for the lot again.
- One outside number can fill several lots when you know how it divides:
  answer `split` for the lot. It then asks for a lot and its amount, one at a
  time, until nothing is left. A new name adds a lot; `rest` for an amount
  gives that lot whatever the others leave. In a brokerage account it also
  asks a new lot's symbol and the day it was bought, and each part's cost
  basis in a taxable one. A fund that isn't traded, such as a retirement
  plan's own, can take its CUSIP or a name you make up as its symbol: one
  that doesn't look like a ticker is confirmed first, in case it is a slip,
  and then marked `no_fetch`, so that nothing looks for its prices. The split is saved in
  `imports/<source>/lot_splits.csv`. The next import uses it as it is when
  one lot takes the rest, or when the number hasn't changed. Otherwise it
  asks again, with the last amounts as the defaults.
- An outside account the scenario has no account for can be added from
  the question: answer with a new name, and it asks what `sim account add`
  asks (kind, subkind, owner) and adds the account before going on. For an
  outside account that is one balance it also asks what to call the
  account's lot: Enter for `default`, or a name of your own such as
  `checking`, which saves renaming it when the account gets a second. It
  asks first whether to add it, and takes Enter as no, since a new name
  may be a slip; `back` at the account's kind returns to the question. A dry
  run adds nothing, so it offers only the accounts there are. A name the
  scenario already uses for something that can't take the balance (an
  expense account, a chain, an account with no lot yet) is not offered as
  new: the question says so and asks again.
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
  grows as its account does. For a made-up symbol or a CUSIP, add
  `--never-fetch`, so that nothing looks for its prices.
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
  an import of a file dated further away warns, naming both dates. A bank's
  or a card's [OFX download](#ofx-and-qfx-files) is the exception: its
  balance is worked back to the first day from its own transactions.

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
you like for the source, other than `quicken` and `ofx`: those are sources
already, and a profile in a folder of either name stops every import until
the folder is renamed.

For example:

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
sim import transactions transactions.csv --from Asset_Checking --dry-run
sim import transactions transactions.csv --from Asset_Checking
```

Turns a period's transactions into one monthly expense rule per category,
at that category's average month, rising with inflation (`--series`).
`--from` is where the money comes from: an account, or a chain that
divides it between several. What a credit card paid is charged to the card
instead; see [A card in the plan](#a-card-in-the-plan).

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
no `--from`, no expense accounts and no `categories.csv` yet, so it is a
good first command with a new file: with no map, each of the outside
program's categories is shown as itself, which is what the map is written
from. It asks nothing: a payee with no category yet is
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
sim import transactions transactions.csv --yearly-script yearly-rules.sh --from Asset_Checking
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
- Fill in each `EDIT_ME`: the account that pays, when `--from` was
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
and no other word. The script runs the `sim` that wrote it, whatever your `PATH` is when you
run it. Should the script stop partway (an `EDIT_ME` left in,
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
sim import transactions ~/Downloads/Checking1.csv --from Asset_Checking --dry-run
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
sim import transactions Checking1.csv Card.csv --from Asset_Checking
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
them is counted once, by its reference alone. The import prints a note if a
reference comes back with another day or amount, or a day and amount come
back under another reference, since then the references may not mean what
they should. A download without references can't be matched that
way, so it mustn't cover the same days as another.

### A card in the plan

If the scenario has the card as an account (a liability of subkind
`credit_card`), the import charges the card's spending to it, so the plan
shows the card's balance rise with its use and fall with its payment:

```sh
sim account add Liability_Card liability credit_card --owner joint --opening-balance 1200
sim import transactions Checking1.csv Card.csv --from Asset_Checking
```

The import needs to know which file's account is the card. At a terminal it
asks about each account it doesn't know, until every card has its account.
Answer with the card, or with the account it is when it is no card, and it
isn't asked about again; the answer is saved in
`imports/<source>/accounts.csv`, where `sim import map` shows and changes
it. Then:

- Each category the card paid for gets a rule from the card, named for it
  (`baseline_expense_food__liability_card`), beside the category's rule
  from `--from` for what the bank account paid directly.
- The card gets one rule that pays it, `card_payment_liability_card`: each
  month, from `--from`, **in full**. Its amount is blank, which means what
  the card was owed when the month opened: last month's charges are paid
  this month, as a statement is.
- That payment rule is written once. Later imports leave it alone, and add
  none if you already have a rule that pays the card, directly or through a
  chain. It is yours to change.
- A card charged only by yearly rules is paid too: the yearly script has a
  `pay_card` line for it.

A card that carries a balance wants two changes. Give the payment an amount
(`sim scheduled edit card_payment_liability_card`), and add the card's
interest:

```sh
sim scheduled add 2027-01 Liability_Card Expense_Interest_Card --kind card_interest --rate 24%
```

Each month this charges the card a month's interest on whatever the
payments left unpaid of the balance it opened the month with. A card paid in
full is charged nothing. It is a month's approximation: a real card works
day by day.

A file whose account isn't a card of the scenario is paid from `--from`, as
before. So is Amazon's summary, which doesn't say what paid. With `--budget`,
a category made from the budget is paid by whatever paid most of it in the
transactions; in the yearly script, a bill that was always charged to one
card is paid from that card.

### OFX and QFX files

Most banks, cards and brokerages offer a download "for Quicken" (`.qfx`) or
"for Money" (`.ofx`). They are one format, and it is read whatever the
institution: no profile is needed. A file is recognized by what is in it,
not by its name.

```sh
sim import transactions ~/Downloads/Checking.qfx ~/Downloads/card.ofx --from-account Asset_Checking --dry-run
sim import holdings ~/Downloads/Checking.qfx --dry-run
```

One download feeds both commands:

| In the file | Becomes |
| --- | --- |
| A bank account's or a card's transactions, and a brokerage account's checks, card purchases and bill payments | spending, for `sim import transactions` |
| Each transaction's own id | what lets two downloads overlap: a transaction in both is counted once |
| The institution's name, the account's type and the account's number | the outside account, as `First Example Bank checking ...4321` (`card` or `investment` where it is one): the last four digits only |
| The statement's balance and its date | the account's balance, for `sim import holdings` (what a card owes is a debt) |
| A brokerage account's positions | holdings by symbol, as for [brokerage files](#brokerage-files) |
| A brokerage account's cash that no position holds | a holding named `(cash)`, for the account's cash lot (`(margin)` for what it has borrowed) |

The transactions have no categories, so they are categorized
[by payee](#bank-and-card-files), with one map for every institution's
file: `imports/ofx/payees.csv`. The same payee text means the same thing
whichever bank reports it. A card paid from the bank is `transfer` there,
as for any other bank file.

The balance is what a bank's CSV download never has, so an OFX file is the
easy way to set a checking or savings account's opening balance. The balance
in the file is always the one on the day you download it: the range of dates
a bank asks for only chooses which transactions the file has. So download
with a range from the scenario's first day (or earlier) through today. The
file then has everything posted since the scenario started, and the import
works the balance back to that first day, saying what it did:

```text
note: First Example Bank checking ...4321: its balance of 4321.09 as of 2026-06-15 is worked back to 2026-05-01: what was posted from that day comes to -1200.00, so it opened the day with 5521.09
```

A download that doesn't reach back to the scenario's first day sets the
balance as the file has it, with a warning that the dates differ and a note
of the days the file covers. So does a card whose file writes what is owed
as a positive amount, and a file with a bank's correction of a transaction.
A brokerage's positions are never worked back, since the file can't say
what the shares and prices were on an earlier day. With `--year-end` the
balance is worked back to the end of the year before, from a download that
starts on January 1 or earlier.

The outside account's name is made from what the file says and nothing
else. A file that doesn't name its institution (many banks' don't) gives
its id, or the bank's routing number, in its place. Two accounts at one
institution, of one type, ending in the same four digits can't be told
apart; a file with both is refused, and from separate files they would be
read as one account.

The mappings, and the counting of a transaction once, go by that name, so
it matters that one account keeps one name. It may not: a bank's `.qfx`
and `.ofx` downloads can name the bank differently, and a reissued card
has a new number. Overlapping downloads under two names are counted
twice. The import prints a note when two outside accounts have
transactions with the same id, day and amount; if you see it, import
downloads that don't overlap.

An id is taken at its word: a transaction with an id an earlier download
had is that transaction, and is left out. Some institutions break that
promise. One that numbers each download from the start again loses
transactions this way, and one that gives a transaction a new id every
time has them counted twice. The import can't tell which it is looking at,
so it prints a note when an id comes back with another day or amount, or a
day and amount come back under another id. If you see either, import
downloads that don't overlap from that institution.

The payee is the file's name for it. Some banks write only the kind of
payment there (`POS PURCHASE`) and put the merchant in the memo, so one
`payees.csv` row would cover every purchase. The import prints a note when
one payee has many different memos.

Nothing is converted from another currency. An account or a transaction
that is not in US dollars is read as it is, with a note.

A card's balance is what is owed, and is stored as a debt whichever sign
the issuer writes it with: most write it negative, some positive. A
positive one is reported as an unusual credit balance, since the file
can't say which it is; check it against the statement.

A brokerage's cash is read best effort. The file gives one figure for all
of the account's cash, which includes a sweep or money market fund where
that is also listed as a position. Positions priced at a dollar a share
are taken to be that fund, and only the cash beyond them is added. Money
borrowed on margin is taken off: the margin balance where the file gives
one. Where that guess decided anything, a note says what was added or left
out; check the account's total against the statement. A download that
leaves the positions out can't tell the cash from a fund a lot already
holds, so download the account with its positions.

The cash goes to the account's cash lot (one with the symbol `CASH`), or
to its lot with no symbol. If the account has neither, `--new-lots` adds a
cash lot named `cash`, and at a terminal you are asked. To put it
somewhere else, give it a row of its own in `imports/ofx/accounts.csv`,
with `(cash)` as its `asset`; the positions keep the account's row.

An OFX file has no cost basis. In a taxable brokerage account the cash
lot's basis is its balance, and a note names the new lots that have none:
set each with `sim account lot edit ACCOUNT LOT --opening-cost-basis
AMOUNT` before a run draws on it. When one lot is split into a lot for
each fund, the old lot is emptied and keeps the account's basis, for you
to share among the new ones.

Amounts are read as institutions write them, with a thousands separator or
a decimal comma (`5,000` is five thousand dollars). An amount with a
fraction of a cent is refused, except a position's value and a brokerage
account's cash and margin balances, which are rounded
to the cent. A number of shares or a position's value written like `2,500`,
which could be 2500 or 2.5, is refused and not guessed.

Files that stray from the format are read where they can be: one with no
header, one that starts with a byte-order mark, one with an accented payee
in the bank's own encoding, one that leaves a value empty. A payee written
`A &amp; B` is read as `A & B`, so the same pattern in `payees.csv` matches
it from any bank. A file that can't be read is refused without quoting it,
since it has whole account numbers in it. So is a download that was cut
off before its end: download it again.

A bank's correction of a transaction replaces or removes the one it
names, whether that is in the same file or in an earlier download given in
the same run.

Not read: a brokerage's investment transactions (buys, sales, dividends,
reinvestments), which are investment activity and not spending.

## A budget: `sim import budget`

```sh
sim import budget budget-full.csv --from Asset_Checking --dry-run
sim import budget budget-full.csv --from Asset_Checking
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
taxes it. Income goes into `--from` unless `--income-to` names
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
sim import transactions transactions.csv --budget budget-full.csv --from Asset_Checking
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
- A category whose transactions were all left out (a payee the yearly
  script took, say) shows an actual of 0.00 and is asked about. Choosing
  actual gives it no rule; choosing budget may pay again what your own
  rules already do.

The budget is read into the transactions' categories, so that nothing is
counted on both sides under two names: a category your `splits.csv`
divides takes the split's row with no account, and a budget category that
the transactions put in `Other`, for want of a row in `categories.csv`,
goes to `Other` too.

Your answers are saved in `imports/budget_choices.csv`, with the two
amounts each was chosen between. The next run uses them and asks nothing,
until one of a category's amounts has moved by more than 5%: then it asks
again, offering what you chose before. Edit the file, or delete a row, to
change your mind.

Away from a terminal the saved choices are used. A category with none
needs `--prefer budget` or `--prefer actual`. A preference answers
wherever a question would be asked (no saved choice, or an amount that
has moved), at a terminal or away from one, and is not saved. With
`--summary` the table is shown and nothing is asked or written. With
`--yearly-script` too, a payee in a category made from the budget (by a
saved choice or `--prefer`) is noted in the script and given no rule: the
budget's rules have that category's yearly payments already.

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
| Portfolio Value & Cost Basis, one saved report for each investment account | `sim import holdings` | a range ending on the scenario's first day |
| A budget's export, Full | `sim import budget` | the year planned |

The transactions report may be the saved one, grouped under each account,
or the "All Transactions" report exported as it stands, with one row for
each transaction and its account in a column. A move between your own
accounts is taken from that report's `Transfer:` category.

With no file named, `sim import transactions` reads
`quicken/Transaction Export.csv` at the data repo's top level, along with the
Amazon categorizer's monthly summaries in `state/`, which split Amazon
orders into real categories.

Quicken's net worth report has one number for a brokerage account, so it
can't fill an account kept as several lots. For those, use the built-in
**Portfolio Value & Cost Basis** report, which is the only export Quicken
has of what an account holds. The report names no account, and one of
several accounts adds them together, so make one for each account:

1. Under *Reports*, open the built-in **Portfolio Value & Cost Basis**
   report.
2. In its account list, turn off every account but the one.
3. Set its dates so that one of them is the scenario's first day (or
   December 31 for `--year-end`). The report has numbers for several dates;
   the import takes those of the date nearest the one the plan wants, and
   says which.
4. For tax lots, open each security's line (the triangle beside its name)
   so that the purchase dates show under it in the *Security* column. Quicken
   exports the dates only for the lines that are open. Leave a line closed
   to import that security as one total: a money market fund, say, whose
   every monthly dividend is a purchase of its own.
5. Save the report under the account's name, and export it as CSV.

```sh
sim import holdings "Joint Brokerage.csv"
```

The import asks which Quicken account the file is, offering the name the
report was saved under and the accounts the net worth report named
(`--outside-account NAME` answers it), then sends each security to the
account's lot of its symbol, with its value and cost basis, and the
account's cash to its cash lot. The report has no share
counts: at a terminal the import offers to work one out from a stored price.
A [brokerage's own file](#brokerage-files) has the counts, and is the better
source where you can get one.

**Tax lots.** A security whose line was open comes as one row for each
purchase date, and the import asks once for each security whether every
one gets a lot of its own:

```text
The quicken file lists VTSAX in Asset_Brokerage_Joint by tax lot: 3 with no lot here yet, acquired 2019-03-15 to 2026-02-10.
  A lot for each keeps its own cost basis and holding period (short or long term), named vtsax-DATE; otherwise they are added together into one lot.
A lot for each tax lot of VTSAX? [Y/n]:
```

A yes (and `--new-lots`, which doesn't ask) makes lots named for the
symbol and the date, such as `vtsax-2019-03-15`, each with its `acquired`
date, value and cost basis. A later import finds each by its date, adds a
lot for a new purchase, and with `--prune` empties one that was sold. A lot
of the symbol with no date, from an earlier import of its total, is emptied
too, since the dated lots now hold what it held. A no asks for one lot's
name instead, and adds them together into it then and in later imports.
Purchases on the same day are one lot. Where the dates listed don't come
to the security's total, the rest gets a lot with no date.

A data repo with Quicken mappings from before the `imports/` folder moves
them with `scripts/migrate_527.py` (it reports first; `--write` does it).
