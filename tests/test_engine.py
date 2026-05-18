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
# (salary_alice + salary_bob) - (living + housing + healthcare).
# The mortgage payment is handled by the mortgage_payment rule below.
NET_CHECKING_PER_MONTH = 800000 + 750000 - 450000 - 80000 - 60000  # = 960000
SALARY_ALICE_MONTHLY = 800000

# Asset_Checking annual rate = 30000 micros = 3.0% APR; mortgage 6.0% APR on
# 25_000_000 cents principal, $1,850/mo P&I. Iterate the joint trajectory.
CHECKING_RATE_MICROS = 30000
MORTGAGE_RATE_MICROS = 60000
MORTGAGE_PAYMENT = 185000
MORTGAGE_OPENING = 25_000_000


def _project(steps: int) -> tuple[list[int], list[int]]:
    checking = [500_000]
    mortgage = [MORTGAGE_OPENING]
    for _ in range(steps):
        c_open = checking[-1]
        m_open = mortgage[-1]
        interest = (m_open * MORTGAGE_RATE_MICROS) // 12_000_000
        principal = min(MORTGAGE_PAYMENT - interest, m_open)
        c_interest = (c_open * CHECKING_RATE_MICROS) // 12_000_000
        checking.append(
            c_open + NET_CHECKING_PER_MONTH - MORTGAGE_PAYMENT + c_interest
        )
        mortgage.append(m_open - principal)
    return checking, mortgage


EXPECTED_CHECKING, EXPECTED_MORTGAGE = _project(11)


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
        for idx in (0, 1, 10):
            bal = con.execute(
                "SELECT balance FROM account_state WHERE step_id=? AND account_id=?",
                (steps[idx]["id"], mortgage_id),
            ).fetchone()[0]
            assert bal == EXPECTED_MORTGAGE[idx + 1]

        # Multi-leg linkage: each month, the principal leg's parent_event_id
        # points at the interest leg's id and both share reason_id.
        mortgage_rule_id = con.execute(
            "SELECT id FROM scheduled_transaction WHERE name='mortgage_pmt'"
        ).fetchone()[0]
        legs = con.execute(
            "SELECT id, to_account, amount, parent_event_id FROM transfer_event "
            "WHERE step_id=? AND reason_id=? ORDER BY id",
            (steps[0]["id"], mortgage_rule_id),
        ).fetchall()
        assert len(legs) == 2
        interest_leg, principal_leg = legs
        assert interest_leg["to_account"] == con.execute(
            "SELECT id FROM account WHERE name='Expense_Interest_Mortgage'"
        ).fetchone()[0]
        assert principal_leg["to_account"] == mortgage_id
        assert interest_leg["parent_event_id"] is None
        assert principal_leg["parent_event_id"] == interest_leg["id"]
        # Step 0: 25M * 6%/12 = 125_000 interest; 185_000 - 125_000 = 60_000 principal
        assert interest_leg["amount"] == 125_000
        assert principal_leg["amount"] == 60_000

        run = con.execute(
            "SELECT status, finished_at, failure_reason, run_args_json "
            "FROM simulation_run"
        ).fetchone()
        assert run["status"] == "succeeded"
        assert run["finished_at"] is not None
        skipped = json.loads(run["run_args_json"]).get("skipped_rule_kinds", {})
        assert "percent_of_balance" not in skipped
        assert "waterfall_fill" not in skipped
        assert "mortgage_payment" not in skipped
        assert "ss_benefit" not in skipped
        assert skipped.get("rmd") == 15

        # ADR-003 §2.5: alice (b. 1965-03-15, claim_age=67) first fires at
        # 2032-03-31 (step_idx 70). pia=300000, mult=1.0, factor=1.0 → 300000.
        ss_alice_id = con.execute(
            "SELECT id FROM scheduled_transaction WHERE name='ss_alice'"
        ).fetchone()[0]
        ss_bob_id = con.execute(
            "SELECT id FROM scheduled_transaction WHERE name='ss_bob'"
        ).fetchone()[0]
        # step 69 = 2032-02-29: alice has not yet reached 67.
        n_pre = con.execute(
            "SELECT COUNT(*) FROM transfer_event WHERE step_id=? AND reason_id=?",
            (steps[69]["id"], ss_alice_id),
        ).fetchone()[0]
        assert n_pre == 0
        alice_70 = con.execute(
            "SELECT amount FROM transfer_event WHERE step_id=? AND reason_id=?",
            (steps[70]["id"], ss_alice_id),
        ).fetchone()[0]
        assert alice_70 == 300_000
        # step 80 = 2033-01-31: one COLA bump (years_elapsed=1, rate=2.5%).
        alice_80 = con.execute(
            "SELECT amount FROM transfer_event WHERE step_id=? AND reason_id=?",
            (steps[80]["id"], ss_alice_id),
        ).fetchone()[0]
        assert alice_80 == 307_500
        # bob (b. 1967-08-20, claim_age=70) first fires at 2037-08-31 (step 135).
        # mult = 1.24, factor = 1.0 → 350000 * 1.24 = 434000.
        bob_135 = con.execute(
            "SELECT amount FROM transfer_event WHERE step_id=? AND reason_id=?",
            (steps[135]["id"], ss_bob_id),
        ).fetchone()[0]
        assert bob_135 == 434_000
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
        # 5 fixed_amount + 1 checking_interest + 1 brokerage_growth + 2 mortgage
        # legs = 9 events per month. Waterfall never fires. ss_alice fires from
        # step 70 (2032-03-31) on; ss_bob from step 135 (2037-08-31) on.
        ss_alice_events = 176 - 70  # 106
        ss_bob_events = 176 - 135   # 41
        expected_events = 9 * 176 + ss_alice_events + ss_bob_events
        assert con.execute(
            "SELECT COUNT(*) FROM transfer_event"
        ).fetchone()[0] == expected_events

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
