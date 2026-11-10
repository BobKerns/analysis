#!/usr/bin/env python3
"""One-time migration of a simulator data repo to residence.csv (#509).

Where the household lives was a state tax's ``bracket_series`` and each
move a calendar ``swap_aux_table`` of ``state_brackets``. Now it is the
scenario's ``residence.csv``: from each row's month, a state and optionally
a county (the series of its ``state_local_tax`` table). For each scenario
folder:

- ``taxes/state.csv``'s ``bracket_series`` becomes ``residence.csv``'s first
  row, dated the scenario's start (or the date of the first row an earlier
  scenario of its lineage gets, so a child's override replaces it), and
  leaves the params.
- ``taxes/local.csv``'s ``bracket_series`` becomes that row's county, unless
  it is the state's own, and leaves the params.
- Each ``swap_aux_table`` of ``state_brackets`` becomes a residence row on its
  date, its ``source_series`` the state, and one of ``state_local_tax`` a row
  with its ``source_series`` the county; both leave ``calendar.csv``, as does a
  swap of another state table on the same date as a state's.

A move into a state with no income tax swapped in the ``none`` table, which
doesn't say which state; its description (``Move to Florida (FL)``, as
``sim tax move`` wrote it) does. What the script can't read that way (a
varied or relative date, a ``none`` it can't name, a swap of another state
table alone) is reported, and nothing is written until it is fixed by hand.

Usage: scripts/migrate_509.py [DATA_DIR] [--write]

DATA_DIR defaults to $ANALYSIS_DATA_DIR. Without --write it only reports what
it would change. Every other row and cell is kept as it is. Delete this
script once every data repo is migrated.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from analysis.sim import states
from analysis.sim.dates import month_end_iso
from analysis.sim.lineage import load_order, scenario_dir

RESIDENCE_HEADER = ("effective_date", "state", "county", "description")
STATE_KEYED = frozenset({
    "state_brackets", "state_standard_deduction", "state_ltcg_brackets", "state_stcg_brackets",
    "state_ltcg_exclusion", "state_social_security", "state_surcharge", "state_hsa",
    "state_retirement_exclusion", "state_local_tax",
})


def _read(path: Path) -> tuple[list[str], list[list[str]], str]:
    text = path.read_text(encoding="utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    rows = list(csv.reader(io.StringIO(text, newline="")))
    return (rows[0] if rows else []), rows[1:], newline


def _render(header: list[str] | tuple[str, ...], rows: list[list[str]], newline: str) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator=newline)
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue()


def _cell(header: list[str], row: list[str], name: str) -> str:
    index = header.index(name) if name in header else -1
    return row[index].strip() if 0 <= index < len(row) else ""


def _params(header: list[str], row: list[str]) -> dict | None:
    try:
        params = json.loads(_cell(header, row, "params_json") or "{}")
    except json.JSONDecodeError:
        return None
    return params if isinstance(params, dict) else None


def _with_params(header: list[str], row: list[str], params: dict) -> list[str]:
    index = header.index("params_json")
    text = json.dumps(params, separators=(",", ":")) if params else ""
    return [*row[:index], text, *row[index + 1:]]


@dataclass
class Plan:
    """What the migration would write, by path, a line per change for the
    report, and what it can't move as it is."""
    writes: dict[Path, str] = field(default_factory=dict)
    report: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


@dataclass
class _Folder:
    """One scenario folder's residence rows by month end, and its files."""
    residence: dict[str, dict[str, str]] = field(default_factory=dict)
    first: str | None = None  # the month of its first (starting) row


def _start(scenarios: Path, name: str) -> str:
    """The scenario's start as its lineage merges it: the last scenario.csv in
    load order that sets one."""
    start = ""
    for each in load_order(scenarios, name):
        path = scenario_dir(scenarios, each) / "scenario.csv"
        if path.is_file():
            header, rows, _ = _read(path)
            if rows and _cell(header, rows[0], "start"):
                start = _cell(header, rows[0], "start")
    return start


def _named_state(description: str) -> str | None:
    """The state a ``Move to Florida (FL)`` description names."""
    found = re.search(r"\(([A-Z]{2})\)\s*$", description)
    if found and found.group(1) in states.BY_CODE:
        return found.group(1)
    match = re.match(r"\s*Move to (.+?)\s*$", description)
    state = states.find(match.group(1)) if match else None
    return state.code if state else None


