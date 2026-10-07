"""Sensitivity notebook widgets (ADR-007 §9): which of a recipe's varying
rows matter, and when.

For a sensitivity (OAT) batch: an overview heatmap (parameters x reporting
years), a tornado at one year, each parameter's delta over time, an exact
attribution of a delta, and -- for a quantile ladder -- a response curve.
For a Monte Carlo batch: R^2 by year and per-row rank correlations with
their 95% intervals.

Each chart is a ``*_view`` function over plain frames (testable without a
kernel); ``sensitivity_widget`` wires them to shared pickers. A picker shown
next to more than one chart is one widget displayed twice, so the views stay
in sync.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import ipywidgets as widgets
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from IPython.display import display
from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm

from analysis.sim import sensitivity
from analysis.sim.ensemble import EnsembleError, read_batch_manifest
from analysis.sim.loader import batch_mode
from analysis.sim.notebook_ui.pickers import (
    AttributionPicker,
    DeltaScalePicker,
    DollarModePicker,
    HeatmapValuePicker,
    ParameterPicker,
    ReportYearPicker,
    find_runs_dir,
)
from analysis.sim.plotting import FRAME_ALPHA
from analysis.sim.types import DeltaScale, DollarMode, HeatmapValue
from analysis.sim.notebook_ui.styled import StyledMarkdown

__all__ = [
    "list_sensitivity_recipes", "list_batches", "parameter_labels", "parameters_note",
    "heatmap_view", "tornado_view", "delta_over_time_view", "attribution_view",
    "response_curve_view", "correlation_view", "r2_view", "sensitivity_widget",
]

# Validated categorical order (dataviz reference palette; CVD-checked).
CATEGORICAL = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948")
LOW_ARM, HIGH_ARM = "#eb6834", "#2a78d6"  # quantiles below / above the center
LOSS, NEUTRAL, GAIN = "#e34948", "#f0efec", "#2a78d6"  # diverging pair, gray midpoint
CRITICAL = "#d03b3b"  # status color: reserved for "unattributed", never a series
INK, MUTED, GRID = "#0b0b0b", "#898781", "#e1e0d9"
_SHARE_CMAP = LinearSegmentedColormap.from_list("share", ["#fcfcfb", "#86b6ef", "#184f95"])
_SWING_CMAP = LinearSegmentedColormap.from_list("swing", [LOSS, NEUTRAL, GAIN])
_MAX_KEYS = len(CATEGORICAL) - 1  # the last slot is "other"


def list_sensitivity_recipes(runs_dir: Path) -> list[str]:
    """Recipes with at least one generated batch of either mode."""
    from analysis.sim.ensemble import latest_ensemble_batch

    runs_dir = Path(runs_dir)
    if not runs_dir.is_dir():
        return []
    return sorted(p.name for p in runs_dir.iterdir()
                  if p.is_dir() and latest_ensemble_batch(p) is not None)


def list_batches(recipe_dir: Path) -> list[tuple[str, Path]]:
    """``(label, batch dir)`` for every batch, newest first, labeled with its
    mode and member count."""
    recipe_dir = Path(recipe_dir)
    if not recipe_dir.is_dir():
        return []
    batches = []
    for p in sorted((p for p in recipe_dir.iterdir() if p.is_dir()), reverse=True):
        manifest = read_batch_manifest(p)
        members = manifest["member_count"] if manifest else len(list(p.glob("member_*.sqlite3")))
        if not members:
            continue
        label = "one-at-a-time" if batch_mode(p) == "sensitivity" else "Monte Carlo"
        batches.append((f"{p.name} ({label}, {members} members)", p))
    return batches


def parameter_labels(deltas: pd.DataFrame) -> dict[str, str]:
    """Row name -> "target, low to high" from an OAT batch's deltas, in the
    order rows first appear."""
    labels: dict[str, str] = {}
    for row, group in deltas.groupby("row", sort=False):
        by_q = group.drop_duplicates("quantile").sort_values("quantile")
        labels[str(row)] = (f"{by_q['target'].iloc[0]}, {by_q['value'].iloc[0]} "
                       f"to {by_q['value'].iloc[-1]}")
    return labels


def _delta_column(scale: DeltaScale) -> str:
    return "relative" if scale == DeltaScale.RELATIVE else "delta"


def _signed_dollars(v: float) -> str:
    """``+$1,234`` / ``-$1,234`` / ``$0`` -- the sign before the dollar sign."""
    if round(v) == 0:
        return "$0"
    return f"{'+' if v > 0 else '-'}${abs(v):,.0f}"


def _format_value_axis(axis: Any, scale: DeltaScale) -> None:
    if scale == DeltaScale.RELATIVE:
        axis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:+.1%}"))
    else:
        axis.set_major_formatter(plt.FuncFormatter(lambda v, _: _signed_dollars(v)))


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
    base = np.array(plt.matplotlib.colors.to_rgb(LOW_ARM if q < 0.5 else HIGH_ARM))
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
        _fig, ax = plt.subplots(figsize=(10, 0.5 * len(grid) + 1.5))
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
        colorbar.ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0%}"))
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
        _fig, ax = plt.subplots(figsize=(10, 0.55 * len(table) + 1.2))
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


def delta_over_time_view(deltas: pd.DataFrame, row: str, scale: DeltaScale, ax: Any = None) -> Any:
    """One parameter's delta against the base, by reporting year, one line
    per quantile -- where a fund-exhaustion plateau, a retirement-date step
    or a compounding curve shows up."""
    column = _delta_column(scale)
    rows = deltas[(deltas["row"] == row) & (deltas["measure"] == "net_worth")]
    if ax is None:
        _fig, ax = plt.subplots(figsize=(10, 4.5))
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
        _fig, ax = plt.subplots(figsize=(10, 4.5))
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
                        ax: Any = None) -> Any:
    """Delta against quantile at one year, with the base at the center: a
    step or a sharp change of slope here is a transition inside the range."""
    column = _delta_column(scale)
    points = deltas[(deltas["row"] == row) & (deltas["year"] == year)
                    & (deltas["measure"] == "net_worth")].sort_values("quantile")
    curve = pd.concat([
        points[["quantile", column, "value"]],
        pd.DataFrame({"quantile": [0.5], column: [0.0], "value": ["base"]}),
    ]).sort_values("quantile")
    if ax is None:
        _fig, ax = plt.subplots(figsize=(10, 4))
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
    table: pd.DataFrame, year: int, negligible: float, ax: Any = None, by: str = "rho",
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
        _fig, ax = plt.subplots(figsize=(10, 0.5 * len(at) + 1.2))
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
        "Spearman rank correlation with net worth" if by == "rho"
        else "standardized rank-regression coefficient (others held fixed)", color=MUTED,
    )
    _recessive(ax, grid_axis="x")
    ax.legend(framealpha=FRAME_ALPHA, loc="lower right")
    return ax


def r2_view(table: pd.DataFrame, ax: Any = None) -> Any:
    """R^2 by year: how much of the outcome per-row effects explain at all."""
    r2 = table.drop_duplicates("year").set_index("year")["r2"]
    if ax is None:
        _fig, ax = plt.subplots(figsize=(10, 2.2))
    ax.plot(r2.index, r2, color=HIGH_ARM, linewidth=2, marker="o", markersize=4, label="R²")
    ax.axhline(sensitivity.R2_TRUST, color=MUTED, linewidth=1, linestyle="--",
               label=f"trust threshold ({sensitivity.R2_TRUST})")
    ax.set_ylim(0, 1.05)
    _recessive(ax)
    ax.legend(framealpha=FRAME_ALPHA, loc="lower right")
    return ax


def parameters_note(mode: str | None) -> str:
    """The explanation shown above a batch's parameters table."""
    note = (
        "Each varying row is a `calendar.csv` row with `vary_*` columns set: **affects** is "
        "what it acts on, **target** whether its date or amount varies, and **range**/"
        "**center**/**shape** come from its distribution; **moments** are what those settings "
        "imply. The center is the deterministic base case: member 0 resolves every row to it. "
        "A correlated row names its **correlate_with** parent and **rho**. Latent factors "
        "appear as their own rows with target **factor**: they have no distribution, so no "
        "range, center, shape or moments, and only a parent and rho if they have one."
    )
    if mode == "sensitivity":
        return note + (
            " Each other one-at-a-time member moves one row or latent factor to the "
            "**value@q** shown and holds the rest at their centers, with one exception: a "
            "latent factor (target **factor**, no distribution of its own; its value is a "
            "standard score) also carries every row under it to that row's likeliest value "
            "given the factor. The charts label it **(factor)**."
        )
    if mode == "monte_carlo":
        return note + (
            " Every other member draws every row at random; **drawn** is the range they "
            "actually sampled."
        )
    return note


