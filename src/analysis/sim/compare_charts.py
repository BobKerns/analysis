"""The scenario comparison's charts as plain matplotlib figures: each
scenario's values over time above each variant's difference from the
baseline (#188), gross Social Security by scenario (#335) and total tax by
year (#336).
``scenario_compare.ipynb`` shows them inline and ``sim compare --chart`` in
the terminal. No pyplot, as in ``ensemble_plots``: the CLI must
not pick a GUI backend."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import matplotlib
import pandas as pd
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter

from analysis.sim.plotting import FRAME_ALPHA, include_zero, mark_deaths
from analysis.sim.types import Death

__all__ = ["comparison_figure", "scenario_colors", "ss_figure", "tax_figure"]


def scenario_colors(scenarios: Sequence[str]) -> dict[str, str]:
    """A color per scenario, in matplotlib's default cycle order."""
    cycle = matplotlib.rcParams["axes.prop_cycle"].by_key()["color"]
    return {name: cycle[i % len(cycle)] for i, name in enumerate(scenarios)}


def comparison_figure(
    values: pd.DataFrame, baseline: str, variants: Sequence[str], title: str, *,
    ylabel: str = "net worth (USD)", colors: Mapping[str, str] | None = None,
    deaths: Sequence[Death] = (), xlabel: str = "month",
) -> Figure:
    """Two panels sharing the month axis: ``values`` (one column per
    scenario) with the baseline solid and the variants dashed, then each
    variant minus the baseline. Both include zero, so a small difference
    doesn't look dramatic. Each of ``deaths`` is a red line across both
    (``compare.scenario_deaths``)."""
    colors = colors or scenario_colors([baseline, *variants])
    fig = Figure(figsize=(11, 9))
    ax_values, ax_delta = fig.subplots(2, 1, sharex=True, height_ratios=[2, 1])
    for name in (baseline, *variants):
        ax_values.plot(
            values.index, values[name], label=name, color=colors[name],
            linestyle="-" if name == baseline else "--",
            linewidth=2 if name == baseline else 1.5,
        )
    ax_values.set_ylabel(ylabel)
    ax_values.set_title(title)
    ax_values.legend(framealpha=FRAME_ALPHA)
    for name in variants:
        ax_delta.plot(values.index, values[name] - values[baseline], label=name,
                      color=colors[name])
    ax_delta.axhline(0, color="grey", linewidth=0.8)
    ax_delta.set_ylabel(f"difference vs {baseline}")
    ax_delta.set_xlabel(xlabel)
    ax_delta.legend(framealpha=FRAME_ALPHA)
    for ax in (ax_values, ax_delta):
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v:,.0f}"))
        ax.grid(True, axis="y", alpha=0.3)
        include_zero(ax)
    mark_deaths(ax_values, deaths)
    mark_deaths(ax_delta, deaths)
    fig.tight_layout()
    return fig


def ss_figure(
    ss: pd.DataFrame, cumulative: pd.DataFrame, baseline: str, variants: Sequence[str], title: str, *,
    colors: Mapping[str, str] | None = None, deaths: Sequence[Death] = (),
) -> Figure:
    """Gross Social Security (#335) in three panels sharing the month axis:
    each scenario's monthly benefit, its ``cumulative`` total, and each
    variant's cumulative total less the baseline's. The last is where a
    later claim age catches up with an earlier one, if it does. ``ss`` is
    ``compare.ss_gross_by_month``; ``cumulative`` its running total, which
    may be clipped to fewer months than the run (it still counts from the
    start). Deaths are red lines (#255)."""
    colors = colors or scenario_colors([baseline, *variants])
    fig = Figure(figsize=(11, 10))
    ax_month, ax_cum, ax_delta = fig.subplots(3, 1, sharex=True, height_ratios=[1, 1.2, 1])
    for name in (baseline, *variants):
        style = {"label": name, "color": colors[name],
                 "linestyle": "-" if name == baseline else "--",
                 "linewidth": 2 if name == baseline else 1.5}
        ax_month.plot(ss.index, ss[name], **style)
        ax_cum.plot(cumulative.index, cumulative[name], **style)
    for name in variants:
        ax_delta.plot(cumulative.index, cumulative[name] - cumulative[baseline], label=name,
                      color=colors[name])
    ax_delta.axhline(0, color="grey", linewidth=0.8)
    ax_month.set_title(title)
    ax_month.set_ylabel("per month")
    ax_cum.set_ylabel("since the start")
    ax_delta.set_ylabel(f"difference vs {baseline}")
    ax_delta.set_xlabel("month")
    for ax in (ax_month, ax_cum, ax_delta):
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v:,.0f}"))
        ax.grid(True, axis="y", alpha=0.3)
        ax.legend(framealpha=FRAME_ALPHA)
        include_zero(ax)
        mark_deaths(ax, deaths)
    fig.tight_layout()
    return fig


def tax_figure(
    comparison: pd.DataFrame, baseline: str, variants: Sequence[str], title: str, *,
    colors: Mapping[str, str] | None = None, deaths: Sequence[Death] = (),
) -> Figure:
    """Total tax by tax year (#336) as ``comparison_figure`` draws net worth:
    each scenario's tax, then each variant's less the baseline's.
    ``comparison`` is ``compare.tax_comparison``; each year is drawn at its
    middle, so it sits between its own year ticks and deaths (#255) fall
    in the year they happen."""
    values = comparison[[baseline, *variants]].copy()
    values.index = pd.to_datetime([f"{year}-07-01" for year in values.index])
    return comparison_figure(values, baseline, variants, title, ylabel="total tax (USD)",
                             colors=colors, deaths=deaths, xlabel="tax year")
