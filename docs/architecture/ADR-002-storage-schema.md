# ADR-002: Storage schema for the finance simulator

- **Status:** Accepted
- **Date:** 2026-05-17
- **Supersedes:** —
- **Related:** ADR-001 (overall architecture), ADR-003 (rule and event-kind registries — referenced by `amount_rule_kind` and `calendar_event.kind` values used below), ADR-004 (reproducibility stamps on `simulation_run`).

## Context

The simulator persists both **intent** (what the scenario says) and **ledger** (what the run produced) in a single SQLite database under `state/sim.sqlite3`. The schema must:

- Be queryable ad-hoc by both humans and LLMs (long-narrow tables, descriptive names, stable shape).
- Support `GROUP BY`-driven reporting at any grain: account, subkind, owner, year, reason.
- Make money operations exact (`INTEGER` cents, no floats).
- Reserve affordances for features deferred to later phases (holdings/lots, forking) without future migration.
- Carry enough provenance on every run row to regenerate any chart.

## Decision

### Conventions

- **Money:** `INTEGER` cents. Negative values only for liability balances and explicitly-signed flows (none expected in v1).
- **Dates:** `TEXT` in ISO 8601. Month-grain dates are always pinned to the last day of the month at load time (see ADR-001 cross-cutting principle 2).
- **JSON:** `TEXT` columns named `*_json`, parsed by the application layer. Used for variable-shape payloads (calendar event payloads, amount-rule params, account/event metadata).
- **Foreign keys:** declared with `REFERENCES … ON DELETE RESTRICT` unless otherwise noted. `PRAGMA foreign_keys = ON` set on every connection.
- **Identifiers:** integer surrogate `id` primary keys for everything addressable. Authored names are unique-indexed (`account.name`, `scenario.name`) so CSVs can reference by name and the loader resolves to ids.
- **Append-only ledger:** rows in `simulation_step`, `account_state`, `transfer_event`, `applied_calendar_event`, `tax_year_summary` are written once and never updated. Deleting a `simulation_run` cascades through its ledger rows (the only `ON DELETE CASCADE` in the schema).
- **No stored computed columns.** Anything derivable (net worth, totals, "is tax-deferred") is a view or query, never a stored field.

### Intent layer DDL

