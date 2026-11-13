"""Rolling a scenario forward to a later start (#502): what `sim scenario
roll` needs to know, apart from asking and writing.

Once a year, a plan is brought up to date: it starts again in a later month,
opening with what the statements say rather than what it assumed a year
before. The old plan's run gives the defaults (what it projected each lot to
hold by then), and a later start changes the meaning of some rows, which are
found here so the command can deal with each:

- a year-to-date row with no ``year`` belongs to the scenario's first year,
  so it would quietly move into the new one;
- a rule's ``ytd_amount`` is the old first year's catch-up, and would do the
  same;
- a calendar row dated before the new start no longer happens;
- the months between the old start and the new one leave both the run and
  the year-to-date rows, so their income would drop out of its tax year
  (#564): the old run's flows in those months stand in for them, as
  projected year-to-date rows (:func:`projected_year_to_date`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from analysis.frames import named_rows
from analysis.sim import registries
from analysis.sim.dates import month_before, month_end_iso
from analysis.sim.overlay import CalendarRow, Overlaid, overlay_rows, scenario_calendar_rows

if TYPE_CHECKING:
    from analysis.sim.connection import RunConnection

__all__ = [
    "PARENT_FIRST", "PROJECTED_PREFIX", "Projection", "ProjectedRow", "anchored_rules",
    "calendar_before", "cell_dollars", "default_start", "inherited_projected", "merge_projected",
    "preroll_rules", "projected_year_to_date",
    "projections", "unpinned_year_to_date", "ytd_amount_rules",
]

# A year-to-date row the roll made from a run's flows is named
# ``projected_<year>_<n>``, so a later roll can add to it, and drop it once
# its year no longer counts.
PROJECTED_PREFIX = "projected_"

# How ``sim scenario roll --own`` refuses a scenario whose inherited start
# hasn't moved yet: the nightly roll reads it as "not this time", not a failure.
PARENT_FIRST = "roll the scenario it inherits its start from first"


@dataclass(frozen=True)
class Projection:
    """What a run had in one lot at the end of a month: dollars, and shares
    where the lot tracks them; ``basis`` is ``None`` for a lot without one."""
    balance: float
    basis: float | None
    quantity: float | None


def keep_name(name: str, old_start: str, taken: set[str]) -> str:
    """The name to keep ``name``'s plan as, before a roll from ``old_start``
    (``YYYY-MM``): ``NAME-YEAR``, or when a scenario has that already (a
    second roll in a year), ``NAME-YEAR-MM``, then ``-2``, ``-3``... on that."""
    year, month = old_start.split("-")
    for candidate in (f"{name}-{year}", f"{name}-{year}-{month}"):
        if candidate not in taken:
            return candidate
    count = 2
    while f"{name}-{year}-{month}-{count}" in taken:
        count += 1
    return f"{name}-{year}-{month}-{count}"


def default_start(old_start: str, today: date) -> str:
    """The start to suggest, ``YYYY-MM``: the first January after
    ``old_start`` (``YYYY-MM``) that isn't before this year's, so an update
    made in November suggests the coming January, and one made in March the
    January just past."""
    year = max(int(old_start[:4]) + 1, today.year)
    return f"{year}-01"


def projections(rc: RunConnection, month: date) -> dict[tuple[str, str], Projection]:
    """Every asset and liability lot's balance, basis and shares in the run
    at the end of ``month``, by (account, lot); empty when the run doesn't
    reach that month. A liability's balance is what is owed, as authored."""
    # One month's rows, not the whole view.
    lots = rc._read_sql("SELECT account, lot, balance, cost_basis, quantity_micros "
                        "FROM balance_by_lot WHERE month = ?", (month.isoformat(),))
    found = {}
    for row in named_rows(lots):
        found[(str(row.account), str(row.lot))] = Projection(
            balance=float(row.balance) / 100,
            basis=None if pd.isna(row.cost_basis) else float(row.cost_basis) / 100,
            quantity=None if pd.isna(row.quantity_micros) else float(row.quantity_micros) / 1_000_000,
        )
    return found


def cell_dollars(text: str) -> float | None:
    """A money cell's dollars; ``None`` when it's blank, or an
    ``@itemization`` (no one number to offer)."""
    cleaned = text.strip().replace(",", "").replace("$", "")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _amount(text: str) -> bool:
    """Whether a money cell says anything other than zero (an
    ``@itemization`` says something, whatever it sums to)."""
    if not text.strip():
        return False
    value = cell_dollars(text)
    return value is None or value != 0


