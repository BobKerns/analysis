"""Guard rails (#305) as plain matplotlib figures. On one run: per measure,
the yearly measure against each rail's threshold above, each rail's
cumulative margin below (``net_worth.ipynb``). Across scenarios: each
rail's annual and cumulative margins, a line per scenario
(``scenario_compare.ipynb``). On an ensemble: each rail's
share of members past it and their RMS overshoot, annual and cumulative
(``ensemble.ipynb``, ``sim ensemble guard-rails --graph``). No pyplot, as in
``compare_charts``: a CLI chart must not pick a GUI backend."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter, PercentFormatter

from analysis.sim.guard_rails import EnsembleRailResult, RailResult, ensemble_rail_frame, rail_frame
from analysis.sim.plotting import FRAME_ALPHA, include_zero, mark_deaths
from analysis.sim.types import Death

__all__ = ["CARRIED_CAVEAT", "ENSEMBLE_CAVEAT", "ensemble_guard_rail_figure",
           "guard_rail_comparison_figure", "guard_rail_figure", "rail_colors"]

# A rail's color follows its sense: warm for a rail to stay inside, cool for
# a positive one, each in a fixed order (checked for color-blind separation;
# dashed lines and hatching carry the sense too).
_NEGATIVE_COLORS = ("#eb6834", "#e34948", "#eda100", "#e87ba4")
_POSITIVE_COLORS = ("#2a78d6", "#1baf7a", "#6250d6", "#008300")
_MEASURE_COLOR = "#0b0b0b"

CARRIED_CAVEAT = (
    "The cumulative margin adds up each year's room or overdraft. Money spent "
    "past a rail would have kept growing had it stayed invested, so an "
    "overdraft costs more than its sum: the dotted line carries the margin at "
    "the portfolio's own return. Both are guides, not a re-run: spending "
    "differently would have changed the path itself."
)


ENSEMBLE_CAVEAT = (
    "Past is the share of members past a rail that year; ever past, the share past it in "
    "some year so far (dotted). RMS overshoot is the root mean square of how far past the "
    "members are, those inside counting as zero, so it mixes how often with how far: read "
    "it beside the share. The cumulative form sums each member's margins, so it ignores "
    "what an early overdraft would have earned; it is a guide, not a re-run."
)


def rail_colors(results: Sequence[RailResult | EnsembleRailResult]) -> dict[str, str]:
    """Each rail's color, by its place among the rails of its sense."""
    colors: dict[str, str] = {}
    counts = {"negative": 0, "positive": 0}
    for r in results:
        palette = _POSITIVE_COLORS if r.rail.positive else _NEGATIVE_COLORS
        colors[r.rail.name] = palette[counts[r.rail.sense] % len(palette)]
        counts[r.rail.sense] += 1
    return colors


def _measure_key(r: RailResult) -> tuple[str, str, str]:
    """Rails with the same key measure the same thing year by year."""
    portfolio = str(r.rail.portfolio) if r.rail.measure == "withdrawals" else ""
    return r.rail.measure, str(r.rail.select), portfolio


def _measure_label(key: tuple[str, str, str]) -> str:
    measure, select, portfolio = key
    label = "withdrawals from the portfolio" if measure == "withdrawals" else "spending"
    if select:
        label += f" [{select}]"
    if portfolio:
        label += f" (portfolio: {portfolio})"
    return label


def _threshold_label(r: RailResult) -> str:
    if r.rail.is_percent:
        return r.rail.threshold_text()
    return f"${r.rail.threshold / 100:,.0f}"


def _thousands(ax: Axes) -> None:
    ax.yaxis.set_major_formatter(FuncFormatter(
        lambda v, _: f"{'-' if v < 0 else ''}${abs(v) / 1000:,.0f}k"))
    ax.grid(True, axis="y", alpha=0.3)