```sql
-- People (owners of accounts; needed for RMD ages, life transitions)
CREATE TABLE person (
    id          TEXT PRIMARY KEY,             -- short id, e.g. 'rwk', 'mwk'
    display     TEXT NOT NULL,
    birthdate   TEXT NOT NULL,                -- ISO date
    meta_json   TEXT NOT NULL DEFAULT '{}'
);

-- Accounts: the four bookkeeping roots × subkind
CREATE TABLE account (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL CHECK (kind IN ('asset','liability','income','expense')),
    subkind         TEXT NOT NULL,            -- checking, traditional_ira, real_estate, salary, …
    owner           TEXT REFERENCES person(id),
    tax_treatment   TEXT CHECK (tax_treatment IN
                        ('taxable','tax_deferred','tax_free','none')),
    opened          TEXT,
    closed          TEXT,
    meta_json       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_account_kind_subkind ON account(kind, subkind);
CREATE INDEX idx_account_owner ON account(owner);

-- AccountModel: a named bundle of opening balances
CREATE TABLE account_model (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE account_model_member (
    model_id            INTEGER NOT NULL REFERENCES account_model(id),
    account_id          INTEGER NOT NULL REFERENCES account(id),
    opening_balance     INTEGER NOT NULL,     -- cents
    opening_cost_basis  INTEGER,              -- cents; NULL when basis is not tracked
    PRIMARY KEY (model_id, account_id)
);

-- ScheduledTransaction: a rule that emits transfers on a cadence
CREATE TABLE scheduled_transaction (
    id                      INTEGER PRIMARY KEY,
    model_id                INTEGER NOT NULL REFERENCES account_model(id),
    name                    TEXT NOT NULL,
    from_account            INTEGER NOT NULL REFERENCES account(id),
    to_account              INTEGER NOT NULL REFERENCES account(id),
    cadence                 TEXT NOT NULL CHECK (cadence IN
                                ('monthly','quarterly','annual','one_off')),
    month_of_year           INTEGER CHECK (month_of_year BETWEEN 1 AND 12),
    amount_rule_kind        TEXT NOT NULL,    -- see ADR-003
    amount_rule_params_json TEXT NOT NULL DEFAULT '{}',
    base_amount             INTEGER,          -- cents; semantic per rule
    base_date               TEXT,             -- for inflated rules
    inflation_series        TEXT,             -- 'fixed', 'cpi', 'cpi_medical', …
    tax_character           TEXT NOT NULL CHECK (tax_character IN
                                ('ordinary','ltcg','ss_benefit','tax_free',
                                 'tax_deferred_in','tax_deferred_out',
                                 'return_of_capital','none')),
    start                   TEXT NOT NULL,
    "end"                   TEXT,             -- NULL = open-ended
    priority                INTEGER NOT NULL DEFAULT 500,
    description             TEXT,
    meta_json               TEXT NOT NULL DEFAULT '{}',
    UNIQUE (model_id, name)
);
CREATE INDEX idx_sched_model ON scheduled_transaction(model_id);
CREATE INDEX idx_sched_priority ON scheduled_transaction(priority);

-- Ordered source list for waterfall_fill rules (and any future list-of-accounts rule)
CREATE TABLE scheduled_transaction_source (
    scheduled_transaction_id INTEGER NOT NULL
                                REFERENCES scheduled_transaction(id) ON DELETE CASCADE,
    position                 INTEGER NOT NULL,
    account_id               INTEGER NOT NULL REFERENCES account(id),
    PRIMARY KEY (scheduled_transaction_id, position)
);

-- Calendar: ordered patches to a scenario
CREATE TABLE calendar (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE calendar_event (
    id           INTEGER PRIMARY KEY,
    calendar_id  INTEGER NOT NULL REFERENCES calendar(id),
    date         TEXT NOT NULL,               -- month-end
    kind         TEXT NOT NULL,               -- see ADR-003
    payload_json TEXT NOT NULL DEFAULT '{}',
    description  TEXT
);
CREATE INDEX idx_calevt_cal_date ON calendar_event(calendar_id, date);
CREATE INDEX idx_calevt_kind ON calendar_event(kind);

-- Auxiliary tables: versioned reference data
-- kind ∈ {federal_brackets, state_brackets, standard_deduction,
--         ss_taxation_thresholds, rmd_factors, inflation, rate,
--         filing_status, rmd_start_age, …}
-- series is a subkey: for 'inflation' it's the series name ('cpi'); for 'rate'
-- it's the account name; NULL when not applicable.
CREATE TABLE aux_table (
    id             INTEGER PRIMARY KEY,
    kind           TEXT NOT NULL,
    series         TEXT,
    effective_date TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    description    TEXT
);
CREATE INDEX idx_aux_lookup ON aux_table(kind, series, effective_date);

-- Scenario: a comparison-able unit
CREATE TABLE scenario (
    id                INTEGER PRIMARY KEY,
    name              TEXT NOT NULL UNIQUE,
    description       TEXT,
    account_model_id  INTEGER NOT NULL REFERENCES account_model(id),
    calendar_id       INTEGER NOT NULL REFERENCES calendar(id),
    start             TEXT NOT NULL,
    "end"             TEXT NOT NULL,
    seed              INTEGER,                -- reserved for ensembles; unused in v1
    parent_id         INTEGER REFERENCES scenario(id),  -- provenance
    created_at        TEXT NOT NULL,
    created_by        TEXT NOT NULL,
    meta_json         TEXT NOT NULL DEFAULT '{}'
);

-- Priors: partial-year/seed facts that the run-start needs
CREATE TABLE prior_tax_activity (
    scenario_id INTEGER NOT NULL REFERENCES scenario(id),
    year        INTEGER NOT NULL,
    bucket      TEXT NOT NULL,                -- same vocab as tax_year_summary.bucket
    amount      INTEGER NOT NULL,
    source_note TEXT,
    PRIMARY KEY (scenario_id, year, bucket)
);

CREATE TABLE prior_account_fact (
    scenario_id INTEGER NOT NULL REFERENCES scenario(id),
    account_id  INTEGER NOT NULL REFERENCES account(id),
    fact_kind   TEXT NOT NULL,                -- ytd_401k_contribution, dec31_balance,
                                              -- cost_basis_estimate, …
    value       INTEGER NOT NULL,
    as_of       TEXT NOT NULL,
    source_note TEXT,
    PRIMARY KEY (scenario_id, account_id, fact_kind, as_of)
);
```