def plan(data_dir: Path) -> Plan:
    result = Plan()
    scenarios = data_dir / "scenarios"
    names = sorted(p.name for p in scenarios.iterdir() if p.is_dir()) if scenarios.is_dir() else []
    folders: dict[str, _Folder] = {}
    found = False

    def folder_of(name: str) -> _Folder:
        if name not in folders:
            folders[name] = _Folder()
        return folders[name]

    # Each scenario's taxes first, ancestors before descendants, so a child's
    # starting row can take its parent's date.
    order: list[str] = []
    for name in names:
        order += [n for n in load_order(scenarios, name) if n not in order]
    for name in order:
        folder = scenario_dir(scenarios, name)
        state_path, local_path = folder / "taxes" / "state.csv", folder / "taxes" / "local.csv"
        county = ""
        state_series = None
        for path in (state_path, local_path):
            if not path.is_file():
                continue
            header, rows, newline = _read(path)
            if not rows or (params := _params(header, rows[0])) is None:
                continue
            if "bracket_series" not in params:
                continue
            found = True
            series = str(params.pop("bracket_series"))
            if path == state_path:
                state_series = series
            elif series != (state_series or ""):
                county = series
            result.writes[path] = _render(header, [_with_params(header, rows[0], params)], newline)
            result.report.append(f"{path}: drops bracket_series {series!r}")
        if county and state_series is None:
            result.problems.append(
                f"{local_path}: names its own bracket_series {county!r} without a state "
                "file beside it; add the county to the residence.csv row by hand")
        if state_series is None:
            continue
        if state_series == states.NO_TAX_SERIES or state_series not in states.BY_CODE:
            result.problems.append(
                f"{state_path}: bracket_series {state_series!r} doesn't say which state; "
                "write residence.csv's first row by hand (or run `sim tax state` after)")
            continue
        inherited = [folders[n].first for n in load_order(scenarios, name)[:-1]
                     if n in folders and folders[n].first]
        start = inherited[-1] if inherited else _start(scenarios, name)
        if not start:
            result.problems.append(f"{folder}: no start to date the residence from")
            continue
        month = month_end_iso(start)
        own = folder_of(name)
        own.first = start
        own.residence[month] = {"effective_date": start[:7], "state": state_series,
                                "county": county, "description": ""}

    # The moves: each swap of a state table becomes a residence row.
    replaced: list[tuple[str, str, str]] = []  # (scenario, series replaced, where)
    for name in order:
        folder = scenario_dir(scenarios, name)
        path = folder / "calendar.csv"
        if not path.is_file():
            continue
        header, rows, newline = _read(path)
        kept: list[list[str]] = []
        swaps: dict[str, dict[str, str]] = {}  # by month: aux kind -> source series
        described: dict[str, str] = {}
        for row in rows:
            params = _params(header, row) if _cell(header, row, "kind") == "swap_aux_table" else None
            kind = (params or {}).get("aux_kind")
            if kind not in STATE_KEYED:
                kept.append(row)
                continue
            found = True
            where = f"{path} ({_cell(header, row, 'date')} {kind})"
            date = _cell(header, row, "date")
            if _cell(header, row, "vary_distribution"):
                result.problems.append(f"{where}: a varied move can't be a residence row; "
                                       "fix it by hand")
                kept.append(row)
                continue
            try:
                month = month_end_iso(date)
            except ValueError:
                result.problems.append(f"{where}: date {date!r} isn't a month (a relative date?); "
                                       "write the residence row by hand")
                kept.append(row)
                continue
            swaps.setdefault(month, {})[str(kind)] = str((params or {}).get("source_series", ""))
            if kind == "state_brackets":
                described[month] = _cell(header, row, "description")
                if series := _cell(header, row, "series"):
                    replaced.append((name, series, where))
        if len(kept) == len(rows):
            continue
        own = folder_of(name)
        for month, kinds in sorted(swaps.items()):
            where = f"{path} ({month[:7]})"
            state = kinds.get("state_brackets")
            if state is None and "state_local_tax" not in kinds:
                result.problems.append(
                    f"{where}: swaps {', '.join(sorted(kinds))} without a move; "
                    "a state's tables follow where it lives now, so fix it by hand")
                continue
            if state == states.NO_TAX_SERIES:
                state = _named_state(described.get(month, ""))
                if state is None:
                    result.problems.append(
                        f"{where}: a move to the 'none' tables, and the description doesn't "
                        "name the state; write the residence row by hand")
                    continue
            if state is None:
                state = _state_on(folders, scenarios, name, month)
                if state is None:
                    result.problems.append(f"{where}: a county move with no state before it")
                    continue
            if state not in states.BY_CODE:
                result.problems.append(f"{where}: {state!r} isn't a state's postal code")
                continue
            # A residence row reads the new state's own tables: a companion
            # swap into anything else (another state's, a special table)
            # can't be kept, so it is for the user to decide.
            series = states.NO_TAX_SERIES if kinds.get("state_brackets") == states.NO_TAX_SERIES else state
            odd = sorted(k for k, source in kinds.items()
                         if k not in ("state_brackets", "state_local_tax") and source != series)
            if odd:
                result.problems.append(
                    f"{where}: swaps {', '.join(odd)} into a table other than {series}'s, which a "
                    "residence row can't say; fix it by hand")
                continue
            own.residence[month] = {
                "effective_date": month[:7], "state": state,
                "county": kinds.get("state_local_tax", ""),
                "description": described.get(month, ""),
            }
        result.writes[path] = _render(header, kept, newline)
        result.report.append(f"{path}: {len(rows) - len(kept)} state table swap(s) -> residence.csv")

    # A swap replaced only the table its series named: the state tax's
    # bracket_series, where the household started. A scenario that inherits
    # it but started elsewhere didn't move; as a residence row it would.
    for name, series, where in replaced:
        for each in order:
            if name not in load_order(scenarios, each):
                continue
            state = _state_on(folders, scenarios, each, "")
            if state is not None and state != series:
                result.problems.append(
                    f"{where}: replaces {series}'s tables, but scenario {each} starts in "
                    f"{state}, so it didn't move; fix it by hand")
    for name, own in folders.items():
        if not own.residence:
            continue
        target = scenario_dir(scenarios, name) / "residence.csv"
        if target.exists():
            result.problems.append(f"{target}: already exists; merge by hand")
            continue
        rows = [[row[c] for c in RESIDENCE_HEADER] for _, row in sorted(own.residence.items())]
        result.writes[target] = _render(RESIDENCE_HEADER, rows, "\n")
        for row in rows:
            result.report.append(f"{target}: {row[0]} {row[1]}" + (f" county {row[2]}" if row[2] else ""))
    if not found:
        result.problems.append(f"{data_dir}: no bracket_series or state table swaps; already migrated?")
    return result


