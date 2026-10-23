"""ADR-007: sensitivity analysis over an ensemble recipe's varying rows.

An OAT (one-at-a-time) batch -- ``sim run <recipe> --sensitivity`` -- holds a
base member (every varying row at its central value) plus one member per
(row, quantile). This module turns those member run files into per-year
deltas against the base (§4), the per-year tornado table and rank-by-year
grid (§6.1), and the exact attribution of a delta to income/expense flows or
holdings (§6.1). A Monte Carlo batch gets the rank-correlation report instead
(§6.2).

Everything is computed at report time from the member run files' full
monthly history; nothing about the reporting years, units or measure is
baked into the batch.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd


from analysis.frames import index_years
from analysis.sim.connection import RunConnection, year_end_values
from analysis.sim.correlation import Link, implied_correlation, rho_from_kendall_tau, tree_root
from analysis.sim.distributions import Discrete, Distribution, Triangular, TruncatedNormal, moments
from analysis.sim.life_table import LifeTableAge
from analysis.sim.ensemble import (
    EnsembleError,
    correlation_links,
    lifespan_distribution,
    load_ensemble,
    read_batch_manifest,
)
from analysis.sim.registries import LIFESPAN_NODE_PREFIX, LIFESPAN_TARGET, format_calendar_amount
from analysis.sim.types import DollarMode
from analysis.sim.schema import connect_sim_file

__all__ = [
    "OatMember", "MEASURES", "RELATIVE_NEAR_ZERO_DOLLARS", "ASYMMETRY_RATIO", "BEND_FACTOR",
    "DEFAULT_NEGLIGIBLE", "oat_members", "member_measures", "oat_deltas", "oat_table",
    "rank_by_year", "pin_candidates", "default_report_years", "months_per_year",
    "display_value", "ATTRIBUTION_AXES", "OPENING_BALANCES", "UNATTRIBUTED", "TOTAL",
    "attribution", "oat_attribution", "base_net_worth", "parameters", "R2_TRUST", "draws_frame", "outcomes_frame",
    "rho_interval", "required_members", "correlation_table", "classify", "mc_correlation",
    "has_correlated_rows", "FACTOR_TARGET", "factor_rows", "row_label", "RANKING_MEASURES",
    "score_correlations", "McReport", "mc_report",
]

MEASURES: tuple[str, ...] = ("net_worth", "min_net_worth")
"""ADR-007 §4: net worth at each reporting year, and the lowest net worth from
the start through that year ("how close do I get to running out")."""

RELATIVE_NEAR_ZERO_DOLLARS = 1_000.0
"""Relative delta is blank (NaN) where the base is within this many dollars
of zero -- a percentage of almost nothing is noise, not a finding."""

ASYMMETRY_RATIO = 2.0
"""``asymmetric`` flag: the larger outer |delta| exceeds the smaller by more
than this factor. A first guess (ADR-007 plan, Risks); revisit with real
batches."""

BEND_FACTOR = 3.0
"""``bend`` flag (ladders of >= 4 quantiles): the steepest slope between
adjacent rungs exceeds the median slope by more than this factor, or the
slope changes sign."""

DEFAULT_NEGLIGIBLE = 0.05
"""Share of the total impact below which a row is a pin candidate -- in every
reporting year, not just the last."""

FACTOR_TARGET = "factor"
"""The ``target`` of a latent factor (ADR-008 §3) in an OAT batch's deltas
and tables: perturbing one moves its whole family (ADR-007 §2)."""

_FLAT_DOLLARS = 1.0
"""Deltas smaller than this (or this fraction, for relative) count as zero."""


@dataclass(frozen=True)
class OatMember:
    """One OAT member: which row it perturbed and to which quantile
    (``row``/``quantile`` are ``None`` for the base)."""

    stem: str
    row: str | None
    quantile: float | None
    target: str | None
    value: str | None
    """The resolved value, formatted for display (``display_value``)."""


def _perturbed_quantile(member: OatMember) -> float:
    if member.quantile is None:
        raise EnsembleError(f"{member.stem}: a perturbed OAT member records no quantile")
    return member.quantile


def _day_ordinal(raw: str) -> int:
    """A drawn date's day ordinal."""
    stamp = pd.Timestamp(raw)
    if not isinstance(stamp, pd.Timestamp):  # NaT
        raise EnsembleError(f"drawn date {raw!r} is not a date")
    return stamp.toordinal()


def display_value(kind: str | None, target: str, raw: str) -> str:
    """An ``ensemble_draw.drawn_value`` in display units: a date as-is, a
    latent factor's standard normal score as ``z=+1.28`` (``z=+inf`` at an
    endpoint quantile), a lifespan's age as ``age 84.5`` (#221), a ``set_rate``
    amount (micros) as a percent, any other amount (cents) as dollars."""
    if target == "date":
        return raw
    if target == LIFESPAN_TARGET:
        return f"age {float(raw):.1f}"
    if target == FACTOR_TARGET:
        return f"z={float(raw):+.2f}"
    return format_calendar_amount(kind or "", int(raw), cents=False, rate_places=2)


def oat_members(connections: dict[str, RunConnection]) -> list[OatMember]:
    """Read each member's own ``ensemble_draw`` (ADR-007 §5) to find which
    row it perturbed: the one row with a non-NULL ``quantile``, or none for
    the base. Self-documenting -- nothing is inferred from file order."""
    members = []
    for stem, rc in connections.items():
        con = connect_sim_file(rc.run_path)
        try:
            row = con.execute(
                "SELECT d.calendar_event_name, d.quantile, d.vary_target, d.drawn_value, c.kind "
                "FROM ensemble_draw d "
                # A lifespan's draw (#221) has no calendar row.
                "LEFT JOIN calendar_event c ON c.name = d.calendar_event_name "
                "WHERE d.quantile IS NOT NULL"
            ).fetchone()
        finally:
            con.close()
        if row is None:
            members.append(OatMember(stem, None, None, None, None))
        else:
            name, q, target, raw, kind = row
            members.append(OatMember(stem, name, q, target, display_value(kind, target, raw)))
    return members


def member_measures(rc: RunConnection, dollars: DollarMode) -> pd.DataFrame:
    """Year-indexed ``net_worth`` and ``min_net_worth`` (ADR-007 §4) for one
    member, in that member's own dollars -- its own CPI path, which differs
    from the base's when the perturbed row is the inflation rate."""
    column = "cpi_constant" if dollars == DollarMode.CPI_CONSTANT else "nominal"
    monthly = rc.net_worth_frame()[column].astype(float)
    frame = pd.DataFrame({"net_worth": monthly, "min_net_worth": monthly.cummin()})
    return year_end_values(frame)


