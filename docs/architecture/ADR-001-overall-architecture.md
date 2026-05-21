# ADR-001: Overall architecture of the finance simulator

- **Status:** Accepted
- **Date:** 2026-05-17
- **Scope:** New top-level subsystem `src/analysis/sim/`, sibling to the existing `amazon/` subsystem. Shares utility code (logging, sqlite helpers) but not data.

## Context

The audience is two retirement planners (one of whom is the author; the other is Python-literate but skeptical of "investment income" as a real category and tends to model retirement as pure savings drawdown). The tool must:

- **Mimic how the world works** so the model itself is reassuring, not just the output.
- Support comparing scenarios (claim SS at FRA vs. delay; downsize in year X; one spouse predeceasing the other).
- Run deterministically against authored CSV inputs.
- Provide a database backing that supports flexible, ad-hoc reporting (including AI-assisted SQL exploration) in a later phase.

A monthly simulation step is sufficient resolution. Stochastic analysis is deferred to a later ensemble layer that generates many deterministic scenarios from a base.

## Decision

### Two-layer domain model

The architecture has exactly two layers, and the separation is load-bearing:

1. **Intent layer** — what was *decided*. Authored content: `AccountModel`, `ScheduledTransaction`, `Calendar`, `CalendarEvent`, `AuxiliaryTable`, `Scenario`, `Priors`.
2. **Ledger layer** — what *happened*. Run output: `SimulationRun`, `SimulationStep`, `AccountState`, `TransferEvent`, `TaxYearSummary`.

Every dollar movement in the ledger is a `TransferEvent` (date, from-account, to-account, amount, tax character, reason). Bundled high-level decisions (`sell_asset`, `life_transition`) live in the intent layer and **expand at run-time** into multiple primitive `TransferEvent`s, all linked via `reason_id` so reports can group at either level. Nothing in the ledger is "virtual" or "derived on the fly" — what's in the table is the truth.

### Domain objects

| Object | Layer | Sharing | Description |
| --- | --- | --- | --- |
| `Person` | Intent | Shared | Owner identity (id, display, birthdate). Needed for RMD ages and life-transition events. |
| `Account` | Intent | Shared | One real-world place where money sits. `kind` ∈ {asset, liability, income, expense}; `subkind` carries tax/usage semantics; optional `owner`. |
| `AccountModel` | Intent | Shared | A named set of `Account`s with opening balances and opening cost basis. Typically shared across scenarios. |
| `ScheduledTransaction` | Intent | Mostly shared | A *rule* that emits `TransferEvent`s on a cadence. Amount is computed by a typed `amount_rule` (fixed, percent-of-balance, RMD, waterfall-fill, …) so interest accrual, SS payments, withdrawals, and tax reconciliation all use the same machinery. |
| `Calendar` | Intent | Per-scenario | An ordered list of `CalendarEvent`s. Represents the assumptions being compared. |
| `CalendarEvent` | Intent | Per-scenario | A *patch* to the running scenario on a given date: change a rate, add/retire a scheduled transaction, swap an aux table, sell an asset, apply a life transition, etc. Bounded set of kinds; see ADR-003. |
| `AuxiliaryTable` | Intent | Mostly shared | Versioned reference data: tax brackets, RMD divisors, SS taxation thresholds, inflation series, per-account return rates, filing status. Calendars can swap these by date. |
| `Scenario` | Intent | Per-comparison | `(AccountModel, Calendar, AuxTables, Priors, start, end)`. Identified by name; runs are reproducible from it. |
| `Priors` | Intent | Per-scenario | YTD/prior-period facts that seed a partial first year: prior tax activity, prior account facts (YTD contributions, year-end balances for RMD, cost-basis estimates). |
| `SimulationRun` | Ledger | — | One execution. Stamps code/data versions, acting user, host, status. |
| `SimulationStep` | Ledger | — | One month. Links to previous step (nullable for initial state), to applied `CalendarEvent`s, and owns the `AccountState` snapshot and `TransferEvent`s emitted in its window. |
| `AccountState` | Ledger | — | `(step_id, account_id) → balance, cost_basis`. Long-and-narrow for pivoting. |
| `TransferEvent` | Ledger | — | Atomic ledger entry. Tagged with `tax_character` so the retrospective tax scan can bucket without parallel bookkeeping. |
| `TaxYearSummary` | Ledger | — | Cache/audit row for each (run, year, bucket); always recomputable from `TransferEvent` + active aux tables. |

### Monthly step algorithm

