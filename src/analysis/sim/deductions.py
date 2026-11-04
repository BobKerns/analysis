"""Itemized deductions, one total per tax year (#370): the
``federal_deductions``/``state_deductions`` aux tables that ``sim deduction``
lists and writes.

A scenario's own ``aux/<kind>.csv`` replaces an ancestor's or the shared one
whole (ADR-003 §4.1d), so a scenario that changes one row starts from the
rows it inherits. A row's amount is dollars, or ``@name`` for an
``itemizations/<name>.csv`` listing the expenses, which ``sim deduction``
writes beside it in the scenario's own folder."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from analysis.sim.lineage import TEMPLATE_AUX_DIR
from analysis.sim.loader import _MoneyReferenceResolver
from analysis.sim.setup_listings import SHARED, THIS_SCENARIO, _lineage_dirs
from analysis.sim.units import dollars_to_cents

KINDS = {False: "federal_deductions", True: "state_deductions"}
"""The aux kind, by whether the deduction is the state's."""

HEADER = ("effective_date", "amount")
ITEM_HEADER = ("Item", "Value")


@dataclass(frozen=True)
class Deduction:
    year: int
    amount: str
    """The cell as written: dollars or ``@name``."""
    total_cents: int | None
    """What the loader would deduct; None when it can't be worked out."""
    error: str


@dataclass(frozen=True)
class InForce:
    path: Path | None
    """The file the scenario reads, or None when it has none."""
    defined_in: str
    """:data:`THIS_SCENARIO`, the ancestor whose ``aux/`` has it,
    :data:`SHARED`, or "templates"."""


def in_force(data_dir: Path, scenario_dir: Path, kind: str) -> InForce:
    """The ``aux/<kind>.csv`` the scenario reads: its own, else its nearest
    ancestor's, else the data repo's shared one, else a template's."""
    for folder in reversed(_lineage_dirs(scenario_dir)):
        path = folder / "aux" / f"{kind}.csv"
        if path.is_file():
            return InForce(path, THIS_SCENARIO if folder == scenario_dir else folder.name)
    for folder, label in ((data_dir / "aux", SHARED), (TEMPLATE_AUX_DIR, "templates")):
        path = folder / f"{kind}.csv"
        if path.is_file():
            return InForce(path, label)
    return InForce(None, "")


def read_rows(path: Path | None) -> list[dict[str, str]]:
    if path is None or not path.is_file() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return [{column: (row.get(column) or "").strip() for column in HEADER}
                for row in csv.DictReader(fh)]


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda row: row["effective_date"])
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(HEADER), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def year_of(row: dict[str, str]) -> int | None:
    date = row.get("effective_date", "")
    return int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None


def listing(data_dir: Path, scenario_dir: Path, kind: str) -> tuple[InForce, list[Deduction]]:
    """The scenario's itemized deductions of ``kind``, by year, each with
    the total the loader would use."""
    found = in_force(data_dir, scenario_dir, kind)
    resolver = _MoneyReferenceResolver(data_dir, _lineage_dirs(scenario_dir)[::-1])
    out = []
    for row in read_rows(found.path):
        year = year_of(row)
        if year is None:
            continue
        amount = row["amount"]
        try:
            total: int | None = (resolver.resolve(amount[1:]) if amount.startswith("@")
                                 else dollars_to_cents(amount))
            error = ""
        except ValueError as exc:
            total, error = None, str(exc)
        out.append(Deduction(year, amount, total, error))
    return found, sorted(out, key=lambda d: d.year)


def itemization_name(year: int, state: bool) -> str:
    return f"{'state_' if state else ''}deductions_{year}"


def write_items(path: Path, items: list[dict[str, str]]) -> None:
    """``items`` as an itemization file: Item and Value first, then any
    other columns the rows carry (a note, say), kept as they were."""
    columns = list(ITEM_HEADER)
    for row in items:
        columns += [column for column in row if column not in columns]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n", restval="")
        writer.writeheader()
        writer.writerows(items)
