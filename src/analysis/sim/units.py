"""Natural units in the scenario CSVs (#130).

A person writes dollars (``1234.56``), percents (``4.5%``, sign required) and
plain numbers (an RMD divisor of ``26.5``); past the loader everything is
integer cents or micros. This module is the one conversion in each
direction, shared by the loader (CSV to storage) and the CLI commands that
write rows (storage back to CSV text).
"""
from __future__ import annotations

import re
from collections.abc import MutableMapping
from decimal import Decimal

from analysis.sim import registries

MICROS_PER_PERCENT = 10_000

# A set_rate cell beyond this is almost certainly a value still in micros
# from before #130 (``45000%`` for 4.5%): no rate or rate shock this
# simulator models moves a balance by more than its whole value in a year.
MAX_RATE_PERCENT = Decimal(100)


# The only shapes a CSV cell may take (Bob, #130): at least one digit before
# any decimal point, so ``.99`` is rejected and ``0.99`` accepted; no ``$``,
# thousands separators or exponents. A dollar amount has whole cents or none.
_DOLLARS = re.compile(r"-?\d+(?:\.\d\d)?")
_PERCENT = re.compile(r"(-?\d+(?:\.\d{1,4})?)(%?)")
_NUMBER = re.compile(r"-?\d+(?:\.\d{1,6})?")


def percent_to_micros(text: str, *, require_sign: bool = True) -> int:
    """A rate as a percent (``4.5%``, ``-0.25%``) to millionths of 1.

    In a CSV the ``%`` is required, so a bare number can't be mistaken for a
    fraction, for dollars, or for a value in micros; a CLI argument already
    named as a percent may leave it off (``require_sign=False``). Unlike an
    allocation weight, a rate may be negative or exceed 100%."""
    stripped = text.strip()
    match = _PERCENT.fullmatch(stripped)
    if match is None:
        raise ValueError(
            f"invalid percent value {text!r}: write it like 4.5% "
            "(at most four decimal places)"
        )
    number, sign = match.groups()
    if require_sign and not sign:
        hint = ""
        if number.lstrip("-").isdigit() and abs(int(number)) >= 100:
            hint = f"; as micros, {number} would be {micros_to_percent(int(number))}"
        raise ValueError(f"percent value {text!r} needs its % sign (4.5% for 4.5 percent){hint}")
    return int(Decimal(number) * MICROS_PER_PERCENT)


def micros_to_percent(micros: int) -> str:
    """Millionths of 1 as the percent a CSV holds: ``45000`` -> ``4.5%``."""
    return format((Decimal(micros) / MICROS_PER_PERCENT).normalize(), "f") + "%"


def number_to_micros(text: str) -> int:
    """A plain decimal number (``26.5``) to millionths."""
    if _NUMBER.fullmatch(text.strip()) is None:
        raise ValueError(f"invalid number {text!r}: write it like 26.5 (at most six decimal places)")
    return int(Decimal(text.strip()) * 1_000_000)


def dollars_to_cents(text: str) -> int:
    """Decimal dollars, ``1234`` or ``-1234.56`` and nothing else, to integer cents."""
    stripped = text.strip()
    if stripped.endswith("%"):
        # Where a column holds a percent on some rows and dollars on others
        # (a calendar amount and its vary_* bounds), say which this one is.
        raise ValueError(f"{text!r} is a percent, but this value is in dollars")
    if _DOLLARS.fullmatch(stripped) is None:
        raise ValueError(f"invalid dollar value {text!r}: write it like 1234 or -1234.56")
    return int(Decimal(stripped) * 100)


def cents_to_dollars(cents: int) -> str:
    """Integer cents as CSV dollars: ``123456`` -> ``1234.56``."""
    sign = "-" if cents < 0 else ""
    whole, rem = divmod(abs(cents), 100)
    return f"{sign}{whole}.{rem:02d}"


# The unit a calendar.csv row's `amount` is written in, by kind; the kinds
# not listed have no amount.
CALENDAR_AMOUNT_UNITS: dict[str, str] = {
    "set_rate": "percent",
    "set_amount": "dollars",
    "one_off_transfer": "dollars",
}

_VARY_AMOUNT_COLUMNS = ("vary_low", "vary_high", "vary_median", "vary_sd", "vary_mode")


def parse(unit: str, text: str) -> int:
    """``text`` in ``unit`` (``dollars`` or ``percent``) to its stored integer."""
    return dollars_to_cents(text) if unit == "dollars" else percent_to_micros(text)


def format_stored(unit: str, value: int) -> str:
    """A stored integer back to its CSV text in ``unit``."""
    return cents_to_dollars(value) if unit == "dollars" else micros_to_percent(value)


def calendar_row_to_storage(row: MutableMapping[str, str]) -> list[str]:
    """Rewrite a calendar.csv row's amount cells in place, from what is
    authored (dollars or percent, by kind) to the integers the loader
    stores (cents or micros), as text. Returns a problem per cell that
    doesn't parse; that cell is left as it was.

    The cells are the row's `amount`, its `vary_*` bounds when it varies its
    amount, and `vary_innovation_sd` (a yearly rate shock, so a percent)."""
    kind = (row.get("kind") or "").strip()
    unit = CALENDAR_AMOUNT_UNITS.get(kind)
    cells: list[tuple[str, str]] = []
    if unit is not None:
        cells.append(("amount", unit))
        if (row.get("vary_target") or "").strip() == "amount":
            cells.extend((col, unit) for col in _VARY_AMOUNT_COLUMNS)
    cells.append(("vary_innovation_sd", "percent"))
    problems: list[str] = []
    for col, cell_unit in cells:
        raw = (row.get(col) or "").strip()
        if not raw:
            continue
        try:
            value = parse(cell_unit, raw)
        except ValueError as e:
            problems.append(f"{col} {e}")
            continue
        if (
            cell_unit == "percent" and kind in registries.RATE_MICROS_CALENDAR_KINDS
            and abs(value) > MAX_RATE_PERCENT * MICROS_PER_PERCENT
        ):
            problems.append(
                f"{col} {raw} is over {MAX_RATE_PERCENT}%, so it looks like a value "
                "still in micros (4.5% was 45000)"
            )
            continue
        row[col] = str(value)
    return problems
