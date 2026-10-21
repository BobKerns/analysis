"""#261: the Draws notebook widget and its views."""

from __future__ import annotations

from pathlib import Path

import matplotlib
import pandas as pd
import pytest

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from ipywidgets import widgets

from analysis.sim.draws_db import draws_frames
from analysis.sim.ensemble import EnsembleError
from analysis.sim.notebook_ui import draws_widgets as dw


def _quiet(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    shown: list[object] = []
    monkeypatch.setattr(dw, "display", lambda *args, **kwargs: shown.extend(args))
    monkeypatch.setattr(plt, "show", lambda *args, **kwargs: plt.close("all"))
    return shown


def test_views_draw_a_node_and_a_pair(mc_batch: Path) -> None:
    frames = draws_frames(mc_batch)
    table = dw.check_table(frames)
    assert table.loc["gift", "n"] == 7 and table.loc["gift", "distribution"] == "uniform"
    left, right = dw.draws_view(frames, "retire_bill_vary")
    assert left.get_title("left").startswith("retire_bill_vary") and len(left.lines) == 3
    assert right.get_xlim() == (0.0, 1.0)
    a, b = dw.pair_options(frames)[0][1]
    ax = dw.pair_view(frames, a, b)
    assert "implied rho" in ax.get_title("left")
    plt.close("all")


def test_draws_view_refuses_a_node_with_no_draws(mc_batch: Path) -> None:
    with pytest.raises(EnsembleError, match="no drawn members"):
        dw.draws_view(draws_frames(mc_batch), "nope")


def test_draws_widget_on_a_monte_carlo_batch(
    mc_batch: Path, runs_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    shown = _quiet(monkeypatch)
    box = dw.draws_widget(runs_dir=runs_dir)
    assert isinstance(box, widgets.VBox)
    recipe, batch = box.children[0].children
    assert (recipe.value, batch.value) == ("retire_alice", mc_batch)
    assert any(isinstance(item, pd.DataFrame) and "ks_u_p" in item.columns for item in shown)
    node, pair = box.children[2], box.children[4]
    node.value = "gift"
    pair.value = pair.options[-1][1]
    assert not [s for s in shown if "_" in str(s)[:1]]  # no error message shown


def test_draws_widget_on_a_sensitivity_batch(
    oat_batch: Path, runs_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    shown = _quiet(monkeypatch)
    dw.draws_widget(runs_dir=runs_dir)
    assert any("one-at-a-time" in str(getattr(s, "data", s)) for s in shown)
