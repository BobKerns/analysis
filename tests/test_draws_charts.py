"""#261, #330: the draws charts, shared by the notebook and the CLI."""

from __future__ import annotations

from pathlib import Path

import pytest

from analysis.sim import draws_charts as dc
from analysis.sim.draws_db import draws_frames
from analysis.sim.ensemble import EnsembleError


def test_views_draw_a_node_and_a_pair(mc_batch: Path) -> None:
    frames = draws_frames(mc_batch)
    left, right = dc.draws_view(frames, "retire_bill_vary")
    assert left.get_title("left").startswith("retire_bill_vary") and len(left.lines) == 3
    assert right.get_xlim() == (0.0, 1.0)
    a, b = dc.pair_options(frames)[0][1]
    ax = dc.pair_view(frames, a, b)
    assert "implied rho" in ax.get_title("left")


def test_figures_are_their_own(mc_batch: Path) -> None:
    """No pyplot: each call draws a new figure of its own (#119)."""
    frames = draws_frames(mc_batch)
    node = dc.draws_figure(frames, "gift")
    assert len(node.axes) == 2 and node is not dc.draws_figure(frames, "gift")
    a, b = dc.pair_options(frames)[0][1]
    assert len(dc.pair_figure(frames, a, b).axes) == 1


def test_draws_view_refuses_a_node_with_no_draws(mc_batch: Path) -> None:
    with pytest.raises(EnsembleError, match="no drawn members"):
        dc.draws_view(draws_frames(mc_batch), "nope")


def test_pair_options_put_links_first(mc_batch: Path) -> None:
    kinds = [label.split("(")[1].split(",")[0] for label, _pair in dc.pair_options(draws_frames(mc_batch))]
    assert "implied" in kinds and kinds == sorted(kinds, key=lambda k: k != "link")


def test_pair_row_takes_either_order(mc_batch: Path) -> None:
    frames = draws_frames(mc_batch)
    a, b = dc.pair_options(frames)[0][1]
    row = dc.pair_row(frames, b, a)
    assert row is not None and {row["a"], row["b"]} == {a, b}
    assert dc.pair_row(frames, a, a) is None
