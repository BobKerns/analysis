"""#34: month dates accept YYYY-MM or YYYY-MM-DD."""

from __future__ import annotations

from datetime import date

import pytest

from analysis.sim.dates import (
    is_month_or_date, month_end_iso, parse_day, parse_month, parse_vary_date,
)


@pytest.mark.parametrize(("raw", "expected"), [
    ("2030-02", date(2030, 2, 28)),
    ("2024-02", date(2024, 2, 29)),
    ("2030-02-10", date(2030, 2, 28)),
    ("2030-12-31", date(2030, 12, 31)),
    (" 2030-06 ", date(2030, 6, 30)),
])
def test_parse_month_lands_on_month_end(raw: str, expected: date) -> None:
    assert parse_month(raw) == expected
    assert month_end_iso(raw) == expected.isoformat()


@pytest.mark.parametrize("raw", ["", "2030", "2030-1", "2030-13", "2030-00", "2029-1-1",
                                 "20300101", "2030-02-30", "soon", "2021-W01-1", "2021-W01"])
def test_parse_month_rejects_other_forms(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_month(raw)
    assert not is_month_or_date(raw)


@pytest.mark.parametrize(("raw", "role", "expected"), [
    ("2030-02", "low", date(2030, 2, 1)),
    ("2030-02", "high", date(2030, 2, 28)),
    ("2030-02", "point", date(2030, 2, 15)),
    ("2030-02-10", "low", date(2030, 2, 10)),
    ("2030-02-10", "high", date(2030, 2, 10)),
    ("2030-02-10", "point", date(2030, 2, 10)),
])
def test_vary_date_bare_month_spans_the_month_and_full_date_is_exact(
    raw: str, role, expected: date,
) -> None:
    assert parse_vary_date(raw, role) == expected


@pytest.mark.parametrize("raw", ["2029-1-1", "2021-W01-1"])
def test_vary_date_rejects_other_forms(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_vary_date(raw, "low")


def test_parse_day_is_strict_and_keeps_the_day() -> None:
    assert parse_day("2030-02-10") == date(2030, 2, 10)
    for raw in ("2030-02", "2021-W01-1", "20300210"):
        with pytest.raises(ValueError):
            parse_day(raw)
