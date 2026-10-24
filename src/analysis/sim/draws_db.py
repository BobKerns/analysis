"""#261: one database of an ensemble batch's draws and what they depend on.

Each member file records its own draws (``ensemble_draw``, plus
``ensemble_tail_scale`` and ``ensemble_path_shock``). Assessing a batch's
statistical setup wants them side by side, with each node's distribution and
correlation inputs, so ``build_draws_db`` gathers them into
``<batch>/draws.sqlite3``, next to the member run files:

- ``batch``: the ensemble, mode, master seed, versions;
- ``member``: every member, ``is_base`` marking member 0 (the deterministic
  base, ADR-006 §9), its derived seed and status;
- ``node``: each varying row, lifespan and latent factor: its distribution
  and parameters, its correlation parent, rho, tree root and nu, and the
  distribution's own moments;
- ``node_pair``: each pair sharing a tree, its implied and achieved rho;
- ``draw``: per member and node, the noise, score, ``u`` and value;
- ``tail_scale``, ``path_shock``: the t-copula scales and process shocks;
- ``draw_check``: per node, drawn statistics beside the distribution's own,
  and a Kolmogorov-Smirnov test of ``u`` against uniform (Monte Carlo only).

Values are stored in the simulator's own units (cents, micros, day
ordinals, years); the ``*_display`` views translate to dollars, percent and
ISO dates. ``draws_wide`` and ``draws_wide_display`` are member x node
matrices of the drawn members, member 0 left out.

The file is rebuilt whole, never updated, so it always matches the member
files it was gathered from.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from analysis.frames import named_rows

from analysis.sim.correlation import (
    Link, expected_kendall_tau, implied_correlation, score_at, tree_members, tree_root,
)
from analysis.sim.distributions import (
    Discrete, Distribution, Triangular, TruncatedNormal, Uniform, moments,
)
from analysis.sim.ensemble import (
    EnsembleError, build_distribution, correlation_links, lifespan_distribution,
    member_run_statuses, read_batch_manifest,
)
from analysis.sim.life_table import LifeTableAge
from analysis.sim.registries import (
    DATE_MARKER_KIND, LATENT_FACTOR_KIND, LIFE_TABLE_DISTRIBUTION, LIFESPAN_NODE_PREFIX, LIFESPAN_TARGET,
    RATE_MICROS_CALENDAR_KINDS,
)
from analysis.sim.schema import SCHEMA_VERSION, connect_sim_file

__all__ = [
    "DRAWS_DB_FILENAME", "DRAWS_DDL", "CHECK_QUANTILES", "build_draws_db", "draws_db_path",
    "open_draws_db", "draws_frames", "ks_uniform",
]

DRAWS_DB_FILENAME = "draws.sqlite3"

FACTOR_TARGET = "factor"

CHECK_QUANTILES: tuple[float, ...] = (0.05, 0.25, 0.5, 0.75, 0.95)
"""The quantiles ``draw_check`` sets beside the distribution's own."""

# Python's date ordinal 1 (0001-01-01) is Julian day 1721425.5 at midnight;
# SQLite's date() takes a Julian day.
_JULIAN_OFFSET = 1721424.5