def guard_rail_figure(
    results: Sequence[RailResult], title: str, *, unit: str = "CPI-constant",
    carried: bool = True, years: tuple[int, int] | None = None, deaths: Sequence[Death] = (),
) -> Figure:
    """A column per measure (rails with the same measure share one): the
    year's measure and each rail's threshold, shaded where past it, over
    each rail's cumulative margin, shaded below zero, with ``carried`` its
    carried-at-return line dotted. A positive rail is dashed and hatched.
    ``years`` limits the axis to that span (first, last)."""
    shown = [r for r in results if r.applies]
    colors = rail_colors(shown)
    groups: dict[tuple[str, str, str], list[RailResult]] = {}
    for r in shown:
        groups.setdefault(_measure_key(r), []).append(r)
    fig = Figure(figsize=(min(7.0 * max(len(groups), 1), 16.0), 9), layout="constrained")
    if not groups:
        fig.text(0.5, 0.5, "No guard rail covers this run's years.", ha="center")
        return fig
    axes = fig.subplots(2, len(groups), sharex="col", squeeze=False)
    fig.suptitle(f"{title}: annual (top) and cumulative (bottom), {unit} dollars")
    for column, (key, rails) in enumerate(groups.items()):
        top, bottom = axes[0, column], axes[1, column]
        frames = {r.rail.name: rail_frame(r) for r in rails}
        span = sorted(set().union(*(f.index for f in frames.values())))
        if years is not None:
            span = [y for y in span if years[0] <= y <= years[1]]
        if not span:
            continue
        position = {year: i for i, year in enumerate(span)}
        # Rails sharing a measure may cover different years: the measure line
        # spans them all.
        measure = pd.concat([f["measure"] for f in frames.values()])
        measure = measure[~measure.index.duplicated()].reindex(span)
        x = np.arange(len(span))
        top.plot(x, measure, color=_MEASURE_COLOR, linewidth=2, marker="o",
                 markersize=4, label=_measure_label(key), zorder=3)
        for r in rails:
            frame = frames[r.rail.name].reindex(span)
            rail_x = np.array([position[y] for y in frame.index])
            color, style = colors[r.rail.name], "--" if r.rail.positive else "-"
            hatch = "//" if r.rail.positive else None
            top.plot(rail_x, frame["threshold"], color=color, linestyle=style, linewidth=2,
                     label=f"{r.rail.name}: {r.rail.side} {_threshold_label(r)}"
                           + (" (positive)" if r.rail.positive else ""))
            top.fill_between(rail_x, frame["measure"], frame["threshold"],
                             where=(frame["margin"] < 0).to_numpy(), interpolate=True,
                             color=color, alpha=0.2, hatch=hatch, edgecolor=color, linewidth=0)
            bottom.plot(rail_x, frame["cumulative"], color=color, linestyle=style, linewidth=2,
                        marker="o", markersize=4, label=r.rail.name)
            bottom.fill_between(rail_x, frame["cumulative"], 0,
                                where=(frame["cumulative"] < 0).to_numpy(), interpolate=True,
                                color=color, alpha=0.15, hatch=hatch, edgecolor=color, linewidth=0)
            if carried:
                bottom.plot(rail_x, frame["carried"], color=color, linestyle=":", linewidth=1.5,
                            label=f"{r.rail.name}, carried at the portfolio's return")
        top.set_title(f"Annual: {_measure_label(key)}")
        bottom.set_title("Cumulative margin: room banked (+) or overdrawn (−)")
        bottom.axhline(0, color="grey", linewidth=0.8)
        for ax in (top, bottom):
            _thousands(ax)
            include_zero(ax)
            mark_deaths(ax, deaths, years=span)
            ax.legend(framealpha=FRAME_ALPHA, fontsize="small")
        top.set_ylabel(f"per year ({unit})")
        bottom.set_ylabel(f"cumulative ({unit})")
        step = max(1, len(span) // 12)
        bottom.set_xticks(x[::step], [str(y) for y in span[::step]], rotation=90)
    return fig


def guard_rail_comparison_figure(
    results: Mapping[str, Sequence[RailResult]], baseline: str, variants: Sequence[str],
    title: str, *, unit: str = "CPI-constant", colors: Mapping[str, str] | None = None,
    years: tuple[int, int] | None = None, deaths: Sequence[Death] = (),
) -> Figure:
    """A row per rail any scenario has (matched by name), annual margin
    (left) and cumulative (right): a line per scenario that has the rail,
    the baseline solid and the variants dashed, as ``comparison_figure``
    draws them. Below zero is past the rail, tinted warm for a rail to stay
    inside and cool, hatched, for a positive one. ``years`` limits the axis
    to that span (first, last); the margins still sum from each rail's
    start."""
    names = [baseline, *variants]
    by_name = {name: {r.rail.name: r for r in results.get(name, ()) if r.applies}
               for name in names}
    rails = list(dict.fromkeys(rail for found in by_name.values() for rail in found))
    colors = colors or {name: f"C{i}" for i, name in enumerate(names)}
    fig = Figure(figsize=(14, min(3.4 * max(len(rails), 1) + 1, 24)), layout="constrained")
    if not rails:
        fig.text(0.5, 0.5, "No guard rail covers these runs' years.", ha="center")
        return fig
    axes = fig.subplots(len(rails), 2, sharex=True, squeeze=False)
    fig.suptitle(f"{title}: margins, annual (left) and cumulative (right), {unit} dollars; "
                 "below zero is past the rail")
    frames = {(name, rail): rail_frame(found[rail])
              for name, found in by_name.items() for rail in found}
    span = sorted(set().union(*(f.index for f in frames.values())))
    if years is not None:
        span = [y for y in span if years[0] <= y <= years[1]]
    x = np.arange(len(span))
    for row, rail in enumerate(rails):
        sample = next(by_name[name][rail] for name in names if rail in by_name[name])
        positive = sample.rail.positive
        tint = _POSITIVE_COLORS[0] if positive else _NEGATIVE_COLORS[1]
        for column, (form, field_) in enumerate((("annual", "margin"),
                                                 ("cumulative", "cumulative"))):
            ax = axes[row, column]
            for name in names:
                if (name, rail) not in frames:
                    continue
                values = frames[name, rail][field_].reindex(span)
                ax.plot(x, values, color=colors[name], marker="o", markersize=3,
                        linestyle="-" if name == baseline else "--",
                        linewidth=2 if name == baseline else 1.5, label=name)
            ax.axhline(0, color="grey", linewidth=0.8)
            _thousands(ax)
            include_zero(ax)
            low, _high = ax.get_ylim()
            if low < 0:
                ax.axhspan(low, 0, color=tint, alpha=0.08, hatch="//" if positive else None,
                           linewidth=0)
                ax.set_ylim(bottom=low)
            ax.set_title(f"{rail} ({sample.rail.sense}): {form} margin")
            mark_deaths(ax, deaths, years=span)
            ax.legend(framealpha=FRAME_ALPHA, fontsize="small")
        axes[row, 0].set_ylabel(f"margin ({unit})")
    step = max(1, len(span) // 12)
    for ax in axes[-1]:
        ax.set_xticks(x[::step], [str(y) for y in span[::step]], rotation=90)
    return fig


def ensemble_guard_rail_figure(
    results: Sequence[EnsembleRailResult], title: str, *, unit: str = "CPI-constant",
    years: tuple[int, int] | None = None, deaths: Sequence[Death] = (),
) -> Figure:
    """Each rail across an ensemble's members, annual (left) and cumulative
    (right): the share of members past it each year, with the share ever
    past it so far dotted, above their RMS overshoot. A positive rail is
    dashed. ``years`` limits the axis to that span (first, last); the
    shares still look back to each rail's start. ``deaths`` (member 0's, as
    the fan chart marks them) are drawn as on every chart."""
    shown = [r for r in results if r.applies]
    colors = rail_colors(shown)
    fig = Figure(figsize=(14, 9), layout="constrained")
    if not shown:
        fig.text(0.5, 0.5, "No guard rail covers these members' years.", ha="center")
        return fig
    axes = fig.subplots(2, 2, sharex=True, squeeze=False)
    fig.suptitle(f"{title}: members past each rail, annual (left) and cumulative (right)")
    frames = {r.rail.name: ensemble_rail_frame(r) for r in shown}
    span = sorted(set().union(*(f.index for f in frames.values())))
    if years is not None:
        span = [y for y in span if years[0] <= y <= years[1]]
    x = np.arange(len(span))
    for column, (form, suffix) in enumerate((("Annual", ""), ("Cumulative", " cum"))):
        top, bottom = axes[0, column], axes[1, column]
        for r in shown:
            frame = frames[r.rail.name].reindex(span)
            color, style = colors[r.rail.name], "--" if r.rail.positive else "-"
            label = r.rail.name + (" (positive)" if r.rail.positive else "")
            top.plot(x, frame["past" + suffix], color=color, linestyle=style, linewidth=2,
                     marker="o", markersize=4, label=label)
            top.plot(x, frame["ever_past" + suffix], color=color, linestyle=":", linewidth=1.5,
                     label="_ever")
            bottom.plot(x, frame["rms_over" + suffix], color=color, linestyle=style, linewidth=2,
                        marker="o", markersize=4, label=label)
        # One key for every rail's dotted line, not one per rail.
        top.plot([], [], color="grey", linestyle=":", linewidth=1.5, label="ever past, so far")
        top.set_title(f"{form}: share of members past the rail")
        top.set_ylim(-0.02, 1.02)
        top.yaxis.set_major_formatter(PercentFormatter(1.0))
        top.grid(True, axis="y", alpha=0.3)
        bottom.set_title(f"{form}: RMS overshoot ({unit} dollars, inside counts as zero)")
        _thousands(bottom)
        include_zero(bottom)
        for ax in (top, bottom):
            mark_deaths(ax, deaths, years=span)
            ax.legend(framealpha=FRAME_ALPHA, fontsize="small")
        step = max(1, len(span) // 12)
        bottom.set_xticks(x[::step], [str(y) for y in span[::step]], rotation=90)
    return fig