def _state_on(folders: dict[str, _Folder], scenarios: Path, name: str, month: str) -> str | None:
    """The state the lineage's residence rows have the household in by
    ``month`` (for a county move, which keeps the state), or with ``month``
    blank, where it starts."""
    rows: dict[str, dict[str, str]] = {}
    for each in load_order(scenarios, name):
        if each in folders:
            rows.update(folders[each].residence)
    # A row of the same month counts: a parent's move then is where the
    # county is.
    if not month:
        return rows[min(rows)]["state"] if rows else None
    lived = [rows[m]["state"] for m in sorted(rows) if m <= month]
    return lived[-1] if lived else None


def apply(result: Plan) -> None:
    for path, text in result.writes.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("data_dir", nargs="?", type=Path,
                        default=os.environ.get("ANALYSIS_DATA_DIR"))
    parser.add_argument("--write", action="store_true", help="Write the changes.")
    args = parser.parse_args()
    if args.data_dir is None:
        sys.exit("give DATA_DIR or set $ANALYSIS_DATA_DIR")
    result = plan(Path(args.data_dir))
    for line in result.report:
        print(line)
    if result.problems:
        for line in result.problems:
            print(f"error: {line}", file=sys.stderr)
        sys.exit(1)
    if args.write:
        apply(result)
        print("written")
    else:
        print("dry run; rerun with --write to apply")


if __name__ == "__main__":
    main()