DRAWS_DDL = """
CREATE TABLE batch (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    ensemble_name   TEXT NOT NULL,
    batch           TEXT NOT NULL,
    mode            TEXT NOT NULL,
    -- scenario.seed: every draw keys off it (ADR-006 §3); NULL when unset.
    master_seed     INTEGER,
    member_count    INTEGER NOT NULL,
    recipe_tree_id  TEXT,
    code_version    TEXT,
    data_version    TEXT,
    schema_version  INTEGER NOT NULL,
    built_at        TEXT NOT NULL
);

CREATE TABLE member (
    member_index    INTEGER PRIMARY KEY,
    -- member 0, the deterministic base (ADR-006 §9): every row central.
    is_base         INTEGER NOT NULL,
    derived_seed    INTEGER,
    -- succeeded | failed | running | failed_to_resolve | missing | unknown
    status          TEXT NOT NULL,
    -- the member file the draws came from: its run file, else its intent.
    source_file     TEXT
);

CREATE TABLE node (
    name            TEXT PRIMARY KEY,
    node_kind       TEXT NOT NULL,      -- row | lifespan | factor
    calendar_kind   TEXT,               -- a row's calendar kind
    affects         TEXT,               -- the series, transfer or person it moves
    target          TEXT NOT NULL,      -- amount | date | age_at_death | factor
    -- the stored unit of value and every bound below:
    -- cents | micros | day_ordinal | years | score
    unit            TEXT NOT NULL,
    distribution    TEXT,
    low             REAL,               -- truncated_normal: its clip bounds
    high            REAL,
    median          REAL,
    sd              REAL,               -- truncated_normal's sd (a scale when shaped)
    mode            REAL,
    skew            REAL,
    tailweight      REAL,
    outcomes        TEXT,               -- discrete, as authored in stored units
    ramp_months     INTEGER,
    process         TEXT,
    persistence     REAL,
    shock_sd   REAL,
    process_start   TEXT,               -- 'current', or NULL for a stationary start
    sex             TEXT,               -- life_table
    set_forward     REAL,               -- life_table, years
    birthdate       TEXT,
    base_value      REAL,               -- member 0's value
    correlate_with  TEXT,
    rho             REAL,
    tree_root       TEXT,               -- NULL outside every tree
    nu              REAL,               -- the tree's t-copula nu; NULL = Gaussian
    -- the distribution's own moments, as values are stored
    mean            REAL,
    dist_sd         REAL,
    skewness        REAL,
    excess_kurtosis REAL,
    description     TEXT
);

CREATE TABLE node_pair (
    a               TEXT NOT NULL,
    b               TEXT NOT NULL,
    kind            TEXT NOT NULL,      -- link (parent and child) | implied
    implied_rho     REAL NOT NULL,
    expected_kendall_tau REAL NOT NULL,
    -- Monte Carlo: rho from the drawn members' scores' Kendall tau, with
    -- its 95% interval (ADR-008 §6); NULL for a sensitivity batch.
    achieved_rho    REAL,
    achieved_low    REAL,
    achieved_high   REAL,
    n               INTEGER,
    PRIMARY KEY (a, b)
);

CREATE TABLE draw (
    member_index    INTEGER NOT NULL REFERENCES member(member_index),
    node            TEXT NOT NULL REFERENCES node(name),
    value           REAL NOT NULL,      -- in node.unit
    quantile        REAL,               -- an OAT override
    z               REAL,               -- tree score (after any t scale)
    noise           REAL,               -- the node's own N(0,1), before its tree
    u               REAL,               -- the probability value sits at
    PRIMARY KEY (member_index, node)
);

CREATE TABLE tail_scale (
    member_index    INTEGER NOT NULL REFERENCES member(member_index),
    tree_root       TEXT NOT NULL,
    year            INTEGER,            -- NULL: the member's own scale
    scale           REAL NOT NULL,
    UNIQUE (member_index, tree_root, year)
);

CREATE TABLE path_shock (
    member_index    INTEGER NOT NULL REFERENCES member(member_index),
    node            TEXT NOT NULL,
    year            INTEGER NOT NULL,
    noise           REAL,
    score           REAL,
    scale           REAL,
    shock           REAL NOT NULL,
    rate            INTEGER NOT NULL,   -- micros
    PRIMARY KEY (member_index, node, year)
);

CREATE TABLE draw_check (
    node            TEXT PRIMARY KEY REFERENCES node(name),
    n               INTEGER NOT NULL,   -- drawn members (member 0 excluded)
    expected_mean   REAL,
    drawn_mean      REAL,
    expected_sd     REAL,
    drawn_sd        REAL,
    expected_p05    REAL, drawn_p05 REAL,
    expected_p25    REAL, drawn_p25 REAL,
    expected_p50    REAL, drawn_p50 REAL,
    expected_p75    REAL, drawn_p75 REAL,
    expected_p95    REAL, drawn_p95 REAL,
    -- Kolmogorov-Smirnov distance of the members' u from uniform, and its
    -- p-value (asymptotic); NULL when no member records a u.
    ks_u            REAL,
    ks_u_p          REAL
);
"""


