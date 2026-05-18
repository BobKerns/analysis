"""Tests for analysis.sim.engine."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from analysis.sim.engine import run_scenario
from analysis.sim.loader import load_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = REPO_ROOT / "prototype_data"

# Net cents into Asset_Checking per month from fixed_amount rules.
# (salary_alice + salary_bob) - (living + housing + healthcare)
NET_CHECKING_PER_MONTH = 800000 + 750000 - 450000 - 80000 - 60000  # = 960000
SALARY_ALICE_MONTHLY = 800000

# Asset_Checking annual rate = 30000 micros = 3.0% APR; monthly interest on
# opening balance is added each step.
EXPECTED_CHECKING = [500000]
for _ in range(4):
    _open = EXPECTED_CHECKING[-1]
    EXPECTED_CHECKING.append(_open + NET_CHECKING_PER_MONTH + (_open * 30000) // 12_000_000)


def _intent(tmp_path: Path) -> Path:
    return load_scenario(
        PROTOTYPE, "retire_alice",
        intent_dir=tmp_path / "intent", loaded_by="test",
        allow_dirty_data=True,
    ).intent_path


def test_run_scenario_emits_step_chain_and_applies_rules(tmp_path: Path) -> None:
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
        steps = con.execute(
            "SELECT id, step_index, month, previous_step_id FROM simulation_step "
            "ORDER BY step_index"
        ).fetchall()
        assert len(steps) == 176
        assert steps[0]["month"] == "2026-05-31"
        assert steps[0]["previous_step_id"] is None
        assert steps[-1]["month"] == "2040-12-31"
        for i in range(1, len(steps)):
            assert steps[i]["previous_step_id"] == steps[i - 1]["id"]

        n_accounts = con.execute("SELECT COUNT(*) FROM account").fetchone()[0]
        n_states = con.execute("SELECT COUNT(*) FROM account_state").fetchone()[0]
        assert n_states == n_accounts * len(steps)

        checking_id = con.execute(
            "SELECT id FROM account WHERE name = 'Asset_Checking'"
        ).fetchone()[0]
        bal0 = con.execute(
            "SELECT balance FROM account_state WHERE step_id = ? AND account_id = ?",
            (steps[0]["id"], checking_id),
        ).fetchone()[0]
        bal3 = con.execute(
            "SELECT balance FROM account_state WHERE step_id = ? AND account_id = ?",
            (steps[3]["id"], checking_id),
        ).fetchone()[0]
        assert bal0 == EXPECTED_CHECKING[1]
        assert bal3 == EXPECTED_CHECKING[4]

        income_id = con.execute(
            "SELECT id FROM account WHERE name = 'Income_Salary_Alice'"
        ).fetchone()[0]
        inc0 = con.execute(
            "SELECT balance FROM account_state WHERE step_id = ? AND account_id = ?",
            (steps[0]["id"], income_id),
        ).fetchone()[0]
        assert inc0 == -SALARY_ALICE_MONTHLY

        mortgage_id = con.execute(
            "SELECT id FROM account WHERE name = 'Liability_Mortgage'"
        ).fetchone()[0]
        for s in (steps[0], steps[10], steps[-1]):
            bal = con.execute(
                "SELECT balance FROM account_state WHERE step_id=? AND account_id=?",
                (s["id"], mortgage_id),
            ).fetchone()[0]
            assert bal == 25_000_000

        run = con.execute(
            "SELECT status, finished_at, failure_reason, run_args_json "
            "FROM simulation_run"
        ).fetchone()
        assert run["status"] == "succeeded"
        assert run["finished_at"] is not None
        skipped = json.loads(run["run_args_json"]).get("skipped_rule_kinds", {})
        assert "percent_of_balance" not in skipped
        assert "waterfall_fill" not in skipped
        assert skipped.get("mortgage_payment") == 176
        assert skipped.get("ss_benefit") == 2 * 176
        assert skipped.get("rmd") == 15
    finally:
        con.close()


def test_zero_sum_invariant_holds_per_step(tmp_path: Path) -> None:
    intent_path = _intent(tmp_path)
    run_path = tmp_path / "r.sqlite3"
    run_scenario(
        intent_path, run_path,
        run_by="test", data_mode="prototype", allow_dirty_data=True,
    )
    con = sqlite3.connect(run_path)
    try:
        # 5 fixed_amount + 1 checking_interest + 1 brokerage_growth = 7 monthly
        # rules. Waterfall never fires (Checking stays above 200_000-cent target).
        assert con.execute(
            "SELECT COUNT(*) FROM transfer_event"
        ).fetchone()[0] == 7 * 176

        # Liabilities store debt magnitude; signed ledger sum is the invariant.
        rows = con.execute(
            "SELECT step_id, SUM(CASE WHEN a.kind='liability' "
            "THEN -ast.balance ELSE ast.balance END) "
            "FROM account_state ast JOIN account a ON a.id = ast.account_id "
            "GROUP BY step_id"
        ).fetchall()
        totals = {step: total for step, total in rows}
        opening_total = next(iter(totals.values()))
        for t in totals.values():
            assert t == opening_total
    finally:
        con.close()