def oat_deltas(batch_dir: Path, dollars: DollarMode = DollarMode.CPI_CONSTANT) -> pd.DataFrame:
    """Long-form deltas against the base for every perturbed member of an
    OAT batch: one row per (row, quantile, year, measure) with columns
    ``row, target, quantile, value, year, measure, level, base, delta,
    relative``. ``relative`` is ``delta / |base|``, NaN near zero."""
    connections = load_ensemble(batch_dir)
    try:
        members = oat_members(connections)
        base = [m for m in members if m.row is None]
        if not base:
            raise EnsembleError(
                f"{batch_dir}: no succeeded base member (every varying row central); "
                "deltas need one"
            )
        base_measures = member_measures(connections[base[0].stem], dollars)
        frames = []
        for m in members:
            if m.row is None:
                continue
            measures = member_measures(connections[m.stem], dollars)
            for measure in MEASURES:
                level = measures[measure]
                base_level = base_measures[measure].reindex(level.index)
                delta = level - base_level
                relative = delta / base_level.abs().where(
                    base_level.abs() >= RELATIVE_NEAR_ZERO_DOLLARS
                )
                frames.append(pd.DataFrame({
                    "row": m.row, "target": m.target, "quantile": m.quantile,
                    "value": m.value, "year": level.index, "measure": measure,
                    "level": level.to_numpy(), "base": base_level.to_numpy(),
                    "delta": delta.to_numpy(), "relative": relative.to_numpy(),
                }))
    finally:
        for rc in connections.values():
            rc.close()
    if not frames:
        raise EnsembleError(f"{batch_dir}: no succeeded perturbed members")
    result = pd.concat(frames, ignore_index=True)
    # The batch's own quantile list (ADR-007 §5), so oat_table can tell a
    # missing arm even when that quantile failed for every row.
    manifest = read_batch_manifest(batch_dir) or {}
    result.attrs["quantiles"] = tuple(manifest.get("quantiles") or sorted(result["quantile"].unique()))
    return result


def _flags(points: pd.Series, flat: float) -> list[str]:
    """``asymmetric``/``bend`` (ADR-007 §3) from one row's quantile-indexed
    deltas at one year. The base contributes a (0.5, 0) point."""
    flags = []
    lo, hi = abs(points.iloc[0]), abs(points.iloc[-1])
    if points.index[0] < 0.5 < points.index[-1] and max(lo, hi) > flat:
        if max(lo, hi) > ASYMMETRY_RATIO * max(min(lo, hi), flat):
            flags.append("asymmetric")
    if len(points) >= 4:
        ladder = pd.concat([points, pd.Series({0.5: 0.0})]).sort_index()
        slopes = np.diff(ladder.to_numpy()) / np.diff(ladder.index.to_numpy())
        nonflat = slopes[np.abs(slopes) > flat]
        if len(nonflat) >= 2 and (np.any(nonflat > 0) and np.any(nonflat < 0)):
            flags.append("bend")
        elif len(nonflat) and np.max(np.abs(slopes)) > BEND_FACTOR * np.median(np.abs(slopes)):
            flags.append("bend")
    return flags


def oat_table(
    deltas: pd.DataFrame, year: int, *, measure: str = "net_worth", relative: bool = False,
) -> pd.DataFrame:
    """ADR-007 §6.1's per-year tornado table: one row per perturbed varying
    row or latent factor, sorted by ``impact`` descending, with ``value@q`` and ``delta@q`` per
    quantile, ``swing`` (signed: delta at the highest quantile minus at the
    lowest, for direction), ``impact`` (the spread of every delta *and the
    base's zero*: ``max(0, deltas) - min(0, deltas)``), ``share`` (impact
    over the sum of the ordinary rows' impacts) and ``flags``.

    A latent factor's effect reaches the outcome only through its rows, so
    its impact is left out of the ``share`` denominator, which would
    otherwise count that effect twice. Its own share is still over that
    total: "the whole family" against the rows taken one at a time. (When
    the ordinary rows' total is zero -- a batch that perturbed only factors,
    say -- shares are over the factors' total instead.)

    Importance is ``impact``, not ``|swing|``: a non-monotonic row whose low
    and high quantiles both move the outcome the same way has a swing near
    zero but a large impact, and must not read as negligible.

    A row that can't be measured gets NaN ``swing``/``impact``/``share``
    rather than a number that would read as "no effect", and so can never
    be a pin candidate: ``incomplete`` when a member for one of the batch's
    quantiles failed (the survivor would otherwise stand in for both ends),
    ``undefined`` when a relative delta is blank because the base is near
    zero."""
    column = "relative" if relative else "delta"
    flat = _FLAT_DOLLARS / 1e5 if relative else _FLAT_DOLLARS
    at = deltas[(deltas["year"] == year) & (deltas["measure"] == measure)]
    if at.empty:
        raise EnsembleError(f"no deltas for year {year} ({measure})")
    expected = set(deltas.attrs.get("quantiles") or deltas["quantile"].unique())
    rows = []
    for name, group in at.groupby("row", sort=False):
        group = group.sort_values("quantile")
        points = pd.Series(group[column].to_numpy(dtype=float), index=group["quantile"].to_numpy())
        record: dict[str, object] = {"row": name, "target": group["target"].iloc[0]}
        for q, value in zip(group["quantile"], group["value"], strict=True):
            record[f"value@{q:g}"] = value
        for q, d in zip(group["quantile"], group[column], strict=True):
            record[f"delta@{q:g}"] = d
        if set(points.index) != expected:
            record.update(swing=np.nan, impact=np.nan, flags="incomplete")
        elif points.isna().any():
            record.update(swing=np.nan, impact=np.nan, flags="undefined")
        else:
            record["swing"] = points.iloc[-1] - points.iloc[0]
            record["impact"] = max(0.0, points.max()) - min(0.0, points.min())
            record["flags"] = ",".join(_flags(points, flat))
        rows.append(record)
    table = pd.DataFrame(rows).set_index("row")
    # With no ordinary impact to compare against (--only naming factors
    # alone, or rows that only matter jointly), share over the factors'.
    total = table.loc[table["target"] != FACTOR_TARGET, "impact"].sum()  # NaN-skipping
    if not total:
        total = table["impact"].sum()
    table["share"] = table["impact"] / total if total else table["impact"] * 0.0
    order = table["impact"].sort_values(ascending=False, na_position="last").index
    tail = ("swing", "impact", "share", "flags")
    columns = [c for c in table.columns if c not in tail]
    return table.loc[order, [*columns, *tail]]


def rank_by_year(
    deltas: pd.DataFrame, *, measure: str = "net_worth", relative: bool = False,
    value: str = "share",
) -> pd.DataFrame:
    """Parameter x reporting-year grid (ADR-007 §6.1): ``share``, signed
    ``swing``, or ``rank`` (1 = largest impact that year; NA for a row with
    no effect that year). Rows are sorted
    by peak share, so the overview heatmap reads top-down by importance.
    ``attrs["factors"]`` names the latent factors among the rows."""
    if value not in ("share", "swing", "rank"):
        raise ValueError(f"value must be share, swing or rank (got {value!r})")
    years = sorted(deltas.loc[deltas["measure"] == measure, "year"].unique())
    tables = {y: oat_table(deltas, y, measure=measure, relative=relative) for y in years}
    share = pd.DataFrame({y: t["share"] for y, t in tables.items()})
    order = share.max(axis=1).sort_values(ascending=False).index
    if value == "share":
        grid = share
    elif value == "swing":
        grid = pd.DataFrame({y: t["swing"] for y, t in tables.items()})
    else:
        # A row with no effect that year is unranked (NA), not tied for first.
        grid = share.where(share > 0).rank(ascending=False, method="min").astype("Int64")
    grid = grid.loc[order]
    grid.columns.name = "year"
    grid.attrs["factors"] = factor_rows(deltas)  # for labeling (row_label)
    return grid