def _position(column: str, unit: str = "unit") -> str:
    """SQL turning a stored position (a value or bound) into display units."""
    return (
        f"CASE {unit} WHEN 'cents' THEN {column} / 100.0 "
        f"WHEN 'micros' THEN {column} / 10000.0 "
        f"WHEN 'day_ordinal' THEN date({column} + {_JULIAN_OFFSET}) "
        f"ELSE {column} END"
    )


def _spread(column: str, unit: str = "unit") -> str:
    """SQL turning a stored spread (an SD) into display units: days stay days."""
    return (
        f"CASE {unit} WHEN 'cents' THEN {column} / 100.0 "
        f"WHEN 'micros' THEN {column} / 10000.0 ELSE {column} END"
    )


_DISPLAY_UNIT = (
    "CASE unit WHEN 'cents' THEN 'dollars' WHEN 'micros' THEN 'percent' "
    "WHEN 'day_ordinal' THEN 'date' ELSE unit END"
)

_STATIC_VIEWS = f"""
CREATE VIEW node_display AS
SELECT name, node_kind, calendar_kind, affects, target, {_DISPLAY_UNIT} AS unit, distribution,
    {_position('low')} AS low, {_position('high')} AS high, {_position('median')} AS median,
    {_spread('sd')} AS sd, {_position('mode')} AS mode, skew, tailweight, outcomes,
    ramp_months, process, persistence, {_spread('shock_sd')} AS shock_sd,
    process_start, sex, set_forward, birthdate, {_position('base_value')} AS base_value,
    correlate_with, rho, tree_root, nu,
    {_position('mean')} AS mean, {_spread('dist_sd')} AS dist_sd, skewness, excess_kurtosis,
    description
FROM node;

CREATE VIEW draw_display AS
SELECT d.member_index, m.is_base, d.node, {_DISPLAY_UNIT} AS unit,
    {_position('d.value')} AS value, d.quantile, d.z, d.noise, d.u
FROM draw d JOIN node n ON n.name = d.node JOIN member m USING (member_index);

CREATE VIEW draw_check_display AS
SELECT c.node, {_DISPLAY_UNIT} AS unit, c.n,
    {_position('c.expected_mean')} AS expected_mean, {_position('c.drawn_mean')} AS drawn_mean,
    {_spread('c.expected_sd')} AS expected_sd, {_spread('c.drawn_sd')} AS drawn_sd,
    {_position('c.expected_p05')} AS expected_p05, {_position('c.drawn_p05')} AS drawn_p05,
    {_position('c.expected_p25')} AS expected_p25, {_position('c.drawn_p25')} AS drawn_p25,
    {_position('c.expected_p50')} AS expected_p50, {_position('c.drawn_p50')} AS drawn_p50,
    {_position('c.expected_p75')} AS expected_p75, {_position('c.drawn_p75')} AS drawn_p75,
    {_position('c.expected_p95')} AS expected_p95, {_position('c.drawn_p95')} AS drawn_p95,
    c.ks_u, c.ks_u_p
FROM draw_check c JOIN node n ON n.name = c.node;

CREATE VIEW path_shock_display AS
SELECT p.member_index, m.is_base, p.node, p.year, p.noise, p.score, p.scale, p.shock,
    p.rate / 10000.0 AS rate_percent
FROM path_shock p JOIN member m USING (member_index);
"""


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _wide_views(nodes: list[tuple[str, str]]) -> str:
    """``draws_wide`` and ``draws_wide_display``: one column per node, which
    SQL can't pivot to by itself, so they're written for this batch's nodes."""
    def view(name: str, expression: str) -> str:
        columns = "".join(
            f",\n    MAX(CASE WHEN d.node = '{node.replace(chr(39), chr(39) * 2)}' "
            f"THEN {expression} END) AS {_quoted(node)}"
            for node, _unit in nodes
        )
        return (
            f"CREATE VIEW {name} AS\nSELECT d.member_index{columns}\n"
            "FROM draw d JOIN node n ON n.name = d.node JOIN member m USING (member_index)\n"
            "WHERE m.is_base = 0 GROUP BY d.member_index;\n"
        )
    return view("draws_wide", "d.value") + view("draws_wide_display", _position("d.value", "n.unit"))


def draws_db_path(batch_dir: Path) -> Path:
    return Path(batch_dir) / DRAWS_DB_FILENAME


