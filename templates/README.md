# Data repo templates

See [docs/user-guide.md](../docs/user-guide.md) for a task-based walkthrough (adding accounts and lots, changing expenses, modeling a sale, chains, lifespans, calendar events). This file is the CSV column reference and conventions cheat sheet.

Skeletons for every CSV the simulator's loader expects (per [ADR-002](../docs/architecture/ADR-002-storage-schema.md) + [ADR-003](../docs/architecture/ADR-003-registries-and-csv-schemas.md)).
Copy this folder to a new data repo's `sim/` and edit: replace the `test`
person in `persons.csv` with your own, delete `.analysis_user`, and start from
the one scenario, `scenarios/base`. `scripts/link-data.sh --template` (run by
`scripts/setup.sh` when you give it no data repo, and by CI) copies it unedited
to `data/shared/sim`, where it loads as is. **Every header row matches the
corresponding DDL column name exactly** to minimize confusion when reading the
SQL schema alongside the source CSV.

## Layout

```text
<data_repo>/
    activate-analysis             # sourced shell script ([ADR-004](../docs/architecture/ADR-004-reproducibility-and-activation.md) §4)
    .retention.yaml               # which old runs `sim load`/`sim run` prune ([ADR-004](../docs/architecture/ADR-004-reproducibility-and-activation.md) §10)
    persons.csv                   # household owners + operator identities ($ANALYSIS_USER)
    accounts.csv                  # chart of accounts
    itemizations/<name>.csv       # optional money rollups referenced as @<name>
    aux/<kind>[__<series>].csv    # tax-law aux tables; one per kind ([ADR-003](../docs/architecture/ADR-003-registries-and-csv-schemas.md) §4.4)
    prices/security_price__<SYMBOL>.csv  # security price histories, shared ([ADR-003](../docs/architecture/ADR-003-registries-and-csv-schemas.md) §4.4)
    scenarios/<name>/
        scenario.csv              # one-row scenario header
        accounts.csv              # chart of accounts for this scenario
        account_lots.csv        # opening balances, one row per lot ([ADR-005](../docs/architecture/ADR-005-lots-and-per-lot-state.md))
        scheduled.csv             # ScheduledTransactions
        calendar.csv              # CalendarEvents
        lifespan.csv              # optional per-person final_month override ([ADR-003](../docs/architecture/ADR-003-registries-and-csv-schemas.md) §4.9)
        year_to_date.csv          # optional partial-first-year catch-up facts ([ADR-003](../docs/architecture/ADR-003-registries-and-csv-schemas.md) §4.10)
        chains/<role>/<name>.csv  # optional named chains: role is source or destination ([ADR-003](../docs/architecture/ADR-003-registries-and-csv-schemas.md) §4.7)
        series/<kind>__<series>.csv  # this scenario's growth-rate/inflation series (no shared default; §4.4)
        aux/, prices/, itemizations/  # optional per-scenario overrides ([ADR-003](../docs/architecture/ADR-003-registries-and-csv-schemas.md) §4.1d)
```

## Conventions

- Scenario CSVs use decimal-dollar input where applicable (for example, `scheduled.csv` `amount`): `$1,234.56` is written as `1234.56`. The loader converts to integer cents.
- Aux CSV money columns are also decimal dollars (e.g. `floor`, `amount`, `tier1_floor`, `tier2_floor`): `$1,234.56` → `1234.56`.
- Rates are percents, with the `%` sign required: `5%`, `4.25%`. Dollar amounts are whole dollars or dollars and cents (`1234`, `-1234.56`, `0.99`).
- Dates: a month column takes `YYYY-MM` or `YYYY-MM-DD` and is pinned to month-end at load. `birthdate`, a lot's `acquired`, and price dates are always a full `YYYY-MM-DD` and keep their day. Where the engine counts days, a full date keeps its day too: a `biweekly` rule's `start` (its first payday, so it must be a full date) and `end`, and a `retire_scheduled` event's `date`; a bare month there still means month-end. A date-varying calendar row's `vary_low`/`vary_high` keep a full date exactly, and a bare month spans the whole month.
- Booleans: `0` / `1`.
- JSON columns hold a single JSON value as a string. Use CSV quoting for embedded commas/quotes.
- Empty string means NULL.
- Monetary fields may reference an itemization CSV using `@name` (for example, `@home_basis`).
    The loader reads `itemizations/name.csv`, sums the `Value` column, and uses that total, searching
    a scenario's own `itemizations/` folder before the shared one at the data repo root (see below).

