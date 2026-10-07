"""Tests for analysis.sim.notebook_ui.pickers."""

from __future__ import annotations

import shutil
from pathlib import Path

from click.testing import CliRunner

from analysis.cli import main
from analysis.sim.connection import RunConnection, ScenarioConnection
from analysis.sim.notebook_ui.pickers import (
    MultiScenarioConnections,
    display_warning,
    multi_scenario_picker,
    scenario_picker,
    warning_html,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = REPO_ROOT / "prototype_data"


def _copy_prototype(tmp_path: Path) -> Path:
    dest = tmp_path / "data"
    shutil.copytree(PROTOTYPE, dest)
    return dest


def _state_dirs(data_dir: Path) -> tuple[Path, Path]:
    """Match `resolve_scenario_name`'s default: state/ lives beside data_dir."""
    state = data_dir.parent / "state"
    return state / "intent", state / "runs"


def _load_and_run(data_dir: Path, scenario: str) -> None:
    intent_dir, runs_dir = _state_dirs(data_dir)
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


def test_scenario_picker_opens_scenario_and_run_connections(tmp_path: Path) -> None:
    data = _copy_prototype(tmp_path)
    _load_and_run(data, "retire_alice")

    widget, container, dropdown = scenario_picker(data, "retire_alice")

    assert isinstance(container.scenario, ScenarioConnection)
    assert isinstance(container.run, RunConnection)
    from ipywidgets import Dropdown
    assert isinstance(dropdown, Dropdown)
    assert container.scenario_ref.name == "retire_alice"
    assert dropdown.value == "retire_alice"
    container.close()


def test_scenario_picker_switches_scenarios_closing_the_old_connections(
    data_dir: Path,
    scenarios_dir: Path,
    runs_dir: Path,
    retire_alice_run_file: Path,
    variant_a_run_file: Path,
) -> None:

    widget, container, dropdown = scenario_picker(data_dir, "retire_alice",
                                                  scenarios_dir=scenarios_dir,
                                                  runs_dir=runs_dir)
    old_scenario, old_run = container.scenario, container.run

    # # Keep the test independent of the layout's title/description children.
    # dropdown = next(
    #     child
    #     for child in widget.children
    #     if hasattr(child, "options") and hasattr(child, "value")
    # )
    dropdown.value = "variant_a"

    assert container.scenario_ref.name == "variant_a"
    assert container.scenario is not old_scenario
    assert container.run is not old_run
    container.close()


def test_scenario_picker_require_run_false_allows_missing_run(tmp_path: Path) -> None:
    data = _copy_prototype(tmp_path)
    intent_dir, runs_dir = _state_dirs(data)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["sim", "load", "retire_alice", "--data-dir", str(data),
         "--intent-dir", str(intent_dir), "--runs-dir", str(runs_dir),
         "--allow-dirty-data", "--no-run"],
    )
    assert result.exit_code == 0, result.output

    widget, container, dropdown = scenario_picker(data, "retire_alice", require_run=False)
    assert container.run is None
    container.close()


def test_scenario_picker_require_run_true_raises_when_missing(tmp_path: Path) -> None:
    import pytest

    data = _copy_prototype(tmp_path)
    intent_dir, runs_dir = _state_dirs(data)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["sim", "load", "retire_alice", "--data-dir", str(data),
         "--intent-dir", str(intent_dir), "--runs-dir", str(runs_dir),
         "--allow-dirty-data", "--no-run"],
    )
    assert result.exit_code == 0, result.output

    with pytest.raises(FileNotFoundError):
        scenario_picker(data, "retire_alice", require_run=True)


def _create_and_run_ensemble(data_dir: Path, name: str, parent: str, size: int) -> None:
    intent_dir, runs_dir = _state_dirs(data_dir)
    intent_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.mkdir(parents=True, exist_ok=True)
    runner = CliRunner()
    result = runner.invoke(
        main, ["sim", "ensemble", "create", name, parent,
               "--size", str(size), "--data-dir", str(data_dir)],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        main, ["sim", "calendar", "add", "set-rate", "2027-02-28", "Asset_Checking", "4.5",
               "--scenario", name, "--data-dir", str(data_dir),
               "--name", "checking_rate_vary", "--vary-target", "amount",
               "--vary-distribution", "uniform", "--vary-low", "4.0", "--vary-high", "5.0"],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        main, ["sim", "run", name, "--data-dir", str(data_dir),
               "--intent-dir", str(intent_dir), "--runs-dir", str(runs_dir),
               "--allow-dirty-data"],
    )
    assert result.exit_code == 0, result.output


