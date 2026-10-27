"""SQLite DDL for intent and run files. Mirrors ADR-002 §Intent/Ledger layer DDL."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 49


def connect_sim_file(path: str | Path, **kwargs: Any) -> sqlite3.Connection:
    """Open an intent or run file, without an fsync on every commit.

    Both are rebuilt from the data repo's CSVs (``sim load``, ``sim run``),
    so durability across a power loss or OS crash isn't worth the fsync,
    which was about 30% of an ensemble run's time on a container's disk.
    An application crash or Ctrl-C still leaves the file intact: the
    rollback journal stays on disk.
    """
    con = sqlite3.connect(path, **kwargs)
    con.execute("PRAGMA synchronous = OFF")
    return con

# Shared snapshot tables: present in intent files and copied verbatim into run files.
INTENT_BASE_DDL = """
CREATE TABLE person (
    id          TEXT PRIMARY KEY,
    display     TEXT NOT NULL,
    birthdate   TEXT NOT NULL,
    sex         TEXT,  -- #222: 'f' or 'm', or NULL; a life_table lifespan needs it
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
            'retirement','hsa','real_estate','vehicle','other_asset','business'))
        OR (kind = 'liability' AND subkind IN ('mortgage','heloc','credit_card',
            'other_liability'))
        OR (kind = 'income' AND subkind IN ('salary','ss_benefit','pension','interest',
            'dividend','capital_gain','rental','other_income'))
        OR (kind = 'expense' AND subkind IN ('living','healthcare','housing',
            'interest_expense','taxes_federal','taxes_state','taxes_property',
            'business','other_expense'))
    )
);
CREATE INDEX idx_account_kind_subkind ON account(kind, subkind);
CREATE INDEX idx_account_owner ON account(owner);

-- Liquidity chains (ADR-003 §4.7). A `chain` row is either a LEAF (account_id
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
-- (ADR-003 §4.7), at which point the still-outstanding amount falls through
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
    -- #199: the character of the money this step places, and of every later
    -- step's until another sets one (NULL: inherited).
    tax_character      TEXT,
    -- #199: the asset lot whose basis this step recovers (a sale) or adds to
    -- (a capital improvement), per tax_character.
    basis_of_lot_id    INTEGER REFERENCES account_lot(id),
    CHECK (maximum_cents IS NULL OR maximum_cents >= 0),
    CHECK (percent_micros >= 0 AND percent_micros <= 1000000),
    CHECK (cadence IS NULL OR series IS NOT NULL),
    CHECK (series IS NULL OR maximum_cents IS NOT NULL),
    UNIQUE (chain_id, step_order)
);

-- One row per lot (ADR-005): a lot is a position inside one account, and the
-- engine keys balances, basis and quantity on `id`. Accounts that are not split
-- have a single lot labelled 'default'.
--
-- `subkind` is set on, and only on, the lots of a `retirement` account (#217):
-- Roth or traditional, IRA or 401k. `tax_treatment` is the lot's effective
-- treatment, resolved at load: its subkind's for a retirement lot, else its
-- account's. The engine and the views read it from here, not from account.
--
-- Balance management is per lot (ADR-003 §4.3): a debit that would drop the
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
    subkind                 TEXT CHECK (subkind IN
                                ('traditional_ira','roth_ira','traditional_401k','roth_401k')),
    tax_treatment           TEXT CHECK (tax_treatment IN
                                ('taxable','tax_deferred','tax_free','none')),
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
    vary_shock_sd  INTEGER,
    -- #280: where the path starts: NULL for a stationary draw around the
    -- level, 'current' for the rate in effect just before the row's date.
    vary_process_start  TEXT,
    -- #291: on a set_rate row with no amount of its own, a date: the row puts
    -- back what governed its series the day before it, resuming a process
    -- (its level, persistence and shocks) or restoring a fixed rate. The
    -- ensemble step resolves it, as it does a varying row.
    vary_resume         TEXT,
    -- #165: a `discrete` row's weighted outcomes, each a value or a low..high
    -- range in the target's stored unit ('30%: 0; 25%: 100000..600000').
    vary_outcomes       TEXT,
    -- #281: the columns authored relative to a date_marker row, as a JSON
    -- object of column -> [marker name, months] ({"date": ["start", 24]}).
    -- Those columns hold the date against the markers' base case; the
    -- ensemble step recomputes them from each member's drawn markers.
    date_refs_json      TEXT
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
                                 'excluded_gain','home_sale',
                                 'capital_improvement','roth_nonqualified',
                                 'business_income','business_expense')),
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

