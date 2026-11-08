"""A load's warnings, kept with the intent and the run snapshot, and read
back by `sim explain` and the notebooks (#432)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim import load_warnings
from analysis.sim.connection import RunConnection
from analysis.sim.loader import LoadResult
from analysis.sim.notebook_ui import pickers
from analysis.sim.notebook_ui.ensemble_widgets import batch_load_warnings, load_warnings_message

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


def _shown_html(shown: list[object]) -> str:
    return "\n".join(str(getattr(item, "data", "")) for item in shown)


@pytest.mark.usefixtures("state_tax_without_its_deduction")
def test_a_run_notebooks_picker_counts_the_warnings_in_one_line(
    sim_load_test_copy: Callable[..., Path], test_data_dir: Path, runs_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sim_load_test_copy()
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
    sim_load_test_copy: Callable[..., Path], test_data_dir: Path, runs_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sim_load_test_copy("--no-run")
    shown: list[object] = []
    monkeypatch.setattr(pickers, "display", shown.append)
    _widget, container, _dropdown = pickers.scenario_picker(
        test_data_dir, "retire_alice", runs_dir=runs_dir, require_run=False,
        list_load_warnings=True)
    try:
        text = _shown_html(shown)
        assert "1 load warning.</strong>" in text and DEDUCTION_WARNING in text
        assert "<details><summary>1 thing defined but not read yet" in text
        assert "latent_factors have no dependents yet" in text
    finally:
        container.close()


@pytest.mark.parametrize(("require_run", "show_load_warnings"), [(False, True), (True, False)])
@pytest.mark.usefixtures("state_tax_without_its_deduction")
def test_other_pickers_dont_list_the_warnings(
    require_run: bool, show_load_warnings: bool, sim_load_test_copy: Callable[..., Path],
    test_data_dir: Path, runs_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only scenario_overview lists them (ADR-004 §6): an intent notebook's
    picker counts its run's, and show_load_warnings=False hides even that."""
    sim_load_test_copy()
    shown: list[object] = []
    monkeypatch.setattr(pickers, "display", shown.append)
    _widget, container, _dropdown = pickers.scenario_picker(
        test_data_dir, "retire_alice", runs_dir=runs_dir, require_run=require_run,
        show_load_warnings=show_load_warnings)
    try:
        text = _shown_html(shown)
        assert DEDUCTION_WARNING not in text and "defined but not read yet" not in text
        assert ("1 load warning</strong>" in text) == show_load_warnings
    finally:
        container.close()


def test_pickers_show_nothing_for_a_load_without_warnings(retire_alice_run_file: Path) -> None:
    with RunConnection(retire_alice_run_file) as rc:
        assert pickers.load_warnings_line(rc, "retire_alice") is None
    assert pickers.load_warnings_line(None) is None
    assert pickers.load_warnings_html(load_warnings.LoadWarnings()) is None
    assert load_warnings_message(
        batch_load_warnings(retire_alice_run_file.parent, [retire_alice_run_file.stem])) is None


@pytest.mark.usefixtures("state_tax_without_its_deduction")
def test_the_ensemble_widget_and_the_compare_picker_count_them(
    run_test_copy: Callable[[], Path], sim_load_test_copy: Callable[..., Path],
    test_data_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = run_test_copy()
    message = load_warnings_message(batch_load_warnings(run.parent, [run.stem]))
    assert message is not None
    assert message.startswith("**1 load warning** when the recipe was loaded")
    assert "sim explain --runfile" in message

    for scenario in ("retire_alice", "base"):
        sim_load_test_copy(scenario=scenario)
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


def test_a_pools_widget_counts_each_components_warnings(fake_state: Callable[..., Path]) -> None:
    """A pool's members come from several recipes (ADR-009): each
    component's first member speaks for it, not just member 0."""
    batch = fake_state("batch", "pool_ab", "2026-10-01T120000Z", members=4,
                       pool=["a", "b", "a", "b"], load_warned={0: [], 1: ["b warned"], 3: ["b again"]})
    read = batch_load_warnings(batch, [f"member_{i:04d}" for i in range(4)])
    assert [(component, path.stem) for component, path, _found in read] == [
        ("a", "member_0000"), ("b", "member_0001")]
    message = load_warnings_message(read)
    assert message is not None
    assert message.startswith("**Load warnings** when the pool's components were loaded: b: 1 load warning")
    assert "member_0001.sqlite3" in message and "a:" not in message


def test_a_missing_member_leaves_out_its_warnings(fake_state: Callable[..., Path]) -> None:
    """A pruned or locked member file costs its warning line, not the chart."""
    batch = fake_state("batch", "alice", "2026-10-02T120000Z", members=2, complete=False)
    assert batch_load_warnings(batch, ["member_0001"]) == ()
