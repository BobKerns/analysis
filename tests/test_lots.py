"""Lot-keyed state (ADR-005): one account_model_member row per lot."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from analysis.sim.engine import _post_transfer
from analysis.sim.loader import LoadError, LoadResult


def test_every_account_has_a_default_lot_and_leaf_chain(retire_alice_intent_file: Path) -> None:
    con = sqlite3.connect(retire_alice_intent_file)
    try:
        accounts = con.execute("SELECT COUNT(*) FROM account").fetchone()[0]
        lots = con.execute(
            "SELECT lot, COUNT(*) FROM account_model_member GROUP BY lot"
        ).fetchall()
        assert lots == [("default", accounts)]
        unlinked = con.execute(
            "SELECT COUNT(*) FROM chain c "
            "LEFT JOIN account_model_member m ON m.id = c.lot_id AND m.account_id = c.account_id "
            "WHERE c.account_id IS NOT NULL AND m.id IS NULL"
        ).fetchone()[0]
        assert unlinked == 0
    finally:
        con.close()


def test_account_state_view_sums_lots_and_nulls_partial_basis(ledger_con: sqlite3.Connection) -> None:
    ledger_con.execute(
        "INSERT INTO account_model_member(id, model_id, account_id, lot, opening_balance) "
        "VALUES (200, 1, 5, 'second', 0)"
    )
    ledger_con.execute(
        "INSERT INTO simulation_step(id, step_index, month) VALUES (1, 0, '2026-05-31')"
    )
    ledger_con.executemany(
        "INSERT INTO lot_state(step_id, lot_id, balance, cost_basis) VALUES (1, ?, ?, ?)",
        [(5, 100, 40), (200, 50, 30)],
    )
    assert ledger_con.execute(
        "SELECT account_id, balance, cost_basis FROM account_state"
    ).fetchall() == [(5, 150, 70)]

    ledger_con.execute("UPDATE lot_state SET cost_basis = NULL WHERE lot_id = 200")
    assert ledger_con.execute(
        "SELECT balance, cost_basis FROM account_state"
    ).fetchall() == [(150, None)]


def test_post_transfer_records_lot_and_owning_account(ledger_con: sqlite3.Connection) -> None:
    ledger_con.execute(
        "INSERT INTO account_model_member(id, model_id, account_id, lot, opening_balance) "
        "VALUES (200, 1, 5, 'second', 0)"
    )
    balances = {200: 1_000, 2: 0}
    _post_transfer(
        ledger_con, balances, {200: "asset", 2: "asset"}, 1, "2026-05-31",
        200, 2, 400, "none", 99,
    )
    assert balances == {200: 600, 2: 400}
    assert ledger_con.execute(
        "SELECT from_account, to_account, from_lot_id, to_lot_id FROM transfer_event"
    ).fetchone() == (5, 2, 200, 2)


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, (path.name, old)
    path.write_text(text.replace(old, new, 1))


def _append_line(path: Path, line: str) -> None:
    text = path.read_text()
    path.write_text(text + ("" if text.endswith("\n") else "\n") + line + "\n")


def test_duplicate_account_and_lot_rejected(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult]
) -> None:
    _append_line(retire_alice_csv("account_values.csv"), "Asset_Checking,,1.00,,")
    with pytest.raises(LoadError, match="duplicate account_name and lot"):
        load_test_copy()


def test_lot_label_may_not_contain_colon(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult]
) -> None:
    _edit(retire_alice_csv("account_values.csv"), "Asset_Roth_Bob,default,", "Asset_Roth_Bob,a:b,")
    with pytest.raises(LoadError, match="may not contain ':'"):
        load_test_copy()


def test_investment_account_row_requires_explicit_lot(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult]
) -> None:
    _edit(retire_alice_csv("account_values.csv"), "Asset_Roth_Bob,default,", "Asset_Roth_Bob,,")
    with pytest.raises(LoadError, match=r"Asset_Roth_Bob\): investment account requires an explicit lot"):
        load_test_copy()


def test_bare_investment_reference_names_the_fix(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult]
) -> None:
    _edit(retire_alice_csv("calendar.csv"),
          "Asset_Brokerage_Joint:default", "Asset_Brokerage_Joint")
    with pytest.raises(LoadError, match="needs an explicit lot, e.g. Asset_Brokerage_Joint:default"):
        load_test_copy()


def test_unknown_lot_reference_rejected(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult]
) -> None:
    _edit(retire_alice_csv("calendar.csv"),
          "Asset_Brokerage_Joint:default", "Asset_Brokerage_Joint:nope")
    with pytest.raises(LoadError, match="not a known account, lot or chain"):
        load_test_copy()


def _split_lot(retire_alice_csv: Callable[[str], Path], account: str, second_row: str) -> None:
    _append_line(retire_alice_csv("account_values.csv"), second_row)


def test_rmd_on_multi_lot_account_rejected(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult]
) -> None:
    _split_lot(retire_alice_csv, "Asset_TraditionalIRA_Alice",
               "Asset_TraditionalIRA_Alice,b,10000.00,,9000.00")
    with pytest.raises(LoadError, match="rmd on multi-lot account 'Asset_TraditionalIRA_Alice'"):
        load_test_copy()


def test_prior_year_end_balance_is_all_or_none_per_account(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult]
) -> None:
    _split_lot(retire_alice_csv, "Asset_Roth_Alice", "Asset_Roth_Alice,b,10000.00,,")
    _edit(retire_alice_csv("account_values.csv"), "Asset_Roth_Alice,default,100000.00,,",
          "Asset_Roth_Alice,default,100000.00,,90000.00")
    with pytest.raises(LoadError, match="prior_year_end_balance must be set on every lot"):
        load_test_copy()


def test_cash_management_only_on_single_lot_accounts(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult]
) -> None:
    _split_lot(retire_alice_csv, "Asset_Checking", "Asset_Checking,b,10.00,,")
    with pytest.raises(LoadError, match=r"Asset_Checking\): minimum_balance.*single-lot accounts"):
        load_test_copy()


def test_two_lot_account_has_a_lot_state_row_per_lot(two_lot_run_file: Path) -> None:
    con = sqlite3.connect(two_lot_run_file)
    try:
        lots = dict(con.execute(
            "SELECT m.lot, m.id FROM account_model_member m "
            "JOIN account a ON a.id = m.account_id WHERE a.name = 'Asset_Brokerage_Joint'"
        ).fetchall())
        assert set(lots) == {"a", "b"}
        steps = con.execute("SELECT COUNT(*) FROM simulation_step").fetchone()[0]
        for lot_id in lots.values():
            assert con.execute(
                "SELECT COUNT(*) FROM lot_state WHERE lot_id = ?", (lot_id,)
            ).fetchone()[0] == steps
        # The account-level view is the sum of its lots.
        sums = con.execute(
            "SELECT ls.step_id, SUM(ls.balance) FROM lot_state ls "
            "WHERE ls.lot_id IN (?, ?) GROUP BY ls.step_id ORDER BY ls.step_id",
            tuple(lots.values()),
        ).fetchall()
        viewed = con.execute(
            "SELECT ast.step_id, ast.balance FROM account_state ast "
            "JOIN account a ON a.id = ast.account_id "
            "WHERE a.name = 'Asset_Brokerage_Joint' ORDER BY ast.step_id"
        ).fetchall()
        assert sums == viewed
    finally:
        con.close()


def test_each_lot_grows_at_its_own_series(two_lot_run_file: Path) -> None:
    con = sqlite3.connect(two_lot_run_file)
    try:
        rules = dict(con.execute(
            "SELECT name, series FROM scheduled_transaction WHERE name LIKE 'growth_asset_brokerage_joint%'"
        ).fetchall())
        assert rules == {
            "growth_asset_brokerage_joint_a": "AAA",
            "growth_asset_brokerage_joint_b": "brokerage growth",
        }
    finally:
        con.close()


def test_sale_from_one_lot_leaves_the_other_lots_basis_alone(two_lot_run_file: Path) -> None:
    con = sqlite3.connect(two_lot_run_file)
    try:
        lots = dict(con.execute(
            "SELECT m.lot, m.id FROM account_model_member m "
            "JOIN account a ON a.id = m.account_id WHERE a.name = 'Asset_Brokerage_Joint'"
        ).fetchall())
        def basis(lot: str) -> list[int]:
            return [r[0] for r in con.execute(
                "SELECT cost_basis FROM lot_state WHERE lot_id = ? ORDER BY step_id",
                (lots[lot],))]
        assert set(basis("a")) == {6_000_000}
        b = basis("b")
        assert b[0] < 14_000_000 and b == sorted(b, reverse=True) and b[-1] > 0

        basis_returned = con.execute(
            "SELECT SUM(amount) FROM transfer_event "
            "WHERE from_lot_id = ? AND tax_character = 'return_of_capital'",
            (lots["b"],),
        ).fetchone()[0]
        assert 14_000_000 - b[-1] == basis_returned
        assert con.execute(
            "SELECT COUNT(*) FROM transfer_event "
            "WHERE from_lot_id = ? AND tax_character = 'return_of_capital'",
            (lots["a"],),
        ).fetchone()[0] == 0

        account_basis = con.execute(
            "SELECT cost_basis FROM account_state ast JOIN account a ON a.id = ast.account_id "
            "WHERE a.name = 'Asset_Brokerage_Joint' ORDER BY ast.step_id DESC LIMIT 1"
        ).fetchone()[0]
        assert account_basis == 6_000_000 + b[-1]
    finally:
        con.close()


def test_sale_reduces_only_that_lots_quantity(two_lot_run_file: Path) -> None:
    con = sqlite3.connect(two_lot_run_file)
    try:
        def quantities(lot: str) -> list[int]:
            return [r[0] for r in con.execute(
                "SELECT ls.quantity_micros FROM lot_state ls "
                "JOIN account_model_member m ON m.id = ls.lot_id "
                "JOIN account a ON a.id = m.account_id "
                "WHERE a.name = 'Asset_Brokerage_Joint' AND m.lot = ? "
                "ORDER BY ls.step_id", (lot,))]
        assert set(quantities("a")) == {1_000_000_000}
        b = quantities("b")
        assert b == sorted(b, reverse=True)
        assert 0 < b[-1] < b[0] <= 2_000_500_000
    finally:
        con.close()


def test_post_transfer_reduces_quantity_pro_rata_and_zeroes_on_full_drain(
    ledger_con: sqlite3.Connection,
) -> None:
    from analysis.sim.engine import ChainGraph, RefillContext

    ctx = RefillContext(
        ChainGraph({}, {}, {}, {}, {}, {}, {}, {}), {}, date(2026, 5, 31), {},
        {}, {}, {}, {1: 500_00}, {1: 1_000_000_000},
    )
    balances = {1: 1_000_00, 2: 0}
    kinds = {1: "asset", 2: "asset"}
    _post_transfer(ledger_con, balances, kinds, 1, "2026-05-31", 1, 2, 250_00, "none", 9,
                   refill_ctx=ctx)
    assert ctx.quantities[1] == 750_000_000
    _post_transfer(ledger_con, balances, kinds, 1, "2026-06-30", 1, 2, 750_00, "none", 9,
                   refill_ctx=ctx)
    assert ctx.quantities[1] == 0
