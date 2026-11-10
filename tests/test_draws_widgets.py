"""#261: the Draws notebook widget and its views."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from ipywidgets import widgets
from matplotlib.figure import Figure

from analysis.sim.notebook_ui import draws_widgets as dw


def _quiet(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    shown: list[object] = []
    monkeypatch.setattr(dw, "display", lambda *args, **kwargs: shown.extend(args))
    monkeypatch.setattr(dw, "show_figure", shown.append)
    return shown


def test_draws_widget_on_a_monte_carlo_batch(
    mc_batch: Path, runs_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    shown = _quiet(monkeypatch)
    noted: list[tuple[object, ...]] = []
    monkeypatch.setattr(dw, "note_picks", lambda _data, *pickers: noted.append(pickers))
    box = dw.draws_widget(runs_dir=runs_dir)
    assert isinstance(box, widgets.VBox)
    recipe, batch, component = box.children[0].children
    assert noted == [(recipe,)]  # picking a recipe is use of it (#322)
    assert (recipe.value, batch.value) == ("retire_alice", mc_batch)
    # One component, the batch's own: the picker is for pools only (ADR-009).
    assert component.value == "retire_alice" and component.layout.display == "none"
    assert any(isinstance(item, pd.DataFrame) and "ks_u_p" in item.columns for item in shown)
    node, pair = box.children[2], box.children[4]
    node.value = "gift"
    pair.value = pair.options[-1][1]
    assert not [s for s in shown if "_" in str(s)[:1]]  # no error message shown
    assert sum(isinstance(s, Figure) for s in shown) >= 2  # a node's chart and a pair's


def test_draws_widget_on_a_sensitivity_batch(
    oat_batch: Path, runs_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    shown = _quiet(monkeypatch)
    dw.draws_widget(runs_dir=runs_dir)
    assert any("one-at-a-time" in str(getattr(s, "data", s)) for s in shown)
