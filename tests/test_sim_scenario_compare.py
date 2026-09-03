"""Tests for the scenario_compare notebook's picker/loading/plotting logic."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from analysis.sim.scenario_compare import (
    available_scenarios,
    breakeven_month,
    breakeven_summary,
    comparison_frame,
    filter_comparisons,
    load_scenarios,
    net_worth_by_month,
    render_load_or_warn,
    variant_delta,
    year_end_values,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = REPO_ROOT / "prototype_data"


def test_available_scenarios_lists_sorted_folder_names(tmp_path: Path) -> None:
    for name in ("january", "current", "current-cut"):
        (tmp_path / "scenarios" / name).mkdir(parents=True)

    assert available_scenarios(tmp_path) == ["current", "current-cut", "january"]


def test_load_scenarios_requires_at_least_one_variant() -> None:
    result = load_scenarios(PROTOTYPE, "retire_alice", [])

    assert not result.ok
    assert "at least one variant" in result.warnings[0]


def test_load_scenarios_rejects_too_many_variants() -> None:
    result = load_scenarios(PROTOTYPE, "retire_alice", ["a", "b", "c", "d", "e"], max_variants=4)

    assert not result.ok
    assert "at most 4" in result.warnings[0]


def test_render_load_or_warn_returns_none_when_not_ok() -> None:
    assert render_load_or_warn(PROTOTYPE, "retire_alice", []) is None


def test_load_scenarios_warns_when_baseline_has_no_run(tmp_path: Path) -> None:
    (tmp_path / "scenarios" / "no_run_scenario").mkdir(parents=True)

    result = load_scenarios(tmp_path, "no_run_scenario", ["no_run_scenario"])

    assert not result.ok
    assert "No run for baseline" in result.warnings[0]


def test_year_end_values_keeps_last_row_per_year() -> None:
    idx = pd.date_range("2026-01-31", periods=15, freq="ME")
    series = pd.Series(range(15), index=idx)

    result = year_end_values(series)

    assert list(result.index) == [2026, 2027]
    assert result.loc[2026] == 11  # Dec 2026 is the 12th month (index 11)


def test_breakeven_month_never_ahead_returns_none() -> None:
    idx = pd.date_range("2026-01-31", periods=3, freq="ME")
    series = pd.Series([-1.0, -2.0, -3.0], index=idx)

    assert breakeven_month(series) is None


def test_breakeven_month_finds_first_month_that_stays_ahead() -> None:
    idx = pd.date_range("2026-01-31", periods=4, freq="ME")
    series = pd.Series([-1.0, 1.0, -1.0, 1.0], index=idx)

    assert breakeven_month(series) == idx[3]


def test_net_worth_by_month_and_variant_delta() -> None:
    class _FakeRun:
        def __init__(self, values: list[float]) -> None:
            self._values = values

        def items(self, name: str) -> pd.DataFrame:
            assert name == "net_worth"
            return pd.DataFrame({
                "month": ["2026-01", "2026-02"],
                "net_worth": [v * 100 for v in self._values],
            })

    runs = {"base": _FakeRun([10.0, 20.0]), "variant": _FakeRun([12.0, 18.0])}

    nw = net_worth_by_month(runs, ["base", "variant"])
    delta = variant_delta(nw, "base", ["variant"])

    assert list(nw["base"]) == [10.0, 20.0]
    assert list(delta["variant"]) == [2.0, -2.0]


def test_breakeven_summary_reports_deficit_and_final_values() -> None:
    idx = pd.date_range("2026-01-31", periods=3, freq="ME")
    nw = pd.DataFrame({"base": [100.0, 100.0, 100.0], "variant": [90.0, 95.0, 105.0]}, index=idx)
    delta = variant_delta(nw, "base", ["variant"])

    summary = breakeven_summary(nw, delta, "base", ["variant"])

    assert summary.loc["variant", "final net worth"] == 105.0
    assert summary.loc["variant", "deepest deficit"] == -10.0


def test_filter_comparisons_drops_pairs_with_missing_scenarios() -> None:
    comparisons = {"group": {"a vs b": ("a", "b"), "a vs c": ("a", "c")}}

    result = filter_comparisons(comparisons, ["a", "b"])

    assert result == {"group": {"a vs b": ("a", "b")}}


def test_comparison_frame_computes_pairwise_differences() -> None:
    idx = pd.date_range("2026-01-31", periods=2, freq="ME")
    nw = pd.DataFrame({"a": [10.0, 20.0], "b": [4.0, 5.0]}, index=idx)
    comparisons = {"group": {"a minus b": ("a", "b")}}

    frame = comparison_frame(nw, comparisons)

    assert list(frame["a minus b"]) == [6.0, 15.0]