def unpinned_year_to_date(scenario_dir: Path) -> list[Overlaid]:
    """The scenario's year-to-date rows (own and inherited) with no
    ``year`` and an amount: they belong to the first year, whichever it is."""
    return [found for found in overlay_rows(scenario_dir, "year_to_date.csv").values()
            if not found.row.get("year", "").strip() and _amount(found.row.get("amount", ""))]


def ytd_amount_rules(scenario_dir: Path) -> list[Overlaid]:
    """The scheduled rules with a ``ytd_amount``: the first year's catch-up,
    already paid before the scenario started."""
    return [found for found in overlay_rows(scenario_dir, "scheduled.csv").values()
            if _amount(found.row.get("ytd_amount", ""))]


# Cadences counted from the rule's start: a blank start is the scenario's.
_ANCHORED_CADENCES = frozenset({"quarterly", "biweekly"})


def _preroll(row: dict[str, str]) -> bool:
    return row.get("ytd_preroll", "").strip() in ("1", "true", "True")


def anchored_rules(scenario_dir: Path) -> list[Overlaid]:
    """The scheduled rules with no ``start`` whose schedule counts from it:
    a quarterly or biweekly cadence, or ``ytd_preroll``. The loader gives
    them the scenario's start, so a later one shifts their months."""
    return [found for found in overlay_rows(scenario_dir, "scheduled.csv").values()
            if not found.row.get("start", "").strip()
            and (found.row.get("cadence", "") in _ANCHORED_CADENCES or _preroll(found.row))]


def preroll_rules(scenario_dir: Path) -> list[str]:
    """The scheduled rules with ``ytd_preroll``: their catch-up follows the
    start by itself, counting the new first year's months before it."""
    return [name for name, found in overlay_rows(scenario_dir, "scheduled.csv").items()
            if _preroll(found.row)]


def calendar_before(scenario_dir: Path, start: str) -> list[CalendarRow]:
    """The calendar rows dated before ``start`` (``YYYY-MM``), own and
    inherited, which a run from ``start`` no longer reaches. A row dated by
    a date marker is left out: where it falls depends on the run."""
    first = month_end_iso(start)
    before = []
    for entry in scenario_calendar_rows(scenario_dir):
        when = entry.row.get("date", "").strip()
        try:
            month = month_end_iso(when)
        except ValueError:
            continue
        # A latent factor or a date marker is a definition, not something
        # that happens: its date only places it.
        if month < first and entry.row.get("kind") not in registries.INERT_CALENDAR_KINDS:
            before.append(entry)
    return before


@dataclass(frozen=True)
class ProjectedRow:
    """One year's flows of the old run from one lot to another with one tax
    character, in the months a roll skips: what a year-to-date row stands
    in for (#564). ``amount`` is in cents."""
    year: int
    from_account: str
    from_lot: str
    to_account: str
    to_lot: str
    tax_character: str
    amount: int

    @property
    def key(self) -> tuple[str, str, str, str, str, str]:
        return (str(self.year), self.from_account, self.from_lot, self.to_account,
                self.to_lot, self.tax_character)


# A flow the new run has without a year-to-date row: the opening balances,
# and a tax settlement, which pays a year already over (its April payment,
# or a refund).
_NOT_PROJECTED = ("initial", "tax", "ytd", "ytd_rule")


def projected_year_to_date(rc: RunConnection, old_start: str, new_start: str) -> list[ProjectedRow]:
    """The old run's flows from ``old_start`` (``YYYY-MM``, its first month)
    up to ``new_start``'s month, summed by tax year, lots and character, for
    the years the new run still settles: ``new_start``'s and the one before
    (taxed the April after). A ``ytd_preroll`` rule's flows of the new
    start's year are left out, since the rule posts those months itself
    (ADR-003 §4.10). Nothing when the run doesn't reach that
    far: no month of it is a projection then."""
    new_year = int(new_start[:4])
    frame = rc._read_sql(
        "SELECT COALESCE(t.tax_year, CAST(substr(t.date, 1, 4) AS INTEGER)) AS year, "
        "fa.name AS from_account, fl.lot AS from_lot, ta.name AS to_account, tl.lot AS to_lot, "
        "t.tax_character, SUM(t.amount) AS amount "
        "FROM transfer_event t "
        "JOIN account fa ON fa.id = t.from_account JOIN account ta ON ta.id = t.to_account "
        "JOIN account_lot fl ON fl.id = t.from_lot_id JOIN account_lot tl ON tl.id = t.to_lot_id "
        "WHERE t.date >= ? AND t.date < ? "
        f"AND t.reason_kind NOT IN ({', '.join('?' * len(_NOT_PROJECTED))}) "
        "AND NOT (t.reason_kind = 'rule' AND substr(t.date, 1, 4) = ? AND t.reason_id IN "
        "    (SELECT id FROM scheduled_transaction WHERE ytd_preroll = 1)) "
        "GROUP BY 1, 2, 3, 4, 5, 6 HAVING SUM(t.amount) != 0 ORDER BY 1, 2, 3, 4, 5, 6",
        (f"{old_start}-01", f"{new_start}-01", *_NOT_PROJECTED, str(new_year)),
    )
    if frame.empty or not projections(rc, month_before(new_start)):
        return []
    found = []
    for row in named_rows(frame):
        year = int(row.year)
        if year < new_year - 1 or year > new_year:
            continue
        found.append(ProjectedRow(
            year=year, from_account=str(row.from_account),
            from_lot=str(row.from_lot), to_account=str(row.to_account),
            to_lot=str(row.to_lot), tax_character=str(row.tax_character),
            amount=int(row.amount),
        ))
    return found


