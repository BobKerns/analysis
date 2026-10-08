"""Ensemble notebook widgets (ADR-006): pick a generated recipe, see its
members' net worth as a quintile-shaded ("fan") chart plus a year-end
mean/median/SD/range table."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import ipywidgets as widgets
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import pandas as pd
from IPython.display import display

from analysis.sim.connection import year_end_values
from analysis.sim.ensemble import (
    latest_ensemble_batch,
    load_ensemble,
    member_run_statuses,
    members_net_worth_by_month,
    percentiles,
)
from analysis.sim.ensemble import summary as ensemble_summary
from analysis.sim.notebook_ui.pickers import (
    DollarModePicker,
    LogLinearModePicker,
    find_data_dir,
    find_runs_dir,
)
from analysis.sim.plotting import FRAME_ALPHA
from analysis.sim.readme import description_view
from analysis.sim.types import DollarMode, LogLinearMode
from analysis.sim.notebook_ui.styled import StyledMarkdown

__all__ = [
    "list_ensemble_recipes", "ensemble_summary_view", "ensemble_fan_chart",
    "ensemble_widget",
]


def list_ensemble_recipes(runs_dir: Path) -> list[str]:
    """Names of every subdirectory of ``runs_dir`` holding at least one
    generated batch (a ``<timestamp>/member_*.sqlite3`` run file) -- the
    recipes ``sim run --ensemble-size`` has generated and run."""
    runs_dir = Path(runs_dir)
    if not runs_dir.is_dir():
        return []
    return sorted(
        p.name for p in runs_dir.iterdir()
        if p.is_dir() and latest_ensemble_batch(p, mode="monte_carlo") is not None
    )


def ensemble_summary_view(
    monthly_summary: pd.DataFrame, member_count: int, failed_count: int = 0,
) -> tuple[str, pd.DataFrame]:
    """Heading and year-end mean/median/SD/range table from a month-indexed
    ``summary()`` frame (mean/median/std/min/max columns). ``member_count``
    is how many *succeeded* members the frame was built from; ``failed_count``
    (excluded from it) is reported alongside, not silently dropped."""
    yearly = year_end_values(monthly_summary)
    yearly.index.name = "year"
    heading = f"**{member_count} member(s)**"
    if failed_count:
        heading += f" ({failed_count} failed, excluded)"
    return heading, yearly


def _band_edges(monthly_summary: pd.DataFrame) -> list[str]:
    """``min``, any ``pNN`` percentile columns in ascending order, ``max``:
    the fan chart's band boundaries from the outside in."""
    inner = sorted(
        (c for c in monthly_summary.columns if c[:1] == "p" and c[1:].replace(".", "").isdigit()),
        key=lambda c: float(c[1:]),
    )
    return ["min", *inner, "max"]


def _band_label(low: str, high: str) -> str:
    if (low, high) == ("min", "max"):
        return "range"
    return f"{low[1:]}–{high[1:]}%"


def ensemble_fan_chart(monthly_summary: pd.DataFrame, ax: Any = None) -> Any:
    """Shade the member distribution month by month behind mean/median
    lines: one band between each pair of adjacent edges (``min``, the
    ``pNN`` columns ``ensemble.percentiles()`` adds, ``max``), darker toward
    the middle, so the chart shows where members cluster rather than just
    how far apart the extremes are. With no ``pNN`` columns it's the single
    min-max band ``sim ensemble summary`` describes."""
    if ax is None:
        _fig, ax = plt.subplots(figsize=(10, 5))
    edges = _band_edges(monthly_summary)
    bands = list(zip(edges, edges[1:], strict=False))
    depth_count = (len(bands) + 1) // 2
    base = mcolors.to_rgb("steelblue")
    labelled: set[int] = set()
    for i, (low, high) in enumerate(bands):
        depth = min(i, len(bands) - 1 - i)  # 0 for the outermost bands
        # Opaque blends with white rather than alpha: a translucent
        # steelblue (alpha=0.2) was visible in Jupyter but washed out of VS
        # Code's inline PNG rendering entirely, and opaque bands also can't
        # double up where adjacent fills overlap by a pixel.
        strength = 0.35 + 0.5 * depth / max(depth_count - 1, 1)
        color = tuple(1 - strength * (1 - c) for c in base)
        # The band is labelled by the edges it shares a depth with -- the
        # outer pair is "range", the next "20–80%", and so on -- once each.
        label = None
        if depth not in labelled:
            labelled.add(depth)
            label = _band_label(edges[depth], edges[len(edges) - 1 - depth])
        ax.fill_between(
            monthly_summary.index, monthly_summary[low], monthly_summary[high],
            color=color, edgecolor=color, linewidth=0.5, label=label,
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
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:,.0f}"))
    ax.grid(True, axis="y", alpha=0.3)
    ax.legend(framealpha=FRAME_ALPHA)
    return ax