def _section(title: str, controls: list[widgets.Widget], out: widgets.Output) -> widgets.VBox:
    return widgets.VBox([widgets.HTML(f"<h4>{title}</h4>"), widgets.HBox(controls), out])


def _show(out: widgets.Output, draw) -> None:
    out.clear_output(wait=True)
    with out:
        try:
            draw()
        except EnsembleError as exc:
            display(StyledMarkdown(f"_{exc}_"))


def sensitivity_widget(*,
                       data_dir: Path | str | None = None,
                       state_dir: Path | None = None,
                       runs_dir: Path | str | None = None,
                       dollar_mode: DollarModePicker | None = None,
                       negligible: float = sensitivity.DEFAULT_NEGLIGIBLE,
                       ) -> widgets.VBox:
    """Recipe and batch dropdowns, then the ADR-007 §9 views for that batch:
    the OAT views for a sensitivity batch, the correlation views for a Monte
    Carlo one."""
    runs_dir = find_runs_dir(data_dir=data_dir, state_dir=state_dir, runs_dir=runs_dir)
    recipe = widgets.Dropdown(options=list_sensitivity_recipes(runs_dir), description="Recipe:")
    batch = widgets.Dropdown(options=[], description="Batch:", layout=widgets.Layout(width="max-content"))
    dollar_picker = dollar_mode or DollarModePicker(DollarMode.CPI_CONSTANT)
    scale_picker = DeltaScalePicker()
    heatmap_picker = HeatmapValuePicker()
    attribution_picker = AttributionPicker()
    parameter_picker = ParameterPicker()
    year_picker = ReportYearPicker()
    body = widgets.VBox()
    cache: dict[tuple[Path, DollarMode], pd.DataFrame] = {}

    def _deltas() -> pd.DataFrame:
        key = (batch.value, dollar_picker.value)
        if key not in cache:
            cache[key] = sensitivity.oat_deltas(batch.value, dollar_picker.value)
        return cache[key]

    outs = {name: widgets.Output() for name in
            ("parameters", "heatmap", "tornado", "time", "attribution", "curve", "r2", "rho")}

    def _draw_heatmap(*_: object) -> None:
        def draw() -> None:
            grid = sensitivity.rank_by_year(
                _deltas(), relative=scale_picker.value == DeltaScale.RELATIVE,
                value=heatmap_picker.value.value,
            )
            _fig, ax = plt.subplots(figsize=(11, 0.5 * len(grid) + 1.6))
            heatmap_view(grid, heatmap_picker.value, scale_picker.value, ax=ax)
            ax.set_title("Which parameter matters when", loc="left")
            plt.show()
        _show(outs["heatmap"], draw)

    def _draw_tornado(*_: object) -> None:
        def draw() -> None:
            table = sensitivity.oat_table(_deltas(), year_picker.value,
                                          relative=scale_picker.value == DeltaScale.RELATIVE)
            tornado_view(table, scale_picker.value)
            plt.title(f"Effect on net worth in {year_picker.value}", loc="left")
            plt.show()
            fmt = "{:+.1%}" if scale_picker.value == DeltaScale.RELATIVE else _signed_dollars
            display(table.style.format(
                {**{c: fmt for c in table.columns if c.startswith("delta@")},
                 "swing": fmt, "impact": fmt, "share": "{:.0%}"}, na_rep="-"))
        _show(outs["tornado"], draw)

    def _draw_time(*_: object) -> None:
        def draw() -> None:
            if not parameter_picker.value:
                return
            delta_over_time_view(_deltas(), parameter_picker.value, scale_picker.value)
            plt.title(f"{parameter_picker.value}: change in net worth vs. the base", loc="left")
            plt.show()
        _show(outs["time"], draw)

    def _draw_attribution(*_: object) -> None:
        def draw() -> None:
            if not parameter_picker.value:
                return
            tables = sensitivity.oat_attribution(
                batch.value, parameter_picker.value, by=attribution_picker.value.value,
                dollars=dollar_picker.value,
            )
            deltas = _deltas()
            base = deltas.loc[(deltas["row"] == parameter_picker.value)
                              & (deltas["year"] == year_picker.value)
                              & (deltas["measure"] == "net_worth"), "base"]
            _ax, numbers = attribution_view(
                tables, year_picker.value, scale=scale_picker.value,
                base=float(base.iloc[0]) if len(base) else None,
            )
            plt.title(f"{parameter_picker.value} in {year_picker.value}: where the change "
                      "comes from", loc="left")
            plt.show()
            relative = scale_picker.value == DeltaScale.RELATIVE
            display(numbers.style.format("{:+.2%}" if relative else _signed_dollars))
        _show(outs["attribution"], draw)

    def _draw_curve(*_: object) -> None:
        def draw() -> None:
            if not parameter_picker.value:
                return
            response_curve_view(_deltas(), parameter_picker.value, year_picker.value,
                                scale_picker.value)
            plt.title(f"{parameter_picker.value} in {year_picker.value}: response across "
                      "its range", loc="left")
            plt.show()
        _show(outs["curve"], draw)

    def _draw_correlation(*_: object) -> None:
        def draw_r2() -> None:
            r2_view(_correlation())
            plt.title("How much per-parameter effects explain, by year", loc="left")
            plt.show()

        def draw_rho() -> None:
            report = _report()
            table = report.table
            correlation_view(table, year_picker.value, negligible, by=report.by)
            plt.title(
                f"Rank correlation with net worth in {year_picker.value}" if report.by == "rho"
                else f"Effect on net worth in {year_picker.value}, others held fixed", loc="left",
            )
            plt.show()
            if report.correlated:
                display(StyledMarkdown(
                    "_This batch's rows are correlated, so a row's rank correlation includes its "
                    "correlates' effects. Rows are ranked and pinned by their rank-regression "
                    "coefficient (SRRC), which holds the others fixed._"
                ))
            status = sensitivity.classify(table, negligible, by=report.by)
            n = int(table["n"].iloc[0])
            needed = sensitivity.required_members(negligible)
            note = (f" -- {n} members can't call a row negligible at {negligible:.0%}; "
                    f"that needs about {needed}") if report.by == "rho" and n < needed else ""
            # As in the CLI: that count follows from rho's interval, and
            # SRRC's has no fixed one (it narrows as R^2 rises).
            display(StyledMarkdown(f"**{n} drawn members**{note}"))
            display(status.to_frame())
            if not report.scores.empty:
                display(StyledMarkdown(
                    "**Did the batch reproduce its correlations?** The correlation each "
                    "correlated pair achieved, estimated from its scores' rank agreement "
                    "(Kendall's tau), with its 95% interval, against the expected one "
                    "(**link**: the pair's own rho; **implied**: the product along "
                    "the path joining them). **outside** means the batch didn't reproduce it."
                ))
                display(report.scores.round(3))
        _show(outs["r2"], draw_r2)
        _show(outs["rho"], draw_rho)

    correlation_cache: dict[tuple[Path, DollarMode], sensitivity.McReport] = {}

    def _report() -> sensitivity.McReport:
        key = (batch.value, dollar_picker.value)
        if key not in correlation_cache:
            correlation_cache[key] = sensitivity.mc_report(batch.value, dollars=dollar_picker.value)
        return correlation_cache[key]

    def _correlation() -> pd.DataFrame:
        return _report().table

    oat_sections = {
        "heatmap": (_draw_heatmap, _section("Overview", [heatmap_picker], outs["heatmap"])),
        "tornado": (_draw_tornado, _section("At one year", [year_picker], outs["tornado"])),
        "time": (_draw_time, _section("Over time", [parameter_picker], outs["time"])),
        "attribution": (_draw_attribution, _section(
            "Why", [parameter_picker, year_picker, attribution_picker], outs["attribution"])),
        "curve": (_draw_curve, _section(
            "Across the range", [parameter_picker, year_picker], outs["curve"])),
    }
    parameters_section = _section("Parameters", [], outs["parameters"])

    def _draw_parameters() -> None:
        def draw() -> None:
            table = sensitivity.parameters(batch.value)
            if table.empty:
                display(StyledMarkdown("_This batch has no varying rows._"))
                return
            display(StyledMarkdown(parameters_note(state["mode"])))
            display(table)
        _show(outs["parameters"], draw)

    mc_sections = [
        _section("Can per-parameter numbers be trusted?", [], outs["r2"]),
        _section("Which rows could be pinned?", [year_picker], outs["rho"]),
    ]
    state: dict[str, Any] = {"mode": None, "ladder": False, "loading": False}

    def _draw_all(*_: object) -> None:
        if state["mode"] == "sensitivity":
            for name, (draw, _box) in oat_sections.items():
                if name != "curve" or state["ladder"]:
                    draw()
        elif state["mode"] == "monte_carlo":
            _draw_correlation()

    def _on_batch(*_: object) -> None:
        if batch.value is None:
            body.children = []
            state["mode"] = None
            return
        state["mode"] = batch_mode(batch.value)
        try:
            if state["mode"] == "sensitivity":
                deltas = _deltas()
                manifest = read_batch_manifest(batch.value) or {}
                state["ladder"] = len(manifest.get("quantiles", [])) >= 4
                years = sorted(int(y) for y in deltas["year"].unique())
                # Repopulating the pickers fires their observers; the single
                # _draw_all below is the redraw, so suppress theirs meanwhile.
                state["loading"] = True
                try:
                    parameter_picker.set_parameters(parameter_labels(deltas))
                    year_picker.set_years(years)
                finally:
                    state["loading"] = False
                body.children = [parameters_section] + [
                    box for name, (_draw, box) in oat_sections.items()
                    if name != "curve" or state["ladder"]
                ]
            else:
                table = _correlation()
                state["loading"] = True
                try:
                    year_picker.set_years(sorted(int(y) for y in table["year"].unique()))
                finally:
                    state["loading"] = False
                body.children = [parameters_section, *mc_sections]
        except EnsembleError as exc:
            message = widgets.Output()
            with message:
                display(StyledMarkdown(f"_{exc}_"))
            body.children = [message]
            state["mode"] = None
            return
        _draw_parameters()  # depends only on the batch, not on units or pickers
        _draw_all()

    def _on_recipe(*_: object) -> None:
        options = list_batches(runs_dir / recipe.value) if recipe.value else []
        batch.options = options
        batch.value = options[0][1] if options else None

    def _on_units(*_: object) -> None:
        _draw_all()

    def _on_year(*_: object) -> None:
        if state["loading"]:
            return
        if state["mode"] == "sensitivity":
            _draw_tornado()
            _draw_attribution()
            if state["ladder"]:
                _draw_curve()
        elif state["mode"] == "monte_carlo":
            _draw_correlation()

    def _on_parameter(*_: object) -> None:
        if state["loading"]:
            return
        if state["mode"] == "sensitivity":
            _draw_time()
            _draw_attribution()
            if state["ladder"]:
                _draw_curve()

    # Fill options before observing, so setting them doesn't trigger a draw.
    _on_recipe()
    _on_batch()
    recipe.observe(_on_recipe, names="value")
    batch.observe(_on_batch, names="value")
    dollar_picker.observe(_on_units, names="value")
    scale_picker.observe(_on_units, names="value")
    heatmap_picker.observe(_draw_heatmap, names="value")
    attribution_picker.observe(_draw_attribution, names="value")
    year_picker.observe(_on_year, names="value")
    parameter_picker.observe(_on_parameter, names="value")
    return widgets.VBox([
        widgets.HBox([recipe, batch]),
        widgets.HBox([dollar_picker, scale_picker]),
        body,
    ])

