# ADR-002: Storage schema for the finance simulator

- **Status:** Accepted
- **Date:** 2026-05-17
- **Supersedes:** —
- **Related:** ADR-001 (overall architecture), ADR-003 (rule and event-kind registries — referenced by `amount_rule_kind` and `calendar_event.kind` values used below), ADR-004 (reproducibility stamps on the run manifest).

## Context

The simulator persists **intent** (what a scenario says) and **ledger** (what one run produced) in **separate SQLite files**: one intent file per scenario and one ledger file per run. This avoids cross-scenario UNIQUE conflicts, keeps file management trivial (`ls`, `rm`, `cp` a single file), and makes individual runs shareable as standalone artifacts.

The schema must:

- Be queryable ad-hoc by both humans and LLMs (long-narrow tables, descriptive names, stable shape).
- Support `GROUP BY`-driven reporting at any grain: account, subkind, owner, year, reason.
- Make money operations exact (`INTEGER` cents, no floats).
- Reserve affordances for features deferred to later phases (holdings/lots, forking) without future migration.
- Carry enough provenance on every run file to regenerate any chart.
- Support cross-run comparison via `ATTACH DATABASE` rather than by sharing a database.

## Decision

### File layout

```text
data_repo/
    accounts.csv            # shared base: every Account in the household
    persons.csv             # shared base: owners with birthdates
    aux/
        federal_brackets.csv
        rmd_factors.csv
        inflation_series.csv
        ...                 # one CSV per aux_table.kind
    account_models/
        standard.csv        # member rows: account_name, opening_balance, opening_cost_basis
        pre_house_sale.csv  # alternate
    scenarios/
        retire_2030/
            scenario.csv    # one-row scenario header
            scheduled.csv   # this scenario's ScheduledTransactions
            waterfall_sources.csv
            calendar.csv    # CalendarEvents
            year_to_date.csv # partial-first-year catch-up facts (ADR-003 §4.10)
        retire_2032/
            ...
    activate

state/
    intent/
        retire_2030.sqlite3   # built from scenarios/retire_2030/ + shared base
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
- **Dates:** `TEXT` in ISO 8601. Month-grain dates are always pinned to the last day of the month at load time (see ADR-001 cross-cutting principle 2).
- **JSON:** `TEXT` columns named `*_json`, parsed by the application layer.
- **Foreign keys:** declared with `REFERENCES … ON DELETE RESTRICT`. `PRAGMA foreign_keys = ON` set on every connection. No `ON DELETE CASCADE` is needed — deletion is per-file.
- **Identifiers:** integer surrogate `id` primary keys for everything addressable. Authored names are unique-indexed (`account.name`) so CSVs can reference by name and the loader resolves to ids. Ids assigned in the intent file are preserved in the run file's snapshot, keeping ledger FKs valid.
- **Append-only ledger:** ledger rows are written once and never updated.
- **No stored computed columns.** Anything derivable (net worth, totals, "is tax-deferred") is a view or query.
- **No `run_id` in the ledger.** Each run file holds exactly one run, so a `run_id` column on every ledger table would be a constant. `simulation_run` is a single-row manifest; the file itself is the run identifier.
- **One-row manifest tables** are not enforced by CHECK; convention only. The loader/engine write exactly one row.
- **Reserved account names.** A small set of names is reserved for synthetic accounts the engine references by string:
  - `Income_LoanOrigination` — the synthetic income-class account credited by `buy_asset` calendar-event expansions when a new liability appears mid-run (see ADR-003 §3.8). The author lists it once in `accounts.csv` with `kind='income'`, `subkind='other_income'` if any scenario uses `buy_asset` with `new_liability_account`. Additional reserved names may be added in later ADRs.

### Placement summary

| Table | Intent file | Run file |
| --- | --- | --- |
| `person`, `account` | ✓ | ✓ (snapshot) |
| `account_model`, `account_model_member` | ✓ | ✓ (snapshot) |
| `scheduled_transaction`, `scheduled_transaction_source` | ✓ | ✓ (snapshot) |
| `calendar`, `calendar_event` | ✓ | ✓ (snapshot) |
| `aux_table` | ✓ | ✓ (snapshot) |
| `scenario` (one row) | ✓ | ✓ (snapshot) |
| `person_lifespan` | ✓ | ✓ (snapshot) |
| `year_to_date` | ✓ | ✓ (snapshot) |
| `intent_manifest` (one row) | ✓ | — |
| `simulation_run` (one row) | — | ✓ |
| `simulation_step`, `account_state`, `transfer_event`, `applied_calendar_event`, `tax_year_summary` | — | ✓ |
| `holding`, `lot` (reserved) | ✓ | ✓ (snapshot) |
| `schema_overview` | ✓ | ✓ |
| Views (`net_worth`, etc.) | — (intent-only views permitted, none defined in v1) | ✓ |

The snapshot in a run file is byte-comparable to the corresponding intent file's tables at run time — and is the audit trail of what intent the run actually used.

### Intent layer DDL

These tables exist in both intent files and (as snapshot) run files.

```sql
-- People (owners of accounts; needed for RMD ages, life transitions).
-- Doubles as the operator-identity list: `$ANALYSIS_USER` must match a row id here.
CREATE TABLE person (
    id          TEXT PRIMARY KEY,             -- short id, e.g. 'rwk', 'mwk'
    display     TEXT NOT NULL,
    birthdate   TEXT NOT NULL,                -- ISO date
    role        TEXT,                         -- informational; e.g. 'primary', 'secondary';
                                              -- NULL for persons-not-operators (deceased
                                              -- ancestors, household pseudo-rows, future heirs)
    meta_json   TEXT NOT NULL DEFAULT '{}'
);