### Ledger layer DDL

```sql
CREATE TABLE simulation_run (
    id              INTEGER PRIMARY KEY,
    scenario_id     INTEGER NOT NULL REFERENCES scenario(id),
    status          TEXT NOT NULL CHECK (status IN
                        ('running','succeeded','failed')),
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    failure_reason  TEXT,                     -- e.g. 'WaterfallExhausted: 2032-08 short $4,200 …'
    -- reproducibility (see ADR-004)
    code_version    TEXT NOT NULL,            -- git rev-parse HEAD of code repo
    code_dirty      INTEGER NOT NULL,         -- 0/1
    data_version    TEXT NOT NULL,
    data_dirty      INTEGER NOT NULL,
    data_repo_path  TEXT NOT NULL,
    data_repo_name  TEXT NOT NULL,
    -- identity (audit only; excluded from determinism key)
    run_by          TEXT NOT NULL,            -- $ANALYSIS_USER
    host            TEXT NOT NULL,
    -- forking (reserved, unused in v1)
    parent_run_id   INTEGER REFERENCES simulation_run(id),
    fork_month      TEXT
);
CREATE INDEX idx_run_scenario ON simulation_run(scenario_id);
CREATE INDEX idx_run_started ON simulation_run(started_at);

CREATE TABLE simulation_step (
    id               INTEGER PRIMARY KEY,
    run_id           INTEGER NOT NULL REFERENCES simulation_run(id) ON DELETE CASCADE,
    step_index       INTEGER NOT NULL,        -- 0 = initial state, 1 = first month, …
    month            TEXT NOT NULL,           -- month-end date
    previous_step_id INTEGER REFERENCES simulation_step(id),  -- NULL only when step_index=0
    UNIQUE (run_id, step_index)
);
CREATE INDEX idx_step_month ON simulation_step(month);

CREATE TABLE account_state (
    step_id     INTEGER NOT NULL REFERENCES simulation_step(id) ON DELETE CASCADE,
    account_id  INTEGER NOT NULL REFERENCES account(id),
    balance     INTEGER NOT NULL,             -- cents
    cost_basis  INTEGER,                      -- cents; NULL when basis is not tracked
    PRIMARY KEY (step_id, account_id)
);
CREATE INDEX idx_state_account ON account_state(account_id, step_id);

-- Atomic ledger entries; one row per dollar movement
CREATE TABLE transfer_event (
    id              INTEGER PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES simulation_run(id) ON DELETE CASCADE,
    step_id         INTEGER NOT NULL REFERENCES simulation_step(id),
    date            TEXT NOT NULL,            -- month-end
    from_account    INTEGER NOT NULL REFERENCES account(id),
    to_account      INTEGER NOT NULL REFERENCES account(id),
    amount          INTEGER NOT NULL,         -- cents, always non-negative
    tax_character   TEXT NOT NULL,            -- same vocab as scheduled_transaction.tax_character
    reason_kind     TEXT NOT NULL CHECK (reason_kind IN
                        ('scheduled','calendar','expansion_child','initial')),
    reason_id       INTEGER,                  -- scheduled_transaction.id or calendar_event.id
    parent_event_id INTEGER REFERENCES transfer_event(id),  -- groups expansion siblings
    meta_json       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_xfer_run_date ON transfer_event(run_id, date);
CREATE INDEX idx_xfer_run_from ON transfer_event(run_id, from_account);
CREATE INDEX idx_xfer_run_to   ON transfer_event(run_id, to_account);
CREATE INDEX idx_xfer_reason   ON transfer_event(reason_kind, reason_id);
CREATE INDEX idx_xfer_parent   ON transfer_event(parent_event_id);
CREATE INDEX idx_xfer_taxchar  ON transfer_event(run_id, tax_character);

CREATE TABLE applied_calendar_event (
    step_id           INTEGER NOT NULL REFERENCES simulation_step(id) ON DELETE CASCADE,
    calendar_event_id INTEGER NOT NULL REFERENCES calendar_event(id),
    PRIMARY KEY (step_id, calendar_event_id)
);

-- Cache + audit trail for the retrospective tax scan
CREATE TABLE tax_year_summary (
    run_id        INTEGER NOT NULL REFERENCES simulation_run(id) ON DELETE CASCADE,
    year          INTEGER NOT NULL,
    bucket        TEXT NOT NULL,              -- ordinary, ltcg, ss_benefit_taxable,
                                              -- standard_deduction, federal_tax, state_tax, …
    amount        INTEGER NOT NULL,
    computed_at   TEXT NOT NULL,
    PRIMARY KEY (run_id, year, bucket)
);
```

