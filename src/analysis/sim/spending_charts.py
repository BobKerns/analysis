"""Spending-against-plan charts (#408), widget-free so the notebooks and the
CLI draw the same figures: one run's spending by year against its plan, runs
side by side, and an ensemble's spread of spending.

Years sit at whole positions on the x axis, labelled by year, so a death
falls inside its year as on the other yearly charts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd
from matplotlib.figure import Figure

from analysis.sim.plotting import dollar_formatter, include_zero, mark_deaths
from analysis.sim.types import Death

__all__ = ["ensemble_spending_figure", "spending_compare_figure", "spending_figure"]

_DELIVERED = "#2a78d6"


def _plan(ax, x, values, label: str, **extra) -> None:
    ax.plot(x, values, color="black", linewidth=1.4, linestyle="--", label=label, **extra)


def _clip(frame: pd.DataFrame, years: tuple[int, int] | None) -> pd.DataFrame:
    if years is None or frame.empty:
        return frame
    year = np.array([int(y) for y in frame.index])
    return frame[(year >= years[0]) & (year <= years[1])]


def _finish(ax, title: str, labels: Sequence[object], ylabel: str, deaths: Sequence[Death]) -> None:
    ax.set_xticks(range(len(labels)), [str(y) for y in labels],
                  rotation=45 if len(labels) > 12 else 0)
    ax.set_ylabel(ylabel)
    ax.yaxis.set_major_formatter(dollar_formatter())
    ax.grid(True, axis="y", alpha=0.3)
    include_zero(ax)
    mark_deaths(ax, deaths, years=labels)
    ax.set_title(title)
    ax.legend(loc="best", fontsize="small")


def _empty(fig: Figure, ax) -> Figure:
    ax.text(0.5, 0.5, "No spending to show.", ha="center", transform=ax.transAxes)
    return fig


def spending_figure(
    frame: pd.DataFrame, title: str, *, ylabel: str = "dollars",
    years: tuple[int, int] | None = None, deaths: Sequence[Death] = (),
) -> Figure:
    """One run (``reports.spending_report``'s frame): delivered spending as
    a bar per year, the plan (budgeted) as a dashed line over them."""
    fig = Figure(figsize=(11, 5), layout="constrained")
    ax = fig.add_subplot()
    shown = _clip(frame, years)
    if shown.empty:
        return _empty(fig, ax)
    x = np.arange(len(shown))
    ax.bar(x, shown["delivered"], color=_DELIVERED, alpha=0.75, label="delivered")
    _plan(ax, x, shown["budgeted"], "plan", marker="o", markersize=3)
    _finish(ax, title, list(shown.index), ylabel, deaths)
    return fig


def spending_compare_figure(
    frames: Mapping[str, pd.DataFrame], title: str, *, ylabel: str = "dollars",
    years: tuple[int, int] | None = None, deaths: Sequence[Death] = (),
) -> Figure:
    """Runs side by side: each run's delivered spending as a line, and its
    plan dashed in the same colour where a strategy moved it off the plan."""
    fig = Figure(figsize=(11, 5), layout="constrained")
    ax = fig.add_subplot()
    clipped = {name: _clip(frame, years) for name, frame in frames.items()}
    labels = sorted({int(y) for frame in clipped.values() for y in frame.index})
    if not labels:
        return _empty(fig, ax)
    at = {year: i for i, year in enumerate(labels)}
    for name, frame in clipped.items():
        if frame.empty:
            continue
        x = [at[int(y)] for y in frame.index]
        (line,) = ax.plot(x, frame["delivered"], linewidth=1.8, label=name)
        if (frame["delivered"] - frame["budgeted"]).abs().max() >= 0.5:
            ax.plot(x, frame["budgeted"], color=line.get_color(), linewidth=1.0, linestyle="--",
                    label=f"{name} plan")
    _finish(ax, title, labels, ylabel, deaths)
    return fig


def ensemble_spending_figure(
    delivered: pd.DataFrame, budgeted: pd.DataFrame, title: str, *, ylabel: str = "dollars",
    quantiles: Sequence[float] = (0.05, 0.25, 0.5, 0.75, 0.95),
    years: tuple[int, int] | None = None, deaths: Sequence[Death] = (),
) -> Figure:
    """An ensemble (``EnsembleSeries.spending``: a column per member, by
    year): delivered spending's median and shaded quantile bands, the median
    plan dashed."""
    fig = Figure(figsize=(11, 5), layout="constrained")
    ax = fig.add_subplot()
    shown = _clip(delivered, years)
    if shown.empty:
        return _empty(fig, ax)
    plan = _clip(budgeted, years).reindex(shown.index)
    x = np.arange(len(shown))
    qs = sorted(quantiles)
    values = shown.quantile(qs, axis=1).T
    for i in range(len(qs) // 2):
        low, high = qs[i], qs[-1 - i]
        ax.fill_between(x, values[low], values[high], color=_DELIVERED, alpha=0.15 + 0.12 * i,
                        linewidth=0, label=f"delivered {low:.0%}–{high:.0%}")
    if len(qs) % 2:
        mid = qs[len(qs) // 2]
        ax.plot(x, values[mid], color=_DELIVERED, linewidth=1.8, label=f"delivered {mid:.0%}")
    _plan(ax, x, plan.median(axis=1), "plan (median)")
    _finish(ax, f"{title} ({shown.shape[1]} members)", list(shown.index), ylabel, deaths)
    return fig
