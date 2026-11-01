"""Sensitivity charts (ADR-007 §9, #329), widget-free so the notebook's
``sensitivity_widget`` and ``sim ensemble sensitivity --chart`` draw the
same ones.

For a sensitivity (OAT) batch: an overview heatmap (parameters x reporting
years), a tornado at one year, each parameter's delta over time, an exact
attribution of a delta, and -- for a quantile ladder -- a response curve.
For a Monte Carlo batch: R^2 by year and per-row rank correlations with
their 95% intervals.

Each chart is a ``*_view`` function over plain frames that draws on the
axes it's given (or on a new figure's); the ``*_figure`` functions put a
command's panels together on one figure.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm, to_rgb
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter

from analysis.sim import sensitivity
from analysis.sim.ensemble import EnsembleError
from analysis.sim.plotting import CATEGORICAL, FRAME_ALPHA, GRID, INK, MUTED
from analysis.sim.types import DeltaScale, HeatmapValue

__all__ = [
    "attribution_figure", "attribution_view", "correlation_figure", "correlation_view",
    "delta_over_time_view", "heatmap_view", "oat_figure", "r2_view", "response_curve_view",
    "row_figure", "signed_dollars", "tornado_view",
]

LOW_ARM, HIGH_ARM = "#eb6834", "#2a78d6"  # quantiles below / above the center
LOSS, NEUTRAL, GAIN = "#e34948", "#f0efec", "#2a78d6"  # diverging pair, gray midpoint
CRITICAL = "#d03b3b"  # status color: reserved for "unattributed", never a series
_SHARE_CMAP = LinearSegmentedColormap.from_list("share", ["#fcfcfb", "#86b6ef", "#184f95"])
_SWING_CMAP = LinearSegmentedColormap.from_list("swing", [LOSS, NEUTRAL, GAIN])
_MAX_KEYS = len(CATEGORICAL) - 1  # the last slot is "other"


def _delta_column(scale: DeltaScale) -> str:
    return "relative" if scale == DeltaScale.RELATIVE else "delta"


def signed_dollars(v: float) -> str:
    """``+$1,234`` / ``-$1,234`` / ``$0`` -- the sign before the dollar sign."""
    if round(v) == 0:
        return "$0"
    return f"{'+' if v > 0 else '-'}${abs(v):,.0f}"


def _new_axes(width: float, height: float) -> Any:
    """Axes on a new figure, for a view called without ``ax``."""
    return Figure(figsize=(width, height), layout="constrained").add_subplot()


def _format_value_axis(axis: Any, scale: DeltaScale) -> None:
    if scale == DeltaScale.RELATIVE:
        axis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:+.1%}"))
    else:
        axis.set_major_formatter(FuncFormatter(lambda v, _: signed_dollars(v)))


def _recessive(ax: Any, grid_axis: str = "y") -> None:
    ax.grid(True, axis=grid_axis, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.tick_params(colors=MUTED)


def _quantile_color(q: float, quantiles: list[float]) -> str | tuple[float, ...]:
    """Ordered arms: below the center in orange, above in blue, the outermost
    rung at full strength and inner rungs lighter -- never lighter than a
    2:1-contrast step."""
    arm = [x for x in quantiles if (x < 0.5) == (q < 0.5)]
    distance = abs(q - 0.5)
    strength = 1.0 if len(arm) == 1 else 0.45 + 0.55 * distance / max(abs(x - 0.5) for x in arm)
    base = np.array(to_rgb(LOW_ARM if q < 0.5 else HIGH_ARM))
    return tuple(1 - strength * (1 - base))


def _share_cells(data: np.ndarray) -> tuple[dict, str, list[list[str]], np.ndarray]:
    """Sequential styling from 0 to 100%, or to the largest share when a latent
    factor's exceeds 100% (#39: a factor's share is over the ordinary rows'
    total, so it isn't capped), so such cells stay distinguishable rather
    than clipping to one color: (imshow kwargs, colorbar label, cell text,
    dark cells)."""
    finite = data[np.isfinite(data)]
    top = max(1.0, float(finite.max())) if finite.size else 1.0
    text = [[f"{v:.0%}" if v >= 0.005 else "" for v in r] for r in data]
    return ({"cmap": _SHARE_CMAP, "vmin": 0, "vmax": top}, "share of the year's total impact",
            text, data > 0.55 * top)


def _swing_cells(data: np.ndarray, scale: DeltaScale) -> tuple[dict, str, list[list[str]], np.ndarray]:
    """Diverging styling centered on zero, symmetric about the largest |swing|."""
    finite = np.abs(data[np.isfinite(data)])
    # Every swing undefined (relative mode, base near zero throughout) leaves
    # nothing to scale by; any positive bound keeps the norm valid.
    bound = float(finite.max()) if finite.size and finite.max() > 0 else 1.0
    fmt = (lambda v: f"{v:+.0%}") if scale == DeltaScale.RELATIVE else (lambda v: f"{v / 1000:+,.0f}k")
    text = [[fmt(v) if abs(v) > bound * 0.02 else "" for v in r] for r in data]
    return ({"cmap": _SWING_CMAP, "norm": TwoSlopeNorm(vmin=-bound, vcenter=0, vmax=bound)},
            "signed swing (high quantile minus low)", text, np.abs(data) > bound * 0.6)


def heatmap_view(grid: pd.DataFrame, value: HeatmapValue, scale: DeltaScale, ax: Any = None) -> Any:
    """Parameters x reporting years (``sensitivity.rank_by_year`` output):
    sequential share, or diverging signed swing centered on zero. Cells are
    labeled while the grid is small enough to read. Latent factors are
    marked ``(factor)``."""
    if ax is None:
        ax = _new_axes(10, 0.5 * len(grid) + 1.5)
    data = grid.astype(float).to_numpy()
    style, label, text, dark = (
        _share_cells(data) if value == HeatmapValue.SHARE else _swing_cells(data, scale)
    )
    image = ax.imshow(data, aspect="auto", **style)
    factors = grid.attrs.get("factors", set())
    ax.set_yticks(range(len(grid)), [sensitivity.row_label(r, factors) for r in grid.index])
    ax.set_xticks(range(len(grid.columns)), [str(y) for y in grid.columns], rotation=90)
    if len(grid.columns) <= 25:
        for i in range(data.shape[0]):
            for j in range(data.shape[1]):
                if text[i][j]:
                    ax.text(j, i, text[i][j], ha="center", va="center", fontsize=7,
                            color="white" if dark[i, j] else INK)
    colorbar = ax.figure.colorbar(image, ax=ax, fraction=0.03, pad=0.02)
    colorbar.set_label(label, color=MUTED)
    if value == HeatmapValue.SWING:
        _format_value_axis(colorbar.ax.yaxis, scale)
    else:
        colorbar.ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0%}"))
    ax.tick_params(colors=MUTED, length=0)
    for spine in ax.spines.values():
        spine.set_visible(False)
    return ax


def tornado_view(table: pd.DataFrame, scale: DeltaScale, ax: Any = None) -> Any:
    """``sensitivity.oat_table`` at one year: each row's delta at its lowest
    and highest quantile, largest impact on top. Latent factors are marked
    ``(factor)``."""
    delta_cols = [c for c in table.columns if c.startswith("delta@")]
    low_col, high_col = delta_cols[0], delta_cols[-1]
    if ax is None:
        ax = _new_axes(10, 0.55 * len(table) + 1.2)
    y = np.arange(len(table))[::-1]
    height = 0.36
    # An undefined delta (relative, base near zero) or a missing one (failed
    # member) is left out, not drawn as a zero-length bar claiming no effect.
    for column, offset, color, label in (
        (low_col, height / 2, LOW_ARM, f"q={low_col[6:]} (low)"),
        (high_col, -height / 2, HIGH_ARM, f"q={high_col[6:]} (high)"),
    ):
        known = table[column].notna().to_numpy()
        ax.barh(y[known] + offset, table[column].to_numpy(dtype=float)[known], height=height,
                color=color, edgecolor="white", linewidth=1, label=label)
    ax.axvline(0, color=MUTED, linewidth=0.8)
    factors = set(table.index[table["target"] == sensitivity.FACTOR_TARGET])
    ax.set_yticks(y, [sensitivity.row_label(r, factors) for r in table.index])
    _format_value_axis(ax.xaxis, scale)
    _recessive(ax, grid_axis="x")
    ax.legend(framealpha=FRAME_ALPHA, loc="lower right")
    return ax


def delta_over_time_view(deltas: pd.DataFrame, row: str, scale: DeltaScale, ax: Any = None, *,
                         measure: str = "net_worth") -> Any:
    """One parameter's delta against the base, by reporting year, one line
    per quantile -- where a fund-exhaustion plateau, a retirement-date step
    or a compounding curve shows up."""
    column = _delta_column(scale)
    rows = deltas[(deltas["row"] == row) & (deltas["measure"] == measure)]
    if ax is None:
        ax = _new_axes(10, 4.5)
    quantiles = sorted(rows["quantile"].unique())
    for q in quantiles:
        line = rows[rows["quantile"] == q].sort_values("year")
        ax.plot(line["year"], line[column], color=_quantile_color(q, quantiles), linewidth=2,
                marker="o", markersize=4, label=f"q={q:g} ({line['value'].iloc[0]})")
    ax.axhline(0, color=MUTED, linewidth=0.8)
    ax.set_xlabel("year", color=MUTED)
    _format_value_axis(ax.yaxis, scale)
    _recessive(ax)
    ax.legend(framealpha=FRAME_ALPHA)
    return ax


def _fold_keys(tables: dict[float, pd.DataFrame]) -> list[str]:
    """Attribution keys in a stable order -- by |delta| at the final year in
    the outermost quantile -- so a key keeps its color across quantiles and
    years; past ``_MAX_KEYS`` the rest fold into "other"."""
    skip = {sensitivity.UNATTRIBUTED, sensitivity.TOTAL}
    outer = tables[max(tables, key=lambda q: abs(q - 0.5))]
    keys = [k for k in outer.columns if k not in skip]
    for table in tables.values():
        keys += [k for k in table.columns if k not in skip and k not in keys]
    return keys


def attribution_view(
    tables: dict[float, pd.DataFrame], year: int, ax: Any = None, *,
    scale: DeltaScale = DeltaScale.ABSOLUTE, base: float | None = None,
) -> tuple[Any, pd.DataFrame]:
    """One stacked bar per quantile: the delta at ``year`` decomposed into
    ``sensitivity.attribution``'s keys (gains up, losses down), with the
    total marked. Returns the axes and the plotted numbers as a table.

    With ``scale=RELATIVE``, every part is divided by ``|base|`` -- the base
    member's net worth that year, in the same dollars -- so the bars sum to
    the same "% of base" the other charts show. A base within
    ``sensitivity.RELATIVE_NEAR_ZERO_DOLLARS`` of zero has no meaningful
    percentage and raises ``EnsembleError``."""
    keys = _fold_keys(tables)
    shown, folded = keys[:_MAX_KEYS], keys[_MAX_KEYS:]
    rows = {}
    for q, table in tables.items():
        values = {k: float(table.loc[year, k]) if k in table.columns else 0.0 for k in shown}
        if folded:
            values["other"] = float(sum(table.loc[year, k] for k in folded if k in table.columns))
        values[sensitivity.UNATTRIBUTED] = float(table.loc[year, sensitivity.UNATTRIBUTED])
        values[sensitivity.TOTAL] = float(table.loc[year, sensitivity.TOTAL])
        rows[f"q={q:g}"] = values
    numbers = pd.DataFrame(rows)
    if scale == DeltaScale.RELATIVE:
        if base is None or abs(base) < sensitivity.RELATIVE_NEAR_ZERO_DOLLARS:
            raise EnsembleError(
                f"net worth in {year} is too close to zero for % of base; switch to $ delta"
            )
        numbers = numbers / abs(base)
        visible = 0.5 / abs(base)
    else:
        visible = 0.5  # half a dollar
    if ax is None:
        ax = _new_axes(10, 4.5)
    parts = [k for k in numbers.index if k != sensitivity.TOTAL]
    colors = {k: CATEGORICAL[i] for i, k in enumerate(shown)}
    colors["other"] = CATEGORICAL[-1]
    colors[sensitivity.UNATTRIBUTED] = CRITICAL
    x = np.arange(len(numbers.columns))
    up, down = np.zeros(len(x)), np.zeros(len(x))
    for key in parts:
        values = numbers.loc[key].to_numpy()
        if not np.any(np.abs(values) >= visible):
            continue
        bottom = np.where(values >= 0, up, down)
        ax.bar(x, values, bottom=bottom, width=0.55, color=colors[key],
               edgecolor="white", linewidth=1.5,
               label=f"{key} (!)" if key == sensitivity.UNATTRIBUTED else key)
        up = np.where(values >= 0, up + values, up)
        down = np.where(values < 0, down + values, down)
    ax.scatter(x, numbers.loc[sensitivity.TOTAL], marker="D", s=40, color=INK, zorder=3,
               label="total")
    ax.axhline(0, color=MUTED, linewidth=0.8)
    ax.set_xticks(x, numbers.columns)
    _format_value_axis(ax.yaxis, scale)
    _recessive(ax)
    ax.legend(framealpha=FRAME_ALPHA, bbox_to_anchor=(1.01, 1), loc="upper left")
    return ax, numbers


def response_curve_view(deltas: pd.DataFrame, row: str, year: int, scale: DeltaScale,
                        ax: Any = None, *, measure: str = "net_worth") -> Any:
    """Delta against quantile at one year, with the base at the center: a
    step or a sharp change of slope here is a transition inside the range."""
    column = _delta_column(scale)
    points = deltas[(deltas["row"] == row) & (deltas["year"] == year)
                    & (deltas["measure"] == measure)].sort_values("quantile")
    curve = pd.concat([
        points[["quantile", column, "value"]],
        pd.DataFrame({"quantile": [0.5], column: [0.0], "value": ["base"]}),
    ]).sort_values("quantile")
    if ax is None:
        ax = _new_axes(10, 4)
    # NaN breaks the line rather than pretending an undefined point is zero.
    ax.plot(curve["quantile"], curve[column], color=HIGH_ARM, linewidth=2)
    ax.scatter(points["quantile"], points[column], s=50, color=HIGH_ARM, zorder=3,
               edgecolor="white", linewidth=1.5)
    ax.scatter([0.5], [0.0], s=60, color="white", edgecolor=INK, zorder=3, label="base")
    for _, p in points.dropna(subset=[column]).iterrows():
        ax.annotate(p["value"], (p["quantile"], p[column]), textcoords="offset points",
                    xytext=(0, 8), ha="center", fontsize=8, color=MUTED)
    ax.axhline(0, color=MUTED, linewidth=0.8)
    ax.set_xlim(0, 1)
    ax.set_xlabel("quantile of the parameter's range", color=MUTED)
    _format_value_axis(ax.yaxis, scale)
    _recessive(ax)
    ax.legend(framealpha=FRAME_ALPHA)
    return ax


def correlation_view(
    table: pd.DataFrame, year: int, negligible: float, ax: Any = None, by: str = "rho", *,
    measure: str = "net_worth",
) -> Any:
    """Per row at one year: ``by`` (Spearman rho, or SRRC for a correlated
    batch) with its 95% interval, against the band inside which its square is
    negligible. Latent factors, if the table has them, show their rho as
    hollow markers labeled "(factor)": their whole family's effect, which has
    no pin status."""
    at = table[table["year"] == year].set_index("row")
    is_factor = at["factor"].astype(bool) if "factor" in at.columns else pd.Series(False, at.index)
    rows, factor_rows = at[~is_factor], at[is_factor]
    rows = rows.reindex(rows[by].abs().sort_values().index)
    if ax is None:
        ax = _new_axes(10, 0.5 * len(at) + 1.2)
    band = float(np.sqrt(negligible))
    ax.axvspan(-band, band, color=NEUTRAL, label=f"negligible ({by}² < {negligible:.0%})")
    y = np.arange(len(rows))
    # Clipped at zero: float round-off must never make a whisker negative,
    # which errorbar rejects outright.
    xerr = [(rows[by] - rows[f"{by}_low"]).clip(lower=0),
            (rows[f"{by}_high"] - rows[by]).clip(lower=0)]
    ax.errorbar(rows[by], y, xerr=xerr,
                fmt="o", color=HIGH_ARM, ecolor=HIGH_ARM, elinewidth=2, capsize=4, markersize=7,
                label=f"{by}, 95% interval")
    labels = list(rows.index)
    if not factor_rows.empty:
        fy = np.arange(len(rows), len(at))
        factor_err = [(factor_rows["rho"] - factor_rows["rho_low"]).clip(lower=0),
                      (factor_rows["rho_high"] - factor_rows["rho"]).clip(lower=0)]
        ax.errorbar(factor_rows["rho"], fy, xerr=factor_err, fmt="o", color=HIGH_ARM, ecolor=MUTED,
                    markerfacecolor="white", elinewidth=1.5, capsize=4, markersize=7,
                    label="factor's rho (its family)")
        labels += [sensitivity.row_label(name, set(factor_rows.index)) for name in factor_rows.index]
    ax.axvline(0, color=MUTED, linewidth=0.8)
    ax.set_yticks(np.arange(len(at)), labels)
    # rho lives in [-1, 1]; SRRC can pass 1 under collinearity, so widen
    # the axis to its intervals rather than clip them.
    reach = max(1.0, float(np.nanmax(np.abs(rows[[f"{by}_low", f"{by}_high", by]].to_numpy()),
                                     initial=0.0)))
    ax.set_xlim(-reach, reach)
    ax.set_xlabel(
        f"Spearman rank correlation with {_words(measure)}" if by == "rho"
        else "standardized rank-regression coefficient (others held fixed)", color=MUTED,
    )
    _recessive(ax, grid_axis="x")
    ax.legend(framealpha=FRAME_ALPHA, loc="lower right")
    return ax


def r2_view(table: pd.DataFrame, ax: Any = None) -> Any:
    """R^2 by year: how much of the outcome per-row effects explain at all."""
    r2 = table.drop_duplicates("year").set_index("year")["r2"]
    if ax is None:
        ax = _new_axes(10, 2.2)
    ax.plot(r2.index, r2, color=HIGH_ARM, linewidth=2, marker="o", markersize=4, label="R²")
    ax.axhline(sensitivity.R2_TRUST, color=MUTED, linewidth=1, linestyle="--",
               label=f"trust threshold ({sensitivity.R2_TRUST})")
    ax.set_ylim(0, 1.05)
    _recessive(ax)
    ax.legend(framealpha=FRAME_ALPHA, loc="lower right")
    return ax


# What each of sensitivity.MEASURES is called in a chart's title.
_MEASURE_WORDS = {"net_worth": "net worth", "min_net_worth": "lowest net worth so far"}


def _words(measure: str) -> str:
    return _MEASURE_WORDS.get(measure, measure)


def _scale(relative: bool) -> DeltaScale:
    return DeltaScale.RELATIVE if relative else DeltaScale.ABSOLUTE


def _stacked(heights: list[float], width: float = 11) -> tuple[Figure, list[Any]]:
    """A figure of panels one above another, each as tall as ``heights``
    says, in inches."""
    fig = Figure(figsize=(width, sum(heights)), layout="constrained")
    axes = fig.subplots(len(heights), 1, gridspec_kw={"height_ratios": heights}, squeeze=False)
    return fig, [row[0] for row in axes]


def oat_figure(
    deltas: pd.DataFrame, year: int, title: str, *, relative: bool = False,
    measure: str = "net_worth",
) -> Figure:
    """A sensitivity batch's overview, as the notebook shows it: which
    parameter matters when (each one's share of the year's total impact, by
    year), above the tornado at ``year``."""
    scale = _scale(relative)
    grid = sensitivity.rank_by_year(deltas, measure=measure, relative=relative, value="share")
    table = sensitivity.oat_table(deltas, year, measure=measure, relative=relative)
    fig, (top, bottom) = _stacked([0.5 * len(grid) + 1.8, 0.55 * len(table) + 1.4])
    heatmap_view(grid, HeatmapValue.SHARE, scale, ax=top)
    top.set_title(f"{title}: which parameter matters when", loc="left")
    tornado_view(table, scale, ax=bottom)
    bottom.set_title(f"Effect on {_words(measure)} in {year}", loc="left")
    return fig


def row_figure(
    deltas: pd.DataFrame, row: str, year: int, title: str, *, relative: bool = False,
    measure: str = "net_worth", ladder: bool = False,
) -> Figure:
    """One parameter: its delta against the base by year, one line per
    quantile, and -- for a quantile ladder (four or more rungs), where it
    has a shape to show -- its response across the range at ``year``."""
    scale = _scale(relative)
    fig, axes = _stacked([4.5, 4.0] if ladder else [4.5])
    delta_over_time_view(deltas, row, scale, ax=axes[0], measure=measure)
    axes[0].set_title(f"{title}: {row}, change in {_words(measure)} vs. the base", loc="left")
    if ladder:
        response_curve_view(deltas, row, year, scale, ax=axes[1], measure=measure)
        axes[1].set_title(f"{row} in {year}: response across its range", loc="left")
    return fig


def attribution_figure(
    tables: dict[float, pd.DataFrame], row: str, year: int, title: str, *,
    relative: bool = False, base: float | None = None,
) -> tuple[Figure, pd.DataFrame]:
    """``attribution_view`` on a figure of its own: where ``row``'s change
    in net worth at ``year`` comes from, one bar per quantile. ``base`` is
    the base member's net worth that year, needed when ``relative``."""
    fig = Figure(figsize=(11, 4.8), layout="constrained")
    ax = fig.add_subplot()
    _ax, numbers = attribution_view(tables, year, ax, scale=_scale(relative), base=base)
    ax.set_title(f"{title}: {row} in {year}, where the change comes from", loc="left")
    return fig, numbers


def correlation_figure(
    table: pd.DataFrame, year: int, negligible: float, title: str, *, by: str = "rho",
    measure: str = "net_worth",
) -> Figure:
    """A Monte Carlo batch's panels, as the notebook shows them: R^2 by year
    (how far per-row numbers can be trusted) above each row's ``by`` (rho,
    or SRRC for a correlated batch) at ``year`` with its 95% interval."""
    rows = int((table["year"] == year).sum())
    fig, (top, bottom) = _stacked([2.6, 0.5 * rows + 1.6])
    r2_view(table, ax=top)
    top.set_title(f"{title}: how much per-parameter effects explain, by year", loc="left")
    correlation_view(table, year, negligible, ax=bottom, by=by, measure=measure)
    bottom.set_title(
        f"Rank correlation with {_words(measure)} in {year}" if by == "rho"
        else f"Effect on {_words(measure)} in {year}, others held fixed", loc="left",
    )
    return fig
