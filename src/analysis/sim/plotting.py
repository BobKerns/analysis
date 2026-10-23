"""Shared matplotlib helpers for sim notebooks (formatting, common chart shapes).

Kept deliberately thin: these are layout/styling conveniences, not analysis —
the data transforms live on :class:`~analysis.sim.connection.RunConnection`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pandas as pd
from matplotlib.axes import Axes
from matplotlib.dates import date2num
from matplotlib.ticker import FuncFormatter

from analysis.sim.types import Death

FRAME_ALPHA = 0.8
"""Alpha transparency for legend frames."""


def dollar_formatter() -> FuncFormatter:
    """A matplotlib tick formatter rendering values as ``$1,234``."""
    return FuncFormatter(lambda v, _: f"${v:,.0f}")


def include_zero(ax: Axes, *, margin: float = 0.05) -> None:
    """Policy: every chart's y-axis must include zero -- but zero stays
    exactly at whichever edge it naturally belongs to; only the *other* edge
    gets extra headroom.

    A dip that never actually crosses zero (or a rise that never approaches
    it) can otherwise look dramatic purely because matplotlib's autoscale
    zoomed in on the data's own min/max -- misreading true scale/magnitude.
    Naively reusing the existing ``ylim`` for the non-zero edge doesn't work:
    that margin was sized for the data's own (much narrower) range, so once
    zero stretches the axis, a low-volatility line can end up flush against
    the far spine and effectively disappear. Recompute headroom as a
    fraction of the *actual plotted data's* range instead.
    """
    data_min, data_max = ax.dataLim.ymin, ax.dataLim.ymax
    lo = min(0.0, data_min)
    hi = max(0.0, data_max)
    span = hi - lo
    pad = span * margin if span > 0 else max(abs(hi), abs(lo), 1.0) * margin
    if lo == 0.0 and hi > 0.0:
        hi += pad  # zero anchors the bottom; only the top needs headroom
    elif hi == 0.0 and lo < 0.0:
        lo -= pad  # zero anchors the top; only the bottom needs headroom
    else:
        lo -= pad
        hi += pad
    ax.set_ylim(lo, hi)


def mark_deaths(
    ax: Axes, deaths: Sequence[Death], *, years: Sequence[object] | None = None,
) -> None:
    """A dashed red vertical line at each death (#255), at the end of the
    final month; dashed because the date is an assumption, not a fact.
    On a bar chart of years, ``years`` are the bars' labels in order, and a
    death falls within its year's bar. A death outside the axis's range is
    left out, and the limits stay where the data put them. The lines stay out
    of the legend (Bob: a reminder of mortality in every legend is too much);
    the docs explain them once."""
    if not deaths:
        return
    xlim, ylim = ax.get_xlim(), ax.get_ylim()
    keys = [str(y) for y in years] if years is not None else []
    for death in deaths:
        if years is None:
            x = float(date2num(death.month))
        elif str(death.month.year) in keys:
            x = keys.index(str(death.month.year)) + (death.month.month - 6.5) / 12
        else:
            continue
        if not min(xlim) <= x <= max(xlim):
            continue
        ax.axvline(
            x, color="red", linewidth=1.2, zorder=2.6,
            linestyle="--", label="_nolegend_",
        )
    ax.set_xlim(xlim)
    ax.set_ylim(ylim)


def _new_axes() -> Axes:
    """Axes on a new pyplot figure, so a notebook shows it inline. pyplot is
    imported only here: importing it picks a GUI backend, which the CLI's
    charts (``ensemble_plots``, which takes ``FRAME_ALPHA`` from here) must
    not do."""
    import matplotlib.pyplot as plt

    _, ax = plt.subplots(figsize=(10, 5))
    return ax


def _finish(ax: Axes, title: str, ylabel: str) -> None:
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.yaxis.set_major_formatter(dollar_formatter())
    ax.grid(True, alpha=0.3)
    include_zero(ax)


def plot_timeseries(
    series: Mapping[str, pd.Series],
    *,
    title: str,
    ylabel: str = "USD",
    ax: Axes | None = None,
) -> Axes:
    """Line chart of one or more named series sharing a datetime index."""
    if ax is None:
        ax = _new_axes()
    for label, s in series.items():
        if s.notna().any():
            ax.plot(s.index, s, label=label)
    _finish(ax, title, ylabel)
    ax.legend(framealpha=FRAME_ALPHA)
    ax.figure.autofmt_xdate()
    return ax


def plot_stacked_bar(
    df: pd.DataFrame,
    columns: Sequence[str],
    colors: Sequence[str],
    *,
    title: str,
    xlabel: str = "year",
    ylabel: str = "USD",
    legend_title: str | None = None,
    ax: Axes | None = None,
) -> Axes:
    """Stacked bar chart of ``columns`` from a year-indexed DataFrame."""
    if ax is None:
        ax = _new_axes()
    df[list(columns)].plot(kind="bar", stacked=True, ax=ax, color=list(colors))
    _finish(ax, title, ylabel)
    ax.set_xlabel(xlabel)
    ax.grid(True, axis="y", alpha=0.3)
    ax.legend(title=legend_title, framealpha=FRAME_ALPHA)
    return ax


def plot_income_expense_net(net: pd.DataFrame, *, title: str, ax: Axes | None = None) -> Axes:
    """Income (positive bars), expenses (negative bars), and net (line) by year."""
    if ax is None:
        ax = _new_axes()
    years = net.index.astype(str)
    ax.bar(years, net["income"], label="income", color="#2ca02c", alpha=0.7)
    ax.bar(years, -net["expenses"], label="expenses", color="#d62728", alpha=0.7)
    ax.plot(years, net["net"], label="net", color="black", marker="o", linewidth=2)
    ax.axhline(0, color="grey", linewidth=0.8)
    _finish(ax, title, "USD")
    ax.set_xlabel("year")
    ax.grid(True, axis="y", alpha=0.3)
    ax.legend(framealpha=FRAME_ALPHA)
    ax.tick_params(axis="x", labelrotation=90)
    return ax