-- Optional per-scenario lifespan override (ADR-003 §4.9). When the
-- simulated month exceeds final_month, every scheduled_transaction
-- whose `person` column names this person stops firing for the remainder
-- of the run, except percent_of_balance (growth) and on_death rows.
CREATE TABLE person_lifespan (
    scenario_id  INTEGER NOT NULL REFERENCES scenario(id),
    person       TEXT NOT NULL REFERENCES person(id),
    -- #221: the authored age (decimal years); final_month is derived from it
    -- and the person's birthdate, and is all the engine reads. An ensemble
    -- member rewrites both with its draw and clears vary_distribution.
    age_at_death REAL NOT NULL,
    final_month  TEXT NOT NULL,
    vary_distribution   TEXT,
    vary_low            REAL,
    vary_high           REAL,
    vary_mode           REAL,
    vary_correlate_with TEXT,
    vary_correlation    REAL,
    -- #222: a life_table lifespan's set-forward in years (negative is a
    -- set-back), solved at load so the table's median is age_at_death.
    life_table_set_forward REAL,
    -- #315: the aux life_table__<name>.csv's name; NULL for life_table.csv
    life_table TEXT,
    PRIMARY KEY (scenario_id, person)
);

-- Partial-first-year catch-up facts (ADR-003 §4.10) -- consolidates what used
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
                         'excluded_gain','home_sale','capital_improvement',
                         'roth_nonqualified','business_income','business_expense')),
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

-- Ensemble variation (ADR-006). Present (with real rows) only in a member
-- intent resolved from an ensemble recipe by `ensemble.resolve_member`;
-- absent (no rows) in the recipe's own intent and in every ordinary,
-- non-ensemble scenario. Intent-layer so a run file gets them via the
-- ordinary SNAPSHOT_TABLES copy, with no special-casing in open_run_file.
CREATE TABLE ensemble_manifest (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    ensemble_name   TEXT NOT NULL,
    member_index    INTEGER NOT NULL,
    member_count    INTEGER NOT NULL,
    -- NULL for a sensitivity member (ADR-007 §5): an OAT batch draws
    -- nothing, so there is no seed to record.
    seed            INTEGER,
    recipe_tree_id  TEXT NOT NULL,
    generated_at    TEXT NOT NULL,
    -- ADR-007 §5: a random Monte Carlo member, or a deterministic OAT
    -- sensitivity member.
    mode            TEXT NOT NULL DEFAULT 'monte_carlo'
                        CHECK (mode IN ('monte_carlo','sensitivity'))
);

CREATE TABLE ensemble_draw (
    calendar_event_name TEXT NOT NULL,
    vary_target         TEXT NOT NULL,
    drawn_value         TEXT NOT NULL,
    -- ADR-007 §5: the OAT override quantile this row was resolved at; NULL
    -- for a random draw or a row resolved to its central value.
    quantile            REAL,
    -- The row's vary_distribution, which resolving clears from calendar_event
    -- (ADR-007 §5); NULL in files written before schema 28.
    distribution        TEXT,
    -- ADR-008 §6: the row's standard normal score in its correlation tree;
    -- NULL for a row outside every tree.
    z                   REAL,
    -- #261: on a random member, a tree node's own independent normal noise,
    -- before its tree mixes it in (NULL outside every tree, and on member 0
    -- or an OAT member); and the probability the drawn value sits at in its
    -- distribution (for a factor, its score's; NULL only for a lifespan's
    -- authored base age).
    noise               REAL,
    u                   REAL,
    PRIMARY KEY (calendar_event_name)
);

