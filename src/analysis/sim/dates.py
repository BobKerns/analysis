"""Parsing the dates a person types into the CSVs and the CLI (#34).

The simulation steps by month and dates nearly every input by its month end,
so a full ``YYYY-MM-DD`` there is extra typing that suggests a precision the
engine doesn't have. ``parse_month`` accepts ``YYYY-MM`` or ``YYYY-MM-DD``
and always lands on the month's last day.

A `vary_target='date'` row's bounds are the exception: a draw is a real day,
so a full date stays exact, and a bare month means the whole month -- its
first day as a lower bound, its last day as an upper bound, and its middle
(the 15th) as an interior point (a mode or median). A cutoff known to fall
mid-month then doesn't over-sample that month.

Day-precise inputs (birthdates, lot ``acquired`` dates, prices) keep using
``date.fromisoformat`` directly.
"""

from __future__ import annotations

import re
from calendar import monthrange
from datetime import date
from typing import Literal

__all__ = [
    "MONTH_FORMAT_HINT", "VaryBoundRole", "parse_month", "month_end_iso",
    "parse_vary_date", "vary_date_iso", "is_month_or_date",
]

MONTH_FORMAT_HINT = "YYYY-MM or YYYY-MM-DD"
"""How to describe an accepted month date in an error message."""

VaryBoundRole = Literal["low", "high", "point"]
"""Where a bare ``YYYY-MM`` lands in a date-varying row: ``low`` on the
month's first day, ``high`` on its last, ``point`` (a mode/median) mid-month."""

_YEAR_MONTH = re.compile(r"(\d{4})-(\d{2})")
_MID_MONTH_DAY = 15


def _year_month(s: str) -> tuple[int, int] | None:
    """``(year, month)`` for a bare ``YYYY-MM``, else ``None``. Zero-padding
    is required, matching ``date.fromisoformat``'s strictness for full dates."""
    m = _YEAR_MONTH.fullmatch(s)
    if m is None:
        return None
    year, month = int(m[1]), int(m[2])
    if not 1 <= month <= 12 or year < 1:
        raise ValueError(f"invalid month: {s!r}")
    return year, month


def _month_end(year: int, month: int) -> date:
    return date(year, month, monthrange(year, month)[1])


def parse_month(s: str) -> date:
    """``YYYY-MM`` or ``YYYY-MM-DD`` as the last day of its month.

    Raises ``ValueError`` for anything else."""
    raw = s.strip()
    ym = _year_month(raw)
    if ym is None:
        d = date.fromisoformat(raw) if len(raw) == 10 else None
        if d is None:
            raise ValueError(f"not {MONTH_FORMAT_HINT}: {s!r}")
        ym = d.year, d.month
    return _month_end(*ym)


def month_end_iso(s: str) -> str:
    """``parse_month(s)`` as an ISO string -- the canonical stored form."""
    return parse_month(s).isoformat()


def parse_vary_date(s: str, role: VaryBoundRole) -> date:
    """A date-varying row's bound: a full ``YYYY-MM-DD`` exactly as given, a
    bare ``YYYY-MM`` on the day ``role`` names. Raises ``ValueError``."""
    raw = s.strip()
    ym = _year_month(raw)
    if ym is None:
        if len(raw) != 10:
            raise ValueError(f"not {MONTH_FORMAT_HINT}: {s!r}")
        return date.fromisoformat(raw)
    year, month = ym
    match role:
        case "low":
            return date(year, month, 1)
        case "high":
            return _month_end(year, month)
        case "point":
            return date(year, month, _MID_MONTH_DAY)


def vary_date_iso(s: str, role: VaryBoundRole) -> str:
    """``parse_vary_date`` as an ISO string -- the canonical stored form."""
    return parse_vary_date(s, role).isoformat()


def is_month_or_date(s: str) -> bool:
    """True if ``s`` is a ``YYYY-MM`` or ``YYYY-MM-DD`` this module accepts."""
    try:
        parse_month(s)
    except ValueError:
        return False
    return True
