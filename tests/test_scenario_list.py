"""``sim scenario list`` (issue #100): bare names for scripts, or with
--verbose a table of parents, dates and descriptions (dates since #159, seed
and ensemble size since #169). ``sim ensemble list`` is the same, keeping
only the ensembles."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim.loader import write_current_scenario
from analysis.sim.scenario import ScenarioDate, ScenarioValue, scenario_summaries

# One more than the verbose table's nine columns.
ROW_BARS = 10


def _list(data_dir: Path, *args: str, group: str = "scenario") -> str:
    result = CliRunner().invoke(main, ["sim", group, "list", "--data-dir", str(data_dir), *args])
    assert result.exit_code == 0, result.output
    return result.output


@pytest.fixture
def wide_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Room for a whole table row, so assertions don't depend on rich's wrapping."""
    monkeypatch.setenv("COLUMNS", "200")


def test_plain_list_is_just_the_names_one_per_line(
    test_data_dir: Path, derive_scenario: Callable[..., Path],
) -> None:
    derive_scenario("combo", "retire_alice:base", description="Combined")
    assert _list(test_data_dir).splitlines() == ["base", "combo", "retire_alice"]


def test_an_empty_scenarios_folder_lists_nothing(
    test_data_dir: Path, empty_scenarios_dir: Path,
) -> None:
    assert _list(test_data_dir) == ""
    assert _list(test_data_dir, "--verbose").startswith("(no scenarios in ")


def test_list_without_a_scenarios_folder_fails(
    test_data_dir: Path, empty_scenarios_dir: Path,
) -> None:
    empty_scenarios_dir.rmdir()
    result = CliRunner().invoke(main, ["sim", "scenario", "list", "--data-dir", str(test_data_dir)])
    assert result.exit_code != 0
    assert "no scenarios/" in result.output


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.split("│")[1:-1]]


def test_verbose_list_shows_parents_and_description(
    test_data_dir: Path, derive_scenario: Callable[..., Path], wide_terminal: None,
) -> None:
    derive_scenario("combo", "retire_alice:mix", description="Retire with the mix")
    derive_scenario("mix", "base", description="A mix-in")
    rows = {line.split("│")[2].strip(): line for line in _list(test_data_dir, "-v").splitlines()
            if line.count("│") == ROW_BARS and line.split("│")[2].strip()}
    assert set(rows) == {"base", "combo", "mix", "retire_alice"}
    assert "retire_alice + mix" in rows["combo"]
    assert "Retire with the mix" in rows["combo"]
    assert "Alice retires 2028" in rows["retire_alice"]


def test_verbose_list_marks_the_current_scenario(
    test_data_dir: Path, wide_terminal: None,
) -> None:
    write_current_scenario(test_data_dir, "base")
    rows = [line for line in _list(test_data_dir, "--verbose").splitlines() if line.count("│") == ROW_BARS]
    marked = [line.split("│")[2].strip() for line in rows if line.split("│")[1].strip() == "*"]
    assert marked == ["base"]


def test_description_prefers_the_readme_title_over_scenario_csv(
    test_data_dir: Path, derive_scenario: Callable[..., Path],
) -> None:
    derive_scenario("titled", "base", {"README.md": "# Early retirement\n\nDetails.\n"},
                    description="From the CSV")
    derive_scenario("echo", "base", {"README.md": "# Echo\n\nWhat it is for.\n"},
                    description="From the CSV")
    by_name = {s.name: s for s in scenario_summaries(test_data_dir)}
    assert by_name["titled"].description == "Early retirement"
    # A title that only repeats the name gives way to the paragraph.
    assert by_name["echo"].description == "What it is for."
    assert by_name["echo"].parents == ["base"]


def test_a_scenario_that_would_not_load_is_still_listed(
    test_data_dir: Path,
) -> None:
    (test_data_dir / "scenarios" / "broken").mkdir()
    by_name = {s.name: s for s in scenario_summaries(test_data_dir)}
    assert by_name["broken"].parents == []
    assert by_name["broken"].description == ""