## Per-scenario overrides for aux/, prices/, series/ and itemizations/

`aux/`, `prices/` and `itemizations/` at the data repo root are the shared defaults every
scenario uses unless it says otherwise: tax law, security prices, and cost-basis itemizations
are the same regardless of which what-if you're running. `series/` is different: growth-rate
and inflation assumptions are properly per-scenario projections, so `series/` has **no**
shared root default — it only exists under `scenarios/<name>/series/`, and what's "shared" is
just the ordinary inheritance from a scenario's ancestors (its parent's `series/`, its
parent's parent's, ...).

A scenario can replace one `aux/`/`prices/`/`itemizations/` file for itself (and anything
derived from it) by adding a same-named file under its own folder:
`scenarios/<name>/aux/<file>.csv`, `scenarios/<name>/prices/<file>.csv`, or
`scenarios/<name>/itemizations/<file>.csv`. The loader resolves each filename independently,
most specific first — the scenario's own file, else its parent's (following `parent_scenario`
all the way up), else the shared default — and a match replaces the whole file, not a
row-by-row merge. `series/<file>.csv` resolves the same way, just without that last, shared-root
step. This is how you model a different future tax policy or cost-basis itemization on one
scenario without touching any other, and how every scenario gets its own rate/inflation
assumptions by default (inherited from its parent) while still being able to override them.
See [ADR-003](../docs/architecture/ADR-003-registries-and-csv-schemas.md) §4.1d, `aux/README.md` and `prices/README.md`.

## Budget planning

`budget-template.csv` is a template for the core recurring expenses and income that flow into `scenario/scheduled.csv` rows. Fill in the amounts in dollars (e.g., `1234.56` for $1,234.56), then use it as the basis for creating the corresponding `scheduled.csv` entries with the right accounts, cadences, and rules.

For annual items, set `month_of_year` (`1`-`12`) so payments land in the intended month. If you have two annual payments (for example, property tax in April and December), use two rows with the same category and different `month_of_year` values.

## Aux/series/prices table filename pattern

`<kind>__<series>.csv` for kinds that have multiple named series (`rate__Asset_Checking`, `security_price__VTI`); `<kind>.csv` for kinds that have only one (`federal_brackets`, `standard_deduction`, `rmd_factors`). The filename is parsed by the loader the same way in `aux/`, `series/` and `prices/`; see [ADR-003](../docs/architecture/ADR-003-registries-and-csv-schemas.md) §4.4 for the column schema of each kind, and "Per-scenario overrides" above for how the three folders differ and how a scenario can replace one file.

## After editing

```sh
source ./activate-analysis
sim check <scenario>              # lints; no run
sim check --all                   # lints every scenario
sim load <scenario>               # builds the intent cache
sim run <scenario>                # produces a run file under state/runs/
```

When sourced, `activate-analysis` will:

- Discover the analysis program repo from `$ANALYSIS_DATA_DIR/.analysis`.
- Prompt for the repo path if missing/invalid, validate it, and persist it.
- Ensure `.analysis`, `.current_scenario`, `.analysis_user` and `.retention.yaml` are ignored in the data repo's `.gitignore`.
- Create `.venv` with `uv sync --group dev` if needed, then activate it.
- Take `ANALYSIS_USER` from the environment or `.analysis_user`; if neither names a
  person in `persons.csv`, list the `primary` persons and ask for an id or display
  name, then remember it in `.analysis_user`.
- Enable shell completion for `analysis`, `amazon`, and `sim`.

## Editing rows

CSVs are the source of truth and can be edited by hand, by an AI assistant given the schemas in [ADR-003](../docs/architecture/ADR-003-registries-and-csv-schemas.md), or by the built-in CLI commands:

