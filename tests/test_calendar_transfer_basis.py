"""Calendar transfers out of a lot that tracks cost basis (#201): with no
``tax_character`` on the row, a ``one_off_transfer`` or ``transfer_balance``
out of a brokerage lot is a sale, split into the basis returned and the gain,
and the lot's basis falls by the basis returned. A character the row sets is
used as written, and other sources still default to ``none``."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from analysis.sim.engine import run_scenario
from analysis.sim.loader import LoadResult

SALE = {
    "date": "2027-03-31", "kind": "one_off_transfer",
    "account_1": "Asset_Brokerage_Joint", "lot_1": "default",
    "account_2": "Asset_Savings", "lot_2": "",
    "amount": "30000.00", "description": "sell some shares",
}


def _run(load_test_copy: Callable[[], LoadResult], runs_dir: Path) -> Path:
    return run_scenario(load_test_copy().intent_path, runs_dir / "sale.sqlite3", run_by="TEST",
                        allow_dirty_data=True, allow_dirty_code=True)


def _legs(run: Path, kind: str) -> list[tuple[str, int]]:
    """The (tax_character, amount) legs this test's ``kind`` calendar event
    posted, leaving out the prototype's own rows."""
    with sqlite3.connect(run) as con:
        return con.execute(
            "SELECT te.tax_character, te.amount FROM transfer_event te "
            "JOIN scheduled_transaction s ON s.id = te.reason_id AND te.reason_kind = 'rule' "
            "JOIN calendar_event ce ON ce.id = s.created_by_event_id "
            "WHERE ce.kind = ? AND ce.date = ? ORDER BY te.id",
            (kind, SALE["date"]),
        ).fetchall()


def _brokerage(run: Path, month: str) -> tuple[int, int]:
    """The brokerage lot's (balance, cost_basis) at the end of ``month``."""
    with sqlite3.connect(run) as con:
        return con.execute(
            "SELECT ls.balance, ls.cost_basis FROM lot_state ls "
            "JOIN simulation_step s ON s.id = ls.step_id "
            "JOIN account_lot m ON m.id = ls.lot_id "
            "JOIN account a ON a.id = m.account_id "
            "WHERE a.name = 'Asset_Brokerage_Joint' AND m.lot = 'default' AND s.month = ?",
            (month,),
        ).fetchone()


def _sold_before(run: Path) -> tuple[int, int]:
    """The brokerage lot's (balance, basis) just before the March sale: February's
    month-end, since calendar events apply before the month's rules."""
    return _brokerage(run, "2027-02-28")


def test_a_one_off_transfer_out_of_a_brokerage_lot_splits_off_the_gain(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    runs_dir: Path,
) -> None:
    append_calendar_row(**SALE)
    run = _run(load_test_copy, runs_dir)
    balance, basis = _sold_before(run)
    basis_out = basis * 3_000_000 // balance
    assert _legs(run, "one_off_transfer") == [
        ("return_of_capital", basis_out), ("ltcg", 3_000_000 - basis_out),
    ]
    # Nothing else moves the brokerage lot's basis in March.
    assert _brokerage(run, "2027-03-31")[1] == basis - basis_out


def test_a_transfer_balance_out_of_a_brokerage_lot_takes_all_its_basis(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    runs_dir: Path,
) -> None:
    append_calendar_row(**{**SALE, "kind": "transfer_balance", "amount": "",
                           "description": "sell everything"})
    run = _run(load_test_copy, runs_dir)
    balance, basis = _sold_before(run)
    assert _legs(run, "transfer_balance") == [
        ("return_of_capital", basis), ("ltcg", balance - basis),
    ]
    assert _brokerage(run, "2027-03-31")[1] == 0


@pytest.mark.parametrize("kind", ["one_off_transfer", "transfer_balance"])
def test_a_tax_character_on_the_row_is_used_as_written(
    kind: str, append_calendar_row: Callable[..., None],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    amount = "30000.00" if kind == "one_off_transfer" else ""
    append_calendar_row(**{**SALE, "kind": kind, "amount": amount, "tax_character": "none"})
    run = _run(load_test_copy, runs_dir)
    [(character, _amount)] = _legs(run, kind)
    assert character == "none"
    assert _brokerage(run, "2027-03-31")[1] == _sold_before(run)[1]


def test_a_blank_tax_character_out_of_cash_is_still_none(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    runs_dir: Path,
) -> None:
    append_calendar_row(**{**SALE, "account_1": "Asset_Checking", "lot_1": "",
                           "amount": "100.00", "description": "move some cash"})
    run = _run(load_test_copy, runs_dir)
    assert _legs(run, "one_off_transfer") == [("none", 10_000)]
