"""Where a scenario's income tax jurisdictions live (#507, ADR-003 §4.15):
one file per jurisdiction, ``taxes/<jurisdiction>.csv``, holding its
definition (one row).

Like strategies, they merge by whole file down the load order: a later
scenario's file for a jurisdiction replaces an earlier one's. The file name
is the jurisdiction's name. A file whose name starts with ``_`` is a
template the loader skips.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from analysis.sim import registries
from analysis.sim.strategy_files import LineageFile
from analysis.sim.strategy_files import lineage_files as _lineage_files

__all__ = ["LineageFile", "definition_path", "lineage_files", "name_problem", "params"]


def definition_path(scenario_dir: Path, name: str) -> Path:
    """Where jurisdiction ``name``'s file is written in a scenario folder."""
    return scenario_dir / registries.TAXES_DIR / f"{name}.csv"


def lineage_files(scenarios_dir: Path, scenario: str, *, strict: bool = False) -> dict[str, LineageFile]:
    """Each jurisdiction's file as ``scenario``'s load order leaves it."""
    return _lineage_files(scenarios_dir, scenario, registries.TAXES_DIR, strict=strict)


def name_problem(name: str) -> str | None:
    """Why ``name`` can't be a jurisdiction's name, or None."""
    if name not in registries.TAX_JURISDICTIONS:
        return (f"a tax jurisdiction is one of {', '.join(registries.TAX_JURISDICTIONS)} "
                f"(the file's name), not {name!r}")
    return None


def params(row: Mapping[str, str] | None) -> dict[str, Any]:
    """A jurisdiction row's ``params_json`` as a dict: {} when blank, not
    JSON or not an object (the loader reports those)."""
    if row is None:
        return {}
    try:
        value = json.loads(row.get("params_json") or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}