# --- gathering ---------------------------------------------------------------


@dataclass
class _Member:
    index: int
    path: Path | None
    status: str


def _member_files(
    batch_dir: Path, intent_batch_dir: Path | None, member_count: int | None,
) -> list[_Member]:
    """Every member of the batch, with the file its draws come from: its run
    file when there is one, else its resolved intent (a member that failed
    before its run file was written), else none: ``failed_to_resolve`` when
    the batch manifest says so, ``missing`` for any other index below the
    manifest's member count."""
    statuses = member_run_statuses(batch_dir)
    stems = set(statuses) | {f"member_{i:04d}" for i in range(member_count or 0)}
    if intent_batch_dir is not None and intent_batch_dir.is_dir():
        stems |= {p.stem for p in intent_batch_dir.glob("member_*.sqlite3")}
    members = []
    for stem in sorted(stems):
        run = Path(batch_dir) / f"{stem}.sqlite3"
        intent = intent_batch_dir / f"{stem}.sqlite3" if intent_batch_dir else None
        path = run if run.is_file() else intent if intent is not None and intent.is_file() else None
        members.append(_Member(int(stem.removeprefix("member_")), path,
                               statuses.get(stem, "missing" if path is None else "unknown")))
    return members


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {c[1] for c in con.execute(f"PRAGMA table_info({table})")}


def _has_table(con: sqlite3.Connection, table: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,),
    ).fetchone() is not None


def _optional(columns: set[str], column: str, alias: str | None = None) -> str:
    """``column`` if the table has it, else NULL: member files from before
    a schema change lack the newer columns, which stay NULL here."""
    return f"{column} AS {alias or column}" if column in columns else f"NULL AS {alias or column}"


def _stored_value(target: str, raw: str) -> float:
    """An ``ensemble_draw.drawn_value`` as a number in its stored unit."""
    if target == "date":
        return float(date.fromisoformat(raw[:10]).toordinal())
    return float(raw)


def _unit(target: str, kind: str | None) -> str:
    if target == "date":
        return "day_ordinal"
    if target == LIFESPAN_TARGET:
        return "years"
    if target == FACTOR_TARGET:
        return "score"
    return "micros" if kind in RATE_MICROS_CALENDAR_KINDS else "cents"


def _affects(row: sqlite3.Row) -> str:
    if row["kind"] in ("one_off_transfer", "transfer_balance"):
        return f"{row['account_1']} → {row['account_2']}"
    return row["series"] or row["scheduled_name"] or ""


def _marker_affects(cur: sqlite3.Cursor, marker: str) -> str:
    """The rows dated from a date marker (#281), by name where they have one."""
    if "date_refs_json" not in _columns(cur.connection, "calendar_event"):
        return ""
    dated = [
        r["name"] or f"{r['kind']} {r['series'] or r['scheduled_name'] or ''}".strip()
        for r in cur.execute(
            "SELECT * FROM calendar_event WHERE date_refs_json IS NOT NULL ORDER BY id").fetchall()
        if any(anchor == marker for anchor, _ in json.loads(r["date_refs_json"]).values())
    ]
    return ", ".join(dated)


def _get(row: sqlite3.Row, column: str) -> Any:
    return row[column] if column in row.keys() else None


def _dist_columns(dist: Distribution | LifeTableAge, target: str) -> dict[str, Any]:
    """The parameters as resolved (a truncated_normal's inferred bounds
    included) and the distribution's moments, in the stored unit."""
    record: dict[str, Any] = {}
    if isinstance(dist, TruncatedNormal):
        record.update(low=dist.clip_low, high=dist.clip_high, median=dist.median, sd=dist.sd,
                      skew=dist.skew, tailweight=dist.tailweight)
    elif isinstance(dist, Discrete):
        record.update(low=dist.outcomes[0].low, high=dist.outcomes[-1].high,
                      median=dist.quantile(0.5))
    elif isinstance(dist, LifeTableAge):
        record.update(low=dist.low, high=dist.high, median=dist.central(),
                      set_forward=dist.set_forward)
    else:
        record.update(low=dist.low, high=dist.high, median=dist.quantile(0.5))
        if isinstance(dist, Triangular):
            record["mode"] = dist.mode
    if isinstance(dist, LifeTableAge):
        m = moments(dist)
    else:
        m = moments(dist, whole_days=target == "date", rounded=target not in ("date", LIFESPAN_TARGET))
    record.update(mean=m.mean, dist_sd=m.sd, skewness=m.skewness, excess_kurtosis=m.excess_kurtosis)
    return record


