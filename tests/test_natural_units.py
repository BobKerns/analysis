"""Natural units in the scenario CSVs (#130): dollars and percents in, cents
and micros stored, and pre-#130 files refused rather than misread."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest

from analysis.sim import units
from analysis.sim.cli import _calendar_row_label
from analysis.sim.loader import LoadError, LoadResult, calendar_row_key


def _issues(load: Callable[[], LoadResult]) -> list[str]:
    with pytest.raises(LoadError) as ei:
        load()
    return ei.value.issues


@pytest.mark.parametrize(("text", "micros"), [
    ("4.5%", 45_000), (" -0.25% ", -2_500), ("150%", 1_500_000), ("0.0001%", 1), ("0%", 0),
])
def test_percent_to_micros(text: str, micros: int) -> None:
    assert units.percent_to_micros(text) == micros


@pytest.mark.parametrize(("text", "fragment"), [
    ("4.5", "needs its % sign"),
    ("45000", "as micros, 45000 would be 4.5%"),
    ("4.00001%", "invalid percent"),
    (".5%", "invalid percent"),
    ("x%", "invalid percent"),
    ("nan%", "invalid percent"),
])
def test_percent_to_micros_rejects(text: str, fragment: str) -> None:
    with pytest.raises(ValueError, match=fragment):
        units.percent_to_micros(text)


def test_a_typed_percent_may_leave_off_its_sign() -> None:
    assert units.percent_to_micros("4.5", require_sign=False) == 45_000


@pytest.mark.parametrize(("micros", "text"), [
    (45_000, "4.5%"), (60_000, "6%"), (-2_500, "-0.25%"), (0, "0%"),
])
def test_micros_to_percent(micros: int, text: str) -> None:
    assert units.micros_to_percent(micros) == text


@pytest.mark.parametrize(("cents", "text"), [(123_456, "1234.56"), (-5, "-0.05"), (0, "0.00")])
def test_cents_round_trip(cents: int, text: str) -> None:
    assert units.cents_to_dollars(cents) == text
    assert units.dollars_to_cents(text) == cents


@pytest.mark.parametrize("text", [".99", "1234.5", "1,234.56", "$1234.56", "1e3", "4.5%", "12.345"])
def test_dollars_take_only_whole_dollars_or_dollars_and_cents(text: str) -> None:
    with pytest.raises(ValueError):
        units.dollars_to_cents(text)


def test_natural_units_load_as_cents_and_micros(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    append_calendar_row(date="2027-03-31", kind="set_rate", series="Asset_Checking", amount="5.25%")
    append_calendar_row(date="2029-04-30", kind="one_off_transfer", account_1="Income_Inheritance",
                        account_2="Asset_Checking", amount="1234.56", tax_character="none")
    con = sqlite3.connect(load_test_copy().intent_path)
    try:
        amounts = dict(con.execute("SELECT date, amount FROM calendar_event WHERE date IN "
                                   "('2027-03-31', '2029-04-30')").fetchall())
        (rate,) = con.execute("SELECT rate_micros FROM scheduled_transaction "
                              "WHERE name = 'mortgage_pmt'").fetchone()
        factors = [json.loads(p) for (p,) in con.execute(
            "SELECT payload_json FROM aux_table WHERE kind = 'rmd_factors'")]
    finally:
        con.close()
    assert amounts == {"2027-03-31": 52_500, "2029-04-30": 123_456}
    assert rate == 60_000
    assert {"age": 73, "factor_micros": 26_500_000} in factors


def test_a_pre_130_column_names_the_conversion(
    write_aux: Callable[[str, str], Path], retire_alice_csv: Callable[[str], Path],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    write_aux("inflation__cpi.csv", "effective_date,annual_rate_micros\n2026-01,25000\n")
    path = retire_alice_csv("scheduled.csv")
    scheduled = pd.read_csv(path, dtype=str, keep_default_na=False)
    scheduled.rename(columns={"rate": "rate_micros"}).to_csv(path, index=False)
    issues = _issues(load_test_copy)
    for where, column in (("aux/inflation__cpi.csv", "annual_rate_micros"),
                          ("scheduled.csv", "rate_micros")):
        assert any(i.startswith(f"{where}: column {column}") and "convert_units_130.py" in i
                   for i in issues), issues


def test_a_pre_130_header_with_no_rows_is_still_refused(
    write_aux: Callable[[str, str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    write_aux("rmd_factors.csv", "effective_date,age,factor_micros\n")
    assert any(i.startswith("aux/rmd_factors.csv: column factor_micros")
               for i in _issues(load_test_copy))


@pytest.mark.parametrize(("kind", "amount", "shown"), [
    ("set_amount", "1200.00", "$1,200.00"), ("set_rate", "4.5%", "4.5%"),
])
def test_a_calendar_row_label_reads_the_authored_amount(kind: str, amount: str, shown: str) -> None:
    assert shown in _calendar_row_label({"date": "2027-03-31", "kind": kind, "amount": amount})


def test_a_set_rate_over_100_percent_reads_as_unconverted_micros(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    append_calendar_row(date="2027-03-31", kind="set_rate", series="Asset_Checking", amount="45000%")
    assert any("amount 45000% is over 100%" in i for i in _issues(load_test_copy))


def test_a_bare_calendar_rate_names_its_missing_sign(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    append_calendar_row(date="2027-03-31", kind="set_rate", series="Asset_Checking", amount="45000")
    assert any("needs its % sign" in i and "as micros, 45000 would be 4.5%" in i
               for i in _issues(load_test_copy))


def test_calendar_row_key_compares_amounts_by_value() -> None:
    row = {"date": "2027-03-31", "kind": "set_rate", "series": "Asset_Checking"}
    assert calendar_row_key({**row, "amount": "4.5%"}) == calendar_row_key({**row, "amount": "4.50%"})