```text
load initial AccountState from AccountModel + Priors → step 0 (previous_step_id = NULL)
for each month m in [start..end]:
    apply Calendar events dated within m (in calendar order)
        → expand high-level events into primitive TransferEvents and rule mutations
        → record applied_calendar_event rows
    fire active ScheduledTransactions for m, ordered by rule priority:
        evaluate amount_rule(current_state, aux_tables, date)
        emit TransferEvent, mutate balances and cost basis
    snapshot AccountState as end-of-month
    persist SimulationStep referencing previous step and applied events
```

Within-month ordering is **strictly by rule priority**, never by date. Priority bands are reserved:

- `100` accruals (interest, dividends, appreciation)
- `200` income (salary, SS, pensions)
- `300` expenses
- `400` tax accumulators / accruals
- `500` conversions / one-offs
- `900` waterfall fill (must run last so it sees the actual shortfall)

### Cross-cutting principles

These are project-wide rules that all subsequent ADRs and code reference:

1. **Deterministic engine.** Pure function of `(AccountModel, Calendar, AuxTables, Priors, start, end)`. No `datetime.now()` in engine paths, no RNG, no dict-iteration-order dependence, no floats for money. Money is `INTEGER` cents end-to-end.
2. **Month-granularity authoring; month-end dates internally.** CSVs use `YYYY-MM`. Loader pins to last day of month. No within-month sequencing semantics.
3. **Intent vs. ledger separation.** If a real-world decision has multiple legs and any leg depends on run-time state, it earns a `CalendarEvent` kind. Otherwise it's a primitive `one_off_transfer`. The set of kinds is bounded and reviewed (see ADR-003).
4. **Visible approximation beats invisible omission.** Zero-valued accounts for assets we choose not to model (ISOs, potential JPY inheritance); basis-with-sources lists for the home; `data/known_exclusions.md` for things deliberately out of scope.
5. **The scenario is a checklist.** Every `ScheduledTransaction` should be referenceable by `life_transition` events; a `--check life_transitions` mode flags omissions without erroring.
6. **Schema is documentation.** Long-narrow tables, descriptive names, a `schema_overview` row per table for LLM priming. See ADR-002.
7. **Hard errors on impossible states.** Insufficient funds, waterfall exhaustion, unknown user, dirty data without override → halt, persist partial results, report enough context to fix the scenario.

### Reproducibility and gating contract

Detail in ADR-004; summary here:

- Code and data live in separate git repos.
- Every run file's `simulation_run` row stamps `code_version`, `code_dirty`, `data_version`, `data_dirty`, `scenario_tree_id`, `data_repo_path`, `data_repo_name`, `run_by`, `host`. The matching intent file's `intent_manifest` stamps the same data identifiers.
- Determinism key is `(code_version, scenario_tree_id)`—the tree id subsumes `data_version` and the dirty-vs-clean distinction since it's the content-addressed git tree object covering the scenario's input files. Identity metadata (`run_by`, `host`) is recorded but excluded from the byte-identical guarantee. Re-runs are compared via a logical diff (timestamps and host vary) rather than file-byte equality.
- Dirty data is **refused** by default (`--allow-dirty-data` override, stamped).
- Dirty code is **warned**, allowed.

### Activation and gating

The data repo provides a sourceable `activate` script (venv-style) that sets:

- `ANALYSIS_DATA_DIR` — absolute path to the data repo. Engine refuses to run if unset.
- `ANALYSIS_USER` — short identifier (`rwk`, `mwk`, …) matching a row `id` in `persons.csv` in the data repo. Refuses to run if unset or unknown. (Persons and operator identities are the same set: every operator is a person, though not every person needs to be an operator.)
- `ANALYSIS_DATA_REPO_NAME` — display label, defaults to basename of `ANALYSIS_DATA_DIR`.
- `ANALYSIS_DATA_MODE` — set to `prototype` if `ANALYSIS_DATA_DIR` is inside the code repo tree; informational, banner-printed.

`PS1` is prefixed `[analysis:$ANALYSIS_DATA_REPO_NAME:$ANALYSIS_USER]` so the active context is visible in a shared shell. `deactivate` reverses all of it.

### Prototype data

`prototype_data/` lives in the code repo and is a valid data repo from the engine's perspective. Contents are obviously fake (`Asset_Alice`, $100/$1000 amounts, fake birthdates). Used for tests, CI smoke runs, and demos. A pre-commit hook in the code repo blocks obviously-real patterns (SSN-shaped strings, etc.) to keep this repo personal-data-clean. The code repo itself is **not** the production data repo; production data lives in a separate private repo per the dual-repo design.

### Code organization

