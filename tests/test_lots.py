"""Lot-keyed state (ADR-005): one account_model_member row per lot."""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import pytest

from analysis.sim.engine import _post_transfer
from analysis.sim.loader import LoadError, load_scenario


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


def _append_account_value(scenarios_dir: Path, **fields: str) -> None:
    path = scenarios_dir / "retire_alice" / "account_values.csv"
    with path.open(newline="") as f:
        header = next(csv.reader(f))
    with path.open("a", newline="") as f:
        f.write("\n")
        csv.writer(f).writerow([fields.get(column, "") for column in header])


def test_duplicate_account_and_lot_rejected(
    test_data_dir: Path, test_scenarios_dir: Path, intent_dir: Path
) -> None:
    _append_account_value(test_scenarios_dir, account_name="Asset_Checking", opening_balance="1.00")
    with pytest.raises(LoadError, match="duplicate account_name and lot"):
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir,
                      scenarios_dir=test_scenarios_dir, allow_dirty_data=True)


def test_lot_label_may_not_contain_colon(
    test_data_dir: Path, test_scenarios_dir: Path, intent_dir: Path
) -> None:
    path = test_scenarios_dir / "retire_alice" / "account_values.csv"
    text = path.read_text()
    header = text.splitlines()[0]
    assert "lot" not in header.split(","), "prototype data already has a lot column"
    path.write_text(text.replace(header, header + ",lot", 1).replace(
        "Asset_Checking,5000.00,,", "Asset_Checking,5000.00,,,a:b", 1))
    with pytest.raises(LoadError, match="may not contain ':'"):
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir,
                      scenarios_dir=test_scenarios_dir, allow_dirty_data=True)
