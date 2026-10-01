"""Ensemble notebook widgets (ADR-006): pick a generated recipe, see its
members' net worth as a percentile-band ("fan") chart plus a year-end
mean/median/SD/range table."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import ipywidgets as widgets
import matplotlib.pyplot as plt
import pandas as pd
from IPython.display import Markdown, display

from analysis.sim.connection import year_end_values
from analysis.sim.ensemble import (
    latest_ensemble_batch,
    load_ensemble,
    member_run_statuses,
    members_net_worth_by_month,
)
from analysis.sim.ensemble import summary as ensemble_summary
from analysis.sim.notebook_ui.pickers import DollarModePicker, LogLinearModePicker, find_data_dir, find_runs_dir
from analysis.sim.plotting import FRAME_ALPHA
from analysis.sim.types import DollarMode, LogLinearMode

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
        if p.is_dir() and latest_ensemble_batch(p) is not None
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


def ensemble_fan_chart(monthly_summary: pd.DataFrame, ax: Any = None) -> Any:
    """Shade the member range (min-max) behind mean/median lines, month by
    month -- the same three columns ``sim ensemble summary`` prints."""
    if ax is None:
        _fig, ax = plt.subplots(figsize=(10, 5))
    # alpha=0.2 steelblue over white is ~(227,239,245) -- visible in Jupyter's
    # rendering path but apparently not past whatever VS Code's inline PNG
    # renderer does to it; a stronger fill plus a drawn edge doesn't depend
    # on the two matching exactly.
    ax.fill_between(
        monthly_summary.index, monthly_summary["min"], monthly_summary["max"],
        color="steelblue", alpha=0.5, edgecolor="steelblue", linewidth=0.5,
        label="range",
    )
    ax.plot(monthly_summary.index, monthly_summary["mean"], color="steelblue",
            linewidth=2, label="mean")
    ax.plot(monthly_summary.index, monthly_summary["median"], color="steelblue",
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
                    ) -> widgets.VBox:
    """Recipe dropdown + fan chart + year-end summary table for an ensemble
    already generated and run via ``sim run <recipe> --ensemble-size N``."""
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
        batch_dir = latest_ensemble_batch(runs_dir / dropdown.value)
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
            monthly_summary = ensemble_summary(
                members_net_worth_by_month(connections, mode=mode)
            )
            heading, yearly = ensemble_summary_view(monthly_summary, len(connections), failed_count)
            with out:
                display(Markdown(f"{heading} (batch {batch_dir.name})"))
                _fig, ax = plt.subplots(figsize=(10, 5))
                # Scale before drawing: matplotlib's set_yscale resets the
                # major formatter to its own log-scale default, which would
                # otherwise clobber ensemble_fan_chart's dollar formatting.
                if log_linear_picker.value == LogLinearMode.LOG:
                    ax.set_yscale("log")
                ensemble_fan_chart(monthly_summary, ax=ax)
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
