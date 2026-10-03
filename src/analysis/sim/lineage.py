"""Scenario inheritance order (ADR-003 §4.5).

A scenario's ``parent_scenario`` names one parent or, colon-separated, a main
parent followed by mix-ins (``base:retire_early:high_inflation``). Everything
that reasons about "the scenarios this one is built from" -- the loader's
overlays, the aux/series/prices/itemizations override search, staging's
affected-scenario check and the provenance scope -- goes through
:func:`load_order`, so they can't disagree about which folders are read or in
what order.
"""

from __future__ import annotations

import csv
from pathlib import Path


class ScenarioCycleError(ValueError):
    """``parent_scenario`` links lead back to a scenario already on the path."""

    def __init__(self, path: tuple[str, ...]) -> None:
        super().__init__(f"scenario inheritance cycle: {' -> '.join(path)}")
        self.path = path


def parse_parents(value: str | None) -> list[str]:
    """The names in a ``parent_scenario`` cell, main parent first."""
    return [name for name in (part.strip() for part in (value or "").split(":")) if name]


def read_parents(scenario_dir: Path) -> list[str]:
    """``parent_scenario`` out of ``scenario_dir/scenario.csv``, or [] if absent.

    Deliberately tolerant: the loader validates scenario.csv properly and
    reports what's wrong with it; working out the order must not be the thing
    that fails first, with a worse message.
    """
    path = scenario_dir / "scenario.csv"
    if not path.is_file():
        return []
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                return parse_parents(row.get("parent_scenario"))
    except (OSError, UnicodeDecodeError, csv.Error):
        return []
    return []


def load_order(scenarios_dir: Path, scenario_name: str, *, strict: bool = False) -> list[str]:
    """Every scenario ``scenario_name`` is built from, root first, itself last.

    The first parent's order comes first in full; each further parent then
    adds only the part of its own order not already present -- for a mix-in
    deriving from ``base``, just the mix-in. Overlaying the scenarios in this
    order, later winning, is the load.

    A scenario whose folder is missing still appears (with no parents), so the
    loader can report it. With ``strict``, a cycle raises
    :class:`ScenarioCycleError`; otherwise the link closing it is ignored.
    """
    memo: dict[str, list[str]] = {}

    def visit(name: str, path: tuple[str, ...]) -> list[str]:
        if name in path:
            if strict:
                raise ScenarioCycleError((*path, name))
            return []
        if name in memo:
            return memo[name]
        order: list[str] = []
        for parent in read_parents(scenarios_dir / name):
            for n in visit(parent, (*path, name)):
                if n not in order:
                    order.append(n)
        if name not in order:
            order.append(name)
        memo[name] = order
        return order

    return visit(scenario_name, ())
