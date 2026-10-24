"""A scenario's CSV rows as its load order leaves them: each row merged down
the lineage the way the loader merges it, and where it comes from -- the
scenario's own file, or the ancestor it inherits the row from (#202).

The edit commands use this to find a row the scenario inherits and write an
override; the listings (#185) use it to mark where each row is defined.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import pandas as pd

from analysis.sim import registries
from analysis.sim.lineage import load_order
from analysis.sim.lineage import scenario_dir as _scenario_dir
from analysis.sim.loader import calendar_row_key

__all__ = ["CalendarRow", "Overlaid", "overlay_rows", "scenario_calendar_rows"]


class Overlaid(NamedTuple):
    """One row of a scenario's table, as its load order leaves it."""
    row: dict[str, str]  # merged down the load order, as the loader merges it
    index: int | None  # its data row (0-based) in this scenario's own file, if there
    inherited_from: str | None  # the last scenario before this one to have it


def overlay_rows(scenario_dir: Path, filename: str, key: str = "name") -> dict[str, Overlaid]:
    """Every row of the scenario's ``filename`` (accounts.csv, scheduled.csv)
    by ``key``, overlaid down its load order as ``_merge_scenario_rows`` does:
    a later scenario's row with the same key updates the earlier one, cell by
    cell. So an edit can find a row the scenario inherits, and write it back
    as an override (#202)."""
    name = scenario_dir.name
    rows: dict[str, Overlaid] = {}
    for scenario_name in load_order(scenario_dir.parent, name):
        path = _scenario_dir(scenario_dir.parent, scenario_name) / filename
        if not path.exists() or path.stat().st_size == 0:
            continue
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        if key not in frame.columns:
            continue
        for index, record in enumerate(frame.to_dict("records")):
            # Stripped, as the loader reads every cell.
            row = {str(k): str(v).strip() for k, v in record.items()}
            if not row[key]:
                continue
            prior = rows.get(row[key])
            merged = {**prior.row, **row} if prior is not None else row
            if scenario_name != name:
                rows[row[key]] = Overlaid(merged, None, scenario_name)
            else:
                inherited_from = prior.inherited_from if prior is not None else None
                rows[row[key]] = Overlaid(merged, index, inherited_from)
    return rows


class CalendarRow(NamedTuple):
    """One calendar.csv row of a scenario, as its load order leaves it."""
    row: dict[str, str]  # merged down the load order, every CALENDAR_HEADER column
    index: int | None  # its data row (0-based) in this scenario's own file, if there
    inherited_from: str | None  # the last scenario before this one to have it


def scenario_calendar_rows(scenario_dir: Path) -> list[CalendarRow]:
    """Every calendar row of the scenario, its own and inherited, overlaid
    down its load order the way ``_merge_calendar_rows`` overlays them: by
    name where a row has one the inherited calendar knows, else by its key
    columns. In the order they first appear (#202)."""
    name = scenario_dir.name
    entries: list[CalendarRow] = []
    by_key: dict[tuple[str, ...], int] = {}
    by_name: dict[str, int] = {}

    def full(row: dict[str, str]) -> dict[str, str]:
        return {column: row.get(column, "") for column in registries.CALENDAR_HEADER}

    for scenario_name in load_order(scenario_dir.parent, name):
        path = _scenario_dir(scenario_dir.parent, scenario_name) / "calendar.csv"
        if not path.exists() or path.stat().st_size == 0:
            continue
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        own = scenario_name == name
        for index, record in enumerate(frame.to_dict("records")):
            # Only the file's own columns: one it leaves out is inherited.
            row = {str(k): str(v).strip() for k, v in record.items()}
            row_name = row.get("name", "")
            position = by_name.get(row_name) if row_name else None
            if position is None:
                position = by_key.get(calendar_row_key(full(row)))
            if position is None:
                merged = full(row)
                prior_from = None
                position = len(entries)
                entries.append(CalendarRow(merged, None, None))
            else:
                old = entries[position]
                merged = full({**old.row, **row})
                prior_from = old.inherited_from
                by_key.pop(calendar_row_key(old.row), None)
                if old.row["name"] and old.row["name"] != merged["name"]:
                    by_name.pop(old.row["name"], None)
            by_key[calendar_row_key(merged)] = position
            if merged["name"]:
                by_name[merged["name"]] = position
            entries[position] = (
                CalendarRow(merged, index, prior_from) if own
                else CalendarRow(merged, None, scenario_name)
            )
    return entries
