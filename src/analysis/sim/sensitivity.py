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
from pathlib import Path

import numpy as np
import pandas as pd

from analysis.sim.connection import RunConnection, year_end_values
from analysis.sim.ensemble import EnsembleError, load_ensemble
from analysis.sim.types import DollarMode

__all__ = [
    "OatMember", "MEASURES", "RELATIVE_NEAR_ZERO_DOLLARS", "ASYMMETRY_RATIO", "BEND_FACTOR",
    "DEFAULT_NEGLIGIBLE", "oat_members", "member_measures", "oat_deltas", "oat_table",
    "rank_by_year", "pin_candidates", "default_report_years", "months_per_year",
    "display_value", "ATTRIBUTION_AXES", "OPENING_BALANCES", "UNATTRIBUTED", "TOTAL",
    "attribution", "oat_attribution", "R2_TRUST", "draws_frame", "outcomes_frame",
    "rho_interval", "required_members", "correlation_table", "classify", "mc_correlation",
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


def display_value(kind: str, target: str, raw: str) -> str:
    """An ``ensemble_draw.drawn_value`` in display units: a date as-is, a
    ``set_rate`` amount (micros) as a percent, any other amount (cents) as
    dollars."""
    if target == "date":
        return raw
    if kind == "set_rate":
        return f"{int(raw) / 10_000:.2f}%"
    return f"${int(raw) / 100:,.0f}"


def oat_members(connections: dict[str, RunConnection]) -> list[OatMember]:
    """Read each member's own ``ensemble_draw`` (ADR-007 §5) to find which
    row it perturbed: the one row with a non-NULL ``quantile``, or none for
    the base. Self-documenting -- nothing is inferred from file order."""
    members = []
    for stem, rc in connections.items():
        con = sqlite3.connect(rc.run_path)
        try:
            row = con.execute(
                "SELECT d.calendar_event_name, d.quantile, d.vary_target, d.drawn_value, c.kind "
                "FROM ensemble_draw d "
                "JOIN calendar_event c ON c.name = d.calendar_event_name "
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
    return pd.concat(frames, ignore_index=True)


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
    row, sorted by ``impact`` descending, with ``value@q`` and ``delta@q`` per
    quantile, ``swing`` (signed: delta at the highest quantile minus at the
    lowest, for direction), ``impact`` (the spread of every delta *and the
    base's zero*: ``max(0, deltas) - min(0, deltas)``), ``share`` (impact
    over the sum of impacts) and ``flags``.

    Importance is ``impact``, not ``|swing|``: a non-monotonic row whose low
    and high quantiles both move the outcome the same way has a swing near
    zero but a large impact, and must not read as negligible."""
    column = "relative" if relative else "delta"
    flat = _FLAT_DOLLARS / 1e5 if relative else _FLAT_DOLLARS
    at = deltas[(deltas["year"] == year) & (deltas["measure"] == measure)]
    if at.empty:
        raise EnsembleError(f"no deltas for year {year} ({measure})")
    rows = []
    for name, group in at.groupby("row", sort=False):
        group = group.sort_values("quantile")
        points = pd.Series(group[column].fillna(0.0).to_numpy(), index=group["quantile"].to_numpy())
        record: dict[str, object] = {"row": name, "target": group["target"].iloc[0]}
        for q, value in zip(group["quantile"], group["value"], strict=True):
            record[f"value@{q:g}"] = value
        for q, d in zip(group["quantile"], group[column], strict=True):
            record[f"delta@{q:g}"] = d
        record["swing"] = points.iloc[-1] - points.iloc[0]
        record["impact"] = max(0.0, points.max()) - min(0.0, points.min())
        record["flags"] = ",".join(_flags(points, flat))
        rows.append(record)
    table = pd.DataFrame(rows).set_index("row")
    total = table["impact"].sum()
    table["share"] = table["impact"] / total if total else 0.0
    order = table["impact"].sort_values(ascending=False).index
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
    by peak share, so the overview heatmap reads top-down by importance."""
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
    return grid


def pin_candidates(
    deltas: pd.DataFrame, negligible: float = DEFAULT_NEGLIGIBLE, *, measure: str = "net_worth",
) -> list[str]:
    """Rows whose share stays below ``negligible`` in *every* reporting year:
    a parameter that only matters in the short term still matters."""
    share = rank_by_year(deltas, measure=measure, value="share")
    return [row for row, shares in share.iterrows() if (shares < negligible).all()]


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
    return pd.Series(1, index=index).groupby(index.year).sum().to_dict()



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
    return series.groupby(series.index.year).last()


def _cumulative_flows(rc: RunConnection, by: str) -> pd.DataFrame:
    """Year-indexed cumulative net-worth contribution (dollars) of each
    income/expense account (``flow_account``) or subkind (``category``):
    every transfer out of one adds to net worth, every transfer into one
    subtracts. ``ytd`` events record pre-simulation history without moving
    balances, so they're excluded."""
    key = "name" if by == "flow_account" else "subkind"
    con = sqlite3.connect(rc.run_path)
    try:
        events = pd.read_sql_query(
            f"""
            SELECT CAST(substr(te.date, 1, 4) AS INTEGER) AS year, fa.{key} AS key,
                   SUM(te.amount) AS amount
            FROM transfer_event te JOIN account fa ON fa.id = te.from_account
            WHERE te.reason_kind != 'ytd' AND fa.kind IN ('income', 'expense')
            GROUP BY year, key
            UNION ALL
            SELECT CAST(substr(te.date, 1, 4) AS INTEGER) AS year, ta.{key} AS key,
                   -SUM(te.amount) AS amount
            FROM transfer_event te JOIN account ta ON ta.id = te.to_account
            WHERE te.reason_kind != 'ytd' AND ta.kind IN ('income', 'expense')
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
    con = sqlite3.connect(rc.run_path)
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
        perturbed = sorted((m for m in members if m.row == row), key=lambda m: m.quantile)
        if not perturbed:
            known = sorted({m.row for m in members if m.row})
            raise EnsembleError(f"{batch_dir}: no succeeded member perturbs {row!r} (have {known})")
        return {
            m.quantile: attribution(connections[base.stem], connections[m.stem], by=by, dollars=dollars)
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


def draws_frame(connections: dict[str, RunConnection]) -> pd.DataFrame:
    """Member x varying-row matrix of numeric draws (dates as day ordinals)
    from each member's own ``ensemble_draw``, excluding member 0 -- the
    deterministic base, not a draw (ADR-006 §9)."""
    records: dict[str, dict[str, float]] = {}
    for stem, rc in connections.items():
        con = sqlite3.connect(rc.run_path)
        try:
            index = con.execute("SELECT member_index FROM ensemble_manifest").fetchone()
            draws = con.execute(
                "SELECT calendar_event_name, vary_target, drawn_value FROM ensemble_draw"
            ).fetchall()
        finally:
            con.close()
        if index is None or index[0] == 0:
            continue
        records[stem] = {
            name: float(pd.Timestamp(raw).toordinal()) if target == "date" else float(raw)
            for name, target, raw in draws
        }
    return pd.DataFrame.from_dict(records, orient="index").sort_index(axis=1)


def outcomes_frame(
    connections: dict[str, RunConnection], members: list[str], *,
    measure: str = "net_worth", dollars: DollarMode = DollarMode.CPI_CONSTANT,
) -> pd.DataFrame:
    """Member x reporting-year matrix of ``measure``."""
    return pd.DataFrame({
        stem: member_measures(connections[stem], dollars)[measure] for stem in members
    }).T


def rho_interval(rho: float, n: int) -> tuple[float, float]:
    """95% confidence interval on Spearman's rho via the Fisher z-transform."""
    if n <= 3 or pd.isna(rho):
        return (float("nan"), float("nan"))
    z = np.arctanh(np.clip(rho, -0.999999, 0.999999))
    half = _Z95 * np.sqrt(_SPEARMAN_VARIANCE / (n - 3))
    # The clip keeps arctanh finite at |rho| = 1 (every member ranked the
    # same), but then the transformed interval stops just short of rho
    # itself; widen it to include rho so it's always a valid interval.
    return (min(float(np.tanh(z - half)), rho), max(float(np.tanh(z + half)), rho))


def required_members(negligible: float) -> int:
    """Members needed for rho's 95% half-width near zero to fall below
    ``sqrt(negligible)`` -- the smallest batch that can call a row with no
    effect negligible at that threshold."""
    return int(np.ceil(3 + _SPEARMAN_VARIANCE * _Z95**2 / negligible))


def _ranks(frame: pd.DataFrame | pd.Series) -> pd.DataFrame | pd.Series:
    return frame.rank(method="average")


def correlation_table(draws: pd.DataFrame, outcomes: pd.DataFrame) -> pd.DataFrame:
    """ADR-007 §6.2: per (row, year): Spearman ``rho`` with its 95% interval
    (``rho_low``/``rho_high``), the standardized rank-regression coefficient
    ``srrc``, and that year's regression ``r2`` (repeated per row). A year
    whose outcome doesn't vary across members has NaN throughout -- there's
    nothing to explain."""
    members = draws.index.intersection(outcomes.index)
    draws, outcomes = draws.loc[members], outcomes.loc[members]
    n = len(members)
    x = _ranks(draws)
    x = (x - x.mean()) / x.std(ddof=0)
    x = x.fillna(0.0)  # a row that never varies (zero spread) explains nothing
    records = []
    for year in outcomes.columns:
        y = _ranks(outcomes[year])
        spread = y.std(ddof=0)
        if n < 4 or not spread:
            for row in draws.columns:
                records.append({"row": row, "year": year, "rho": np.nan, "rho_low": np.nan,
                                "rho_high": np.nan, "srrc": np.nan, "r2": np.nan, "n": n})
            continue
        y = (y - y.mean()) / spread
        coef, *_ = np.linalg.lstsq(x.to_numpy(), y.to_numpy(), rcond=None)
        fitted = x.to_numpy() @ coef
        r2 = 1 - float(np.sum((y.to_numpy() - fitted) ** 2)) / float(np.sum(y.to_numpy() ** 2))
        for i, row in enumerate(draws.columns):
            rho = float(draws[row].rank().corr(outcomes[year].rank()))
            low, high = rho_interval(rho, n)
            records.append({"row": row, "year": year, "rho": rho, "rho_low": low,
                            "rho_high": high, "srrc": float(coef[i]), "r2": r2, "n": n})
    return pd.DataFrame(records)


def classify(table: pd.DataFrame, negligible: float = DEFAULT_NEGLIGIBLE) -> pd.Series:
    """Per row, over every year whose outcome varies at all:

    - ``pin_candidate``: the upper bound of rho^2's interval is below
      ``negligible`` *and* that year's R^2 meets ``R2_TRUST``, in every year;
    - ``inconclusive``: rho^2 itself is below ``negligible`` every year, but
      either an interval is too wide to say so, or R^2 is too low to trust a
      marginal correlation at all -- with a pure interaction (outcome =
      x1 * x2) each row's rho is near zero although together they decide
      the outcome, so a small rho is no evidence of irrelevance there;
    - ``keep``: otherwise.
    """
    informative = table.dropna(subset=["rho"])
    result = {}
    for row, group in informative.groupby("row", sort=False):
        upper = np.maximum(group["rho_low"] ** 2, group["rho_high"] ** 2)
        # An interval straddling zero has rho^2 upper bound at its wider end.
        trusted = group["r2"] >= R2_TRUST
        if ((upper < negligible) & trusted).all():
            result[row] = "pin_candidate"
        elif (group["rho"] ** 2 < negligible).all():
            result[row] = "inconclusive"
        else:
            result[row] = "keep"
    return pd.Series(result, name="status", dtype=object)


def mc_correlation(
    batch_dir: Path, *, measure: str = "net_worth", dollars: DollarMode = DollarMode.CPI_CONSTANT,
) -> pd.DataFrame:
    """``correlation_table`` for a Monte Carlo batch's succeeded members."""
    connections = load_ensemble(batch_dir)
    try:
        draws = draws_frame(connections)
        if draws.empty:
            raise EnsembleError(f"{batch_dir}: no drawn members (member 0 is never a draw)")
        outcomes = outcomes_frame(connections, list(draws.index), measure=measure, dollars=dollars)
    finally:
        for rc in connections.values():
            rc.close()
    return correlation_table(draws, outcomes)
