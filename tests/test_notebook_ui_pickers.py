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
         "--data-mode", "prototype", "--allow-dirty-data"],
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

    widget, container = scenario_picker(data, "retire_alice")

    assert isinstance(container.scenario, ScenarioConnection)
    assert isinstance(container.run, RunConnection)
    assert container.scenario_ref.name == "retire_alice"
    container.close()


def test_scenario_picker_switches_scenarios_closing_the_old_connections(
    tmp_path: Path,
) -> None:
    data = _copy_prototype(tmp_path)
    _load_and_run(data, "retire_alice")
    _create_scenario(data, "second", "retire_alice")
    _load_and_run(data, "second")

    widget, container = scenario_picker(data, "retire_alice")
    old_scenario, old_run = container.scenario, container.run

    dropdown = widget.children[0]
    dropdown.value = "second"

    assert container.scenario_ref.name == "second"
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
         "--data-mode", "prototype", "--allow-dirty-data", "--no-run"],
    )
    assert result.exit_code == 0, result.output

    widget, container = scenario_picker(data, "retire_alice", require_run=False)
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
         "--data-mode", "prototype", "--allow-dirty-data", "--no-run"],
    )
    assert result.exit_code == 0, result.output

    with pytest.raises(FileNotFoundError):
        scenario_picker(data, "retire_alice", require_run=True)


def test_multi_scenario_picker_requires_at_least_one_variant(tmp_path: Path) -> None:
    data = _copy_prototype(tmp_path)
    _load_and_run(data, "retire_alice")

    widget, container = multi_scenario_picker(data, "retire_alice", [])

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
