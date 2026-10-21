"""Draws notebook widget (#261): an ensemble batch's draws beside the
distributions and correlations they came from, read from its draws.sqlite3.

``draws_view`` shows one node: a histogram of its drawn values with the
distribution's own 5th, 50th and 95th percentiles marked, and a histogram
of the members' ``u``, which a correct setup spreads evenly over 0 to 1.
``pair_view`` is a scatter of two correlated nodes' scores. Both work on
``draws_db.draws_frames`` (testable without a kernel); ``draws_widget``
wires them to recipe, batch, node and pair pickers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import ipywidgets as widgets
import matplotlib.pyplot as plt
import pandas as pd
from IPython.display import display

from analysis.frames import named_rows

from analysis.sim.draws_db import build_draws_db, draws_db_path, draws_frames
from analysis.sim.ensemble import EnsembleError
from analysis.sim.notebook_ui.pickers import find_runs_dir
from analysis.sim.notebook_ui.sensitivity_widgets import (
    CATEGORICAL, GRID, INK, MUTED, list_batches, list_sensitivity_recipes,
)
from analysis.sim.notebook_ui.styled import StyledMarkdown

__all__ = ["check_table", "draws_view", "pair_view", "pair_options", "draws_widget"]

DRAWN = CATEGORICAL[0]


def check_table(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """``draw_check`` in display units, one row per node, member 0 left out."""
    check = frames["draw_check"].set_index("node")
    nodes = frames["node"].set_index("name")
    table = check.join(nodes[["distribution", "node_kind"]], how="left")
    table["distribution"] = table["distribution"].fillna(table["node_kind"])
    return table[["distribution", "unit", "n", "expected_p50", "drawn_p50", "expected_sd",
                  "drawn_sd", "expected_p05", "drawn_p05", "expected_p95", "drawn_p95", "ks_u_p"]]


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
        _fig, axes = plt.subplots(1, 2, figsize=(11, 3.4), layout="constrained")
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
        right.hist(u, bins=min(20, max(5, len(u) // 5)), range=(0, 1), color=DRAWN,
                   edgecolor="white", linewidth=1)
        right.axhline(len(u) / min(20, max(5, len(u) // 5)), color=INK, linestyle="--", linewidth=1.2)
    right.set_xlim(0, 1)
    right.set_title("u: where each draw sits in its distribution", loc="left", color=INK)
    right.set_xlabel("dashed: the count each bin expects when u is uniform", color=MUTED)
    _recessive(right)
    return axes


def pair_options(frames: dict[str, pd.DataFrame]) -> list[tuple[str, tuple[str, str]]]:
    """``(label, (a, b))`` for each correlated pair with a recorded achieved rho,
    parent-and-child links first."""
    pairs = frames["node_pair"].sort_values(
        ["kind", "a", "b"], key=lambda c: c.ne("link") if c.name == "kind" else c)
    return [
        (f"{r.a} ~ {r.b} ({r.kind}, rho {r.implied_rho:+.2f})", (str(r.a), str(r.b)))
        for r in named_rows(pairs)
    ]


def pair_view(frames: dict[str, pd.DataFrame], a: str, b: str, ax: Any = None) -> Any:
    """Two nodes' scores across the drawn members, with the implied and
    achieved rho in the title."""
    draws = frames["draw"]
    drawn = draws[draws["is_base"] == 0].pivot(index="member_index", columns="node", values="z")
    if a not in drawn.columns or b not in drawn.columns:
        raise EnsembleError(f"{a!r} and {b!r}: no scores recorded")
    pair = drawn[[a, b]].dropna()
    if ax is None:
        _fig, ax = plt.subplots(figsize=(5, 4.6), layout="constrained")
    ax.scatter(pair[a], pair[b], s=36, color=DRAWN, edgecolor="white", linewidth=1)
    found = frames["node_pair"]
    row = found[((found["a"] == a) & (found["b"] == b)) | ((found["a"] == b) & (found["b"] == a))]
    title = f"{a} vs {b}"
    if not row.empty:
        r = row.iloc[0]
        achieved = "" if pd.isna(r["achieved_rho"]) else (
            f", achieved {r['achieved_rho']:+.2f} ({r['achieved_low']:+.2f} to {r['achieved_high']:+.2f})")
        title += f"\nimplied rho {r['implied_rho']:+.2f}{achieved}"
    ax.set_title(title, loc="left", color=INK)
    ax.set_xlabel(f"{a} score", color=MUTED)
    ax.set_ylabel(f"{b} score", color=MUTED)
    _recessive(ax)
    return ax


def _show(out: widgets.Output, draw) -> None:
    out.clear_output(wait=True)
    with out:
        try:
            draw()
            plt.show()
        except EnsembleError as exc:
            display(StyledMarkdown(f"_{exc}_"))


def draws_widget(*,
                 data_dir: Path | str | None = None,
                 state_dir: Path | None = None,
                 runs_dir: Path | str | None = None,
                 ) -> widgets.VBox:
    """Recipe and batch dropdowns; then the batch's ``draw_check`` table, one
    node's draws (Node) and one correlated pair's scores (Pair). A batch run
    before draws.sqlite3 existed gets one gathered from its run files."""
    runs_dir = find_runs_dir(data_dir=data_dir, state_dir=state_dir, runs_dir=runs_dir)
    recipe = widgets.Dropdown(options=list_sensitivity_recipes(runs_dir), description="Recipe:")
    batch = widgets.Dropdown(options=[], description="Batch:", layout=widgets.Layout(width="max-content"))
    node = widgets.Dropdown(options=[], description="Node:")
    pair = widgets.Dropdown(options=[], description="Pair:", layout=widgets.Layout(width="max-content"))
    table_out, node_out, pair_out = widgets.Output(), widgets.Output(), widgets.Output()
    state: dict[str, Any] = {"frames": None, "loading": False}

    def _draw_node(*_: object) -> None:
        frames = state["frames"]
        if frames is not None and node.value and not state["loading"]:
            _show(node_out, lambda: draws_view(frames, node.value))

    def _draw_pair(*_: object) -> None:
        frames = state["frames"]
        if state["loading"]:
            return
        pair_out.clear_output()
        if frames is not None and pair.value:
            _show(pair_out, lambda: pair_view(frames, *pair.value))

    def _on_batch(*_: object) -> None:
        state["frames"] = None
        for out in (table_out, node_out, pair_out):
            out.clear_output()
        if batch.value is None:
            return
        try:
            if not draws_db_path(batch.value).is_file():
                build_draws_db(batch.value)
            frames = draws_frames(batch.value)
        except EnsembleError as exc:
            with table_out:
                display(StyledMarkdown(f"_{exc}_"))
            return
        state["frames"] = frames
        with table_out:
            if frames["draw_check"].empty:
                display(StyledMarkdown("_A one-at-a-time batch draws nothing at random; "
                                       "its nodes are listed below._"))
                display(frames["node"].set_index("name"))
            else:
                display(check_table(frames))
        names = sorted(frames["node"]["name"])
        state["loading"] = True  # setting options fires the observers
        try:
            node.options = names
            node.value = names[0] if names else None
            options = pair_options(frames)
            pair.options = options
            pair.value = options[0][1] if options else None
        finally:
            state["loading"] = False
        _draw_node()
        _draw_pair()

    def _on_recipe(*_: object) -> None:
        options = list_batches(runs_dir / recipe.value) if recipe.value else []
        batch.options = options
        batch.value = options[0][1] if options else None

    _on_recipe()
    _on_batch()
    recipe.observe(_on_recipe, names="value")
    batch.observe(_on_batch, names="value")
    node.observe(_draw_node, names="value")
    pair.observe(_draw_pair, names="value")
    return widgets.VBox([
        widgets.HBox([recipe, batch]), table_out,
        node, node_out, pair, pair_out,
    ])
