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
from analysis.sim.ensemble import load_ensemble, members_net_worth_by_month
from analysis.sim.ensemble import summary as ensemble_summary
from analysis.sim.plotting import FRAME_ALPHA

__all__ = [
    "list_ensemble_recipes", "ensemble_summary_view", "ensemble_fan_chart",
    "ensemble_widget",
]


def list_ensemble_recipes(runs_dir: Path) -> list[str]:
    """Names of every subdirectory of ``runs_dir`` holding at least one
    ``member_*.sqlite3`` run file -- the recipes ``sim run --ensemble-size``
    has generated and run."""
    runs_dir = Path(runs_dir)
    if not runs_dir.is_dir():
        return []
    return sorted(
        p.name for p in runs_dir.iterdir()
        if p.is_dir() and any(p.glob("member_*.sqlite3"))
    )


def ensemble_summary_view(monthly_summary: pd.DataFrame, member_count: int) -> tuple[str, pd.DataFrame]:
    """Heading and year-end mean/median/SD/range table from a month-indexed
    ``summary()`` frame (mean/median/std/min/max columns)."""
    yearly = year_end_values(monthly_summary)
    yearly.index.name = "year"
    heading = f"**{member_count} member(s)**"
    return heading, yearly


def ensemble_fan_chart(monthly_summary: pd.DataFrame, ax: Any = None) -> Any:
    """Shade the member range (min-max) behind mean/median lines, month by
    month -- the same three columns ``sim ensemble summary`` prints."""
    if ax is None:
        _fig, ax = plt.subplots(figsize=(10, 5))
    ax.fill_between(
        monthly_summary.index, monthly_summary["min"], monthly_summary["max"],
        color="steelblue", alpha=0.2, label="range",
    )
    ax.plot(monthly_summary.index, monthly_summary["mean"], color="steelblue",
            linewidth=2, label="mean")
    ax.plot(monthly_summary.index, monthly_summary["median"], color="steelblue",
            linewidth=1, linestyle="--", label="median")
    ax.set_ylim(bottom=0)
    ax.set_xlabel("month")
    ax.set_ylabel("USD")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:,.0f}"))
    ax.grid(True, axis="y", alpha=0.3)
    ax.legend(framealpha=FRAME_ALPHA)
    return ax


def ensemble_widget(runs_dir: Path) -> widgets.VBox:
    """Recipe dropdown + fan chart + year-end summary table for an ensemble
    already generated and run via ``sim run <recipe> --ensemble-size N``."""
    runs_dir = Path(runs_dir)
    recipes = list_ensemble_recipes(runs_dir)
    dropdown = widgets.Dropdown(options=recipes, description="Recipe:")
    out = widgets.Output()

    def _redraw(*_: object) -> None:
        out.clear_output(wait=True)
        if not dropdown.value:
            return
        connections = load_ensemble(runs_dir / dropdown.value)
        try:
            monthly_summary = ensemble_summary(members_net_worth_by_month(connections))
            heading, yearly = ensemble_summary_view(monthly_summary, len(connections))
            with out:
                display(Markdown(heading))
                _fig, ax = plt.subplots(figsize=(10, 5))
                ensemble_fan_chart(monthly_summary, ax=ax)
                ax.set_title(f"Net worth fan chart ({dropdown.value})")
                plt.xticks(rotation=90)
                plt.show()
                display(yearly.style.format({c: "${:,.0f}" for c in yearly.columns}))
        finally:
            for con in connections.values():
                con.close()

    dropdown.observe(_redraw, names="value")
    _redraw()
    return widgets.VBox([dropdown, out])
