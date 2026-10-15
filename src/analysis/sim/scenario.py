"""
Scenario management for the simulation.

This module provides functions to create, load, and manipulate scenarios
within the simulation framework.
"""

import csv
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from analysis.sim.loader import LoadError
from analysis.sim import chain_edit, registries
from analysis.sim.dates import month_end_iso
from analysis.sim.lineage import load_order, read_parents
from analysis.sim.loader import ScenarioRef, list_scenario_names, resolve_scenario_name
from analysis.sim.readme import parse_readme_summary
from analysis.sim import cli


# Written into every new scenario's series/ so the folder isn't empty (git
# doesn't keep an empty folder) and says what belongs there. It names its
# references in prose: a link wouldn't resolve from inside the data repo.
SERIES_README = """\
# Series

Growth-rate and inflation series for this scenario, one CSV file per
series. A file named `rate__<series>.csv` is a growth rate (for an
account's or lot's `series`, or a fund's asset class);
`inflation__<series>.csv` is an inflation rate (for a scheduled
transaction's `series`).

Each file has two columns:

- `effective_date`: the month the rate takes effect; it holds until the
  next row's date.
- `annual_rate`: the annual rate as a percent, with its sign (`4.5%`).

A file here replaces the parent scenario's file of the same name; a series
with no file here is inherited from the parent. CPI is the exception: it
lives in `aux/inflation__cpi.csv`, not here.

Details: ADR-003 sections 4.1d and 4.4 in the analysis code repository.
"""


def create_scenario(data_dir: Path, name: str, parent: str | ScenarioRef,
                    description: str | None = None,
                    intent_dir: Path | None = None,
                    mixins: Sequence[str] = ()) -> Path:
    """
    Create a new scenario with the given name, parent, and optional description.

    ``mixins`` are further parents applied after ``parent``'s line, in order
    (ADR-003 §4.5); ``parent_scenario`` records them colon-separated.

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
    mixin_names = [
        resolve_scenario_name(data_dir, mixin, intent_dir=intent_dir).name for mixin in mixins
    ]
    target = data_dir / "scenarios" / name
    if target.exists():
        raise LoadError(f"scenario already exists: {target}")
    target.mkdir(parents=True, exist_ok=True)
    parents = [parent_ref.name, *mixin_names]

    skeletons: dict[str, tuple[str, ...]] = {
        "accounts.csv": registries.ACCOUNTS_CSV_HEADER,
        "account_values.csv": registries.ACCOUNT_VALUES_CSV_HEADER,
        "scheduled.csv": registries.SCHEDULED_CSV_HEADER,
        "calendar.csv": registries.CALENDAR_HEADER,
    }

    series_dir = target / "series"
    series_dir.mkdir()
    (series_dir / "README.md").write_text(SERIES_README, encoding="utf-8")

    # Both role folders, kept in git by an empty .gitignore, so it's plain
    # where a new chain goes (ADR-003 §4.7).
    for role in registries.CHAIN_ROLES:
        role_dir = target / "chains" / role
        role_dir.mkdir(parents=True)
        (role_dir / ".gitignore").write_text("", encoding="utf-8")
    pd.DataFrame(columns=list(chain_edit.CHAIN_COLUMNS)).to_csv(
        target / "chains" / "source" / "_example.csv", index=False,
    )
    cli._append_csv_row(
        target / "scenario.csv",
        registries.SCENARIO_HEADER,
        {
            "name": name,
            "description": description or f"Derived from {' + '.join(parents)}",
            "parent_scenario": ":".join(parents),
            "created_by": os.environ.get("ANALYSIS_USER")
            or os.environ.get("USER")
            or "unknown",
            "meta_json": "{}",
        },
    )
    for filename, header in skeletons.items():
        pd.DataFrame(columns=list(header)).to_csv(target / filename, index=False)

    return target


@dataclass(frozen=True)
class ScenarioDate:
    """A scenario's ``start`` or ``end`` month as the load would merge it,
    and whether it came from an ancestor rather than the scenario itself."""

    month: str
    inherited: bool


@dataclass(frozen=True)
class ScenarioSummary:
    """One scenario folder, as ``sim scenario list --verbose`` shows it."""

    name: str
    parents: list[str]
    description: str
    start: ScenarioDate | None = None
    end: ScenarioDate | None = None


def scenario_summaries(data_dir: Path, scenarios_dir: Path | None = None) -> list[ScenarioSummary]:
    """Every scenario folder's name, parents, one-line description, and start
    and end months, sorted.

    Read straight from the folders rather than loaded, so it is quick and
    still lists a scenario that wouldn't load. The description follows the
    loader's rule (ADR-003 §4.1e), cut to one line: the README.md title
    (unless it just restates the name), else its first paragraph, else
    scenario.csv's own ``description``. The dates follow the loader's
    field-by-field merge down the load order (ADR-003 §4.5).
    """
    scenarios = scenarios_dir if scenarios_dir is not None else Path(data_dir) / "scenarios"
    summaries = []
    for name in list_scenario_names(data_dir, scenarios):
        rows = {n: _scenario_row(scenarios / n) for n in load_order(scenarios, name)}
        summaries.append(ScenarioSummary(
            name=name,
            parents=read_parents(scenarios / name),
            description=_one_line_description(scenarios / name, rows[name]),
            start=_merged_date(rows, name, "start"),
            end=_merged_date(rows, name, "end"),
        ))
    return summaries


def _scenario_row(scenario_dir: Path) -> dict[str, str]:
    """scenario.csv's data row, or {} -- tolerant, like read_parents:
    reporting a malformed scenario.csv is the loader's job, and a listing
    shouldn't fail on one."""
    try:
        with (scenario_dir / "scenario.csv").open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                return {k: v or "" for k, v in row.items() if k is not None}
    except (OSError, UnicodeDecodeError, csv.Error):
        pass
    return {}


def _merged_date(rows: dict[str, dict[str, str]], name: str, col: str) -> ScenarioDate | None:
    """The last valid ``col`` in load order (``rows`` is root first), as
    ``YYYY-MM``; the loader pins a month column to its last day, so the day
    adds nothing. Like the loader, which reports and blanks a date that
    doesn't parse, an invalid one lets the ancestor's show through."""
    for owner, row in reversed(rows.items()):
        raw = (row.get(col) or "").strip()
        if not raw:
            continue
        try:
            month = month_end_iso(raw)[:len("YYYY-MM")]
        except ValueError:
            continue
        return ScenarioDate(month, inherited=owner != name)
    return None


def _one_line_description(scenario_dir: Path, row: dict[str, str]) -> str:
    readme = scenario_dir / "README.md"
    if readme.is_file():
        try:
            summary = parse_readme_summary(readme.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            summary = None
        if summary is not None:
            # A title that only restates the folder name ("# Base") says
            # nothing the name column doesn't; the paragraph does.
            title = summary.title
            if title and _name_key(title) == _name_key(scenario_dir.name):
                title = ""
            if title or summary.paragraph:
                return title or summary.paragraph
    return " ".join(row.get("description", "").split())


def _name_key(text: str) -> str:
    return re.sub(r"[\s_-]+", "", text).casefold()
