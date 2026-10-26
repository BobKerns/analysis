"""#300: `sim scenario|ensemble enable|disable`. A disabled scenario (a
mix-in, or a base that only exists to be built on) is built only when named:
`sim all`, the background builder and the notebooks' pickers leave it out."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.sim.loader import DISABLE_FILE, read_disabled_scenarios


def _sim(test_data_dir: Path, *args: str, answers: tuple[str, ...] = ()) -> Result:
    return CliRunner().invoke(main, ["sim", *args, "--data-dir", str(test_data_dir)],
                              input="".join(f"{a}\n" for a in answers))


def test_disable_and_enable_write_and_remove_the_folders_file(test_data_dir: Path) -> None:
    marker = test_data_dir / "scenarios" / "base" / DISABLE_FILE
    result = _sim(test_data_dir, "scenario", "disable", "base")
    assert result.exit_code == 0, result.output
    assert marker.is_file()
    assert read_disabled_scenarios(test_data_dir) == {"base"}
    assert "already disabled" in _sim(test_data_dir, "scenario", "disable", "base").output
    result = _sim(test_data_dir, "scenario", "enable", "base")
    assert result.exit_code == 0, result.output
    assert not marker.exists() and read_disabled_scenarios(test_data_dir) == set()


def test_disable_asks_for_a_name_left_out(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "scenario", "disable", answers=("retire_alice",))
    assert result.exit_code == 0, result.output
    assert read_disabled_scenarios(test_data_dir) == {"retire_alice"}


def test_ensemble_disable_refuses_a_plain_scenario(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "ensemble", "disable", "base")
    assert result.exit_code != 0 and "not an ensemble" in result.output
    assert read_disabled_scenarios(test_data_dir) == set()


def test_disable_refuses_what_is_not_a_scenario_folder(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "scenario", "disable", "../aux")
    assert result.exit_code != 0 and "scenario not found" in result.output


def test_scenario_list_marks_a_disabled_scenario(test_data_dir: Path) -> None:
    _sim(test_data_dir, "scenario", "disable", "base")
    result = _sim(test_data_dir, "scenario", "list", "-v")
    assert result.exit_code == 0, result.output
    row = next(line for line in result.output.splitlines() if " base " in line)
    assert " no " in row
    plain = _sim(test_data_dir, "scenario", "list").output.split()
    assert "base" in plain  # plain output is every name, for scripts


def test_pickers_leave_out_disabled_scenarios_but_the_one_they_start_on(
    test_data_dir: Path,
) -> None:
    from analysis.sim.notebook_ui.pickers import enabled_scenario_names

    _sim(test_data_dir, "scenario", "disable", "base")
    assert "base" not in enabled_scenario_names(test_data_dir)
    assert "base" in enabled_scenario_names(test_data_dir, keep=["base"])


def test_recipe_lists_leave_out_disabled_ensembles(
    test_data_dir: Path, runs_dir: Path,
) -> None:
    from analysis.sim.notebook_ui.ensemble_widgets import list_ensemble_recipes
    from analysis.sim.notebook_ui.sensitivity_widgets import list_sensitivity_recipes

    for name in ("base", "retire_alice"):
        batch = runs_dir / name / "2026-10-26T043000Z"
        batch.mkdir(parents=True)
        (batch / "batch_manifest.json").write_text('{"mode": "monte_carlo"}')
    (test_data_dir / "scenarios" / "base" / DISABLE_FILE).write_text("")
    assert list_ensemble_recipes(runs_dir) == ["base", "retire_alice"]
    assert list_ensemble_recipes(runs_dir, test_data_dir) == ["retire_alice"]
    assert list_ensemble_recipes(runs_dir, test_data_dir, keep=["base"]) == ["base", "retire_alice"]
    assert list_sensitivity_recipes(runs_dir, test_data_dir) == ["retire_alice"]


@pytest.mark.parametrize("command", ["rename", "copy"])
def test_rename_and_copy_carry_the_folders_file(test_data_dir: Path, command: str) -> None:
    _sim(test_data_dir, "scenario", "disable", "retire_alice")
    result = _sim(test_data_dir, "scenario", command, "retire_alice", "later")
    assert result.exit_code == 0, result.output
    assert "later" in read_disabled_scenarios(test_data_dir)
