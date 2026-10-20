"""Tests for analysis.sim.notebook_ui.compare_plots."""

from __future__ import annotations

from pathlib import Path

import matplotlib
import pandas as pd

matplotlib.use("Agg")

from click.testing import CliRunner

from analysis.cli import main
from analysis.sim.connection import RunConnection
from analysis.sim.types import DollarMode
from analysis.sim.notebook_ui import compare_plots as cp
from analysis.sim.notebook_ui.pickers import (
    DollarModePicker,
    MultiScenarioConnections,
    TimeRangePicker,
    run_years,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = REPO_ROOT / "prototype_data"


class _FakeRun:
    def __init__(self, values: list[float]) -> None:
        # An empty list is a run with no accounts: no net-worth rows at all.
        idx = pd.to_datetime(["2026-01-31", "2026-02-28"])[:len(values)]
        self._nw = pd.DataFrame({"nominal": values}, index=idx)
        self._nw.index.name = "month"

    def net_worth_frame(self) -> pd.DataFrame:
        return self._nw


def _fake_container(baseline_values, variant_values) -> MultiScenarioConnections:
    return MultiScenarioConnections(
        connections={  # ty: ignore[invalid-argument-type]  # net_worth_frame() is all they use
            "base": _FakeRun(baseline_values), "variant": _FakeRun(variant_values),
        },
        baseline="base", variants=["variant"], colors={"base": "C0", "variant": "C1"},
    )


def test_net_worth_by_month_and_variant_delta() -> None:
    container = _fake_container([10.0, 20.0], [12.0, 18.0])

    nw = cp.net_worth_by_month(container)
    delta = cp.variant_delta(nw, container)

    assert list(nw["base"]) == [10.0, 20.0]
    assert list(delta["variant"]) == [2.0, -2.0]


def test_net_worth_by_month_treats_a_scenario_without_rows_as_zero() -> None:
    container = _fake_container([10.0, 20.0], [])

    nw = cp.net_worth_by_month(container)
    summary = cp.breakeven_summary(nw, cp.variant_delta(nw, container), container)

    assert list(nw["variant"]) == [0.0, 0.0]
    assert summary.loc["variant", "deepest deficit"] == -20.0
    assert summary.loc["variant", "break-even"] == "never"


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


def _two_runs(retire_alice_run: RunConnection, variant_a_run: RunConnection,
              ) -> MultiScenarioConnections:
    # Built directly: the multi_connections fixture's picker finds no runs.
    return MultiScenarioConnections(
        connections={"retire_alice": retire_alice_run, "variant_a": variant_a_run},
        baseline="retire_alice", variants=["variant_a"],
        colors={"retire_alice": "C0", "variant_a": "C1"},
    )


def test_compare_widgets_redraw_on_a_shared_dollar_toggle(
    retire_alice_run: RunConnection, variant_a_run: RunConnection,
) -> None:
    multi_connections = _two_runs(retire_alice_run, variant_a_run)
    dollar_mode = DollarModePicker()

    cp.net_worth_compare_widget(multi_connections, dollar_mode=dollar_mode)
    cp.breakeven_widget(multi_connections, dollar_mode=dollar_mode, baseline="retire_alice")
    cp.year_end_widget(multi_connections, dollar_mode=dollar_mode, baseline="retire_alice")

    dollar_mode.value = DollarMode.CPI_CONSTANT  # redraws all three; must not raise


def test_compare_widgets_share_one_time_range_over_every_run(
    retire_alice_run: RunConnection, variant_a_run: RunConnection,
) -> None:
    multi_connections = _two_runs(retire_alice_run, variant_a_run)
    time_range = TimeRangePicker()

    cp.net_worth_compare_widget(multi_connections, time_range=time_range)
    cp.year_end_widget(multi_connections, time_range=time_range, baseline="retire_alice")

    years = run_years(*multi_connections.connections.values())
    assert (time_range.min, time_range.max) == (years[0], years[-1])
    time_range.value = (years[0], years[0])  # redraws both; must not raise


def _empty_compare(
    runs: tuple[RunConnection, RunConnection], baseline: str,
) -> MultiScenarioConnections:
    full, empty = runs
    connections = {"retire_alice": full, "empty": empty}
    return MultiScenarioConnections(
        connections=connections, baseline=baseline,
        variants=[name for name in connections if name != baseline],
        colors={"retire_alice": "C0", "empty": "C1"},
    )


def test_breakeven_widget_compares_against_an_empty_scenario(
    empty_scenario_runs: tuple[RunConnection, RunConnection],
) -> None:
    for baseline in ("retire_alice", "empty"):
        container = _empty_compare(empty_scenario_runs, baseline)
        cp.breakeven_widget(container, baseline=baseline)
        for mode in DollarMode:
            nw = cp.net_worth_by_month(container, mode)
            assert (nw["empty"] == 0).all()


def test_account_balance_diffs_show_accounts_an_empty_scenario_lacks_as_zero(
    empty_scenario_runs: tuple[RunConnection, RunConnection],
) -> None:
    full, empty = empty_scenario_runs
    for baseline_rc, variant, variant_rc in ((full, "empty", empty), (empty, "retire_alice", full)):
        _, _, diffs = cp.account_balance_diffs(
            baseline="", baseline_rc=baseline_rc, variant_rcs={variant: variant_rc},
        )
        assert "Asset_Checking" in diffs
        frame = diffs["Asset_Checking"]
        zero_side = variant if variant_rc is empty else "baseline"
        assert (frame[zero_side] == 0).all()
        dollar_mode = DollarModePicker()
        cp.show_account_balance_diffs(
            None, baseline="", baseline_rc=baseline_rc, variant_rcs={variant: variant_rc},
            dollar_mode=dollar_mode,
        )
        dollar_mode.value = DollarMode.CPI_CONSTANT  # redraws; the empty run has no CPI rows