```sh
sim account list                          # tabular view with balances and rates
sim account add <name> <kind> <subkind> [--opening-balance ...]
sim account check                         # report missing/duplicate/unknown account_lots rows
sim account sync                          # scaffold missing account_lots rows with zero balance
sim account lot list [<account>]          # an account's lots, and where each comes from
sim account lot add <account> <lot> [--symbol ...] [--opening-balance ...]
sim account lot edit <account> <lot>      # change a lot's fields, or rename it
sim account lot rename <account> <lot> <new>  # in every scenario sharing it
sim account lot delete <account> <lot>    # with the rows that reference it
sim transfer list                         # one-off calendar transfers
sim transfer add <date> <from> <to> <amount>
sim scheduled list                        # recurring scheduled transfers
sim scheduled add <start> <from> <to> <amount> [--cadence monthly]
sim scenario rename <old> <new>           # also: account, chain, scheduled, calendar, series rename
sim scenario edit <name> [--start ...] [--parents ...]  # scenario.csv; '' inherits
sim scenario delete <name>                # refused while another scenario builds on it
```

The CLI is interactive (tab completion on account names, prompts when arguments are omitted) and validates against the registries in [ADR-003](../docs/architecture/ADR-003-registries-and-csv-schemas.md) before writing. Prefer it when you want precise control or want to script the same edit across scenarios; ask the assistant when the change is exploratory or spans many rows.

### account_lots.csv data hygiene

`accounts.csv` defines your chart of accounts; `account_lots.csv` holds scenario-specific opening balances and prior year balances. To keep them in sync:

- **Check first**: `sim account check` reports any missing, duplicate, or unknown accounts.
- **Sync**: `sim account sync` creates missing account_lots rows (with `opening_balance = 0.00`).
- **Edit balances**: After syncing, use your editor or `sim account lot edit` to set opening balances as needed.

The loader enforces referential integrity (strict join validation), so missing or mismatched account names will raise a clear error before the scenario loads.

### Lots

Each `account_lots.csv` row is one **lot**: a position inside one account. An account you don't split has a single lot named `default`, and you can leave the `lot` column blank for it. Every file that names an account has a `lot` column beside it: `lot` in `account_lots.csv` and the chain files, `from_lot`/`to_lot` in `scheduled.csv` and `year_to_date.csv`, `lot_1`/`lot_2` in `calendar.csv`. Lot names are free-form (`default`, `1`, `2024-03`, ...) but may not contain `:`.

**Investment accounts** (`brokerage_taxable`, `traditional_ira`, `roth_ira`, `traditional_401k`, `roth_401k`, `hsa`) must always name their lot, everywhere; a blank lot is a load error that lists the account's lots. Other accounts can leave it blank. A chain named in an account column takes no lot.

To split an account, give it several rows with different `lot` values, each with its own `opening_balance` and `opening_cost_basis`; `sim account lot add|edit|delete|list` write and show these rows for you, and keep the other files' lot references in step (see the [user guide](../docs/user-guide.md), Part 4.2). The columns are `account_name, lot, symbol, acquired, quantity, series, opening_balance, opening_cost_basis, prior_year_end_balance`, then the balance-management columns described below. `symbol`, `acquired` (ISO date), `quantity` (shares) and `series` (the lot's growth-rate series) are optional and may stay blank; they are in the file so you can see where that information goes. A lot grows at its `series`, else at a rate series named for its `symbol`, else at the account's `series`. A withdrawal drawn from one lot (through a chain that lists it) splits that lot's basis pro-rata and leaves the other lots alone.

**Prices and derived balances.** Leave a lot's `opening_balance` blank and give its `symbol` and `quantity`, and the loader fills it in as `quantity × price`, using the latest price on or before the scenario start from `prices/security_price__<SYMBOL>.csv` (columns `effective_date,price`; price is decimal dollars, up to 6 places). If you fill in `opening_balance` yourself it is used as given and no price is needed. A symbol may not be the same as an account name. A lot's blank `prior_year_end_balance` is derived the same way, as its `quantity` times the price on or before the prior Dec 31, when another lot of the same account states one (an account must give it on every lot or none). It uses today's share count, so it ignores buys and reinvestment since; a value you enter wins. Prices are only used for these two values; after that a lot grows by its rate series, not by price.