def _expected_quantile(dist: Distribution | LifeTableAge, target: str, q: float) -> float:
    if target == "date" and isinstance(dist, Uniform | Triangular | Discrete):
        return float(dist.quantile_int(q))
    return dist.quantile(q)


def _node_records(
    con: sqlite3.Connection, draws: list[sqlite3.Row],
) -> tuple[list[dict[str, Any]], dict[str, Distribution | LifeTableAge]]:
    """``node`` rows from one member file's draws and the rows behind them,
    and each node's distribution (for ``draw_check``)."""
    links = correlation_links(con)
    in_tree = tree_members(links)
    tails: dict[str, float] = {}
    if "vary_tail_dependence" in _columns(con, "calendar_event"):
        tails = dict(con.execute(
            "SELECT name, vary_tail_dependence FROM calendar_event "
            "WHERE vary_tail_dependence IS NOT NULL AND name IS NOT NULL"
        ).fetchall())
    cur = con.cursor()
    cur.row_factory = sqlite3.Row
    records: list[dict[str, Any]] = []
    dists: dict[str, Distribution | LifeTableAge] = {}
    for d in draws:
        name, target, distribution = d["name"], d["target"], d["distribution"]
        root = tree_root(links, name) if name in in_tree else None
        record: dict[str, Any] = {
            "name": name, "target": target, "distribution": distribution,
            "correlate_with": links[name].parent if name in links else None,
            "rho": links[name].rho if name in links else None,
            "tree_root": root, "nu": tails.get(root) if root else None,
        }
        dist: Distribution | LifeTableAge | None = None
        if target == LIFESPAN_TARGET:
            person = name.removeprefix(LIFESPAN_NODE_PREFIX)
            row = cur.execute(
                "SELECT l.*, p.birthdate, p.sex FROM person_lifespan l "
                "JOIN person p ON p.id = l.person WHERE l.person = ?", (person,),
            ).fetchone() if "sex" in _columns(con, "person") else cur.execute(
                "SELECT l.*, p.birthdate, NULL AS sex FROM person_lifespan l "
                "JOIN person p ON p.id = l.person WHERE l.person = ?", (person,),
            ).fetchone()
            record.update(node_kind="lifespan", affects=person, unit="years")
            if row is not None:
                record.update(birthdate=row["birthdate"],
                              sex=row["sex"] if distribution == LIFE_TABLE_DISTRIBUTION else None)
                if distribution:
                    try:
                        dist = lifespan_distribution({**dict(row), "vary_distribution": distribution}, con)
                    except EnsembleError:
                        dist = None
        elif target == FACTOR_TARGET:
            record.update(node_kind="factor", calendar_kind=LATENT_FACTOR_KIND, unit="score")
            row = cur.execute(
                "SELECT description FROM calendar_event WHERE name = ?", (name,),
            ).fetchone()
            record["description"] = row["description"] if row else None
        else:
            row = cur.execute("SELECT * FROM calendar_event WHERE name = ?", (name,)).fetchone()
            kind = row["kind"] if row else None
            record.update(node_kind="row", calendar_kind=kind, unit=_unit(target, kind))
            if row is not None:
                record.update(
                    affects=_marker_affects(cur, name) if kind == DATE_MARKER_KIND else _affects(row),
                    outcomes=_get(row, "vary_outcomes"),
                    ramp_months=_get(row, "vary_ramp_months"), process=_get(row, "vary_process"),
                    persistence=_get(row, "vary_persistence"),
                    shock_sd=_get(row, "vary_shock_sd"),
                    process_start=_get(row, "vary_process_start"), description=row["description"],
                )
                if distribution:
                    try:
                        dist = build_distribution({**dict(row), "vary_distribution": distribution}, target)
                    except EnsembleError:
                        dist = None
        if dist is not None:
            record.update(_dist_columns(dist, target))
            dists[name] = dist
        records.append(record)
    return records, dists


