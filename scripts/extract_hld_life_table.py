#!/usr/bin/env python3
"""Extract a Human Life-Table Database country table into an aux life_table__<name>.csv (#315).

Reads one of lifetable.de's PDFs of a national statistics office's single-age
table (for Japan, MHLW's "Table A. Abridged Life Tables for Japan, <year>"),
either the PDF itself (through poppler's `pdftotext -layout`) or that command's
text output, and writes `effective_date,age,q_female,q_male` like
`life_table.csv`, dated December of the table's year.

Such a table ends in an open interval ("105 -") where everyone dies. Ages from
there to 108 are extrapolated: a quadratic in log hazard, log(-log(1 - q)),
fitted to the FIT_YEARS ages before it. The death rate keeps rising with age,
as it does in real old-age mortality, but a little more slowly each year.
Everyone left dies at 109, before turning 110.
For Japan's 2023 table that gives back the open interval's own remaining
life expectancy to within 0.02 years; the script prints both to check.

Usage: scripts/extract_hld_life_table.py TABLE.pdf|TABLE.txt OUT.csv
"""
from __future__ import annotations

import argparse
import csv
import math
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

# The last age, where q is 1: no one reaches 110. Past 105 the population is
# tiny, and many recorded supercentenarians are record errors.
LAST_AGE = 109
FIT_YEARS = 15  # ages 90-104 for a table open at 105: old age, but past the noisy small counts

# An age row: the age, an optional "(Y)" marker on the yearly table's first
# row, then q. The rows before "(Y)" are the weeks and months of the first year.
_ROW = re.compile(r"\s*(\d+)\s*(?:\(Y\))?\s+(\d\.\d+)\s")
_OPEN = re.compile(r"\s*(\d+)\s+-\s+1\.0+\s.*\s(\d+\.\d+)\s*$")


def read_text(path: Path) -> str:
    if path.suffix.lower() != ".pdf":
        return path.read_text(encoding="utf-8", errors="replace")
    try:
        done = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            check=True, capture_output=True, text=True,
        )
    except FileNotFoundError:
        raise SystemExit("pdftotext not found: install poppler, or pass its -layout output") from None
    return done.stdout


def extrapolate(q: dict[int, float], start: int) -> dict[int, float]:
    """q from ``start`` to LAST_AGE (where it is 1), from a quadratic in log hazard fitted to
    the FIT_YEARS ages before it (each year's hazard taken at its middle)."""
    ages = np.arange(start - FIT_YEARS, start)
    log_hazard = np.log(-np.log1p(-np.array([q[int(a)] for a in ages])))
    fit = np.polyfit(ages + 0.5, log_hazard, 2)
    return {
        age: 1.0 if age == LAST_AGE else min(1.0, 1 - math.exp(-math.exp(np.polyval(fit, age + 0.5))))
        for age in range(start, LAST_AGE + 1)
    }


def expectancy(q: dict[int, float], start: int) -> float:
    """Remaining life expectancy at ``start``, with a constant hazard within
    each year and everyone dying in LAST_AGE's year at the latest."""
    alive, years = 1.0, 0.0
    for age in range(start, LAST_AGE + 1):
        if q[age] >= 1 or age == LAST_AGE:
            return years + alive / 2
        hazard = -math.log1p(-q[age])
        years += alive * q[age] / hazard
        alive *= 1 - q[age]
    return years


def extract(text: str) -> tuple[str, dict[str, dict[int, str]]]:
    """The table's year, and each sex's q by age through LAST_AGE."""
    found = re.search(r"Life Tables? for .*?, (\d{4})", text)
    if found is None:
        raise SystemExit("no 'Life Tables for <country>, <year>' heading: is this an HLD table?")
    tables: dict[str, dict[int, str]] = {}
    sex: str | None = None
    yearly = False
    for line in text.splitlines():
        # A section starts with the sex flush left; page headers repeat it indented.
        if line.rstrip() in ("Male", "Female"):
            sex, yearly = line.strip().lower(), False
            tables[sex] = {}
            continue
        if sex is None:
            continue
        if "(Y)" in line:
            yearly = True
        if not yearly:
            continue
        if found_open := _OPEN.match(line):
            start, table_e = int(found_open.group(1)), float(found_open.group(2))
            q = {age: float(value) for age, value in tables[sex].items()}
            tail = extrapolate(q, start)
            print(f"{sex}: life expectancy at {start} is {table_e:.2f} in the table, "
                  f"{expectancy(tail, start):.2f} extrapolated")
            for age, value in tail.items():
                tables[sex][age] = f"{value:.6f}"
            sex = None
        elif row := _ROW.match(line):
            tables[sex][int(row.group(1))] = row.group(2)
    for name in ("female", "male"):
        ages = sorted(tables.get(name, {}))
        if ages != list(range(LAST_AGE + 1)):
            raise SystemExit(f"{name}: expected ages 0..{LAST_AGE}; found {ages[:3]}...{ages[-3:]}")
    return found.group(1), tables


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("table", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args(argv)
    year, tables = extract(read_text(args.table))
    with args.out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["effective_date", "age", "q_female", "q_male"])
        for age in range(LAST_AGE + 1):
            writer.writerow([f"{year}-12-31", age, tables["female"][age], tables["male"][age]])
    print(f"wrote ages 0-{LAST_AGE} of the {year} table to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
