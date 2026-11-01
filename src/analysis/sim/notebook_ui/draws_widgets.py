"""Draws notebook widget (#261): an ensemble batch's draws beside the
distributions and correlations they came from, read from its draws.sqlite3.

The node and pair charts are ``draws_charts``' (shared with ``sim ensemble
draws --chart``, #330); ``draws_widget`` wires them to recipe, batch, node
and pair pickers, plus a component picker for a pool (ADR-009), whose
components are checked one at a time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import ipywidgets as widgets
import pandas as pd
from IPython.display import display

from analysis.sim.draws_charts import draws_figure, pair_figure, pair_options
from analysis.sim.draws_db import (
    build_draws_db, component_frames, components, draws_db_current, draws_frames,
)
from analysis.sim.ensemble import EnsembleError
from analysis.sim.notebook_ui.pickers import find_data_dir, find_runs_dir, note_picks
from analysis.sim.notebook_ui.sensitivity_widgets import list_batches, list_sensitivity_recipes
from analysis.sim.notebook_ui.styled import StyledMarkdown, show_figure

__all__ = ["check_table", "draws_widget"]


def check_table(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """``draw_check`` in display units, one row per node, member 0 left out."""
    check = frames["draw_check"].set_index("node")
    nodes = frames["node"].set_index("name")
    table = check.join(nodes[["distribution", "node_kind"]], how="left")
    table["distribution"] = table["distribution"].fillna(table["node_kind"])
    return table[["distribution", "unit", "n", "expected_p50", "drawn_p50", "expected_sd",
                  "drawn_sd", "expected_p05", "drawn_p05", "expected_p95", "drawn_p95", "ks_u_p"]]


def _show(out: widgets.Output, draw) -> None:
    out.clear_output(wait=True)
    with out:
        try:
            show_figure(draw())
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
    recipe = widgets.Dropdown(options=list_sensitivity_recipes(runs_dir, find_data_dir(data_dir)),
                              description="Recipe:")
    batch = widgets.Dropdown(options=[], description="Batch:", layout=widgets.Layout(width="max-content"))
    # A pool's members come from several ensembles, each drawn from its own
    # distributions, so the checks are per component; hidden otherwise.
    component = widgets.Dropdown(options=[], description="Component:",
                                 layout=widgets.Layout(width="max-content", display="none"))
    node = widgets.Dropdown(options=[], description="Node:")
    pair = widgets.Dropdown(options=[], description="Pair:", layout=widgets.Layout(width="max-content"))
    table_out, node_out, pair_out = widgets.Output(), widgets.Output(), widgets.Output()
    state: dict[str, Any] = {"frames": None, "batch_frames": None, "loading": False}

    def _draw_node(*_: object) -> None:
        frames = state["frames"]
        if frames is not None and node.value and not state["loading"]:
            _show(node_out, lambda: draws_figure(frames, node.value))

    def _draw_pair(*_: object) -> None:
        frames = state["frames"]
        if state["loading"]:
            return
        pair_out.clear_output()
        if frames is not None and pair.value:
            _show(pair_out, lambda: pair_figure(frames, *pair.value))

    def _on_batch(*_: object) -> None:
        state["frames"] = state["batch_frames"] = None
        for out in (table_out, node_out, pair_out):
            out.clear_output()
        if batch.value is None:
            return
        try:
            if not draws_db_current(batch.value):
                build_draws_db(batch.value)
            frames = draws_frames(batch.value)
        except EnsembleError as exc:
            with table_out:
                display(StyledMarkdown(f"_{exc}_"))
            return
        state["batch_frames"] = frames
        names = components(frames)
        state["loading"] = True
        try:
            component.options = names
            component.value = names[0] if names else None
        finally:
            state["loading"] = False
        component.layout.display = None if len(names) > 1 else "none"
        _on_component()

    def _on_component(*_: object) -> None:
        batch_frames = state["batch_frames"]
        if batch_frames is None or state["loading"]:
            return
        frames = component_frames(batch_frames, component.value) if component.value else batch_frames
        state["frames"] = frames
        for out in (table_out, node_out, pair_out):
            out.clear_output()
        with table_out:
            if frames["draw_check"].empty:
                mode = frames["batch"]["mode"].iloc[0] if not frames["batch"].empty else ""
                if mode != "monte_carlo":
                    why = "A one-at-a-time batch draws nothing at random"
                elif len(component.options) > 1:
                    why = "Only the base case (member 0) came from this component, and it draws nothing"
                else:
                    why = "No drawn member succeeded (member 0 is the base case), so there is nothing to check"
                display(StyledMarkdown(f"_{why}; its nodes are listed below._"))
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
    note_picks(find_data_dir(data_dir), recipe)
    batch.observe(_on_batch, names="value")
    component.observe(_on_component, names="value")
    node.observe(_draw_node, names="value")
    pair.observe(_draw_pair, names="value")
    return widgets.VBox([
        widgets.HBox([recipe, batch, component]), table_out,
        node, node_out, pair, pair_out,
    ])