def pin_candidates(
    deltas: pd.DataFrame, negligible: float = DEFAULT_NEGLIGIBLE, *, measure: str = "net_worth",
) -> list[str]:
    """Rows whose share stays below ``negligible`` in *every* reporting year:
    a parameter that only matters in the short term still matters. Never a
    latent factor: it has no literal to be pinned to."""
    share = rank_by_year(deltas, measure=measure, value="share")
    factors = factor_rows(deltas)
    return [str(row) for row, shares in share.iterrows()
            if row not in factors and (shares < negligible).all()]


def factor_rows(deltas: pd.DataFrame) -> set[str]:
    """The latent factors an OAT batch perturbed (ADR-007 §2)."""
    return set(deltas.loc[deltas["target"] == FACTOR_TARGET, "row"])


def row_label(name: str, factors: set[str]) -> str:
    """A row's name for a table or chart axis, marking a latent factor so it
    isn't read as one more row: its effect is its whole family's."""
    return f"{name} (factor)" if name in factors else name


def default_report_years(years: list[int], months_per_year: dict[int, int] | None = None) -> list[int]:
    """The years the CLI prints when ``--years`` isn't given: the first full
    year, every fifth year after it, and the final year."""
    years = sorted(years)
    if not years:
        return []
    first = years[0]
    if months_per_year is not None:
        full = [y for y in years if months_per_year.get(y, 0) == 12]
        if full:
            first = full[0]
    picked = [y for y in years if y >= first and (y - first) % 5 == 0]
    if years[-1] not in picked:
        picked.append(years[-1])
    return picked


def months_per_year(batch_dir: Path) -> dict[int, int]:
    """Simulated months in each calendar year, from any one succeeded member."""
    connections = load_ensemble(batch_dir)
    try:
        index = next(iter(connections.values())).net_worth_frame().index
    finally:
        for rc in connections.values():
            rc.close()
    months = pd.Series(1, index=index)
    return months.groupby(index_years(months)).sum().to_dict()



# --- §6.1 attribution --------------------------------------------------------

ATTRIBUTION_AXES: tuple[str, ...] = ("category", "flow_account", "holding_account")

OPENING_BALANCES = "opening_balances"
"""CPI-constant attribution's extra row: the same opening balance deflated by
the member's CPI path versus the base's. Zero unless the perturbed row moves
inflation itself, but needed then for the rows to sum exactly."""

UNATTRIBUTED = "unattributed"
TOTAL = "total"

UNATTRIBUTED_TOLERANCE = 0.05
"""Dollars of ``unattributed`` treated as zero: CPI-constant net worth is
stored rounded to the cent per member, so a cent or two of residue is
rounding, not a ledger gap (which would be dollars, not cents)."""


def _year_end(series: pd.Series) -> pd.Series:
    return series.groupby(index_years(series)).last()


def _cumulative_flows(rc: RunConnection, by: str) -> pd.DataFrame:
    """Year-indexed cumulative net-worth contribution (dollars) of each
    income/expense account (``flow_account``) or subkind (``category``):
    every transfer out of one adds to net worth, every transfer into one
    subtracts. ``ytd`` and ``ytd_rule`` events record pre-simulation history
    without moving balances (``engine._post_ytd_transfer``), so they're
    excluded."""
    key = "name" if by == "flow_account" else "subkind"
    con = connect_sim_file(rc.run_path)
    try:
        events = pd.read_sql_query(
            f"""
            SELECT CAST(substr(te.date, 1, 4) AS INTEGER) AS year, fa.{key} AS key,
                   SUM(te.amount) AS amount
            FROM transfer_event te JOIN account fa ON fa.id = te.from_account
            WHERE te.reason_kind NOT IN ('ytd', 'ytd_rule') AND fa.kind IN ('income', 'expense')
            GROUP BY year, key
            UNION ALL
            SELECT CAST(substr(te.date, 1, 4) AS INTEGER) AS year, ta.{key} AS key,
                   -SUM(te.amount) AS amount
            FROM transfer_event te JOIN account ta ON ta.id = te.to_account
            WHERE te.reason_kind NOT IN ('ytd', 'ytd_rule') AND ta.kind IN ('income', 'expense')
            GROUP BY year, key
            """,
            con,
        )
    finally:
        con.close()
    years = _year_end(rc.net_worth_frame()["nominal"]).index
    by_year = events.pivot_table(index="year", columns="key", values="amount", aggfunc="sum")
    return (by_year.reindex(years).fillna(0).cumsum() / 100).rename_axis(columns=None)


def _holdings(rc: RunConnection) -> pd.DataFrame:
    """Year-end balance (dollars) per asset/liability account, liabilities
    negative -- the same signs the ``net_worth`` view sums."""
    con = connect_sim_file(rc.run_path)
    try:
        balances = pd.read_sql_query(
            "SELECT month, name, CASE WHEN kind = 'liability' THEN -balance ELSE balance END "
            "AS balance FROM balance_by_account",
            con,
        )
    finally:
        con.close()
    balances["month"] = pd.to_datetime(balances["month"])
    wide = balances.pivot_table(index="month", columns="name", values="balance", aggfunc="sum")
    return (year_end_values(wide.fillna(0)) / 100).rename_axis(columns=None)


def _member_parts(rc: RunConnection, by: str, dollars: DollarMode) -> tuple[pd.DataFrame, pd.Series]:
    """(parts, net worth), year-indexed, in ``dollars``: parts sum to net
    worth exactly for ``holding_account``, and to net worth minus the
    opening balance (carried as its own column) for the flow axes."""
    nominal = _year_end(rc.net_worth_frame()["nominal"].astype(float))
    if by == "holding_account":
        parts = _holdings(rc)
    else:
        parts = _cumulative_flows(rc, by)
        # Net worth = opening + cumulative flows (the double-entry identity),
        # so the opening balance is whatever the flows don't account for.
        opening = (nominal - parts.sum(axis=1)).iloc[0]
        parts[OPENING_BALANCES] = opening
    if dollars == DollarMode.CPI_CONSTANT:
        factor = 1_000_000 / _year_end(rc.cpi_index().astype(float))
        parts = parts.mul(factor, axis=0)
        net_worth = _year_end(rc.net_worth_frame()["cpi_constant"].astype(float))
    else:
        net_worth = nominal
    return parts, net_worth


