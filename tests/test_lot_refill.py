"""Minimum balance, refill, maximum and overflow are per lot (ADR-003 §4.3)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from analysis.sim.engine import run_scenario
from analysis.sim.loader import LoadError, LoadResult

CHECKING = "Asset_Checking"


@pytest.fixture(scope="function")
def reserve_lot(add_account_value: Callable[..., None],
                update_account_value: Callable[..., None]) -> None:
    """Checking gets a second lot, ``reserve``, and its default lot is refilled from it."""
    add_account_value(CHECKING, lot="reserve", opening_balance="5000000.00")
    update_account_value(CHECKING, "", refill_chain=CHECKING, refill_lot="reserve")


def test_each_lot_keeps_its_own_settings(reserve_lot: None,
                                         load_test_copy: Callable[[], LoadResult]) -> None:
    con = sqlite3.connect(load_test_copy().intent_path)
    try:
        rows = con.execute(
            "SELECT m.lot, m.minimum_balance_cents, m.refill_chain_id IS NOT NULL "
            "FROM account_model_member m JOIN account a ON a.id = m.account_id "
            "WHERE a.name = ? ORDER BY m.id", (CHECKING,),
        ).fetchall()
        refill_from = con.execute(
            "SELECT c.name FROM account_model_member m JOIN chain c ON c.id = m.refill_chain_id "
            "JOIN account a ON a.id = m.account_id WHERE a.name = ? AND m.lot = 'default'",
            (CHECKING,),
        ).fetchone()[0]
    finally:
        con.close()
    assert rows == [("default", 200_000, 1), ("reserve", 0, 0)]  # only the default lot has the $2,000 minimum
    assert refill_from == "Asset_Checking:reserve"


def test_a_lot_is_refilled_from_a_sibling_lot(reserve_lot: None,
                                              load_test_copy: Callable[[], LoadResult],
                                              runs_dir: Path) -> None:
    run = run_scenario(load_test_copy().intent_path, runs_dir / "sibling.sqlite3",
                       run_by="TEST", allow_dirty_data=True, allow_dirty_code=True)
    con = sqlite3.connect(run)
    try:
        lots = dict(con.execute(
            "SELECT m.lot, m.id FROM account_model_member m "
            "JOIN account a ON a.id = m.account_id WHERE a.name = ?", (CHECKING,)))
        refills = con.execute(
            "SELECT from_lot_id, to_lot_id, SUM(amount) FROM transfer_event "
            "WHERE reason_kind = 'refill' GROUP BY from_lot_id, to_lot_id"
        ).fetchall()
        lowest = con.execute(
            "SELECT MIN(balance) FROM balance_by_lot WHERE lot_id = ? AND month > '2026-06'",
            (lots["default"],),
        ).fetchone()[0]
    finally:
        con.close()
    assert refills and all(r[:2] == (lots["reserve"], lots["default"]) for r in refills)
    assert lowest >= 200_000  # the default lot never drops below its $2,000 minimum


def test_cash_management_columns_moved_off_accounts_csv(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    import pandas as pd
    path = retire_alice_csv("accounts.csv")
    accounts = pd.read_csv(path, dtype=str, keep_default_na=False)
    accounts["minimum_balance"] = ""
    accounts.loc[accounts["name"] == CHECKING, "minimum_balance"] = "5.00"
    accounts.to_csv(path, index=False)
    with pytest.raises(LoadError, match=r"Asset_Checking\): minimum_balance moved to account_values.csv"):
        load_test_copy()


@pytest.mark.parametrize(("columns", "message"), [
    ({"refill_target": "10.00", "refill_amount": "5.00", "refill_chain": CHECKING},
     "mutually exclusive"),
    ({"refill_amount": "5.00", "refill_chain": ""}, "requires refill_chain"),
    ({"refill_lot": "reserve", "refill_chain": ""}, "refill_lot set without refill_chain"),
    ({"refill_chain": "Asset_Roth_Alice"}, "refill_chain 'Asset_Roth_Alice' is an investment account"),
    ({"overflow_chain": CHECKING, "overflow_lot": "nope"}, "overflow_chain 'Asset_Checking' has no lot 'nope'"),
    ({"minimum_balance": "lots"}, "minimum_balance"),
])
def test_bad_settings_are_rejected(
    columns: dict[str, str], message: str,
    update_account_value: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    update_account_value(CHECKING, "", **columns)
    with pytest.raises(LoadError, match=message):
        load_test_copy()