**Fetching prices.** `sim prices fetch [SYMBOL...]` writes those files for you. With no symbols it takes every symbol on a lot that has a `quantity` in any scenario (`CASH` is skipped; dollar-denominated lots need no price). For each it stores the last close of every month since the December before the earliest scenario start's year, plus the latest close, so a scenario starting on a month-end finds a price on or before it, and a prior year-end balance can be derived from the prior December's close. It only adds dates that are not already in a file, so hand-entered prices stay; `--refresh` overwrites existing dates, `--since YYYY-MM-DD` changes the start, and `--dry-run` shows what would change. Prices come from Yahoo Finance's public endpoint (`--provider yahoo`, no account or key), and only the ticker symbols are sent. Closes are the unadjusted price, which is what `quantity × price` needs. Re-run it whenever you want a scenario's opening balances to reflect current prices; a `security_price` you want for a specific date can be added to the file by hand.

**Security names.** `securities.csv`, at the top of the data repo beside `persons.csv`, gives each symbol a name (`symbol,name`, one row per symbol, shared by all scenarios). It is optional, and a symbol without a row is simply shown unnamed. The holdings commands and widgets show the names.

**Asset allocation.** `allocations.csv`, beside `securities.csv`, says what each symbol is made of: `symbol,as_of,asset_class,weight`, one row per asset class, with the weights of a (`symbol`, `as_of`) set adding up to 100. `asset_class` is `stock_us`, `stock_intl`, `stock` (stock with an unknown US/international split, to refine when you know it), `bond`, `cd`, `cash`, `real_estate` or `other`. `as_of` is optional; give several dated sets to follow a fund whose mix changes (a target-date fund), and the overview uses the latest set on or before the date it shows. Cash lots, real estate and vehicles need no rows (they are classed from their account subkind), and neither do lots with the symbol `CASH` (cash) or `CD` (certificates of deposit, which have no ticker; a CD is reinvested at maturity, so it stays in its lot and `cd` is only a classification). `sim allocation [--by account|owner|subkind|tax_treatment|security]` prints the whole portfolio by asset class, with the same `--month`, `--opening` and `--runfile` options as the holdings commands, and `portfolio_allocation_widget(rc)` from `analysis.sim.notebook_ui.allocation_widgets` shows it with a pie chart. A lot whose symbol has no rows is listed as `unclassified` so the gap is visible. Leave classes out with `--exclude` (repeatable) or the widget's checkboxes, for instance `real_estate` and `unclassified` to see the balance classified so far among the liquid holdings; the total and percentages then cover what remains. The weights are as of a date; the model does not rebalance funds or let their mix drift with prices.

**Adding a symbol.** `sim symbol add SYMBOL --name NAME --weight CLASS=PERCENT ...` writes a symbol's `securities.csv` row and one set of `allocations.csv` weights (with `--as-of` to date it), checked as the loader checks them, then fetches its prices; without SYMBOL it asks for each. `sim symbol list` shows every symbol named in these files, `prices/` or a lot, with its latest weights, its prices and the lots holding it. The user guide's Part 4.11 has a prompt for asking an AI to look up a fund's weights.

**Viewing holdings.** `sim account holdings <account>` lists an account's lots (symbol, acquired, quantity, implied price, balance, basis, unrealized gain, with totals), and `sim account holders <symbol>` lists every account holding a symbol. Both read the scenario's latest run at its last month; use `--month YYYY-MM` for another month, `--runfile` for a specific run, or `--opening` for the opening values (also what you get when there is no run yet). In a notebook, `account_holdings_widget(rc)` and `symbol_holders_widget(rc)` from `analysis.sim.notebook_ui.holdings_widgets` show the same tables with dropdowns; the account widget offers every asset and liability account, the symbol widget only the symbols held in investment accounts, and the commands accept any account or symbol.

**Short-term and long-term.** A sale from a lot books its gain as long-term (`ltcg`) once the lot has been held more than a year, and short-term (`stcg`, taxed as ordinary income) before that, using the lot's `acquired` date; a lot with no `acquired` date is long-term. If `aux/ltcg_brackets.csv` exists, federal tax taxes long-term gains at its preferential rates; without it they are taxed as ordinary income. To draw long-term lots first, list them first in a chain.

