"""ADR-006: generating and summarizing ensemble scenarios.

``resolve_member`` (Phase 2) copies a recipe's intent file and resolves every
`calendar_event` row with `vary_distribution` set into a concrete, literal
value -- dates first, then amounts/rates (ADR-006 SS5). The result is an
ordinary intent file: `run_scenario`/`open_run_file` are never imported here
and never modified by this module.

``load_ensemble``/``members_net_worth_by_month``/``summary`` (Phase 4) read
back an already-run ensemble's member run files and report mean/median/SD/
range across members, the same wide-frame-then-row-stats shape
``notebook_ui.compare_plots.net_worth_by_month`` uses across scenarios.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import sqlite3
from calendar import monthrange
from datetime import UTC, date, datetime
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from analysis.sim.connection import RunConnection
from analysis.sim.distributions import (
    Discrete,
    Distribution,
    DistributionError,
    Triangular,
    TruncatedNormal,
    Uniform,
    member_rng,
    path_rng,
    path_tail_rng,
    parse_outcomes,
    resolve_truncated_normal_bounds,
    tail_rng,
)
from analysis.sim.correlation import (
    CorrelationError,
    Link,
    conditional_scores,
    probability,
    resolve_scores,
    score_at,
    standard_normal,
    tail_scale,
    tree_members,
    tree_root,
)
from analysis.sim.loader import BATCH_MANIFEST_FILENAME, latest_ensemble_batch
from analysis.sim import units
from analysis.sim.dates import final_month_for_age
from analysis.sim.life_table import (
    LifeTableAge, LifeTableError, life_table_in, months_since_birth,
)
from analysis.sim.registries import (
    ENSEMBLE_MODES,
    LATENT_FACTOR_KIND,
    LIFE_TABLE_DISTRIBUTION,
    LIFESPAN_NODE_PREFIX,
    LIFESPAN_TARGET,
    PROCESS_SHOCK_CLAMP,
)
from analysis.sim.types import Death, DollarMode
from analysis.sim.schema import connect_sim_file

__all__ = [
    "EnsembleError", "resolve_member", "load_ensemble", "member_run_statuses",
    "latest_ensemble_batch", "members_net_worth_by_month", "summary",
    "QUINTILES", "REPORT_PERCENTILES", "percentiles", "summary_with_percentiles",
    "write_batch_manifest", "read_batch_manifest", "oat_plan", "build_distribution",
    "factor_reach", "process_path", "varying_names", "lifespan_distribution",
    "life_table_age",
    "correlation_links",
    "members_balance_by_month", "TAIL_LEVELS", "tail_means", "reserve_shares",
    "recipe_reserve", "quantile_deltas", "member_pairs", "paired_deltas",
    "risk_summary", "comparison_deltas",
]

_MICROS = 1_000_000


class EnsembleError(RuntimeError):
    """A recipe's `vary_*` row can't be resolved as specified."""


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _member_seed(seed: int, member_index: int) -> int:
    """A per-member seed recorded in `ensemble_manifest` for humans/diagnostics.

    Not fed back into `member_rng` -- every row draw is always derived from
    the ensemble's own master ``seed`` plus the member index and row name
    (ADR-006 SS3), never from this value.
    """
    digest = hashlib.sha256(f"{seed}:{member_index}:member".encode()).digest()
    return int.from_bytes(digest[:8], "big") % (2**63)  # fits SQLite's signed 64-bit INTEGER


def _add_months_end(d: date, months: int) -> date:
    """``d`` advanced by ``months`` whole months, landing on that month's own
    last day (matches calendar.csv's month-end convention)."""
    total = d.year * 12 + (d.month - 1) + months
    year, month0 = divmod(total, 12)
    month = month0 + 1
    return date(year, month, monthrange(year, month)[1])


def _bound(raw: str | None, target: str) -> float | None:
    """A `vary_low`/`vary_high`/etc. cell as a float draw bound: the row's own
    unit for `amount` (already a plain int, stored as text); a date's
    ordinal for `date` (ADR-006 SS2's "target column's own unit")."""
    if raw in (None, ""):
        return None
    if target == "date":
        return float(date.fromisoformat(raw).toordinal())
    return float(raw)


def _required_bound(row: Mapping[str, Any], column: str, target: str) -> float:
    """``_bound`` of a column the row's distribution can't do without."""
    value = _bound(row[column], target)
    if value is None:
        raise DistributionError(f"{row['vary_distribution']} requires {column}")
    return value


def build_distribution(row: Mapping[str, Any], target: str) -> Distribution:
    """The distribution a varying row's ``vary_*`` columns describe, in the
    target column's own unit (a day ordinal for a date target). The loader
    already validated every row, so a failure here would mean an intent
    that skipped validation, not a normal case."""
    kind = row["vary_distribution"]
    try:
        if kind == "uniform":
            return Uniform(low=_required_bound(row, "vary_low", target),
                           high=_required_bound(row, "vary_high", target))
        if kind == "discrete":
            raw = row["vary_outcomes"]
            if not raw:
                raise DistributionError("discrete requires vary_outcomes")
            return Discrete(parse_outcomes(raw, lambda text, _role: _bound(text, target) or 0.0))
        if kind == "triangular":
            low = _required_bound(row, "vary_low", target)
            high = _required_bound(row, "vary_high", target)
            mode = _bound(row["vary_mode"], target)
            return Triangular(low, (low + high) / 2 if mode is None else mode, high)
        if kind == "truncated_normal":
            # vary_low/vary_high double as truncated_normal's own (optional)
            # clip bounds; any of median/sd/low/high left blank is inferred
            # from the others (distributions.resolve_truncated_normal_bounds).
            skew = _shape(row, "vary_skew", 0.0)
            tailweight = _shape(row, "vary_tailweight", 1.0)
            median, sd, low, high = resolve_truncated_normal_bounds(
                _bound(row["vary_median"], target), _bound(row["vary_sd"], target),
                _bound(row["vary_low"], target), _bound(row["vary_high"], target),
                skew=skew, tailweight=tailweight,
            )
            return TruncatedNormal(median=median, sd=sd, clip_low=low, clip_high=high,
                                   skew=skew, tailweight=tailweight)
    except DistributionError as e:
        raise EnsembleError(f"row {row['name']!r}: {e}") from e
    raise EnsembleError(f"row {row['name']!r}: unknown vary_distribution {kind!r}")


def _shape(row: Mapping[str, Any], column: str, default: float) -> float:
    """A unitless shape cell (#31), or its default when blank or absent."""
    value = row.get(column) if hasattr(row, "get") else row[column]
    return default if value in (None, "") else float(value)


def _row_aux_kind(row: sqlite3.Row) -> str:
    params = json.loads(row["payload_json"] or "{}")
    return params.get("aux_kind", "rate")


def _current_rate_value(con: sqlite3.Connection, row: sqlite3.Row) -> int:
    """The series' latest *known* rate as of ``row``'s own date: the later of
    the (not-yet-run) intent's own `aux_table` baseline and any literal
    (already-resolved, non-varying) `set_rate` calendar_event for the same
    series and aux_kind dated on or before this row's date.

    A rate change the recipe itself already authors earlier in the calendar
    is part of "the current rate", not just the load-time snapshot
    underneath it -- otherwise a ramp starting after such a change would
    reset to the pre-change value before ascending. This still doesn't
    pre-simulate the calendar in general: an *unresolved* (still-varying)
    earlier `set_rate` row is excluded, since its value isn't known yet
    either (ADR-006's open question on cross-variation ordering beyond the
    two-pass rule).
    """
    aux_kind = _row_aux_kind(row)
    candidates: list[tuple[str, int]] = []

    aux_found = con.execute(
        "SELECT effective_date, payload_json FROM aux_table "
        "WHERE kind = ? AND series = ? AND effective_date <= ? "
        "ORDER BY effective_date DESC LIMIT 1",
        (aux_kind, row["series"], row["date"]),
    ).fetchone()
    if aux_found is not None:
        candidates.append((aux_found[0], json.loads(aux_found[1])["annual_rate_micros"]))

    for ev_date, ev_amount, ev_payload in con.execute(
        "SELECT date, amount, payload_json FROM calendar_event "
        "WHERE kind = 'set_rate' AND series = ? AND date <= ? AND id != ? "
        "AND vary_distribution IS NULL AND amount IS NOT NULL "
        "ORDER BY date DESC",
        (row["series"], row["date"], row["id"]),
    ).fetchall():
        if (json.loads(ev_payload or "{}").get("aux_kind", "rate")) == aux_kind:
            candidates.append((ev_date, ev_amount))
            break

    if not candidates:
        raise EnsembleError(
            f"ramp on row {row['name']!r}: no existing {aux_kind!r} row for "
            f"series {row['series']!r} on or before {row['date']} to ramp from"
        )
    candidates.sort(key=lambda c: c[0])
    return candidates[-1][1]


def _apply_ramp(con: sqlite3.Connection, row: sqlite3.Row, target: int, months: int) -> None:
    """Expand one `vary_ramp_months` row into ``months`` additional ordinary
    `set_rate` rows, linearly interpolated from the series' current value up
    to ``target``, landing on it at ``row['date'] + months`` (ADR-006 SS4)."""
    current = _current_rate_value(con, row)
    con.execute(
        "UPDATE calendar_event SET amount = ?, vary_distribution = NULL WHERE id = ?",
        (current, row["id"]),
    )
    base_date = date.fromisoformat(row["date"])
    payload = json.loads(row["payload_json"] or "{}")
    payload.setdefault("aux_kind", _row_aux_kind(row))
    for k in range(1, months + 1):
        value = round(current + (target - current) * (k / months))
        step_date = _add_months_end(base_date, k)
        con.execute(
            "INSERT INTO calendar_event("
            "calendar_id, date, kind, series, amount, payload_json, description) "
            "VALUES (?, ?, 'set_rate', ?, ?, ?, ?)",
            (
                row["calendar_id"], step_date.isoformat(), row["series"], value,
                json.dumps(payload),
                f"ADR-006 ramp step {k}/{months} for {row['name']}",
            ),
        )


def _process_params(row: sqlite3.Row) -> tuple[float, int] | None:
    """A process row's ``(phi, shock SD in micros)`` (ADR-006 §4a), or
    ``None`` for a held draw. Intent files written before #115 have no
    process columns at all."""
    if "vary_process" not in row.keys() or not row["vary_process"]:
        return None
    return float(row["vary_persistence"]), int(row["vary_shock_sd"])


def _process_steps(con: sqlite3.Connection, row: sqlite3.Row, end: date) -> list[date]:
    """A process row's step dates (ADR-006 §4a): its own date, then the same
    month each later year, through the scenario's ``end`` and before the next
    authored ``set_rate`` for the same ``(aux_kind, series)`` (in the
    engine's ``date, id`` order), which takes over from there. Called after the date pass, so a varying date there has
    its drawn value, and before any ramp or path expands, so every
    ``set_rate`` row seen is an authored one."""
    aux_kind = _row_aux_kind(row)
    cutoff = next(
        (date.fromisoformat(ev_date) for ev_date, ev_payload in con.execute(
            # The engine applies same-date rows in id order, so a later row
            # on the process row's own date takes over from it too.
            "SELECT date, payload_json FROM calendar_event "
            "WHERE kind = 'set_rate' AND series = ? AND (date > ? OR (date = ? AND id > ?)) "
            "ORDER BY date, id",
            (row["series"], row["date"], row["date"], row["id"]),
        ) if json.loads(ev_payload or "{}").get("aux_kind", "rate") == aux_kind),
        None,
    )
    base = date.fromisoformat(row["date"])
    steps = [base]
    while True:
        step = _add_months_end(base, 12 * len(steps))
        if (step.year, step.month) > (end.year, end.month) or (cutoff and step >= cutoff):
            return steps
        steps.append(step)


def process_path(level_micros: int, persistence: float, shock_sd_micros: int,
                 shocks: list[float]) -> list[int]:
    """An AR(1) path's yearly rates in micros (ADR-006 §4a), one per shock:
    the log departure ``x`` of a year's growth factor from a held row's
    starts at its stationary spread and carries ``persistence`` of itself
    into each next year, plus that year's shock. All-zero shocks give a flat
    path at the level.

    The engine applies a rate ``r`` as ``r/12`` a month, so a year grows by
    ``(1 + r/12)**12``; each rate is chosen so that factor is the level's
    times ``exp(x)``, keeping the path's median compound growth a held
    row's and every month's factor positive."""
    level = level_micros / _MICROS
    sigma = shock_sd_micros / _MICROS
    rates: list[int] = []
    x = 0.0
    for k, eps in enumerate(shocks):
        x = (eps * sigma / math.sqrt(1 - persistence ** 2) if k == 0
             else persistence * x + sigma * eps)
        # Overflow protection only: realistic inputs never come near it.
        x = max(-PROCESS_SHOCK_CLAMP, min(PROCESS_SHOCK_CLAMP, x))
        rates.append(round(12 * ((1 + level / 12) * math.exp(x / 12) - 1) * _MICROS))
    return rates


def _apply_process(
    con: sqlite3.Connection, row: sqlite3.Row, rates: list[int], steps: list[date],
) -> None:
    """Write a process row's path (ADR-006 §4a): year 0 on the row itself,
    each later year as an ordinary ``set_rate`` row carrying the row's series
    and payload, as a ramp's steps do."""
    con.execute(
        "UPDATE calendar_event SET amount = ?, vary_distribution = NULL WHERE id = ?",
        (rates[0], row["id"]),
    )
    payload = json.loads(row["payload_json"] or "{}")
    payload.setdefault("aux_kind", _row_aux_kind(row))
    for k in range(1, len(rates)):
        con.execute(
            "INSERT INTO calendar_event("
            "calendar_id, date, kind, series, amount, payload_json, description) "
            "VALUES (?, ?, 'set_rate', ?, ?, ?, ?)",
            (
                row["calendar_id"], steps[k].isoformat(), row["series"], rates[k],
                json.dumps(payload), f"ADR-006 process year {k} for {row['name']}",
            ),
        )


def _yearly_shocks(
    links: dict[str, Link], factors: list[str], node_tails: dict[str, float],
    process_years: dict[str, list[int]], *, seed: int | None, member_index: int,
    flat: bool,
) -> tuple[dict[str, dict[int, float]], dict[str, dict[int, PathShock]], dict[tuple[str, int], float]]:
    """Each process row's shock for each calendar year of its path (ADR-006
    §4a, ADR-008 §5). A row in a tree takes that year's score, computed
    through the tree from every node's year noise (``path_rng``) and, in a
    t-copula tree, scaled by that year's ``S`` (``path_tail_rng``); keying by
    calendar year makes two paths' shocks in one year correlate wherever each
    starts. A row in no tree draws its own. ``flat`` (member 0, or an OAT
    member) sets every shock to 0, so every path sits at its level.

    Also returns, for inspection (#261), each shock's parts (``PathShock``)
    and each t-copula tree's yearly scale by ``(root, year)``."""
    if flat:
        return (
            {name: dict.fromkeys(years, 0.0) for name, years in process_years.items()},
            {name: {y: PathShock(None, None, None, 0.0) for y in years}
             for name, years in process_years.items()},
            {},
        )
    if seed is None:
        raise EnsembleError("a process path needs the ensemble's seed")
    members = tree_members(links) | set(factors)
    shocks: dict[str, dict[int, float]] = {
        name: {y: standard_normal(path_rng(seed, member_index, name, y)) for y in years}
        for name, years in process_years.items() if name not in members
    }
    parts: dict[str, dict[int, PathShock]] = {
        name: {y: PathShock(z, z, None, z) for y, z in by_year.items()}
        for name, by_year in shocks.items()
    }
    year_scales: dict[tuple[str, int], float] = {}
    roots = {name: tree_root(links, name) for name in members}
    by_root: dict[str, set[int]] = {}
    for name, years in process_years.items():
        if name in members:
            by_root.setdefault(roots[name], set()).update(years)
            shocks[name] = {}
            parts[name] = {}
    for root, years in sorted(by_root.items()):
        nodes = sorted(n for n, r in roots.items() if r == root)
        tree_links = {n: links[n] for n in nodes if n in links}
        for year in sorted(years):
            noise = {n: standard_normal(path_rng(seed, member_index, n, year)) for n in nodes}
            try:
                scores = resolve_scores(tree_links, noise)
            except CorrelationError as e:  # the loader rejects these first
                raise EnsembleError(str(e)) from e
            scores.update({n: noise[n] for n in nodes if n not in scores})
            nu = node_tails.get(root)
            scale = 1.0 if nu is None else tail_scale(nu, path_tail_rng(seed, member_index, root, year))
            if nu is not None:
                year_scales[(root, year)] = scale
            for name in nodes:
                if name in process_years and year in process_years[name]:
                    shocks[name][year] = scores[name] * scale
                    parts[name][year] = PathShock(
                        noise[name], scores[name], None if nu is None else scale, scores[name] * scale,
                    )
    return shocks, parts, year_scales


@dataclass(frozen=True)
class PathShock:
    """One year of a process path's shock (ADR-006 §4a), as recorded in
    ``ensemble_path_shock`` (#261): the node's own noise that year, its
    score after its tree mixes it in, the tree's t-copula scale that year
    (``None`` for a Gaussian tree or a lone row), and the shock the path
    took, ``score * scale``. Member 0 and OAT members record only the zero
    shock."""

    noise: float | None
    score: float | None
    scale: float | None
    shock: float


def _discrete(
    dist: Distribution | LifeTableAge, row: sqlite3.Row,
) -> Uniform | Triangular | Discrete:
    """``dist`` for a date target, which needs its whole-day (``*_int``) forms;
    the loader rejects truncated_normal there, which has none (and a life
    table only ever varies a lifespan)."""
    if isinstance(dist, TruncatedNormal | LifeTableAge):
        raise EnsembleError(f"row {row['name']!r}: {row['vary_distribution']} can't vary a date")
    return dist


def _resolve_value(
    row: sqlite3.Row, dist: Distribution | LifeTableAge, *, as_int: bool,
    overrides: dict[str, float] | None, seed: int | None, member_index: int,
    score: float | None = None, conditional: bool = False, nu: float | None = None,
) -> tuple[float, float | None, float | None]:
    """One varying row's value on one member, the OAT quantile it was
    resolved at (``None`` unless ``overrides`` names this row), and the
    probability ``u`` its value sits at in ``dist`` (#261):

    - named in ``overrides``: that quantile of ``dist`` (ADR-007 §2);
    - ``conditional``: an OAT member perturbing a latent factor above this
      row, which sits at its conditional median, ``dist.value_at(Phi(score))``
      (ADR-007 §2, ADR-008 §5);
    - otherwise, on member 0 or any OAT member: ``dist``'s central value;
    - otherwise, in a correlation tree (ADR-008): ``dist.value_at(Phi(score))``,
      ``score`` being this row's correlated standard normal score;
    - otherwise: a draw from this (member, row)'s own RNG stream (ADR-006 §3),
      exactly as before correlation existed.

    ``as_int`` selects the discrete (day-ordinal) form, for a date target.
    In a t-copula tree, ``nu`` is its degrees of freedom and the t CDF
    stands in for ``Phi`` throughout.

    ``u`` is the quantile for an override, ``Phi(score)`` for a scored row,
    0.5 for a central value (every family's center is its median), and for
    an independent random draw the uniform its stream gave (``_random_u``).
    """
    if overrides is not None and row["name"] in overrides:
        q = overrides[row["name"]]
        try:
            return (_discrete(dist, row).quantile_int(q) if as_int else dist.quantile(q)), q, q
        except DistributionError as e:
            raise EnsembleError(f"row {row['name']!r}: {e}") from e
    if conditional and score is not None:
        u = probability(score, nu)
        return (_discrete(dist, row).value_at_int(u) if as_int else dist.value_at(u)), None, u
    if overrides is not None or member_index == 0:
        return (_discrete(dist, row).central_int() if as_int else dist.central()), None, 0.5
    if score is not None:
        u = probability(score, nu)
        return (_discrete(dist, row).value_at_int(u) if as_int else dist.value_at(u)), None, u
    if seed is None:
        raise EnsembleError(f"row {row['name']!r}: a random draw needs the ensemble's seed")
    rng = member_rng(seed, member_index, row["name"])
    value = _discrete(dist, row).draw_int(rng) if as_int else dist.draw(rng)
    return value, None, _random_u(dist, as_int, value, seed, member_index, row["name"])


def _random_u(
    dist: Distribution | LifeTableAge, as_int: bool, value: float, seed: int, member_index: int,
    name: str,
) -> float:
    """The uniform an independent draw of ``value`` came from (#261), read
    off a fresh copy of its stream rather than by changing how ``draw``
    works, so no draw moves: every family's ``draw`` (and ``draw_int``, but
    a uniform date's) is its quantile at the stream's first ``random()``.
    A uniform date is ``randint`` over its days, so its ``u`` is the middle
    of the drawn day's share of the span."""
    if as_int and isinstance(dist, Uniform):
        low, high = round(dist.low), round(dist.high)
        return (value - low + 0.5) / (high - low + 1)
    return member_rng(seed, member_index, name).random()


def _resolve_lifespan(
    row: sqlite3.Row, con: sqlite3.Connection, *, overrides: dict[str, float] | None, seed: int | None,
    member_index: int, score: float | None, conditional: bool, nu: float | None,
) -> tuple[float, float | None, float | None]:
    """A varying lifespan's age on one member (#221), the OAT quantile it
    was resolved at and its ``u``, by ``_resolve_value``'s rules, with one difference: its
    base case (member 0, and an OAT member that doesn't move it) is the
    authored ``age_at_death``, not the distribution's median, so a run dies at
    the age written down, and has no ``u``. The age is whole months, as its
    final month is."""
    dist = lifespan_distribution(row, con)
    if overrides is None and member_index != 0 or row["name"] in (overrides or {}) or conditional:
        value, quantile, u = _resolve_value(
            row, dist, as_int=False, overrides=overrides, seed=seed,
            member_index=member_index, score=score, conditional=conditional, nu=nu,
        )
    else:
        value, quantile, u = row["age_at_death"], None, None
    return round(value * 12) / 12, quantile, u


def _has_columns(con: sqlite3.Connection, table: str, *columns: str) -> bool:
    present = {c[1] for c in con.execute(f"PRAGMA table_info({table})")}
    return set(columns) <= present


def _lifespan_rows(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """The varying lifespans (#221), each with its person's birthdate and its
    correlation node name; none in a file written before schema 40."""
    if not _has_columns(con, "person_lifespan", "vary_distribution"):
        return []
    cur = con.cursor()
    cur.row_factory = sqlite3.Row
    return cur.execute(
        "SELECT l.*, ? || l.person AS name, p.birthdate FROM person_lifespan l "
        "JOIN person p ON p.id = l.person "
        "WHERE l.vary_distribution IS NOT NULL ORDER BY l.person",
        (LIFESPAN_NODE_PREFIX,),
    ).fetchall()


def varying_names(con: sqlite3.Connection) -> list[str]:
    """Every unresolved varying node in an intent, sorted: the calendar rows
    with `vary_distribution` set, and the varying lifespans' nodes (#221)."""
    names = [name for (name,) in con.execute(
        "SELECT name FROM calendar_event WHERE vary_distribution IS NOT NULL AND name IS NOT NULL"
    )]
    if _has_columns(con, "person_lifespan", "vary_distribution"):
        names += [LIFESPAN_NODE_PREFIX + person for (person,) in con.execute(
            "SELECT person FROM person_lifespan WHERE vary_distribution IS NOT NULL"
        )]
    return sorted(names)


def correlation_links(con: sqlite3.Connection) -> dict[str, Link]:
    """Each varying calendar row's, factor's or lifespan's (#221) link to its
    correlation parent (ADR-008); tolerant of files from before either set
    of columns existed."""
    links: dict[str, Link] = {}
    if _has_columns(con, "calendar_event", "vary_correlate_with"):
        links.update({
            name: Link(parent, rho) for name, parent, rho in con.execute(
                "SELECT name, vary_correlate_with, vary_correlation FROM calendar_event "
                "WHERE vary_correlate_with IS NOT NULL AND name IS NOT NULL"
            )
        })
    if _has_columns(con, "person_lifespan", "vary_correlate_with"):
        links.update({
            LIFESPAN_NODE_PREFIX + person: Link(parent, rho)
            for person, parent, rho in con.execute(
                "SELECT person, vary_correlate_with, vary_correlation FROM person_lifespan "
                "WHERE vary_correlate_with IS NOT NULL"
            )
        })
    return links


def lifespan_distribution(
    row: sqlite3.Row | Mapping[str, Any], con: sqlite3.Connection,
) -> Uniform | Triangular | LifeTableAge:
    """A varying lifespan's distribution over ages (#221). A triangular's
    mode was filled with the authored age at load when left blank. A
    ``life_table`` one (#222) reads the person's sex and birthdate, the
    scenario start and the life table from ``con``, with the set-forward the
    loader solved."""
    if row["vary_distribution"] == LIFE_TABLE_DISTRIBUTION:
        return life_table_age(con, row["person"], row["life_table_set_forward"])
    try:
        if row["vary_distribution"] == "uniform":
            return Uniform(row["vary_low"], row["vary_high"])
        if row["vary_distribution"] == "triangular":
            return Triangular(row["vary_low"], row["vary_mode"], row["vary_high"])
    except DistributionError as e:
        raise EnsembleError(f"lifespan of {row['person']!r}: {e}") from e
    raise EnsembleError(
        f"lifespan of {row['person']!r}: {row['vary_distribution']!r} can't vary a lifespan"
    )


def life_table_age(con: sqlite3.Connection, person: str, set_forward: float) -> LifeTableAge:
    """``person``'s ``life_table`` distribution in the intent or run file
    behind ``con`` (#222), read ``set_forward`` years older."""
    found = con.execute("SELECT birthdate, sex FROM person WHERE id = ?", (person,)).fetchone()
    start = con.execute("SELECT start FROM scenario").fetchone()
    if found is None or not found[1] or start is None or set_forward is None:
        raise EnsembleError(f"lifespan of {person!r}: life_table needs a sex and a solved set-forward")
    try:
        table = life_table_in(con, found[1], start[0])
    except LifeTableError as e:
        raise EnsembleError(f"lifespan of {person!r}: {e}") from e
    return LifeTableAge(table, months_since_birth(found[0], start[0]), set_forward)


def _correlation_links(con: sqlite3.Connection) -> tuple[dict[str, Link], list[str]]:
    """The intent's correlation forest (ADR-008): each varying row's,
    lifespan's or factor's link to its parent, and the latent factors' names."""
    links = correlation_links(con)
    factors = [name for (name,) in con.execute(
        "SELECT name FROM calendar_event WHERE kind = ? AND name IS NOT NULL ORDER BY name",
        (LATENT_FACTOR_KIND,),
    )]
    return links, factors


def _tail_dependence(con: sqlite3.Connection) -> dict[str, float]:
    """Each t-copula tree's ``nu``, by its root's name (ADR-008 §1). The
    loader allows ``vary_tail_dependence`` only on a root."""
    columns = {c[1] for c in con.execute("PRAGMA table_info(calendar_event)")}
    if "vary_tail_dependence" not in columns:  # files written before #68
        return {}
    return dict(con.execute(
        "SELECT name, vary_tail_dependence FROM calendar_event "
        "WHERE vary_tail_dependence IS NOT NULL AND name IS NOT NULL"
    ).fetchall())


def _node_tails(
    links: dict[str, Link], members: set[str], tails: dict[str, float],
) -> dict[str, float]:
    """Each tree member's ``nu``, for the members whose tree's root sets one."""
    if not tails:
        return {}
    roots = {name: tree_root(links, name) for name in members}
    return {name: tails[root] for name, root in roots.items() if root in tails}


def _score_at(name: str, q: float, nu: float | None = None) -> float:
    """``Phi^-1(q)``, or the t inverse CDF with ``nu`` in a t-copula tree,
    exact at the ends: q = 0 or 1 (valid quantiles) is an infinite score,
    so a factor's family reaches its conditional endpoint -- each
    descendant at its own bound -- rather than just short of it.
    A quantile outside [0, 1] raises, as ``Distribution.quantile`` does for
    a row: a factor has no distribution to reject it later."""
    if not 0.0 <= q <= 1.0:
        raise EnsembleError(f"{name!r}: quantile must be in [0, 1] (got {q})")
    if q == 0.0:
        return -math.inf
    if q == 1.0:
        return math.inf
    return score_at(q, nu)


def _member_scores(
    links: dict[str, Link], factors: list[str], *, overrides: dict[str, float] | None,
    seed: int | None, member_index: int, node_tails: dict[str, float] | None = None,
) -> tuple[dict[str, float], set[str], dict[str, float], dict[str, float]]:
    """Every tree member's score on this member (ADR-008 §5),
    the rows an OAT member moves to a conditional median, and, for a random
    member, each node's own noise and each t-copula tree's scale ``S`` by
    its root, recorded for inspection (#261):

    - a random member: each node's noise from its own keyed stream
      (``member_rng(seed, member, name)``), combined down the tree;
    - member 0: all 0 -- every row at its median, its ``central()``;
    - an OAT member: 0, except an overridden node at ``Phi^-1(q)``. An
      overridden row's children aren't moved: that stays "this row alone".
      An overridden latent factor moves its whole family: each descendant
      to its conditional median given the factor, the product of rho along
      the path times the factor's score (ADR-007 §2). Those descendants are
      the second element.

    In a t-copula tree (``node_tails``, ADR-008 §1) a random member's
    scores are all multiplied by the tree's one scale ``S``, drawn on its own
    stream (``tail_rng``); an OAT member's overridden node sits at the t
    quantile instead, and its family follows by the same rho products. Trees
    without ``nu`` take exactly the Gaussian path.

    Every latent factor gets a score, even one nothing depends on yet (an
    unused factor still resolves to its own draw, ADR-008 §4, and is
    recorded). Rows outside every tree get no score and resolve exactly as
    before."""
    node_tails = node_tails or {}
    members = tree_members(links) | set(factors)
    if not members:
        return {}, set(), {}, {}
    if overrides is not None or member_index == 0:
        scores = dict.fromkeys(members, 0.0)
        moved: set[str] = set()
        for name, q in (overrides or {}).items():
            if name in scores:
                scores[name] = _score_at(name, q, node_tails.get(name))
                if name in factors:
                    family = conditional_scores(links, name, scores[name])
                    scores.update(family)
                    # A zero score (through a zero rho) is the row's median: it
                    # stays at central(), exactly the base, not value_at(0.5),
                    # which can differ by a day for a uniform date.
                    moved |= {n for n, z in family.items() if z != 0.0}
        return scores, moved, {}, {}
    if seed is None:
        raise EnsembleError("a correlated draw needs the ensemble's seed")
    noise = {name: standard_normal(member_rng(seed, member_index, name)) for name in members}
    try:
        scores = resolve_scores(links, noise)
        # An isolated factor (no parent, no dependents) is its own root.
        scores.update({name: noise[name] for name in members if name not in scores})
    except CorrelationError as e:  # the loader rejects these first
        raise EnsembleError(str(e)) from e
    scale: dict[str, float] = {}
    if node_tails:
        roots = {tree_root(links, name) for name in node_tails}
        scale = {
            root: tail_scale(node_tails[root], tail_rng(seed, member_index, root))
            for root in sorted(roots)
        }
        for name in node_tails:
            scores[name] *= scale[tree_root(links, name)]
    return scores, set(), noise, scale


def resolve_member(
    recipe_intent_path: Path,
    member_path: Path,
    *,
    seed: int | None,
    member_index: int,
    member_count: int,
    recipe_tree_id: str,
    overrides: dict[str, float] | None = None,
    mode: str = "monte_carlo",
) -> Path:
    """Copy ``recipe_intent_path`` to ``member_path`` and resolve every
    `vary_distribution` row into a literal value: every `vary_target='date'`
    row first, then every `vary_target='amount'` row (ADR-006 SS3, SS5).

    Member 0 is always deterministic -- each row's distribution central
    value rather than a draw -- so every ensemble has one member that's a
    plausible, in-bounds "base case" instead of just a bag of random draws,
    and anything that only knows how to open a single run file can treat an
    ensemble recipe's member 0 as that scenario's one run (ADR-006 §8).

    ``overrides`` (ADR-007 §2) makes this an OAT sensitivity member: a row
    named there resolves to that quantile of its distribution, and every
    other row to its central value, exactly as member 0's does -- no RNG is
    consulted at all. ``mode`` is recorded in ``ensemble_manifest``; a
    ``sensitivity`` member records a NULL seed, since it draws nothing, and
    ``seed`` may be ``None`` for one.
    """
    # Member 0 draws nothing, so a scenario's base case needs no seed.
    if mode == "monte_carlo" and seed is None and member_index != 0:
        raise EnsembleError("a Monte Carlo member needs the ensemble's seed")
    if mode not in ENSEMBLE_MODES:
        raise EnsembleError(f"unknown ensemble mode {mode!r}")
    member_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(recipe_intent_path, member_path)

    con = connect_sim_file(member_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT * FROM calendar_event WHERE vary_distribution IS NOT NULL"
        ).fetchall()
        lifespans = _lifespan_rows(con)
        links, factors = _correlation_links(con)
        node_tails = _node_tails(links, tree_members(links) | set(factors), _tail_dependence(con))
        if overrides is not None:
            unknown = (
                set(overrides) - {r["name"] for r in rows} - {r["name"] for r in lifespans}
                - set(factors)
            )
            if unknown:
                raise EnsembleError(
                    f"overrides name no unresolved vary_* row or latent factor: {sorted(unknown)}"
                )
        scores, moved, noise, level_scales = _member_scores(
            links, factors, overrides=overrides, seed=seed, member_index=member_index,
            node_tails=node_tails,
        )

        # (name, target, drawn_value, quantile, distribution, z, noise, u)
        draws: list[tuple[
            str, str, str, float | None, str | None, float | None, float | None, float | None,
        ]] = []

        for row in [r for r in rows if r["vary_target"] == "date"]:
            dist = build_distribution(row, "date")
            if not isinstance(dist, Uniform | Triangular | Discrete):
                # The loader rejects truncated_normal with a date target (its
                # sd is a numeric spread, not a date span); this is
                # unreachable for an intent that passed validation.
                raise EnsembleError(
                    f"row {row['name']!r}: {row['vary_distribution']!r} has no "
                    "discrete date draw"
                )
            drawn_ordinal, quantile, u = _resolve_value(
                row, dist, as_int=True, overrides=overrides, seed=seed, member_index=member_index,
                score=scores.get(row["name"]), conditional=row["name"] in moved,
                nu=node_tails.get(row["name"]),
            )
            drawn = date.fromordinal(int(drawn_ordinal))
            con.execute(
                "UPDATE calendar_event SET date = ?, vary_distribution = NULL WHERE id = ?",
                (drawn.isoformat(), row["id"]),
            )
            draws.append((row["name"], "date", drawn.isoformat(), quantile,
                          row["vary_distribution"], scores.get(row["name"]),
                          noise.get(row["name"]), u))

        for row in lifespans:
            age, quantile, u = _resolve_lifespan(
                row, con, overrides=overrides, seed=seed, member_index=member_index,
                score=scores.get(row["name"]), conditional=row["name"] in moved,
                nu=node_tails.get(row["name"]),
            )
            con.execute(
                "UPDATE person_lifespan SET age_at_death = ?, final_month = ?, "
                "vary_distribution = NULL WHERE scenario_id = ? AND person = ?",
                (age, final_month_for_age(row["birthdate"], age), row["scenario_id"],
                 row["person"]),
            )
            draws.append((row["name"], LIFESPAN_TARGET, f"{age:.4f}", quantile,
                          row["vary_distribution"], scores.get(row["name"]),
                          noise.get(row["name"]), u))

        # Process paths (ADR-006 §4a) need the drawn dates (a path stops at the
        # next set_rate for its series), and every yearly shock before the
        # amount pass expands anything.
        end = date.fromisoformat(con.execute('SELECT "end" FROM scenario').fetchone()[0])
        process_rows = {
            r["name"]: (params, _process_steps(con, r, end))
            for r in rows if r["vary_target"] == "amount"
            and (params := _process_params(r)) is not None
        }
        shocks, shock_parts, year_scales = _yearly_shocks(
            links, factors, node_tails,
            {name: [s.year for s in steps] for name, (_, steps) in process_rows.items()},
            seed=seed, member_index=member_index,
            flat=overrides is not None or member_index == 0,
        )

        path_shocks: list[tuple[str, int, float | None, float | None, float | None, float, int]] = []
        for row in [r for r in rows if r["vary_target"] == "amount"]:
            dist = build_distribution(row, "amount")
            value, quantile, u = _resolve_value(
                row, dist, as_int=False, overrides=overrides, seed=seed, member_index=member_index,
                score=scores.get(row["name"]), conditional=row["name"] in moved,
                nu=node_tails.get(row["name"]),
            )
            target = round(value)
            # A discrete outcome of exactly 0 is "nothing happens" (#165).
            floor = 0 if row["vary_distribution"] == "discrete" else 1
            if row["kind"] == "one_off_transfer" and target < floor:
                # Belt-and-suspenders: the loader already requires a strictly
                # positive uniform range / truncated_normal clip floor for a
                # transfer amount, so this should be unreachable -- but a
                # nonpositive draw reaching _post_transfer would reverse or
                # silently omit the transfer, so it's worth failing loudly
                # rather than trusting that invariant silently.
                raise EnsembleError(
                    f"row {row['name']!r}: drew a nonpositive one_off_transfer "
                    f"amount ({target}); its distribution's bounds should have "
                    "prevented this"
                )
            if row["vary_ramp_months"]:
                _apply_ramp(con, row, target, int(row["vary_ramp_months"]))
            elif row["name"] in process_rows:
                (phi, sd), steps = process_rows[row["name"]]
                yearly = shocks[row["name"]]
                rates = process_path(target, phi, sd, [yearly[s.year] for s in steps])
                _apply_process(con, row, rates, steps)
                parts = shock_parts[row["name"]]
                path_shocks.extend(
                    (row["name"], step.year, parts[step.year].noise, parts[step.year].score,
                     parts[step.year].scale, parts[step.year].shock, rate)
                    for step, rate in zip(steps, rates, strict=True)
                )
            else:
                con.execute(
                    "UPDATE calendar_event SET amount = ?, vary_distribution = NULL "
                    "WHERE id = ?",
                    (target, row["id"]),
                )
            draws.append((row["name"], "amount", str(target), quantile,
                          row["vary_distribution"], scores.get(row["name"]),
                          noise.get(row["name"]), u))
        # Factors are drawn too (ADR-008 §6), so achieved correlations can be
        # checked after the fact; their value is just their score.
        # An OAT-perturbed factor records its quantile, so the report can tell
        # which node a member moved (ADR-007 §5).
        # A factor's u is its score's probability, the copula's uniform for
        # it (#261): what its descendants' values are read at, when rho = 1.
        for factor in factors:
            z = scores.get(factor)
            if z is not None:
                quantile = overrides.get(factor) if overrides is not None else None
                u = quantile if quantile is not None else probability(z, node_tails.get(factor))
                draws.append((factor, "factor", repr(z), quantile, None, z, noise.get(factor), u))

        ensemble_name = con.execute("SELECT name FROM scenario").fetchone()[0]
        con.execute(
            "INSERT INTO ensemble_manifest("
            "id, ensemble_name, member_index, member_count, seed, "
            "recipe_tree_id, generated_at, mode) VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
            (
                ensemble_name, member_index, member_count,
                None if mode == "sensitivity" or seed is None else _member_seed(seed, member_index),
                recipe_tree_id, _now_iso(), mode,
            ),
        )
        con.executemany(
            "INSERT INTO ensemble_draw(calendar_event_name, vary_target, drawn_value, "
            "quantile, distribution, z, noise, u) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            draws,
        )
        # #261 (#72): each t-copula tree's scale, the member's (year NULL)
        # and each process year's, and every process path's yearly shocks.
        con.executemany(
            "INSERT INTO ensemble_tail_scale(tree_root, year, scale) VALUES (?, ?, ?)",
            [(root, None, s) for root, s in sorted(level_scales.items())]
            + [(root, year, s) for (root, year), s in sorted(year_scales.items())],
        )
        con.executemany(
            "INSERT INTO ensemble_path_shock(node, year, noise, score, scale, shock, rate) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            path_shocks,
        )
        con.commit()
    finally:
        con.close()
    return member_path


def factor_reach(intent_path: Path) -> dict[str, int]:
    """Each latent factor in an intent (ADR-008 §3), and how many unresolved
    varying rows perturbing it would move: its varying descendants, through
    any sub-factors, other than those it reaches only through a zero rho. A
    factor that moves none is a no-op to perturb."""
    con = connect_sim_file(intent_path)
    try:
        links, factors = _correlation_links(con)
        varying = set(varying_names(con))
    finally:
        con.close()
    try:
        # A descendant reached only through a zero rho stays at its median
        # (score 0), so it doesn't count: moving the factor wouldn't move it.
        return {
            f: sum(1 for name, z in conditional_scores(links, f, 1.0).items()
                   if name in varying and z != 0.0)
            for f in factors
        }
    except CorrelationError as e:  # the loader rejects these first
        raise EnsembleError(str(e)) from e


def oat_plan(
    row_names: list[str], quantiles: tuple[float, ...], only: list[str] | None = None,
    factors: Mapping[str, int] | None = None,
) -> list[dict[str, float]]:
    """The ``overrides`` for every member of an OAT sensitivity batch
    (ADR-007 §2): index 0 is ``{}`` (the base -- every row central), then one
    ``{name: q}`` per (row or factor, quantile), names sorted, quantiles
    ascending.

    ``factors`` maps each latent factor to how many varying rows it moves
    (``factor_reach``). A factor that moves at least one is swept like a
    row; one that moves none is left out, since its members would all equal
    the base.

    ``only`` restricts which rows and factors are perturbed; naming one that
    isn't varying (or isn't a factor), or a factor that moves nothing,
    raises, so a typo can't silently perturb nothing.
    """
    factors = dict(factors or {})
    names = set(row_names) | {f for f, reach in factors.items() if reach}
    if only is not None:
        unknown = set(only) - set(row_names) - set(factors)
        if unknown:
            raise EnsembleError(
                f"--only names no varying row or latent factor: {sorted(unknown)}"
            )
        idle = sorted(set(only) - names)
        if idle:
            raise EnsembleError(f"--only names a latent factor no varying row depends on: {idle}")
        names &= set(only)
    return [{}] + [{name: q} for name in sorted(names) for q in quantiles]


def write_batch_manifest(
    batch_dir: Path, *, member_count: int,
    failures: list[tuple[int, Path, str]],
    mode: str = "monte_carlo",
    quantiles: tuple[float, ...] | None = None,
    rows: list[str] | None = None,
) -> None:
    """Persist what ``_run_ensemble`` knows that a run file alone can't show:
    how many members were expected, and which ones failed before
    ``resolve_member``/``run_scenario`` ever produced a run file for them to
    record their own ``status='failed'`` in. Without this, a batch where
    every member's resolution fails has no ``member_*.sqlite3`` files at
    all and is invisible to every other function in this module.
    """
    path = Path(batch_dir) / BATCH_MANIFEST_FILENAME
    manifest: dict[str, Any] = {
        "mode": mode,
        "member_count": member_count,
        "failures": [
            {"index": i, "intent_path": str(intent_path), "error": error}
            for i, intent_path, error in failures
        ],
    }
    if mode == "sensitivity":
        # ADR-007 §5: what an OAT batch perturbed, in oat_plan's member order.
        manifest["quantiles"] = list(quantiles or ())
        manifest["rows"] = list(rows or ())
    path.write_text(json.dumps(manifest, indent=2))


def read_batch_manifest(batch_dir: Path) -> dict[str, Any] | None:
    """``write_batch_manifest``'s record for this batch, or ``None`` for a
    batch generated before it existed (or synthesized directly by a test,
    bypassing ``_run_ensemble``)."""
    path = Path(batch_dir) / BATCH_MANIFEST_FILENAME
    if not path.is_file():
        return None
    manifest = json.loads(path.read_text())
    # Batches written before ADR-007 carry no mode; they're all Monte Carlo.
    manifest.setdefault("mode", "monte_carlo")
    return manifest


def member_run_statuses(batch_dir: Path) -> dict[str, str]:
    """``simulation_run.status`` of every ``member_*.sqlite3`` run file
    directly under ``batch_dir``, keyed by filename stem, merged with
    ``batch_manifest.json``'s record of any member that failed before a run
    file could even be created (``resolve_member`` itself raising) --
    reported as ``"failed_to_resolve"``, so it isn't silently absent from a
    count that's supposed to total ``member_count``.

    A member whose run raised mid-simulation still has a run file --
    ``run_scenario`` writes ``status='failed'`` before re-raising, rather
    than deleting it, so its (partial, not reliably comparable) history
    stays inspectable -- so status has to be checked explicitly rather than
    assuming every file found is a succeeded member.
    """
    statuses: dict[str, str] = {}
    for p in sorted(Path(batch_dir).glob("member_*.sqlite3")):
        con = connect_sim_file(p)
        try:
            row = con.execute("SELECT status FROM simulation_run").fetchone()
        finally:
            con.close()
        statuses[p.stem] = row[0] if row else "unknown"
    manifest = read_batch_manifest(batch_dir)
    if manifest:
        for failure in manifest.get("failures", []):
            stem = f"member_{failure['index']:04d}"
            statuses.setdefault(stem, "failed_to_resolve")
    return statuses


def load_ensemble(batch_dir: Path) -> dict[str, RunConnection]:
    """Every succeeded ``member_*.sqlite3`` run file directly under
    ``batch_dir``, keyed by filename stem. Caller owns closing each
    connection. A failed member (see ``member_run_statuses``) is excluded --
    it would otherwise distort the mean/range with a partial run -- so
    reporting the failure count/detail alongside a summary means calling
    ``member_run_statuses`` too.
    """
    statuses = member_run_statuses(batch_dir)
    if not statuses:
        raise EnsembleError(f"no member run files found under {batch_dir}")
    succeeded = [stem for stem, status in statuses.items() if status == "succeeded"]
    if not succeeded:
        raise EnsembleError(
            f"no succeeded member run files found under {batch_dir} "
            f"({len(statuses)} present, none succeeded: {statuses})"
        )
    return {stem: RunConnection(Path(batch_dir) / f"{stem}.sqlite3") for stem in succeeded}


def _each_member(
    connections: dict[str, RunConnection], on_member: Callable[[], None] | None,
) -> Iterator[tuple[str, RunConnection]]:
    """``connections``' items, calling ``on_member`` after each one's read,
    so a notebook can show progress through a large ensemble (#235)."""
    for item in connections.items():
        yield item
        if on_member is not None:
            on_member()


def members_net_worth_by_month(
    connections: dict[str, RunConnection], mode: DollarMode = DollarMode.NOMINAL,
    on_member: Callable[[], None] | None = None, *, after_tax: bool = False,
) -> pd.DataFrame:
    """Net worth (USD) by month, one column per member, after tax with
    ``after_tax`` (#251). ``on_member`` is called after each member is read."""
    return pd.DataFrame({
        name: con.net_worth_series(mode, after_tax=after_tax)
        for name, con in _each_member(connections, on_member)
    })


def summary(frame: pd.DataFrame) -> pd.DataFrame:
    """Mean/median/SD/range across ``frame``'s columns (one column per
    member), row by row (ADR-006 SS8)."""
    return pd.DataFrame({
        "mean": frame.mean(axis=1),
        "median": frame.median(axis=1),
        "std": frame.std(axis=1),
        "min": frame.min(axis=1),
        "max": frame.max(axis=1),
    })


QUINTILES = (0.2, 0.4, 0.6, 0.8)

# The quintile breakpoints plus a 5th/95th-percentile envelope: what the
# ensemble table and fan chart report in place of min/max (#155).
REPORT_PERCENTILES = (0.05, *QUINTILES, 0.95)


def percentiles(frame: pd.DataFrame, qs: tuple[float, ...] = QUINTILES) -> pd.DataFrame:
    """The ``qs`` quantiles across ``frame``'s columns (one column per
    member), row by row, as columns ``p05``, ``p20``, ... (at least two
    digits, so they line up and sort as text) -- a fan chart's band edges."""
    return pd.DataFrame(
        {f"p{q * 100:02g}": frame.quantile(q, axis=1) for q in qs},
        index=frame.index,
    )


def summary_with_percentiles(
    frame: pd.DataFrame, qs: tuple[float, ...] = REPORT_PERCENTILES,
) -> pd.DataFrame:
    """``summary()``'s mean/median/std, then the ``qs`` percentiles low to
    high: the one frame the notebook's year-end table, its fan chart and
    ``sim ensemble summary`` all report (#155). Min and max are left out:
    with a large ensemble they sit far beyond where most members land and
    read as typical outcomes when they're one member's luck, so the outer
    edge is the 5th/95th percentile instead. ``qs`` is sorted, so a row
    stays low to high whatever order a caller gives."""
    return pd.concat(
        [summary(frame)[["mean", "median", "std"]], percentiles(frame, tuple(sorted(qs)))],
        axis=1,
    )


def members_balance_by_month(
    connections: dict[str, RunConnection], account: str | None = None,
    mode: DollarMode = DollarMode.NOMINAL, lot: str | None = None,
    on_member: Callable[[], None] | None = None, *, after_tax: bool = False,
) -> pd.DataFrame:
    """One account's balance (USD) by month, one column per member, or net
    worth when ``account`` is None (#163), after tax with ``after_tax``
    (#251), or one ``lot`` of the account (#170). Each member converts its own series to CPI-constant dollars,
    since its inflation path is its own. ``on_member`` is called after
    each member is read (#235)."""
    if account is None:
        if lot is not None:
            raise EnsembleError("a lot needs its account")
        return members_net_worth_by_month(connections, mode, on_member, after_tax=after_tax)
    if after_tax:
        raise EnsembleError("after-tax figures are for net worth, not one account")
    if lot is None:
        frame = pd.DataFrame({
            name: con.account_balance(account, mode)
            for name, con in _each_member(connections, on_member)
        })
        if frame.empty:
            raise EnsembleError(f"no account {account!r} in these members")
        return frame
    frame = pd.DataFrame({
        name: con.lot_balance(account, lot, mode)
        for name, con in _each_member(connections, on_member)
    })
    if frame.empty:
        raise EnsembleError(f"no lot {lot!r} of account {account!r} in these members")
    return frame


# The tail fractions whose mean ("CVaR", expected shortfall) the ensemble
# reports and compares (#163). Fixed rather than chosen per comparison: a
# standard pair leaves nothing to fiddle with until a comparison looks better.
TAIL_LEVELS = (0.05, 0.20)


def tail_means(frame: pd.DataFrame, levels: tuple[float, ...] = TAIL_LEVELS) -> pd.DataFrame:
    """CVaR row by row: the mean of the worst ``level`` share of ``frame``'s
    columns (one per member), as columns ``cvar05``, ``cvar20``. The worst
    share is rounded up to whole members, at least one. A tail's mean is
    steadier than the single breakpoint at its edge, and says how bad the
    bad futures are, not just where they start (#163)."""
    ordered = np.sort(frame.to_numpy(dtype=float), axis=1)
    n = ordered.shape[1]
    return pd.DataFrame(
        # The epsilon keeps 0.2 * 80 at 16 rather than float-rounding to 17.
        {f"cvar{q * 100:02g}": ordered[:, :max(1, math.ceil(q * n - 1e-9))].mean(axis=1)
         for q in levels},
        index=frame.index,
    )


def reserve_shares(frame: pd.DataFrame, reserve: float) -> pd.DataFrame:
    """Row by row, the share of members below ``reserve`` that month
    (``below_reserve``) and the share that have been below it at any month
    so far (``ever_below_reserve``). ``frame`` and ``reserve`` must be in
    the same dollars: the reserve is CPI-constant, so pass a CPI-constant
    frame."""
    below = frame < reserve
    return pd.DataFrame({
        "below_reserve": below.mean(axis=1),
        "ever_below_reserve": below.cummax().mean(axis=1),
    })


def recipe_reserve(connections: dict[str, RunConnection]) -> float | None:
    """The ``scenario.meta_json`` ``reserve`` (CPI-constant dollars) the
    members ran with, or None when the recipe sets none (#163)."""
    con = next(iter(connections.values()))
    raw = con.scenario_meta().get("reserve")
    return None if raw is None else units.json_dollars_to_cents(raw) / 100


def quantile_deltas(
    baseline: pd.DataFrame, variant: pd.DataFrame,
    qs: tuple[float, ...] = REPORT_PERCENTILES,
) -> pd.DataFrame:
    """``variant``'s percentiles minus ``baseline``'s, row by row: each
    breakpoint against the same breakpoint (``p05`` ... ``p95``). Valid for
    any two ensembles; the members at a breakpoint needn't be the same
    future on both sides."""
    qs = tuple(sorted(qs))
    return percentiles(variant, qs) - percentiles(baseline, qs)


def base_case_deaths(connections: Mapping[str, RunConnection]) -> list[Death]:
    """The deaths an ensemble chart marks (#255): member 0's, the base case,
    whose lifespans are the authored ages. Other members' vary. None when
    member 0 isn't among the succeeded members."""
    for con in connections.values():
        member = con.ensemble_member()
        if member is not None and member[0] == 0:
            return con.deaths()
    return []


def member_pairs(
    baseline: dict[str, RunConnection], variant: dict[str, RunConnection],
) -> tuple[list[tuple[str, str]], str | None]:
    """Members of the two ensembles that drew the same future: same member
    index, same seed. Draws are keyed by (seed, member, row name), and a
    child recipe inherits its parent's seed, so these share every draw for
    every row the recipes share (ADR-006 §3). Returns the ``(baseline stem,
    variant stem)`` pairs, or no pairs and the reason they can't be paired."""

    def by_index(connections: dict[str, RunConnection]) -> dict[int, tuple[str, int | None]]:
        members = {}
        for stem, con in connections.items():
            member = con.ensemble_member()
            if member is not None:
                members[member[0]] = (stem, member[1])
        return members

    left, right = by_index(baseline), by_index(variant)
    common = sorted(left.keys() & right.keys())
    if not common:
        return [], "no member index succeeded in both ensembles"
    if any(left[i][1] is None or left[i][1] != right[i][1] for i in common):
        return [], "the recipes have different seeds, so their members drew different futures"
    return [(left[i][0], right[i][0]) for i in common], None


def paired_deltas(
    baseline: pd.DataFrame, variant: pd.DataFrame, pairs: list[tuple[str, str]],
) -> pd.DataFrame:
    """Per-future differences, ``variant`` minus ``baseline``, one column per
    pair from ``member_pairs`` (named for the baseline member)."""
    return pd.DataFrame({a: variant[b] - baseline[a] for a, b in pairs})


def risk_summary(
    frame: pd.DataFrame, reserve: float | None = None,
    cpi_frame: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """``tail_means(frame)``, plus ``reserve_shares`` against ``reserve``
    when one is given: computed on ``cpi_frame`` (the same members in
    CPI-constant dollars) when ``frame`` is nominal, since the reserve is
    CPI-constant."""
    tails = tail_means(frame)
    if reserve is None:
        return tails
    shares = reserve_shares(frame if cpi_frame is None else cpi_frame, reserve)
    return pd.concat([tails, shares], axis=1)


def comparison_deltas(
    baseline: pd.DataFrame, variant: pd.DataFrame,
    pairs: list[tuple[str, str]] | None = None, reserve: float | None = None,
    cpi_frames: tuple[pd.DataFrame, pd.DataFrame] | None = None,
) -> pd.DataFrame:
    """Month by month, ``variant`` against ``baseline``: each percentile
    breakpoint's delta (``Δp05`` ...), each tail mean's (``Δcvar05``,
    ``Δcvar20``), given a ``reserve`` the change in each reserve share
    (``Δbelow_reserve``, ``Δever_below_reserve``; measured on
    ``cpi_frames``, the two sides in CPI-constant dollars, when the frames
    are nominal), and, given ``member_pairs``, the share of paired futures
    where the variant comes out ahead (``P(ahead)``) (#163)."""
    parts = [quantile_deltas(baseline, variant), tail_means(variant) - tail_means(baseline)]
    if reserve is not None:
        base_cpi, variant_cpi = cpi_frames or (baseline, variant)
        parts.append(reserve_shares(variant_cpi, reserve) - reserve_shares(base_cpi, reserve))
    deltas = pd.concat(parts, axis=1).add_prefix("Δ")
    if pairs:
        deltas["P(ahead)"] = (paired_deltas(baseline, variant, pairs) > 0).mean(axis=1)
    return deltas
