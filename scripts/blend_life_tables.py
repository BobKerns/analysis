#!/usr/bin/env python3
"""Blend aux life tables into one, by weight, for a lifespan between populations (#315).

Each input is a `life_table.csv`-style file (`effective_date,age,q_female,q_male`);
the latest vintage in each is used. At every age and for each sex the blend's
yearly death rate (hazard, -log(1 - q)) is the weighted average of the inputs',
so `us 0.5, jp 0.5` is a person whose risk each year sits halfway between the
two populations'. (Averaging q itself is nearly the same at most ages;
averaging survival curves would instead describe a mixed group, part of whom
follow each table.) Weights are scaled to add to 1. Past an input's last age
its q is 1, so the blend ends where the shortest input does.

The output is dated the latest of the inputs' vintages, for a scenario's aux/
or the data repo's: name it `life_table__<name>.csv` and set `life_table` to
`<name>` in a lifespan.csv row.

Usage: scripts/blend_life_tables.py OUT.csv TABLE.csv WEIGHT TABLE.csv WEIGHT ...
  e.g. scripts/blend_life_tables.py aux/life_table__us_jp.csv \\
         templates/aux/life_table.csv 0.5 templates/aux/life_table__jp.csv 0.5
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
from pathlib import Path

SEXES = ("q_female", "q_male")


def latest_vintage(path: Path) -> tuple[str, dict[int, dict[str, float]]]:
    """The file's latest effective_date, and its q by age and sex column."""
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise SystemExit(f"{path}: no rows")
    latest = max(row["effective_date"] for row in rows)
    return latest, {
        int(row["age"]): {sex: float(row[sex]) for sex in SEXES}
        for row in rows if row["effective_date"] == latest
    }


def blend(tables: list[tuple[dict[int, dict[str, float]], float]]) -> dict[int, dict[str, float]]:
    """q by age and sex from (table, weight) pairs, averaging hazards."""
    total = sum(weight for _, weight in tables)
    if total <= 0 or any(weight < 0 for _, weight in tables):
        raise SystemExit("weights must be zero or more, and not all zero")
    last = max(max(table) for table, _ in tables)
    out: dict[int, dict[str, float]] = {}
    for age in range(last + 1):
        out[age] = {}
        for sex in SEXES:
            hazard = 0.0
            for table, weight in tables:
                q = table[age][sex] if age in table else 1.0
                if weight and q >= 1:
                    hazard = math.inf
                    break
                hazard += weight / total * -math.log1p(-q)
            out[age][sex] = 1.0 if math.isinf(hazard) else 1 - math.exp(-hazard)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__,
    )
    parser.add_argument("out", type=Path)
    parser.add_argument("pairs", nargs="+", metavar="TABLE WEIGHT")
    args = parser.parse_args(argv)
    if len(args.pairs) % 2:
        parser.error("give each table followed by its weight")
    dates, tables = [], []
    for path, weight in zip(args.pairs[::2], args.pairs[1::2], strict=True):
        date, table = latest_vintage(Path(path))
        if sorted(table) != list(range(len(table))):
            raise SystemExit(f"{path}: ages must run 0, 1, 2, ... with no gaps")
        dates.append(date)
        tables.append((table, float(weight)))
    blended = blend(tables)
    with args.out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["effective_date", "age", *SEXES])
        for age, q in blended.items():
            writer.writerow([max(dates), age, *(f"{q[sex]:.6f}" for sex in SEXES)])
    print(f"wrote ages 0-{max(blended)} to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
