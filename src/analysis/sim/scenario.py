"""
Scenario management for the simulation.

This module provides functions to create, load, and manipulate scenarios
within the simulation framework.
"""

import os
import re
from collections.abc import Sequence
from pathlib import Path

import pandas as pd

from analysis.sim.loader import LoadError
from analysis.sim import registries
from analysis.sim.loader import ScenarioRef, resolve_scenario_name
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
- `annual_rate_micros`: the annual rate in micros (1% is `10000`).

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