```text
src/analysis/sim/
    __init__.py
    model.py          # Person, Account, AccountModel, ScheduledTransaction dataclasses
    calendar.py       # Calendar, CalendarEvent
    aux.py            # tax brackets, RMD factors, SS thresholds, inflation series helpers
    rules.py          # amount_rule registry; see ADR-003
    expansions.py     # high-level CalendarEvent expanders (sell_asset, life_transition, …)
    state.py          # in-memory AccountState; invariants
    engine.py         # run(scenario) -> SimulationRun
    storage.py        # per-scenario intent DB + per-run ledger DB, schema, DAOs
    loaders.py        # CSV → intent DB (built per-scenario; silently rebuilt on staleness)
    activate.py       # env-var validation, ANALYSIS_USER lookup, gating
    tax.py            # retrospective tax scan
    cli.py            # `analysis sim` subcommands
tests/sim/...
```

The existing `analysis.storage` module contains item-import schemas specific to Amazon CSVs; generic sqlite helpers (connection setup, normalization utilities) will be extracted into a sibling module if shared use justifies it. Until then, `sim/storage.py` defines its own DDL and connection.

### CLI surface (v1)

- `analysis sim load <scenario>` — build/refresh `state/intent/<scenario>.sqlite3` from the data repo. Silently rebuilds when the source CSV hash or `data_version` no longer matches; `--force` always rebuilds. Intent files are a derived cache and may be deleted at any time.
- `analysis sim run <scenario>` — execute against the (auto-loaded) intent file, write a new run file at `state/runs/<isoZ>_<scenario>_<dataSHA>.sqlite3`.
- `analysis sim list` — list run files with status, versions, who, when.
- `analysis sim explain <runfile>` — scenario name, versions, year-end net worth, total taxes per year, life-transitions applied. Sanity dump.
- `analysis sim compare <runfile_a> <runfile_b> [--by subkind|account|owner]` — `ATTACH`es both files, prints net-worth table + matplotlib chart; flags version skew in the report header.
- `analysis sim query "<sql>" <runfile>` — read-only SQL passthrough, pretty-printed. Entry point for AI-assisted exploration.
- `analysis sim check <scenario> [--life-transitions|--contribution-limits|...]` — soft validators against the intent file, exit non-zero on findings.
- `analysis sim verify <runfile>` — re-check invariants (zero-sum, no-negative-assets, step-chain integrity, manifest singletons) against a persisted run.
- `analysis sim account list|add` and `analysis sim transfer list|add fixed|add scheduled` — inspect and append rows to `accounts.csv` / `calendar.csv` / `scheduled.csv` with tab completion and validation.

These mutating commands are convenience wrappers around the same CSV edits an AI assistant or a human editor would make directly: the AI can be asked in natural language to "add a $2,000 monthly transfer from checking to expenses starting July", and it will produce the equivalent row. The commands exist so the same change can be made deterministically from a shell, scripted in batches, or driven from completion-aware prompts when precise control is wanted.

## Consequences

### Positive

- The transfer-event-per-dollar-move model makes "no income in retirement" structurally falsifiable: any income-account flow is visible in one query. This is the central audience-trust goal.
- Determinism + dual-repo SHA stamping means any chart can be regenerated from its run row.
- Bounded set of `CalendarEvent` kinds (see ADR-003) keeps the simulator legible. A reader can enumerate all the ways a scenario can patch reality.
- Schema designed for ad-hoc SQL: AI-assisted querying is the primary v1 "reporting" surface, with named views for the common cases.
- Future ensemble layer is a pure scenario-generator over the same engine — no engine changes required.

### Negative / accepted

- Authoring overhead: every recurring item is a row in `scheduled.csv`, every assumption is data. Deliberate tradeoff — the visibility builds trust and doubles as a survivor checklist.
- Single currency (USD cents) in v1. JPY inheritance is documented as unaddressed in `data/known_exclusions.md`; revisit if/when it materializes.
- Forking is deferred. Scenarios are independent runs; conceptual forking lives in git. Schema affordance (`parent_run_id`, `fork_month`) is reserved.
- Holdings/lots are reserved schema (ADR-002) but the engine ignores them in v1. Cost basis tracked at account level via running `cost_basis` mutated by basis-tagged transfer events.
- Roth conversions, sub-monthly cadences, ISO valuation, and contribution-limit enforcement are explicitly out of scope for v1, with rationale captured in `data/known_exclusions.md`.

## Related ADRs

- ADR-002: Storage schema (full DDL, views, indexes).
- ADR-003: Amount-rule registry and Calendar-event-kind registry.
- ADR-004: Reproducibility contract and dual-repo gating.