### Reserved schema (v1 unused; populated in later phases)

```sql
-- Holdings + lots: enable rebalancing analysis later without migration
CREATE TABLE holding (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES account(id),
    symbol      TEXT NOT NULL,
    name        TEXT,
    asset_class TEXT,
    meta_json   TEXT NOT NULL DEFAULT '{}',
    UNIQUE (account_id, symbol)
);

CREATE TABLE lot (
    id                INTEGER PRIMARY KEY,
    holding_id        INTEGER NOT NULL REFERENCES holding(id),
    acquired          TEXT NOT NULL,
    quantity_micros   INTEGER NOT NULL,       -- shares × 1e6 (fractional shares)
    cost_basis_total  INTEGER NOT NULL,       -- cents
    source_note       TEXT
);

-- transfer_event optionally references holdings/lots; columns left for the
-- holdings phase. Until then they are NULL and the engine doesn't read them.
ALTER TABLE transfer_event ADD COLUMN from_holding_id INTEGER REFERENCES holding(id);
ALTER TABLE transfer_event ADD COLUMN to_holding_id   INTEGER REFERENCES holding(id);
```

The two `ALTER TABLE`s are stated above for clarity but will be inlined into the
initial `CREATE TABLE transfer_event` so v1 ships with one migration, not two.

### Operational tables

```sql
-- Reload-skip cache: lets `analysis sim load` no-op when data SHA matches
CREATE TABLE data_load (
    id               INTEGER PRIMARY KEY,
    data_version     TEXT NOT NULL,
    data_repo_name   TEXT NOT NULL,
    account_model_id INTEGER REFERENCES account_model(id),
    calendar_id      INTEGER REFERENCES calendar(id),
    loaded_at        TEXT NOT NULL,
    loaded_by        TEXT NOT NULL
);
CREATE INDEX idx_dataload_version ON data_load(data_version, data_repo_name);

-- Schema priming: one row per table, one sentence, for human + LLM readers
CREATE TABLE schema_overview (
    table_name  TEXT PRIMARY KEY,
    description TEXT NOT NULL
);
```

Seeded `schema_overview` rows (written by the migration):

| table_name | description |
| --- | --- |
| `person` | Account owners; carries birthdate for RMD-age calculations and life-transition events. |
| `account` | One row per real-world place money sits; `kind` is the bookkeeping root (asset/liability/income/expense), `subkind` carries tax/usage semantics. |
| `account_model` | A named bundle of accounts with opening balances; shared across scenarios. |
| `account_model_member` | Opening balance and cost basis for each account in a model. |
| `scheduled_transaction` | Recurring or one-off rule that emits transfer events on a cadence; `amount_rule_kind` selects the formula. |
| `scheduled_transaction_source` | Ordered list of source accounts for rules that draw from multiple places (e.g. `waterfall_fill`). |
| `calendar` | A named sequence of patches a scenario applies to the running model. |
| `calendar_event` | One patch on one date; `kind` selects the semantics, `payload_json` carries the parameters. |
| `aux_table` | Versioned reference data: tax brackets, RMD divisors, inflation series, per-account rates, filing status. |
| `scenario` | A comparable unit: account model + calendar + start/end + creation metadata. |
| `prior_tax_activity` | Year-to-date taxable activity preceding a partial-first-year run, so April reconciliation is correct. |
| `prior_account_fact` | Other partial-period facts: YTD contributions, year-end balances for RMD, cost-basis estimates. |
| `simulation_run` | One execution of a scenario; carries reproducibility stamps (code/data SHA) and acting-user audit fields. |
| `simulation_step` | One month inside a run; links to the previous step and to the calendar events that fired in this window. |
| `account_state` | Per-account balance and cost basis at the end of one step; long-narrow for pivoting. |
| `transfer_event` | Atomic ledger entry: every dollar movement is exactly one row, tagged with tax character and reason. |
| `applied_calendar_event` | Junction table: which calendar events fired in which step. |
| `tax_year_summary` | Cache and audit trail for the retrospective tax scan, by (run, year, bucket). |
| `holding` | Per-account holdings (reserved; unused in v1, populated when rebalancing analysis is added). |
| `lot` | Tax lots within a holding (reserved; unused in v1). |
| `data_load` | Records which data-repo SHA was last loaded, to support skip-reload on match. |
| `schema_overview` | This table; one short description per table, for human and LLM readers. |

