"""
Scenario management for the simulation.

This module provides functions to create, load, and manipulate scenarios
within the simulation framework.
"""

import os
from pathlib import Path
import re

import pandas as pd

from analysis.sim.loader import LoadError
from analysis.sim import registries
from analysis.sim.loader import ScenarioRef, ScenarioRef, resolve_scenario_name
from analysis.sim import cli


def create_scenario(data_dir: Path, name: str, parent: str | ScenarioRef,
                    description: str | None = None,
                    intent_dir: Path | None = None) -> Path:
    """
    Create a new scenario with the given name, parent, and optional description.

    Returns the path to the created scenario.
    """
    if not re.match(registries.SCENARIO_NAME_RE, name):
        raise LoadError(f"invalid scenario name: {name}")
    match parent:
        case ScenarioRef():
            parent_ref = parent
        case str():
            parent_ref = resolve_scenario_name(data_dir, parent, intent_dir=intent_dir)
        case _:
            raise LoadError(f"invalid parent type: {type(parent)}")
    target = data_dir / "scenarios" / name
    if target.exists():
        raise LoadError(f"scenario already exists: {target}")
    target.mkdir(parents=True, exist_ok=True)

    skeletons: dict[str, tuple[str, ...]] = {
        "accounts.csv": registries.ACCOUNTS_CSV_HEADER,
        "account_values.csv": registries.ACCOUNT_VALUES_CSV_HEADER,
        "scheduled.csv": registries.SCHEDULED_CSV_HEADER,
        "calendar.csv": registries.CALENDAR_HEADER,
    }

    chains_dir = target / "chains"
    chains_dir.mkdir()
    (chains_dir / ".gitignore").write_text("", encoding="utf-8")
    pd.DataFrame(columns=["account_name", "maximum", "percent", "series", "cadence"]).to_csv(
        chains_dir / "_asset__example.csv", index=False,
    )
    cli._append_csv_row(
        target / "scenario.csv",
        registries.SCENARIO_HEADER,
        {
            "name": name,
            "description": description or f"Derived from {parent_ref.name}",
            "parent_scenario": parent_ref.name,
            "created_by": os.environ.get("ANALYSIS_USER")
            or os.environ.get("USER")
            or "unknown",
            "meta_json": "{}",
        },
    )
    for filename, header in skeletons.items():
        pd.DataFrame(columns=list(header)).to_csv(target / filename, index=False)

    return target