-- Accounts: the four bookkeeping roots × subkind
CREATE TABLE account (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL CHECK (kind IN ('asset','liability','income','expense')),
    subkind         TEXT NOT NULL,            -- see ADR-003 §1
    owner           TEXT REFERENCES person(id),
    tax_treatment   TEXT CHECK (tax_treatment IN
                        ('taxable','tax_deferred','tax_free','none')),
    opened          TEXT,
    closed          TEXT,
    meta_json       TEXT NOT NULL DEFAULT '{}',
    CHECK (
        (kind = 'asset' AND subkind IN ('checking','savings','brokerage_taxable',
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

-- AccountModel: one row per intent file (the model this scenario uses)
CREATE TABLE account_model (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE account_model_member (
    model_id                INTEGER NOT NULL REFERENCES account_model(id),
    account_id              INTEGER NOT NULL REFERENCES account(id),
    opening_balance         INTEGER NOT NULL,     -- cents
    opening_cost_basis      INTEGER,              -- cents; NULL when basis is not tracked
    prior_year_end_balance  INTEGER,              -- cents at Dec 31 preceding scenario.start;
                                                  -- required for traditional_ira / traditional_401k
                                                  -- so the partial-first-year RMD has a Dec-31
                                                  -- balance to divide by (after year 1 the engine
                                                  -- reads it from account_state). NULL otherwise.
    PRIMARY KEY (model_id, account_id)
);

-- ScheduledTransaction: a rule that emits transfers on a cadence.
-- Authored in scheduled.csv, or synthesized at expansion time by a calendar event
-- (in which case created_by_event_id points to that event — see ADR-003 §2 invariant).
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
    amount_cents            INTEGER,          -- cents in DB; CSV `amount` uses decimal dollars (loader converts). Meaning per amount_rule_kind
    base_date               TEXT,             -- for inflated rules
    series                  TEXT,             -- inflation/rate/COLA series per amount_rule_kind; 'fixed' = no inflation
    tax_character           TEXT NOT NULL CHECK (tax_character IN
                                ('ordinary','ltcg','gains','deferred',
                                 'ss_benefit','tax_free',
                                 'tax_deferred_in','tax_deferred_out',
                                 'return_of_capital','property_tax','none')),
    start                   TEXT NOT NULL,
    "end"                   TEXT,             -- NULL = open-ended
    priority                INTEGER NOT NULL DEFAULT 500,
    description             TEXT,
    created_by_event_id     INTEGER REFERENCES calendar_event(id),
                                              -- NULL for authored rows; set for
                                              -- rules synthesized by calendar-event expansion
    meta_json               TEXT NOT NULL DEFAULT '{}',
    UNIQUE (model_id, name)
);
CREATE INDEX idx_sched_priority ON scheduled_transaction(priority);
CREATE INDEX idx_sched_created_by ON scheduled_transaction(created_by_event_id);

-- Ordered source list for waterfall_fill rules (and any future list-of-accounts rule)
CREATE TABLE scheduled_transaction_source (
    scheduled_transaction_id INTEGER NOT NULL REFERENCES scheduled_transaction(id),
    position                 INTEGER NOT NULL,
    account_id               INTEGER NOT NULL REFERENCES account(id),
    PRIMARY KEY (scheduled_transaction_id, position)
);

-- Calendar: ordered patches to a scenario (one calendar per intent file)
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
CREATE INDEX idx_calevt_date ON calendar_event(date);
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

-- Scenario: exactly one row per intent file
CREATE TABLE scenario (
    id                INTEGER PRIMARY KEY,
    name              TEXT NOT NULL UNIQUE,
    description       TEXT,
    account_model_id  INTEGER NOT NULL REFERENCES account_model(id),
    calendar_id       INTEGER NOT NULL REFERENCES calendar(id),
    start             TEXT NOT NULL,
    "end"             TEXT NOT NULL,
    seed              INTEGER,                -- reserved for ensembles; unused in v1
    parent_id         INTEGER REFERENCES scenario(id),  -- provenance (cross-file by name in practice)
    created_at        TEXT NOT NULL,
    created_by        TEXT NOT NULL,
    meta_json         TEXT NOT NULL DEFAULT '{}'
);
```

### Intent manifest (intent files only)

Single-row table; the intent file's identity card.

```sql
CREATE TABLE intent_manifest (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    scenario_name   TEXT NOT NULL,
    schema_version  INTEGER NOT NULL,         -- simulator migration version
    code_version    TEXT NOT NULL,            -- git SHA of code repo at load time
    data_version    TEXT NOT NULL,            -- git SHA of data repo
    data_dirty      INTEGER NOT NULL,         -- 0/1
    scenario_tree_id TEXT NOT NULL,           -- git tree SHA covering the scenario folder + shared base subset; valid even when data_dirty=1
    data_repo_path  TEXT NOT NULL,
    data_repo_name  TEXT NOT NULL,
    loaded_at       TEXT NOT NULL,
    loaded_by       TEXT NOT NULL,            -- $ANALYSIS_USER
    host            TEXT NOT NULL
);
```

`scenario_tree_id` is a **git tree object SHA** covering the exact set of files the loader reads for this scenario: the scenario folder (`data_repo/scenarios/<name>/**`) plus the shared base paths it consumes (`accounts.csv`, `persons.csv`, `aux/*.csv`, the referenced `account_models/<model>.csv`).

- **Clean** (`data_dirty = 0`): computed by `git ls-tree` against `HEAD` for those paths and combined with `git mktree` into a single tree object, or equivalently by writing them to a temporary index and calling `git write-tree`. Either way the result is a normal git tree SHA, addressable forever.
- **Dirty** (`data_dirty = 1`): the loader stages the working-tree contents of those paths into a **separate index file** (`GIT_INDEX_FILE=...` env var, never touching the user's index) and runs `git write-tree` against it. The output is a real tree SHA, identical to what a future commit of the same content would produce. Optionally the engine runs `git update-ref refs/analysis-dirty/<tree-id> <tree-id>` so the object is reachable from a ref and won't be garbage-collected before the run is forgotten.

This gives a single uniform identifier for "what intent did this run consume" — a content-addressed git object — with no bespoke hashing scheme. The cache-validity check for an intent file is `intent_manifest.scenario_tree_id == compute_tree_id(scenario)`.

### Ledger layer DDL (run files only)

```sql
-- The run's identity card; exactly one row.
CREATE TABLE simulation_run (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    scenario_name   TEXT NOT NULL,
    schema_version  INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK (status IN
                        ('running','succeeded','failed','halted')),
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    failure_reason  TEXT,                     -- e.g. 'WaterfallExhausted: 2032-08 short $4,200 …'
    -- reproducibility (see ADR-004)
    code_version    TEXT NOT NULL,
    code_dirty      INTEGER NOT NULL,
    code_repo_path  TEXT NOT NULL,
    data_version    TEXT NOT NULL,
    data_dirty      INTEGER NOT NULL,
    scenario_tree_id TEXT NOT NULL,           -- copied from intent_manifest at run time
    data_repo_path  TEXT NOT NULL,
    data_repo_name  TEXT NOT NULL,
    data_mode       TEXT NOT NULL CHECK (data_mode IN ('prototype','live')),
    allow_dirty_data INTEGER NOT NULL DEFAULT 0,
    allow_dirty_code INTEGER NOT NULL DEFAULT 0,
    -- identity (audit only; excluded from determinism key)
    run_by          TEXT NOT NULL,
    host            TEXT NOT NULL,
    python_version  TEXT NOT NULL,
    -- invocation (for replay)
    run_args_json     TEXT NOT NULL DEFAULT '{}',
    engine_options_json TEXT NOT NULL DEFAULT '{}',
    -- forking (reserved, unused in v1)
    parent_run_path TEXT,                     -- path to a previous run file
    fork_month      TEXT
);

CREATE TABLE simulation_step (
    id               INTEGER PRIMARY KEY,
    step_index       INTEGER NOT NULL UNIQUE, -- 0 = initial state, 1 = first month, …
    month            TEXT NOT NULL,           -- month-end date
    previous_step_id INTEGER REFERENCES simulation_step(id)  -- NULL only when step_index=0
);
CREATE INDEX idx_step_month ON simulation_step(month);

CREATE TABLE account_state (
    step_id     INTEGER NOT NULL REFERENCES simulation_step(id),
    account_id  INTEGER NOT NULL REFERENCES account(id),
    balance     INTEGER NOT NULL,             -- cents
    cost_basis  INTEGER,                      -- cents; NULL when basis is not tracked
    PRIMARY KEY (step_id, account_id)
);
CREATE INDEX idx_state_account ON account_state(account_id, step_id);

-- Atomic ledger entries; one row per dollar movement.
-- Per ADR-003 §2, every row traces to exactly one rule: reason_kind='rule' with
-- reason_id = scheduled_transaction.id. The 'initial' kind is reserved for the
-- synthetic per-account opening-balance rule materialized at run start.
CREATE TABLE transfer_event (
    id              INTEGER PRIMARY KEY,
    step_id         INTEGER NOT NULL REFERENCES simulation_step(id),
    date            TEXT NOT NULL,            -- month-end
    from_account    INTEGER NOT NULL REFERENCES account(id),
    to_account      INTEGER NOT NULL REFERENCES account(id),
    amount          INTEGER NOT NULL,         -- cents, always non-negative
    tax_character   TEXT NOT NULL,
    reason_kind     TEXT NOT NULL CHECK (reason_kind IN ('rule','initial')),
    reason_id       INTEGER,                  -- scheduled_transaction.id when 'rule';
                                              -- opaque per-account id when 'initial'
    parent_event_id INTEGER REFERENCES transfer_event(id),  -- groups multi-leg siblings from one rule firing
    from_holding_id INTEGER REFERENCES holding(id),  -- reserved; NULL in v1
    to_holding_id   INTEGER REFERENCES holding(id),  -- reserved; NULL in v1
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

-- Cache + audit trail for the retrospective tax scan
CREATE TABLE tax_year_summary (
    year          INTEGER NOT NULL,
    bucket        TEXT NOT NULL,              -- ordinary, ltcg, ss_benefit_taxable,
                                              -- standard_deduction, federal_tax, state_tax, …
    amount        INTEGER NOT NULL,
    computed_at   TEXT NOT NULL,
    PRIMARY KEY (year, bucket)
);
```

### Reserved schema (present in both file kinds; v1 unused)

```sql
-- Holdings + lots: enable rebalancing analysis later without migration.
-- Defined in both intent and run files so the snapshot stays consistent.
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
```

The `from_holding_id` / `to_holding_id` columns on `transfer_event` are inlined into its `CREATE TABLE` above so v1 ships with no follow-up `ALTER TABLE`s.

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
| `account_model` | The account bundle this scenario uses (one row per intent/run file). |
| `account_model_member` | Opening balance, opening cost basis, and prior-Dec-31 balance (for partial-first-year RMD) for each account in the model. |
| `scheduled_transaction` | Recurring or one-off rule that emits transfer events on a cadence; `amount_rule_kind` selects the formula. |
| `scheduled_transaction_source` | Ordered list of source accounts for rules that draw from multiple places (e.g. `waterfall_fill`). |
| `calendar` | The patch sequence this scenario applies to the running model (one row per intent/run file). |
| `calendar_event` | One patch on one date; `kind` selects the semantics, `payload_json` carries the parameters. |
| `aux_table` | Versioned reference data: tax brackets, RMD divisors, inflation series, per-account rates, filing status. |
| `scenario` | The scenario this file represents (one row). |
| `person_lifespan` | Optional per-person final_month override for a scenario (ADR-003 §4.9). |
| `year_to_date` | Partial-first-year catch-up facts (ADR-003 §4.10): wages/investment growth/SS already received, prior withholding, and fully-prior-year tax activity (loss carryforwards etc.), posted as balance-neutral transfer_event rows. |
| `simulation_step` | One month inside the run; links to the previous step. |
| `account_state` | Per-account balance and cost basis at the end of one step; long-narrow for pivoting. |
| `transfer_event` | Atomic ledger entry: every dollar movement is exactly one row, tagged with tax character and reason. |
| `applied_calendar_event` | Junction table: which calendar events fired in which step. |
| `tax_year_summary` | Cache and audit trail for the retrospective tax scan, by (year, bucket). |
| `holding` | Per-account holdings (reserved; unused in v1). |
| `lot` | Tax lots within a holding (reserved; unused in v1). |
| `schema_overview` | This table; one short description per table, for human and LLM readers. |

### Views

Defined in run files only. Intent files may define intent-only views in future; none in v1.

```sql
-- Net worth = sum of asset balances minus sum of liability balances.
-- Income/expense flows are excluded (they're flow accounts, not stock).
CREATE VIEW net_worth AS
SELECT s.month,
       SUM(CASE WHEN a.kind = 'asset'     THEN st.balance
                WHEN a.kind = 'liability' THEN -st.balance
                ELSE 0 END) AS net_worth
FROM simulation_step s
JOIN account_state st ON st.step_id = s.id
JOIN account a        ON a.id = st.account_id
GROUP BY s.month;

-- Per-account balance over time (asset and liability accounts).
CREATE VIEW balance_by_account AS
SELECT s.month, a.id AS account_id, a.name, a.kind, a.subkind,
       st.balance, st.cost_basis
FROM simulation_step s
JOIN account_state st ON st.step_id = s.id
JOIN account a        ON a.id = st.account_id
WHERE a.kind IN ('asset','liability');

-- Aggregated by subkind: "tax-deferred vs. taxable vs. Roth vs. cash vs. real estate".
CREATE VIEW balance_by_subkind AS
SELECT month, kind, subkind,
       SUM(CASE WHEN kind = 'asset' THEN balance
                WHEN kind = 'liability' THEN -balance
                ELSE 0 END) AS balance
FROM balance_by_account
GROUP BY month, kind, subkind;

-- Aggregated by owner.
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

-- Annual cashflow per account: in, out, net. Useful for income/expense roots.
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

-- Annual totals grouped by the rule or event that caused the transfer.
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

### Migrations

v1 ships two SQL files in `src/analysis/sim/migrations/`:

- `001_init_intent.sql` — schema for intent files.
- `001_init_run.sql` — schema for run files (intent tables + ledger tables + views).

Both seed their respective `schema_overview` rows. A `_schema_version` table tracks the highest applied migration per file. Future migrations are paired (`002_*_intent.sql`, `002_*_run.sql`) when they affect both file kinds; single-sided migrations are common too.

**Lazy migration on open.** Run files are read-mostly. When the engine opens an older run file (e.g. for `compare` or `query`), it applies any forward-compatible additive migrations (adding columns, adding views) and bumps `_schema_version`. Destructive migrations (renames, drops) print a warning and refuse to modify the file — the user can re-run the scenario instead.

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
- **Cross-file `ATTACH` schema skew.** Comparing runs from different schema versions can fail on views that reference dropped/renamed columns. Mitigation: lazy additive migration on open; comparisons across major schema versions are best-effort with a header warning, as already planned in Q12.
- **JSON columns not strongly queryable.** SQLite's `json_extract` is available but ergonomically weak. The variable-shape pieces genuinely vary; `kind` columns let queries filter before extracting JSON.
- **No row-level CHECK on `payload_json` shape matching `kind`.** Validation lives in `loaders.py` and `expansions.py`. If shape drift becomes a problem, JSON-Schema validation at load time is the response.
- **`account_state` writes O(accounts × months) rows per run.** For ~40 accounts × 40 years × 12 months that's ~19k rows — trivially small.
- **The `flow_by_account` view double-counts an account's own internal flows if `from_account = to_account`**, which the engine never emits. Documenting rather than guarding.
- **Reserved columns are dead weight in v1.** `parent_run_path`, `fork_month`, `seed`, `holding`/`lot` tables, the `*_holding_id` columns on `transfer_event`. Worth it to avoid future migrations of the ledger tables, which dominate file size.

## Open questions deferred to later ADRs

- Exact vocabulary of `account.subkind` values — drafted in code, will become a checked list in ADR-003.
- Exact vocabulary of `aux_table.kind` — same.
- Whether `tax_year_summary` should also store the inputs used (bracket-table aux_table id, filing status). Likely yes; added when the tax scan is implemented.
- Exact CSV layout for the per-scenario folder (column headers for `scenario.csv`, `scheduled.csv`, `calendar.csv`, `year_to_date.csv`, `waterfall_sources.csv`) — to be specified in ADR-003 alongside the rule and event-kind registries.
