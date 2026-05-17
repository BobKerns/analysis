# Data repo templates

Skeletons for every CSV the simulator's loader expects (per ADR-002 + ADR-003).
Copy this folder to a new data repo and edit. **Every header row matches the
corresponding DDL column name exactly** to minimize confusion when reading the
SQL schema alongside the source CSV.

## Layout

```text
<data_repo>/
    activate                      # sourced shell script (ADR-004 §4)
    users.toml                    # validates ANALYSIS_USER
    persons.csv                   # household owners
    accounts.csv                  # chart of accounts
    account_models/<name>.csv     # opening balances per model
    aux/<kind>[__<series>].csv    # aux tables; one per kind/series (ADR-003 §4.4)
    scenarios/<name>/
        scenario.csv              # one-row scenario header
        scheduled.csv             # ScheduledTransactions
        waterfall_sources.csv     # ordered sources per waterfall_fill rule
        calendar.csv              # CalendarEvents
        priors.csv                # PriorTaxActivity + PriorAccountFact (union table)
```

## Conventions

- Money in **integer cents**. `$1,234.56` → `123456`.
- Rates in **integer micros** (× 1e6). `5%` → `50000`. `4.25%` → `42500`.
- Dates: `YYYY-MM` (pinned to month-end at load) or `YYYY-MM-DD` where the column expects a full date.
- Booleans: `0` / `1`.
- JSON columns hold a single JSON value as a string. Use CSV quoting for embedded commas/quotes.
- Empty string means NULL.

## Aux table filename pattern

`aux/<kind>__<series>.csv` for kinds that have multiple named series (`rate__Asset_Checking`, `state_brackets__CA`, `inflation__cpi`); `aux/<kind>.csv` for kinds that have only one (`federal_brackets`, `standard_deduction`, `rmd_factors`). The filename is parsed by the loader; see ADR-003 §4.4 for the column schema of each kind.

## After editing

```sh
source ./activate
analysis sim check <scenario>     # lints; no run
analysis sim load <scenario>      # builds the intent cache
analysis sim run <scenario>       # produces a run file under state/runs/
```
