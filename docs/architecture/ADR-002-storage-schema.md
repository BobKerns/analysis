# ADR-002: Storage schema for the finance simulator

- **Status:** Accepted
- **Date:** 2026-05-17
- **Supersedes:** —
- **Amended:** see [Amendments](#amendments) at the end.
- **Related:** [ADR-001](ADR-001-overall-architecture.md) (overall architecture), [ADR-003](ADR-003-registries-and-csv-schemas.md) (rule and event-kind registries — referenced by `amount_rule_kind` and `calendar_event.kind` values used below), [ADR-004](ADR-004-reproducibility-and-activation.md) (reproducibility stamps on the run manifest).

## Context

The simulator persists **intent** (what a scenario says) and **ledger** (what one run produced) in **separate SQLite files**: one intent file per scenario and one ledger file per run. This avoids cross-scenario UNIQUE conflicts, keeps file management trivial (`ls`, `rm`, `cp` a single file), and makes individual runs shareable as standalone artifacts.

The schema must:

- Be queryable ad-hoc by both humans and LLMs (long-narrow tables, descriptive names, stable shape).
- Support `GROUP BY`-driven reporting at any grain: account, subkind, owner, year, reason.
- Make money operations exact (`INTEGER` cents, no floats).
- Reserve affordances for features deferred to later phases (forking) in the run-file layout. Lots are designed in [ADR-005](ADR-005-lots-and-per-lot-state.md).
- Carry enough provenance on every run file to regenerate any chart.
- Support cross-run comparison via `ATTACH DATABASE` rather than by sharing a database.

## Decision

### File layout

```text
data_repo/
    persons.csv             # shared base: owners with birthdates (and operator role)
    aux/
        federal_brackets.csv
        rmd_factors.csv
        inflation__cpi.csv
        ...                 # one CSV per aux_table.kind; `kind__series.csv`
    itemizations/
        <name>.csv          # expansions for `@name` monetary references
    scenarios/
        retire_2030/
            scenario.csv    # one-row scenario header (incl. parent_scenario)
            accounts.csv    # every Account this scenario models ([ADR-003](ADR-003-registries-and-csv-schemas.md) §4.2)
            account_values.csv # opening balances/bases, one row per lot ([ADR-003](ADR-003-registries-and-csv-schemas.md) §4.3, [ADR-005](ADR-005-lots-and-per-lot-state.md))
            scheduled.csv   # this scenario's ScheduledTransactions
            calendar.csv    # CalendarEvents
            lifespan.csv    # per-person final month
            year_to_date.csv # partial-first-year catch-up facts ([ADR-003](ADR-003-registries-and-csv-schemas.md) §4.10)
            chains/
                source/asset_checking_liquidity.csv   # named liquidity chains ([ADR-003](ADR-003-registries-and-csv-schemas.md) §4.7)
                destination/salary_split.csv
        retire_2032/
            ...             # a derived scenario names `parent_scenario` (plus any
                            # colon-separated mix-ins) in scenario.csv and ships
                            # only the files it overrides
    activate-analysis

state/
    intent/
        retire_2030.sqlite3   # built from scenarios/retire_2030/ (+ its parents) + shared base
        retire_2032.sqlite3
    runs/
        2026-05-17T193045Z_retire_2030_d4f7a2b.sqlite3
        2026-05-17T201122Z_retire_2032_d4f7a2b.sqlite3
```

- **Intent files** (`state/intent/<scenario>.sqlite3`) are derived caches. They share a stem with the scenario folder name. `analysis sim load <scenario>` rebuilds them silently from CSVs when `data_version` or `scenario_tree_id` no longer matches; the file is semantically equivalent to a set of virtual tables over the CSVs. It can be deleted at any time; the engine recreates it on next use.
- **Run files** (`state/runs/<timestamp>_<scenario>_<dataSHA>.sqlite3`) are produced by `analysis sim run <scenario>` and never modified after `status` reaches a terminal state. Filenames are ISO-Z timestamp + scenario name + short data SHA — sortable, tab-completable, immediately identifiable. To delete a run, `rm` its file.
- Each run file **embeds a snapshot of the intent it was built from**, with the same row ids the ledger references. The run file is therefore self-contained: shareable as a single attachment, queryable standalone, valid forever even if the intent cache is regenerated or the data repo evolves.
- Cross-run comparison opens a fresh in-memory DB and `ATTACH`es both run files:

  ```sql
  ATTACH DATABASE 'runs/A.sqlite3' AS a;
  ATTACH DATABASE 'runs/B.sqlite3' AS b;
  SELECT month, a_nw.net_worth AS nw_a, b_nw.net_worth AS nw_b,
         b_nw.net_worth - a_nw.net_worth AS delta
  FROM a.net_worth a_nw JOIN b.net_worth b_nw USING (month);
  ```

  Each run file defines the headline views (`net_worth`, `balance_by_subkind`, …) locally, so attached queries Just Work. SQLite's default `ATTACH` limit is 10 — sufficient for personal comparisons.

### Conventions

- **Money:** `INTEGER` cents. Negative values only for liability balances and explicitly-signed flows (none expected in v1).
- **Liability balances:** store outstanding debt magnitude. Payments into a liability are capped at the current outstanding balance; a payment that would overpay posts only the amount needed to reach zero, and later payments into an already-zero liability are no-ops. Deliberate credit balances should be modeled as a separate asset, not as a negative liability.
- **Dates:** `TEXT` in ISO 8601. Month-grain dates are always pinned to the last day of the month at load time (see [ADR-001](ADR-001-overall-architecture.md) cross-cutting principle 2).
- **JSON:** `TEXT` columns named `*_json`, parsed by the application layer.
- **Foreign keys:** declared with `REFERENCES … ON DELETE RESTRICT`. `PRAGMA foreign_keys = ON` set on every connection. No `ON DELETE CASCADE` is needed — deletion is per-file.
- **Identifiers:** integer surrogate `id` primary keys for everything addressable. Authored names are unique-indexed (`account.name`) so CSVs can reference by name and the loader resolves to ids. Ids assigned in the intent file are preserved in the run file's snapshot, keeping ledger FKs valid.
- **Append-only ledger:** ledger rows are written once and never updated.
- **No stored computed columns.** Anything derivable (net worth, totals, "is tax-deferred") is a view or query.
- **No `run_id` in the ledger.** Each run file holds exactly one run, so a `run_id` column on every ledger table would be a constant. `simulation_run` is a single-row manifest; the file itself is the run identifier.
- **One-row manifest tables** are not enforced by CHECK; convention only. The loader/engine write exactly one row.
- **Reserved account names.** A small set of names is reserved for synthetic accounts the engine references by string:
  - `Income_LoanOrigination` — the synthetic income-class account credited by `buy_asset` calendar-event expansions when a new liability appears mid-run (see [ADR-003](ADR-003-registries-and-csv-schemas.md) §3.8). The author lists it once in `accounts.csv` with `kind='income'`, `subkind='other_income'` if any scenario uses `buy_asset` with `new_liability_account`. Additional reserved names may be added in later ADRs.

### Placement summary

| Table | Intent file | Run file |
| --- | --- | --- |
| `person`, `account`, `security`, `security_allocation` | ✓ | ✓ (snapshot) |
| `account_lot` | ✓ | ✓ (snapshot) |
| `scheduled_transaction` | ✓ | ✓ (snapshot) |
| `chain`, `chain_steps` | ✓ | ✓ (snapshot) |
| `calendar`, `calendar_event` | ✓ | ✓ (snapshot) |
| `aux_table` | ✓ | ✓ (snapshot) |
| `scenario` (one row) | ✓ | ✓ (snapshot) |
| `person_lifespan` | ✓ | ✓ (snapshot) |
| `tag`, `account_tag`, `scheduled_transaction_tag`, `calendar_event_tag` | ✓ | ✓ (snapshot) |
| `year_to_date` | ✓ | ✓ (snapshot) |
| `ensemble_manifest`, `ensemble_draw` ([ADR-006](ADR-006-ensemble-scenarios.md)) | ✓ | ✓ (snapshot) |
| `intent_manifest` (one row) | ✓ | — |
| `simulation_run` (one row) | — | ✓ |
| `simulation_step`, `lot_state`, `transfer_event`, `applied_calendar_event`, `tax_year_summary`, `inflation_index_state` | — | ✓ |
| `schema_overview` (planned; not yet created) | ✓ | ✓ |
| Views (`account_state`, `net_worth`, etc.) | — (intent-only views permitted, none defined in v1) | ✓ |

The snapshot in a run file is byte-comparable to the corresponding intent file's tables at run time — and is the audit trail of what intent the run actually used.

### Intent layer DDL

The DDL in this section and the next three is copied from `src/analysis/sim/schema.py` (`SCHEMA_VERSION` 34) and must be kept identical to it; the code is authoritative. These tables exist in both intent files and (as snapshot) run files. Not in the DDL: `tag`, `account_tag`, `scheduled_transaction_tag` and `calendar_event_tag` are a generic labelling facility (role/name pairs) that no loader writes yet.

```sql
CREATE TABLE person (
    id          TEXT PRIMARY KEY,
    display     TEXT NOT NULL,
    birthdate   TEXT NOT NULL,
    role        TEXT,
    meta_json   TEXT NOT NULL DEFAULT '{}'
);

-- Names for the symbols lots hold (data repo securities.csv); shared reference
-- data like `person`. A symbol without a row is simply unnamed.
CREATE TABLE security (
    symbol  TEXT PRIMARY KEY,
    name    TEXT NOT NULL
);

-- What a symbol is made of, by asset class (data repo allocations.csv). One set
-- of weights per (symbol, as_of); as_of '' means it holds at every date. Weights
-- of one set sum to 1000000 (100%).
CREATE TABLE security_allocation (
    symbol       TEXT NOT NULL,
    as_of        TEXT NOT NULL DEFAULT '',
    asset_class  TEXT NOT NULL,
    weight_micros INTEGER NOT NULL,
    PRIMARY KEY (symbol, as_of, asset_class)
);

CREATE TABLE account (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL CHECK (kind IN ('asset','liability','income','expense')),
    subkind         TEXT NOT NULL,
    owner           TEXT REFERENCES person(id),
    tax_treatment   TEXT CHECK (tax_treatment IN
                        ('taxable','tax_deferred','tax_free','none')),
    opened          TEXT,
    closed          TEXT,
    meta_json       TEXT NOT NULL DEFAULT '{}',
    CHECK (
        (kind = 'asset' AND subkind IN ('cash','brokerage_taxable',
            'traditional_ira','roth_ira','traditional_401k','roth_401k','hsa',
            'real_estate','vehicle','other_asset'))
        OR (kind = 'liability' AND subkind IN ('mortgage','heloc','credit_card',
            'other_liability'))
        OR (kind = 'income' AND subkind IN ('salary','ss_benefit','pension','interest',
            'dividend','capital_gain','rental','other_income'))
        OR (kind = 'expense' AND subkind IN ('living','healthcare','housing',
            'interest_expense','taxes_federal','taxes_state','taxes_property',
            'other_expense'))
    )
);
CREATE INDEX idx_account_kind_subkind ON account(kind, subkind);
CREATE INDEX idx_account_owner ON account(owner);

-- Liquidity chains ([ADR-003](ADR-003-registries-and-csv-schemas.md) §4.7). A `chain` row is either a LEAF (account_id
-- set: the chain IS that account) or an INTERIOR named chain (account_id NULL,
-- with >=1 chain_steps rows, walked in step_order until a step's amount need
-- is satisfied). Every real account gets an implicit leaf chain at load time
-- (name = account.name), so `chain.name` is one namespace covering both
-- kinds -- account.refill_chain_id/overflow_chain_id and
-- chain_steps.draw_from_chain_id always reference `chain(id)`, never
-- `account(id)` directly, so no union-typed reference column is ever needed.
-- An interior chain's `role` says which way money moves through it: 'source'
-- (drawn from) or 'destination' (paid into). A leaf plays either part, so its
-- role is NULL.
CREATE TABLE chain (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    role       TEXT CHECK (role IN ('source', 'destination')),
    account_id INTEGER REFERENCES account(id),
    lot_id     INTEGER REFERENCES account_lot(id),
    CHECK ((account_id IS NULL) = (role IS NOT NULL))
);
CREATE INDEX idx_chain_account ON chain(account_id);

-- A step's allocation is `percent_micros` of the amount still outstanding
-- when the step is reached (default 100%, so an unallocated step consumes
-- whatever remains), capped at `maximum_cents` if set (an optional ceiling,
-- not a target -- the two may be combined). `series`/`cadence` inflate that
-- cap and are only meaningful when `maximum_cents` is set. There is
-- deliberately no per-step reserve: a step is "exhausted" once its account
-- (after any refill_chain draw) is at or below its own minimum_balance_cents
-- ([ADR-003](ADR-003-registries-and-csv-schemas.md) §4.7), at which point the still-outstanding amount falls through
-- to the next step.
CREATE TABLE chain_steps (
    id                 INTEGER PRIMARY KEY,
    chain_id           INTEGER NOT NULL REFERENCES chain(id),
    step_order         INTEGER NOT NULL,
    draw_from_chain_id INTEGER NOT NULL REFERENCES chain(id),
    maximum_cents      INTEGER,
    percent_micros     INTEGER NOT NULL DEFAULT 1000000,
    series             TEXT,
    cadence            TEXT CHECK (cadence IN ('monthly','annual')),
    CHECK (maximum_cents IS NULL OR maximum_cents >= 0),
    CHECK (percent_micros >= 0 AND percent_micros <= 1000000),
    CHECK (cadence IS NULL OR series IS NOT NULL),
    CHECK (series IS NULL OR maximum_cents IS NOT NULL),
    UNIQUE (chain_id, step_order)
);

-- One row per lot ([ADR-005](ADR-005-lots-and-per-lot-state.md)): a lot is a position inside one account, and the
-- engine keys balances, basis and quantity on `id`. Accounts that are not split
-- have a single lot labelled 'default'.
--
-- Balance management is per lot ([ADR-003](ADR-003-registries-and-csv-schemas.md) §4.3): a debit that would drop the
-- lot's balance below minimum_balance_cents triggers a look-ahead draw on
-- refill_chain_id BEFORE the debit posts (not a month-end reconciliation);
-- when it would exceed maximum_balance_cents (NULL = unbounded), the excess is
-- swept into overflow_chain_id at month end. Both chains are resolved
-- recursively, and may name other lots of the same account.
-- refill_target_cents/refill_amount_cents are mutually exclusive: the former
-- refills to that fixed level, the latter by that fixed amount (or enough to
-- reach minimum_balance_cents if that is greater); neither set means refill
-- exactly to minimum_balance_cents.
CREATE TABLE account_lot (
    id                      INTEGER PRIMARY KEY,
    account_id              INTEGER NOT NULL REFERENCES account(id),
    lot                     TEXT NOT NULL DEFAULT 'default',
    symbol                  TEXT,
    acquired                TEXT,
    quantity_micros         INTEGER,
    series                  TEXT,
    opening_balance         INTEGER NOT NULL,
    opening_cost_basis      INTEGER,
    prior_year_end_balance  INTEGER,
    minimum_balance_cents   INTEGER NOT NULL DEFAULT 0,
    refill_chain_id         INTEGER REFERENCES chain(id),
    refill_target_cents     INTEGER,
    refill_amount_cents     INTEGER,
    maximum_balance_cents   INTEGER,
    overflow_chain_id       INTEGER REFERENCES chain(id),
    UNIQUE (account_id, lot)
);

CREATE TABLE calendar (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE calendar_event (
    id           INTEGER PRIMARY KEY,
    calendar_id  INTEGER NOT NULL REFERENCES calendar(id),
    date         TEXT NOT NULL,
    kind         TEXT NOT NULL,
    name           TEXT,
    scheduled_name TEXT,
    account_1      TEXT,
    account_2      TEXT,
    series         TEXT,
    amount         INTEGER,
    param_date     TEXT,
    tax_character  TEXT,
    payload_json   TEXT,
    description    TEXT,
    -- Ensemble variation (ADR-006): a row with vary_distribution set has this
    -- row's own `amount` or `date` (per vary_target) drawn per member, instead
    -- of using the value above literally -- which is then just a placeholder.
    -- vary_low/high/median/sd/mode are in the *target* column's own unit: cents/
    -- micros for `amount` (whichever this row's own `amount` column would
    -- already mean for its `kind`), an ISO date string for `date`.
    -- vary_low/vary_high are `uniform`'s and `triangular`'s own bounds, and
    -- also double as `truncated_normal`'s optional clip bounds; any of
    -- vary_median/vary_sd/vary_low/vary_high may be inferred from the others
    -- when blank (distributions.resolve_truncated_normal_bounds), so
    -- truncated_normal is never actually unbounded. vary_median is the
    -- *truncated* distribution's median (#31). vary_skew/vary_tailweight are
    -- unitless sinh-arcsinh shape parameters (defaults 0 and 1: a normal);
    -- vary_mode is a triangular's peak.
    -- vary_ramp_months is only meaningful for vary_target='amount' on a
    -- kind='set_rate' row: it expands into that many additional set_rate rows,
    -- ramping to the drawn target (ADR-006 §4) instead of jumping to it.
    vary_target        TEXT,
    vary_distribution  TEXT,
    vary_low           TEXT,
    vary_high          TEXT,
    vary_median        TEXT,
    vary_sd            TEXT,
    vary_skew          REAL,
    vary_tailweight    REAL,
    vary_mode          TEXT,
    vary_ramp_months   INTEGER,
    -- ADR-008: this row's parent in a correlation tree (another varying
    -- row's or a latent_factor's name), and rho between their normal scores.
    vary_correlate_with TEXT,
    vary_correlation    REAL,
    -- #68: on a correlation tree's root only, the Student-t copula's degrees
    -- of freedom nu for the whole tree; NULL is the Gaussian copula.
    vary_tail_dependence REAL,
    -- ADR-006 §4a (#115): on a varying set_rate amount row, a year-by-year
    -- path around the drawn level: vary_process ('ar1'), its persistence phi
    -- in [0, 1), and the yearly shock size in micros. Expanded into yearly
    -- set_rate rows by the vary step, as a ramp is.
    vary_process        TEXT,
    vary_persistence    REAL,
    vary_innovation_sd  INTEGER
);
CREATE INDEX idx_calevt_date ON calendar_event(date);
CREATE INDEX idx_calevt_kind ON calendar_event(kind);

CREATE TABLE scheduled_transaction (
    id                      INTEGER PRIMARY KEY,
    name                    TEXT NOT NULL,
    from_account            INTEGER NOT NULL REFERENCES chain(id),
    to_account              INTEGER NOT NULL REFERENCES chain(id),
    cadence                 TEXT NOT NULL CHECK (cadence IN
                                ('monthly','biweekly','quarterly','annual','one_off')),
    month_of_year           INTEGER CHECK (month_of_year BETWEEN 1 AND 12),
    amount_rule_kind        TEXT NOT NULL,
    interest_account        TEXT,
    person                  TEXT REFERENCES person(id),
    -- `series` and `amount_cents` are interpreted per amount_rule_kind:
    -- inflation/rate/COLA series, and base amount/PIA/threshold respectively.
    series                  TEXT,
    rate_micros             INTEGER,
    claim_age               INTEGER,
    amount_cents            INTEGER,
    params_json             TEXT,
    base_date               TEXT,
    tax_character           TEXT NOT NULL CHECK (tax_character IN
                                ('ordinary','ltcg','stcg','gains','deferred',
                                 'ss_benefit','tax_free',
                                 'tax_deferred_in','tax_deferred_out',
                                 'return_of_capital','property_tax','none',
                                 'excluded_gain')),
    start                   TEXT NOT NULL,
    "end"                   TEXT,
    priority                INTEGER NOT NULL DEFAULT 500,
    description             TEXT,
    created_by_event_id     INTEGER REFERENCES calendar_event(id),
    meta_json               TEXT NOT NULL DEFAULT '{}',
    ytd_amount              INTEGER,
    ytd_preroll             INTEGER NOT NULL DEFAULT 0,
    inflation_cadence       TEXT CHECK (inflation_cadence IN ('monthly','annual')),
    tax_year                INTEGER,
    UNIQUE (name)
);
CREATE INDEX idx_sched_priority ON scheduled_transaction(priority);
CREATE INDEX idx_sched_created_by ON scheduled_transaction(created_by_event_id);

CREATE TABLE aux_table (
    id             INTEGER PRIMARY KEY,
    kind           TEXT NOT NULL,
    series         TEXT,
    effective_date TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    description    TEXT
);
CREATE INDEX idx_aux_lookup ON aux_table(kind, series, effective_date);

CREATE TABLE scenario (
    id                INTEGER PRIMARY KEY,
    name              TEXT NOT NULL UNIQUE,
    description       TEXT,
    calendar_id       INTEGER NOT NULL REFERENCES calendar(id),
    start             TEXT NOT NULL,
    "end"             TEXT NOT NULL,
    seed              INTEGER,
    -- JSON arrays of scenario names (ADR-003 §4.5): parent_scenario as
    -- authored (main parent, then mix-ins), and the whole load order, root
    -- first and this scenario last. Baked in so a run file can say what it
    -- inherited without the data repo.
    parents_json      TEXT NOT NULL DEFAULT '[]',
    load_order_json   TEXT NOT NULL DEFAULT '[]',
    created_at        TEXT NOT NULL,
    created_by        TEXT NOT NULL,
    meta_json         TEXT NOT NULL DEFAULT '{}'
);

-- Optional per-scenario lifespan override ([ADR-003](ADR-003-registries-and-csv-schemas.md) §4.9). When the
-- simulated month exceeds final_month, every scheduled_transaction
-- associated with this person (explicit `person` column, or owner of
-- from_account/to_account) stops firing for the remainder of the run.
CREATE TABLE person_lifespan (
    scenario_id INTEGER NOT NULL REFERENCES scenario(id),
    person      TEXT NOT NULL REFERENCES person(id),
    final_month TEXT NOT NULL,
    PRIMARY KEY (scenario_id, person)
);

-- Partial-first-year catch-up facts ([ADR-003](ADR-003-registries-and-csv-schemas.md) §4.10) -- consolidates what used
-- to be split across two mechanisms: a single row's own YTD amount (not tied
-- to any one scheduled_transaction's own amount, e.g. investment growth,
-- SS already received) and prior tax-year activity (superseded
-- `prior_tax_activity`: wages/withholding YTD, loss carryforwards from a
-- fully-prior tax year). Posted as balance-neutral transfer_event rows
-- (reason_kind='ytd') so tax/reporting queries see them without double-
-- counting against account_lot.opening_balance. `year` defaults to
-- scenario.start's year (dated the month before start); an explicit
-- different year is dated that year's Dec 31.
CREATE TABLE year_to_date (
    id            INTEGER PRIMARY KEY,
    scenario_id   INTEGER NOT NULL REFERENCES scenario(id),
    name          TEXT NOT NULL,
    from_account  INTEGER NOT NULL REFERENCES account(id),
    to_account    INTEGER NOT NULL REFERENCES account(id),
    from_lot_id   INTEGER NOT NULL REFERENCES account_lot(id),
    to_lot_id     INTEGER NOT NULL REFERENCES account_lot(id),
    amount        INTEGER NOT NULL,
    tax_character TEXT NOT NULL CHECK (tax_character IN
                        ('ordinary','ltcg','stcg','gains','deferred',
                         'ss_benefit','tax_free',
                         'tax_deferred_in','tax_deferred_out',
                         'return_of_capital','property_tax','none',
                         'excluded_gain')),
    year          INTEGER,
    description   TEXT,
    UNIQUE (scenario_id, name)
);

CREATE TABLE tag (
    id    INTEGER PRIMARY KEY,
    role  TEXT NOT NULL,
    name  TEXT NOT NULL,
    UNIQUE (role, name)
);

CREATE TABLE account_tag (
    account_id INTEGER NOT NULL REFERENCES account(id),
    tag_id     INTEGER NOT NULL REFERENCES tag(id),
    PRIMARY KEY (account_id, tag_id)
);

CREATE TABLE scheduled_transaction_tag (
    scheduled_transaction_id INTEGER NOT NULL REFERENCES scheduled_transaction(id),
    tag_id                   INTEGER NOT NULL REFERENCES tag(id),
    PRIMARY KEY (scheduled_transaction_id, tag_id)
);

CREATE TABLE calendar_event_tag (
    calendar_event_id INTEGER NOT NULL REFERENCES calendar_event(id),
    tag_id            INTEGER NOT NULL REFERENCES tag(id),
    PRIMARY KEY (calendar_event_id, tag_id)
);
```

### Ensemble provenance ([ADR-006](ADR-006-ensemble-scenarios.md))

Present (with a real row / rows) only in a member's own intent and run files; absent (empty) in the recipe's own intent, and in every ordinary, non-ensemble scenario.

```sql
CREATE TABLE ensemble_manifest (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    ensemble_name   TEXT NOT NULL,      -- the recipe scenario's name
    member_index    INTEGER NOT NULL,
    member_count    INTEGER NOT NULL,
    seed            INTEGER,            -- this member's derived seed; NULL for a sensitivity member (ADR-007 §5)
    recipe_tree_id  TEXT NOT NULL,      -- the recipe's own scenario_tree_id at generation time
    generated_at    TEXT NOT NULL,
    mode            TEXT NOT NULL DEFAULT 'monte_carlo'   -- ADR-007 §5
                        CHECK (mode IN ('monte_carlo','sensitivity'))
);

CREATE TABLE ensemble_draw (
    calendar_event_name TEXT NOT NULL,
    vary_target          TEXT NOT NULL,  -- 'amount' or 'date'
    drawn_value           TEXT NOT NULL, -- in the target column's own unit
    quantile             REAL,           -- ADR-007 §5: OAT override quantile; NULL for a draw or central value
    distribution         TEXT,           -- ADR-007 §5: the row's vary_distribution, which resolving clears
    z                    REAL,           -- ADR-008 §6: the row's normal score in its correlation tree; NULL outside one
    PRIMARY KEY (calendar_event_name)
);
```

### Intent manifest (intent files only)

Single-row table; the intent file's identity card.

```sql
CREATE TABLE intent_manifest (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    scenario_name   TEXT NOT NULL,
    schema_version  INTEGER NOT NULL,
    code_version    TEXT NOT NULL,
    data_version    TEXT NOT NULL,
    data_dirty      INTEGER NOT NULL,
    scenario_tree_id TEXT NOT NULL,
    data_repo_path  TEXT NOT NULL,
    data_repo_name  TEXT NOT NULL,
    loaded_at       TEXT NOT NULL,
    loaded_by       TEXT NOT NULL,
    host            TEXT NOT NULL
);
```

`scenario_tree_id` is a **git tree object SHA** covering the data repo paths this scenario's load can read: the scenario folder (`data_repo/scenarios/<name>/**`), the folders of every scenario it inherits from via `parent_scenario` (mix-ins included, [ADR-003](ADR-003-registries-and-csv-schemas.md) §4.5), and the shared base paths (`persons.csv`, `securities.csv`, `allocations.csv`, and the whole `aux/`, `prices/`, and `itemizations/` folders). The shared folders are scoped whole rather than per file read, so an edit to an unused file there also moves the id.

- **Clean** (`data_dirty = 0`): computed by `git ls-tree` against `HEAD` for those paths and combined with `git mktree` into a single tree object, or equivalently by writing them to a temporary index and calling `git write-tree`. Either way the result is a normal git tree SHA, addressable forever.
- **Dirty** (`data_dirty = 1`): the loader stages the working-tree contents of those paths into a **separate index file** (`GIT_INDEX_FILE=...` env var, never touching the user's index) and runs `git write-tree` against it. The output is a real tree SHA, identical to what a future commit of the same content would produce. Optionally the engine runs `git update-ref refs/analysis-dirty/<tree-id> <tree-id>` so the object is reachable from a ref and won't be garbage-collected before the run is forgotten.

This gives a single uniform identifier for "what intent did this run consume" — a content-addressed git object — with no bespoke hashing scheme. The cache-validity check for an intent file is `intent_manifest.scenario_tree_id == compute_tree_id(scenario)`.

### Ledger layer DDL (run files only)

```sql
CREATE TABLE simulation_run (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    scenario_name   TEXT NOT NULL,
    schema_version  INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK (status IN
                        ('running','succeeded','failed','halted')),
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    failure_reason  TEXT,
    code_version    TEXT NOT NULL,
    code_dirty      INTEGER NOT NULL,
    code_repo_path  TEXT NOT NULL,
    data_version    TEXT NOT NULL,
    data_dirty      INTEGER NOT NULL,
    scenario_tree_id TEXT NOT NULL,
    data_repo_path  TEXT NOT NULL,
    data_repo_name  TEXT NOT NULL,
    allow_dirty_data INTEGER NOT NULL DEFAULT 0,
    allow_dirty_code INTEGER NOT NULL DEFAULT 0,
    run_by          TEXT NOT NULL,
    host            TEXT NOT NULL,
    python_version  TEXT NOT NULL,
    run_args_json   TEXT NOT NULL DEFAULT '{}',
    engine_options_json TEXT NOT NULL DEFAULT '{}',
    parent_run_path TEXT,
    fork_month      TEXT
);

CREATE TABLE simulation_step (
    id               INTEGER PRIMARY KEY,
    step_index       INTEGER NOT NULL UNIQUE,
    month            TEXT NOT NULL,
    previous_step_id INTEGER REFERENCES simulation_step(id)
);
CREATE INDEX idx_step_month ON simulation_step(month);

CREATE TABLE lot_state (
    step_id         INTEGER NOT NULL REFERENCES simulation_step(id),
    lot_id          INTEGER NOT NULL REFERENCES account_lot(id),
    balance         INTEGER NOT NULL,
    cost_basis      INTEGER,
    quantity_micros INTEGER,
    PRIMARY KEY (step_id, lot_id)
);
CREATE INDEX idx_lot_state_lot ON lot_state(lot_id, step_id);

-- Account-level view of lot_state. Basis is NULL if any lot's basis is
-- unknown, rather than under-reporting.
CREATE VIEW account_state AS
SELECT ls.step_id, m.account_id, SUM(ls.balance) AS balance,
       CASE WHEN COUNT(ls.cost_basis) < COUNT(*) THEN NULL
            ELSE SUM(ls.cost_basis) END AS cost_basis
FROM lot_state ls
JOIN account_lot m ON m.id = ls.lot_id
GROUP BY ls.step_id, m.account_id;

CREATE TABLE transfer_event (
    id              INTEGER PRIMARY KEY,
    step_id         INTEGER NOT NULL REFERENCES simulation_step(id),
    date            TEXT NOT NULL,
    from_account    INTEGER NOT NULL REFERENCES account(id),
    to_account      INTEGER NOT NULL REFERENCES account(id),
    amount          INTEGER NOT NULL,
    -- Total amount asked for by the original triggering rule/event -- the
    -- SAME value on every leg of one draw, not that leg's own recursively-
    -- allocated slice, so a chain's legs can be summed/scanned to see how
    -- one request broke down across accounts. `remainder` is how much of
    -- that original request is still outstanding after this leg posted
    -- (0 once fully satisfied); for a single-leg, non-chain transfer,
    -- requested == amount and remainder == 0.
    requested_cents INTEGER NOT NULL,
    remainder_cents INTEGER NOT NULL,
    tax_year        INTEGER,
    tax_character   TEXT NOT NULL,
    reason_kind     TEXT NOT NULL CHECK (reason_kind IN
                        ('rule','initial','ytd','ytd_rule','refill','overflow')),
    reason_id       INTEGER,
    -- The event this leg derives from: a multi-leg rule's primary leg, a
    -- withdrawal's basis leg for its gain leg, or, for a reason_kind='refill'
    -- leg, the debit whose look-ahead refill drew it (a nested refill points
    -- at the refill leg that triggered it). The refill posts first, so its
    -- parent has the higher id.
    parent_event_id INTEGER REFERENCES transfer_event(id),
    from_lot_id     INTEGER NOT NULL REFERENCES account_lot(id),
    to_lot_id       INTEGER NOT NULL REFERENCES account_lot(id),
    meta_json       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_xfer_date     ON transfer_event(date);
CREATE INDEX idx_xfer_from     ON transfer_event(from_account);
CREATE INDEX idx_xfer_to       ON transfer_event(to_account);
CREATE INDEX idx_xfer_reason   ON transfer_event(reason_kind, reason_id);
CREATE INDEX idx_xfer_parent   ON transfer_event(parent_event_id);
CREATE INDEX idx_xfer_taxchar  ON transfer_event(tax_character);

CREATE TABLE applied_calendar_event (
    step_id           INTEGER NOT NULL REFERENCES simulation_step(id),
    calendar_event_id INTEGER NOT NULL REFERENCES calendar_event(id),
    PRIMARY KEY (step_id, calendar_event_id)
);

CREATE TABLE tax_year_summary (
    year          INTEGER NOT NULL,
    bucket        TEXT NOT NULL,
    amount        INTEGER NOT NULL,
    computed_at   TEXT NOT NULL,
    PRIMARY KEY (year, bucket)
);

CREATE TABLE inflation_index_state (
    step_id                  INTEGER NOT NULL REFERENCES simulation_step(id),
    series                   TEXT NOT NULL,
    cumulative_index_micros  INTEGER NOT NULL,
    PRIMARY KEY (step_id, series)
);
CREATE INDEX idx_inflation_state_series ON inflation_index_state(series, step_id);
```

### Holdings and lots

A lot is a row of `account_lot` (above), the engine's state is `lot_state`, and `transfer_event` records the lots a transfer moves between; [ADR-005](ADR-005-lots-and-per-lot-state.md) has the design.

### Schema priming (both file kinds)

```sql
CREATE TABLE schema_overview (
    table_name  TEXT PRIMARY KEY,
    description TEXT NOT NULL
);
```

Seeded rows (the loader inserts the subset relevant to that file kind; tables not present in the file get no row):

| table_name | description |
| --- | --- |
| `intent_manifest` | One row identifying the intent file: scenario name, code/data SHAs, CSV content hash, who/when loaded. (Intent files only.) |
| `simulation_run` | One row identifying this run: scenario name, code/data SHAs, status, who/when ran. (Run files only.) |
| `person` | Account owners; carries birthdate for RMD-age calculations and life-transition events. |
| `account` | One row per real-world place money sits; `kind` is the bookkeeping root (asset/liability/income/expense), `subkind` carries tax/usage semantics. |
| `account_lot` | One row per lot: symbol, acquired date, quantity, opening balance, opening cost basis, and prior-Dec-31 balance (for partial-first-year RMD) of each position in each account. |
| `security` | The name of each symbol lots hold (from `securities.csv`). |
| `security_allocation` | What each symbol is made of by asset class, as dated weight sets (from `allocations.csv`, [ADR-003](ADR-003-registries-and-csv-schemas.md) §4.1b). |
| `scheduled_transaction` | Recurring or one-off rule that emits transfer events on a cadence; `amount_rule_kind` selects the formula. |
| `chain` | A place money can be drawn from or deposited into: either a leaf (one lot) or a named, ordered, reusable liquidity chain, which is either a source (drawn from) or a destination (paid into). One namespace over both. |
| `chain_steps` | The ordered steps of an interior chain — which chain each step draws from, and the maximum/percent allocation limiting that step. |
| `calendar` | The patch sequence this scenario applies to the running model (one row per intent/run file). |
| `calendar_event` | One patch on one date; `kind` selects the semantics; the common parameters are columns (`scheduled_name`, `account_1`, `account_2`, `series`, `amount`, `param_date`, `tax_character`) and `payload_json` carries the rest. `name` is a stable identifier (required, unique, for a row varied under [ADR-006](ADR-006-ensemble-scenarios.md)); its `vary_*` columns mark the row's `amount` or `date` as drawn per ensemble member rather than literal. |
| `aux_table` | Versioned reference data: tax brackets, RMD divisors, inflation series, per-account rates, filing status. |
| `scenario` | The scenario this file represents (one row). |
| `person_lifespan` | Optional per-person final_month override for a scenario ([ADR-003](ADR-003-registries-and-csv-schemas.md) §4.9). |
| `year_to_date` | Partial-first-year catch-up facts ([ADR-003](ADR-003-registries-and-csv-schemas.md) §4.10): wages/investment growth/SS already received, prior withholding, and fully-prior-year tax activity (loss carryforwards etc.), posted as balance-neutral transfer_event rows. |
| `simulation_step` | One month inside the run; links to the previous step. |
| `lot_state` | Per-lot balance, cost basis and quantity at the end of one step; long-narrow for pivoting. `account_state` is a view summing it by account. |
| `transfer_event` | Atomic ledger entry: every dollar movement is exactly one row, tagged with tax character and reason. |
| `applied_calendar_event` | Junction table: which calendar events fired in which step. |
| `tax_year_summary` | Cache and audit trail for the retrospective tax scan, by (year, bucket). |
| `inflation_index_state` | Cumulative inflation index per series at the end of each step; the `inflation_index` and constant-dollar views read it. |
| `tag`, `account_tag`, `scheduled_transaction_tag`, `calendar_event_tag` | Role/name labels and their junctions to accounts, rules and calendar events. |
| `ensemble_manifest` | One row: which ensemble member this file is, its derived seed, member count, and the recipe's `scenario_tree_id` at generation time ([ADR-006](ADR-006-ensemble-scenarios.md)), and whether it's a Monte Carlo or sensitivity member (`mode`, [ADR-007](ADR-007-sensitivity-analysis.md)). Present only in a resolved member's intent/run files. |
| `ensemble_draw` | One row per resolved `vary_distribution` row: which `calendar_event` (`name`), which target (`amount`/`date`), and the drawn value ([ADR-006](ADR-006-ensemble-scenarios.md)); `quantile` is the OAT override quantile for a sensitivity member's perturbed row ([ADR-007](ADR-007-sensitivity-analysis.md)). Present only in a resolved member's intent/run files. |
| `schema_overview` | This table; one short description per table, for human and LLM readers. |

### Views

Defined in run files only. Intent files may define intent-only views in future; none in v1.

```sql
CREATE VIEW net_worth AS
SELECT s.month,
       SUM(CASE WHEN a.kind = 'asset'     THEN st.balance
                WHEN a.kind = 'liability' THEN -st.balance
                ELSE 0 END) AS net_worth
FROM simulation_step s
JOIN account_state st ON st.step_id = s.id
JOIN account a        ON a.id = st.account_id
GROUP BY s.month;

CREATE VIEW inflation_index AS
SELECT s.month,
       iis.series,
       iis.cumulative_index_micros,
       iis.cumulative_index_micros - 1000000 AS cumulative_inflation_micros
FROM simulation_step s
JOIN inflation_index_state iis ON iis.step_id = s.id;

CREATE VIEW net_worth_constant_dollars AS
SELECT nw.month,
       ii.series AS inflation_series,
       nw.net_worth AS nominal_cents,
       ii.cumulative_index_micros,
       (nw.net_worth * 1000000) / ii.cumulative_index_micros AS constant_cents
FROM net_worth nw
JOIN inflation_index ii ON ii.month = nw.month;

CREATE VIEW net_worth_constant_cpi AS
SELECT month,
       nominal_cents,
       cumulative_index_micros,
       constant_cents
FROM net_worth_constant_dollars
WHERE inflation_series = 'cpi';

CREATE VIEW balance_by_account AS
SELECT s.month, a.id AS account_id, a.name, a.kind, a.subkind,
       st.balance, st.cost_basis
FROM simulation_step s
JOIN account_state st ON st.step_id = s.id
JOIN account a        ON a.id = st.account_id
WHERE a.kind IN ('asset','liability');

-- One row per lot per month: the holdings inside each account.
CREATE VIEW balance_by_lot AS
SELECT s.month, a.id AS account_id, a.name AS account, m.id AS lot_id, m.lot,
       m.symbol, a.kind, a.subkind, ls.balance, ls.cost_basis, ls.quantity_micros
FROM simulation_step s
JOIN lot_state ls ON ls.step_id = s.id
JOIN account_lot m ON m.id = ls.lot_id
JOIN account a ON a.id = m.account_id
WHERE a.kind IN ('asset','liability');

CREATE VIEW balance_by_subkind AS
SELECT month, kind, subkind,
       SUM(CASE WHEN kind = 'asset' THEN balance
                WHEN kind = 'liability' THEN -balance
                ELSE 0 END) AS balance
FROM balance_by_account
GROUP BY month, kind, subkind;

CREATE VIEW balance_by_owner AS
SELECT s.month, a.owner,
       SUM(CASE WHEN a.kind = 'asset' THEN st.balance
                WHEN a.kind = 'liability' THEN -st.balance
                ELSE 0 END) AS balance
FROM simulation_step s
JOIN account_state st ON st.step_id = s.id
JOIN account a        ON a.id = st.account_id
WHERE a.kind IN ('asset','liability')
GROUP BY s.month, a.owner;

CREATE VIEW flow_by_account AS
SELECT CAST(substr(date, 1, 4) AS INTEGER) AS year,
       account_id, kind, subkind,
       inflow, outflow, inflow - outflow AS net
FROM (
    SELECT te.date, a.id AS account_id, a.kind, a.subkind,
           SUM(CASE WHEN te.to_account   = a.id THEN te.amount ELSE 0 END) AS inflow,
           SUM(CASE WHEN te.from_account = a.id THEN te.amount ELSE 0 END) AS outflow
    FROM transfer_event te
    JOIN account a ON a.id IN (te.from_account, te.to_account)
    GROUP BY te.date, a.id
);

CREATE VIEW flow_by_reason AS
SELECT CAST(substr(date, 1, 4) AS INTEGER) AS year,
       reason_kind, reason_id,
       SUM(amount) AS amount
FROM transfer_event
GROUP BY year, reason_kind, reason_id;
```

Cross-run comparisons access these views as `a.net_worth`, `b.net_worth`, etc. via `ATTACH` — no parameterized "compare" view exists. The `analysis sim compare` command supplies the join.

### Invariants and integrity rules

Enforced at run end (assertions; failures mark the run `failed`):

- **Bookkeeping zero-sum:** every `transfer_event` debits one account and credits another by the same amount, so the *total* of all balances — assets, liabilities, and the cumulative income/expense flow accounts with sign — is always zero. One query at the final step.
- **No negative asset balances** (per Q13): `SELECT 1 FROM account_state st JOIN account a ON a.id=st.account_id WHERE a.kind='asset' AND st.balance < 0 LIMIT 1` returns no rows.
- **Step chain integrity:** every `simulation_step` with `step_index > 0` has a non-NULL `previous_step_id` pointing to `step_index - 1`.
- **Transfer/step alignment:** every `transfer_event.step_id` corresponds to a `simulation_step` whose `month` equals `transfer_event.date` (both are month-end).
- **Manifest singleton:** `SELECT COUNT(*) FROM simulation_run` = 1 (similarly `intent_manifest` in intent files).

`analysis sim verify <runfile>` runs these as standalone checks against a persisted file.

### Index strategy summary

Indexes are deliberately conservative — chosen for the queries the views generate plus the common ad-hoc patterns ("balance of X over time", "all events from account X", "all events caused by rule Y"). Adding indexes later in response to slow queries is cheap; this avoids index bloat now.

### Schema changes

There are no migrations: the database files are always generated, never upgraded. The schema is applied whole (`apply_intent_schema` / `apply_run_schema`), and `SCHEMA_VERSION` in `schema.py` is bumped on each change. An intent file is rebuilt from the CSVs on every `sim load`, so it always matches the current code. A run file isn't upgraded: to compare runs across a schema change, re-run the older scenario. Generating from the CSVs every time is simpler and more reliable than maintaining migrations, and the CSVs are the source of truth anyway.

## Consequences

### Positive

- **No cross-scenario conflicts.** Each scenario's intent lives in its own file; UNIQUE constraints can't collide across scenarios.
- **Lifecycle ops are filesystem ops.** List = `ls`. Delete = `rm`. Share = copy. Archive = move. No central registry to keep consistent.
- **Self-contained runs.** A run file carries the intent it used. Charts can be regenerated from one file even after the data repo has moved on.
- **Comparisons via `ATTACH`** are unbounded by schema design and use the same views that single-file queries do.
- **Ledger tables shrank.** Dropping `run_id` simplifies indexes, views, and queries; less to get wrong in ad-hoc SQL.
- **One source of truth for provenance.** `intent_manifest` (intent) and `simulation_run` (run) are both single-row, easy to read at a glance, mutually consistent (`simulation_run` snapshots `intent_manifest`'s SHAs at run time).
- **AI-assisted querying remains the v1 reporting surface.** Long-narrow tables, descriptive names, `schema_overview` priming, predictable views.

### Negative / accepted

- **Snapshot duplication in run files.** Each run file carries a copy of the intent that produced it — a few KB per run. Trivial against the ledger size, and the price of self-containment.
- **Cross-file `ATTACH` schema skew.** Comparing runs from different schema versions can fail on views that reference dropped/renamed columns. Mitigation: re-run the older scenario under the current schema (§Schema changes); comparing run files across schema versions is best-effort.
- **JSON columns not strongly queryable.** SQLite's `json_extract` is available but ergonomically weak. The variable-shape pieces genuinely vary; `kind` columns let queries filter before extracting JSON.
- **No row-level CHECK on `payload_json` shape matching `kind`.** Validation lives in `loaders.py` and `expansions.py`. If shape drift becomes a problem, JSON-Schema validation at load time is the response.
- **`lot_state` writes O(lots × months) rows per run.** For ~75 lots × 15 years × 12 months that's ~13k rows — trivially small.
- **The `flow_by_account` view double-counts an account's own internal flows if `from_account = to_account`**, which the engine never emits. Documenting rather than guarding.
- **Reserved columns are dead weight in v1.** `parent_run_path`, `fork_month`, `seed`. Kept so forking can be added without changing the shape of the ledger tables, which dominate file size.

## Open questions deferred to later ADRs

- Exact vocabulary of `account.subkind` values — drafted in code, will become a checked list in [ADR-003](ADR-003-registries-and-csv-schemas.md).
- Exact vocabulary of `aux_table.kind` — same.
- Whether `tax_year_summary` should also store the inputs used (bracket-table aux_table id, filing status). Likely yes; added when the tax scan is implemented.
- ~~Exact CSV layout for the per-scenario folder~~ — **settled** in [ADR-003](ADR-003-registries-and-csv-schemas.md) §4 (`scenario.csv`, `accounts.csv`, `account_values.csv`, `scheduled.csv`, `calendar.csv`, `lifespan.csv`, `year_to_date.csv`, `chains/<role>/<name>.csv`).

## Amendments

Each entry changed this ADR's design. The sections it names were rewritten in place to describe the current design; git history has the earlier text.

- [ADR-008](ADR-008-correlated-parameters.md) (correlation columns, `ensemble_draw.z`; schema 30).
- [ADR-005](ADR-005-lots-and-per-lot-state.md) (lots): `account_model_member` is one row per lot, `lot_state` replaces the `account_state` table (now a view), `transfer_event` and `chain` reference lots, `security` is added, and the reserved `holding`/`lot` tables are removed.
- [ADR-006](ADR-006-ensemble-scenarios.md) (ensemble scenarios): `calendar_event` gains `name` and the `vary_*` columns; new tables `ensemble_manifest`, `ensemble_draw`.
- [ADR-007](ADR-007-sensitivity-analysis.md) (sensitivity analysis): `ensemble_manifest.mode`, `ensemble_draw.quantile`.
- [#59](https://github.com/BobKerns/analysis/issues/59): `scenario_tree_id` covers mix-in scenarios' folders too, and `scenario` records `parents_json` and `load_order_json` in place of the never-populated `parent_id` (schema 31).
- [#68](https://github.com/BobKerns/analysis/issues/68): `calendar_event.vary_tail_dependence` (schema 32).
- [#104](https://github.com/BobKerns/analysis/issues/104): a refill leg's `transfer_event.parent_event_id` points at the debit that triggered it.
- [#115](https://github.com/BobKerns/analysis/issues/115): `calendar_event.vary_process`, `vary_persistence` and `vary_innovation_sd` (schema 33).
- [#143](https://github.com/BobKerns/analysis/issues/143): `chain.kind` replaced by `chain.role` (`source`/`destination`, NULL for a leaf) (schema 34).
- [#160](https://github.com/BobKerns/analysis/issues/160): the single-row `account_model` table and every `model_id`/`account_model_id` column are dropped, and `account_model_member` is renamed `account_lot` (Intent layer DDL, placement summary, `schema_overview`; schema 35).