### Views

All views are `CREATE VIEW … AS SELECT …` over the ledger; they are the canonical definitions of the headline metrics.

```sql
-- Net worth = sum of asset balances minus sum of liability balances.
-- Income/expense flows are excluded (they're flow accounts, not stock).
CREATE VIEW net_worth AS
SELECT s.run_id, s.month,
       SUM(CASE WHEN a.kind = 'asset'     THEN st.balance
                WHEN a.kind = 'liability' THEN -st.balance
                ELSE 0 END) AS net_worth
FROM simulation_step s
JOIN account_state st ON st.step_id = s.id
JOIN account a        ON a.id = st.account_id
GROUP BY s.run_id, s.month;

-- Per-account balance over time (asset and liability accounts).
CREATE VIEW balance_by_account AS
SELECT s.run_id, s.month, a.id AS account_id, a.name, a.kind, a.subkind,
       st.balance, st.cost_basis
FROM simulation_step s
JOIN account_state st ON st.step_id = s.id
JOIN account a        ON a.id = st.account_id
WHERE a.kind IN ('asset','liability');

-- Aggregated by subkind: "tax-deferred vs. taxable vs. Roth vs. cash vs. real estate".
CREATE VIEW balance_by_subkind AS
SELECT run_id, month, kind, subkind,
       SUM(CASE WHEN kind = 'asset' THEN balance
                WHEN kind = 'liability' THEN -balance
                ELSE 0 END) AS balance
FROM balance_by_account
GROUP BY run_id, month, kind, subkind;

-- Aggregated by owner.
CREATE VIEW balance_by_owner AS
SELECT s.run_id, s.month, a.owner,
       SUM(CASE WHEN a.kind = 'asset' THEN st.balance
                WHEN a.kind = 'liability' THEN -st.balance
                ELSE 0 END) AS balance
FROM simulation_step s
JOIN account_state st ON st.step_id = s.id
JOIN account a        ON a.id = st.account_id
WHERE a.kind IN ('asset','liability')
GROUP BY s.run_id, s.month, a.owner;

-- Annual cashflow per account: in, out, net. Useful for income/expense roots.
CREATE VIEW flow_by_account AS
SELECT run_id,
       CAST(substr(date, 1, 4) AS INTEGER) AS year,
       account_id, kind, subkind,
       inflow, outflow, inflow - outflow AS net
FROM (
    SELECT te.run_id, te.date, a.id AS account_id, a.kind, a.subkind,
           SUM(CASE WHEN te.to_account   = a.id THEN te.amount ELSE 0 END) AS inflow,
           SUM(CASE WHEN te.from_account = a.id THEN te.amount ELSE 0 END) AS outflow
    FROM transfer_event te
    JOIN account a ON a.id IN (te.from_account, te.to_account)
    GROUP BY te.run_id, te.date, a.id
);

-- Annual totals grouped by the rule or event that caused the transfer.
CREATE VIEW flow_by_reason AS
SELECT run_id,
       CAST(substr(date, 1, 4) AS INTEGER) AS year,
       reason_kind, reason_id,
       SUM(amount) AS amount
FROM transfer_event
GROUP BY run_id, year, reason_kind, reason_id;
```

