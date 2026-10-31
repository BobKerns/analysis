"""The ensemble charts (ADR-006, #163), as plain matplotlib figures: the
fan chart of one recipe's members and the comparison's delta panels. The
notebook widgets show them inline and ``sim ensemble summary|compare --chart``
in the terminal (#167), so neither the CLI nor these charts depend on
ipywidgets or IPython. No pyplot either: a figure pyplot doesn't track can't
be flushed half-drawn by another notebook thread (#119), and needs no GUI
backend in a terminal."""

from __future__ import annotations
from matplotlib.axes import Axes

import io
from collections.abc import Callable, Sequence, Iterator
from typing import Any

import matplotlib
import matplotlib.colors as mcolors
import pandas as pd
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter

from analysis.sim.ensemble import comparison_deltas, paired_deltas, summary_with_percentiles
from analysis.sim.plotting import FRAME_ALPHA, mark_deaths
from analysis.sim.types import Death

__all__ = ["band_edges", "ensemble_compare_figure", "ensemble_fan_chart", "ensemble_fan_figure", "figure_bytes"]


def band_edges(monthly_summary: pd.DataFrame) -> list[str]:
    """The ``pNN`` percentile columns in ascending order -- the fan chart's
    band boundaries, outermost first and last -- or ``min``/``max`` for a
    plain ``summary()`` frame that has none."""
    edges = sorted(
        (c for c in monthly_summary.columns if c[:1] == "p" and c[1:].replace(".", "").isdigit()),
        key=lambda c: float(c[1:]),
    )
    return edges or ["min", "max"]


def _band_label(low: str, high: str) -> str:
    if (low, high) == ("min", "max"):
        return "range"
    return f"{float(low[1:]):g}–{float(high[1:]):g}%"


def ensemble_fan_chart(monthly_summary: pd.DataFrame, ax: Any = None) -> Any:
    """Shade the member distribution month by month behind mean/median
    lines: one band between each pair of adjacent ``pNN`` columns
    (``ensemble.summary_with_percentiles()``'s 5th to 95th percentiles),
    darker toward the middle, so the chart shows where members cluster
    rather than how far apart one lucky and one unlucky member ended up.
    A plain ``summary()`` frame, with no ``pNN`` columns, gets the single
    min-max band."""
    if ax is None:
        ax = Figure(figsize=(10, 5)).subplots()
    edges = band_edges(monthly_summary)
    bands = list(zip(edges, edges[1:], strict=False))
    depth_count = (len(bands) + 1) // 2
    base = mcolors.to_rgb("steelblue")
    labelled: set[int] = set()
    for i, (low, high) in enumerate(bands):
        depth = min(i, len(bands) - 1 - i)  # 0 for the outermost bands
        # Opaque blends with white rather than alpha: a translucent
        # steelblue (alpha=0.2) was visible in Jupyter but washed out of VS
        # Code's inline PNG rendering entirely, and opaque bands also can't
        # double up where adjacent fills overlap by a pixel. Even the
        # outermost band starts at half strength: a lighter one (~0.35) still
        # read as plain white in VS Code while looking fine in JupyterLab.
        strength = 0.5 + 0.4 * depth / max(depth_count - 1, 1)
        color = tuple(1 - strength * (1 - c) for c in base)
        # The band is labelled by the edges it shares a depth with -- the
        # outer pair is "5–95%", the next "20–80%", and so on -- once each.
        label = None
        if depth not in labelled:
            labelled.add(depth)
            label = _band_label(edges[depth], edges[len(edges) - 1 - depth])
        ax.fill_between(
            monthly_summary.index, monthly_summary[low], monthly_summary[high],
            # A full-strength edge outlines every band, so the outer
            # envelope stays visible however a renderer treats pale fills.
            color=color, edgecolor=base, linewidth=0.6, label=label,
        )
    ax.plot(monthly_summary.index, monthly_summary["mean"], color="midnightblue",
            linewidth=2, label="mean")
    ax.plot(monthly_summary.index, monthly_summary["median"], color="midnightblue",
            linewidth=1, linestyle="--", label="median")
    # No forced bottom=0: a shortfall scenario can carry a real negative net
    # worth, and clipping it would hide the low end of the range (and
    # potentially the mean/median themselves) rather than show it. A thin
    # zero rule instead -- but axhline, left to itself, feeds into autoscale
    # like any other artist and would drag the axis down to include 0 even
    # when the data is nowhere near it (the exact distortion this chart is
    # trying to avoid), so the autoscaled limits are captured first and
    # restored after: the rule only ends up visible when 0 already falls
    # within the range the data itself earned. Meaningless on a log axis
    # (zero has no position there), so skipped entirely rather than drawn
    # and silently invisible.
    if ax.get_yscale() != "log":
        ylim = ax.get_ylim()
        ax.axhline(0, color="red", linewidth=0.8, zorder=2.5)
        ax.set_ylim(ylim)
    ax.set_xlabel("month")
    ax.set_ylabel("USD")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v:,.0f}"))
    ax.grid(True, axis="y", alpha=0.3)
    ax.legend(framealpha=FRAME_ALPHA)
    return ax


