"""Natural units in the scenario CSVs (#130).

A person writes dollars (``1234.56``), percents (``4.5``, or ``4.5%``) and
plain numbers (an RMD divisor of ``26.5``); past the loader everything is
integer cents or micros. This module is the one conversion in each
direction, shared by the loader (CSV to storage) and the CLI commands that
write rows (storage back to CSV text).
"""
from __future__ import annotations

from collections.abc import MutableMapping
from decimal import Decimal, InvalidOperation

from analysis.sim import registries

MICROS_PER_PERCENT = 10_000

# A set_rate cell beyond this is almost certainly a value still in micros
# from before #130 (``45000`` for 4.5%): no rate or rate shock this
# simulator models moves a balance by more than its whole value in a year.
MAX_RATE_PERCENT = Decimal(100)


def _decimal(text: str, what: str) -> Decimal:
    raw = text.strip().replace(",", "")
    try:
        value = Decimal(raw)
    except InvalidOperation as e:
        raise ValueError(f"invalid {what} value: {text!r}") from e
    if not value.is_finite():
        raise ValueError(f"invalid {what} value: {text!r}")
    return value


def percent_to_micros(text: str) -> int:
    """A rate as a percent (``4.5``, ``-0.25``, ``4.5%``) to millionths of 1.

    Unlike an allocation weight, a rate may be negative or exceed 100%."""
    value = _decimal(text.strip().removesuffix("%"), "percent") * MICROS_PER_PERCENT
    if value != value.to_integral_value():
        raise ValueError(f"percent value {text!r} has more than four decimal places")
    return int(value)


def micros_to_percent(micros: int) -> str:
    """Millionths of 1 as the percent a person would type: ``45000`` -> ``4.5``."""
    return format((Decimal(micros) / MICROS_PER_PERCENT).normalize(), "f")


def number_to_micros(text: str) -> int:
    """A plain decimal number (``26.5``) to millionths."""
    value = _decimal(text, "number") * 1_000_000
    if value != value.to_integral_value():
        raise ValueError(f"number {text!r} has more than six decimal places")
    return int(value)


def dollars_to_cents(text: str) -> int:
    """Decimal dollars (``1234.56``, ``$1,234.56``, ``-150``) to integer cents."""
    value = _decimal(text.strip().replace("$", "", 1), "dollar") * 100
    if value != value.to_integral_value():
        raise ValueError(f"dollar value {text!r} has sub-cent precision")
    return int(value)


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
            hint = (
                f"; as micros, {raw} would be {micros_to_percent(int(raw))}"
                if raw.lstrip("-").isdigit() else ""
            )
            problems.append(
                f"{col} {raw} is over {MAX_RATE_PERCENT}%: rates are written as a "
                f"percent (4.5 for 4.5%), so this looks like a value still in "
                f"micros{hint}"
            )
            continue
        row[col] = str(value)
    return problems
