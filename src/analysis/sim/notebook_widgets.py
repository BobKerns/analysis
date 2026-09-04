"""Small notebook-only ipywidgets helpers shared across sim notebooks."""

from __future__ import annotations

from pathlib import Path

from analysis.sim.loader import list_scenario_names


def scenario_dropdown(data_dir: Path | str, default: str):
    """A ``Scenario:`` Dropdown listing every scenario under ``data_dir``.

    ``default`` (typically the scenario the notebook already auto-resolved,
    via ``resolve_scenario_name``) is preselected.
    """
    import ipywidgets as widgets

    options = list_scenario_names(data_dir)
    return widgets.Dropdown(options=options, value=default, description="Scenario:")
