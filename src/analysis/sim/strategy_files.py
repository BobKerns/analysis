"""Where a scenario's strategies live (#408, ADR-003 §4.12, §4.14): one file
per strategy, ``strategies/<name>.csv`` holding its definition (one row) and
``strategy_vary/<name>.csv`` the parameters an ensemble varies (a row each).

Like chains, they merge by whole file down the load order: a later
scenario's file for a strategy replaces an earlier one's. The file name is
the strategy's name. A file whose name starts with ``_`` is a template the
loader skips, as for chains.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from analysis.sim import registries
from analysis.sim.lineage import load_order
from analysis.sim.lineage import scenario_dir as _scenario_dir

__all__ = [
    "LineageFile", "definition_path", "lineage_files", "name_problem", "own_files",
    "vary_path",
]


class LineageFile(NamedTuple):
    """A strategy's file as the load order leaves it."""
    path: Path
    scenario: str  # the scenario whose folder holds it
    inherited: bool  # True when that is an ancestor, not the scenario itself


def definition_path(scenario_dir: Path, name: str) -> Path:
    """Where strategy ``name``'s definition is written in a scenario folder."""
    return scenario_dir / registries.STRATEGIES_DIR / f"{name}.csv"


def vary_path(scenario_dir: Path, name: str) -> Path:
    """Where strategy ``name``'s varied parameters are written in a scenario folder."""
    return scenario_dir / registries.STRATEGY_VARY_DIR / f"{name}.csv"


def own_files(scenario_dir: Path, folder: str) -> list[Path]:
    """The scenario folder's own files in ``folder`` (``strategies`` or
    ``strategy_vary``), by name, templates skipped."""
    return [p for p in sorted((scenario_dir / folder).glob("*.csv")) if not p.stem.startswith("_")]


def lineage_files(
    scenarios_dir: Path, scenario: str, folder: str, *, strict: bool = False,
) -> dict[str, LineageFile]:
    """Each strategy's file in ``folder`` as ``scenario``'s load order leaves
    it: the last scenario to have a file for a name wins, whole."""
    found: dict[str, LineageFile] = {}
    for name in load_order(scenarios_dir, scenario, strict=strict):
        for path in own_files(_scenario_dir(scenarios_dir, name), folder):
            found[path.stem] = LineageFile(path, name, name != scenario)
    return found


def name_problem(name: str) -> str | None:
    """Why ``name`` can't be a strategy's name, or None. It becomes a file
    name, and with a parameter a correlation node ``strategy:<name>.<param>``,
    so it is one word without ':', '/', '\\' or '.'."""
    if not name or any(c.isspace() for c in name):
        return "a strategy name is one word, without spaces"
    if any(c in name for c in ":/\\.") or name.startswith("_"):
        return f"a strategy name can't contain ':', '/', '\\' or '.', or start with '_': {name!r}"
    return None
