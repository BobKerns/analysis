"""#322: the working set, the scenarios most recently used first, which the
background builder builds in that order."""

from __future__ import annotations

from pathlib import Path

import ipywidgets as widgets
import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.sim import working_set
from analysis.sim.loader import current_scenario_path
from analysis.sim.locks import BUILDER_CHILD_ENV
from analysis.sim.notebook_ui.pickers import note_picks
from analysis.sim.working_set import read_working_set, working_set_path

pytestmark = pytest.mark.uses_working_set_write


def _sim(data_dir: Path, *args: str) -> Result:
    return CliRunner().invoke(main, ["sim", *args, "--data-dir", str(data_dir)])


def _scenarios(data_dir: Path, *names: str) -> None:
    for name in names:
        (data_dir / "scenarios" / name).mkdir()


def test_use_moves_scenarios_to_the_top_first_named_topmost(test_data_dir: Path) -> None:
    _scenarios(test_data_dir, "a", "b")
    working_set.use(test_data_dir, "a")
    working_set.use(test_data_dir, "base", "b")
    working_set.use(test_data_dir, "a")
    assert read_working_set(test_data_dir) == ["a", "base", "b"]


def test_an_update_drops_missing_and_disabled_scenarios(test_data_dir: Path) -> None:
    _scenarios(test_data_dir, "a", "b")
    working_set_path(test_data_dir).write_text("gone\na\nb\nbase\n")
    (test_data_dir / "scenarios" / "b" / ".disable").write_text("")
    working_set.use(test_data_dir, "retire_alice", "nope", None)
    assert read_working_set(test_data_dir) == ["retire_alice", "a", "base"]


def test_the_working_set_keeps_the_most_recent_names(
    test_data_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(working_set, "LIMIT", 2)
    working_set.use(test_data_dir, "retire_alice", "base")
    _scenarios(test_data_dir, "a")
    working_set.use(test_data_dir, "a")
    assert read_working_set(test_data_dir) == ["a", "retire_alice"]


def test_a_deleted_scenario_goes(test_data_dir: Path) -> None:
    _scenarios(test_data_dir, "a")
    working_set.use(test_data_dir, "a", "base")
    working_set.remove(test_data_dir, "a")
    assert read_working_set(test_data_dir) == ["base"]


def test_a_background_build_is_not_use(
    test_data_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(BUILDER_CHILD_ENV, "1")
    working_set.use(test_data_dir, "base")
    assert not working_set_path(test_data_dir).exists()


@pytest.mark.uses_current_scenario_write
def test_naming_or_defaulting_a_scenario_on_the_cli_is_use(test_data_dir: Path) -> None:
    assert _sim(test_data_dir, "account", "list", "--scenario", "base").exit_code == 0
    assert read_working_set(test_data_dir) == ["base"]
    current_scenario_path(test_data_dir).write_text("retire_alice\n")
    assert _sim(test_data_dir, "account", "list").exit_code == 0
    assert read_working_set(test_data_dir) == ["retire_alice", "base"]


@pytest.mark.uses_current_scenario_write
def test_switch_is_use_even_when_unchanged(test_data_dir: Path) -> None:
    working_set_path(test_data_dir).write_text("base\nretire_alice\n")
    result = _sim(test_data_dir, "switch", "retire_alice")
    assert result.exit_code == 0, result.output
    assert read_working_set(test_data_dir) == ["retire_alice", "base"]


@pytest.mark.uses_current_scenario_write
def test_an_edit_is_use(test_data_dir: Path) -> None:
    working_set_path(test_data_dir).write_text("retire_alice\nbase\n")
    result = _sim(test_data_dir, "scenario", "edit", "base", "--description", "edited")
    assert result.exit_code == 0, result.output
    assert read_working_set(test_data_dir) == ["base", "retire_alice"]


@pytest.mark.uses_current_scenario_write
def test_a_new_scenario_is_use_but_its_parent_is_not(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "scenario", "create", "child", "base")
    assert result.exit_code == 0, result.output
    assert read_working_set(test_data_dir) == ["child"]


@pytest.mark.uses_current_scenario_write
def test_cli_rename_puts_it_on_top_and_delete_drops_it(test_data_dir: Path) -> None:
    working_set_path(test_data_dir).write_text("base\nretire_alice\n")
    current_scenario_path(test_data_dir).write_text("base\n")
    assert _sim(test_data_dir, "scenario", "rename", "retire_alice", "retire_bill").exit_code == 0
    assert read_working_set(test_data_dir) == ["retire_bill", "base"]
    assert _sim(test_data_dir, "scenario", "delete", "retire_bill", "--yes").exit_code == 0
    assert read_working_set(test_data_dir) == ["base"]


def test_a_notebook_pick_is_use_but_the_starting_value_is_not(test_data_dir: Path) -> None:
    _scenarios(test_data_dir, "a")
    baseline = widgets.Dropdown(options=["base", "retire_alice"], value="base")
    variants = widgets.SelectMultiple(options=["a", "retire_alice"], value=())
    note_picks(test_data_dir, baseline, variants)
    assert not working_set_path(test_data_dir).exists()
    variants.value = ("a", "retire_alice")
    assert read_working_set(test_data_dir) == ["base", "a", "retire_alice"]
    baseline.value = "retire_alice"
    assert read_working_set(test_data_dir) == ["retire_alice", "a", "base"]