def attribution(
    base: RunConnection, member: RunConnection, *, by: str = "category",
    dollars: DollarMode = DollarMode.CPI_CONSTANT,
) -> pd.DataFrame:
    """ADR-007 §6.1: a perturbed member's net-worth delta against the base,
    decomposed exactly along ``by`` (``category``, ``flow_account`` or
    ``holding_account``). Year-indexed; one column per key that moved,
    ordered by |delta| in the final year, then ``unattributed`` (delta minus
    the sum of the rows -- zero by the double-entry identity, so non-zero
    means a ledger gap worth knowing about) and ``total`` (the delta)."""
    if by not in ATTRIBUTION_AXES:
        raise ValueError(f"by must be one of {ATTRIBUTION_AXES} (got {by!r})")
    base_parts, base_nw = _member_parts(base, by, dollars)
    member_parts, member_nw = _member_parts(member, by, dollars)
    keys = base_parts.columns.union(member_parts.columns)
    delta = (
        member_parts.reindex(columns=keys, fill_value=0.0)
        - base_parts.reindex(columns=keys, fill_value=0.0)
    )
    total = member_nw - base_nw
    delta = delta.loc[:, (delta.abs() >= 0.005).any()]
    if not delta.empty:
        delta = delta[delta.iloc[-1].abs().sort_values(ascending=False).index]
    residual = total - delta.sum(axis=1)
    delta[UNATTRIBUTED] = residual.where(residual.abs() >= UNATTRIBUTED_TOLERANCE, 0.0).round(2)
    delta[TOTAL] = total
    delta.index.name = "year"
    return delta


def base_net_worth(batch_dir: Path, dollars: DollarMode = DollarMode.CPI_CONSTANT) -> pd.Series:
    """Year-end net worth of an OAT batch's base member, in ``dollars`` --
    the denominator for relative attribution."""
    connections = load_ensemble(batch_dir)
    try:
        base = next((m for m in oat_members(connections) if m.row is None), None)
        if base is None:
            raise EnsembleError(f"{batch_dir}: no succeeded base member")
        return member_measures(connections[base.stem], dollars)["net_worth"]
    finally:
        for rc in connections.values():
            rc.close()


def oat_attribution(
    batch_dir: Path, row: str, *, by: str = "category",
    dollars: DollarMode = DollarMode.CPI_CONSTANT,
) -> dict[float, pd.DataFrame]:
    """``attribution`` for every quantile ``row`` was perturbed to in an OAT
    batch, keyed by quantile."""
    connections = load_ensemble(batch_dir)
    try:
        members = oat_members(connections)
        base = next((m for m in members if m.row is None), None)
        if base is None:
            raise EnsembleError(f"{batch_dir}: no succeeded base member")
        perturbed = sorted((m for m in members if m.row == row), key=_perturbed_quantile)
        if not perturbed:
            known = sorted({m.row for m in members if m.row})
            raise EnsembleError(f"{batch_dir}: no succeeded member perturbs {row!r} (have {known})")
        return {
            _perturbed_quantile(m): attribution(connections[base.stem], connections[m.stem], by=by, dollars=dollars)
            for m in perturbed
        }
    finally:
        for rc in connections.values():
            rc.close()


# --- §6.2 correlation over a Monte Carlo batch ------------------------------

R2_TRUST = 0.7
"""Below this per-year R^2, a substantial part of the outcome comes from
interactions or transitions no per-parameter number captures."""

_Z95 = 1.959963984540054
_SPEARMAN_VARIANCE = 1.06
"""Fisher-z variance correction for Spearman's rho: ``1.06 / (n - 3)``."""


def _member_draws(
    connections: dict[str, RunConnection],
) -> tuple[pd.DataFrame, pd.DataFrame, set[str]]:
    """One pass over the drawn members' ``ensemble_draw`` rows, excluding
    member 0 -- the deterministic base, not a draw (ADR-006 §9):

    - member x varying-row matrix of numeric draws (dates as day ordinals);
      latent factors (ADR-008) aren't inputs of their own, since their effect
      reaches the outcome only through their dependents;
    - member x tree-node matrix of recorded normal scores ``z`` (ADR-008 §6),
      factors included; a row outside every tree has none;
    - the latent factors' names.
    """
    records: dict[str, dict[str, float]] = {}
    scores: dict[str, dict[str, float]] = {}
    factors: set[str] = set()
    for stem, rc in connections.items():
        con = connect_sim_file(rc.run_path)
        try:
            index = con.execute("SELECT member_index FROM ensemble_manifest").fetchone()
            columns = {c[1] for c in con.execute("PRAGMA table_info(ensemble_draw)")}
            z = "z" if "z" in columns else "NULL"  # files written before ADR-008
            draws = con.execute(
                f"SELECT calendar_event_name, vary_target, drawn_value, {z} FROM ensemble_draw"
            ).fetchall()
        finally:
            con.close()
        if index is None or index[0] == 0:
            continue
        records[stem] = {
            name: float(_day_ordinal(raw)) if target == "date" else float(raw)
            for name, target, raw, _z in draws if target != FACTOR_TARGET
        }
        scores[stem] = {name: float(score) for name, _t, _r, score in draws if score is not None}
        factors |= {name for name, target, _r, _z in draws if target == FACTOR_TARGET}
    return (
        pd.DataFrame.from_dict(records, orient="index").sort_index(axis=1),
        pd.DataFrame.from_dict(scores, orient="index").sort_index(axis=1),
        factors,
    )


def draws_frame(connections: dict[str, RunConnection]) -> pd.DataFrame:
    """Member x varying-row matrix of numeric draws (dates as day ordinals)
    from each member's own ``ensemble_draw``, excluding member 0 and latent
    factors (see ``_member_draws``)."""
    return _member_draws(connections)[0]


def outcomes_frame(
    connections: dict[str, RunConnection], members: list[str], *,
    measure: str = "net_worth", dollars: DollarMode = DollarMode.CPI_CONSTANT,
) -> pd.DataFrame:
    """Member x reporting-year matrix of ``measure``."""
    return pd.DataFrame({
        stem: member_measures(connections[stem], dollars)[measure] for stem in members
    }).T


def _fisher_interval(r: float, n: int, variance: float) -> tuple[float, float]:
    """95% interval on a correlation via the Fisher z-transform, with
    ``variance / (n - 3)`` on the transformed scale."""
    if n <= 3 or pd.isna(r):
        return (float("nan"), float("nan"))
    z = np.arctanh(np.clip(r, -0.999999, 0.999999))
    half = _Z95 * np.sqrt(variance / (n - 3))
    # The clip keeps arctanh finite at |r| = 1 (every member ranked the
    # same), but then the transformed interval stops just short of r
    # itself; widen it to include r so it's always a valid interval.
    return (min(float(np.tanh(z - half)), r), max(float(np.tanh(z + half)), r))


def rho_interval(rho: float, n: int) -> tuple[float, float]:
    """95% confidence interval on Spearman's rho via the Fisher z-transform."""
    return _fisher_interval(rho, n, _SPEARMAN_VARIANCE)


def required_members(negligible: float) -> int:
    """Members needed for rho's 95% half-width near zero to fall below
    ``sqrt(negligible)`` -- the smallest batch that can call a row with no
    effect negligible at that threshold."""
    return int(np.ceil(3 + _SPEARMAN_VARIANCE * _Z95**2 / negligible))