-- #261 (#72): a t-copula tree's scale S on a random member, by its root:
-- the member's own (year NULL), multiplying every score in the tree, and
-- each process path year's.
CREATE TABLE ensemble_tail_scale (
    tree_root   TEXT NOT NULL,
    year        INTEGER,
    scale       REAL NOT NULL,
    UNIQUE (tree_root, year)
);

-- #261: each year of a process path (ADR-006 §4a): the node's own noise,
-- its score after its tree mixes it in, the tree's scale that year (NULL
-- for a Gaussian tree or a lone row), the shock the path took (score *
-- scale; 0 on member 0 and OAT members, whose noise and score are NULL),
-- and the resulting rate in micros.
CREATE TABLE ensemble_path_shock (
    node        TEXT NOT NULL,
    year        INTEGER NOT NULL,
    noise       REAL,
    score       REAL,
    scale       REAL,
    shock       REAL NOT NULL,
    rate        INTEGER NOT NULL,
    PRIMARY KEY (node, year)
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

-- #199: basis a sale could not recover, from a chain step whose
-- tax_character is return_of_capital. No money moved, so it is not a
-- transfer_event; the tax accumulator nets it against the year's capital
-- gains, deducts up to the capital_loss_limit, and carries the rest forward.
CREATE TABLE capital_loss (
    id              INTEGER PRIMARY KEY,
    step_id         INTEGER NOT NULL REFERENCES simulation_step(id),
    date            TEXT NOT NULL,
    lot_id          INTEGER NOT NULL REFERENCES account_lot(id),
    amount          INTEGER NOT NULL CHECK (amount > 0),
    tax_character   TEXT NOT NULL CHECK (tax_character IN ('ltcg','stcg')),
    tax_year        INTEGER,
    reason_kind     TEXT NOT NULL,
    reason_id       INTEGER
);

-- #255: a lot's basis reset at a death (IRC §1014) to its value at the end
-- of the person's final month: all of the gain or loss, or the decedent's
-- half of a joint lot outside community property. No money moves.
CREATE TABLE basis_step_up (
    id              INTEGER PRIMARY KEY,
    step_id         INTEGER NOT NULL REFERENCES simulation_step(id),
    date            TEXT NOT NULL,
    lot_id          INTEGER NOT NULL REFERENCES account_lot(id),
    person          TEXT NOT NULL,
    balance         INTEGER NOT NULL,
    basis_before    INTEGER NOT NULL,
    basis_after     INTEGER NOT NULL
);

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
"""

# Backward-compatible composite used by the intent loader.
INTENT_DDL = INTENT_BASE_DDL + INTENT_MANIFEST_DDL


def apply_intent_schema(con: sqlite3.Connection) -> None:
    con.executescript(INTENT_DDL)


def apply_run_schema(con: sqlite3.Connection) -> None:
    con.executescript(INTENT_BASE_DDL + LEDGER_DDL + RUN_VIEWS_DDL + INTENT_MANIFEST_DDL)


# Tables snapshotted from intent into run files. Order respects FK dependencies.
SNAPSHOT_TABLES: tuple[str, ...] = (
    "person",
    "security",
    "security_allocation",
    "account",
    "chain",
    "chain_steps",
    "account_lot",
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
    "ensemble_manifest",
    "ensemble_draw",
    "ensemble_tail_scale",
    "ensemble_path_shock",
)


def inserted_id(cur: sqlite3.Cursor) -> int:
    """The rowid ``cur``'s INSERT just made.

    ``lastrowid`` is typed ``int | None`` because it is None before any
    insert; after one it is always set, so None here is a bug."""
    if cur.lastrowid is None:
        raise RuntimeError("cursor has not inserted a row")
    return cur.lastrowid