def inherited_projected(scenario_dir: Path, new_start: str) -> list[dict[str, str]]:
    """The projected year-to-date rows the scenario only inherits, of the
    years a run from ``new_start`` still settles. A roll of a scenario built
    on another replaces them with its own run's flows (each overridden at
    zero, then :func:`merge_projected` adds its own by key), since its
    parent's flows in those months aren't its own: it may earn or spend
    differently, and counting both would count the shared ones twice."""
    new_year = int(new_start[:4])
    found = []
    for one in overlay_rows(scenario_dir, "year_to_date.csv").values():
        year = _projected_year(one.row)
        if one.index is None and year is not None and year >= new_year - 1:
            found.append({column: one.row.get(column, "")
                          for column in registries.YEAR_TO_DATE_CSV_HEADER} | {"amount": "0.00"})
    return found


def merge_projected(
    rows: list[dict[str, str]], projected: list[ProjectedRow], new_start: str,
    taken: set[str] | frozenset[str] = frozenset(),
) -> tuple[list[dict[str, str]], int]:
    """A scenario's own year_to_date.csv ``rows`` with ``projected`` added:
    onto an earlier roll's row for the same year, lots and character when
    there is one (a scenario rolled each month keeps one row each), else as
    a new ``projected_<year>_<n>`` row. The earlier rolls' rows of a year
    the new run no longer settles (before the one before ``new_start``'s)
    are dropped. A new row's name avoids ``taken`` (the names the scenario
    inherits) too. Returns the rows and how many of them were dropped."""
    new_year = int(new_start[:4])
    # Replaced by the actual figures once known (`sim ytd edit`).
    described = f"projected by sim scenario roll, through {month_before(new_start):%Y-%m}"
    kept: list[dict[str, str]] = []
    dropped = 0
    for row in rows:
        year = _projected_year(row)
        if year is not None and year < new_year - 1:
            dropped += 1
            continue
        kept.append(dict(row))
    by_key = {(row["year"].strip(), row["from_account"], row["from_lot"], row["to_account"],
               row["to_lot"], row["tax_character"]): row
              for row in kept if _projected_year(row) is not None}
    used = {row["name"] for row in kept} | set(taken)
    for one in projected:
        row = by_key.get(one.key)
        if row is not None:
            total = round(Decimal(cell_dollars(row["amount"]) or 0) * 100) + one.amount
            row["amount"] = _cents_text(total)
            row["description"] = described
            continue
        count = 1
        while f"{PROJECTED_PREFIX}{one.year}_{count}" in used:
            count += 1
        name = f"{PROJECTED_PREFIX}{one.year}_{count}"
        used.add(name)
        row = {"name": name, "from_account": one.from_account, "from_lot": one.from_lot,
               "to_account": one.to_account, "to_lot": one.to_lot,
               "amount": _cents_text(one.amount), "tax_character": one.tax_character,
               "year": str(one.year),
               "description": described}
        by_key[one.key] = row
        kept.append(row)
    return kept, dropped


def _projected_year(row: dict[str, str]) -> int | None:
    """The year of a row an earlier roll projected; None for any other."""
    name = row.get("name", "")
    if not name.startswith(PROJECTED_PREFIX):
        return None
    year = row.get("year", "").strip()
    return int(year) if year.isdigit() else None


def _cents_text(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    whole, part = divmod(abs(cents), 100)
    return f"{sign}{whole}.{part:02d}"
