# Data repo templates

Skeletons for every CSV the simulator's loader expects (per ADR-002 + ADR-003).
Copy this folder to a new data repo and edit. **Every header row matches the
corresponding DDL column name exactly** to minimize confusion when reading the
SQL schema alongside the source CSV.

## Layout

```text
<data_repo>/
    activate-analysis             # sourced shell script (ADR-004 §4)
    persons.csv                   # household owners + operator identities ($ANALYSIS_USER)
    accounts.csv                  # chart of accounts
    account_models/<name>.csv     # opening balances per model
    itemizations/<name>.csv       # optional money rollups referenced as @<name>
    aux/<kind>[__<series>].csv    # aux tables; one per kind/series (ADR-003 §4.4)
    scenarios/<name>/
        scenario.csv              # one-row scenario header
        scheduled.csv             # ScheduledTransactions
        waterfall_sources.csv     # ordered sources per waterfall_fill rule
        calendar.csv              # CalendarEvents
        priors.csv                # PriorTaxActivity rows (tax activity only)
        lifespan.csv              # optional per-person final_month override (ADR-003 §4.10)
        year_to_date.csv          # optional partial-first-year catch-up facts (ADR-003 §4.11)
```

## Conventions

- Scenario CSVs use decimal-dollar input where applicable (for example, `scheduled.csv` `base_amount` and `amount`): `$1,234.56` is written as `1234.56`. The loader converts to integer cents.
- Aux CSV money columns are also decimal dollars (e.g. `floor`, `amount`, `tier1_floor`, `tier2_floor`): `$1,234.56` → `1234.56`.
- Rates in **integer micros** (× 1e6). `5%` → `50000`. `4.25%` → `42500`.
- Dates: `YYYY-MM` (pinned to month-end at load) or `YYYY-MM-DD` where the column expects a full date.
- Booleans: `0` / `1`.
- JSON columns hold a single JSON value as a string. Use CSV quoting for embedded commas/quotes.
- Empty string means NULL.
- Monetary fields may reference an itemization CSV using `@name` (for example, `@home_basis`).
    The loader reads `itemizations/name.csv`, sums the `Value` column, and uses that total.

## Budget planning

`budget-template.csv` is a template for the core recurring expenses and income that flow into `scenario/scheduled.csv` rows. Fill in the amounts in dollars (e.g., `1234.56` for $1,234.56), then use it as the basis for creating the corresponding `scheduled.csv` entries with the right accounts, cadences, and rules.

For annual items, set `month_of_year` (`1`-`12`) so payments land in the intended month. If you have two annual payments (for example, property tax in April and December), use two rows with the same category and different `month_of_year` values.

## Aux table filename pattern

`aux/<kind>__<series>.csv` for kinds that have multiple named series (`rate__Asset_Checking`, `state_brackets__CA`, `inflation__cpi`); `aux/<kind>.csv` for kinds that have only one (`federal_brackets`, `standard_deduction`, `rmd_factors`). The filename is parsed by the loader; see ADR-003 §4.4 for the column schema of each kind.

## After editing

```sh
source ./activate-analysis
sim check <scenario>              # lints; no run
sim load <scenario>               # builds the intent cache
sim run <scenario>                # produces a run file under state/runs/
```

When sourced, `activate-analysis` will:

- Discover the analysis program repo from `$ANALYSIS_DATA_DIR/.analysis`.
- Prompt for the repo path if missing/invalid, validate it, and persist it.
- Ensure `.analysis` is ignored in the data repo's `.gitignore`.
- Create `.venv` with `uv sync --group dev` if needed, then activate it.
- Enable shell completion for `analysis`, `amazon`, and `sim`.

## Editing rows

CSVs are the source of truth and can be edited by hand, by an AI assistant given the schemas in ADR-003, or by the built-in CLI commands:

```sh
sim account list                          # tabular view with balances and rates
sim account add <name> <kind> <subkind> [--opening-balance ...]
sim account check                         # report missing/duplicate/unknown account_values rows
sim account sync                          # scaffold missing account_values rows with zero balance
sim transfer list                         # one-off calendar transfers
sim transfer add <date> <from> <to> <amount>
sim scheduled list                        # recurring scheduled transfers
sim scheduled add <start> <from> <to> <amount> [--cadence monthly]
```

The CLI is interactive (tab completion on account names, prompts when arguments are omitted) and validates against the registries in ADR-003 before writing. Prefer it when you want precise control or want to script the same edit across scenarios; ask the assistant when the change is exploratory or spans many rows.

### account_values.csv data hygiene

`accounts.csv` defines your chart of accounts; `account_values.csv` holds scenario-specific opening balances and prior year balances. To keep them in sync:

- **Check first**: `sim account check` reports any missing, duplicate, or unknown accounts.
- **Sync**: `sim account sync` creates missing account_values rows (with `opening_balance = 0.00`).
- **Edit balances**: After syncing, use your editor or `sim account add` to set opening balances as needed.

The loader enforces referential integrity (strict join validation), so missing or mismatched account names will raise a clear error before the scenario loads.

### Importing balances from Quicken

`sim account import-quicken` updates `account_values.csv` from a Quicken account-tree
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
`prior_year_end_balance` is only meaningful for the RMD rule (ADR-003 §2.3), hence the
investment-asset restriction.

**Bookkeeping:** `sim account rename` and `sim account edit --new-name` already update
`quicken_account_map.csv`'s `account_name` column alongside `accounts.csv`,
`account_values.csv`, `scheduled.csv`, `calendar.csv`, and `waterfall_sources.csv` — no
manual edit of the map is needed after a rename. If you add a new account, add its
`quicken_account_map.csv` row (or a `skip` row with a `note` explaining why) before the
next import.
