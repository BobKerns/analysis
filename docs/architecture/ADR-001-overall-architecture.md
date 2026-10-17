# ADR-001: Overall architecture of the finance simulator

- **Status:** Accepted
- **Date:** 2026-05-17
- **Amended:** see [Amendments](#amendments) at the end.
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

1. **Intent layer** — what was *decided*. Authored content: `AccountModel`, `ScheduledTransaction`, `Calendar`, `CalendarEvent`, `AuxiliaryTable`, `Scenario`, `YearToDate`.
2. **Ledger layer** — what *happened*. Run output: `SimulationRun`, `SimulationStep`, `LotState` (with `AccountState` as a view over it), `TransferEvent`, `TaxYearSummary`.

Every dollar movement in the ledger is a `TransferEvent` (date, from-account, to-account, amount, tax character, reason). Rules and calendar events live in the intent layer and **expand at run-time** into primitive `TransferEvent`s (a withdrawal from a basis-tracked lot into its return-of-capital and gain legs, a refill into the legs its chain draws), all linked via `reason_id` and `parent_event_id` so reports can group at either level. Nothing in the ledger is "virtual" or "derived on the fly" — what's in the table is the truth.

### Domain objects

| Object | Layer | Sharing | Description |
| --- | --- | --- | --- |
| `Person` | Intent | Shared | Owner identity (id, display, birthdate). Needed for RMD ages and life-transition events. |
| `Account` | Intent | Shared | One real-world place where money sits. `kind` ∈ {asset, liability, income, expense}; `subkind` carries tax/usage semantics; optional `owner`. Holds one or more `Lot`s. |
| `Lot` | Intent | Per-scenario | A position inside one account: symbol, acquired date, quantity, opening balance and cost basis ([ADR-005](ADR-005-lots-and-per-lot-state.md)). An account that is not split has one lot, `default`. Chains, rules and events refer to a lot as an account plus a lot column. |
| `AccountModel` | Intent | Shared | A named set of `Account`s with, per lot, opening balance and opening cost basis. Typically shared across scenarios. |
| `ScheduledTransaction` | Intent | Mostly shared | A *rule* that emits `TransferEvent`s on a cadence. Amount is computed by a typed `amount_rule` (fixed, percent-of-balance, RMD, SS benefit, …) so interest accrual, SS payments, withdrawals, and tax reconciliation all use the same machinery. |
| `Calendar` | Intent | Per-scenario | An ordered list of `CalendarEvent`s. Represents the assumptions being compared. |
| `CalendarEvent` | Intent | Per-scenario | A *patch* to the running scenario on a given date: change a rate, add/retire a scheduled transaction, swap an aux table, sell an asset, apply a life transition, etc. Bounded set of kinds; see [ADR-003](ADR-003-registries-and-csv-schemas.md). |
| `AuxiliaryTable` | Intent | Mostly shared | Versioned reference data: tax brackets, RMD divisors, SS taxation thresholds, inflation series, per-account return rates, filing status. Calendars can swap these by date. |
| `Scenario` | Intent | Per-comparison | `(AccountModel, Calendar, AuxTables, YearToDate, start, end)`. Identified by name; runs are reproducible from it. |
| `YearToDate` | Intent | Per-scenario | Partial-first-year catch-up facts ([ADR-003](ADR-003-registries-and-csv-schemas.md) §4.10): YTD wages/investment growth/SS already received, prior withholding, and fully-prior-year tax activity (e.g. loss carryforwards). Posted as balance-neutral `TransferEvent`s at run start. Account-level state facts (year-end balances for RMD, cost-basis estimates) live on `AccountModel` instead. |
| `SimulationRun` | Ledger | — | One execution. Stamps code/data versions, acting user, host, status. |
| `SimulationStep` | Ledger | — | One month. Links to previous step (nullable for initial state), to applied `CalendarEvent`s, and owns the `LotState` snapshot and `TransferEvent`s emitted in its window. |
| `LotState` | Ledger | — | `(step_id, lot_id) → balance, cost_basis, quantity`. The engine's state. Long-and-narrow for pivoting. `AccountState`, `(step_id, account_id) → balance, cost_basis`, is a view that sums it. |
| `TransferEvent` | Ledger | — | Atomic ledger entry. Tagged with `tax_character` so the retrospective tax scan can bucket without parallel bookkeeping. |
| `TaxYearSummary` | Ledger | — | Cache/audit row for each (run, year, bucket); always recomputable from `TransferEvent` + active aux tables. |

### Monthly step algorithm

```text
load initial LotState from AccountModel → step 0 (previous_step_id = NULL); post YearToDate catch-ups as balance-neutral TransferEvents
for each month m in [start..end]:
    apply Calendar events dated within m (in calendar order)
        → expand high-level events into primitive TransferEvents and rule mutations
        → record applied_calendar_event rows
    fire active ScheduledTransactions for m, ordered by rule priority:
        evaluate amount_rule(current_state, aux_tables, date)
        emit TransferEvent, mutate balances and cost basis
    snapshot LotState as end-of-month
    persist SimulationStep referencing previous step and applied events
```

Within-month ordering is **strictly by rule priority**, never by date. Priority bands are reserved:

- `100` accruals (interest, dividends, appreciation)
- `200` income (salary, SS, pensions)
- `300` expenses
- `400` tax accumulators / accruals
- `500` conversions / one-offs
- `900` terminal rules (`on_death`)

Liquidity is *not* a priority band. Refilling a drawn-down account is a
look-ahead check on each debit, and overflow is a month-end sweep, both driven
by the lot's own `refill_chain`/`overflow_chain` ([ADR-003](ADR-003-registries-and-csv-schemas.md) §4.3) rather than by a rule that
has to be ordered last — see [ADR-003](ADR-003-registries-and-csv-schemas.md) §4.7.

### Cross-cutting principles

These are project-wide rules that all subsequent ADRs and code reference:

1. **Deterministic engine.** Pure function of `(AccountModel, Calendar, AuxTables, YearToDate, start, end)`. No `datetime.now()` in engine paths, no RNG, no dict-iteration-order dependence, no floats for money. Money is `INTEGER` cents end-to-end.
2. **Month-granularity authoring; month-end dates internally.** CSVs use `YYYY-MM`. Loader pins to last day of month. No within-month sequencing semantics.
3. **Intent vs. ledger separation.** If a real-world decision has multiple legs and any leg depends on run-time state, it earns a `CalendarEvent` kind. Otherwise it's a primitive `one_off_transfer`. The set of kinds is bounded and reviewed (see [ADR-003](ADR-003-registries-and-csv-schemas.md)).
4. **Visible approximation beats invisible omission.** Zero-valued accounts for assets we choose not to model (ISOs, potential JPY inheritance); basis-with-sources lists for the home; `data/known_exclusions.md` for things deliberately out of scope.
5. **The scenario is a checklist.** Life events are dated rows a reader can scan: a rule's `start`/`end`, `calendar.csv` events (`retire_scheduled`, `set_amount`, `swap_aux_table` for a move), and `lifespan.csv`. A bundled event per transition (retire, predecease, relocate) was planned and dropped: each would be as specific as the rules it stood for.
6. **Schema is documentation.** Long-narrow tables, descriptive names, a `schema_overview` row per table for LLM priming. See [ADR-002](ADR-002-storage-schema.md).
7. **Hard errors on impossible states.** Unresolvable references, unknown user, dirty data without override → halt before the run starts, report enough context to fix the scenario. *Amended:* running an account negative is **not** in this category. The draft had insufficient funds halt the run; in practice that made a scenario un-runnable at the first shortfall, which is exactly when you most want to see the projection. A debit that a chain cannot cover posts anyway, drives the balance negative, raises a `RuntimeWarning` once per negative transition, and is recorded under `simulation_run.run_args_json.$.negative_balance_warnings`, with the refill chain it tried and what that chain supplied — visible and queryable, per principle 4, rather than fatal.

### Reproducibility and gating contract

Detail in [ADR-004](ADR-004-reproducibility-and-activation.md); summary here:

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

`PS1` is prefixed `[analysis:$ANALYSIS_DATA_REPO_NAME:$ANALYSIS_USER:<current scenario>]` (the scenario left out when none is current) so the active context is visible in a shared shell. `deactivate` reverses all of it.

### Prototype data

`prototype_data/` lives in the code repo and is a valid data repo from the engine's perspective. Contents are obviously fake (`Asset_Alice`, $100/$1000 amounts, fake birthdates). Used for tests, CI smoke runs, and demos. A pre-commit hook in the code repo blocks obviously-real patterns (SSN-shaped strings, etc.) to keep this repo personal-data-clean. The code repo itself is **not** the production data repo; production data lives in a separate private repo per the dual-repo design.

### Code organization

```text
src/analysis/sim/
    __init__.py
    model.py          # Person, Account, AccountModel, ScheduledTransaction dataclasses
    calendar.py       # Calendar, CalendarEvent
    aux.py            # tax brackets, RMD factors, SS thresholds, inflation series helpers
    rules.py          # amount_rule registry; see [ADR-003](ADR-003-registries-and-csv-schemas.md)
    expansions.py     # CalendarEvent expanders (one_off_transfer, transfer_balance, …)
    state.py          # in-memory lot state; invariants
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
- Bounded set of `CalendarEvent` kinds (see [ADR-003](ADR-003-registries-and-csv-schemas.md)) keeps the simulator legible. A reader can enumerate all the ways a scenario can patch reality.
- Schema designed for ad-hoc SQL: AI-assisted querying is the primary v1 "reporting" surface, with named views for the common cases.
- Future ensemble layer is a pure scenario-generator over the same engine — no engine changes required.

### Negative / accepted

- Authoring overhead: every recurring item is a row in `scheduled.csv`, every assumption is data. Deliberate tradeoff — the visibility builds trust and doubles as a survivor checklist.
- Single currency (USD cents) in v1. JPY inheritance is documented as unaddressed in `data/known_exclusions.md`; revisit if/when it materializes.
- Forking is deferred. Scenarios are independent runs; conceptual forking lives in git. Schema affordance (`parent_run_id`, `fork_month`) is reserved.
- Holdings and lots are built ([ADR-005](ADR-005-lots-and-per-lot-state.md)). Cost basis is tracked per lot, mutated by basis-tagged transfer events: a withdrawal splits its lot's basis pro-rata, and money moved into a basis-tracked lot (a purchase, an inheritance) adds to it. Growth is total return: dividends are not split out.
- Roth conversions, sub-monthly cadences, ISO valuation, and contribution-limit enforcement are explicitly out of scope for v1, with rationale captured in `data/known_exclusions.md`.

## Related ADRs

- [ADR-002](ADR-002-storage-schema.md): Storage schema (full DDL, views, indexes).
- [ADR-003](ADR-003-registries-and-csv-schemas.md): Amount-rule registry and Calendar-event-kind registry.
- [ADR-004](ADR-004-reproducibility-and-activation.md): Reproducibility contract and dual-repo gating.

## Amendments

Each entry changed this ADR's design. The sections it names were rewritten in place to describe the current design; git history has the earlier text.

- [ADR-005](ADR-005-lots-and-per-lot-state.md) (lots and per-lot state): accounts are split into lots, and the engine's state is per lot.
- [#105](https://github.com/BobKerns/analysis/issues/105): a negative-balance warning records the refill chain it tried and what that chain supplied (principle 7).
- [#197](https://github.com/BobKerns/analysis/issues/197): principle 5 no longer relies on `life_transition` events, which are dropped.
- [#214](https://github.com/BobKerns/analysis/issues/214): the prompt shows the current scenario (Activation).
