#!/usr/bin/env python3
"""One-time migration of a data repo's import mappings to imports/ (#527).

The Quicken importers' mapping files were in two places with two
conventions. They move to one folder in the simulator data dir, shared by
every scenario, with column names that don't assume Quicken:

| From | To |
| --- | --- |
| each scenarios/<name>/quicken_account_map.csv | imports/quicken/accounts.csv |
| ../quicken/expense_category_map.csv | imports/quicken/categories.csv |
| ../quicken/expense_category_split.csv | imports/quicken/splits.csv |
| ../quicken/expense_excluded_payees.csv | imports/quicken/excluded_payees.csv |
| ../quicken/expense_account_map.csv | imports/expense_accounts.csv |

(``..`` is the data repo's top level, beside the simulator data dir.) The
per-scenario account maps are merged into one. A Quicken account two
scenarios map differently can't be: it is reported, and nothing is written
until the maps agree (edit them, or delete the one that is out of date).
Exports themselves (``quicken/Transaction Export.csv``) stay where they are.

Usage: scripts/migrate_527.py [DATA_DIR] [--write]

DATA_DIR defaults to $ANALYSIS_DATA_DIR. Without --write it only reports what
it would change. With it, the new files are written and the old ones
removed. Delete this script once every data repo is migrated.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from analysis.sim.imports import mapping

_OLD_ACCOUNT_MAP = "quicken_account_map.csv"
_OLD_ACCOUNT_HEADER = ("quicken_name", "account_name", "action", "note")
SOURCE = "quicken"

# old file in <data repo>/quicken/ -> (new path under imports/, old header, new header)
_MOVED: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {
    "expense_category_map.csv": (
        f"{SOURCE}/{mapping.CATEGORIES}", ("quicken_category", "target_category"),
        mapping.CATEGORIES_HEADER),
    "expense_category_split.csv": (
        f"{SOURCE}/{mapping.SPLITS}", ("category", "account", "target_category"),
        mapping.SPLITS_HEADER),
    "expense_excluded_payees.csv": (
        f"{SOURCE}/{mapping.EXCLUDED_PAYEES}", ("payee_pattern",), mapping.EXCLUDED_PAYEES_HEADER),
    "expense_account_map.csv": (
        mapping.EXPENSE_ACCOUNTS, ("category", "target_account"), mapping.EXPENSE_ACCOUNTS_HEADER),
}


@dataclass
class Plan:
    """The files the migration would write (header and rows, by path), the
    old files it would remove, a line per change for the report, and what
    stops it."""

    writes: dict[Path, tuple[tuple[str, ...], list[list[str]]]] = field(default_factory=dict)
    removes: list[Path] = field(default_factory=list)
    report: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _read(path: Path, header: tuple[str, ...], plan: Plan) -> list[list[str]] | None:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    if not rows or tuple(cell.strip() for cell in rows[0]) != header:
        plan.problems.append(f"{path}: expected header {list(header)}, got {rows[0] if rows else []}")
        return None
    width = len(header)
    return [[*(cell.strip() for cell in row), *[""] * width][:width] for row in rows[1:] if any(row)]


def _target(plan: Plan, path: Path) -> bool:
    """Whether ``path`` is free to write; a file already there is a problem."""
    if path.exists():
        plan.problems.append(f"{path} already exists; move it aside, or finish the migration by hand")
        return False
    return True


def plan_migration(data_dir: Path) -> Plan:
    plan = Plan()
    imports = mapping.imports_dir(data_dir)

    merged: dict[str, tuple[list[str], str]] = {}
    conflicts: dict[str, set[str]] = {}
    old_maps = sorted((data_dir / "scenarios").glob(f"*/{_OLD_ACCOUNT_MAP}"))
    for path in old_maps:
        scenario = path.parent.name
        rows = _read(path, _OLD_ACCOUNT_HEADER, plan)
        if rows is None:
            continue
        plan.removes.append(path)
        for quicken_name, account, action, note in rows:
            if not quicken_name:
                continue
            row = [quicken_name, "", action, account, "", note]
            earlier = merged.setdefault(quicken_name, (row, scenario))
            if (earlier[0][2], earlier[0][3]) != (action, account):
                conflicts.setdefault(quicken_name, {
                    f"{earlier[1]}: {earlier[0][2]} {earlier[0][3]}".rstrip()
                }).add(f"{scenario}: {action} {account}".rstrip())
        plan.report.append(f"{path}: {len(rows)} row(s) -> {imports / SOURCE / mapping.ACCOUNTS}")
    for quicken_name, places in sorted(conflicts.items()):
        plan.problems.append(
            f"Quicken account {quicken_name!r} is mapped differently: " + "; ".join(sorted(places)))
    accounts_path = imports / SOURCE / mapping.ACCOUNTS
    if merged and _target(plan, accounts_path):
        plan.writes[accounts_path] = (mapping.ACCOUNTS_HEADER, [row for row, _ in merged.values()])

    for name, (new, old_header, new_header) in _MOVED.items():
        path = data_dir.parent / "quicken" / name
        if not path.is_file():
            continue
        rows = _read(path, old_header, plan)
        if rows is None or not _target(plan, imports / new):
            continue
        plan.writes[imports / new] = (new_header, rows)
        plan.removes.append(path)
        plan.report.append(f"{path}: {len(rows)} row(s) -> {imports / new}")
    return plan


def apply(plan: Plan) -> None:
    for path, (header, rows) in plan.writes.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(header)
            writer.writerows(rows)
    for path in plan.removes:
        path.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("data_dir", nargs="?", default=os.environ.get("ANALYSIS_DATA_DIR"))
    parser.add_argument("--write", action="store_true", help="write the changes (default: report only)")
    args = parser.parse_args(argv)
    if not args.data_dir:
        parser.error("DATA_DIR not given and ANALYSIS_DATA_DIR not set")
    data_dir = Path(args.data_dir).resolve()
    if not (data_dir / "scenarios").is_dir():
        parser.error(f"{data_dir} has no scenarios/ folder")

    plan = plan_migration(data_dir)
    for line in plan.report:
        print(line)
    for problem in plan.problems:
        print(f"error: {problem}", file=sys.stderr)
    if plan.problems:
        print("nothing written", file=sys.stderr)
        return 1
    if not plan.report:
        print("nothing to migrate")
    elif args.write:
        apply(plan)
        print(f"wrote {len(plan.writes)} file(s), removed {len(plan.removes)}")
    else:
        print("(report only; rerun with --write)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
