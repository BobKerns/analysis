"""Tests for analysis.sim.engine v0."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from analysis.sim.engine import run_scenario
from analysis.sim.loader import load_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = REPO_ROOT / "prototype_data"


def _intent(tmp_path: Path) -> Path:
    return load_scenario(
        PROTOTYPE, "retire_alice",
        intent_dir=tmp_path / "intent", loaded_by="test",
        allow_dirty_data=True,
    ).intent_path


def test_run_scenario_emits_step_chain_and_carries_balances(tmp_path: Path) -> None:
    intent_path = _intent(tmp_path)
    run_path = tmp_path / "runs" / "r.sqlite3"

    out = run_scenario(
        intent_path, run_path,
        run_by="test", data_mode="prototype", allow_dirty_data=True,
    )
    assert out == run_path

    con = sqlite3.connect(run_path)
    con.row_factory = sqlite3.Row
    try:
        # 2026-05-31 .. 2040-12-31 inclusive: 8 + 14*12 = 176 months
        steps = con.execute(
            "SELECT step_index, month, previous_step_id FROM simulation_step "
            "ORDER BY step_index"
        ).fetchall()
        assert len(steps) == 176
        assert steps[0]["step_index"] == 0
        assert steps[0]["month"] == "2026-05-31"
        assert steps[0]["previous_step_id"] is None
        assert steps[-1]["month"] == "2040-12-31"
        for i in range(1, len(steps)):
            assert steps[i]["previous_step_id"] is not None
            assert steps[i]["step_index"] == i

        account_count = con.execute("SELECT COUNT(*) FROM account").fetchone()[0]
        state_count = con.execute("SELECT COUNT(*) FROM account_state").fetchone()[0]
        assert state_count == account_count * len(steps)

        first_step_id = steps[0]["step_index"]
        checking_id = con.execute(
            "SELECT id FROM account WHERE name = 'Asset_Checking'"
        ).fetchone()[0]
        bal = con.execute(
            "SELECT balance FROM account_state "
            "WHERE step_id = (SELECT id FROM simulation_step WHERE step_index = ?) "
            "  AND account_id = ?",
            (first_step_id, checking_id),
        ).fetchone()[0]
        assert bal == 500000  # opening_balance from account_model_member

        income_id = con.execute(
            "SELECT id FROM account WHERE name = 'Income_Salary_Alice'"
        ).fetchone()[0]
        zero = con.execute(
            "SELECT balance FROM account_state "
            "WHERE step_id = (SELECT id FROM simulation_step WHERE step_index = 0) "
            "  AND account_id = ?",
            (income_id,),
        ).fetchone()[0]
        assert zero == 0

        run = con.execute(
            "SELECT status, finished_at, failure_reason FROM simulation_run"
        ).fetchone()
        assert run["status"] == "succeeded"
        assert run["finished_at"] is not None
        assert run["failure_reason"] is None

        nw_rows = con.execute("SELECT COUNT(*) FROM net_worth").fetchone()[0]
        assert nw_rows == len(steps)
    finally:
        con.close()


def test_run_scenario_balance_carries_forward_unchanged(tmp_path: Path) -> None:
    intent_path = _intent(tmp_path)
    run_path = tmp_path / "r.sqlite3"
    run_scenario(
        intent_path, run_path,
        run_by="test", data_mode="prototype", allow_dirty_data=True,
    )
    con = sqlite3.connect(run_path)
    try:
        distinct_per_account = con.execute(
            "SELECT account_id, COUNT(DISTINCT balance) FROM account_state "
            "GROUP BY account_id"
        ).fetchall()
        for _, n in distinct_per_account:
            assert n == 1
    finally:
        con.close()