def ensemble_fan_figure(
    monthly_summary: pd.DataFrame, title: str, *,
    reserve: float | None = None, log_scale: bool = False, height: float = 5,
    deaths: Sequence[Death] = (),
) -> Figure:
    """One recipe's fan chart as a whole figure, titled ``title``: on a log
    scale for ``log_scale`` (the caller checks the values are positive), and
    with ``reserve`` as a dashed line when given, which the caller passes
    only when the chart is in the reserve's own CPI-constant dollars.
    ``deaths`` are red lines, member 0's (``ensemble.base_case_deaths``)."""
    fig = Figure(figsize=(10, height))
    ax = fig.subplots()
    # Scale before drawing: matplotlib's set_yscale resets the major
    # formatter to its own log-scale default, which would otherwise clobber
    # ensemble_fan_chart's dollar formatting.
    if log_scale:
        ax.set_yscale("log")
    ensemble_fan_chart(monthly_summary, ax=ax)
    if reserve is not None:
        ax.axhline(reserve, color="darkorange", linewidth=1, linestyle="--", label="reserve")
        ax.legend(framealpha=FRAME_ALPHA)
    mark_deaths(ax, deaths)
    ax.set_title(title)
    ax.tick_params(axis="x", labelrotation=90)
    return fig


def figure_bytes(fig: Figure, fmt: str = "png", dpi: float | None = None) -> bytes:
    """``fig`` rendered as ``fmt`` (``png`` or ``svg``), cropped to what it
    draws, as matplotlib-inline crops the notebook's own output."""
    buf = io.BytesIO()
    fig.savefig(buf, format=fmt, bbox_inches="tight", dpi=dpi or "figure")
    return buf.getvalue()


def _symlog(ax: Any, values: pd.DataFrame) -> None:
    """A log scale for deltas, which run both ways: symmetric log, linear
    within 1% of the largest swing of ``values`` so small deltas near zero
    don't explode. Set before drawing: matplotlib resets the formatter."""
    peak = float(values.abs().max().max()) if values.size else 0.0
    ax.set_yscale("symlog", linthresh=max(peak / 100, 1.0))


