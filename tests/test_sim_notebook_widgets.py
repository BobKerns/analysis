"""Tests for analysis.sim.notebook_widgets."""

from __future__ import annotations

from pathlib import Path

from analysis.sim.notebook_widgets import scenario_dropdown

PROTOTYPE = Path(__file__).resolve().parents[1] / "prototype_data"


def test_scenario_dropdown_lists_scenarios_and_defaults() -> None:
    dropdown = scenario_dropdown(PROTOTYPE, "retire_alice")

    assert "retire_alice" in dropdown.options
    assert dropdown.value == "retire_alice"
    assert dropdown.description == "Scenario:"
