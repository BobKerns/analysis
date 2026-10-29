"""Guard rails on ensembles (#305): the series store's flows, each member's
rails, and the statistics across members."""
# cspell:ignore Δpast Δrms

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.sim import guard_rails as gr
from analysis.sim.connection import RunConnection
from analysis.sim.series_db import open_series
from analysis.sim.types import DollarMode


@pytest.fixture
def mc_rails(scenarios_dir: Path) -> list[gr.GuardRail]:
    """The prototype retire_alice's own rails (the batches run a copy of it)."""
    return gr.scenario_rails(scenarios_dir, "retire_alice")


@pytest.mark.slow
@pytest.mark.parametrize("mode", list(DollarMode))
def test_each_member_from_the_store_matches_its_run_file(
    mc_batch: Path, mc_rails: list[gr.GuardRail], mode: DollarMode,
) -> None:
    """The store keeps enough to rebuild every rail exactly as a member's
    own run file gives it."""
    results = gr.evaluate_ensemble(open_series(mc_batch).rail_pieces(), mc_rails, mode)
    assert [r.rail.name for r in results] == [r.name for r in mc_rails]
    for stem in results[0].stems:
        with RunConnection(mc_batch / f"{stem}.sqlite3") as rc:
            want = gr.evaluate_run(rc, mc_rails, mode)
        for got, expected in zip(results, want, strict=True):
            member = got.member(stem)
            assert member is not None
            for form in ("measure", "threshold", "margin", "cumulative", "carried"):
                np.testing.assert_allclose(
                    getattr(member.years, form)[0], getattr(expected.years, form)[0],
                    rtol=1e-12, atol=1e-6, equal_nan=True, err_msg=f"{stem} {got.rail.name} {form}")


def _ensemble(*args: str) -> Result:
    return CliRunner().invoke(main, ["sim", "ensemble", *args])


def test_share_past_ever_past_and_rms_across_members() -> None:
    """Two members, one year inside and one past: the shares count members
    the rail covers, and ever past looks back."""
    margin = np.array([[10.0, -30.0, 5.0], [-40.0, np.nan, np.nan]])
    years = np.array([2031, 2032, 2033])
    stacked = gr.measures.RailYears(years, margin, margin, margin, margin, margin)
    rail, _ = gr.parse_cells({"name": "r", "measure": "spending", "side": "above",
                              "threshold": "1000.00", "basis": "nominal"})
    assert rail is not None
    result = gr.EnsembleRailResult(rail, ["m0", "m1"], [0, 1], stacked)
    frame = gr.ensemble_rail_frame(result)
    assert frame["past"].tolist() == [0.5, 1.0, 0.0]
    assert frame["ever_past"].tolist() == [0.5, 1.0, 1.0]
    np.testing.assert_allclose(frame["rms_over"], [np.sqrt(40.0**2 / 2), 30.0, 0.0])
    summary = gr.ensemble_summary_frame([result]).set_index("form")
    assert summary.loc["annual", "past"] == pytest.approx(2 / 4)
    assert summary.loc["annual", "ever_past"] == 1.0
    assert summary.loc["annual", "past_at_end"] == pytest.approx(0.5)  # m0 ends inside, m1 past
    assert summary.loc["annual", "rms_over"] == pytest.approx(np.sqrt((30**2 + 40**2) / 4))


def _synthetic(name: str, margins: list[list[float]]) -> gr.EnsembleRailResult:
    margin = np.array(margins)
    years = np.arange(2031, 2031 + margin.shape[1])
    rail, _ = gr.parse_cells({"name": name, "measure": "spending", "side": "above",
                              "threshold": "1000.00", "basis": "nominal"})
    assert rail is not None
    return gr.EnsembleRailResult(rail, ["m0", "m1"], [0, 1],
                                 gr.measures.RailYears(years, *[margin] * 5))