def _ranks(frame: pd.DataFrame | pd.Series) -> pd.DataFrame | pd.Series:
    return frame.rank(method="average")


def _srrc_intervals(
    x: np.ndarray, varying: np.ndarray, coef: np.ndarray, residuals: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per row: the 95% interval on its SRRC from the rank regression's
    standard errors (t with ``n - k - 1`` degrees of freedom, ``k`` counting
    only the rows that vary), and its PRCC,
    which shares the coefficient's t statistic: ``t / sqrt(t^2 + dof)``.

    All NaN without residual degrees of freedom, or when the varying rows'
    ranks are exactly collinear (no coefficient is identifiable). A row that
    never varies gets NaN too: it has no coefficient to bound.
    """
    n, k = x.shape
    # A row that never varies is an all-zero column: no fitted parameter,
    # so it costs no degree of freedom.
    dof = n - int(varying.sum()) - 1
    low, high, prcc = (np.full(k, np.nan) for _ in range(3))
    if dof <= 0 or not varying.any():
        return low, high, prcc
    xv = x[:, varying]
    gram = xv.T @ xv
    if np.linalg.matrix_rank(gram) < xv.shape[1]:
        return low, high, prcc
    sigma2 = float(residuals @ residuals) / dof
    se = np.sqrt(sigma2 * np.diag(np.linalg.inv(gram)))
    from scipy.stats import t as student_t  # slow to import; most commands never get here
    half = float(student_t.ppf(0.975, dof)) * se
    b = coef[varying]
    with np.errstate(divide="ignore", invalid="ignore"):
        t = b / se
        # A perfect fit has se = 0: the partial correlation is +-1 (or 0
        # for a zero coefficient), not the NaN that inf / inf gives.
        partial = np.where(se > 0, t / np.sqrt(t**2 + dof), np.sign(b))
    low[varying], high[varying], prcc[varying] = b - half, b + half, partial
    return low, high, prcc


def correlation_table(
    draws: pd.DataFrame, outcomes: pd.DataFrame, factors: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """ADR-007 §6.2: per (row, year): Spearman ``rho`` with its 95% interval
    (``rho_low``/``rho_high``), the standardized rank-regression coefficient
    ``srrc`` with its 95% interval (``srrc_low``/``srrc_high``), the partial
    rank correlation ``prcc``, and that year's adjusted regression ``r2``
    (repeated per row; NaN, so untrusted, when there are too few members for
    the number of rows). A year whose outcome doesn't vary across members has
    NaN throughout -- there's nothing to explain.

    ``factors`` (member x latent factor normal scores) adds a row per factor
    with ``factor`` True: its ``rho`` with the outcome, the effect of its
    whole family through it. Factors aren't regressors -- their effect is
    only through their dependents, which are -- so their SRRC and PRCC are
    NaN, and ``classify`` gives them no status.
    """
    members = draws.index.intersection(outcomes.index)
    draws, outcomes = draws.loc[members], outcomes.loc[members]
    factors = pd.DataFrame(index=members) if factors is None else factors.reindex(members)
    n = len(members)
    x = _ranks(draws)
    spread = x.std(ddof=0)
    x = (x - x.mean()) / spread
    x = x.fillna(0.0)  # a row that never varies (zero spread) explains nothing
    varying = (spread > 0).to_numpy()
    xv = x.to_numpy()[:, varying]
    # With exactly collinear ranks (a rho of +-1 makes them so) lstsq still
    # returns a minimum-norm answer, but no coefficient is identifiable.
    identifiable = bool(varying.any()) and int(np.linalg.matrix_rank(xv.T @ xv)) == xv.shape[1]
    nan = float("nan")
    records = []
    for year in outcomes.columns:
        y = _ranks(outcomes[year])
        y_spread = y.std(ddof=0)
        if n < 4 or not y_spread:
            for row in draws.columns:
                records.append({"row": row, "year": year, "rho": nan, "rho_low": nan,
                                "rho_high": nan, "srrc": nan, "srrc_low": nan, "srrc_high": nan,
                                "prcc": nan, "r2": nan, "n": n, "factor": False})
            for row in factors.columns:
                records.append({"row": row, "year": year, "rho": nan, "rho_low": nan,
                                "rho_high": nan, "srrc": nan, "srrc_low": nan, "srrc_high": nan,
                                "prcc": nan, "r2": nan, "n": n, "factor": True})
            continue
        y = (y - y.mean()) / y_spread
        coef, *_ = np.linalg.lstsq(x.to_numpy(), y.to_numpy(), rcond=None)
        residuals = y.to_numpy() - x.to_numpy() @ coef
        r2 = 1 - float(np.sum(residuals**2)) / float(np.sum(y.to_numpy() ** 2))
        # Adjusted for the number of rows: in-sample R^2 climbs toward 1 as
        # rows approach members whatever the outcome, which would let noise
        # pass the trust gate. With no residual degrees of freedom there's
        # nothing to adjust from, so it's undefined (and untrusted).
        # Only rows that vary are fitted predictors; a constant one is an
        # all-zero column and mustn't count against the adjustment.
        k = int(varying.sum())
        r2 = 1 - (1 - r2) * (n - 1) / (n - k - 1) if n - k - 1 > 0 else np.nan
        low, high, prcc = _srrc_intervals(x.to_numpy(), varying, coef, residuals)
        for i, row in enumerate(draws.columns):
            # A row that never varies has no rank correlation (and computing
            # one only warns about dividing by its zero spread).
            rho = float(draws[row].rank().corr(outcomes[year].rank())) if varying[i] else nan
            rho_low, rho_high = rho_interval(rho, n)
            records.append({"row": row, "year": year, "rho": rho, "rho_low": rho_low,
                            "rho_high": rho_high,
                            # A row that never varies has no coefficient to
                            # report, and none is identifiable without full rank.
                            "srrc": float(coef[i]) if varying[i] and identifiable else nan,
                            "srrc_low": float(low[i]), "srrc_high": float(high[i]),
                            "prcc": float(prcc[i]), "r2": r2, "n": n, "factor": False})
        for row in factors.columns:
            rho = float(factors[row].rank().corr(outcomes[year].rank()))
            rho_low, rho_high = rho_interval(rho, n)
            records.append({"row": row, "year": year, "rho": rho, "rho_low": rho_low,
                            "rho_high": rho_high, "srrc": nan, "srrc_low": nan,
                            "srrc_high": nan, "prcc": nan, "r2": r2, "n": n, "factor": True})
    return pd.DataFrame(records)


RANKING_MEASURES: tuple[str, ...] = ("rho", "srrc")
"""What ``classify`` ranks and pins by: marginal ``rho`` for independent
inputs, ``srrc`` when rows are correlated (ADR-007 §6.2)."""


def classify(
    table: pd.DataFrame, negligible: float = DEFAULT_NEGLIGIBLE, by: str = "rho",
) -> pd.Series:
    """Per row (latent factors excluded), over every year whose outcome
    varies at all, with ``m`` the ``by`` measure (``rho`` or ``srrc``):

    - ``pin_candidate``: the upper bound of m^2's interval is below
      ``negligible`` *and* that year's R^2 meets ``R2_TRUST``, in every year;
    - ``inconclusive``: m^2 itself is below ``negligible`` every year, but
      either an interval is too wide to say so, or R^2 is too low to trust a
      per-row number at all -- with a pure interaction (outcome = x1 * x2)
      each row's rho is near zero although together they decide the
      outcome, so a small rho is no evidence of irrelevance there;
    - ``keep``: otherwise.

    Every row in ``table`` gets a status; one with no estimable value in any
    year is ``inconclusive``.
    """
    if by not in RANKING_MEASURES:
        raise ValueError(f"by must be one of {RANKING_MEASURES} (got {by!r})")
    if "factor" in table.columns:
        table = table[~table["factor"].astype(bool)]
    result = {}
    for row, group in table.groupby("row", sort=False):
        group = group.dropna(subset=[by])
        if group.empty:
            # No year gave an estimable value (too few members, or a draw
            # that never varies): nothing says it's negligible.
            result[row] = "inconclusive"
            continue
        # An interval straddling zero has its m^2 upper bound at its wider
        # end. A NaN bound (no residual freedom) compares False: not pinned.
        upper = np.maximum(group[f"{by}_low"] ** 2, group[f"{by}_high"] ** 2)
        trusted = group["r2"] >= R2_TRUST
        if ((upper < negligible) & trusted).all():
            result[row] = "pin_candidate"
        elif (group[by] ** 2 < negligible).all():
            result[row] = "inconclusive"
        else:
            result[row] = "keep"
    return pd.Series(result, name="status", dtype=object)


def _links_and_rows(run_path: Path) -> tuple[dict[str, Link], list[str], set[str]]:
    """A member run file's correlation links (ADR-008), its modeled
    (non-factor) varying rows, and the roots of its t-copula trees (#68)."""
    con = connect_sim_file(run_path)
    try:
        columns = {c[1] for c in con.execute("PRAGMA table_info(calendar_event)")}
        links = correlation_links(con)
        t_roots = set() if "vary_tail_dependence" not in columns else {
            name for (name,) in con.execute(
                "SELECT name FROM calendar_event "
                "WHERE vary_tail_dependence IS NOT NULL AND name IS NOT NULL"
            )
        }
        modeled = sorted(name for (name,) in con.execute(
            "SELECT calendar_event_name FROM ensemble_draw WHERE vary_target != ?",
            (FACTOR_TARGET,),
        ))
    finally:
        con.close()
    return links, modeled, t_roots


def _any_dependent(links: dict[str, Link], modeled: list[str], t_roots: set[str]) -> bool:
    """Whether any two modeled rows are dependent: a nonzero implied
    correlation, or one t-copula tree (#68), whose shared scale ties even
    rows at zero implied rho together in their extremes."""
    def t_tree(name: str) -> str | None:
        root = tree_root(links, name)
        return root if root in t_roots else None

    for i, a in enumerate(modeled):
        for b in modeled[i + 1:]:
            if implied_correlation(links, a, b) != 0:
                return True
            if (root := t_tree(a)) is not None and root == t_tree(b):
                return True
    return False


def has_correlated_rows(batch_dir: Path) -> bool:
    """Whether any two of a batch's modeled (non-factor) rows are dependent
    (ADR-008): some pair has a nonzero implied correlation through the
    correlation links stored in the batch. A row merely being in a tree isn't
    enough -- a lone row under a factor, or a zero-rho link in a Gaussian
    tree, leaves the report's inputs independent. Two rows in one t-copula
    tree (#68) are dependent whatever their rho. The Monte Carlo report ranks and pins by
    marginal rho for independent inputs, and by SRRC when they aren't."""
    connections = load_ensemble(batch_dir)
    try:
        links, modeled, t_roots = _links_and_rows(next(iter(connections.values())).run_path)
    finally:
        for rc in connections.values():
            rc.close()
    return _any_dependent(links, modeled, t_roots)


def _kendall_tau(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """Kendall's tau between two untied samples, and its standard error from
    the samples themselves: tau is the mean of each member's concordance
    ``c_i`` with the others, and the U-statistic variance is
    ``4 * var(c) / n``. Unlike a fixed formula (e.g. ``0.437 / (n - 4)``,
    which assumes a Gaussian copula), this keeps its coverage under the t
    copula, whose turbulent members make tau noisier (#68).

    Each ``c_i`` counts the members below-left and above-right of member
    ``i`` with a Fenwick tree over y-ranks, sweeping in x order: O(n log n),
    so a large batch's report stays fast. Scores don't tie; a tie would be
    broken by position rather than counted as neither."""
    n = len(x)
    rank_x = np.argsort(np.argsort(x, kind="stable"), kind="stable")
    rank_y = np.argsort(np.argsort(y, kind="stable"), kind="stable")
    tree = [0] * (n + 1)
    below_left = np.empty(n)
    for i in np.argsort(rank_x).tolist():  # members in increasing x
        r = int(rank_y[i])
        k, count = r, 0
        while k > 0:  # members already swept (lower x) with a lower y
            count += tree[k]
            k -= k & -k
        below_left[i] = count
        k = r + 1
        while k <= n:
            tree[k] += 1
            k += k & -k
    above_right = (n - 1) - rank_x - rank_y + below_left
    concordance = (2 * (below_left + above_right) - (n - 1)) / (n - 1)
    tau = float(concordance.mean())
    spread = float(((concordance - tau) ** 2).sum() / (n - 1))
    return tau, float(np.sqrt(4 * spread / n))


def _kendall_rho_interval(tau: float, se: float, n: int) -> tuple[float, float]:
    """95% interval on the copula rho a Kendall ``tau`` estimates: a Fisher-z
    interval on tau, with ``se`` carried through the transform, mapped
    through ``rho_from_kendall_tau``, which is increasing, so the bounds map
    too. Simulated coverage is about 95% for the Gaussian copula and for t
    down to nu = 3, from 40 members up."""
    if n <= 3 or pd.isna(tau):
        return (float("nan"), float("nan"))
    clipped = float(np.clip(tau, -0.999999, 0.999999))
    z = np.arctanh(clipped)
    half = _Z95 * se / (1 - clipped * clipped)
    low, high = min(float(np.tanh(z - half)), tau), max(float(np.tanh(z + half)), tau)
    return (rho_from_kendall_tau(low), rho_from_kendall_tau(high))


def score_correlations(
    scores: pd.DataFrame, links: dict[str, Link], nodes: list[str],
) -> pd.DataFrame:
    """ADR-007 §6.2, ADR-008 §6: per pair of ``nodes`` with a nonzero implied
    correlation, the copula rho the batch achieved between their recorded
    scores with its 95% interval, against the ``expected`` one: the pair's
    own rho when one is the other's parent (``kind`` "link"), else the
    product along the path joining them ("implied"). ``outside`` marks an
    expected rho outside the achieved interval -- a sign the batch is too
    small to reproduce the structure, or something is wrong with it.

    The achieved rho is estimated from the scores' Kendall tau as
    ``sin(pi * tau / 2)``, which holds exactly for the Gaussian and the
    Student-t copula alike (#68), where a Pearson correlation of t scores
    would be dominated by a few turbulent members (and is undefined at
    ``nu <= 2``). Scores never tie, unlike values (ADR-008 §1)."""
    records = []
    for i, a in enumerate(nodes):
        for b in nodes[i + 1:]:
            expected = implied_correlation(links, a, b)
            if expected == 0 or a not in scores.columns or b not in scores.columns:
                continue
            pair = scores[[a, b]].dropna()
            n = len(pair)
            tau, se = (_kendall_tau(pair[a].to_numpy(), pair[b].to_numpy()) if n > 1
                       else (float("nan"), float("nan")))
            achieved = rho_from_kendall_tau(tau) if not pd.isna(tau) else float("nan")
            low, high = _kendall_rho_interval(tau, se, n)
            direct = (a in links and links[a].parent == b) or (b in links and links[b].parent == a)
            records.append({
                "a": a, "b": b, "kind": "link" if direct else "implied", "expected": expected,
                "achieved": achieved, "low": low, "high": high, "n": n,
                "outside": bool(not pd.isna(low) and not low <= expected <= high),
            })
    return pd.DataFrame(records, columns=[
        "a", "b", "kind", "expected", "achieved", "low", "high", "n", "outside",
    ])


@dataclass(frozen=True)
class McReport:
    """Everything the Monte Carlo report (ADR-007 §6.2) shows for a batch."""

    table: pd.DataFrame
    """``correlation_table``, with a row per latent factor that reaches a
    modeled row."""
    scores: pd.DataFrame
    """``score_correlations`` over the modeled rows and the factors that
    reach them; empty when no two of them are correlated."""
    correlated: bool
    """``has_correlated_rows``: whether ranking and pinning use SRRC."""

    @property
    def by(self) -> str:
        """What rows are ranked and pinned by: marginal rho with independent
        inputs, where SRRC ~ rho anyway; SRRC with correlated ones, where a
        row's marginal rho includes its correlates' effects."""
        return "srrc" if self.correlated else "rho"


def mc_report(
    batch_dir: Path, *, measure: str = "net_worth", dollars: DollarMode = DollarMode.CPI_CONSTANT,
) -> McReport:
    """The Monte Carlo report over a batch's succeeded members."""
    connections = load_ensemble(batch_dir)
    try:
        draws, scores, factors = _member_draws(connections)
        if draws.empty:
            raise EnsembleError(f"{batch_dir}: no drawn members (member 0 is never a draw)")
        outcomes = outcomes_frame(connections, list(draws.index), measure=measure, dollars=dollars)
        links, modeled, t_roots = _links_and_rows(next(iter(connections.values())).run_path)
    finally:
        for rc in connections.values():
            rc.close()
    # A factor that reaches no modeled row (unused, or only through a zero
    # rho) moves nothing, so its correlation with the outcome is pure noise.
    reaching = sorted(
        f for f in factors
        if f in scores.columns and any(implied_correlation(links, f, row) for row in modeled)
    )
    table = correlation_table(draws, outcomes, scores[reaching] if reaching else None)
    nodes = [*modeled, *reaching]
    return McReport(table, score_correlations(scores, links, nodes), _any_dependent(links, modeled, t_roots))


def mc_correlation(
    batch_dir: Path, *, measure: str = "net_worth", dollars: DollarMode = DollarMode.CPI_CONSTANT,
) -> pd.DataFrame:
    """``correlation_table`` for a Monte Carlo batch's succeeded members."""
    return mc_report(batch_dir, measure=measure, dollars=dollars).table


# --- the varying rows themselves ----------------------------------------------


def _display_number(kind: str, target: str, value: float) -> str:
    """A number in a target column's own unit, for display: a day ordinal as
    a date, a ``set_rate`` amount (micros) as a percent, cents as dollars."""
    if target == "date":
        return date.fromordinal(round(value)).isoformat()
    if target == LIFESPAN_TARGET:
        return display_value(kind, target, str(value))
    return display_value(kind, target, str(round(value)))


def _spread(kind: str, target: str, sd: float) -> str:
    """A spread (SD or scale) in display units (a duration has no date form)."""
    if kind == "set_rate":
        return f"{sd / 10_000:.2f}%"
    return f"${sd / 100:,.0f}"


def _affects(row: sqlite3.Row) -> str:
    """What a varying calendar row acts on, in the author's own names."""
    if row["kind"] in ("one_off_transfer", "transfer_balance"):
        return f"{row['account_1']} → {row['account_2']}"
    return row["series"] or row["scheduled_name"] or ""


def _row_distribution(row: sqlite3.Row, target: str) -> Distribution | None:
    """The distribution a member file's resolved row was drawn from, or
    ``None`` if its stored parameters don't describe a valid one."""
    from analysis.sim.ensemble import build_distribution

    try:
        return build_distribution({**dict(row), "vary_distribution": row["distribution"]}, target)
    except EnsembleError:
        return None


def _describe(dist: Distribution, kind: str, target: str) -> dict[str, str]:
    """``range``, ``center``, ``shape`` and ``moments`` cells for one row."""
    def show(value: float | None) -> str:
        return "unbounded" if value is None else _display_number(kind, target, value)

    if isinstance(dist, TruncatedNormal):
        low, high = dist.clip_low, dist.clip_high
        # vary_sd is the SD only for the default shape; with skew or tail
        # weight it's a scale factor (the moments cell shows the real SD).
        default_shape = dist.skew == 0 and dist.tailweight == 1
        shape = [f"{'sd' if default_shape else 'scale'} {_spread(kind, target, dist.sd)}"]
        if dist.skew:
            shape.append(f"skew {dist.skew:g}")
        if dist.tailweight != 1:
            shape.append(f"tail weight {dist.tailweight:g}")
    elif isinstance(dist, Discrete):
        low, high = dist.outcomes[0].low, dist.outcomes[-1].high
        shape = [
            f"{o.weight * 100:g}% {show(o.low)}" if o.low == o.high
            else f"{o.weight * 100:g}% {show(o.low)}..{show(o.high)}"
            for o in dist.outcomes
        ]
    else:
        low, high = dist.low, dist.high
        shape = [f"mode {show(dist.mode)}"] if isinstance(dist, Triangular) else []
    # As actually resolved: dates in whole days, amounts rounded to cents/micros.
    m = moments(dist, whole_days=target == "date", rounded=target != "date")
    spread = f"{m.sd:,.0f} days" if target == "date" else _spread(kind, target, m.sd)
    return {
        "range": f"{show(low)} to {show(high)}",
        "center": show(dist.central()),
        "shape": ", ".join(shape),
        "moments": (f"mean {show(m.mean)}, sd {spread}, skew {m.skewness:+.2f}, "
                    f"excess kurtosis {m.excess_kurtosis:+.2f}"),
    }


def _lifespan_record(
    con: sqlite3.Connection, name: str, distribution: str | None, base_ages: dict[str, float],
) -> dict[str, object]:
    """``parameters``' row for a varying lifespan (#221): what it acts on is
    the person, its center the authored age (the base case, not the median)."""
    person = name.removeprefix(LIFESPAN_NODE_PREFIX)
    cur = con.cursor()
    cur.row_factory = sqlite3.Row
    row = cur.execute("SELECT * FROM person_lifespan WHERE person = ?", (person,)).fetchone()
    record: dict[str, object] = {
        "name": name, "kind": "lifespan", "affects": person, "target": LIFESPAN_TARGET,
        "distribution": distribution or "", "range": "", "center": "", "shape": "",
        "moments": "", "ramp_months": "", "process": "", "description": "",
        "correlate_with": (row["vary_correlate_with"] or "") if row else "",
        "rho": "" if row is None or row["vary_correlation"] is None
        else f"{row['vary_correlation']:+g}",
        "nu": "",
    }
    if row is None or not distribution:
        return record
    try:
        dist = lifespan_distribution({**dict(row), "vary_distribution": distribution}, con)
    except EnsembleError:
        return record
    m = moments(dist)
    if isinstance(dist, LifeTableAge):
        # A life table runs to the oldest age it allows; its 1st to 99th
        # percentiles say more about the range a member actually draws (#222).
        span = f"age {dist.quantile(0.01):.1f} to {dist.quantile(0.99):.1f} (1st to 99th percentile)"
        shape = f"set-forward {dist.set_forward:+.1f} years"
    else:
        span = f"age {dist.low:g} to {dist.high:g}"
        shape = f"mode age {dist.mode:g}" if isinstance(dist, Triangular) else ""
    record.update({
        "range": span,
        "center": f"age {base_ages[person]:.1f}" if person in base_ages else "",
        "shape": shape,
        "moments": (f"mean age {m.mean:.1f}, sd {m.sd:.1f} years, skew {m.skewness:+.2f}, "
                    f"excess kurtosis {m.excess_kurtosis:+.2f}"),
    })
    return record


def _base_ages(connections: dict[str, RunConnection]) -> dict[str, float]:
    """Each person's age at death on the batch's base (member 0), if run."""
    for rc in connections.values():
        con = connect_sim_file(rc.run_path)
        try:
            index = con.execute("SELECT member_index FROM ensemble_manifest").fetchone()
            if index is not None and index[0] == 0:
                return dict(con.execute("SELECT person, age_at_death FROM person_lifespan"))
        finally:
            con.close()
    return {}


def _describe_process(row: sqlite3.Row) -> str:
    """A process row's path (ADR-006 §4a), e.g. ``ar1, persistence 0.5,
    shock 2.00%``; blank for a held draw, or a file before schema 33."""
    if "vary_process" not in row.keys() or not row["vary_process"]:
        return ""
    shock = _spread(row["kind"], "amount", row["vary_innovation_sd"])
    start = (row["vary_process_start"] if "vary_process_start" in row.keys() else None)
    return (f"{row['vary_process']}, persistence {row['vary_persistence']:g}, shock {shock}"
            + (f", from the {start} rate" if start else ""))


def parameters(batch_dir: Path) -> pd.DataFrame:
    """The batch's varying rows (ADR-007 §9), one per row: what each acts on
    (``kind``, ``affects``), which column varies (``target``), its
    ``distribution``, ``range`` and ``center`` (the base / member-0 value:
    the median), its ``shape`` (a ``truncated_normal``'s sd and any skew or
    tail weight; a ``triangular``'s mode; a ``discrete`` row's outcomes), the implied ``moments`` (mean, sd,
    skewness, excess kurtosis -- what the shape parameters mean in
    practice, #31), ``ramp_months``, its year-by-year ``process`` (ADR-006
    §4a: persistence and yearly shock), the author's ``description``, and its
    correlation: ``correlate_with``, ``rho``, and on a t-copula tree's root
    its ``nu`` (ADR-008). A sensitivity batch adds the resolved value at
    each perturbed quantile (``value@q``); a Monte Carlo batch adds the range
    actually ``drawn`` across its members.

    Read from the batch's own member files, so it describes what was run --
    not the recipe as it stands now. A batch written before schema 28
    doesn't record the distribution, so its description columns are blank."""
    from analysis.sim.loader import batch_mode

    connections = load_ensemble(batch_dir)
    try:
        con = connect_sim_file(next(iter(connections.values())).run_path)
        con.row_factory = sqlite3.Row
        try:
            has_distribution = any(
                col[1] == "distribution" for col in con.execute("PRAGMA table_info(ensemble_draw)")
            )
            rows = con.execute(
                "SELECT d.calendar_event_name AS name, d.vary_target AS target, "
                f"{'d.distribution' if has_distribution else 'NULL'} AS distribution, c.* "
                "FROM ensemble_draw d JOIN calendar_event c ON c.name = d.calendar_event_name "
                "ORDER BY name"
            ).fetchall()
            lifespans = con.execute(
                f"SELECT calendar_event_name, {'distribution' if has_distribution else 'NULL'} "
                "FROM ensemble_draw WHERE vary_target = ?", (LIFESPAN_TARGET,),
            ).fetchall()
            base_ages = _base_ages(connections) if lifespans else {}
            records = [_lifespan_record(con, name, dist, base_ages) for name, dist in lifespans]
        finally:
            con.close()
        for row in rows:
            kind, target = row["kind"], row["target"]
            record: dict[str, object] = {
                "name": row["name"], "kind": kind, "affects": _affects(row), "target": target,
                "distribution": row["distribution"] or "", "range": "", "center": "",
                "shape": "", "moments": "", "ramp_months": row["vary_ramp_months"] or "",
                "process": _describe_process(row),
                "description": row["description"] or "",
                "correlate_with": row["vary_correlate_with"] or "",
                "rho": "" if row["vary_correlation"] is None else f"{row['vary_correlation']:+g}",
                # On a t-copula tree's root (#68); files before schema 32 have none.
                "nu": f"{row['vary_tail_dependence']:g}"
                if "vary_tail_dependence" in row.keys() and row["vary_tail_dependence"] is not None
                else "",
            }
            dist = _row_distribution(row, target) if row["distribution"] else None
            if dist is not None:
                record.update(_describe(dist, kind, target))
            records.append(record)
        table = pd.DataFrame(records).set_index("name").sort_index() if records else pd.DataFrame()
        if table.empty:
            return table
        if batch_mode(batch_dir) == "sensitivity":
            for m in oat_members(connections):
                if m.row is not None and m.row in table.index:
                    table.loc[m.row, f"value@{m.quantile:g}"] = m.value
        else:
            draws = draws_frame(connections)
            for name in table.index.intersection(draws.columns):
                kind, target = table.loc[name, "kind"], table.loc[name, "target"]
                low, high = draws[name].min(), draws[name].max()
                table.loc[name, "drawn"] = (f"{_display_number(kind, target, low)} to "
                                            f"{_display_number(kind, target, high)} "
                                            f"({draws[name].count()} draws)")
        return table.fillna("")
    finally:
        for rc in connections.values():
            rc.close()
