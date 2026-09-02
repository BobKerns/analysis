"""Tests for the scenario_compare notebook's picker/loading logic."""

from __future__ import annotations

from pathlib import Path

from analysis.sim.scenario_compare import available_scenarios, load_scenarios

REPO_ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = REPO_ROOT / "prototype_data"


def test_available_scenarios_lists_sorted_folder_names(tmp_path: Path) -> None:
    for name in ("january", "current", "current-cut"):
        (tmp_path / "scenarios" / name).mkdir(parents=True)

    assert available_scenarios(tmp_path) == ["current", "current-cut", "january"]


def test_load_scenarios_requires_at_least_one_variant() -> None:
    result = load_scenarios(PROTOTYPE, "retire_alice", [])

    assert not result.ok
    assert "at least one variant" in result.warnings[0]


def test_load_scenarios_rejects_too_many_variants() -> None:
    result = load_scenarios(PROTOTYPE, "retire_alice", ["a", "b", "c", "d", "e"], max_variants=4)

    assert not result.ok
    assert "at most 4" in result.warnings[0]


def test_load_scenarios_warns_when_baseline_has_no_run(tmp_path: Path) -> None:
    (tmp_path / "scenarios" / "no_run_scenario").mkdir(parents=True)

    result = load_scenarios(tmp_path, "no_run_scenario", ["no_run_scenario"])

    assert not result.ok
    assert "No run for baseline" in result.warnings[0]