def test_several_recipes_against_a_baseline_by_year_and_recipe() -> None:
    """Rows are (year, recipe), the variants in the order given; a rail only
    one variant shares has only its rows; the summary puts the baseline first
    with no change, then each recipe's change."""
    baseline = [_synthetic("a", [[10.0, -30.0], [5.0, 5.0]]), _synthetic("b", [[1.0, 1.0], [1.0, 1.0]])]
    worse = [_synthetic("a", [[-10.0, -30.0], [-5.0, 5.0]])]
    same = [_synthetic("a", [[10.0, -30.0], [5.0, 5.0]]), _synthetic("b", [[1.0, -1.0], [1.0, 1.0]])]
    results = {"base": baseline, "worse": worse, "same": same}
    tables = gr.ensemble_comparison_table(results, "base", ["worse", "same"])
    assert list(tables) == ["a", "b"]
    assert list(tables["a"].index) == [(2031, "worse"), (2031, "same"), (2032, "worse"),
                                       (2032, "same")]
    assert tables["a"].loc[(2031, "worse"), "Δpast"] == 1.0
    assert tables["a"].loc[(2031, "same"), "Δpast"] == 0.0
    assert list(tables["b"].index) == [(2031, "same"), (2032, "same")]

    summary = gr.ensemble_summary_comparison(results, "base", ["worse", "same"])
    annual_a = summary[(summary["rail"] == "a") & (summary["form"] == "annual")]
    assert annual_a["recipe"].tolist() == ["base", "worse", "same"]
    assert np.isnan(annual_a["Δpast"].iloc[0])
    assert annual_a["Δpast"].iloc[1] == pytest.approx(0.5)  # 3 of 4 member-years against 1
    assert annual_a["Δever_past"].iloc[2] == 0.0
    assert summary["rail"].tolist()[:3] == ["a"] * 3
    assert set(summary[summary["rail"] == "b"]["recipe"]) == {"base", "same"}


@pytest.mark.slow
def test_sim_ensemble_guard_rails_reports_each_rail(
    mc_batch: Path, test_data_dir: Path, runs_dir: Path, tmp_path: Path,
) -> None:
    result = _ensemble("guard-rails", "retire_alice", "--data-dir", str(test_data_dir),
                       "--runs-dir", str(runs_dir), "--graph-file", str(tmp_path / "rails.png"))
    assert result.exit_code == 0, result.output
    assert "8 member(s); guard rails, CPI-constant dollars" in result.output
    lines = result.output.splitlines()
    at = lines.index(next(line for line in lines if line.startswith("four_percent (negative)")))
    assert lines[at + 1].split() == ["year", "past", "ever_past", "rms_over", "past", "cum",
                                     "ever_past", "cum", "rms_over", "cum"]
    assert lines[at + 2].split()[:3] == ["2031", "100%", "100%"]  # the bridge years
    assert "Over the whole run:" in result.output
    assert (tmp_path / "rails.png").stat().st_size > 0

    one = _ensemble("guard-rails", "retire_alice", "--rail", "budget",
                    "--data-dir", str(test_data_dir), "--runs-dir", str(runs_dir))
    assert one.exit_code == 0, one.output
    assert "budget (negative)" in one.output and "four_percent (negative)" not in one.output
    bad = _ensemble("guard-rails", "retire_alice", "--rail", "nope",
                    "--data-dir", str(test_data_dir), "--runs-dir", str(runs_dir))
    assert bad.exit_code == 1 and "no guard rail 'nope'" in bad.output


@pytest.mark.slow
def test_sim_ensemble_guard_rails_reads_the_rails_as_they_are_now(
    mc_batch: Path, test_data_dir: Path, runs_dir: Path, write_guard_rails,
) -> None:
    """A lens: an edited rail shows at once, with no rerun; no rails, no table."""
    write_guard_rails({"name": "tight", "measure": "spending", "side": "above",
                       "threshold": "1.00", "basis": "nominal"})
    result = _ensemble("guard-rails", "retire_alice", "--data-dir", str(test_data_dir),
                       "--runs-dir", str(runs_dir))
    assert result.exit_code == 0, result.output
    assert "tight (negative)" in result.output and "budget" not in result.output
    write_guard_rails()
    none = _ensemble("guard-rails", "retire_alice", "--data-dir", str(test_data_dir),
                     "--runs-dir", str(runs_dir))
    assert none.exit_code == 0 and "no guard rails in 'retire_alice'" in none.output


@pytest.mark.slow
def test_sim_ensemble_compare_shows_each_rails_change(
    mc_batch: Path, test_data_dir: Path, runs_dir: Path,
) -> None:
    """Against itself every share and RMS changes by zero."""
    shutil.copytree(mc_batch.parent, runs_dir / "same")
    shutil.copytree(test_data_dir / "scenarios" / "retire_alice", test_data_dir / "scenarios" / "same")
    result = _ensemble("compare", "retire_alice", "same", "--data-dir", str(test_data_dir),
                       "--runs-dir", str(runs_dir))
    assert result.exit_code == 0, result.output
    assert "guard rails, same − retire_alice (CPI-constant dollars)" in result.output
    lines = result.output.splitlines()
    at = lines.index("four_percent:")
    assert lines[at + 1].split()[:4] == ["year", "Δpast", "Δever_past", "Δrms_over"]
    assert set(lines[at + 2].split()[1:]) <= {"+0%", "+0", "-0"}


