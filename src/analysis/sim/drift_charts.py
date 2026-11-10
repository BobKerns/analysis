"""Allocation drift charts (#359), widget-free so the notebooks and the CLI
draw the same figures: one run's drift by class, runs side by side (a
strategy off vs on), and an ensemble's spread of drift.

Drift is each class's share less its target, in percentage points; positive
is overweight. A strategy's band, when known, is shaded around zero.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd
from matplotlib.figure import Figure

from analysis.sim.drift import MAX_ABS
from analysis.frames import clip_years
from analysis.sim.plotting import mark_deaths
from analysis.sim.types import Death

__all__ = ["drift_compare_figure", "drift_fan_figure", "drift_figure"]

_BAND_COLOR = "#2a78d6"
_ZERO = {"color": "black", "linewidth": 0.8, "linestyle": "--"}


def _dates(index: pd.Index) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.to_datetime([str(m) for m in index]))


def _band(ax, band: float | None) -> None:
    if band:
        ax.axhspan(-band, band, color=_BAND_COLOR, alpha=0.08, linewidth=0, label=f"band ±{band:g} pts")


def _finish(ax, ylabel: str = "percentage points") -> None:
    ax.axhline(0, **_ZERO)
    ax.set_ylabel(ylabel)
    ax.grid(True, alpha=0.3)


def drift_figure(
    frame: pd.DataFrame, title: str, *, band: float | None = None,
    decisions: Sequence[str] = (), years: tuple[int, int] | None = None, deaths: Sequence[Death] = (),
) -> Figure:
    """One run: a line per asset class (``drift.drift``'s columns but
    ``max_abs``), the band shaded, and a tick at the foot for each month a
    strategy decided (``decisions``, ISO months)."""
    fig = Figure(figsize=(11, 5), layout="constrained")
    ax = fig.add_subplot()
    shown = clip_years(frame, years)
    if shown.empty:
        ax.text(0.5, 0.5, "No months to show.", ha="center", transform=ax.transAxes)
        return fig
    x = _dates(shown.index)
    _band(ax, band)
    for column in [c for c in shown.columns if c != MAX_ABS]:
        ax.plot(x, shown[column], linewidth=1.6, label=column)
    picked = sorted({m for m in decisions if str(m) in set(map(str, shown.index))})
    if picked:
        ax.plot(_dates(pd.Index(picked)), np.zeros(len(picked)), linestyle="none", marker="|",
                markersize=10, color="#555555", transform=ax.get_xaxis_transform(), clip_on=False,
                label="strategy decided")
    _finish(ax)
    mark_deaths(ax, deaths)
    ax.set_title(title)
    ax.legend(loc="best", fontsize="small")
    return fig


def drift_compare_figure(
    frames: Mapping[str, pd.DataFrame], title: str, *, band: float | None = None,
    years: tuple[int, int] | None = None, deaths: Sequence[Death] = (),
) -> Figure:
    """Runs side by side against one target set: a panel per asset class and
    one of ``max_abs``, a line per run (the first is the baseline)."""
    clipped = {name: clip_years(frame, years) for name, frame in frames.items()}
    classes: list[str] = []
    for frame in clipped.values():
        classes += [c for c in frame.columns if c != MAX_ABS and c not in classes]
    panels = [*classes, MAX_ABS]
    cols = min(len(panels), 3)
    rows = -(-len(panels) // cols)
    fig = Figure(figsize=(5.0 * cols, 3.4 * rows + 0.6), layout="constrained")
    axes = fig.subplots(rows, cols, sharex=True, squeeze=False).ravel()
    for ax, panel in zip(axes, panels, strict=False):
        if panel == MAX_ABS:
            if band:
                ax.axhline(band, color=_BAND_COLOR, linewidth=1, linestyle=":", label="band")
        else:
            _band(ax, band)
        for name, frame in clipped.items():
            if panel in frame.columns and not frame.empty:
                ax.plot(_dates(frame.index), frame[panel], linewidth=1.5, label=name)
        _finish(ax, "largest drift, pts" if panel == MAX_ABS else "percentage points")
        ax.set_title("largest drift either way" if panel == MAX_ABS else panel)
        mark_deaths(ax, deaths)
    for ax in axes[len(panels):]:
        ax.set_visible(False)
    axes[0].legend(loc="best", fontsize="small")
    fig.suptitle(title)
    return fig


def drift_fan_figure(
    members: pd.DataFrame, title: str, *, band: float | None = None,
    quantiles: Sequence[float] = (0.05, 0.25, 0.5, 0.75, 0.95), years: tuple[int, int] | None = None,
    deaths: Sequence[Death] = (), ylabel: str = "percentage points",
) -> Figure:
    """An ensemble's drift: one column per member, indexed by ISO month; the
    median line and shaded quantile bands between the outer and inner pairs."""
    fig = Figure(figsize=(11, 5), layout="constrained")
    ax = fig.add_subplot()
    shown = clip_years(members, years)
    if shown.empty:
        ax.text(0.5, 0.5, "No months to show.", ha="center", transform=ax.transAxes)
        return fig
    x = _dates(shown.index)
    qs = sorted(quantiles)
    values = shown.quantile(qs, axis=1).T
    _band(ax, band)
    for i in range(len(qs) // 2):
        low, high = qs[i], qs[-1 - i]
        ax.fill_between(x, values[low], values[high], color="#7f8c8d", alpha=0.18 + 0.12 * i, linewidth=0,
                        label=f"{low:.0%}–{high:.0%}")
    if len(qs) % 2:
        mid = qs[len(qs) // 2]
        ax.plot(x, values[mid], color="black", linewidth=1.6, label=f"{mid:.0%}")
    _finish(ax, ylabel)
    mark_deaths(ax, deaths)
    ax.set_title(f"{title} ({shown.shape[1]} members)")
    ax.legend(loc="best", fontsize="small")
    return fig
