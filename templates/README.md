# Data repo templates

Skeletons for every CSV the simulator's loader expects (per ADR-002 + ADR-003).
Copy this folder to a new data repo and edit. **Every header row matches the
corresponding DDL column name exactly** to minimize confusion when reading the
SQL schema alongside the source CSV.

## Layout

```text
<data_repo>/
    activate                      # sourced shell script (ADR-004 §4)
    persons.csv                   # household owners + operator identities ($ANALYSIS_USER)
    accounts.csv                  # chart of accounts
    account_models/<name>.csv     # opening balances per model
    aux/<kind>[__<series>].csv    # aux tables; one per kind/series (ADR-003 §4.4)
    scenarios/<name>/
        scenario.csv              # one-row scenario header
        scheduled.csv             # ScheduledTransactions
        waterfall_sources.csv     # ordered sources per waterfall_fill rule
        calendar.csv              # CalendarEvents
        priors.csv                # PriorTaxActivity rows (tax activity only)
```

## Conventions

- Scenario CSVs use decimal-dollar input where applicable (for example, `scheduled.csv` `base_amount`): `$1,234.56` is written as `1234.56` and converted to cents by the loader.
- Aux CSV money columns with `_cents` in the header use **integer cents**: `$1,234.56` → `123456`.
- Rates in **integer micros** (× 1e6). `5%` → `50000`. `4.25%` → `42500`.
- Dates: `YYYY-MM` (pinned to month-end at load) or `YYYY-MM-DD` where the column expects a full date.
- Booleans: `0` / `1`.
- JSON columns hold a single JSON value as a string. Use CSV quoting for embedded commas/quotes.
- Empty string means NULL.

## Budget planning

`budget-template.csv` is a template for the core recurring expenses and income that flow into `scenario/scheduled.csv` rows. Fill in the amounts in dollars (e.g., `1234.56` for $1,234.56), then use it as the basis for creating the corresponding `scheduled.csv` entries with the right accounts, cadences, and rules.

For annual items, set `month_of_year` (`1`-`12`) so payments land in the intended month. If you have two annual payments (for example, property tax in April and December), use two rows with the same category and different `month_of_year` values.

## Aux table filename pattern

`aux/<kind>__<series>.csv` for kinds that have multiple named series (`rate__Asset_Checking`, `state_brackets__CA`, `inflation__cpi`); `aux/<kind>.csv` for kinds that have only one (`federal_brackets`, `standard_deduction`, `rmd_factors`). The filename is parsed by the loader; see ADR-003 §4.4 for the column schema of each kind.

## After editing

```sh
source ./activate
analysis sim check <scenario>     # lints; no run
analysis sim load <scenario>      # builds the intent cache
analysis sim run <scenario>       # produces a run file under state/runs/
```

## Editing rows

CSVs are the source of truth and can be edited by hand, by an AI assistant given the schemas in ADR-003, or by the built-in CLI commands:

```sh
analysis sim account list                 # tabular view with balances and rates
analysis sim account add <name> <kind> <subkind> [--opening-balance ...]
analysis sim transfer list                # one-off and recurring transfers
analysis sim transfer add fixed <date> <from> <to> <amount>
analysis sim transfer add scheduled <start> <from> <to> <amount> [--cadence monthly]
```

The CLI is interactive (tab completion on account names, prompts when arguments are omitted) and validates against the registries in ADR-003 before writing. Prefer it when you want precise control or want to script the same edit across scenarios; ask the assistant when the change is exploratory or spans many rows.
