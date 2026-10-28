"""The guard rail chart (#305): one column per measure, annual over cumulative."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from analysis.sim import guard_rails as gr
from analysis.sim.connection import RunConnection
from analysis.sim.guard_rail_charts import guard_rail_figure, rail_colors


def test_a_column_per_measure_with_each_rails_lines(
    retire_alice_run: RunConnection, scenarios_dir: Path,
) -> None:
    results = gr.evaluate_run(retire_alice_run, gr.run_rails(retire_alice_run, scenarios_dir))
    fig = guard_rail_figure(results, "retire_alice")

    top_withdrawals, top_spending, cum_withdrawals, cum_spending = fig.axes
    assert top_withdrawals.get_title() == "Annual: withdrawals from the portfolio"
    assert top_spending.get_title() == "Annual: spending"
    labels = [line.get_label() for line in top_withdrawals.get_lines()]
    assert labels[:4] == ["withdrawals from the portfolio", "four_percent: above 4%",
                          "gk_upper: above 6%", "gk_lower: below 4% (positive)"]
    lines = {line.get_label(): line for line in cum_withdrawals.get_lines()}
    assert lines["gk_lower"].get_linestyle() == "--"  # positive: dashed
    assert lines["four_percent"].get_linestyle() == "-"
    assert lines["four_percent, carried at the portfolio's return"].get_linestyle() == ":"
    assert [line.get_label() for line in top_spending.get_lines()][1] == "budget: above $85,000"
    assert len(cum_spending.get_lines()) >= 2

    without = guard_rail_figure(results, "retire_alice", carried=False)
    assert not any("carried" in str(line.get_label()) for line in without.axes[2].get_lines())


def test_colors_follow_the_rails_sense(retire_alice_run: RunConnection, scenarios_dir: Path) -> None:
    results = gr.evaluate_run(retire_alice_run, gr.run_rails(retire_alice_run, scenarios_dir))
    colors = rail_colors(results)
    assert colors["four_percent"] != colors["gk_upper"]
    assert colors["gk_lower"] == "#2a78d6"  # the first positive rail: blue


def test_the_measure_spans_every_rail_sharing_it(retire_alice_run: RunConnection) -> None:
    """Rails on one measure but different years: the measure line covers
    them all, not just the first rail's."""
    early, _ = gr.parse_cells({"name": "early", "measure": "spending", "side": "above",
                               "threshold": "90000.00", "basis": "nominal", "end": "2033-12"})
    late, _ = gr.parse_cells({"name": "late", "measure": "spending", "side": "above",
                              "threshold": "90000.00", "basis": "nominal", "start": "2036-01"})
    assert early is not None and late is not None
    fig = guard_rail_figure(gr.evaluate_run(retire_alice_run, [early, late]), "two windows")
    measure = fig.axes[0].get_lines()[0]
    years = [int(label.get_text()) for label in fig.axes[1].get_xticklabels()]
    assert not any(np.isnan(np.asarray(measure.get_ydata(), dtype=float)))
    assert years[0] <= 2033 and years[-1] >= 2036
