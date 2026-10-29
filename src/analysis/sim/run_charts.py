"""One run's charts (#327) as plain matplotlib figures: net worth, income,
expenses, gains, economic income, tax and an account's balance history.
``net_worth.ipynb`` and ``accounts.ipynb`` show them inline, and ``sim
networth``, ``sim report ...`` and ``sim account history`` draw them with
``--graph``. Each takes the frame its report prints, so a chart and its table
can't disagree. No pyplot, as in ``compare_charts``: a CLI chart must not pick
a GUI backend."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

import pandas as pd
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.ticker import PercentFormatter

from analysis.sim.plotting import FRAME_ALPHA, dollar_formatter, group_colors, include_zero, mark_deaths
from analysis.sim.reports import (
    CASH_FLOW, CASH_INCOME, ECONOMIC_INCOME, NET_WORTH_CHANGE, NET_WORTH_COLUMNS,
)
from analysis.sim.types import Death, ValuePlotMode

__all__ = [
    "CATEGORY_COLORS", "account_history_figure", "balance_history_figure", "draw_economic_income",
    "draw_flows", "economic_income_figure", "flow_groups", "flows_figure", "gains_figure",
    "income_figure", "net_worth_figure", "nothing_figure", "one_year_figure", "tax_figure",
    "tax_reconciliation_figure", "trim_to_last_nonzero",
]

CATEGORY_COLORS: Mapping[str, str] = {
    # income
    "salary": "#1f77b4", "soc security": "#2ca02c", "investment": "#ff7f0e",
    # expenses
    "housing": "#9467bd", "healthcare": "#d62728", "taxes": "#e377c2", "living": "#17becf",
    # both sides
    "business": "#8c564b", "other": "#7f7f7f",
}
"""A report category's color, the same wherever it is drawn. Accounts and tags
have no fixed color; they take matplotlib's cycle in the report's order."""

_FIGSIZE = (10, 5)


def _money_axis(ax: Axes, title: str, *, xlabel: str = "year", ylabel: str = "USD") -> None:
    ax.set_title(title)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.yaxis.set_major_formatter(dollar_formatter())
    ax.grid(True, axis="y", alpha=0.3)


def _group_colors(columns: Sequence[str]) -> list[str]:
    """Category colors for categories; for other groups, matplotlib's cycle
    (tab20 past its ten colors, so a long list of accounts doesn't repeat)."""
    if all(c in CATEGORY_COLORS for c in columns):
        return [CATEGORY_COLORS[c] for c in columns]
    return group_colors(len(columns))


def _nothing(fig: Figure, what: str) -> Figure:
    fig.text(0.5, 0.5, f"No {what} to chart.", ha="center", va="center")
    return fig


def nothing_figure(what: str) -> Figure:
    """A chart saying there's no ``what`` to chart: what --graph-file writes
    when a report has nothing to show, so the file it asked for exists."""
    return _nothing(Figure(figsize=_FIGSIZE), what)


# ---------- net worth ----------


def net_worth_figure(lines: pd.DataFrame, title: str, *, deaths: Sequence[Death] = ()) -> Figure:
    """One line per column of ``lines`` (month-indexed dollars): the first
    solid, the rest dashed, as references beside it (CPI-constant beside
    nominal, or pre-tax beside after-tax). Includes zero."""
    fig = Figure(figsize=_FIGSIZE)
    ax = fig.subplots()
    for position, (name, series) in enumerate(lines.items()):
        if series.notna().any():
            ax.plot(lines.index, series, label=str(name),
                    linestyle="-" if position == 0 else "--",
                    linewidth=2 if position == 0 else 1.2)
    _money_axis(ax, title, xlabel="month")
    include_zero(ax)
    mark_deaths(ax, deaths)
    ax.legend(framealpha=FRAME_ALPHA)
    fig.autofmt_xdate()
    fig.tight_layout()
    return fig


# ---------- income and expenses ----------

_NOT_GROUPS = frozenset({"total", ECONOMIC_INCOME, CASH_INCOME, CASH_FLOW, *NET_WORTH_COLUMNS})


def flow_groups(frame: pd.DataFrame) -> list[str]:
    """The group columns of a ``reports.flows_by_year`` frame: categories,
    accounts or tags, not its totals or the columns that follow them."""
    return [str(c) for c in frame.columns if c not in _NOT_GROUPS]


def draw_flows(ax: Axes, frame: pd.DataFrame, title: str, *, legend_title: str = "category",
               deaths: Sequence[Death] = ()) -> None:
    """``frame``'s groups (``flow_groups``) as stacked bars by year; a
    negative group (a year's market drop) stacks below zero."""
    groups = flow_groups(frame)
    frame[groups].plot(kind="bar", stacked=True, ax=ax, color=_group_colors(groups),
                       width=0.8)
    ax.axhline(0, color="grey", linewidth=0.8)
    _money_axis(ax, title)
    include_zero(ax)
    mark_deaths(ax, deaths, years=list(frame.index))
    ax.legend(title=legend_title, framealpha=FRAME_ALPHA, fontsize="small")
    ax.tick_params(axis="x", labelrotation=90)


def flows_figure(frame: pd.DataFrame, title: str, *, legend_title: str = "category",
                 deaths: Sequence[Death] = ()) -> Figure:
    """Income or expenses by year (``reports.flows_by_year``), stacked by group."""
    fig = Figure(figsize=_FIGSIZE)
    if frame.empty or not flow_groups(frame):
        return _nothing(fig, "flows")
    draw_flows(fig.subplots(), frame, title, legend_title=legend_title, deaths=deaths)
    fig.tight_layout()
    return fig


def one_year_figure(frame: pd.DataFrame, title: str) -> Figure:
    """One year of income or expenses (``reports.flows_for_year``): a
    horizontal bar per group, largest at the top, its share at its end."""
    body = frame.drop(index="total", errors="ignore")
    body = body[body["amount"] != 0].sort_values("amount")
    fig = Figure(figsize=(10, max(2.5, 0.45 * len(body) + 1.2)))
    if body.empty:
        return _nothing(fig, "flows")
    ax = fig.subplots()
    names = [str(n) for n in body.index]
    ax.barh(names, body["amount"], color=_group_colors(names))
    for y, (amount, share) in enumerate(zip(body["amount"], body["share"], strict=True)):
        if pd.notna(share):
            ax.annotate(f" {share:.0%}", (amount, y), va="center",
                        ha="left" if amount >= 0 else "right", fontsize="small")
    ax.axvline(0, color="grey", linewidth=0.8)
    ax.set_title(title)
    ax.xaxis.set_major_formatter(dollar_formatter())
    ax.grid(True, axis="x", alpha=0.3)
    ax.margins(x=0.12)
    fig.tight_layout()
    return fig


def draw_economic_income(ax: Axes, story: pd.DataFrame, title: str, *,
                         deaths: Sequence[Death] = ()) -> None:
    """Economic income carried down to the change in net worth (#249), from
    the income report's columns: what was kept (after-tax income, or economic
    income before tax) as bars above zero, spending and inflation stacked
    below, with tax and the change in net worth as lines."""
    taxed = "after_tax_income" in story.columns
    kept = "after_tax_income" if taxed else ECONOMIC_INCOME
    tax = "tax" if taxed else "tax_paid"
    years = story.index.astype(str)
    ax.bar(years, story[kept], label=kept.replace("_", " "), color="#2ca02c", alpha=0.7)
    bottom = pd.Series(0.0, index=story.index)
    for column, color in (("spending", "#d62728"), ("inflation", "#ff7f0e")):
        if column in story.columns:
            ax.bar(years, -story[column], bottom=bottom, label=column, color=color, alpha=0.7)
            bottom = bottom - story[column]
    if tax in story.columns:
        ax.plot(years, story[tax], label=tax.replace("_", " "),
                color="#9467bd", marker="o", linewidth=2)
    ax.plot(years, story[NET_WORTH_CHANGE], label="Δnet worth",
            color="black", marker="o", linewidth=2)
    ax.axhline(0, color="grey", linewidth=0.8)
    _money_axis(ax, title)
    mark_deaths(ax, deaths, years=list(story.index))
    ax.legend(framealpha=FRAME_ALPHA, fontsize="small")
    ax.tick_params(axis="x", labelrotation=90)


def _story(frame: pd.DataFrame) -> pd.DataFrame:
    """The years of an income report that follow economic income to the
    change in net worth: empty when it has none (tagged only, or no accounts)."""
    if NET_WORTH_CHANGE not in frame.columns:
        return frame.iloc[0:0]
    return frame.dropna(subset=[NET_WORTH_CHANGE])


def economic_income_figure(frame: pd.DataFrame, title: str, *,
                           deaths: Sequence[Death] = ()) -> Figure:
    """``draw_economic_income`` on its own figure."""
    fig = Figure(figsize=_FIGSIZE)
    story = _story(frame)
    if story.empty:
        return _nothing(fig, "net worth")
    draw_economic_income(fig.subplots(), story, title, deaths=deaths)
    fig.tight_layout()
    return fig


def income_figure(frame: pd.DataFrame, title: str, *, legend_title: str = "category",
                  deaths: Sequence[Death] = ()) -> Figure:
    """``sim report income --graph``: the income groups stacked by year, over
    the economic-income story when the report tells it (not when it shows
    only tagged flows)."""
    story = _story(frame)
    if story.empty:
        return flows_figure(frame, title, legend_title=legend_title, deaths=deaths)
    fig = Figure(figsize=(11, 9))
    top, bottom = fig.subplots(2, 1, sharex=True)
    # Every year the table prints, in both panels: a year without a change in
    # net worth (one before the run's first step) has its bars on top and
    # nothing below, rather than going missing from the chart.
    draw_flows(top, frame, title, legend_title=legend_title, deaths=deaths)
    story = story.reindex(frame.index)
    top.set_xlabel("")
    draw_economic_income(bottom, story, "Economic income to the change in net worth",
                         deaths=deaths)
    fig.tight_layout()
    return fig


# ---------- gains ----------

_GAIN_COLORS = {"taxable": "#2ca02c", "non-taxable": "#1f77b4", "unrealized": "#7f7f7f"}


def gains_figure(frame: pd.DataFrame, title: str, *, deaths: Sequence[Death] = ()) -> Figure:
    """Realized (taxable, non-taxable) and unrealized gains stacked by year
    (``reports.gains_by_year``), with that year's expenses as a line."""
    fig = Figure(figsize=_FIGSIZE)
    columns = [c for c in _GAIN_COLORS if c in frame.columns]
    if frame.empty or not columns:
        return _nothing(fig, "gains")
    ax = fig.subplots()
    frame[columns].plot(kind="bar", stacked=True, ax=ax,
                        color=[_GAIN_COLORS[c] for c in columns])
    if "expenses" in frame.columns:
        ax.plot(frame.index.astype(str), frame["expenses"], label="expenses",
                color="#d62728", marker="o", linewidth=2)
    ax.axhline(0, color="grey", linewidth=0.8)
    _money_axis(ax, title)
    include_zero(ax)
    mark_deaths(ax, deaths, years=list(frame.index))
    ax.legend(title="category", framealpha=FRAME_ALPHA)
    ax.tick_params(axis="x", labelrotation=90)
    fig.tight_layout()
    return fig


# ---------- tax ----------


def tax_figure(frame: pd.DataFrame, title: str, *, deaths: Sequence[Death] = ()) -> Figure:
    """Federal and state tax stacked by year (``reports.tax_by_year``), with
    the effective rate (total tax over federal AGI) on the right-hand axis."""
    fig = Figure(figsize=_FIGSIZE)
    columns = [c for c in ("fed_tax", "state_tax") if c in frame.columns]
    if frame.empty or not columns:
        return _nothing(fig, "tax")
    ax = fig.subplots()
    labels = {"fed_tax": "federal", "state_tax": "state"}
    frame[columns].fillna(0).rename(columns=labels).plot(
        kind="bar", stacked=True, ax=ax, color=["#9467bd", "#e377c2"][:len(columns)])
    ax.axhline(0, color="grey", linewidth=0.8)
    _money_axis(ax, title)
    include_zero(ax)
    handles, names = ax.get_legend_handles_labels()
    if "effective_rate" in frame.columns and frame["effective_rate"].notna().any():
        rate = ax.twinx()
        (line,) = rate.plot(range(len(frame)), frame["effective_rate"], color="black",
                            marker="o", linewidth=2, label="effective rate")
        rate.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
        # From zero, unless refundable credits made a year's rate negative.
        rates = frame["effective_rate"]
        rate.set_ylim(bottom=min(0.0, float(rates.min()) * 1.15),
                      top=max(float(rates.max()) * 1.15, 0.01))
        rate.set_ylabel("effective rate")
        handles, names = [*handles, line], [*names, "effective rate"]
    mark_deaths(ax, deaths, years=list(frame.index))
    ax.legend(handles, names, framealpha=FRAME_ALPHA, loc="upper right")
    ax.tick_params(axis="x", labelrotation=90)
    fig.tight_layout()
    return fig


def tax_reconciliation_figure(frame: pd.DataFrame, title: str, *,
                              deaths: Sequence[Death] = ()) -> Figure:
    """Each tax year's April settlement (``reports.tax_reconciliation``):
    the balance due above zero and the refund below, federal and state
    stacked, with the year's total liability as a line."""
    fig = Figure(figsize=_FIGSIZE)
    if frame.empty:
        return _nothing(fig, "tax")
    ax = fig.subplots()
    years = frame.index.astype(str)
    above = pd.Series(0.0, index=frame.index)
    below = pd.Series(0.0, index=frame.index)
    for scope, label, color in (("fed", "federal", "#9467bd"), ("state", "state", "#e377c2")):
        due, refund = f"{scope}_due", f"{scope}_refund"
        if due in frame.columns and frame[due].ne(0).any():
            ax.bar(years, frame[due], bottom=above, color=color, label=f"{label} due")
            above = above + frame[due]
        if refund in frame.columns and frame[refund].ne(0).any():
            ax.bar(years, -frame[refund], bottom=below, color=color, alpha=0.5,
                   hatch="//", label=f"{label} refund")
            below = below - frame[refund]
    liability = frame[[c for c in ("fed_liability", "state_liability") if c in frame.columns]]
    if not liability.empty:
        ax.plot(years, liability.sum(axis=1), color="black", marker="o", linewidth=1.5,
                label="liability")
    ax.axhline(0, color="grey", linewidth=0.8)
    _money_axis(ax, title)
    mark_deaths(ax, deaths, years=list(frame.index))
    ax.legend(framealpha=FRAME_ALPHA, fontsize="small")
    ax.tick_params(axis="x", labelrotation=90)
    fig.tight_layout()
    return fig


# ---------- account balances ----------


def trim_to_last_nonzero(
    balance_pivot: pd.DataFrame, selected: list[str]
) -> pd.DataFrame:
    """Trim trailing months where every selected account is zero (or missing).

    Keeps one month past the last non-zero balance -- the month it actually
    settles to its final zero -- instead of stretching the x-axis out to the
    scenario's full end for accounts that closed out early.
    """
    nonzero = balance_pivot[selected].fillna(0).ne(0).any(axis=1)
    if not nonzero.any():
        return balance_pivot
    last_nonzero_pos = nonzero.to_numpy().nonzero()[0].max()
    end_pos = min(last_nonzero_pos + 1, len(balance_pivot.index) - 1)
    return balance_pivot.iloc[: end_pos + 1]


def balance_history_figure(
    balance_pivot: pd.DataFrame,
    selected: list[str],
    title: str,
    *,
    zero_anchor: bool = True,
    value_mode: ValuePlotMode = ValuePlotMode.TOTAL,
    clip: Callable[[pd.DataFrame], pd.DataFrame] | None = None,
    deaths: Sequence[Death] = (),
) -> Figure | None:
    """The selected accounts' (or lots') balances over time, with their
    total when there are several; None when nothing is selected.

    ``zero_anchor`` applies the include-zero policy by default; turn it off
    for a series whose true variation is tiny relative to its magnitude (e.g.
    a home's value in CPI-constant terms) -- zero-anchoring would otherwise
    flatten it to an invisible line hugging the top of the axis.

    ``value_mode``: ``ValuePlotMode.TOTAL`` (default) plots the raw balance;
    ``ValuePlotMode.MONTHLY`` plots the change from the previous month -- the
    first month has no prior value to diff against, so the visible line
    effectively starts one month later.

    ``clip`` cuts the rows to plot (``TimeRangePicker.clip``), after the
    monthly change is taken, so a window's first month keeps its change.

    ``deaths`` are red lines (#255).
    """
    if not selected:
        return None

    balance_pivot = trim_to_last_nonzero(balance_pivot, selected)[selected].copy()
    if len(selected) > 1:
        balance_pivot["Total"] = balance_pivot.sum(axis=1)
    ylabel = "USD"
    if value_mode == ValuePlotMode.MONTHLY:
        balance_pivot = balance_pivot.diff()
        ylabel = "USD / month"
    if clip is not None:
        balance_pivot = clip(balance_pivot)
    fig = Figure(figsize=_FIGSIZE)
    ax = fig.subplots()
    for name in balance_pivot.columns:
        ax.plot(balance_pivot.index, balance_pivot[name], label=name)
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.yaxis.set_major_formatter(dollar_formatter())
    ax.grid(True, alpha=0.3)
    mark_deaths(ax, deaths)
    ax.legend(loc="best", fontsize="small", framealpha=0.5)
    if zero_anchor:
        include_zero(ax)
    fig.autofmt_xdate()
    return fig


def account_history_figure(frame: pd.DataFrame, title: str, *,
                           deaths: Sequence[Death] = ()) -> Figure:
    """``sim account history --graph``: the balance (``reports.account_history``,
    by month or by year), and a lot's cost basis beside it when the run
    tracks one. A year is drawn at its Dec 31."""
    fig = Figure(figsize=_FIGSIZE)
    if frame.empty:
        return _nothing(fig, "balances")
    if frame.index.name == "year":
        when = pd.to_datetime([f"{y}-12-31" for y in frame.index])
    else:
        when = pd.to_datetime(frame.index.astype(str)) + pd.offsets.MonthEnd(0)
    ax = fig.subplots()
    ax.plot(when, frame["balance"], label="balance", linewidth=2,
            marker="o" if frame.index.name == "year" else None)
    if "cost_basis" in frame.columns and frame["cost_basis"].notna().any():
        ax.plot(when, frame["cost_basis"], label="cost basis", linestyle="--", linewidth=1.2)
    _money_axis(ax, title, xlabel=str(frame.index.name))
    ax.grid(True, alpha=0.3)
    include_zero(ax)
    mark_deaths(ax, deaths)
    ax.legend(framealpha=FRAME_ALPHA)
    fig.autofmt_xdate()
    fig.tight_layout()
    return fig