**No parameterized comparison view.** `net_worth_compare` was considered as a view but SQLite views can't take parameters, and an all-pairs cross-product is wasteful. `analysis sim compare <a> <b>` instead executes a saved query template against `net_worth` filtered to the two run ids.

### Invariants and integrity rules

Enforced at run end (assertions; failures mark the run `failed`):

- **Bookkeeping zero-sum:** `SELECT SUM(balance × sign) FROM account_state WHERE step_id = LAST` should equal zero, where `sign = +1` for assets, `-1` for liabilities, and income/expense are summed cumulatively over the run with sign matching their flow direction. (Concretely: every `transfer_event` debits one account and credits another by the same amount, so the *total* of all balances — including the cumulative income and expense flow accounts — is always zero. The check is one query.)
- **No negative asset balances** (per Q13): `SELECT 1 FROM account_state st JOIN account a WHERE a.kind='asset' AND st.balance < 0 LIMIT 1` returns no rows.
- **Step chain integrity:** every `simulation_step` with `step_index > 0` has a non-NULL `previous_step_id` pointing to `step_index - 1` in the same run.
- **Transfer/step alignment:** every `transfer_event.step_id` corresponds to a `simulation_step` whose `month` matches `substr(transfer_event.date, 1, 7)`.

A `analysis sim verify <run_id>` subcommand runs these as standalone checks against persisted data.

### Index strategy summary

The indexes above are deliberately conservative — chosen for the queries the views above generate, plus the common ad-hoc patterns ("balance of X over time", "all events from account X", "all events caused by rule Y"). The schema is small enough that adding indexes later in response to slow queries is cheap; this avoids index bloat now.

### Migrations

v1 ships a single migration `001_init.sql` that creates everything above and seeds `schema_overview`. Migrations live in `src/analysis/sim/migrations/` and are applied in numeric order at engine startup; a `_schema_version` table tracks the highest applied id. Until v1 ships there is no v0 to migrate from — `001_init.sql` is the baseline.

## Consequences

### Positive

- One table per concept; reports are SQL, not application code. AI-assisted querying works out of the box.
- Every dollar movement is one row; reason-based and account-based grouping are symmetric. Bundled decisions (`sell_asset`, `life_transition`) are reconstructable via `reason_id` and `parent_event_id`.
- Cost basis tracked at the account level today, holdings/lots reserved for later — same `transfer_event` rows just gain non-NULL `*_holding_id` when that phase arrives. No data migration.
- Reproducibility stamps live on `simulation_run`, separate from the determinism inputs (`scenario` + intent tables) — clean conceptual split between "what was simulated" and "who/when/where simulated it".
- All money is `INTEGER` cents; eliminates a whole class of float-drift bugs and makes LLM-generated SQL more reliable.

### Negative / accepted

- JSON in `payload_json` / `meta_json` / `amount_rule_params_json` is not queryable with strong typing. SQLite's `json_extract` is available but ergonomically weak. Tradeoff accepted: the variable-shape pieces genuinely vary, and `kind` columns let queries filter before extracting JSON.
- No row-level CHECK on `payload_json` shape matching `kind`. Validation lives in `loaders.py` and `expansions.py`. If shape drift becomes a problem, JSON Schema validation at load time is the response, not stored constraints.
- `account_state` writes O(accounts × months) rows per run. For ~40 accounts × 40 years × 12 months that's ~19k rows per run — trivially small. Stated here to document we considered it.
- The `flow_by_account` view double-counts an account's own internal flows if `from_account = to_account`, which the engine never emits. Documenting rather than guarding.
- Reserved `parent_run_id`, `fork_month`, `seed`, `holding`, `lot`, and the `*_holding_id` columns are dead weight in v1. Worth it to avoid future migrations of the ledger tables (which will be the biggest by row count).

## Open questions deferred to later ADRs

- Exact vocabulary of `account.subkind` values — drafted in code, will become a checked list in ADR-003 alongside the rule registry.
- Exact vocabulary of `aux_table.kind` — same.
- Whether `tax_year_summary` should also store the inputs used (bracket-table aux_table id, filing status) for audit. Likely yes; added when the tax scan is implemented.
