"""Security prices and opening balances derived from lot quantities (ADR-005 §4, §6)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from analysis.sim.loader import LoadError, LoadResult

PRICES = (
    "effective_date,price\n"
    "2026-01-31,10.00\n"
    "2026-04-30,12.50\n"
    "2026-06-30,99.00\n"  # after the scenario start (2026-05-31): must be ignored
)
LOT = {"lot": "default", "symbol": "AAA", "quantity": "1000", "opening_cost_basis": "200000.00"}


def _opening_balance(loaded: LoadResult) -> int:
    con = sqlite3.connect(loaded.intent_path)
    try:
        return con.execute(
            "SELECT m.opening_balance FROM account_model_member m "
            "JOIN account a ON a.id = m.account_id WHERE a.name = 'Asset_Brokerage_Joint'"
        ).fetchone()[0]
    finally:
        con.close()


def test_blank_balance_is_quantity_times_latest_price_on_or_before_start(
    write_aux: Callable[[str, str], Path],
    set_brokerage_lots: Callable[[list[dict[str, str]]], None],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    write_aux("security_price__AAA.csv", PRICES)
    set_brokerage_lots([LOT])
    assert _opening_balance(load_test_copy()) == 1_250_000  # 1000 x $12.50


def test_prices_are_stored_in_aux_table_as_micros(
    write_aux: Callable[[str, str], Path],
    set_brokerage_lots: Callable[[list[dict[str, str]]], None],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    write_aux("security_price__AAA.csv", PRICES)
    set_brokerage_lots([LOT])
    con = sqlite3.connect(load_test_copy().intent_path)
    try:
        rows = con.execute(
            "SELECT effective_date, payload_json FROM aux_table "
            "WHERE kind = 'security_price' AND series = 'AAA' ORDER BY effective_date"
        ).fetchall()
    finally:
        con.close()
    assert [(d, json.loads(p)) for d, p in rows] == [
        ("2026-01-31", {"price_micros": 10_000_000}),
        ("2026-04-30", {"price_micros": 12_500_000}),
        ("2026-06-30", {"price_micros": 99_000_000}),
    ]


@pytest.mark.parametrize(("quantity", "price", "cents"), [
    ("3", "0.333333", 100),      # 0.999999 rounds to $1.00
    ("2.5", "0.01", 3),          # 0.025 rounds half up
    ("0.000001", "1000000", 100),
    ("1,234.5", "10.1234", 1_249_734),  # 1234.5 x 10.1234 = 12497.3373
])
def test_derived_balance_rounds_to_the_nearest_cent(
    quantity: str, price: str, cents: int,
    write_aux: Callable[[str, str], Path],
    set_brokerage_lots: Callable[[list[dict[str, str]]], None],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    write_aux("security_price__AAA.csv", f"effective_date,price\n2026-01-31,{price}\n")
    set_brokerage_lots([{**LOT, "quantity": quantity}])
    assert _opening_balance(load_test_copy()) == cents


def test_a_given_balance_stays_authoritative(
    write_aux: Callable[[str, str], Path],
    set_brokerage_lots: Callable[[list[dict[str, str]]], None],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    write_aux("security_price__AAA.csv", PRICES)
    set_brokerage_lots([{**LOT, "opening_balance": "300000.00"}])
    assert _opening_balance(load_test_copy()) == 30_000_000


def test_missing_price_before_start_is_an_error(
    write_aux: Callable[[str, str], Path],
    set_brokerage_lots: Callable[[list[dict[str, str]]], None],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    write_aux("security_price__AAA.csv", "effective_date,price\n2026-06-30,99.00\n")
    set_brokerage_lots([LOT])
    # The message names the file in prices/, where prices live (ADR-003 §4.4).
    with pytest.raises(LoadError, match=r"no security_price for 'AAA' on or before 2026-05-31 "
                                        r"\(prices/security_price__AAA\.csv\)"):
        load_test_copy()


def test_quantity_without_a_symbol_is_an_error(
    set_brokerage_lots: Callable[[list[dict[str, str]]], None],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    set_brokerage_lots([{**LOT, "symbol": ""}])
    with pytest.raises(LoadError, match="quantity without opening_balance needs a symbol"):
        load_test_copy()


def test_blank_balance_without_quantity_says_how_to_derive_it(
    set_brokerage_lots: Callable[[list[dict[str, str]]], None],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    set_brokerage_lots([{**LOT, "quantity": ""}])
    with pytest.raises(LoadError, match="opening_balance is required .or give quantity, symbol"):
        load_test_copy()


@pytest.mark.parametrize("price", ["0", "-1", "1.0000001", "abc"])
def test_bad_price_is_an_error(
    price: str,
    write_aux: Callable[[str, str], Path],
    set_brokerage_lots: Callable[[list[dict[str, str]]], None],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    write_aux("security_price__AAA.csv", f"effective_date,price\n2026-01-31,{price}\n")
    set_brokerage_lots([{**LOT, "opening_balance": "1.00"}])
    with pytest.raises(LoadError, match=r"security_price__AAA.csv row 2 \(price\)"):
        load_test_copy()


def test_symbol_may_not_be_an_account_name(
    set_brokerage_lots: Callable[[list[dict[str, str]]], None],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    set_brokerage_lots([{**LOT, "symbol": "Asset_Checking", "opening_balance": "1.00"}])
    with pytest.raises(LoadError, match="symbol 'Asset_Checking' is also an account name"):
        load_test_copy()


def test_a_blank_prior_year_end_balance_is_derived_when_a_sibling_states_one(
    write_aux: Callable[[str, str], Path],
    add_account_value: Callable[..., None],
    update_account_value: Callable[..., None],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    write_aux("security_price__AAA.csv",
              "effective_date,price\n2025-12-31,8.00\n2026-04-30,12.50\n")
    update_account_value("Asset_Roth_Alice", "default", prior_year_end_balance="90000.00")
    add_account_value("Asset_Roth_Alice", lot="b", symbol="AAA", quantity="100",
                      opening_balance="1250.00")
    con = sqlite3.connect(load_test_copy().intent_path)
    try:
        rows = dict(con.execute(
            "SELECT m.lot, m.prior_year_end_balance FROM account_model_member m "
            "JOIN account a ON a.id = m.account_id WHERE a.name = 'Asset_Roth_Alice'"
        ).fetchall())
    finally:
        con.close()
    assert rows == {"default": 9_000_000, "b": 80_000}  # lot b: 100 shares x the $8.00 close


def test_no_derivation_when_no_lot_of_the_account_states_a_prior_year_end_balance(
    write_aux: Callable[[str, str], Path],
    add_account_value: Callable[..., None],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    write_aux("security_price__AAA.csv", "effective_date,price\n2025-12-31,8.00\n2026-04-30,12.50\n")
    add_account_value("Asset_Roth_Alice", lot="b", symbol="AAA", quantity="100", opening_balance="1250.00")
    con = sqlite3.connect(load_test_copy().intent_path)
    try:
        values = [r[0] for r in con.execute(
            "SELECT m.prior_year_end_balance FROM account_model_member m "
            "JOIN account a ON a.id = m.account_id WHERE a.name = 'Asset_Roth_Alice'")]
    finally:
        con.close()
    assert values == [None, None]


@pytest.mark.parametrize("bad", ["0000", "2026-04", "2026-W17-1"])
def test_price_date_must_be_a_full_day(
    bad: str,
    write_aux: Callable[[str, str], Path],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    """#34: price dates stay day-precise (not snapped to month end), so a bare
    month or any other form is reported rather than stored."""
    write_aux("security_price__AAA.csv", f"effective_date,price\n{bad},10\n")
    with pytest.raises(LoadError, match=f"effective_date '{bad}' is not YYYY-MM-DD"):
        load_test_copy()


def test_mid_month_price_date_keeps_its_day(
    write_aux: Callable[[str, str], Path],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    write_aux("security_price__AAA.csv", "effective_date,price\n2026-04-17,10\n")
    con = sqlite3.connect(load_test_copy().intent_path)
    try:
        rows = con.execute(
            "SELECT effective_date FROM aux_table WHERE kind = 'security_price'"
        ).fetchall()
    finally:
        con.close()
    assert rows == [("2026-04-17",)]