def _member_draws(con: sqlite3.Connection) -> list[sqlite3.Row]:
    if not _has_table(con, "ensemble_draw"):
        return []
    columns = _columns(con, "ensemble_draw")
    cur = con.cursor()
    cur.row_factory = sqlite3.Row
    return cur.execute(
        "SELECT calendar_event_name AS name, vary_target AS target, drawn_value, "
        f"{_optional(columns, 'quantile')}, {_optional(columns, 'distribution')}, "
        f"{_optional(columns, 'z')}, {_optional(columns, 'noise')}, {_optional(columns, 'u')} "
        "FROM ensemble_draw ORDER BY calendar_event_name"
    ).fetchall()


def ks_uniform(us: list[float]) -> tuple[float, float]:
    """The Kolmogorov-Smirnov distance of ``us`` from Uniform(0, 1), and its
    asymptotic p-value (the Kolmogorov series, with Stephens' small-sample
    correction), good to a few percent from about 5 members up."""
    n = len(us)
    xs = sorted(us)
    d = max(max((i + 1) / n - x, x - i / n) for i, x in enumerate(xs))
    root = math.sqrt(n)
    t = (root + 0.12 + 0.11 / root) * d
    if t < 0.2:
        return d, 1.0
    p = 2 * math.fsum((-1) ** (k - 1) * math.exp(-2 * k * k * t * t) for k in range(1, 101))
    return d, min(1.0, max(0.0, p))


def _check_rows(
    draws: pd.DataFrame, us: pd.DataFrame, dists: dict[str, Distribution | LifeTableAge],
    targets: dict[str, str], nus: dict[str, float],
) -> list[dict[str, Any]]:
    records = []
    for node in draws.columns:
        values = draws[node].dropna()
        if values.empty:
            continue
        record: dict[str, Any] = {
            "node": node, "n": len(values), "drawn_mean": float(values.mean()),
            "drawn_sd": float(values.std(ddof=1)) if len(values) > 1 else None,
        }
        for q in CHECK_QUANTILES:
            record[f"drawn_p{round(q * 100):02d}"] = float(values.quantile(q))
        dist = dists.get(node)
        if dist is not None:
            target = targets[node]
            m = moments(dist) if isinstance(dist, LifeTableAge) else moments(
                dist, whole_days=target == "date", rounded=target != "date")
            record.update(expected_mean=m.mean, expected_sd=m.sd)
            for q in CHECK_QUANTILES:
                record[f"expected_p{round(q * 100):02d}"] = _expected_quantile(dist, target, q)
        elif targets[node] == FACTOR_TARGET:
            # A factor is a standard normal score (ADR-008 §3), or a t
            # score with its tree's nu under a t copula.
            nu = nus.get(node)
            record["expected_mean"] = 0.0
            record["expected_sd"] = 1.0 if nu is None else math.sqrt(nu / (nu - 2)) if nu > 2 else None
            for q in CHECK_QUANTILES:
                record[f"expected_p{round(q * 100):02d}"] = score_at(q, nu)
        if node in us.columns:
            u = [float(x) for x in us[node].dropna()]
            if u:
                record["ks_u"], record["ks_u_p"] = ks_uniform(u)
        records.append(record)
    return records


def _pairs(
    links: dict[str, Link], nodes: list[str], scores: pd.DataFrame | None,
) -> list[dict[str, Any]]:
    from analysis.sim.sensitivity import score_correlations

    achieved = {}
    if scores is not None and not scores.empty:
        table = score_correlations(scores, links, nodes)
        achieved = {(r.a, r.b): r for r in named_rows(table)}
    records = []
    for i, a in enumerate(nodes):
        for b in nodes[i + 1:]:
            if a not in links and b not in links:
                continue
            if tree_root(links, a) != tree_root(links, b):
                continue
            rho = implied_correlation(links, a, b)
            direct = (a in links and links[a].parent == b) or (b in links and links[b].parent == a)
            got = achieved.get((a, b))
            records.append({
                "a": a, "b": b, "kind": "link" if direct else "implied", "implied_rho": rho,
                "expected_kendall_tau": expected_kendall_tau(rho),
                "achieved_rho": None if got is None else _finite(got.achieved),
                "achieved_low": None if got is None else _finite(got.low),
                "achieved_high": None if got is None else _finite(got.high),
                "n": None if got is None else int(got.n),
            })
    return records


