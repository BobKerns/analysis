"""Every life table a data repo can name, side by side (``sim lifespan``,
``notebooks/age_comparison.ipynb``).

A life table is an aux file, ``life_table.csv`` (the default) or
``life_table__<name>.csv`` (one a lifespan.csv row names, #315), found in the
code repository's ``templates/aux/``, the data repo's ``aux/``, and any
scenario's own ``aux/``. Each file's latest vintage is shown, by sex: ``q``,
the chance of dying before the next birthday, and the expected age at death
for someone alive at that age.

The expected age is the simulator's own reading of the table
(:func:`life_table.survival_curve`): a constant hazard within each year of
age, deaths spread evenly through each month, and nobody past
``registries.MAX_AGE_AT_DEATH``. A fractional age interpolates both values
linearly between the whole ages either side.

:func:`blend` mixes tables into a new one (``sim lifespan blend``).
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from analysis.sim import registries
from analysis.sim.life_table import LIFE_TABLE_KIND, LifeTable, life_table_from_rows, survival_curve
from analysis.sim.lineage import TEMPLATE_AUX_DIR, TEMPLATE_SCENARIOS_DIR

__all__ = [
    "DEFAULT_NAME", "SEXES", "TableFile", "at_age", "at_age_frame", "blend", "expected_ages",
    "find_table", "find_tables", "read_table", "comparison", "write_table",
]

# What the unnamed life_table.csv is called here; lifespan.csv leaves its
# life_table cell blank for it.
DEFAULT_NAME = "default"
SEXES = {"f": "female", "m": "male"}
# Half a month, in years: the expected wait for a death certain this month.
_WITHIN_THE_MONTH = 0.5 / 12


@dataclass(frozen=True)
class TableFile:
    """One life table file: its name (``DEFAULT_NAME`` for ``life_table.csv``),
    a label unique among those found, and where it is."""

    name: str
    label: str
    location: str
    path: Path


def _name(path: Path) -> str:
    _, _, name = path.stem.partition("__")
    return name or DEFAULT_NAME


def find_tables(data_dir: Path) -> list[TableFile]:
    """Every life table under the code repository's templates and the data
    repo ``data_dir``, in the loader's order of precedence, lowest first. A
    name found in more than one place is labelled ``<location>/<name>`` after
    its first."""
    folders = [
        (TEMPLATE_AUX_DIR, "templates/aux"),
        *((d / "aux", f"templates/scenarios/{d.name}/aux")
          for d in sorted(TEMPLATE_SCENARIOS_DIR.glob("*/"))),
        (data_dir / "aux", "aux"),
        *((d / "aux", f"scenarios/{d.name}/aux") for d in sorted((data_dir / "scenarios").glob("*/"))),
    ]
    found: list[TableFile] = []
    seen: set[str] = set()
    for folder, location in folders:
        for path in sorted(folder.glob(f"{LIFE_TABLE_KIND}.csv")) + sorted(
                folder.glob(f"{LIFE_TABLE_KIND}__*.csv")):
            name = _name(path)
            label = name if name not in seen else f"{location}/{name}"
            seen.add(name)
            found.append(TableFile(name, label, location, path))
    return found


def read_table(path: Path) -> tuple[str, dict[str, LifeTable]]:
    """A life table file's latest vintage: its effective date and each sex's
    (``f``/``m``) table."""
    frame = pd.read_csv(path, dtype={"effective_date": str})
    latest = frame["effective_date"].max()
    rows = frame[frame["effective_date"] == latest]
    return latest, {
        sex: life_table_from_rows([(int(a), float(q)) for a, q in zip(rows["age"], rows[column], strict=True)])
        for sex, column in registries.LIFE_TABLE_Q_COLUMNS.items()
    }


def expected_ages(table: LifeTable) -> list[float]:
    """The expected age at death for someone who has just turned each of
    the table's ages."""
    expected = []
    for age in range(len(table.q)):
        # Alive after each month from here, plus half the month of death.
        expected.append(age + sum(survival_curve(table, age * 12)) / 12 + _WITHIN_THE_MONTH)
    return expected


def comparison(data_dir: Path) -> pd.DataFrame:
    """Every table found, by age (the rows): columns ``(label, sex, measure)``,
    with sex ``female``/``male`` and measure ``q`` or ``expected`` (the
    expected age at death). A table shorter than another is blank past its end.
    """
    columns = {}
    for each in find_tables(data_dir):
        _, by_sex = read_table(each.path)
        for sex, table in by_sex.items():
            columns[(each.label, SEXES[sex], "q")] = pd.Series(table.q)
            columns[(each.label, SEXES[sex], "expected")] = pd.Series(expected_ages(table))
    frame = pd.DataFrame(columns).rename_axis("age")
    frame.columns = pd.MultiIndex.from_tuples(frame.columns, names=["table", "sex", "measure"])
    return frame


