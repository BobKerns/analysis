"""``sim scenario list`` (issue #100): bare names for scripts, or with
--verbose a table of parents and descriptions."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim.loader import write_current_scenario
from analysis.sim.scenario import scenario_summaries


def _list(data_dir: Path, *args: str) -> str:
    result = CliRunner().invoke(main, ["sim", "scenario", "list", "--data-dir", str(data_dir), *args])
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


def test_verbose_list_shows_parents_and_description(
    test_data_dir: Path, derive_scenario: Callable[..., Path], wide_terminal: None,
) -> None:
    derive_scenario("combo", "retire_alice:mix", description="Retire with the mix")
    derive_scenario("mix", "base", description="A mix-in")
    rows = {line.split("│")[2].strip(): line for line in _list(test_data_dir, "-v").splitlines()
            if line.count("│") == 5}
    assert set(rows) == {"base", "combo", "mix", "retire_alice"}
    assert "retire_alice + mix" in rows["combo"]
    assert "Retire with the mix" in rows["combo"]
    assert "Alice retires 2028" in rows["retire_alice"]


def test_verbose_list_marks_the_current_scenario(
    test_data_dir: Path, wide_terminal: None,
) -> None:
    write_current_scenario(test_data_dir, "base")
    rows = [line for line in _list(test_data_dir, "--verbose").splitlines() if line.count("│") == 5]
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