def _finite(x: float) -> float | None:
    return None if x is None or not math.isfinite(float(x)) else float(x)


def _insert(con: sqlite3.Connection, table: str, records: list[dict[str, Any]]) -> None:
    columns = [c[1] for c in con.execute(f"PRAGMA table_info({table})")]
    con.executemany(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
        [tuple(r.get(c) for c in columns) for r in records],
    )


def build_draws_db(batch_dir: Path, intent_batch_dir: Path | None = None) -> Path:
    """Gather a batch's member files into ``<batch_dir>/draws.sqlite3``,
    replacing any earlier one, and return its path. ``intent_batch_dir``
    (``state/intent/<ensemble>/<batch>``) supplies the draws of a member
    whose run file is missing; leave it out to read run files only."""
    batch_dir = Path(batch_dir)
    manifest = read_batch_manifest(batch_dir) or {}
    members = _member_files(batch_dir, intent_batch_dir, manifest.get("member_count"))
    if not members:
        raise EnsembleError(f"{batch_dir}: no members to gather draws from")

    member_rows: list[dict[str, Any]] = []
    draw_rows: list[dict[str, Any]] = []
    scale_rows: list[dict[str, Any]] = []
    shock_rows: list[dict[str, Any]] = []
    nodes: list[dict[str, Any]] = []
    dists: dict[str, Distribution | LifeTableAge] = {}
    links: dict[str, Link] = {}
    # A batch whose every member failed to resolve has only its manifest:
    # it still lists them, with no nodes or draws.
    batch: dict[str, Any] = {
        "ensemble_name": batch_dir.parent.name, "batch": batch_dir.name,
        "mode": manifest.get("mode", "monte_carlo"), "member_count": manifest.get("member_count", len(members)),
    }
    # Nodes are described from member 0 where it exists, else the first file.
    describe_from = next((m for m in members if m.index == 0 and m.path), None) or next(
        (m for m in members if m.path), None)
    for m in members:
        record: dict[str, Any] = {"member_index": m.index, "is_base": int(m.index == 0),
                                  "status": m.status,
                                  "source_file": None if m.path is None else m.path.name}
        if m.path is None:
            member_rows.append(record)
            continue
        con = connect_sim_file(m.path)
        try:
            found = con.execute(
                "SELECT ensemble_name, member_count, seed, recipe_tree_id, mode "
                "FROM ensemble_manifest"
            ).fetchone() if "mode" in _columns(con, "ensemble_manifest") else con.execute(
                "SELECT ensemble_name, member_count, seed, recipe_tree_id, 'monte_carlo' "
                "FROM ensemble_manifest"
            ).fetchone()
            if found is not None:
                record["derived_seed"] = found[2]
            draws = _member_draws(con)
            for d in draws:
                draw_rows.append({
                    "member_index": m.index, "node": d["name"],
                    "value": _stored_value(d["target"], d["drawn_value"]),
                    "quantile": d["quantile"], "z": d["z"], "noise": d["noise"], "u": d["u"],
                })
            if _has_table(con, "ensemble_tail_scale"):
                scale_rows += [
                    {"member_index": m.index, "tree_root": r, "year": y, "scale": s}
                    for r, y, s in con.execute("SELECT tree_root, year, scale FROM ensemble_tail_scale")
                ]
            if _has_table(con, "ensemble_path_shock"):
                shock_rows += [
                    {"member_index": m.index, "node": n, "year": y, "noise": e, "score": z,
                     "scale": s, "shock": k, "rate": r}
                    for n, y, e, z, s, k, r in con.execute(
                        "SELECT node, year, noise, score, scale, shock, rate FROM ensemble_path_shock")
                ]
            if m is describe_from:
                nodes, dists = _node_records(con, draws)
                links = correlation_links(con)
                seed = con.execute("SELECT seed FROM scenario").fetchone()
                mode = manifest.get("mode") or (found[4] if found else "monte_carlo")
                batch = {
                    "ensemble_name": found[0] if found else batch_dir.parent.name,
                    "batch": batch_dir.name,
                    "mode": mode,
                    # An OAT batch draws nothing (ADR-007 §5), so no seed applies.
                    "master_seed": seed[0] if seed and mode == "monte_carlo" else None,
                    "member_count": manifest.get("member_count") or (found[1] if found else len(members)),
                    "recipe_tree_id": found[3] if found else None,
                    **_versions(con),
                }
        finally:
            con.close()
        member_rows.append(record)
    for node in nodes:
        base = next((d["value"] for d in draw_rows
                     if d["member_index"] == 0 and d["node"] == node["name"]), None)
        node["base_value"] = base
    batch.update(schema_version=SCHEMA_VERSION,
                 built_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))

    frame = pd.DataFrame(draw_rows)
    drawn = frame[frame["member_index"] != 0] if not frame.empty else frame
    targets = {n["name"]: n["target"] for n in nodes}
    mc = batch["mode"] == "monte_carlo"
    checks: list[dict[str, Any]] = []
    scores = None
    if mc and not drawn.empty:
        values = drawn.pivot(index="member_index", columns="node", values="value")
        us = drawn.pivot(index="member_index", columns="node", values="u")
        nus = {n["name"]: n["nu"] for n in nodes if n.get("nu") is not None}
        checks = _check_rows(values[[c for c in values.columns if c in targets]], us, dists, targets, nus)
        scores = drawn.pivot(index="member_index", columns="node", values="z").astype(float)
    tree = sorted(tree_members(links) & set(targets))
    pairs = _pairs(links, tree, scores)

    path = draws_db_path(batch_dir)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    con = connect_sim_file(tmp)
    try:
        con.executescript(DRAWS_DDL)
        con.executescript(_STATIC_VIEWS)
        con.executescript(_wide_views([(n["name"], n["unit"]) for n in nodes]))
        _insert(con, "batch", [{"id": 1, **batch}])
        _insert(con, "member", member_rows)
        _insert(con, "node", nodes)
        _insert(con, "node_pair", pairs)
        _insert(con, "draw", [d for d in draw_rows if d["node"] in targets])
        _insert(con, "tail_scale", scale_rows)
        _insert(con, "path_shock", shock_rows)
        _insert(con, "draw_check", checks)
        con.commit()
    finally:
        con.close()
    os.replace(tmp, path)
    return path