def _delta_lines(
    deltas: pd.DataFrame, columns: list[str], ax: Any, title: str, shares: bool = False,
    log_scale: bool = False, deaths: Sequence[Death] = (),
) -> None:
    """One line per column of ``deltas``, light to dark, labelled at the
    right end, over a zero rule; dollars, or percentage points for
    ``shares``, on a symmetric log scale for ``log_scale`` (dollars only).
    ``deaths`` are red lines."""
    if log_scale and not shares:
        _symlog(ax, deltas[columns])
    shades = matplotlib.colormaps["Blues"]
    for i, column in enumerate(columns):
        color = shades(0.35 + 0.6 * i / max(len(columns) - 1, 1))
        ax.plot(deltas.index, deltas[column], color=color, linewidth=2, label=column)
        ax.annotate(column, (deltas.index[-1], deltas[column].iloc[-1]),
                    xytext=(4, 0), textcoords="offset points", va="center", fontsize=8)
    ax.axhline(0, color="gray", linewidth=0.8)
    mark_deaths(ax, deaths)
    ax.set_title(title)
    ax.yaxis.set_major_formatter(FuncFormatter(
        (lambda v, _: f"{v * 100:+.0f} pts") if shares else (lambda v, _: f"${v:,.0f}")
    ))
    ax.grid(True, axis="y", alpha=0.3)
    ax.legend(framealpha=FRAME_ALPHA, ncol=4, fontsize=8)


def ensemble_compare_figure(
    baseline: str, variant: str,
    baseline_frame: pd.DataFrame, variant_frame: pd.DataFrame,
    pairs: list[tuple[str, str]], mode_label: str,
    reserve: float | None = None,
    cpi_frames: tuple[pd.DataFrame, pd.DataFrame] | None = None,
    log_scale: bool = False,
    clip: Callable[[pd.DataFrame], pd.DataFrame] | None = None,
    deaths: Sequence[Death] = (),
) -> Figure:
    """The comparison's panels, top to bottom (#163): the fan of paired
    per-future deltas when the recipes pair; the delta in each tail mean
    (CVaR); each percentile breakpoint's delta; and, given a
    ``reserve``, the change in the shares below it (see
    ``comparison_deltas`` for ``cpi_frames``). ``log_scale`` puts the dollar
    panels on a symmetric log scale (#171), since deltas run both ways.
    ``clip`` cuts what is drawn to the months shown (#245), after the deltas
    are taken over the whole run: the share ever below the reserve looks
    back to the start. ``deaths`` are red lines on every panel, the
    baseline's member 0's (``ensemble.base_case_deaths``)."""
    clip = clip or (lambda frame: frame)
    deltas = clip(comparison_deltas(
        baseline_frame, variant_frame, reserve=reserve, cpi_frames=cpi_frames,
    ))
    panels = 2 + bool(pairs) + (reserve is not None)
    fig = Figure(figsize=(10, 4 * panels),
                 layout="tight")
    all_axes: list[Axes] = list(fig.subplots(panels, 1, sharex=True))
    axes: Iterator[Axes] = iter(all_axes)
    if pairs:
        ax = next(axes)
        paired = clip(summary_with_percentiles(
            paired_deltas(baseline_frame, variant_frame, pairs),
        ))
        if log_scale:
            _symlog(ax, paired)
        ensemble_fan_chart(paired, ax=ax)
        mark_deaths(ax, deaths)
        ax.set_title(
            f"{variant} − {baseline}, future by future ({len(pairs)} paired members)"
        )
        ax.set_xlabel("")
    ax = next(axes)
    _delta_lines(
        deltas, [c for c in deltas.columns if c.startswith("Δcvar")], ax,
        f"{variant} − {baseline}: mean of the worst 5% and 20% (CVaR)",
        log_scale=log_scale, deaths=deaths,
    )
    ax = next(axes)
    _delta_lines(
        deltas, [c for c in deltas.columns if c.startswith("Δp")], ax,
        f"{variant} − {baseline}: each percentile vs the same percentile ({mode_label})",
        log_scale=log_scale, deaths=deaths,
    )
    if reserve is not None:
        ax = next(axes)
        _delta_lines(
            deltas, ["Δbelow_reserve", "Δever_below_reserve"], ax,
            f"{variant} − {baseline}: share of members below the ${reserve:,.0f} reserve",
            shares=True, deaths=deaths,
        )
    # sharex=True hides the x tick labels on all but the bottom panel;
    # each panel is read on its own, so put them back.
    for ax in all_axes:
        ax.tick_params(axis="x", labelbottom=True, labelrotation=90)
    return fig