def ensemble_widget(*,
                    data_dir: Path | str | None = None,
                    state_dir: Path | None = None,
                    runs_dir: Path | str | None = None,
                    log_linear_mode: LogLinearModePicker | None = None,
                    dollar_mode: DollarModePicker | None = None,
                    show_description: bool = False,
                    ) -> widgets.VBox:
    """Recipe dropdown + fan chart + year-end summary table for an ensemble
    already generated and run via ``sim run <recipe> --ensemble-size N``."""
    resolved_data_dir = find_data_dir(data_dir)
    runs_dir = find_runs_dir(data_dir=data_dir, state_dir=state_dir, runs_dir=runs_dir)
    recipes = list_ensemble_recipes(runs_dir)
    dropdown = widgets.Dropdown(options=recipes, description="Recipe:")
    log_linear_picker = log_linear_mode or LogLinearModePicker()
    dollar_picker = dollar_mode or DollarModePicker()
    out = widgets.Output()

    def _redraw(*_: object) -> None:
        out.clear_output(wait=True)
        if not dropdown.value:
            return
        batch_dir = latest_ensemble_batch(runs_dir / dropdown.value, mode="monte_carlo")
        if batch_dir is None:
            with out:
                display(StyledMarkdown(f"**No Monte Carlo batch for {dropdown.value}.**"))
            return
        statuses = member_run_statuses(batch_dir)
        failed_count = sum(1 for status in statuses.values() if status != "succeeded")
        connections = load_ensemble(batch_dir)
        try:
            mode = dollar_picker.value
            mode_label = "CPI-constant" if mode == DollarMode.CPI_CONSTANT else "nominal"
            # Each member's own net worth is converted before aggregating, not
            # the aggregate after the fact: a member's CPI-constant series
            # reflects its own inflation path, which can differ member to
            # member when the varying row is itself inflation/CPI-linked.
            net_worth = members_net_worth_by_month(connections, mode=mode)
            monthly_summary = ensemble_summary(net_worth)
            heading, yearly = ensemble_summary_view(monthly_summary, len(connections), failed_count)
            with out:
                display(StyledMarkdown(f"{heading} (batch {batch_dir.name})"))
                if show_description:
                    conn = next(iter(connections.values()))
                    display(description_view(conn, data_dir=resolved_data_dir))
                _fig, ax = plt.subplots(figsize=(10, 5))
                # Scale before drawing: matplotlib's set_yscale resets the
                # major formatter to its own log-scale default, which would
                # otherwise clobber ensemble_fan_chart's dollar formatting.
                wants_log = log_linear_picker.value == LogLinearMode.LOG
                if wants_log and (monthly_summary["min"] <= 0).any():
                    # Log scale can't represent zero or negative net worth --
                    # silently switching to linear would clip exactly the
                    # insolvency tail this chart exists to show, rather than
                    # just rendering it oddly.
                    display(StyledMarkdown(
                        "_Log scale can't show net worth that reaches zero "
                        "or below; showing linear instead._"
                    ))
                    wants_log = False
                if wants_log:
                    ax.set_yscale("log")
                # Quintiles shade the chart only; the year-end table stays the
                # same columns `sim ensemble summary` prints.
                ensemble_fan_chart(monthly_summary.join(percentiles(net_worth)), ax=ax)
                ax.set_title(f"Net worth fan chart ({mode_label}) ({dropdown.value})")
                plt.xticks(rotation=90)
                plt.show()
                display(yearly.style.format({c: "${:,.0f}" for c in yearly.columns}))
        finally:
            for con in connections.values():
                con.close()

    dropdown.observe(_redraw, names="value")
    log_linear_picker.observe(_redraw, names="value")
    dollar_picker.observe(_redraw, names="value")
    _redraw()
    pickers = widgets.HBox([log_linear_picker, dollar_picker]) 
    return widgets.VBox([dropdown, pickers, out])
