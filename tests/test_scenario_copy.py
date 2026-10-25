"""``sim scenario copy`` and ``sim ensemble copy``: a sibling under a new name (issue #295)."""

from __future__ import annotations

import csv
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.sim import rename
from analysis.sim import scenario as sc
from analysis.sim.lineage import read_parents
from analysis.sim.loader import load_scenario


def _sim(data_dir: Path, *args: str, input: str | None = None) -> Result:  # noqa: A002 -- CliRunner's name
    return CliRunner().invoke(main, ["sim", *args, "--data-dir", str(data_dir)], input=input,
                              terminal_width=200)


def _row(folder: Path) -> dict[str, str]:
    with (folder / "scenario.csv").open(newline="", encoding="utf-8") as fh:
        return next(csv.DictReader(fh))


@pytest.fixture
def loads(test_data_dir: Path, intent_dir: Path) -> Callable[[str], None]:
    def _loads(name: str) -> None:
        load_scenario(test_data_dir, name, intent_dir=intent_dir, allow_dirty_data=True)
    return _loads


@pytest.mark.uses_current_scenario_write
def test_scenario_copy_makes_a_sibling(
    test_data_dir: Path, test_scenarios_dir: Path, loads: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANALYSIS_USER", "bill")
    old = test_scenarios_dir / "retire_alice"
    readme = old / "README.md"
    readme.write_text("# retire_alice\n\nSee [lots](../retire_alice/account_lots.csv) and "
                      "[base](../base/README.md).\n", encoding="utf-8")
    (test_data_dir / ".current_scenario").write_text("base\n")
    before = {p.relative_to(old): p.read_bytes() for p in old.rglob("*") if p.is_file()}

    result = _sim(test_data_dir, "scenario", "copy", "retire_alice", "retire_early")

    assert result.exit_code == 0, result.output
    assert f"copied scenario retire_alice to {test_scenarios_dir / 'retire_early'}" in result.output
    assert "inherits: base" in result.output
    new = test_scenarios_dir / "retire_early"
    # The copy has every file, a sibling under the same parent ...
    assert {p.relative_to(new) for p in new.rglob("*") if p.is_file()} == set(before)
    assert read_parents(new) == read_parents(old) == ["base"]
    row = _row(new)
    assert (row["name"], row["created_by"]) == ("retire_early", "bill")
    assert (new / "accounts.csv").read_bytes() == before[Path("accounts.csv")]
    # ... whose own name follows, links to itself included.
    assert (new / "README.md").read_text(encoding="utf-8") == (
        "# retire_early\n\nSee [lots](../retire_early/account_lots.csv) and [base](../base/README.md).\n")
    # The original is untouched, and the copy is current.
    assert {p.relative_to(old): p.read_bytes() for p in old.rglob("*") if p.is_file()} == before
    assert (test_data_dir / ".current_scenario").read_text().strip() == "retire_early"
    assert "Current scenario is now retire_early (was base)" in result.stderr
    loads("retire_early")


def test_scenario_copy_of_a_template(test_data_dir: Path, test_scenarios_dir: Path) -> None:
    result = _sim(test_data_dir, "scenario", "copy", "t_market_current", "my_market")
    assert result.exit_code == 0, result.output
    assert _row(test_scenarios_dir / "my_market")["name"] == "my_market"
    assert not (test_scenarios_dir / "t_market_current").exists()


def test_scenario_copy_asks_for_both_names(test_data_dir: Path, test_scenarios_dir: Path) -> None:
    result = _sim(test_data_dir, "scenario", "copy", input="retire_alice\nretire_early\n")
    assert result.exit_code == 0, result.output
    assert _row(test_scenarios_dir / "retire_early")["name"] == "retire_early"


@pytest.mark.parametrize(("old", "new", "message"), [
    ("retire_alice", "base", "scenario already exists"),
    ("retire_alice", "Retire Early", "invalid scenario name"),
    ("nobody", "somebody", "scenario not found"),
    ("../scenarios/retire_alice", "somebody", "scenario not found"),
    ("retire_alice/series", "somebody", "scenario not found"),
])
def test_scenario_copy_refuses(test_data_dir: Path, test_scenarios_dir: Path,
                               old: str, new: str, message: str) -> None:
    result = _sim(test_data_dir, "scenario", "copy", old, new)
    assert result.exit_code != 0 and message in result.output
    assert not (test_scenarios_dir / "somebody").exists()
    assert not (test_scenarios_dir / "Retire Early").exists()


@pytest.mark.parametrize("old", ["../scenarios/retire_alice", "..", ".", ""])
def test_copy_scenario_takes_only_a_folder_name(test_data_dir: Path, old: str) -> None:
    with pytest.raises(FileNotFoundError, match="scenario not found"):
        rename.copy_scenario(test_data_dir, old, "somebody")


def test_ensemble_copy_keeps_seed_and_size(
    test_data_dir: Path, test_scenarios_dir: Path, test_ensemble_scenario: str,
) -> None:
    result = _sim(test_data_dir, "ensemble", "copy", test_ensemble_scenario, "recipe_9")
    assert result.exit_code == 0, result.output
    assert "same seed and size" in result.output
    assert sc.ensemble_settings(test_data_dir, "recipe_9") == sc.ensemble_settings(
        test_data_dir, test_ensemble_scenario)
    assert read_parents(test_scenarios_dir / "recipe_9") == ["retire_alice"]


def test_ensemble_copy_offers_only_ensembles(test_data_dir: Path, test_ensemble_scenario: str) -> None:
    result = _sim(test_data_dir, "ensemble", "copy", input=f"{test_ensemble_scenario}\nrecipe_9\n")
    assert result.exit_code == 0, result.output
    assert f"ensembles: {test_ensemble_scenario}" in result.output
    assert (test_data_dir / "scenarios" / "recipe_9").is_dir()


def test_ensemble_copy_refuses_a_plain_scenario(test_data_dir: Path, test_scenarios_dir: Path) -> None:
    result = _sim(test_data_dir, "ensemble", "copy", "retire_alice", "somebody")
    assert result.exit_code != 0 and "not an ensemble" in result.output
    assert not (test_scenarios_dir / "somebody").exists()
