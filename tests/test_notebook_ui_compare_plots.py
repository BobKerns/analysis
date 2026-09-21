"""Tests for analysis.sim.notebook_ui.compare_plots."""

from __future__ import annotations

import shutil
from pathlib import Path

import matplotlib
import pandas as pd

from tests.conftest import multi_connections

matplotlib.use("Agg")

from click.testing import CliRunner

from analysis.cli import main
from analysis.sim.notebook_ui import compare_plots as cp
from analysis.sim.notebook_ui.pickers import (
    DollarModePicker,
    MultiScenarioConnections,
    multi_scenario_picker,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = REPO_ROOT / "prototype_data"


class _FakeRun:
    def __init__(self, values: list[float]) -> None:
        idx = pd.to_datetime(["2026-01-31", "2026-02-28"])
        self._nw = pd.DataFrame({"nominal": values}, index=idx)
        self._nw.index.name = "month"

    def net_worth_frame(self) -> pd.DataFrame:
        return self._nw


def _fake_container(baseline_values, variant_values) -> MultiScenarioConnections:
    return MultiScenarioConnections(
        connections={"base": _FakeRun(baseline_values), "variant": _FakeRun(variant_values)},
        baseline="base", variants=["variant"], colors={"base": "C0", "variant": "C1"},
    )


def test_net_worth_by_month_and_variant_delta() -> None:
    container = _fake_container([10.0, 20.0], [12.0, 18.0])

    nw = cp.net_worth_by_month(container)
    delta = cp.variant_delta(nw, container)

    assert list(nw["base"]) == [10.0, 20.0]
    assert list(delta["variant"]) == [2.0, -2.0]


def test_breakeven_month_never_ahead_returns_none() -> None:
    idx = pd.date_range("2026-01-31", periods=3, freq="ME")
    series = pd.Series([-1.0, -2.0, -3.0], index=idx)

    assert cp.breakeven_month(series) is None


def test_breakeven_month_finds_first_month_that_stays_ahead() -> None:
    idx = pd.date_range("2026-01-31", periods=4, freq="ME")
    series = pd.Series([-1.0, 1.0, -1.0, 1.0], index=idx)

    assert cp.breakeven_month(series) == idx[3]


def test_breakeven_summary_reports_deficit_and_final_values() -> None:
    idx = pd.date_range("2026-01-31", periods=3, freq="ME")
    nw = pd.DataFrame({"base": [100.0, 100.0, 100.0], "variant": [90.0, 95.0, 105.0]}, index=idx)
    container = MultiScenarioConnections({}, "base", ["variant"], {})
    delta = cp.variant_delta(nw, container)

    summary = cp.breakeven_summary(nw, delta, container)

    assert summary.loc["variant", "final net worth"] == 105.0
    assert summary.loc["variant", "deepest deficit"] == -10.0


def test_filter_comparisons_drops_pairs_with_missing_scenarios() -> None:
    comparisons = {"group": {"a vs b": ("a", "b"), "a vs c": ("a", "c")}}

    result = cp.filter_comparisons(comparisons, ["a", "b"])

    assert result == {"group": {"a vs b": ("a", "b")}}


def test_comparison_frame_computes_pairwise_differences() -> None:
    idx = pd.date_range("2026-01-31", periods=2, freq="ME")
    nw = pd.DataFrame({"a": [10.0, 20.0], "b": [4.0, 5.0]}, index=idx)
    comparisons = {"group": {"a minus b": ("a", "b")}}

    frame = cp.comparison_frame(nw, comparisons)

    assert list(frame["a minus b"]) == [6.0, 15.0]


def test_has_any_comparisons() -> None:
    assert cp.has_any_comparisons({"group": {"label": ("a", "b")}})
    assert not cp.has_any_comparisons({"group": {}})
    assert not cp.has_any_comparisons({})


def test_comparison_frame_keeps_index_when_no_comparisons_survive_filtering() -> None:
    from analysis.sim.connection import year_end_values

    idx = pd.date_range("2026-01-31", periods=2, freq="ME")
    nw = pd.DataFrame({"a": [10.0, 20.0], "b": [4.0, 5.0]}, index=idx)
    comparisons = cp.filter_comparisons({"group": {"a minus c": ("a", "c")}}, ["a", "b"])

    frame = cp.comparison_frame(nw, comparisons)

    assert not cp.has_any_comparisons(comparisons)
    assert list(frame.columns) == []
    assert list(frame.index) == list(idx)
    year_end_values(frame)  # must not raise


def test_warning_html_escapes_and_formats() -> None:
    from analysis.sim.notebook_ui.pickers import warning_html

    html = warning_html("run `analysis sim load <name>` -- **bold**")
    assert "&lt;name&gt;" in html
    assert "<strong>bold</strong>" in html


# ---------- integration: real scenarios + widget wiring ----------

def _load_and_run(intent_dir: Path, state_dir: Path, data_dir: Path, runs_dir: Path, scenario: str) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["sim", "load", scenario, "--data-dir", str(data_dir),
         "--intent-dir", str(intent_dir), "--runs-dir", str(runs_dir),
         "--allow-dirty-data"],
    )
    assert result.exit_code == 0, result.output


def _create_scenario(data_dir: Path, name: str, parent: str) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main, ["sim", "scenario", "create", name, parent, "--data-dir", str(data_dir)],
    )
    assert result.exit_code == 0, result.output


def test_comparability_widget_renders_without_error(multi_connections: MultiScenarioConnections) -> None:
    cp.comparability_widget(multi_connections)


def test_net_worth_compare_widget_renders_without_error(multi_connections: MultiScenarioConnections) -> None:
    widget = cp.net_worth_compare_widget(multi_connections)
    assert widget is not None


def test_year_end_widget_includes_delta_column(multi_connections: MultiScenarioConnections) -> None:
    widget = cp.year_end_widget(multi_connections, baseline="retire_alice")
    assert widget is not None


def test_compare_widgets_share_dollar_mode_observers(multi_connections: MultiScenarioConnections) -> None:
    dollar_mode = DollarModePicker()

    cp.net_worth_compare_widget(multi_connections, dollar_mode=dollar_mode)
    cp.breakeven_widget(multi_connections, dollar_mode=dollar_mode, baseline="retire_alice")
    cp.year_end_widget(multi_connections, dollar_mode=dollar_mode, baseline="retire_alice")

    assert set(dollar_mode._compare_plot_redraws) == {
        "net_worth", "breakeven", "year_end",
    }