def _versions(con: sqlite3.Connection) -> dict[str, Any]:
    """code/data versions from a run file's ``simulation_run``, else an
    intent's ``intent_manifest`` (which has no code version of the run)."""
    if _has_table(con, "simulation_run"):
        row = con.execute("SELECT code_version, data_version FROM simulation_run").fetchone()
        if row is not None:
            return {"code_version": row[0], "data_version": row[1]}
    if _has_table(con, "intent_manifest"):
        row = con.execute("SELECT code_version, data_version FROM intent_manifest").fetchone()
        if row is not None:
            return {"code_version": row[0], "data_version": row[1]}
    return {}


# --- reading -----------------------------------------------------------------


@contextmanager
def open_draws_db(batch_dir: Path) -> Generator[sqlite3.Connection]:
    """A read-only connection to a batch's draws database."""
    path = draws_db_path(batch_dir)
    if not path.is_file():
        raise EnsembleError(f"{batch_dir}: no {DRAWS_DB_FILENAME}; 'sim ensemble draws --rebuild' makes one")
    con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        yield con
    finally:
        con.close()


def draws_frames(batch_dir: Path, *, display: bool = True) -> dict[str, pd.DataFrame]:
    """Every table of a batch's draws database as a DataFrame, keyed by
    table name; with ``display``, the translating views stand in for
    ``node``, ``draw``, ``draw_check`` and ``path_shock``, and ``draws_wide``
    is in display units."""
    names = ["batch", "member", "node", "node_pair", "draw", "tail_scale", "path_shock",
             "draw_check", "draws_wide"]
    views = {"node": "node_display", "draw": "draw_display", "draw_check": "draw_check_display",
             "path_shock": "path_shock_display", "draws_wide": "draws_wide_display"}
    with open_draws_db(batch_dir) as con:
        return {
            name: pd.read_sql_query(f"SELECT * FROM {views[name] if display and name in views else name}", con)
            for name in names
        }
