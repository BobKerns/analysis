"""The portfolio's asset allocation (#327) as plain matplotlib figures: a pie
of the asset classes, a bar per class split by a grouping, and the drift from
a target set by year. ``accounts.ipynb`` shows the first two inline, and
``sim allocation --graph`` (with ``--by``, or ``--drift``) draws them. No
pyplot, as in ``compare_charts``: a CLI chart must not pick a GUI backend."""

from __future__ import annotations

import pandas as pd
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter

from analysis.sim.plotting import FRAME_ALPHA, dollar_formatter, group_colors

MAX_ABS = "max_abs"
"""``drift_by_year``'s column of the largest drift of any class, not a class itself."""

__all__ = ["allocation_drift_figure", "allocation_figure", "allocation_pie_figure",
           "allocation_split_figure"]


def _body(table: pd.DataFrame) -> pd.DataFrame:
    """The asset classes of a ``portfolio_allocation`` table, without its Total row."""
    return table.drop(index="Total", errors="ignore")


def allocation_pie_figure(table: pd.DataFrame, title: str = "") -> Figure:
    """Each asset class's share of the portfolio (``portfolio_allocation``
    without ``by``), largest first, clockwise from the top. Classes with no
    balance are left out."""
    body = _body(table)
    body = body[body["percent"] > 0].sort_values("percent", ascending=False)
    fig = Figure(figsize=(7, 5.5))
    if body.empty:
        fig.text(0.5, 0.5, "No holdings to chart.", ha="center", va="center")
        return fig
    ax = fig.subplots()
    ax.pie(body["percent"], labels=[str(n) for n in body.index], autopct="%1.0f%%",
           startangle=90, counterclock=False)
    ax.axis("equal")
    if title:
        ax.set_title(title, fontsize="medium")
    fig.tight_layout()
    return fig


def allocation_split_figure(table: pd.DataFrame, title: str = "") -> Figure:
    """A horizontal bar per asset class, split by the table's grouping
    (``portfolio_allocation`` with ``by``: a balance column per group), in
    the table's order from the top, each labelled with its share."""
    body = _body(table)
    groups = [c for c in body.columns if c not in ("Total", "percent")]
    body = body[body["Total"] != 0].iloc[::-1]
    fig = Figure(figsize=(12, max(2.5, 0.5 * len(body) + 1.5)))
    if body.empty:
        fig.text(0.5, 0.5, "No holdings to chart.", ha="center", va="center")
        return fig
    ax = fig.subplots()
    colors = group_colors(len(groups))
    names = [str(n) for n in body.index]
    left = pd.Series(0.0, index=body.index)
    for i, group in enumerate(groups):
        ax.barh(names, body[group], left=left, label=str(group), color=colors[i])
        left = left + body[group]
    for y, (total, percent) in enumerate(zip(body["Total"], body["percent"], strict=True)):
        ax.annotate(f" {percent:.0f}%", (total, y), va="center", fontsize="small")
    ax.xaxis.set_major_formatter(dollar_formatter())
    ax.grid(True, axis="x", alpha=0.3)
    ax.margins(x=0.1)
    # Beside the bars: a long account name over them would hide one.
    ax.legend(framealpha=FRAME_ALPHA, fontsize="small", loc="upper left", bbox_to_anchor=(1.01, 1))
    if title:
        ax.set_title(title)
    fig.tight_layout()
    return fig


def allocation_figure(table: pd.DataFrame, title: str = "") -> Figure:
    """The pie, or with a grouping the split bars: whichever the table has."""
    if "Total" in table.columns:
        return allocation_split_figure(table, title)
    return allocation_pie_figure(table, title)


def allocation_drift_figure(frame: pd.DataFrame, title: str = "") -> Figure:
    """A line per asset class (``strategy_guide.drift_by_year``: rows the
    run's year-ends, values percentage points off target), zero marked, and
    the largest drift of any class (its ``max_abs`` column) dotted grey."""
    fig = Figure(figsize=(10, 5))
    if frame.empty:
        fig.text(0.5, 0.5, "No months in the run.", ha="center", va="center")
        return fig
    ax = fig.subplots()
    when = pd.to_datetime(frame.index.astype(str))
    for column in frame.columns:
        if column == MAX_ABS:
            continue
        ax.plot(when, frame[column], label=str(column), marker="o", markersize=3)
    if MAX_ABS in frame.columns:
        ax.plot(when, frame[MAX_ABS], label="largest", color="grey", linestyle=":", linewidth=1.2)
    ax.axhline(0, color="grey", linewidth=0.8)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:+.0f} pp"))
    ax.set_ylabel("share less target")
    ax.set_xlabel("year end")
    ax.grid(True, axis="y", alpha=0.3)
    ax.legend(framealpha=FRAME_ALPHA, fontsize="small")
    if title:
        ax.set_title(title)
    fig.autofmt_xdate()
    fig.tight_layout()
    return fig