def at_age(table: LifeTable, expected: list[float], age: float) -> tuple[float, float]:
    """``q`` and the expected age at death at ``age``, linear between whole
    ages. Past the table, ``q`` is 1: death comes within the month."""
    if age >= len(table.q):
        return 1.0, age + _WITHIN_THE_MONTH
    whole = math.floor(age)
    frac = age - whole
    after = expected[whole + 1] if whole + 1 < len(expected) else whole + 1 + _WITHIN_THE_MONTH
    return table.at(age), (1 - frac) * expected[whole] + frac * after


def at_age_frame(data_dir: Path, age: float) -> pd.DataFrame:
    """Every table found at ``age``, one row each (indexed by table and
    location): columns ``(sex, measure)``, with measure ``q``, ``expected``
    (the expected age at death) or ``years left``."""
    rows = {}
    for each in find_tables(data_dir):
        _, by_sex = read_table(each.path)
        row = {}
        for sex, table in by_sex.items():
            q, expected = at_age(table, expected_ages(table), age)
            row |= {(SEXES[sex], "q"): q, (SEXES[sex], "expected"): expected,
                    (SEXES[sex], "years left"): expected - age}
        rows[(each.label, each.location)] = row
    frame = pd.DataFrame.from_dict(rows, orient="index")
    frame.index = pd.MultiIndex.from_tuples(frame.index, names=["table", "location"])
    frame.columns = pd.MultiIndex.from_tuples(frame.columns, names=["sex", "measure"])
    return frame


def find_table(data_dir: Path, table: str) -> Path:
    """The file a table names: a label :func:`find_tables` gives, or a path
    to a life table CSV. Raises ``ValueError`` when it is neither."""
    for each in find_tables(data_dir):
        if each.label == table:
            return each.path
    if (path := Path(table)).is_file():
        return path
    known = ", ".join(t.label for t in find_tables(data_dir))
    raise ValueError(f"no life table {table!r}: give one of {known}, or a CSV file")


def blend(tables: list[tuple[dict[str, LifeTable], float]]) -> dict[str, LifeTable]:
    """One table from ``(table by sex, weight)`` pairs, for a person between
    populations (#315). At every age and for each sex the yearly hazard,
    ``-log(1 - q)``, is the weighted average of the inputs', so equal weights
    are a person whose risk each year sits halfway between them. (Averaging
    ``q`` itself is nearly the same at most ages; averaging survival curves
    would instead describe a mixed group, part of whom follow each table.)
    Weights are scaled to add to 1, and a weight of 0 leaves its table out.
    Past an input's last age its ``q`` is 1, so the blend ends where the
    shortest weighted input does. Raises ``ValueError`` for a negative
    weight or none above 0."""
    total = sum(weight for _, weight in tables)
    if total <= 0 or any(weight < 0 for _, weight in tables):
        raise ValueError("weights must be zero or more, and not all zero")
    weighted = [(by_sex, weight / total) for by_sex, weight in tables if weight]
    blended = {}
    for sex in registries.LIFE_TABLE_Q_COLUMNS:
        last = min(len(by_sex[sex].q) for by_sex, _ in weighted)
        blended[sex] = LifeTable(tuple(
            -math.expm1(sum(share * math.log1p(-by_sex[sex].q[age]) for by_sex, share in weighted))
            if all(by_sex[sex].q[age] < 1 for by_sex, _ in weighted) else 1.0
            for age in range(last)
        ) + (1.0,) * (max(len(by_sex[sex].q) for by_sex, _ in tables) - last))
    return blended


def write_table(path: Path, effective: str, by_sex: dict[str, LifeTable]) -> None:
    """Write a life table CSV, one vintage dated ``effective``, ``q`` to six
    places, as the aux tables are."""
    ages = max(len(t.q) for t in by_sex.values())
    columns = registries.LIFE_TABLE_Q_COLUMNS
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["effective_date", "age", columns["f"], columns["m"]])
        for age in range(ages):
            writer.writerow([effective, age, *(
                f"{by_sex[sex].q[age] if age < len(by_sex[sex].q) else 1.0:.6f}"
                for sex in ("f", "m"))])
