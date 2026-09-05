"""SQLite DDL for intent and run files. Mirrors ADR-002 §Intent/Ledger layer DDL."""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 15

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
    -- Balance management (ADR-003 §4.7): a debit that would drop the balance
    -- below minimum_balance_cents triggers a look-ahead draw on refill_chain_id
    -- BEFORE the debit posts (not a month-end reconciliation); when it would
    -- exceed maximum_balance_cents (NULL = unbounded), sweep the excess into
    -- overflow_chain_id at month end. Both chains are resolved recursively.
    -- refill_target_cents/refill_amount_cents are mutually exclusive: the
    -- former refills to that fixed level, the latter by that fixed amount (or
    -- enough to reach minimum_balance_cents if that is greater); neither set
    -- means refill exactly to minimum_balance_cents.
    minimum_balance_cents  INTEGER NOT NULL DEFAULT 0,
    refill_chain_id        INTEGER REFERENCES chain(id),
    refill_target_cents    INTEGER,
    refill_amount_cents    INTEGER,
    maximum_balance_cents  INTEGER,
    overflow_chain_id      INTEGER REFERENCES chain(id),
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

-- Liquidity chains (ADR-003 §4.7). A `chain` row is either a LEAF (account_id
-- set: the chain IS that account) or an INTERIOR named chain (account_id NULL,
-- with >=1 chain_steps rows, walked in step_order until a step's amount need
-- is satisfied). Every real account gets an implicit leaf chain at load time
-- (name = account.name, kind = account.kind), so `chain.name` is one namespace
-- covering both kinds -- account.refill_chain_id/overflow_chain_id and
-- chain_steps.draw_from_chain_id always reference `chain(id)`, never
-- `account(id)` directly, so no union-typed reference column is ever needed.
CREATE TABLE chain (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    kind       TEXT NOT NULL,
    account_id INTEGER REFERENCES account(id)
);
CREATE INDEX idx_chain_account ON chain(account_id);

CREATE TABLE chain_steps (
    id                 INTEGER PRIMARY KEY,
    chain_id           INTEGER NOT NULL REFERENCES chain(id),
    step_order         INTEGER NOT NULL,
    draw_from_chain_id INTEGER NOT NULL REFERENCES chain(id),
    threshold_cents    INTEGER NOT NULL DEFAULT 0,
    amount_cents       INTEGER,
    percent_micros     INTEGER,
    series             TEXT,
    cadence            TEXT CHECK (cadence IN ('monthly','annual')),
    CHECK (amount_cents IS NULL OR amount_cents >= 0),
    CHECK (percent_micros IS NULL OR percent_micros >= 0 AND percent_micros <= 1000000),
    CHECK (amount_cents IS NULL OR percent_micros IS NULL),
    UNIQUE (chain_id, step_order)
);

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
                                ('ordinary','ltcg','gains','deferred',
                                 'ss_benefit','tax_free',
                                 'tax_deferred_in','tax_deferred_out',
                                 'return_of_capital','property_tax','none')),
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
    UNIQUE (model_id, name)
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

-- Optional per-scenario lifespan override (ADR-003 §4.9). When the
-- simulated month exceeds final_month, every scheduled_transaction
-- associated with this person (explicit `person` column, or owner of
-- from_account/to_account) stops firing for the remainder of the run.
CREATE TABLE person_lifespan (
    scenario_id INTEGER NOT NULL REFERENCES scenario(id),
    person      TEXT NOT NULL REFERENCES person(id),
    final_month TEXT NOT NULL,
    PRIMARY KEY (scenario_id, person)
);

-- Partial-first-year catch-up facts (ADR-003 §4.10) -- consolidates what used
-- to be split across two mechanisms: a single row's own YTD amount (not tied
-- to any one scheduled_transaction's own amount, e.g. investment growth,
-- SS already received) and prior tax-year activity (superseded
-- `prior_tax_activity`: wages/withholding YTD, loss carryforwards from a
-- fully-prior tax year). Posted as balance-neutral transfer_event rows
-- (reason_kind='ytd') so tax/reporting queries see them without double-
-- counting against account_model_member.opening_balance. `year` defaults to
-- scenario.start's year (dated the month before start); an explicit
-- different year is dated that year's Dec 31.
CREATE TABLE year_to_date (
    id            INTEGER PRIMARY KEY,
    scenario_id   INTEGER NOT NULL REFERENCES scenario(id),
    name          TEXT NOT NULL,
    from_account  INTEGER NOT NULL REFERENCES account(id),
    to_account    INTEGER NOT NULL REFERENCES account(id),
    amount        INTEGER NOT NULL,
    tax_character TEXT NOT NULL CHECK (tax_character IN
                        ('ordinary','ltcg','gains','deferred',
                         'ss_benefit','tax_free',
                         'tax_deferred_in','tax_deferred_out',
                         'return_of_capital','property_tax','none')),
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
    tax_year        INTEGER,
    tax_character   TEXT NOT NULL,
    reason_kind     TEXT NOT NULL CHECK (reason_kind IN
                        ('rule','initial','ytd','ytd_rule','refill','overflow')),
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

CREATE TABLE inflation_index_state (
    step_id                  INTEGER NOT NULL REFERENCES simulation_step(id),
    series                   TEXT NOT NULL,
    cumulative_index_micros  INTEGER NOT NULL,
    PRIMARY KEY (step_id, series)
);
CREATE INDEX idx_inflation_state_series ON inflation_index_state(series, step_id);
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
    "chain",
    "chain_steps",
    "account_model",
    "account_model_member",
    "calendar",
    "calendar_event",
    "scheduled_transaction",
    "aux_table",
    "scenario",
    "person_lifespan",
    "year_to_date",
    "tag",
    "account_tag",
    "scheduled_transaction_tag",
    "calendar_event_tag",
    "holding",
    "lot",
)