@pytest.mark.slow
def test_the_ensemble_guard_rails_widget_shows_each_rail_and_rereads_them(
    mc_batch: Path, test_data_dir: Path, runs_dir: Path, write_guard_rails,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from analysis.sim.notebook_ui import ensemble_widgets
    from analysis.sim.notebook_ui.pickers import TimeRangePicker

    shown: list[Any] = []
    monkeypatch.setattr(ensemble_widgets, "display", lambda *args, **kwargs: shown.extend(args))
    time_range = TimeRangePicker()
    controls = ensemble_widgets.EnsembleControls(runs_dir=runs_dir, time_range=time_range)
    box = ensemble_widgets.ensemble_guard_rails_widget(
        data_dir=test_data_dir, runs_dir=runs_dir, controls=controls)
    dropdown, row, _loading, _out = box.children
    assert dropdown.value == "retire_alice"
    assert list(row.children) == [controls.dollar_picker, time_range]
    texts = [str(getattr(item, "data", "")) for item in shown]
    assert any(t.startswith("**four_percent** (negative)") for t in texts)
    tables = [item.data for item in shown if hasattr(item, "data") and hasattr(item.data, "columns")]
    assert list(tables[0].columns) == ["past", "ever_past", "rms_over", "past cum",
                                       "ever_past cum", "rms_over cum"]

    shown.clear()
    time_range.value = (2035, 2036)
    tables = [item.data for item in shown if hasattr(item, "data") and hasattr(item.data, "columns")]
    assert list(tables[0].index) == [2035, 2036]

    write_guard_rails()
    shown.clear()
    dropdown.value = None
    dropdown.value = "retire_alice"
    assert any("No guard rails in retire_alice" in str(getattr(item, "data", "")) for item in shown)


@pytest.mark.slow
def test_the_ensemble_guard_rails_table_widget_compares_recipes_by_year(
    mc_batch: Path, test_data_dir: Path, runs_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recipe sharing the baseline's seed against it: every change is zero,
    one row per (year, recipe), then the whole-run summary by recipe."""
    from analysis.sim.notebook_ui import ensemble_widgets
    from analysis.sim.notebook_ui.pickers import TimeRangePicker

    shutil.copytree(mc_batch.parent, runs_dir / "same")
    shutil.copytree(test_data_dir / "scenarios" / "retire_alice", test_data_dir / "scenarios" / "same")
    shown: list[Any] = []
    monkeypatch.setattr(ensemble_widgets, "display", lambda *args, **kwargs: shown.extend(args))
    time_range = TimeRangePicker()
    controls = ensemble_widgets.EnsembleControls(runs_dir=runs_dir, time_range=time_range)
    box = ensemble_widgets.ensemble_guard_rails_table_widget(
        data_dir=test_data_dir, runs_dir=runs_dir, controls=controls, default="retire_alice")
    pickers, row, _loading, _out = box.children
    baseline, variants = pickers.children
    assert baseline.value == "retire_alice" and "same" in variants.options
    assert list(row.children) == [controls.dollar_picker, time_range]
    assert any("Pick a baseline" in str(getattr(item, "data", "")) for item in shown)

    shown.clear()
    variants.value = ("same",)
    tables = [item.data for item in shown if hasattr(item, "data") and hasattr(item.data, "columns")]
    by_year, summary = tables[0], tables[-1]
    assert list(by_year.columns) == ["Δpast", "Δever_past", "Δrms_over", "Δpast cum",
                                     "Δever_past cum", "Δrms_over cum"]
    assert set(by_year.index.get_level_values("recipe")) == {"same"}
    assert (by_year.abs().fillna(0) < 1e-9).all().all()
    assert summary["recipe"].tolist()[:2] == ["retire_alice", "same"]
    assert summary["Δpast"].dropna().abs().max() < 1e-9

    shown.clear()
    time_range.value = (2035, 2036)
    tables = [item.data for item in shown if hasattr(item, "data") and hasattr(item.data, "columns")]
    assert sorted(set(tables[0].index.get_level_values("year"))) == [2035, 2036]

    # A recipe without rails: said so, and its store is never opened.
    from analysis.sim.registries import GUARD_RAILS_CSV_HEADER
    (test_data_dir / "scenarios" / "same" / "guard_rails.csv").write_text(
        ",".join(GUARD_RAILS_CSV_HEADER) + "\n")
    opened: list[str] = []
    real_open = ensemble_widgets._open_recipe
    monkeypatch.setattr(ensemble_widgets, "_open_recipe",
                        lambda runs, name, loading: opened.append(name) or real_open(runs, name, loading))
    shown.clear()
    time_range.value = (2035, 2037)
    assert opened == ["retire_alice"]
    assert any("No guard rails in `same`" in str(getattr(item, "data", "")) for item in shown)
