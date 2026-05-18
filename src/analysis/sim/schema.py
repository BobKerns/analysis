"""Intent-layer SQLite DDL. Mirrors ADR-002 §Intent layer DDL."""

from __future__ import annotations

SCHEMA_VERSION = 1

INTENT_DDL = """
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
    payload_json TEXT NOT NULL DEFAULT '{}',
    description  TEXT
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
    amount_rule_params_json TEXT NOT NULL DEFAULT '{}',
    base_amount             INTEGER,
    base_date               TEXT,
    inflation_series        TEXT,
    tax_character           TEXT NOT NULL CHECK (tax_character IN
                                ('ordinary','ltcg','ss_benefit','tax_free',
                                 'tax_deferred_in','tax_deferred_out',
                                 'return_of_capital','none')),
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