def test_dates_merge_down_the_load_order_and_say_whether_inherited(
    test_data_dir: Path, derive_scenario: Callable[..., Path],
) -> None:
    derive_scenario("longer", "retire_alice", end="2045-06")
    derive_scenario("mix", "base", start="2027-01-15")
    derive_scenario("combo", "longer:mix")
    # The loader blanks a date it can't parse, so the parent's shows through.
    derive_scenario("typo", "base", end="2040-13")
    by_name = {s.name: s for s in scenario_summaries(test_data_dir)}
    assert by_name["base"].start == ScenarioDate("2026-05", inherited=False)
    assert by_name["retire_alice"].end == ScenarioDate("2040-12", inherited=False)
    assert by_name["longer"].start == ScenarioDate("2026-05", inherited=True)
    assert by_name["longer"].end == ScenarioDate("2045-06", inherited=False)
    # A mix-in's non-blank field overrides the main parent's, as in the load.
    assert by_name["combo"].start == ScenarioDate("2027-01", inherited=True)
    assert by_name["combo"].end == ScenarioDate("2045-06", inherited=True)
    assert by_name["typo"].end == ScenarioDate("2040-12", inherited=True)


def test_verbose_list_brackets_inherited_dates(
    test_data_dir: Path, derive_scenario: Callable[..., Path], wide_terminal: None,
) -> None:
    derive_scenario("longer", "retire_alice", end="2045-06")
    rows = {_cells(line)[1]: _cells(line) for line in _list(test_data_dir, "-v").splitlines()
            if line.count("│") == ROW_BARS and line.split("│")[2].strip()}
    assert rows["longer"][3:5] == ["[2026-05]", "2045-06"]
    assert rows["base"][3:5] == ["2026-05", "2040-12"]


def test_a_scenario_without_dates_shows_none(test_data_dir: Path) -> None:
    (test_data_dir / "scenarios" / "broken").mkdir()
    by_name = {s.name: s for s in scenario_summaries(test_data_dir)}
    assert by_name["broken"].start is None
    assert by_name["broken"].end is None


def test_seed_and_ensemble_size_merge_down_the_load_order(
    test_data_dir: Path, derive_scenario: Callable[..., Path],
) -> None:
    derive_scenario("recipe", "retire_alice", seed="7", meta_json='{"ensemble_size":5}')
    derive_scenario("variant", "recipe", meta_json='{"note":"x"}')
    derive_scenario("plain", "recipe", meta_json='{"ensemble_size":0}')
    by_name = {s.name: s for s in scenario_summaries(test_data_dir)}
    assert by_name["retire_alice"].seed == ScenarioValue(42, inherited=False)
    assert by_name["retire_alice"].ensemble_size is None
    assert by_name["recipe"].ensemble_size == ScenarioValue(5, inherited=False)
    assert by_name["variant"].seed == ScenarioValue(7, inherited=True)
    assert by_name["variant"].ensemble_size == ScenarioValue(5, inherited=True)
    # A size of 0 is no size: not an ensemble, even under one.
    assert by_name["plain"].ensemble_size is None
    assert not by_name["plain"].is_ensemble


def test_verbose_list_brackets_an_inherited_seed_and_size(
    test_data_dir: Path, derive_scenario: Callable[..., Path], wide_terminal: None,
) -> None:
    derive_scenario("recipe", "retire_alice", seed="7", meta_json='{"ensemble_size":5}')
    derive_scenario("variant", "recipe")
    rows = {_cells(line)[1]: _cells(line) for line in _list(test_data_dir, "-v").splitlines()
            if line.count("│") == ROW_BARS and line.split("│")[2].strip()}
    assert rows["recipe"][5:7] == ["7", "5"]
    assert rows["variant"][5:7] == ["[7]", "[5]"]
    assert rows["retire_alice"][5:7] == ["42", ""]


def test_ensemble_list_keeps_only_the_ensembles(
    test_data_dir: Path, derive_scenario: Callable[..., Path], wide_terminal: None,
) -> None:
    derive_scenario("recipe", "retire_alice", seed="7", meta_json='{"ensemble_size":5}')
    derive_scenario("variant", "recipe")
    derive_scenario("plain", "recipe", meta_json='{"ensemble_size":0}')
    assert _list(test_data_dir, group="ensemble").splitlines() == ["recipe", "variant"]
    rows = {_cells(line)[1] for line in _list(test_data_dir, "-v", group="ensemble").splitlines()
            if line.count("│") == ROW_BARS and line.split("│")[2].strip()}
    assert rows == {"recipe", "variant"}


def test_ensemble_list_without_ensembles_says_so(test_data_dir: Path) -> None:
    assert _list(test_data_dir, group="ensemble").startswith("(no ensembles in ")