**Purchases and growth.** Money you move into a taxable lot (a purchase from checking, an inheritance) adds that amount to its basis, and to its shares if `quantity` is filled in. The lot's own growth does not: it is total return, so reinvested dividends are not split out. To buy a position later in the scenario, add a lot (`sim account lot add`) with `opening_balance` 0 and the purchase date in `acquired`, and schedule the purchase into it. Leave `quantity` blank for dollar-denominated funds such as money markets.

**Cash accounts.** Bank accounts use the single subkind `cash`, and can have lots like any other account (for instance one lot per bank account under a single `Bank` account). By convention, give every cash lot the symbol `CASH`: then `sim account holders CASH` and the symbol widget list all your cash in one table. A lot grows at a rate series named for its symbol, so an `aux/rate__CASH.csv` would also become the default yield of every cash lot that has no series of its own.

**RMDs from an account with several lots.** The `rmd` rule's amount is based on the whole account (all its lots together), and it must draw from a chain that lists the lots to take from, in order. Set `from_account` to the chain and put `{"subject_account": "<the IRA>"}` in `params_json`; the chain may only contain that account's lots.

**Minimum balance, refill and overflow are per lot.** `minimum_balance`, `refill_chain`, `refill_lot`, `refill_target`, `refill_amount`, `maximum_balance`, `overflow_chain` and `overflow_lot` are the last columns of `account_lots.csv` (they used to be on `accounts.csv`, where a value is now an error). A debit that would take a lot below its `minimum_balance` first draws from its `refill_chain`, which can be a named chain or another lot: `refill_chain` an account name with `refill_lot` its lot. The lots of one account can differ, so a cash lot can refill from the fund lots beside it, or several bank accounts can be lots of one account with only some of them refilling. `maximum_balance` and `overflow_chain` sweep a lot's excess at month end in the same way.

### Importing balances from Quicken

`sim account import-quicken` updates `account_lots.csv` from a Quicken account-tree
report export, without ever renaming Quicken's own account names. It has two modes:

- **Opening balances** (default): use **My Reports → Net Worth Export** (re-run "as of"
  today before each export). Sets `opening_balance` for every mapped account. Also
  *seeds* `prior_year_end_balance` from the same figure, but only for investment asset
  accounts (`brokerage_taxable`, `traditional_ira`, `roth_ira`, `traditional_401k`,
  `roth_401k`, `hsa`) whose `prior_year_end_balance` is still blank — it never overwrites
  an existing value, and never touches non-investment or liability accounts.
- **Year-end balances** (`--year-end`): use **My Reports → Year End Export** (as of
  12/31 of the prior year). Sets `prior_year_end_balance` for investment asset accounts
  only, always overwriting any existing value; `opening_balance` is left untouched.

Steps:

1. Export the appropriate report from Quicken as CSV.
2. `scenarios/<name>/quicken_account_map.csv` maps `quicken_name -> account_name`
   (columns: `quicken_name, account_name, action, note`; `action` is `map` or `skip`).
   Every account row present in the Quicken export must appear here — new or renamed
   Quicken accounts are reported as errors instead of silently skipped. Both reports
   share the same account list, so one map file covers both.
3. Run:

   ```sh
   sim account import-quicken <export.csv> --scenario <name> --dry-run              # preview
   sim account import-quicken <export.csv> --scenario <name>                        # apply
   sim account import-quicken <year_end.csv> --scenario <name> --year-end --dry-run # preview
   sim account import-quicken <year_end.csv> --scenario <name> --year-end           # apply
   ```

Liability balances are stored as a non-negative debt magnitude (per the engine's sign
convention), so the importer negates Quicken's signed liability values automatically.
`prior_year_end_balance` is only meaningful for the RMD rule ([ADR-003](../docs/architecture/ADR-003-registries-and-csv-schemas.md) §2.3), hence the
investment-asset restriction.

**Bookkeeping:** `sim account rename` and `sim account edit --new-name` already update
`quicken_account_map.csv`'s `account_name` column alongside `accounts.csv`,
`account_lots.csv`, `scheduled.csv`, `calendar.csv`, `year_to_date.csv` and chain
files, in every scenario that shares the account — no manual edit of the map is needed
after a rename. If you add a new account, add its
`quicken_account_map.csv` row (or a `skip` row with a `note` explaining why) before the
next import.
