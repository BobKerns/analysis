#!/usr/bin/env python3
"""Extract SSA's period life table into an aux life_table.csv (#222).

Reads SSA's Actuarial Life Table page (https://www.ssa.gov/oact/STATS/table4c6.html),
saved from a browser since ssa.gov refuses scripted downloads, and writes
`effective_date,age,q_female,q_male`: the death probability at each whole age,
dated December of the table's year (the page's "Period Life Table, <year>").

Usage: scripts/extract_ssa_life_table.py PAGE.html OUT.csv
"""
from __future__ import annotations

import argparse
import csv
import html
import re
import sys
from pathlib import Path


def extract(page: str) -> tuple[str, list[tuple[int, str, str]]]:
    """The table's year, and (age, male q, female q) for every age row."""
    found = re.search(r"Period Life Table, (\d{4})", page)
    if found is None:
        raise SystemExit("no 'Period Life Table, <year>' heading: is this SSA's table4c6 page?")
    rows: list[tuple[int, str, str]] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S | re.I):
        cells = [
            html.unescape(re.sub(r"<[^>]+>", "", c)).strip()
            for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I)
        ]
        # Age, then male q, lives, life expectancy, then the same for females.
        if len(cells) >= 7 and cells[0].isdigit():
            rows.append((int(cells[0]), cells[1], cells[4]))
    ages = [age for age, *_ in rows]
    if ages != list(range(len(ages))):
        raise SystemExit(f"expected ages 0, 1, 2, ... in order; found {ages[:5]}...")
    return found.group(1), rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("page", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args(argv)
    year, rows = extract(args.page.read_text(encoding="utf-8", errors="replace"))
    with args.out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["effective_date", "age", "q_female", "q_male"])
        for age, q_male, q_female in rows:
            writer.writerow([f"{year}-12-31", age, q_female, q_male])
    print(f"wrote {len(rows)} ages of the {year} period life table to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
