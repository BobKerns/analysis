"""A load's warnings, kept with the intent and the run snapshot, and read
back by `sim explain` and the notebooks (#432)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim import load_warnings
from analysis.sim.connection import RunConnection
from analysis.sim.loader import LoadResult
from analysis.sim.notebook_ui import pickers
from analysis.sim.notebook_ui.ensemble_widgets import load_warnings_message

DEDUCTION_WARNING = "state tax uses the federal standard deduction"


@pytest.mark.usefixtures("state_tax_without_its_deduction")
def test_the_intent_and_its_run_keep_the_loads_warnings_in_order(
    load_test_copy: Callable[[], LoadResult], run_test_copy: Callable[[], Path],
) -> None:
    result = load_test_copy()
    assert any(DEDUCTION_WARNING in w for w in result.warnings)
    assert result.unused  # the prototype's idle latent factors
    recorded = load_warnings.read_file(result.intent_path)
    assert recorded == load_warnings.LoadWarnings(tuple(result.warnings), tuple(result.unused))
    assert load_warnings.read_file(run_test_copy()) == recorded


def test_a_clean_load_records_no_warnings(retire_alice_run_file: Path) -> None:
    """retire_alice loads with no warnings, only its one unused note."""
    found = load_warnings.read_file(retire_alice_run_file)
    assert found.warnings == () and len(found.unused) == 1


def test_a_file_from_before_the_table_reads_as_none(tmp_path: Path) -> None:
    old = tmp_path / "old.sqlite3"
    sqlite3.connect(old).close()
    found = load_warnings.read_file(old)
    assert found == load_warnings.LoadWarnings() and not found


@pytest.mark.usefixtures("state_tax_without_its_deduction")
def test_sim_explain_lists_the_loads_warnings(run_test_copy: Callable[[], Path]) -> None:
    run = run_test_copy()
    result = CliRunner().invoke(main, ["sim", "explain", str(run)])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    start = lines.index("load warnings (1):")
    assert DEDUCTION_WARNING in lines[start + 1]
    assert lines[start + 2] == "defined but not read yet (1):"
    assert "latent_factors have no dependents yet" in lines[start + 3]


def test_sim_explain_says_when_a_load_had_no_warnings(retire_alice_run_file: Path) -> None:
    result = CliRunner().invoke(main, ["sim", "explain", str(retire_alice_run_file)])
    assert result.exit_code == 0, result.output
    assert "load warnings: none" in result.output


def _load(data_dir: Path, intent_dir: Path, runs_dir: Path, *extra: str,
          scenario: str = "retire_alice") -> None:
    loaded = CliRunner().invoke(main, [
        "sim", "load", scenario, "--data-dir", str(data_dir),
        "--intent-dir", str(intent_dir), "--runs-dir", str(runs_dir), "--allow-dirty-data",
        *extra,
    ])
    assert loaded.exit_code == 0, loaded.output


def _shown_html(shown: list[object]) -> str:
    return "\n".join(str(getattr(item, "data", "")) for item in shown)


@pytest.mark.usefixtures("state_tax_without_its_deduction")
def test_a_run_notebooks_picker_counts_the_warnings_in_one_line(
    test_data_dir: Path, intent_dir: Path, runs_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _load(test_data_dir, intent_dir, runs_dir)
    shown: list[object] = []
    monkeypatch.setattr(pickers, "display", shown.append)
    _widget, container, _dropdown = pickers.scenario_picker(
        test_data_dir, "retire_alice", runs_dir=runs_dir)
    try:
        text = _shown_html(shown)
        assert "1 load warning</strong> when this run" in text
        assert "scenario_overview.ipynb" in text and "sim explain retire_alice" in text
        assert DEDUCTION_WARNING not in text  # counted, not listed
    finally:
        container.close()


@pytest.mark.usefixtures("state_tax_without_its_deduction")
def test_scenario_overviews_picker_lists_the_warnings_and_folds_the_unused(
    test_data_dir: Path, intent_dir: Path, runs_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _load(test_data_dir, intent_dir, runs_dir, "--no-run")
    shown: list[object] = []
    monkeypatch.setattr(pickers, "display", shown.append)
    _widget, container, _dropdown = pickers.scenario_picker(
        test_data_dir, "retire_alice", runs_dir=runs_dir, require_run=False)
    try:
        text = _shown_html(shown)
        assert "1 load warning.</strong>" in text and DEDUCTION_WARNING in text
        assert "<details><summary>1 thing defined but not read yet" in text
        assert "latent_factors have no dependents yet" in text
    finally:
        container.close()


def test_pickers_show_nothing_for_a_load_without_warnings(retire_alice_run_file: Path) -> None:
    with RunConnection(retire_alice_run_file) as rc:
        assert pickers.load_warnings_line(rc, "retire_alice") is None
    assert pickers.load_warnings_line(None) is None
    assert pickers.load_warnings_html(load_warnings.LoadWarnings()) is None
    assert load_warnings_message(retire_alice_run_file) is None


@pytest.mark.usefixtures("state_tax_without_its_deduction")
def test_the_ensemble_widget_and_the_compare_picker_count_them(
    run_test_copy: Callable[[], Path], test_data_dir: Path, intent_dir: Path, runs_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = load_warnings_message(run_test_copy())
    assert message is not None
    assert message.startswith("**1 load warning** when the recipe was loaded")
    assert "sim explain --runfile" in message

    for scenario in ("retire_alice", "base"):
        _load(test_data_dir, intent_dir, runs_dir, scenario=scenario)
    shown: list[str] = []
    monkeypatch.setattr(pickers, "display_warning",
                        lambda message, details=(): shown.append("\n".join([message, *details])))
    _widget, container = pickers.multi_scenario_picker(test_data_dir, "retire_alice", ["base"])
    try:
        (warned,) = [m for m in shown if m.startswith("**Load warnings**")]
        # base has no state tax rule, so nothing to warn of.
        assert warned.splitlines()[1:] == ["retire_alice: 1 load warning (sim explain retire_alice)"]
    finally:
        for rc in container.connections.values():
            rc.close()
