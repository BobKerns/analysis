#!/usr/bin/env python3
"""One-time conversion of a simulator data repo to natural CSV units (#130).

Before #130 the scenario CSVs carried rates and factors in micros and
calendar amounts in cents or micros. This rewrites them as the loader now
reads them:

- `annual_rate_micros` (rate and inflation series), `marginal_rate_micros`
  (tax brackets) and `rate_micros` (scheduled.csv, energy_credits) become
  `annual_rate`, `marginal_rate` and `rate`, in percent: 45000 -> 4.5%.
- `factor_micros` (rmd_factors) becomes `factor`, a plain number:
  26500000 -> 26.5.
- calendar.csv: a `set_rate` row's `amount` and its `vary_*` amount bounds
  go from micros to percent, a `set_amount` or `one_off_transfer` row's from
  cents to dollars (120000 -> 1200.00); `vary_innovation_sd` goes from
  micros to percent.
- allocations.csv's `weight` and a chain file's `percent`, already percents,
  gain the % sign the loader now requires on every percent: 60 -> 60%.
- A scenario's series/README.md gets its column description updated.

Usage: scripts/convert_units_130.py [DATA_DIR] [--write]

DATA_DIR defaults to $ANALYSIS_DATA_DIR. Without --write it only reports
what it would change. It refuses a data repo with no pre-#130 columns left,
since a calendar.csv can't tell by its header whether it is converted and
would otherwise be converted twice. Rows, column order and every other cell
are kept as they are. Delete this script once every data repo is converted.
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from decimal import Decimal
from pathlib import Path

PERCENT_COLUMNS = {
    "annual_rate_micros": "annual_rate",
    "marginal_rate_micros": "marginal_rate",
    "rate_micros": "rate",
}
NUMBER_COLUMNS = {"factor_micros": "factor"}
LEGACY_COLUMNS = {*PERCENT_COLUMNS, *NUMBER_COLUMNS}

AMOUNT_UNITS = {"set_rate": "percent", "set_amount": "dollars", "one_off_transfer": "dollars"}
VARY_AMOUNT_COLUMNS = ("vary_low", "vary_high", "vary_median", "vary_sd", "vary_mode")

OLD_README_LINE = "- `annual_rate_micros`: the annual rate in micros (1% is `10000`)."
NEW_README_LINE = "- `annual_rate`: the annual rate as a percent, with its sign (`4.5%`)."


def _plain(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "0" if text in ("-0", "0") else text


def micros_to_percent(raw: str) -> str:
    return _plain(Decimal(int(raw)) / 10_000) + "%"


def micros_to_number(raw: str) -> str:
    return _plain(Decimal(int(raw)) / 1_000_000)


def cents_to_dollars(raw: str) -> str:
    cents = int(raw)
    whole, rem = divmod(abs(cents), 100)
    return f"{'-' if cents < 0 else ''}{whole}.{rem:02d}"


CONVERT = {"percent": micros_to_percent, "dollars": cents_to_dollars}


def _read(path: Path) -> tuple[list[str], list[list[str]], str]:
    text = path.read_text(encoding="utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    rows = list(csv.reader(io.StringIO(text, newline="")))
    return (rows[0] if rows else []), rows[1:], newline


def _write(path: Path, header: list[str], rows: list[list[str]], newline: str) -> None:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator=newline)
    writer.writerow(header)
    writer.writerows(rows)
    path.write_text(out.getvalue(), encoding="utf-8", newline="")


def convert_columns(header: list[str], rows: list[list[str]]) -> tuple[list[str], int]:
    """Rename and convert a file's legacy micros columns; returns the new
    header and how many cells changed."""
    changed = 0
    new_header = list(header)
    for index, name in enumerate(header):
        if name in PERCENT_COLUMNS:
            new_header[index], convert = PERCENT_COLUMNS[name], micros_to_percent
        elif name in NUMBER_COLUMNS:
            new_header[index], convert = NUMBER_COLUMNS[name], micros_to_number
        else:
            continue
        for row in rows:
            if index < len(row) and row[index].strip():
                row[index] = convert(row[index].strip())
                changed += 1
    return new_header, changed


def add_percent_signs(header: list[str], rows: list[list[str]], column: str) -> int:
    """Append a % to each value in ``column`` that lacks one; returns how many."""
    if column not in header:
        return 0
    index = header.index(column)
    changed = 0
    for row in rows:
        if index < len(row) and row[index].strip() and not row[index].strip().endswith("%"):
            row[index] = row[index].strip() + "%"
            changed += 1
    return changed


def convert_calendar(header: list[str], rows: list[list[str]], where: Path) -> int:
    """Convert a calendar.csv's amount cells in place; returns how many changed."""
    col = {name: index for index, name in enumerate(header)}

    def cell(row: list[str], name: str) -> str:
        index = col.get(name)
        return row[index].strip() if index is not None and index < len(row) else ""

    changed = 0
    for line, row in enumerate(rows, start=2):
        unit = AMOUNT_UNITS.get(cell(row, "kind"))
        cells: list[tuple[str, str]] = []
        if unit is not None:
            cells.append(("amount", unit))
            if cell(row, "vary_target") == "amount":
                cells.extend((name, unit) for name in VARY_AMOUNT_COLUMNS)
        cells.append(("vary_innovation_sd", "percent"))
        for name, cell_unit in cells:
            raw = cell(row, name)
            if not raw:
                continue
            if not raw.lstrip("-").isdigit():
                sys.exit(f"{where} row {line}: {name} {raw!r} is not a whole number; "
                         "is this calendar already converted? Nothing was written.")
            row[col[name]] = CONVERT[cell_unit](raw)
            changed += 1
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("data_dir", nargs="?", default=os.environ.get("ANALYSIS_DATA_DIR"))
    parser.add_argument("--write", action="store_true", help="rewrite the files (default: report only)")
    args = parser.parse_args()
    if not args.data_dir:
        sys.exit("give DATA_DIR or set ANALYSIS_DATA_DIR")
    data_dir = Path(args.data_dir)
    # The simulator's inputs live under sim/ in the data repo; accept either.
    if (data_dir / "sim" / "scenarios").is_dir():
        data_dir = data_dir / "sim"
    if not (data_dir / "scenarios").is_dir():
        sys.exit(f"{data_dir} has no scenarios/ folder")

    paths = sorted(p for p in data_dir.rglob("*.csv") if ".git" not in p.parts)
    if not any(LEGACY_COLUMNS & set(_read(p)[0]) for p in paths):
        sys.exit(f"{data_dir} has no pre-#130 columns left: already converted. Nothing was written.")

    planned: list[tuple[Path, list[str], list[list[str]], str, int]] = []
    for path in paths:
        header, rows, newline = _read(path)
        new_header, changed = convert_columns(header, rows)
        if path.name == "calendar.csv":
            changed += convert_calendar(header, rows, path)
        elif path.name == "allocations.csv":
            changed += add_percent_signs(header, rows, "weight")
        elif path.parent.name == "chains":
            changed += add_percent_signs(header, rows, "percent")
        if changed or new_header != header:
            planned.append((path, new_header, rows, newline, changed))
    readmes = [
        p for p in sorted(data_dir.glob("scenarios/*/series/README.md"))
        if OLD_README_LINE in p.read_text(encoding="utf-8")
    ]

    for path, header, _rows, _newline, changed in planned:
        print(f"{path.relative_to(data_dir)}: {changed} cells; columns {','.join(header)}")
    for path in readmes:
        print(f"{path.relative_to(data_dir)}: column description")
    if not args.write:
        print(f"\n{len(planned)} CSV files and {len(readmes)} READMEs would change; "
              "run again with --write to rewrite them.")
        return
    for path, header, rows, newline, _changed in planned:
        _write(path, header, rows, newline)
    for path in readmes:
        path.write_text(
            path.read_text(encoding="utf-8").replace(OLD_README_LINE, NEW_README_LINE),
            encoding="utf-8",
        )
    print(f"\nrewrote {len(planned)} CSV files and {len(readmes)} READMEs.")


if __name__ == "__main__":
    main()