def test_scenario_picker_shows_a_member_dropdown_for_an_ensemble_recipe(tmp_path: Path) -> None:
    data = _copy_prototype(tmp_path)
    _create_and_run_ensemble(data, "recipe_1", "retire_alice", size=3)

    widget, container, _dropdown = scenario_picker(data, "recipe_1")
    _scenario_dropdown, member_dropdown, _ref_out = widget.children
    assert member_dropdown.layout.display is None
    assert [v for _label, v in member_dropdown.options] == [
        "member_0000", "member_0001", "member_0002",
    ]
    assert member_dropdown.value == "member_0000"
    assert container.run is not None
    assert container.run.run_path.stem == "member_0000"
    container.close()


def test_scenario_picker_hides_the_member_dropdown_for_an_ordinary_scenario(
    tmp_path: Path,
) -> None:
    data = _copy_prototype(tmp_path)
    _load_and_run(data, "retire_alice")

    widget, container, _dropdown = scenario_picker(data, "retire_alice")
    _scenario_dropdown, member_dropdown, _ref_out = widget.children
    assert member_dropdown.layout.display == "none"
    assert member_dropdown.options == ()
    container.close()


def test_scenario_picker_hides_the_member_dropdown_when_require_run_is_false(
    tmp_path: Path,
) -> None:
    """An ensemble recipe's own intent (what require_run=False looks at) is
    the unresolved recipe, identical regardless of member -- member
    resolution happens per-run, not in the shared scenario-level intent."""
    data = _copy_prototype(tmp_path)
    _create_and_run_ensemble(data, "recipe_1", "retire_alice", size=2)

    widget, container, _dropdown = scenario_picker(data, "recipe_1", require_run=False)
    _scenario_dropdown, member_dropdown, _ref_out = widget.children
    assert member_dropdown.layout.display == "none"
    container.close()


def test_scenario_picker_selecting_a_member_switches_the_run_connection(
    tmp_path: Path,
) -> None:
    data = _copy_prototype(tmp_path)
    _create_and_run_ensemble(data, "recipe_1", "retire_alice", size=2)

    widget, container, _dropdown = scenario_picker(data, "recipe_1")
    _scenario_dropdown, member_dropdown, _ref_out = widget.children
    old_run = container.run

    member_dropdown.value = "member_0001"

    assert container.run is not None
    assert container.run.run_path.stem == "member_0001"
    assert container.run is not old_run
    container.close()


def test_scenario_picker_switching_away_from_an_ensemble_hides_the_member_dropdown(
    tmp_path: Path,
) -> None:
    data = _copy_prototype(tmp_path)
    _create_and_run_ensemble(data, "recipe_1", "retire_alice", size=2)
    _load_and_run(data, "retire_alice")

    widget, _container, dropdown = scenario_picker(data, "recipe_1")
    _scenario_dropdown, member_dropdown, _ref_out = widget.children
    assert member_dropdown.layout.display is None

    dropdown.value = "retire_alice"

    assert member_dropdown.layout.display == "none"


def test_multi_scenario_picker_requires_at_least_one_variant(tmp_path: Path) -> None:
    data = _copy_prototype(tmp_path)
    _load_and_run(data, "retire_alice")

    widget, container = multi_scenario_picker(data, "retire_alice", [])
    # An empty default still preselects the other scenarios (here the
    # prototype's `base`), so clear the selection the way a user would.
    variant_picker = widget.children[0].children[1]
    variant_picker.value = ()

    assert isinstance(container, MultiScenarioConnections)
    assert not container.ok
    assert "at least one variant" in container.warnings[0]


def test_warning_html_converts_markdown_lite_and_escapes_brackets() -> None:
    html = warning_html("run `analysis sim load <name>` -- **bold**")
    assert "<code>analysis sim load &lt;name&gt;</code>" in html
    assert "<strong>bold</strong>" in html
    assert "background-color:#fff8c5" in html


def test_display_warning_does_not_raise() -> None:
    display_warning("just a message")
