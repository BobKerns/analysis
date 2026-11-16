"""The current scenario (#214): a command that takes an optional scenario
records a named one as current, announcing the switch when it changes (#233),
announces a defaulted one on stderr once, and with no current scenario asks
for a name rather than guessing. With $ANALYSIS_KEEP_SCENARIO set, a named
scenario is used without becoming current (#350)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.sim.loader import current_scenario_path, read_current_scenario


def _sim(data_dir: Path, *args: str, input: str | None = None) -> Result:
    return CliRunner().invoke(main, ["sim", *args, "--data-dir", str(data_dir)], input=input)


def test_a_defaulted_scenario_is_announced_once_on_stderr(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "account", "list")

    assert result.exit_code == 0, result.output
    assert result.stderr == "Using scenario retire_alice\n"
    assert "Using scenario" not in result.stdout


def test_a_command_resolving_the_default_twice_announces_it_once(test_data_dir: Path) -> None:
    # `transfer add` finds the scenario's folder, then opens its intent: two
    # resolutions of the same default, one line. (It stops at the first
    # endpoint prompt, past both.)
    result = _sim(test_data_dir, "transfer", "add", input="2030-06\n")

    assert result.stderr.count("Using scenario retire_alice") == 1, result.output


def test_a_named_scenario_is_not_announced(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "account", "list", "--scenario", "retire_alice")

    assert result.exit_code == 0, result.output
    assert "Using scenario" not in result.output


@pytest.mark.uses_current_scenario_write
def test_naming_a_scenario_makes_it_current(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "account", "list", "--scenario", "base")

    assert result.exit_code == 0, result.output
    assert read_current_scenario(test_data_dir) == "base"
    assert _sim(test_data_dir, "account", "list").stderr == "Using scenario base\n"


@pytest.mark.uses_current_scenario_write
def test_naming_another_scenario_announces_the_switch(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "account", "list", "--scenario", "base")

    assert result.exit_code == 0, result.output
    assert result.stderr == "Current scenario is now base (was retire_alice)\n"
    assert "Current scenario" not in result.stdout


@pytest.mark.uses_current_scenario_write
def test_a_switch_from_no_current_scenario_names_only_the_new_one(test_data_dir: Path) -> None:
    current_scenario_path(test_data_dir).unlink()

    result = _sim(test_data_dir, "account", "list", "--scenario", "base")

    assert result.exit_code == 0, result.output
    assert result.stderr == "Current scenario is now base\n"


@pytest.mark.uses_current_scenario_write
def test_a_new_scenarios_parent_does_not_become_current(test_data_dir: Path) -> None:
    current_scenario_path(test_data_dir).write_text("base\n")

    result = _sim(test_data_dir, "scenario", "create", "late", "retire_alice")

    assert result.exit_code == 0, result.output
    assert read_current_scenario(test_data_dir) == "late"
    assert "Using scenario" not in result.output
    assert result.stderr == "Current scenario is now late (was base)\n"


@pytest.mark.uses_current_scenario_write
def test_a_new_ensemble_announces_that_it_is_current(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "ensemble", "create", "spread", "retire_alice")

    assert result.exit_code == 0, result.output
    assert read_current_scenario(test_data_dir) == "spread"
    assert "Current scenario is now spread (was retire_alice)" in result.stderr


@pytest.mark.uses_current_scenario_write
def test_renaming_another_scenario_leaves_the_current_one(test_data_dir: Path) -> None:
    current_scenario_path(test_data_dir).write_text("base\n")

    result = _sim(test_data_dir, "scenario", "rename", "retire_alice", "retire_bill")

    assert result.exit_code == 0, result.output
    assert read_current_scenario(test_data_dir) == "base"


@pytest.mark.parametrize("args", [
    ("account", "list"),
    ("check",),
    ("load", "--no-run"),
    ("calendar", "add"),
    ("networth",),
])
def test_without_a_current_scenario_a_command_asks_for_one(
    test_data_dir: Path, args: tuple[str, ...],
) -> None:
    # The prototype data has two scenarios, but one of them is `base`, which
    # the notebooks still fall back to; the CLI must not.
    current_scenario_path(test_data_dir).unlink()

    result = _sim(test_data_dir, *args, input="")

    assert result.exit_code == 2, result.output
    assert "no current scenario; name one (scenarios: base, retire_alice)" in result.stderr
    assert not current_scenario_path(test_data_dir).exists()


@pytest.mark.uses_current_scenario_write
def test_a_guided_scenario_edit_makes_its_scenario_current_even_unchanged(
    test_data_dir: Path,
) -> None:
    current_scenario_path(test_data_dir).unlink()

    # Picked at the prompt (`edit` is guided), then written with no change.
    _sim(test_data_dir, "scenario", "edit", input="base\n\n")

    assert read_current_scenario(test_data_dir) == "base"


def test_a_current_scenario_that_is_gone_is_named_as_such(test_data_dir: Path) -> None:
    current_scenario_path(test_data_dir).write_text("gone\n")

    result = _sim(test_data_dir, "account", "list")

    assert result.exit_code != 0
    assert "names a missing scenario: 'gone'" in result.stderr
    assert "Using scenario" not in result.stderr


def test_switch_with_no_scenario_prints_the_current_one(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "switch")

    assert result.exit_code == 0, result.output
    assert result.stdout == "Current scenario: retire_alice\n"
    assert read_current_scenario(test_data_dir) == "retire_alice"


def test_switch_with_no_current_scenario_says_none(test_data_dir: Path) -> None:
    current_scenario_path(test_data_dir).unlink()

    result = _sim(test_data_dir, "switch")

    assert result.exit_code == 0, result.output
    assert result.stdout == "Current scenario: (none)\n"


@pytest.mark.uses_current_scenario_write
def test_switch_prints_the_old_scenario_then_makes_the_new_one_current(
    test_data_dir: Path,
) -> None:
    result = _sim(test_data_dir, "switch", "base")

    assert result.exit_code == 0, result.output
    assert result.stdout == "Current scenario: retire_alice\nSwitched to: base\n"
    assert "Current scenario is now" not in result.stderr  # it says so itself
    assert read_current_scenario(test_data_dir) == "base"


def test_switch_to_the_current_scenario_says_unchanged(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "switch", "retire_alice")

    assert result.exit_code == 0, result.output
    assert result.stdout == "Current scenario: retire_alice\nUnchanged: retire_alice\n"


@pytest.mark.uses_current_scenario_write
def test_switch_to_a_missing_scenario_fails_and_keeps_the_current_one(
    test_data_dir: Path,
) -> None:
    result = _sim(test_data_dir, "switch", "gone")

    assert result.exit_code != 0
    assert read_current_scenario(test_data_dir) == "retire_alice"


@pytest.mark.uses_current_scenario_write
def test_with_keep_scenario_naming_a_scenario_leaves_the_current_one(
    test_data_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANALYSIS_KEEP_SCENARIO", "1")

    result = _sim(test_data_dir, "account", "list", "--scenario", "base")

    assert result.exit_code == 0, result.output
    assert read_current_scenario(test_data_dir) == "retire_alice"
    assert "Current scenario" not in result.stderr


@pytest.mark.uses_current_scenario_write
def test_with_keep_scenario_switch_still_switches(
    test_data_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANALYSIS_KEEP_SCENARIO", "1")

    result = _sim(test_data_dir, "switch", "base")

    assert result.exit_code == 0, result.output
    assert read_current_scenario(test_data_dir) == "base"


@pytest.mark.uses_current_scenario_write
def test_with_keep_scenario_a_new_ensemble_is_not_made_current(
    test_data_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANALYSIS_KEEP_SCENARIO", "1")

    result = _sim(test_data_dir, "ensemble", "create", "spread", "retire_alice")

    assert result.exit_code == 0, result.output
    assert read_current_scenario(test_data_dir) == "retire_alice"
    assert "Current scenario" not in result.stderr
