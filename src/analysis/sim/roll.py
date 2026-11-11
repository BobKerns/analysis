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
- a calendar row dated before the new start no longer happens.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from analysis.frames import named_rows
from analysis.sim import registries
from analysis.sim.dates import month_end_iso
from analysis.sim.overlay import CalendarRow, Overlaid, overlay_rows, scenario_calendar_rows

if TYPE_CHECKING:
    from analysis.sim.connection import RunConnection

__all__ = [
    "Projection", "calendar_before", "default_start", "month_before", "projections",
    "preroll_rules", "unpinned_year_to_date", "ytd_amount_rules",
]


@dataclass(frozen=True)
class Projection:
    """What a run had in one lot at the end of a month: dollars, and shares
    where the lot tracks them; ``basis`` is ``None`` for a lot without one."""
    balance: float
    basis: float | None
    quantity: float | None


def default_start(old_start: str, today: date) -> str:
    """The start to suggest, ``YYYY-MM``: the first January after
    ``old_start`` (``YYYY-MM``) that isn't before this year's, so an update
    made in November suggests the coming January, and one made in March the
    January just past."""
    year = max(int(old_start[:4]) + 1, today.year)
    return f"{year}-01"


def month_before(start: str) -> pd.Timestamp:
    """The month end before ``start`` (``YYYY-MM``): the last month the old
    plan has to say about before the new one begins."""
    first = date(int(start[:4]), int(start[5:7]), 1)
    stamp = pd.Timestamp(first - timedelta(days=1))
    assert isinstance(stamp, pd.Timestamp)  # never NaT: a real date
    return stamp


def projections(rc: RunConnection, month: pd.Timestamp) -> dict[tuple[str, str], Projection]:
    """Every asset and liability lot's balance, basis and shares in the run
    at the end of ``month``, by (account, lot); empty when the run doesn't
    reach that month. A liability's balance is what is owed, as authored."""
    lots = rc.items("lot_balance")
    if lots.empty:
        return {}
    lots = lots[pd.to_datetime(lots["month"]) == month]
    found = {}
    for row in named_rows(lots):
        found[(str(row.account), str(row.lot))] = Projection(
            balance=float(row.balance) / 100,
            basis=None if pd.isna(row.cost_basis) else float(row.cost_basis) / 100,
            quantity=None if pd.isna(row.quantity_micros) else float(row.quantity_micros) / 1_000_000,
        )
    return found


def _amount(text: str) -> bool:
    """Whether a money cell says anything other than zero."""
    cleaned = text.strip().replace(",", "").replace("$", "")
    if not cleaned:
        return False
    try:
        return float(cleaned) != 0
    except ValueError:
        return True  # an @itemization: something, whatever it sums to


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


def preroll_rules(scenario_dir: Path) -> list[str]:
    """The scheduled rules with ``ytd_preroll``: their catch-up follows the
    start by itself, counting the new first year's months before it."""
    return [name for name, found in overlay_rows(scenario_dir, "scheduled.csv").items()
            if found.row.get("ytd_preroll", "").strip() in ("1", "true", "True")]


# A latent factor or a date marker is a definition, not something that
# happens: its date only places it.
_NOT_EVENTS = frozenset({registries.LATENT_FACTOR_KIND, "date_marker"})


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
        if month < first and entry.row.get("kind") not in _NOT_EVENTS:
            before.append(entry)
    return before
