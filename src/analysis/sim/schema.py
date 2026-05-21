"""SQLite DDL for intent and run files. Mirrors ADR-002 §Intent/Ledger layer DDL."""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 3

# Shared snapshot tables: present in intent files and copied verbatim into run files.
INTENT_BASE_DDL = """
CREATE TABLE person (
    id          TEXT PRIMARY KEY,
    display     TEXT NOT NULL,
    birthdate   TEXT NOT NULL,
    role        TEXT,
    meta_json   TEXT NOT NULL DEFAULT '{}'
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

CREATE TABLE account_model (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE account_model_member (
    model_id                INTEGER NOT NULL REFERENCES account_model(id),
    account_id              INTEGER NOT NULL REFERENCES account(id),
    opening_balance         INTEGER NOT NULL,
    opening_cost_basis      INTEGER,
    prior_year_end_balance  INTEGER,
    PRIMARY KEY (model_id, account_id)
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
    scheduled_name TEXT,
    account_1      TEXT,
    account_2      TEXT,
    series         TEXT,
    amount         INTEGER,
    param_date     TEXT,
    tax_character  TEXT,
    payload_json   TEXT,
    description    TEXT
);
CREATE INDEX idx_calevt_date ON calendar_event(date);
CREATE INDEX idx_calevt_kind ON calendar_event(kind);

CREATE TABLE scheduled_transaction (
    id                      INTEGER PRIMARY KEY,
    model_id                INTEGER NOT NULL REFERENCES account_model(id),
    name                    TEXT NOT NULL,
    from_account            INTEGER NOT NULL REFERENCES account(id),
    to_account              INTEGER NOT NULL REFERENCES account(id),
    cadence                 TEXT NOT NULL CHECK (cadence IN
                                ('monthly','quarterly','annual','one_off')),
    month_of_year           INTEGER CHECK (month_of_year BETWEEN 1 AND 12),
    amount_rule_kind        TEXT NOT NULL,
    account_1               TEXT,
    account_2               TEXT,
    person                  TEXT REFERENCES person(id),
    series                  TEXT,
    rate_micros             INTEGER,
    claim_age               INTEGER,
    amount_cents            INTEGER,
    params_json             TEXT,
    base_amount             INTEGER,
    base_date               TEXT,
    inflation_series        TEXT,
    tax_character           TEXT NOT NULL CHECK (tax_character IN
                                ('ordinary','ltcg','ss_benefit','tax_free',
                                 'tax_deferred_in','tax_deferred_out',
                                 'return_of_capital','property_tax','none')),
    start                   TEXT NOT NULL,
    "end"                   TEXT,
    priority                INTEGER NOT NULL DEFAULT 500,
    description             TEXT,
    created_by_event_id     INTEGER REFERENCES calendar_event(id),
    meta_json               TEXT NOT NULL DEFAULT '{}',
    UNIQUE (model_id, name)
);
CREATE INDEX idx_sched_priority ON scheduled_transaction(priority);
CREATE INDEX idx_sched_created_by ON scheduled_transaction(created_by_event_id);

CREATE TABLE scheduled_transaction_source (
    scheduled_transaction_id INTEGER NOT NULL REFERENCES scheduled_transaction(id),
    position                 INTEGER NOT NULL,
    account_id               INTEGER NOT NULL REFERENCES account(id),
    PRIMARY KEY (scheduled_transaction_id, position)
);

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
    account_model_id  INTEGER NOT NULL REFERENCES account_model(id),
    calendar_id       INTEGER NOT NULL REFERENCES calendar(id),
    start             TEXT NOT NULL,
    "end"             TEXT NOT NULL,
    seed              INTEGER,
    parent_id         INTEGER REFERENCES scenario(id),
    created_at        TEXT NOT NULL,
    created_by        TEXT NOT NULL,
    meta_json         TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE prior_tax_activity (
    scenario_id INTEGER NOT NULL REFERENCES scenario(id),
    year        INTEGER NOT NULL,
    bucket      TEXT NOT NULL,
    amount      INTEGER NOT NULL,
    source_note TEXT,
    PRIMARY KEY (scenario_id, year, bucket)
);
"""

# Reserved schema (present in both file kinds; unused in v1, per ADR-002).
RESERVED_DDL = """
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
    quantity_micros   INTEGER NOT NULL,
    cost_basis_total  INTEGER NOT NULL,
    source_note       TEXT
);
"""

# Intent file's identity card (single row).
INTENT_MANIFEST_DDL = """
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
"""

# Run file's identity card + ledger tables.
LEDGER_DDL = """
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
    data_mode       TEXT NOT NULL CHECK (data_mode IN ('prototype','live')),
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

CREATE TABLE account_state (
    step_id     INTEGER NOT NULL REFERENCES simulation_step(id),
    account_id  INTEGER NOT NULL REFERENCES account(id),
    balance     INTEGER NOT NULL,
    cost_basis  INTEGER,
    PRIMARY KEY (step_id, account_id)
);
CREATE INDEX idx_state_account ON account_state(account_id, step_id);

CREATE TABLE transfer_event (
    id              INTEGER PRIMARY KEY,
    step_id         INTEGER NOT NULL REFERENCES simulation_step(id),
    date            TEXT NOT NULL,
    from_account    INTEGER NOT NULL REFERENCES account(id),
    to_account      INTEGER NOT NULL REFERENCES account(id),
    amount          INTEGER NOT NULL,
    tax_character   TEXT NOT NULL,
    reason_kind     TEXT NOT NULL CHECK (reason_kind IN ('rule','initial')),
    reason_id       INTEGER,
    parent_event_id INTEGER REFERENCES transfer_event(id),
    from_holding_id INTEGER REFERENCES holding(id),
    to_holding_id   INTEGER REFERENCES holding(id),
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
"""

# Views (run files only).
RUN_VIEWS_DDL = """
CREATE VIEW net_worth AS
SELECT s.month,
       SUM(CASE WHEN a.kind = 'asset'     THEN st.balance
                WHEN a.kind = 'liability' THEN -st.balance
                ELSE 0 END) AS net_worth
FROM simulation_step s
JOIN account_state st ON st.step_id = s.id
JOIN account a        ON a.id = st.account_id
GROUP BY s.month;

CREATE VIEW balance_by_account AS
SELECT s.month, a.id AS account_id, a.name, a.kind, a.subkind,
       st.balance, st.cost_basis
FROM simulation_step s
JOIN account_state st ON st.step_id = s.id
JOIN account a        ON a.id = st.account_id
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
"""

# Backward-compatible composite used by the intent loader.
INTENT_DDL = INTENT_BASE_DDL + RESERVED_DDL + INTENT_MANIFEST_DDL


def apply_intent_schema(con: sqlite3.Connection) -> None:
    con.executescript(INTENT_DDL)


def apply_run_schema(con: sqlite3.Connection) -> None:
    con.executescript(INTENT_BASE_DDL + RESERVED_DDL + LEDGER_DDL + RUN_VIEWS_DDL)


# Tables snapshotted from intent into run files. Order respects FK dependencies.
SNAPSHOT_TABLES: tuple[str, ...] = (
    "person",
    "account",
    "account_model",
    "account_model_member",
    "calendar",
    "calendar_event",
    "scheduled_transaction",
    "scheduled_transaction_source",
    "aux_table",
    "scenario",
    "prior_tax_activity",
    "holding",
    "lot",
)
