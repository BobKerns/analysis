"""The draws charts (#261, #330), widget-free so ``sensitivity.ipynb``'s
Draws section and ``sim ensemble draws --chart`` draw the same figures.

``draws_view`` shows one node: a histogram of its drawn values with the
distribution's own 5th, 50th and 95th percentiles marked, and a histogram
of the members' ``u``, which a correct setup spreads evenly over 0 to 1.
``pair_view`` is a scatter of two correlated nodes' scores, with the
implied and achieved rho in its title. Both work on
``draws_db.draws_frames`` (one component's, for a pool) and draw on plain
matplotlib figures, not pyplot's, as ``ensemble_plots`` does.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
from matplotlib.figure import Figure

from analysis.frames import named_rows
from analysis.sim.ensemble import EnsembleError
from analysis.sim.plotting import CATEGORICAL, GRID, INK, MUTED

__all__ = ["draws_figure", "draws_view", "pair_figure", "pair_options", "pair_row", "pair_view"]

DRAWN = CATEGORICAL[0]


def _numeric(values: pd.Series, unit: str) -> pd.Series:
    return pd.to_datetime(values) if unit == "date" else pd.to_numeric(values)


def _recessive(ax: Any) -> None:
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(MUTED)
    ax.tick_params(colors=MUTED, labelcolor=INK)


def draws_view(frames: dict[str, pd.DataFrame], node: str, axes: Any = None) -> Any:
    """One node's drawn values and ``u``, two panels side by side."""
    draws = frames["draw"]
    mine = draws[(draws["node"] == node) & (draws["is_base"] == 0)]
    if mine.empty:
        raise EnsembleError(f"{node!r}: no drawn members (member 0 is never a draw)")
    unit = str(mine["unit"].iloc[0])
    if axes is None:
        axes = Figure(figsize=(11, 3.4), layout="constrained").subplots(1, 2)
    left, right = axes
    left.hist(_numeric(mine["value"], unit), bins="auto", color=DRAWN, edgecolor="white", linewidth=1)
    check = frames["draw_check"].set_index("node")
    if node in check.index:
        for column, style in (("expected_p05", ":"), ("expected_p50", "--"), ("expected_p95", ":")):
            value = check.loc[node, column]
            if pd.notna(value):
                left.axvline(_numeric(pd.Series([value]), unit).iloc[0], color=INK,
                             linestyle=style, linewidth=1.2)
    left.set_title(f"{node}: drawn values ({unit})", loc="left", color=INK)
    left.set_xlabel("dashed: the distribution's median; dotted: its 5th and 95th percentiles",
                    color=MUTED)
    _recessive(left)
    u = mine["u"].dropna()
    if u.empty:
        right.text(0.5, 0.5, "no u recorded", ha="center", va="center", color=MUTED,
                   transform=right.transAxes)
    else:
        bins = min(20, max(5, len(u) // 5))
        right.hist(u, bins=bins, range=(0, 1), color=DRAWN, edgecolor="white", linewidth=1)
        right.axhline(len(u) / bins, color=INK, linestyle="--", linewidth=1.2)
    right.set_xlim(0, 1)
    right.set_title("u: where each draw sits in its distribution", loc="left", color=INK)
    right.set_xlabel("dashed: the count each bin expects when u is uniform", color=MUTED)
    _recessive(right)
    return axes


def draws_figure(frames: dict[str, pd.DataFrame], node: str) -> Figure:
    """``draws_view`` on a figure of its own."""
    left, _right = draws_view(frames, node)
    return left.figure


def pair_options(frames: dict[str, pd.DataFrame]) -> list[tuple[str, tuple[str, str]]]:
    """``(label, (a, b))`` for each correlated pair in ``node_pair``,
    parent-and-child links first."""
    pairs = frames["node_pair"].sort_values(
        ["kind", "a", "b"], key=lambda c: c.ne("link") if c.name == "kind" else c)
    return [
        (f"{r.a} ~ {r.b} ({r.kind}, rho {r.implied_rho:+.2f})", (str(r.a), str(r.b)))
        for r in named_rows(pairs)
    ]


def pair_row(frames: dict[str, pd.DataFrame], a: str, b: str) -> pd.Series | None:
    """``node_pair``'s row for ``a`` and ``b``, in either order; None when
    they aren't a correlated pair."""
    found = frames["node_pair"]
    row = found[((found["a"] == a) & (found["b"] == b)) | ((found["a"] == b) & (found["b"] == a))]
    return None if row.empty else row.iloc[0]


def pair_view(frames: dict[str, pd.DataFrame], a: str, b: str, ax: Any = None) -> Any:
    """Two nodes' scores across the drawn members, with the implied and
    achieved rho in the title."""
    draws = frames["draw"]
    drawn = draws[draws["is_base"] == 0].pivot(index="member_index", columns="node", values="z")
    if a not in drawn.columns or b not in drawn.columns:
        raise EnsembleError(f"{a!r} and {b!r}: no scores recorded")
    pair = drawn[[a, b]].dropna()
    if ax is None:
        ax = Figure(figsize=(5, 4.6), layout="constrained").subplots()
    ax.scatter(pair[a], pair[b], s=36, color=DRAWN, edgecolor="white", linewidth=1)
    title = f"{a} vs {b}"
    r = pair_row(frames, a, b)
    if r is not None:
        achieved = "" if pd.isna(r["achieved_rho"]) else (
            f", achieved {r['achieved_rho']:+.2f} ({r['achieved_low']:+.2f} to {r['achieved_high']:+.2f})")
        title += f"\nimplied rho {r['implied_rho']:+.2f}{achieved}"
    ax.set_title(title, loc="left", color=INK)
    ax.set_xlabel(f"{a} score", color=MUTED)
    ax.set_ylabel(f"{b} score", color=MUTED)
    _recessive(ax)
    return ax


def pair_figure(frames: dict[str, pd.DataFrame], a: str, b: str) -> Figure:
    """``pair_view`` on a figure of its own."""
    return pair_view(frames, a, b).figure
