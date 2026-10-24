"""Scenario inheritance order (ADR-003 §4.5).

A scenario's ``parent_scenario`` names one parent or, colon-separated, a main
parent followed by mix-ins (``base:retire_early:high_inflation``). Everything
that reasons about "the scenarios this one is built from" -- the loader's
overlays, the aux/series/prices/itemizations override search, staging's
affected-scenario check and the provenance scope -- goes through
:func:`load_order`, so they can't disagree about which folders are read or in
what order.

Template scenarios (``t_`` names) are the exception to "a scenario is a folder
under the data repo's ``scenarios/``": a ``t_`` name with no folder there is
read from this code repository's ``templates/scenarios/`` instead, so every
data repo can build on the vetted templates without a copy to maintain
(ADR-003 §4.5a). A folder of the same name in the data repo replaces the
template whole. :func:`scenario_dir` is the one place that decides.
"""

from __future__ import annotations

import csv
from pathlib import Path


TEMPLATE_PREFIX = "t_"
TEMPLATE_SCENARIOS_DIR = Path(__file__).resolve().parents[3] / "templates" / "scenarios"


def is_template_name(name: str) -> bool:
    """Whether ``name`` is a template scenario's: only these fall back."""
    return name.startswith(TEMPLATE_PREFIX)


def scenario_dir(scenarios_dir: Path, name: str) -> Path:
    """The folder scenario ``name`` is read from: the data repo's own, else,
    for a template name, the code repository's; a missing scenario's path is
    the data repo's, so a message names where it was looked for."""
    own = Path(scenarios_dir) / name
    if own.is_dir() or not is_template_name(name) or Path(name).name != name:
        return own
    template = TEMPLATE_SCENARIOS_DIR / name
    return template if template.is_dir() else own


def is_template(scenarios_dir: Path, name: str) -> bool:
    """Whether ``name`` is read from the code repository's templates, so
    nothing in the data repo can be edited to change it."""
    return scenario_dir(scenarios_dir, name) != Path(scenarios_dir) / name


def template_names(scenarios_dir: Path) -> list[str]:
    """The template scenarios the data repo doesn't replace, sorted."""
    if not TEMPLATE_SCENARIOS_DIR.is_dir():
        return []
    return sorted(
        p.name for p in TEMPLATE_SCENARIOS_DIR.iterdir()
        if p.is_dir() and is_template_name(p.name) and not (Path(scenarios_dir) / p.name).is_dir()
    )


class ScenarioCycleError(ValueError):
    """``parent_scenario`` links lead back to a scenario already on the path."""

    def __init__(self, path: tuple[str, ...]) -> None:
        super().__init__(f"scenario inheritance cycle: {' -> '.join(path)}")
        self.path = path


def default_description(parents: list[str]) -> str:
    """The ``description`` ``scenario create`` writes when given none; a
    rename or a parents edit keeps it in step with the parents it names."""
    return f"Derived from {' + '.join(parents)}"


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
        for parent in read_parents(scenario_dir(scenarios_dir, name)):
            for n in visit(parent, (*path, name)):
                if n not in order:
                    order.append(n)
        if name not in order:
            order.append(name)
        memo[name] = order
        return order

    return visit(scenario_name, ())
