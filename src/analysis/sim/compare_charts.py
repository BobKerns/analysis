"""The scenario comparison's chart (#188) as a plain matplotlib figure:
each scenario's values over time above each variant's difference from the
baseline. ``scenario_compare.ipynb`` shows it inline and ``sim compare
--chart`` in the terminal. No pyplot, as in ``ensemble_plots``: the CLI must
not pick a GUI backend."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import matplotlib
import pandas as pd
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter

from analysis.sim.plotting import FRAME_ALPHA, include_zero, mark_deaths
from analysis.sim.types import Death

__all__ = ["comparison_figure", "scenario_colors"]


def scenario_colors(scenarios: Sequence[str]) -> dict[str, str]:
    """A color per scenario, in matplotlib's default cycle order."""
    cycle = matplotlib.rcParams["axes.prop_cycle"].by_key()["color"]
    return {name: cycle[i % len(cycle)] for i, name in enumerate(scenarios)}


def comparison_figure(
    values: pd.DataFrame, baseline: str, variants: Sequence[str], title: str, *,
    ylabel: str = "net worth (USD)", colors: Mapping[str, str] | None = None,
    deaths: Sequence[Death] = (),
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
    ax_delta.set_xlabel("month")
    ax_delta.legend(framealpha=FRAME_ALPHA)
    for ax in (ax_values, ax_delta):
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v:,.0f}"))
        ax.grid(True, axis="y", alpha=0.3)
        include_zero(ax)
    mark_deaths(ax_values, deaths)
    mark_deaths(ax_delta, deaths)
    fig.tight_layout()
    return fig
