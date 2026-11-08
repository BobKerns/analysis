#!/usr/bin/env python3
"""One-time migration of a simulator data repo's income tax rules to tax
jurisdiction files (#507).

Each `tax_accumulator` row of a scenario's `scheduled.csv` becomes that
scenario's `taxes/federal.csv` or `taxes/state.csv` (by the rule's `scope`),
and leaves `scheduled.csv`:

- `from_account` becomes `source_chain` and `to_account` `destination`
- `start`, `end` and `description` are kept
- `params_json` is kept less `scope` and `year`

The settlement keeps the rule's place in the month only for the default
priority (400) and the April, previous-year schedule. A rule that differs
(another month, `year` "current" or a year, a priority, a lot, tags, a
year-to-date amount, two of one scope in a folder, a calendar row naming
it, or a descendant scenario's partial override of it) is reported, and
nothing is written until it is fixed by hand. A ``fill_bracket`` rule's
``tax_rule`` param is dropped: its ``scope`` names the jurisdiction now.

Usage: scripts/migrate_507_taxes.py [DATA_DIR] [--write]

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
import sys
from dataclasses import dataclass, field
from pathlib import Path

from analysis.sim.lineage import load_order

RULE_KIND = "tax_accumulator"
TAX_HEADER = (
    "kind", "upstream", "source_chain", "destination", "schedule", "start", "end",
    "params_json", "description",
)
KINDS = {"federal": "federal", "state": "state_tables"}


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


@dataclass
class Plan:
    """What the migration would write, by path, a line per change for the
    report, and the rules it can't move as they are."""
    writes: dict[Path, str] = field(default_factory=dict)
    report: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _jurisdiction(path: Path, header: list[str], row: list[str], problems: list[str]) -> tuple[str, list[str]] | None:
    """The jurisdiction a tax rule becomes and its file's row, or None (with
    the problems) when it can't move as it is."""
    name = _cell(header, row, "name")
    where = f"{path} ({name})"
    try:
        params = json.loads(_cell(header, row, "params_json") or "{}")
    except json.JSONDecodeError:
        problems.append(f"{where}: params_json is not JSON")
        return None
    if not isinstance(params, dict):
        problems.append(f"{where}: params_json is not a JSON object")
        return None
    scope = str(params.pop("scope", "federal"))
    year = params.pop("year", "previous")
    before = len(problems)
    if scope not in KINDS:
        problems.append(f"{where}: scope {scope!r} is neither federal nor state")
    if year != "previous":
        problems.append(f"{where}: year {year!r}: a jurisdiction settles the year before, each April")
    if _cell(header, row, "cadence") != "annual" or _cell(header, row, "month_of_year") != "4":
        problems.append(f"{where}: not annual in April: a jurisdiction settles each April")
    if _cell(header, row, "priority") not in ("", "400"):
        problems.append(f"{where}: priority {_cell(header, row, 'priority')}: a settlement "
                        "takes the default rule's place (400)")
    for column in ("from_lot", "to_lot", "tags", "ytd_amount", "ytd_preroll", "person"):
        if _cell(header, row, column) not in ("", "0"):
            problems.append(f"{where}: {column} {_cell(header, row, column)!r} has no "
                            "place in a jurisdiction file")
    for column in ("from_account", "to_account"):
        if not _cell(header, row, column):
            problems.append(f"{where}: {column} is blank; move it by hand")
    if len(problems) > before:
        return None
    cells = {
        "kind": KINDS[scope], "upstream": "",
        "source_chain": _cell(header, row, "from_account"),
        "destination": _cell(header, row, "to_account"),
        "schedule": "april",
        "start": _cell(header, row, "start"), "end": _cell(header, row, "end"),
        "params_json": json.dumps(params, separators=(",", ":")) if params else "",
        "description": _cell(header, row, "description"),
    }
    return scope, [cells[column] for column in TAX_HEADER]


def _tax_rule_names(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    header, rows, _ = _read(path)
    return {_cell(header, row, "name") for row in rows
            if _cell(header, row, "amount_rule_kind") == RULE_KIND} - {""}


def _without_tax_rule(header: list[str], row: list[str]) -> list[str] | None:
    """``row`` with ``tax_rule`` dropped from its params_json, or None when
    it has none."""
    index = header.index("params_json") if "params_json" in header else -1
    if not 0 <= index < len(row):
        return None
    try:
        params = json.loads(row[index] or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(params, dict) or "tax_rule" not in params:
        return None
    params.pop("tax_rule")
    return [*row[:index], json.dumps(params, separators=(",", ":")), *row[index + 1:]]


def _scenario_name(scenarios: Path, folder: Path) -> str | None:
    return folder.name if folder.parent == scenarios else None


def plan(data_dir: Path) -> Plan:
    result = Plan()
    found = 0
    scenarios = data_dir / "scenarios"
    # Every scenario folder: one with a scheduled.csv or a calendar.csv (a
    # descendant may have only the calendar row naming a parent's rule).
    folders = sorted({p.parent for name in ("scheduled.csv", "calendar.csv")
                      for p in data_dir.rglob(name) if ".git" not in p.parts})
    own_rules = {f: _tax_rule_names(f / "scheduled.csv") for f in folders}
    for folder in folders:
        # The tax rules this scenario sees: its own and its lineage's.
        name_of = _scenario_name(scenarios, folder)
        lineage = [scenarios / n for n in load_order(scenarios, name_of)] if name_of else [folder]
        seen = set().union(*(own_rules.get(f, set()) for f in lineage))
        calendar = folder / "calendar.csv"
        if calendar.is_file():
            cal_header, cal_rows, _ = _read(calendar)
            for named in sorted({_cell(cal_header, r, "scheduled_name") for r in cal_rows} & seen):
                result.problems.append(
                    f"{calendar}: a row names tax rule {named!r}, which becomes a jurisdiction "
                    "file; change that row by hand")
        path = folder / "scheduled.csv"
        if not path.is_file():
            continue
        header, rows, newline = _read(path)
        kept, moved, changed = [], {}, False
        for row in rows:
            name = _cell(header, row, "name")
            if _cell(header, row, "amount_rule_kind") != RULE_KIND:
                if name in seen:
                    result.problems.append(
                        f"{path} ({name}): overrides the tax rule {name!r} a parent defines; "
                        "merge it into that rule's taxes/ file by hand")
                elif (fixed := _without_tax_rule(header, row)) is not None:
                    row, changed = fixed, True
                    result.report.append(f"{path}: rule {name!r} drops its tax_rule param")
                kept.append(row)
                continue
            found += 1
            made = _jurisdiction(path, header, row, result.problems)
            if made is None:
                continue
            scope, cells = made
            target = path.parent / "taxes" / f"{scope}.csv"
            if scope in moved:
                result.problems.append(f"{path}: two {scope} tax rules ({moved[scope]}, {name}); "
                                       "a scenario has one of each")
                continue
            if target.exists():
                result.problems.append(f"{target}: already exists; merge {name!r} into it by hand")
                continue
            moved[scope] = name
            result.writes[target] = _render(TAX_HEADER, [cells], newline)
            result.report.append(f"{path}: rule {name!r} -> {target.relative_to(data_dir)}")
        if moved or changed:
            result.writes[path] = _render(header, kept, newline)
    if not found:
        result.problems.append(f"{data_dir}: no {RULE_KIND} rules left; already migrated?")
    return result


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
