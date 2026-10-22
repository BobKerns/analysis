"""registries.format_calendar_amount: a calendar amount in its kind's own unit."""

from __future__ import annotations

import pytest

from analysis.sim.registries import format_calendar_amount


@pytest.mark.parametrize(("kind", "amount", "expected"), [
    ("set_rate", 30_000, "3%"),          # issue #24: was shown as $300.00
    ("set_rate", 45_000, "4.5%"),
    ("set_rate", 31_250, "3.125%"),
    ("set_rate", -20_000, "-2%"),        # depreciation
    ("set_amount", 120_000, "$1,200.00"),
    ("one_off_transfer", 5_000_000, "$50,000.00"),
    ("one_off_transfer", -150, "-$1.50"),
    ("retire_scheduled", None, "-"),
    ("set_rate", float("nan"), "-"),
])
def test_format_calendar_amount(kind: str, amount: float | None, expected: str) -> None:
    assert format_calendar_amount(kind, amount) == expected


def test_format_calendar_amount_fixed_places_and_whole_dollars() -> None:
    assert format_calendar_amount("set_rate", 34_469, rate_places=2) == "3.45%"
    assert format_calendar_amount("set_rate", 30_000, rate_places=2) == "3.00%"
    assert format_calendar_amount("one_off_transfer", 2_600_049, cents=False) == "$26,000"
