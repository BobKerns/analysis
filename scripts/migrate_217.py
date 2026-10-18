#!/usr/bin/env python3
"""One-time migration of a simulator data repo to the #217 account layout.

- Every `account_values.csv` is renamed `account_lots.csv` and gains a
  `subkind` column after `lot`.
- An account whose subkind is `traditional_ira`, `roth_ira`,
  `traditional_401k` or `roth_401k` becomes subkind `retirement` with a blank
  `tax_treatment`; its old subkind moves to each of its lots' `subkind`.
- Such an account with no lot row in the folder whose accounts.csv defines it
  gets a zero-balance `default` lot there, to carry that subkind (starting an
  `account_lots.csv` in that folder if it had no lots file).

Usage: scripts/migrate_217.py [DATA_DIR] [--write]

DATA_DIR defaults to $ANALYSIS_DATA_DIR. Without --write it only reports what
it would change. It refuses a data repo with no `account_values.csv` left.
Rows, column order and every other cell are kept as they are. Delete this
script once every data repo is migrated.
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

OLD_NAME = "account_values.csv"
NEW_NAME = "account_lots.csv"
LOT_SUBKINDS = ("traditional_ira", "roth_ira", "traditional_401k", "roth_401k")
RETIREMENT = "retirement"


def _read(path: Path) -> tuple[list[str], list[list[str]], str]:
    text = path.read_text(encoding="utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    rows = list(csv.reader(io.StringIO(text, newline="")))
    return (rows[0] if rows else []), rows[1:], newline


def _render(header: list[str], rows: list[list[str]], newline: str) -> str:
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
    """What the migration would write: new file contents by path, the old
    lots files to remove, and a line per change for the report."""

    writes: dict[Path, str] = field(default_factory=dict)
    removes: list[Path] = field(default_factory=list)
    report: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _files(data_dir: Path, name: str) -> list[Path]:
    return sorted(p for p in data_dir.rglob(name) if ".git" not in p.parts)


def plan(data_dir: Path) -> Plan:
    result = Plan()
    lots_files = _files(data_dir, OLD_NAME)
    if not lots_files:
        result.problems.append(f"{data_dir}: no {OLD_NAME} left; already migrated?")
        return result

    # Accounts to convert, by name, with their old subkind. Account names are
    # one namespace across scenarios, so one name must mean one subkind.
    moved: dict[str, str] = {}
    defined_in: dict[str, Path] = {}
    for path in _files(data_dir, "accounts.csv"):
        header, rows, newline = _read(path)
        if "subkind" not in header:
            continue
        changed = 0
        for row in rows:
            subkind = _cell(header, row, "subkind")
            if subkind not in LOT_SUBKINDS:
                continue
            name = _cell(header, row, "name")
            if moved.setdefault(name, subkind) != subkind:
                result.problems.append(
                    f"{path}: account {name!r} is {subkind} here but {moved[name]} elsewhere"
                )
            defined_in.setdefault(name, path.parent)
            row[header.index("subkind")] = RETIREMENT
            if "tax_treatment" in header and header.index("tax_treatment") < len(row):
                row[header.index("tax_treatment")] = ""
            changed += 1
        if changed:
            result.writes[path] = _render(header, rows, newline)
            result.report.append(f"{path}: {changed} account(s) now subkind {RETIREMENT}")

    # Accounts with a lot row, by the folder that has it.
    with_lots: dict[Path, set[str]] = {}
    for path in lots_files:
        header, rows, newline = _read(path)
        if "subkind" in header:
            result.problems.append(f"{path}: already has a subkind column")
            continue
        if path.with_name(NEW_NAME).exists():
            result.problems.append(f"{path}: {NEW_NAME} already exists beside it; merge them by hand")
            continue
        at = header.index("lot") + 1 if "lot" in header else len(header)
        set_count = 0
        for row in rows:
            row.extend([""] * (len(header) - len(row)))
            account = _cell(header, row, "account_name")
            with_lots.setdefault(path.parent, set()).add(account)
            subkind = moved.get(account, "")
            set_count += bool(subkind)
            row.insert(at, subkind)
        new_header = [*header[:at], "subkind", *header[at:]]
        result.writes[path.with_name(NEW_NAME)] = _render(new_header, rows, newline)
        result.removes.append(path)
        result.report.append(
            f"{path}: renamed {NEW_NAME}; subkind set on {set_count} lot(s)"
        )

    # A converted account needs a lot in the folder that defines it: a lot
    # only in some derived scenario leaves the others, and the defining one,
    # with none. A zero default lot there carries the subkind.
    for account in sorted(moved):
        folder = defined_in[account]
        if account in with_lots.get(folder, set()):
            continue
        target = folder / NEW_NAME
        if target not in result.writes:
            # This folder had no lots file: start one with the others' columns.
            others = [text for path, text in result.writes.items() if path.name == NEW_NAME]
            if not others:
                result.problems.append(f"{folder}: no {NEW_NAME} to seed {account!r}'s lot in")
                continue
            some_header, _rows, newline = _read_text(others[0])
            result.writes[target] = _render(some_header, [], newline)
        header, rows, newline = _read_text(result.writes[target])
        row = [""] * len(header)
        row[header.index("account_name")] = account
        row[header.index("lot")] = "default"
        row[header.index("subkind")] = moved[account]
        row[header.index("opening_balance")] = "0.00"
        rows.append(row)
        result.writes[target] = _render(header, rows, newline)
        result.report.append(f"{target}: added a zero default lot for {account!r} ({moved[account]})")
    return result


def _read_text(text: str) -> tuple[list[str], list[list[str]], str]:
    newline = "\r\n" if "\r\n" in text else "\n"
    rows = list(csv.reader(io.StringIO(text, newline="")))
    return rows[0], rows[1:], newline


def apply(result: Plan) -> None:
    for path, text in result.writes.items():
        path.write_text(text, encoding="utf-8", newline="")
    for path in result.removes:
        path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
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
