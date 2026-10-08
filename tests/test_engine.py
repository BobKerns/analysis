"""Tests for analysis.sim.engine."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from analysis.sim.engine import EngineError, run_scenario
from analysis.sim.loader import growth_rule_name, load_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = REPO_ROOT / "prototype_data"

# Net cents into Asset_Checking per month from fixed_amount rules, before CPI
# growth (salary_alice + salary_bill) - (living + housing + healthcare). All
# five rows share base_date = scenario start and series = cpi
# (prototype_data/aux/inflation__cpi.csv: flat 2.5% APR), so each grows by the
# same monthly-compounded factor. The mortgage payment isn't inflated.
SALARY_ALICE_MONTHLY = 800000
SALARY_BILL_MONTHLY = 750000
LIVING_MONTHLY = 450000
HOUSING_MONTHLY = 80000
HEALTHCARE_MONTHLY = 60000
CPI_ANNUAL_MICROS = 25000

# Asset_Checking annual rate = 30000 micros = 3.0% APR; mortgage 6.0% APR on
# 25_000_000 cents principal, $1,850/mo P&I. Iterate the joint trajectory.
CHECKING_RATE_MICROS = 30000
MORTGAGE_RATE_MICROS = 60000
MORTGAGE_PAYMENT = 185000
MORTGAGE_OPENING = 25_000_000


def _cpi_factor(n: int) -> int:
    """Cumulative monthly-compounded CPI factor n months after base_date,
    mirroring engine._cumulative_inflation_factor's integer arithmetic."""
    factor = 1_000_000
    for _ in range(n):
        factor = factor * (12_000_000 + CPI_ANNUAL_MICROS) // 12_000_000
    return factor


def _project(steps: int) -> tuple[list[int], list[int]]:
    checking = [500_000]
    mortgage = [MORTGAGE_OPENING]
    for i in range(steps):
        c_open = checking[-1]
        m_open = mortgage[-1]
        interest = (m_open * MORTGAGE_RATE_MICROS) // 12_000_000
        principal = min(MORTGAGE_PAYMENT - interest, m_open)
        c_interest = (c_open * CHECKING_RATE_MICROS) // 12_000_000
        factor = _cpi_factor(i)
        net_checking = (
            SALARY_ALICE_MONTHLY * factor // 1_000_000
            + SALARY_BILL_MONTHLY * factor // 1_000_000
            - LIVING_MONTHLY * factor // 1_000_000
            - HOUSING_MONTHLY * factor // 1_000_000
            - HEALTHCARE_MONTHLY * factor // 1_000_000
        )
        checking.append(
            c_open + net_checking - MORTGAGE_PAYMENT + c_interest
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
        run_by="test", allow_dirty_data=True,
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

        # Inflation indexes are persisted monthly for each inflation series.
        n_inflation, n_series = con.execute(
            "SELECT COUNT(*), COUNT(DISTINCT series) FROM inflation_index_state"
        ).fetchone()
        n_supplied = con.execute(
            "SELECT COUNT(DISTINCT series) FROM aux_table WHERE kind = 'inflation'"
        ).fetchone()[0]
        assert n_series == n_supplied
        assert n_inflation == n_series * len(steps)
        first_cpi = con.execute(
            "SELECT cumulative_index_micros FROM inflation_index "
            "WHERE month = ? AND series = 'cpi'",
            (steps[0]["month"],),
        ).fetchone()[0]
        assert first_cpi == 1_002_083

        nw_cpi = con.execute(
            "SELECT nominal_cents, cumulative_index_micros, constant_cents "
            "FROM net_worth_constant_cpi WHERE month = ?",
            (steps[0]["month"],),
        ).fetchone()
        assert nw_cpi is not None
        assert nw_cpi[2] == (nw_cpi[0] * 1_000_000) // nw_cpi[1]

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
        assert "rmd" not in skipped
        skipped_cal = json.loads(run["run_args_json"]).get("skipped_calendar_kinds", {})
        assert skipped_cal == {}

        # ADR-003 §3: all 5 calendar events in retire_alice are supported.
        assert con.execute(
            "SELECT COUNT(*) FROM applied_calendar_event"
        ).fetchone()[0] == 5

        # ADR-003 §2.5: alice (b. 1965-03-15, claim_age=67) first fires at
        # 2032-03-31 (step_idx 70). pia=300000, mult=1.0, factor=1.0 → 300000.
        ss_alice_id = con.execute(
            "SELECT id FROM scheduled_transaction WHERE name='ss_alice'"
        ).fetchone()[0]
        ss_bill_id = con.execute(
            "SELECT id FROM scheduled_transaction WHERE name='ss_bill'"
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
        # bill (b. 1967-08-20, claim_age=70) first fires at 2037-08-31 (step 135).
        # mult = 1.24, factor = 1.0 → 350000 * 1.24 = 434000.
        bill_135 = con.execute(
            "SELECT amount FROM transfer_event WHERE step_id=? AND reason_id=?",
            (steps[135]["id"], ss_bill_id),
        ).fetchone()[0]
        assert bill_135 == 434_000

        # ADR-003 §2.3: alice reaches 73 in 2038 (b. 1965). RMD fires Dec
        # 2038 (step 151), Dec 2039 (163), Dec 2040 (175). IRA opening
        # 50_000_000, no growth or withdrawals before 2038.
        rmd_id = con.execute(
            "SELECT id FROM scheduled_transaction WHERE name='rmd_alice'"
        ).fetchone()[0]
        ira_id = con.execute(
            "SELECT id FROM account WHERE name='Asset_TraditionalIRA_Alice'"
        ).fetchone()[0]
        # No RMD before age 73 (Dec 2037 = step 139).
        assert con.execute(
            "SELECT COUNT(*) FROM transfer_event WHERE step_id=? AND reason_id=?",
            (steps[139]["id"], rmd_id),
        ).fetchone()[0] == 0
        # Dec 2038: RMD off Asset_TraditionalIRA_Alice's balance at that point
        # (which now also reflects the asset_checking_liquidity chain's earlier
        # look-ahead refill draws, ADR-003 §4.7 -- these differ slightly from a
        # month-end-only settle since a proactive refill never lets checking
        # dip below its floor between debits, so it can draw a bit more from
        # the IRA over the run than a once-a-month reconciliation would).
        rmd_2038 = con.execute(
            "SELECT amount FROM transfer_event WHERE step_id=? AND reason_id=?",
            (steps[151]["id"], rmd_id),
        ).fetchone()[0]
        assert rmd_2038 == 681_542
        ira_2038 = con.execute(
            "SELECT balance FROM account_state WHERE step_id=? AND account_id=?",
            (steps[151]["id"], ira_id),
        ).fetchone()[0]
        assert ira_2038 == 14_233_856
        rmd_2039 = con.execute(
            "SELECT amount FROM transfer_event WHERE step_id=? AND reason_id=?",
            (steps[163]["id"], rmd_id),
        ).fetchone()[0]
        assert rmd_2039 == 558_190
        rmd_2040 = con.execute(
            "SELECT amount FROM transfer_event WHERE step_id=? AND reason_id=?",
            (steps[175]["id"], rmd_id),
        ).fetchone()[0]
        assert rmd_2040 == 454_889
    finally:
        con.close()


def test_zero_sum_invariant_holds_per_step(tmp_path: Path) -> None:
    intent_path = _intent(tmp_path)
    run_path = tmp_path / "r.sqlite3"
    run_scenario(
        intent_path, run_path,
        run_by="test", allow_dirty_data=True,
    )
    con = sqlite3.connect(run_path)
    try:
        # Per-rule firing counts after calendar events expand:
        #   salary_alice retires 2028-06-30 (step 25, fires 0..25 = 26)
        #   salary_bill   retires 2030-12-31 (step 55, fires 0..55 = 56)
        #   one_off_transfer 2029-03-31 synthesizes 1 fixed_amount rule.
        # The asset_checking_liquidity settle pass (ADR-003 §4.7) posts its own
        # 'refill' legs outside reason_kind='rule', so it's checked separately.
        per_rule = dict(con.execute(
            "SELECT st.name, COUNT(*) FROM transfer_event te "
            "JOIN scheduled_transaction st ON st.id = te.reason_id "
            "WHERE te.reason_kind = 'rule' "
            "GROUP BY st.name"
        ).fetchall())
        assert per_rule["salary_alice"] == 26
        assert per_rule["salary_bill"] == 56
        assert per_rule["mortgage_pmt"] == 2 * 176  # 2 legs
        assert per_rule["living"] == 176
        assert per_rule["housing"] == 176
        assert per_rule["healthcare"] == 176
        assert per_rule[growth_rule_name("Asset_Checking", "default")] == 176
        assert per_rule[growth_rule_name("Asset_Brokerage_Joint", "default")] == 176
        assert per_rule["ss_alice"] == 176 - 70
        assert per_rule["ss_bill"] == 176 - 135
        assert per_rule["rmd_alice"] == 3
        oneoff_count = sum(
            v for k, v in per_rule.items() if k.startswith("oneoff_evt_")
        )
        assert oneoff_count == 1
        expected_rule_events = sum(per_rule.values())
        assert con.execute(
            "SELECT COUNT(*) FROM transfer_event WHERE reason_kind = 'rule'"
        ).fetchone()[0] == expected_rule_events
        # The settle pass draws Asset_Checking's refill_chain (asset_checking_
        # liquidity: Asset_Savings then Asset_TraditionalIRA_Alice) whenever
        # checking drains below its $2,000 minimum_balance.
        assert con.execute(
            "SELECT COUNT(*) FROM transfer_event WHERE reason_kind = 'refill'"
        ).fetchone()[0] > 0
        # Fixture's year_to_date.csv (3 rows) posts once, balance-neutral.
        assert con.execute(
            "SELECT COUNT(*) FROM transfer_event WHERE reason_kind = 'ytd'"
        ).fetchone()[0] == 3

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


def test_biweekly_count_in_month_basic() -> None:
    from datetime import date

    from analysis.sim.engine import _biweekly_count_in_month

    start = date(2026, 5, 15)
    end = date(2027, 5, 26)
    # May 2026: fires on 5/15, 5/29 -> 2
    assert _biweekly_count_in_month(start, date(2026, 5, 31), end) == 2
    # June 2026: 6/12, 6/26 -> 2
    assert _biweekly_count_in_month(start, date(2026, 6, 30), end) == 2
    # July 2026: 7/10, 7/24 -> 2
    assert _biweekly_count_in_month(start, date(2026, 7, 31), end) == 2
    # Oct 2026: 10/2, 10/16, 10/30 -> 3
    assert _biweekly_count_in_month(start, date(2026, 10, 31), end) == 3
    # Before start
    assert _biweekly_count_in_month(start, date(2026, 4, 30), end) == 0
    # End clipping: end = 2027-05-26; May 2027 occurrences are 5/14 and 5/28,
    # only 5/14 falls on/before the end.
    assert _biweekly_count_in_month(start, date(2027, 5, 31), end) == 1
    # June 2027 (after end): 0
    assert _biweekly_count_in_month(start, date(2027, 6, 30), end) == 0


def _append_scheduled(path: Path, **fields: str) -> None:
    """Append one scheduled.csv row, addressing columns by name."""
    import pandas as pd

    from analysis.sim.registries import SCHEDULED_CSV_HEADER

    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df = df.reindex(columns=SCHEDULED_CSV_HEADER, fill_value="")
    unknown = set(fields) - set(SCHEDULED_CSV_HEADER)
    assert not unknown, f"unknown scheduled.csv columns: {sorted(unknown)}"
    df.loc[len(df)] = [fields.get(column, "") for column in SCHEDULED_CSV_HEADER]
    df.to_csv(path, index=False)


def _shorten_scenario_end(repo: Path, end: str) -> None:
    """Pull scenario.csv's own end date in -- a test that only asserts on
    events shortly after a lifespan.csv change doesn't need the scenario
    simulated through its full declared range, and retire_alice's household
    budget isn't sized to survive losing a person's income for a decade-plus
    (unlike the unmodified scenario, which balances fine on its own)."""
    import pandas as pd

    path = repo / "scenarios" / "retire_alice" / "scenario.csv"
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df.loc[0, "end"] = end
    df.to_csv(path, index=False)


def test_post_transfer_caps_payment_to_liability_balance(ledger_con: sqlite3.Connection) -> None:
    from analysis.sim.engine import _post_transfer

    balances = {1: 1_000_00, 2: 300_00}
    account_kinds = {1: "asset", 2: "liability"}
    event_id = _post_transfer(
        ledger_con, balances, account_kinds, 1, "2026-05-31",
        1, 2, 500_00, "none", 99,
    )
    assert event_id == 1
    assert balances == {1: 700_00, 2: 0}
    assert ledger_con.execute("SELECT amount FROM transfer_event").fetchone() == (300_00,)

    event_id = _post_transfer(
        ledger_con, balances, account_kinds, 2, "2026-06-30",
        1, 2, 100_00, "none", 99,
    )
    assert event_id is None
    assert balances == {1: 700_00, 2: 0}
    assert ledger_con.execute("SELECT COUNT(*) FROM transfer_event").fetchone() == (1,)


def test_chain_draw_single_leaf_account_respects_minimum_balance(ledger_con: sqlite3.Connection) -> None:
    """_chain_draw on a bare leaf chain (a real account) draws only down to
    its own minimum_balance_cents floor, and never draws the target from
    itself."""
    from analysis.sim.engine import ChainGraph, _chain_draw

    balances = {1: 100_000, 2: 500_000}
    account_kinds = {1: "asset", 2: "asset"}
    account_tax_treatment = {1: "taxable", 2: "taxable"}
    account_subkind = {1: "cash", 2: "cash"}
    account_name = {1: "Checking", 2: "Savings"}
    graph = ChainGraph({}, {10: 2}, {2: 100_000}, {}, {}, {}, {}, {})
    taken = _chain_draw(
        ledger_con, balances, account_kinds, account_tax_treatment, account_subkind,
        account_name, step_id=1, month_s="2026-05-31", graph=graph,
        chain_id=10, amount_needed=1_000_000, target_account_id=1,
    )
    assert taken == 400_000  # 500_000 available above the 100_000 floor
    assert balances[2] == 100_000
    assert balances[1] == 500_000
    row = ledger_con.execute(
        "SELECT from_account, to_account, amount, tax_character, reason_kind "
        "FROM transfer_event"
    ).fetchone()
    assert row == (2, 1, 400_000, "none", "refill")


def test_chain_draw_never_draws_target_from_itself(ledger_con: sqlite3.Connection) -> None:
    from analysis.sim.engine import ChainGraph, _chain_draw

    balances = {1: 100_000}
    graph = ChainGraph({}, {10: 1}, {}, {}, {}, {}, {}, {})
    taken = _chain_draw(
        ledger_con, balances, {1: "asset"}, {1: "taxable"}, {1: "cash"},
        {1: "Checking"}, step_id=1, month_s="2026-05-31", graph=graph,
        chain_id=10, amount_needed=50_000, target_account_id=1,
    )
    assert taken == 0
    assert ledger_con.execute("SELECT COUNT(*) FROM transfer_event").fetchone()[0] == 0


def test_chain_draw_walks_multi_step_chain_in_order_with_per_leg_tax_character(ledger_con: sqlite3.Connection) -> None:
    """A chain with an interior step and two leaves drains them in
    step_order, and each leg's tax_character is derived from the source
    account it actually drew from (ADR-003 §4.7's core correctness goal)."""
    from analysis.sim.engine import ChainGraph, _chain_draw

    # 1=target checking, 2=taxable savings, 3=tax_deferred IRA.
    balances = {1: 0, 2: 200_000, 3: 1_000_000}
    account_kinds = {1: "asset", 2: "asset", 3: "asset"}
    account_tax_treatment = {1: "taxable", 2: "taxable", 3: "tax_deferred"}
    account_subkind = {1: "cash", 2: "cash", 3: "traditional_ira"}
    account_name = {1: "Checking", 2: "Savings", 3: "IRA"}
    # chain 100 = interior chain listing leaf chains 2 (savings) then 3 (IRA);
    # leaf chains share ids with their account for simplicity in this test.
    graph = ChainGraph(
        members={100: [(2,), (3,)]},
        leaf_lot={2: 2, 3: 3},
        minimum_of={}, refill_chain_of={},
        refill_target_of={}, refill_amount_of={},
        maximum_of={}, overflow_chain_of={},
    )
    taken = _chain_draw(
        ledger_con, balances, account_kinds, account_tax_treatment, account_subkind,
        account_name, step_id=1, month_s="2026-05-31", graph=graph,
        chain_id=100, amount_needed=500_000, target_account_id=1,
        reason_kind="rule", reason_id=99,
    )
    assert taken == 500_000
    assert balances[2] == 0        # savings drained first, fully
    assert balances[3] == 700_000  # IRA supplies the remaining 300_000
    assert balances[1] == 500_000
    legs = ledger_con.execute(
        "SELECT from_account, amount, tax_character, reason_kind, reason_id "
        "FROM transfer_event "
        "ORDER BY id"
    ).fetchall()
    assert legs == [
        (2, 200_000, "none", "rule", 99),
        (3, 300_000, "tax_deferred_out", "rule", 99),
    ]


def test_chain_draw_percent_splits_then_falls_through_when_one_source_is_exhausted(
        ledger_con: sqlite3.Connection,
    ) -> None:
    """Step 2 requests 1% of what's still outstanding, step 3 the default
    100% of whatever's left -- absent a shortfall this is a cascading
    near-even split, but step 2 (Alice Brokerage) is nearly empty here, so
    its unfulfilled share falls through and step 3 (Bill Brokerage) picks up
    the rest, exactly the parallel-draw-until-exhausted behavior ADR-003
    §4.7 describes rather than the old 'drain each step to zero in turn'
    bug."""
    from analysis.sim.engine import ChainGraph, _chain_draw

    balances = {1: 0, 2: 1_000, 3: 1_000_000}
    account_kinds = dict.fromkeys(balances, "asset")
    account_tax_treatment = dict.fromkeys(balances, "taxable")
    account_subkind = dict.fromkeys(balances, "cash")
    account_name = {1: "Target", 2: "Alice Brokerage", 3: "Bill Brokerage"}
    graph = ChainGraph(
        members={100: [(2, None, 10_000), (3,)]},  # 2: percent=1%, 3: default 100%
        leaf_lot={2: 2, 3: 3},
        minimum_of={}, refill_chain_of={},
        refill_target_of={}, refill_amount_of={},
        maximum_of={}, overflow_chain_of={},
    )
    taken = _chain_draw(
        ledger_con, balances, account_kinds, account_tax_treatment, account_subkind,
        account_name, step_id=1, month_s="2026-05-31", graph=graph,
        chain_id=100, amount_needed=500_000, target_account_id=1,
    )
    assert taken == 500_000
    assert balances[2] == 0        # step 2's 1% target (5_000) exceeded its 1_000 balance
    assert balances[3] == 501_000  # 1_000_000 - (500_000 - 1_000)


def test_chain_draw_step_maximum_caps_allocation_even_when_more_is_available(ledger_con: sqlite3.Connection) -> None:
    """The `amount`/`maximum` column is a per-step ceiling, not a target: once
    a step has supplied its cap, the remainder moves to the next step even
    though the source account could have covered more."""
    from analysis.sim.engine import ChainGraph, _chain_draw

    balances = {1: 0, 2: 1_000_000, 3: 1_000_000}
    account_kinds = dict.fromkeys(balances, "asset")
    account_tax_treatment = dict.fromkeys(balances, "taxable")
    account_subkind = dict.fromkeys(balances, "cash")
    account_name = {1: "Target", 2: "First", 3: "Second"}
    graph = ChainGraph(
        members={100: [(2, 200_000), (3,)]},  # step 2 capped at 200_000
        leaf_lot={2: 2, 3: 3},
        minimum_of={}, refill_chain_of={},
        refill_target_of={}, refill_amount_of={},
        maximum_of={}, overflow_chain_of={},
    )
    taken = _chain_draw(
        ledger_con, balances, account_kinds, account_tax_treatment, account_subkind,
        account_name, step_id=1, month_s="2026-05-31", graph=graph,
        chain_id=100, amount_needed=500_000, target_account_id=1,
    )
    assert taken == 500_000
    assert balances[2] == 800_000  # only the 200_000 cap was drawn
    assert balances[3] == 700_000  # the remaining 300_000 fell through


def test_chain_draw_refills_source_before_checking_availability(ledger_con: sqlite3.Connection) -> None:
    """A leaf's refill_chain is drawn *before* computing how much it can
    supply, so a source that's below its own minimum_balance still
    contributes up to what its refill chain can restore, rather than falling
    straight through to the next step."""
    from analysis.sim.engine import ChainGraph, RefillContext, _chain_draw

    # 1=target, 2=source (near-empty but refillable), 3=source's refill chain.
    balances = {1: 0, 2: 500, 3: 1_000_000}
    account_kinds = dict.fromkeys(balances, "asset")
    account_tax_treatment = dict.fromkeys(balances, "taxable")
    account_subkind = dict.fromkeys(balances, "cash")
    account_name = {1: "Target", 2: "Source", 3: "Refill"}
    graph = ChainGraph(
        members={},
        leaf_lot={10: 2, 20: 3},
        minimum_of={2: 0}, refill_chain_of={2: 20},
        refill_target_of={2: None}, refill_amount_of={2: None},
        maximum_of={}, overflow_chain_of={},
    )
    refill_ctx = RefillContext(
        graph, aux=None, chain_base_date=date(2026, 5, 31),
        inflation_factor_cache={}, account_tax_treatment=account_tax_treatment,
        account_subkind=account_subkind, account_name=account_name, cost_bases={},
    )
    taken = _chain_draw(
        ledger_con, balances, account_kinds, account_tax_treatment, account_subkind,
        account_name, step_id=1, month_s="2026-05-31", graph=graph,
        chain_id=10, amount_needed=50_000, target_account_id=1,
        refill_ctx=refill_ctx,
    )
    assert taken == 50_000
    assert balances[1] == 50_000
    assert balances[2] == 0
    assert balances[3] == 1_000_000 - 49_500  # refilled just enough to cover the draw
    refill_parent, draw_id = ledger_con.execute(
        "SELECT r.parent_event_id, d.id FROM transfer_event r, transfer_event d "
        "WHERE r.from_account = 3 AND d.from_account = 2"
    ).fetchone()
    assert refill_parent == draw_id  # the refill is linked to the chain leg it fed


def test_refill_legs_point_at_the_debit_that_triggered_them(ledger_con: sqlite3.Connection) -> None:
    """A refill posts before its debit, then is linked to it through
    parent_event_id (#104). A nested refill (the refill source needing its
    own refill) points at the refill leg that set it off, so the legs form a
    cause tree rooted at the debit."""
    from analysis.sim.engine import ChainGraph, RefillContext, _post_transfer

    # 1=payee, 2=checking (refills from 3), 3=savings (refills from 4), 4=brokerage.
    balances = {1: 0, 2: 10_000, 3: 1_000, 4: 1_000_000}
    account_kinds = dict.fromkeys(balances, "asset")
    account_tax_treatment = dict.fromkeys(balances, "taxable")
    account_subkind = dict.fromkeys(balances, "cash")
    account_name = {1: "Payee", 2: "Checking", 3: "Savings", 4: "Brokerage"}
    graph = ChainGraph(
        members={},
        leaf_lot={30: 3, 40: 4},
        minimum_of={2: 5_000, 3: 0}, refill_chain_of={2: 30, 3: 40},
        refill_target_of={2: None, 3: None}, refill_amount_of={2: None, 3: None},
        maximum_of={}, overflow_chain_of={},
    )
    refill_ctx = RefillContext(
        graph, aux=None, chain_base_date=date(2026, 5, 31),
        inflation_factor_cache={}, account_tax_treatment=account_tax_treatment,
        account_subkind=account_subkind, account_name=account_name, cost_bases={},
    )
    debit = _post_transfer(
        ledger_con, balances, account_kinds, step_id=1, month_s="2026-05-31",
        from_id=2, to_id=1, amount=20_000, tax_char="none", reason_id=99,
        refill_ctx=refill_ctx,
    )
    legs = {
        (src, dst): (event_id, parent)
        for event_id, src, dst, parent in ledger_con.execute(
            "SELECT id, from_account, to_account, parent_event_id FROM transfer_event"
        )
    }
    savings_leg, savings_parent = legs[(3, 2)]
    _, brokerage_parent = legs[(4, 3)]
    assert legs[(2, 1)] == (debit, None)
    assert savings_parent == debit
    assert brokerage_parent == savings_leg


@pytest.mark.allow_negative_balance
@pytest.mark.parametrize(
    ("refill_chain", "minimum", "refilling", "status", "message", "needed", "drawn"),
    [
        # #101's shape: the chain points at an account with nothing to give.
        (30, 5_000, False, "drawn", r"refill chain 'Wrong_Chain' supplied \$0\.00 of \$250\.00$",
         25_000, 0),
        (None, 0, False, "none_configured", r"no refill chain configured$", None, None),
        (30, 5_000, True, "cycle", r"refill chain 'Wrong_Chain' was skipped: .*refill cycle", None, None),
        (30, -30_000, False, "not_triggered",
         r"refill chain 'Wrong_Chain' was not drawn: .*minimum_balance$", None, None),
    ],
)
def test_negative_balance_warning_says_what_the_refill_did(
    ledger_con: sqlite3.Connection, refill_chain: int | None, minimum: int, refilling: bool,
    status: str, message: str, needed: int | None, drawn: int | None,
) -> None:
    """#105: the warning, and its negative_balance_warnings record, name the
    refill chain tried and how much of what was needed it supplied, so a
    misdirected refill_chain is visible rather than a bare shortfall."""
    from analysis.sim.engine import REFILL_STATUSES, ChainGraph, RefillContext, _post_transfer

    # 1=payee, 2=checking (refills from chain 30), 3=an empty account.
    balances = {1: 0, 2: 0, 3: 0}
    account_kinds = dict.fromkeys(balances, "asset")
    graph = ChainGraph(
        members={}, leaf_lot={30: 3},
        minimum_of={2: minimum}, refill_chain_of={2: refill_chain},
        refill_target_of={2: None}, refill_amount_of={2: None},
        maximum_of={}, overflow_chain_of={},
    )
    refill_ctx = RefillContext(
        graph, aux=None, chain_base_date=date(2026, 5, 31), inflation_factor_cache={},
        account_tax_treatment=dict.fromkeys(balances, "taxable"),
        account_subkind=dict.fromkeys(balances, "cash"),
        account_name={1: "Payee", 2: "Checking", 3: "Empty"}, cost_bases={},
        chain_name={30: "Wrong_Chain"},
    )
    if refilling:
        refill_ctx.refilling.add(2)
    with pytest.warns(RuntimeWarning, match=r"2026-05-31: account 'Checking' went negative "
                      r"\(balance=-200\.00\); " + message):
        _post_transfer(
            ledger_con, balances, account_kinds, step_id=1, month_s="2026-05-31",
            from_id=2, to_id=1, amount=20_000, tax_char="none", reason_id=99,
            refill_ctx=refill_ctx,
        )
    assert status in REFILL_STATUSES
    assert refill_ctx.negative_events == [{
        "month": "2026-05-31", "account": "Checking", "balance_cents": -20_000,
        "refill_status": status, "refill_chain": None if refill_chain is None else "Wrong_Chain",
        "refill_needed_cents": needed, "refill_drawn_cents": drawn,
    }]


@pytest.mark.allow_negative_balance
def test_chain_draw_warning_reports_the_refill_it_drew(ledger_con: sqlite3.Connection) -> None:
    """#105: a chain step that refills, then gives what it can down to a
    negative minimum_balance, warns with that refill -- not with the empty
    re-check a second look-ahead on the same debit used to record."""
    from analysis.sim.engine import ChainGraph, RefillContext, _chain_draw

    # 1=target, 2=step account (minimum -$300, refills from 3), 3=$50 refill source.
    balances = {1: 0, 2: 0, 3: 5_000}
    account_kinds = dict.fromkeys(balances, "asset")
    account_tax_treatment = dict.fromkeys(balances, "taxable")
    account_subkind = dict.fromkeys(balances, "cash")
    account_name = {1: "Target", 2: "Source", 3: "Refill"}
    graph = ChainGraph(
        members={}, leaf_lot={10: 2, 20: 3},
        minimum_of={2: -30_000}, refill_chain_of={2: 20},
        refill_target_of={2: None}, refill_amount_of={2: None},
        maximum_of={}, overflow_chain_of={},
    )
    refill_ctx = RefillContext(
        graph, aux=None, chain_base_date=date(2026, 5, 31),
        inflation_factor_cache={}, account_tax_treatment=account_tax_treatment,
        account_subkind=account_subkind, account_name=account_name, cost_bases={},
        chain_name={20: "Refill"},
    )
    with pytest.warns(RuntimeWarning, match=r"'Source' went negative \(balance=-300\.00\); "
                      r"refill chain 'Refill' supplied \$50\.00 of \$700\.00$"):
        taken = _chain_draw(
            ledger_con, balances, account_kinds, account_tax_treatment, account_subkind,
            account_name, step_id=1, month_s="2026-05-31", graph=graph,
            chain_id=10, amount_needed=100_000, target_account_id=1,
            refill_ctx=refill_ctx,
        )
    assert taken == 35_000
    assert refill_ctx.negative_events[0]["refill_status"] == "drawn"


@pytest.mark.allow_negative_balance
def test_negative_balance_warnings_record_the_refill_in_run_args(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
    tmp_path: Path,
) -> None:
    """#105: run_args_json's negative_balance_warnings carry the refill chain
    and what it supplied, for a notebook to show. A debit far beyond what
    checking's liquidity chain holds drains the chain and still goes negative."""
    append_calendar_row(
        date="2027-03-31", kind="one_off_transfer", name="huge_bill",
        account_1="Asset_Checking", account_2="Expense_Living",
        amount="1000000000", tax_character="none",
    )
    intent_path = load_scenario(
        test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True,
    ).intent_path
    run_path = tmp_path / "r.sqlite3"
    with pytest.warns(RuntimeWarning, match="'Asset_Checking' went negative"):
        run_scenario(intent_path, run_path, run_by="test", allow_dirty_data=True)
    con = sqlite3.connect(run_path)
    try:
        run_args = json.loads(con.execute("SELECT run_args_json FROM simulation_run").fetchone()[0])
    finally:
        con.close()
    first = run_args["negative_balance_warnings"][0]
    assert first["month"] == "2027-03-31"
    assert first["account"] == "Asset_Checking"
    assert first["refill_status"] == "drawn"
    assert first["refill_chain"] == "asset_checking_liquidity"
    assert 0 < first["refill_drawn_cents"] < first["refill_needed_cents"]
    assert first["balance_cents"] < 0


def test_refill_before_a_split_withdrawal_points_at_its_basis_leg(ledger_con: sqlite3.Connection) -> None:
    """A basis-tracked withdrawal refills once on its net amount; the refill
    is linked to the first leg posted, the return_of_capital leg."""
    from analysis.sim.engine import ChainGraph, RefillContext, _post_withdrawal

    # 1=payee, 2=brokerage (refills from 3), 3=cash.
    balances = {1: 0, 2: 10_000, 3: 1_000_000}
    account_kinds = dict.fromkeys(balances, "asset")
    account_tax_treatment = dict.fromkeys(balances, "taxable")
    account_subkind = {1: "cash", 2: "brokerage_taxable", 3: "cash"}
    account_name = {1: "Payee", 2: "Brokerage", 3: "Cash"}
    cost_bases: dict[int, int | None] = {2: 4_000}
    graph = ChainGraph(
        members={}, leaf_lot={30: 3},
        minimum_of={2: 5_000}, refill_chain_of={2: 30},
        refill_target_of={2: None}, refill_amount_of={2: None},
        maximum_of={}, overflow_chain_of={},
    )
    refill_ctx = RefillContext(
        graph, aux=None, chain_base_date=date(2026, 5, 31),
        inflation_factor_cache={}, account_tax_treatment=account_tax_treatment,
        account_subkind=account_subkind, account_name=account_name, cost_bases=cost_bases,
    )
    _post_withdrawal(
        ledger_con, balances, account_kinds, account_tax_treatment, account_subkind,
        account_name, cost_bases, step_id=1, month_s="2026-05-31", from_id=2, to_id=1,
        amount=8_000, reason_id=99, refill_ctx=refill_ctx,
    )
    rows = ledger_con.execute(
        "SELECT id, from_account, tax_character, parent_event_id FROM transfer_event ORDER BY id"
    ).fetchall()
    refill = [r for r in rows if r[1] == 3]
    basis_leg = [r for r in rows if r[2] == "return_of_capital"]
    assert len(refill) == 1 and len(basis_leg) == 1
    assert refill[0][3] == basis_leg[0][0]


def test_post_withdrawal_rejects_untracked_taxable_non_cash_account(ledger_con: sqlite3.Connection) -> None:
    from analysis.sim.engine import EngineError, _post_withdrawal

    balances = {3: 100_000, 4: 0}
    try:
        _post_withdrawal(
            ledger_con, balances, {3: "asset", 4: "asset"}, {3: "taxable"},
            {3: "brokerage_taxable"}, {3: "Brokerage"}, {3: None},
            step_id=1, month_s="2026-05-31", from_id=3, to_id=4, amount=1_000,
            reason_id=99,
        )
    except EngineError as e:
        assert "Brokerage" in str(e)
        assert "opening_cost_basis" in str(e)
    else:
        raise AssertionError("expected EngineError: opening_cost_basis never set")


def test_post_withdrawal_splits_return_of_capital_and_ltcg_pro_rata(ledger_con: sqlite3.Connection) -> None:
    """A taxable brokerage withdrawal with tracked cost basis splits into a
    return_of_capital leg (pro-rata basis) and an ltcg leg (the gain), and
    reduces the account's running cost basis by the basis consumed."""
    from analysis.sim.engine import _post_withdrawal

    balances = {3: 10_000_00, 4: 0}
    cost_bases: dict[int, int | None] = {3: 4_000_00}
    _post_withdrawal(
        ledger_con, balances, {3: "asset", 4: "asset"}, {3: "taxable"},
        {3: "brokerage_taxable"}, {3: "Brokerage"}, cost_bases,
        step_id=1, month_s="2026-05-31", from_id=3, to_id=4, amount=3_000_00,
        reason_id=99, requested=3_000_00, remainder=0,
    )
    legs = ledger_con.execute(
        "SELECT amount, tax_character, parent_event_id FROM transfer_event "
        "ORDER BY id"
    ).fetchall()
    # basis_out = 4_000_00 * 3_000_00 // 10_000_00 = 1_200_00; gain = 1_800_00.
    assert legs == [
        (1_200_00, "return_of_capital", None),
        (1_800_00, "ltcg", 1),
    ]
    assert balances[3] == 10_000_00 - 3_000_00
    assert balances[4] == 3_000_00
    assert cost_bases[3] == 4_000_00 - 1_200_00


def test_post_withdrawal_realizes_loss_as_negative_ltcg_leg(ledger_con: sqlite3.Connection) -> None:
    """A withdrawal from a position underwater vs. its basis realizes a loss:
    the ltcg leg comes out negative rather than erroring or clamping to zero."""
    from analysis.sim.engine import _post_withdrawal

    balances = {3: 9_000_00, 4: 0}
    cost_bases: dict[int, int | None] = {3: 20_000_00}  # bought high, now worth less than basis
    _post_withdrawal(
        ledger_con, balances, {3: "asset", 4: "asset"}, {3: "taxable"},
        {3: "brokerage_taxable"}, {3: "Brokerage"}, cost_bases,
        step_id=1, month_s="2026-05-31", from_id=3, to_id=4, amount=3_000_00,
        reason_id=99,
    )
    legs = ledger_con.execute(
        "SELECT amount, tax_character FROM transfer_event ORDER BY id"
    ).fetchall()
    # basis_out = 20_000_00 * 3_000_00 // 9_000_00 = 6_666_66; gain = -3_666_66.
    assert legs == [(6_666_66, "return_of_capital"), (-3_666_66, "ltcg")]
    assert balances[4] == 3_000_00  # to_id still nets the full amount requested


@pytest.mark.parametrize("refill_funds", [0, 50_000_00], ids=["refill-empty", "refill-funded"])
def test_chain_draw_at_a_loss_checks_the_net_amount_not_each_leg(
    ledger_con: sqlite3.Connection, refill_funds: int,
) -> None:
    """Issue #101: drawing a lot underwater vs. its basis posts a
    return_of_capital leg larger than the draw plus a negative ltcg leg.
    Refill, share tracking and the negative-balance warning see the net
    draw, so the lot neither pulls a phantom refill for the overshoot nor
    warns (an error under this suite) about a negative it never reaches."""
    from analysis.sim.engine import ChainGraph, RefillContext, _chain_draw

    # 1=target, 2=brokerage worth $10k on a $20k basis, 3=its refill source.
    balances = {1: 0, 2: 10_000_00, 3: refill_funds}
    account_kinds = dict.fromkeys(balances, "asset")
    account_tax_treatment = dict.fromkeys(balances, "taxable")
    account_subkind = {1: "cash", 2: "brokerage_taxable", 3: "cash"}
    account_name = {1: "Target", 2: "Brokerage", 3: "Refill"}
    graph = ChainGraph(
        members={},
        leaf_lot={10: 2, 20: 3},
        minimum_of={2: 0}, refill_chain_of={2: 20},
        refill_target_of={2: None}, refill_amount_of={2: None},
        maximum_of={}, overflow_chain_of={},
    )
    refill_ctx = RefillContext(
        graph, aux=None, chain_base_date=date(2026, 5, 31),
        inflation_factor_cache={}, account_tax_treatment=account_tax_treatment,
        account_subkind=account_subkind, account_name=account_name,
        cost_bases={2: 20_000_00}, quantities={2: 1_000},
    )
    taken = _chain_draw(
        ledger_con, balances, account_kinds, account_tax_treatment, account_subkind,
        account_name, step_id=1, month_s="2026-05-31", graph=graph,
        chain_id=10, amount_needed=6_000_00, target_account_id=1,
        refill_ctx=refill_ctx,
    )
    assert taken == 6_000_00
    assert balances == {1: 6_000_00, 2: 4_000_00, 3: refill_funds}  # no refill at all
    assert refill_ctx.negative_events == []
    assert refill_ctx.quantities[2] == 400  # 60% of the value, 60% of the shares
    legs = ledger_con.execute(
        "SELECT amount, tax_character FROM transfer_event ORDER BY id"
    ).fetchall()
    assert legs == [(12_000_00, "return_of_capital"), (-6_000_00, "ltcg")]


def test_loss_withdrawal_into_a_liability_is_capped_before_the_split(
    ledger_con: sqlite3.Connection,
) -> None:
    """A loss-position withdrawal paying down a liability with less owed than
    the draw: the net amount is capped to what's owed first, then split, so
    the legs, the shares and the basis all follow what the liability took."""
    from analysis.sim.engine import ChainGraph, RefillContext, _post_withdrawal

    # 1=mortgage with $1k owed, 2=brokerage worth $10k on a $20k basis.
    balances = {1: 1_000_00, 2: 10_000_00}
    account_kinds = {1: "liability", 2: "asset"}
    cost_bases: dict[int, int | None] = {2: 20_000_00}
    graph = ChainGraph(
        members={}, leaf_lot={}, minimum_of={}, refill_chain_of={},
        refill_target_of={}, refill_amount_of={},
        maximum_of={}, overflow_chain_of={},
    )
    refill_ctx = RefillContext(
        graph, aux=None, chain_base_date=date(2026, 5, 31),
        inflation_factor_cache={}, account_tax_treatment={2: "taxable"},
        account_subkind={2: "brokerage_taxable"}, account_name={1: "Mortgage", 2: "Brokerage"},
        cost_bases=cost_bases, quantities={2: 1_000},
    )
    _post_withdrawal(
        ledger_con, balances, account_kinds, {2: "taxable"},
        {2: "brokerage_taxable"}, {1: "Mortgage", 2: "Brokerage"}, cost_bases,
        step_id=1, month_s="2026-05-31", from_id=2, to_id=1, amount=6_000_00,
        reason_id=99, refill_ctx=refill_ctx,
    )
    legs = ledger_con.execute(
        "SELECT amount, tax_character FROM transfer_event ORDER BY id"
    ).fetchall()
    assert legs == [(2_000_00, "return_of_capital"), (-1_000_00, "ltcg")]
    assert balances == {1: 0, 2: 9_000_00}
    assert cost_bases[2] == 18_000_00
    assert refill_ctx.quantities[2] == 900


def test_chain_draw_into_a_liability_counts_only_what_it_owed(
    ledger_con: sqlite3.Connection,
) -> None:
    """A chain paying a liability that owes less than the ask reports, and
    records as fulfilled, only what was owed: the rest stays outstanding in
    remainder_cents instead of being counted as paid."""
    from analysis.sim.engine import ChainGraph, _chain_draw

    # 1=loan with $1k owed, 2=checking with $5k.
    balances = {1: 1_000_00, 2: 5_000_00}
    account_kinds = {1: "liability", 2: "asset"}
    graph = ChainGraph(
        members={}, leaf_lot={10: 2}, minimum_of={}, refill_chain_of={},
        refill_target_of={}, refill_amount_of={},
        maximum_of={}, overflow_chain_of={},
    )
    taken = _chain_draw(
        ledger_con, balances, account_kinds, {2: "taxable"}, {2: "cash"},
        {1: "Loan", 2: "Checking"}, step_id=1, month_s="2026-05-31", graph=graph,
        chain_id=10, amount_needed=3_000_00, target_account_id=1,
    )
    assert taken == 1_000_00
    assert balances == {1: 0, 2: 4_000_00}
    assert ledger_con.execute(
        "SELECT amount, requested_cents, remainder_cents FROM transfer_event"
    ).fetchall() == [(1_000_00, 3_000_00, 2_000_00)]


def test_chain_sweep_into_a_liability_spills_past_what_it_owed(
    ledger_con: sqlite3.Connection,
) -> None:
    """A sweep whose first destination is a liability owing less than the
    excess pays it off and passes the rest to the next destination, rather
    than counting the whole excess as swept."""
    from analysis.sim.engine import ChainGraph, _chain_sweep

    # 1=checking sweeping $3k, 2=loan with $1k owed, 3=savings.
    balances = {1: 5_000_00, 2: 1_000_00, 3: 0}
    account_kinds = {1: "asset", 2: "liability", 3: "asset"}
    graph = ChainGraph(
        members={30: [(20, None, 1_000_000, None, None), (21, None, 1_000_000, None, None)]},
        leaf_lot={20: 2, 21: 3}, minimum_of={}, refill_chain_of={},
        refill_target_of={}, refill_amount_of={},
        maximum_of={}, overflow_chain_of={},
    )
    swept = _chain_sweep(
        ledger_con, balances, account_kinds, {1: "taxable"}, {1: "cash"},
        {1: "Checking", 2: "Loan", 3: "Savings"}, step_id=1, month_s="2026-05-31",
        graph=graph, chain_id=30, amount_available=3_000_00, source_account_id=1,
    )
    assert swept == 3_000_00
    assert balances == {1: 2_000_00, 2: 0, 3: 2_000_00}


def test_apply_home_sale_splits_basis_excluded_and_taxable_gain(ledger_con: sqlite3.Connection) -> None:
    """Sale price above basis + exclusion cap produces all three legs:
    return_of_capital (the basis), excluded_gain (gain up to the §121 cap,
    untaxed), and ltcg (gain above the cap, taxed)."""
    from analysis.sim.engine import _apply_home_sale

    balances = {5: 900_000_00, 6: 0}
    cost_bases: dict[int, int | None] = {5: 200_000_00}
    _apply_home_sale(
        ledger_con, balances, {5: "asset", 6: "asset"}, cost_bases,
        step_id=1, month_s="2026-06-30",
        params={"exclusion_cap_cents": 500_000_00},
        amount_cents=900_000_00, from_id=5, to_id=6, tid=42, aux={},
    )
    legs = ledger_con.execute(
        "SELECT amount, tax_character, parent_event_id FROM transfer_event "
        "ORDER BY id"
    ).fetchall()
    # gain = 900k - 200k = 700k; excluded = min(700k, 500k) = 500k;
    # taxable = 200k.
    assert legs == [
        (200_000_00, "return_of_capital", None),
        (500_000_00, "excluded_gain", 1),
        (200_000_00, "ltcg", 2),
    ]
    assert balances[6] == 900_000_00
    assert cost_bases[5] == 0


def test_apply_home_sale_excludes_entire_gain_when_under_cap(ledger_con: sqlite3.Connection) -> None:
    """Gain fully covered by the exclusion posts no ltcg leg at all -- not
    a zero-amount leg, no leg."""
    from analysis.sim.engine import _apply_home_sale

    balances = {5: 600_000_00, 6: 0}
    cost_bases: dict[int, int | None] = {5: 200_000_00}
    _apply_home_sale(
        ledger_con, balances, {5: "asset", 6: "asset"}, cost_bases,
        step_id=1, month_s="2026-06-30",
        params={"exclusion_cap_cents": 500_000_00},
        amount_cents=600_000_00, from_id=5, to_id=6, tid=42, aux={},
    )
    legs = ledger_con.execute(
        "SELECT amount, tax_character FROM transfer_event ORDER BY id"
    ).fetchall()
    # gain = 400k, fully under the 500k cap -- no ltcg leg at all.
    assert legs == [
        (200_000_00, "return_of_capital"),
        (400_000_00, "excluded_gain"),
    ]


def test_apply_home_sale_looks_up_exclusion_from_aux_table_by_filing_status(ledger_con: sqlite3.Connection) -> None:
    """With no explicit override, the exclusion cap comes from the
    home_sale_exclusion aux table, keyed by filing_status, using the same
    latest-effective-date-on-or-before convention as every other aux
    lookup."""
    from analysis.sim.engine import _apply_home_sale

    balances = {5: 900_000_00, 6: 0}
    cost_bases: dict[int, int | None] = {5: 200_000_00}
    aux = {
        ("home_sale_exclusion", None): [
            ("1997-05-07", {"filing_status": "Single", "amount": 250_000_00}),
            ("1997-05-07", {"filing_status": "MFJ", "amount": 500_000_00}),
        ],
    }
    _apply_home_sale(
        ledger_con, balances, {5: "asset", 6: "asset"}, cost_bases,
        step_id=1, month_s="2026-06-30",
        params={"filing_status": "MFJ"},
        amount_cents=900_000_00, from_id=5, to_id=6, tid=42, aux=aux,
    )
    legs = ledger_con.execute(
        "SELECT amount, tax_character FROM transfer_event ORDER BY id"
    ).fetchall()
    assert legs == [
        (200_000_00, "return_of_capital"),
        (500_000_00, "excluded_gain"),
        (200_000_00, "ltcg"),
    ]


def test_apply_home_sale_requires_cost_basis_tracking(ledger_con: sqlite3.Connection) -> None:
    """Selling an account with no opening_cost_basis set errors the same
    way an untracked brokerage withdrawal does, rather than silently
    treating the whole sale as tax-free."""
    from analysis.sim.engine import EngineError, _apply_home_sale

    try:
        _apply_home_sale(
            ledger_con, {5: 900_000_00, 6: 0}, {5: "asset", 6: "asset"}, {5: None},
            step_id=1, month_s="2026-06-30", params={},
            amount_cents=900_000_00, from_id=5, to_id=6, tid=42, aux={},
        )
    except EngineError as e:
        assert "cost-basis tracking" in str(e)
    else:
        raise AssertionError("expected EngineError: opening_cost_basis never set")


def test_chain_sweep_allocates_fixed_then_percent_of_remainder(ledger_con: sqlite3.Connection) -> None:
    from analysis.sim.engine import ChainGraph, _chain_sweep

    balances = {1: 1_000_000, 2: 0, 3: 0, 4: 0}
    account_kinds = dict.fromkeys(balances, "asset")
    account_tax_treatment = dict.fromkeys(balances, "taxable")
    account_subkind = {1: "cash", 2: "cash", 3: "cash", 4: "hsa"}
    account_name = {1: "Income_SS", 2: "Checking", 3: "Savings", 4: "HSA"}
    graph = ChainGraph(
        members={100: [(2, 100_000, 1_000_000), (3, None, 500_000),
                       (4,)]},
        leaf_lot={2: 2, 3: 3, 4: 4},
        minimum_of={}, refill_chain_of={},
        refill_target_of={}, refill_amount_of={},
        maximum_of={2: None, 3: None, 4: None},
        overflow_chain_of={},
    )
    swept = _chain_sweep(
        ledger_con, balances, account_kinds, account_tax_treatment, account_subkind,
        account_name, step_id=1, month_s="2026-05-31", graph=graph,
        chain_id=100, amount_available=1_000_000, source_account_id=1,
    )
    assert swept == 1_000_000
    assert balances[1] == 0
    assert {account_id: balances[account_id] for account_id in (2, 3, 4)} == {
        2: 100_000, 3: 450_000, 4: 450_000,
    }
    assert [row[2] for row in ledger_con.execute(
        "SELECT from_account, to_account, amount FROM transfer_event ORDER BY id"
    )] == [100_000, 450_000, 450_000]


def test_ytd_preroll_expands_interior_chain_month_by_month(ledger_con: sqlite3.Connection) -> None:
    from analysis.sim.engine import ChainGraph, _apply_ytd_catchups

    graph = ChainGraph(
        members={100: [(2, 25_000), (3,)]},
        leaf_lot={1: 1, 2: 2, 3: 3},
        minimum_of={}, refill_chain_of={},
        refill_target_of={}, refill_amount_of={},
        maximum_of={}, overflow_chain_of={},
    )
    txn = [
        8, "social_security", 1, 100, "monthly", None, "fixed_amount",
        None, "rwk", None, None, None, 100_000, None, "ss_benefit",
        None, "2026-01-31", None, 200, None, None, 1, None,
    ]
    _apply_ytd_catchups(ledger_con, 1, date(2026, 5, 31), [txn], [], graph)
    rows = ledger_con.execute(
        "SELECT date, to_account, amount FROM transfer_event ORDER BY id"
    ).fetchall()
    assert rows == [
        ("2026-01-31", 2, 25_000), ("2026-01-31", 3, 75_000),
        ("2026-02-28", 2, 25_000), ("2026-02-28", 3, 75_000),
        ("2026-03-31", 2, 25_000), ("2026-03-31", 3, 75_000),
        ("2026-04-30", 2, 25_000), ("2026-04-30", 3, 75_000),
    ]


def test_ytd_preroll_annual_fixed_amount_fires_only_if_month_has_passed(ledger_con: sqlite3.Connection) -> None:
    from analysis.sim.engine import ChainGraph, _apply_ytd_catchups

    graph = ChainGraph(
        members={},
        leaf_lot={1: 1, 2: 2},
        minimum_of={}, refill_chain_of={},
        refill_target_of={}, refill_amount_of={},
        maximum_of={}, overflow_chain_of={},
    )
    apr_tax = [
        10, "property_tax_apr", 1, 2, "annual", 4, "fixed_amount",
        None, None, None, None, None, 580_000, None, "none",
        None, "2026-01-31", None, 200, None, None, 1, None,
    ]
    dec_tax = [
        11, "property_tax_dec", 1, 2, "annual", 12, "fixed_amount",
        None, None, None, None, None, 580_000, None, "none",
        None, "2026-01-31", None, 200, None, None, 1, None,
    ]
    _apply_ytd_catchups(ledger_con, 1, date(2026, 5, 31), [apr_tax, dec_tax], [], graph)
    rows = ledger_con.execute(
        "SELECT date, reason_id, amount FROM transfer_event ORDER BY id"
    ).fetchall()
    assert rows == [("2026-04-30", 10, 580_000)]


def test_ytd_amount_from_interior_source_chain_records_first_leaf_account(ledger_con: sqlite3.Connection) -> None:
    from analysis.sim.engine import ChainGraph, _apply_ytd_catchups

    graph = ChainGraph(
        members={100: [(2,), (3,)]},
        leaf_lot={2: 2, 3: 3, 4: 4, 5: 5},
        minimum_of={}, refill_chain_of={},
        refill_target_of={}, refill_amount_of={},
        maximum_of={}, overflow_chain_of={},
    )
    txn = [
        9, "mortgage_ytd", 100, 4, "monthly", None, "mortgage_payment",
        "Mortgage Interest", "joint", None, 60_000, None, 185_000, None, "none",
        None, "2026-01-31", None, 300, None, 370_000, 0, None,
    ]
    _apply_ytd_catchups(
        ledger_con, 1, date(2026, 5, 31), [txn], [], graph,
        account_id_by_name={"Mortgage Interest": 5}, balances={4: 25_000_000},
    )
    rows = ledger_con.execute(
        "SELECT from_account, to_account, amount, parent_event_id "
        "FROM transfer_event ORDER BY id"
    ).fetchall()
    assert rows == [
        (2, 5, 249_700, None),
        (2, 4, 120_300, 1),
    ]


def test_settle_order_settles_dependency_before_dependent() -> None:
    """An account whose refill_chain reaches another policy account's own
    account must settle after that account (ADR-003 §4.7's topological
    settle pass, independent of authoring/CSV order)."""
    from analysis.sim.engine import ChainGraph, _settle_order

    # Account 1 (checking) refills from chain 10 -> leaf account 2 (savings).
    # Account 2 (savings) itself has a policy: refills from chain 20 -> leaf
    # account 3 (IRA). So 2 must settle before 1.
    graph = ChainGraph(
        members={},
        leaf_lot={10: 2, 20: 3},
        minimum_of={1: 100, 2: 100},
        refill_chain_of={1: 10, 2: 20},
        refill_target_of={}, refill_amount_of={},
        maximum_of={1: None, 2: None},
        overflow_chain_of={1: None, 2: None},
    )
    order = _settle_order(graph)
    assert order.index(2) < order.index(1)


def test_negative_rate_depreciates_asset_into_expense(test_data_dir: Path, tmp_path: Path) -> None:
    """ADR-003 §2.2: a negative rate series swaps the legs, so value leaves the
    asset for the built-in `depreciation` expense account, declining-balance."""
    import pandas as pd

    repo = test_data_dir
    (repo / "aux" / "rate__jalopy.csv").write_text(
        "effective_date,annual_rate_micros\n2020-01-31,-120000\n", encoding="utf-8"
    )
    accounts_path = repo / "scenarios" / "retire_alice" / "accounts.csv"
    df = pd.read_csv(accounts_path, dtype=str, keep_default_na=False)
    row = dict.fromkeys(df.columns, "")
    row.update(
        name="Asset_Car", kind="asset", subkind="vehicle", owner="joint",
        tax_treatment="none", series="jalopy", tax_character="depreciation",
        meta_json="{}",
    )
    df = pd.concat([df, pd.DataFrame([row], columns=df.columns)], ignore_index=True)
    df.to_csv(accounts_path, index=False)

    values_path = repo / "scenarios" / "retire_alice" / "account_values.csv"
    vdf = pd.read_csv(values_path, dtype=str, keep_default_na=False)
    vrow = dict.fromkeys(vdf.columns, "")
    vrow.update(account_name="Asset_Car", opening_balance="30000.00")
    vdf = pd.concat([vdf, pd.DataFrame([vrow], columns=vdf.columns)], ignore_index=True)
    vdf.to_csv(values_path, index=False)

    intent_path = load_scenario(
        repo, "retire_alice",
        intent_dir=tmp_path / "intent", loaded_by="test", allow_dirty_data=True,
    ).intent_path
    run_path = tmp_path / "r.sqlite3"
    run_scenario(
        intent_path, run_path,
        run_by="test", allow_dirty_data=True,
    )

    con = sqlite3.connect(run_path)
    con.row_factory = sqlite3.Row
    try:
        legs = con.execute(
            "SELECT fa.name AS frm, ta.name AS dest, te.amount, te.tax_character "
            "FROM transfer_event te "
            "JOIN account fa ON fa.id = te.from_account "
            "JOIN account ta ON ta.id = te.to_account "
            "JOIN scheduled_transaction st ON st.id = te.reason_id "
            "WHERE st.name = ? AND te.reason_kind = 'rule' "
            "ORDER BY te.date",
            (growth_rule_name("Asset_Car", "default"),),
        ).fetchall()
        assert legs, "depreciation should post transfers"
        # Value leaves the asset for the expense account, not the reverse.
        assert legs[0]["frm"] == "Asset_Car"
        assert legs[0]["dest"] == "depreciation"
        assert legs[0]["tax_character"] == "none"
        assert legs[0]["amount"] == 30_000_00 * 120_000 // (12 * 1_000_000)
        # Declining balance: each month's charge is smaller than the last.
        assert legs[1]["amount"] < legs[0]["amount"]

        balances = [
            r[0] for r in con.execute(
                "SELECT ast.balance FROM account_state ast "
                "JOIN account a ON a.id = ast.account_id "
                "JOIN simulation_step s ON s.id = ast.step_id "
                "WHERE a.name = 'Asset_Car' ORDER BY s.step_index"
            )
        ]
        assert balances[0] < 30_000_00
        assert balances[-1] < balances[0]
        assert balances[-1] > 0, "declining balance never reaches zero"
    finally:
        con.close()


def test_tax_accumulator_computes_federal_and_writes_summary(
    test_data_dir: Path, tmp_path: Path, fund_asset_savings: Callable[..., None],
) -> None:
    """tax_accumulator computes prior-year federal tax and writes summary rows.

    It emits a payment transfer only when computed due is positive.
    """
    repo = test_data_dir
    fund_asset_savings()
    sched_csv = repo / "scenarios" / "retire_alice" / "scheduled.csv"
    _append_scheduled(
        sched_csv,
        name="tax_federal_prev_year",
        from_account="Asset_Checking",
        to_account="Expense_Taxes_Federal",
        cadence="annual",
        month_of_year="4",
        amount_rule_kind="tax_accumulator",
        tax_character="none",
        start="2027-04-30",
        priority="400",
        description="federal income tax forecast",
        meta_json="{}",
        params_json='{"year":"previous","scope":"federal"}',
    )

    intent_path = load_scenario(
        repo, "retire_alice",
        intent_dir=tmp_path / "intent", loaded_by="test",
        allow_dirty_data=True,
    ).intent_path
    run_path = tmp_path / "r.sqlite3"
    run_scenario(
        intent_path, run_path,
        run_by="test", allow_dirty_data=True,
    )

    con = sqlite3.connect(run_path)
    con.row_factory = sqlite3.Row
    try:
        tax_rule_id = con.execute(
            "SELECT id FROM scheduled_transaction WHERE name='tax_federal_prev_year'"
        ).fetchone()[0]

        liability = con.execute(
            "SELECT amount FROM tax_year_summary "
            "WHERE year = 2026 AND bucket = 'federal_tax_liability'"
        ).fetchone()
        due = con.execute(
            "SELECT amount FROM tax_year_summary "
            "WHERE year = 2026 AND bucket = 'federal_tax_due'"
        ).fetchone()
        paid = con.execute(
            "SELECT amount FROM tax_year_summary "
            "WHERE year = 2026 AND bucket = 'federal_tax_paid'"
        ).fetchone()

        assert liability is not None
        assert due is not None
        assert paid is not None
        assert liability[0] > 0
        assert due[0] >= 0
        assert paid[0] >= 0

        emitted = con.execute(
            "SELECT amount FROM transfer_event "
            "WHERE reason_id = ? AND date = '2027-04-30'",
            (tax_rule_id,),
        ).fetchall()
        if due[0] > 0:
            assert len(emitted) == 1
            assert emitted[0][0] == due[0]
        else:
            assert len(emitted) == 0
    finally:
        con.close()


def _summary(con: sqlite3.Connection, year: int, bucket: str) -> int:
    row = con.execute(
        "SELECT amount FROM tax_year_summary WHERE year = ? AND bucket = ?", (year, bucket),
    ).fetchone()
    assert row is not None, bucket
    return row[0]


def _run_federal_tax_accumulator(
    tmp_path: Path, repo: Path, fund_asset_savings: Callable[..., None],
) -> sqlite3.Connection:
    fund_asset_savings()
    sched_csv = repo / "scenarios" / "retire_alice" / "scheduled.csv"
    _append_scheduled(
        sched_csv,
        name="tax_federal_prev_year",
        from_account="Asset_Checking",
        to_account="Expense_Taxes_Federal",
        cadence="annual",
        month_of_year="4",
        amount_rule_kind="tax_accumulator",
        tax_character="none",
        start="2027-04-30",
        priority="400",
        meta_json="{}",
        params_json='{"year":"previous","scope":"federal"}',
    )
    intent_path = load_scenario(
        repo, "retire_alice",
        intent_dir=tmp_path / "intent", loaded_by="test",
        allow_dirty_data=True,
    ).intent_path
    run_path = tmp_path / "r.sqlite3"
    run_scenario(intent_path, run_path, run_by="test", allow_dirty_data=True)
    con = sqlite3.connect(run_path)
    con.row_factory = sqlite3.Row
    return con


def test_itemized_deduction_is_used_when_greater_than_standard(
    test_data_dir: Path, tmp_path: Path, fund_asset_savings: Callable[..., None],
) -> None:
    repo = test_data_dir
    # 2026 MFJ standard deduction is $31,500 (prototype_data/aux/standard_deduction.csv).
    (repo / "aux" / "federal_deductions.csv").write_text(
        "effective_date,amount\n2026-01-31,40000.00\n", encoding="utf-8",
    )
    con = _run_federal_tax_accumulator(tmp_path, repo, fund_asset_savings)
    try:
        assert _summary(con, 2026, "federal_itemized_deduction") == 40_000_00
        assert _summary(con, 2026, "federal_standard_deduction") == 31_500_00
        assert _summary(con, 2026, "federal_deduction_used") == 40_000_00
    finally:
        con.close()


def test_standard_deduction_is_used_when_greater_than_itemized(
    test_data_dir: Path, tmp_path: Path, fund_asset_savings: Callable[..., None],
) -> None:
    repo = test_data_dir
    (repo / "aux" / "federal_deductions.csv").write_text(
        "effective_date,amount\n2026-01-31,10000.00\n", encoding="utf-8",
    )
    con = _run_federal_tax_accumulator(tmp_path, repo, fund_asset_savings)
    try:
        assert _summary(con, 2026, "federal_itemized_deduction") == 10_000_00
        assert _summary(con, 2026, "federal_deduction_used") == 31_500_00
    finally:
        con.close()


def test_no_itemized_deductions_file_behaves_as_before(
    test_data_dir: Path, tmp_path: Path, fund_asset_savings: Callable[..., None],
) -> None:
    repo = test_data_dir
    con = _run_federal_tax_accumulator(tmp_path, repo, fund_asset_savings)
    try:
        assert _summary(con, 2026, "federal_itemized_deduction") == 0
        assert _summary(con, 2026, "federal_deduction_used") == 31_500_00
    finally:
        con.close()


def test_itemized_deduction_amount_can_reference_an_itemization(
    test_data_dir: Path, tmp_path: Path, fund_asset_savings: Callable[..., None],
) -> None:
    repo = test_data_dir
    (repo / "itemizations").mkdir(exist_ok=True)
    (repo / "itemizations" / "medical_2026.csv").write_text(
        "Item,Value\nsurgery,32000.00\nprescriptions,3000.00\n", encoding="utf-8",
    )
    (repo / "aux" / "federal_deductions.csv").write_text(
        "effective_date,amount\n2026-01-31,@medical_2026\n", encoding="utf-8",
    )
    con = _run_federal_tax_accumulator(tmp_path, repo, fund_asset_savings)
    try:
        assert _summary(con, 2026, "federal_itemized_deduction") == 35_000_00
        assert _summary(con, 2026, "federal_deduction_used") == 35_000_00
    finally:
        con.close()


def test_state_itemized_deduction_falls_back_to_federal_when_absent(test_data_dir: Path, tmp_path: Path) -> None:
    """A scenario with no state_deductions rows at all uses federal_deductions'
    figure for the state scope too, so a state-specific file isn't needed just
    to reuse the same itemized total."""
    import pandas as pd

    repo = test_data_dir
    (repo / "aux" / "federal_deductions.csv").write_text(
        "effective_date,amount\n2026-01-31,40000.00\n", encoding="utf-8",
    )
    accounts_path = repo / "scenarios" / "retire_alice" / "accounts.csv"
    df = pd.read_csv(accounts_path, dtype=str, keep_default_na=False)
    row = dict.fromkeys(df.columns, "")
    row.update(name="Expense_Taxes_State", kind="expense", subkind="taxes_state",
              owner="joint", tax_treatment="none", opened="2020-01-31", meta_json="{}")
    df.loc[len(df)] = row
    df.to_csv(accounts_path, index=False)

    sched_csv = repo / "scenarios" / "retire_alice" / "scheduled.csv"
    _append_scheduled(
        sched_csv,
        name="tax_state_prev_year",
        from_account="Asset_Checking",
        to_account="Expense_Taxes_State",
        cadence="annual",
        month_of_year="4",
        amount_rule_kind="tax_accumulator",
        tax_character="none",
        start="2027-04-30",
        priority="400",
        meta_json="{}",
        params_json='{"year":"previous","scope":"state"}',
    )
    intent_path = load_scenario(
        repo, "retire_alice",
        intent_dir=tmp_path / "intent", loaded_by="test",
        allow_dirty_data=True,
    ).intent_path
    run_path = tmp_path / "r.sqlite3"
    run_scenario(intent_path, run_path, run_by="test", allow_dirty_data=True)
    con = sqlite3.connect(run_path)
    con.row_factory = sqlite3.Row
    try:
        assert _summary(con, 2026, "state_itemized_deduction") == 40_000_00
    finally:
        con.close()


def test_person_lifespan_stops_associated_transfers(
    test_data_dir: Path, tmp_path: Path,
) -> None:
    """A person's final_month stops transfers tied to them (via explicit
    `person` column or account owner) but leaves joint-account transfers
    running for the survivor (ADR-003 §4.10)."""
    repo = test_data_dir
    _shorten_scenario_end(repo, "2028-12-31")  # only 2026-2027 events are asserted on
    lifespan_csv = repo / "scenarios" / "retire_alice" / "lifespan.csv"
    lifespan_csv.write_text("person,final_month\nalice,2027-01-31\n")

    intent_path = load_scenario(
        repo, "retire_alice",
        intent_dir=tmp_path / "intent", loaded_by="test",
        allow_dirty_data=True,
    ).intent_path
    run_path = tmp_path / "r.sqlite3"
    run_scenario(
        intent_path, run_path,
        run_by="test", allow_dirty_data=True,
    )

    con = sqlite3.connect(run_path)
    try:
        salary_alice_id, ss_alice_id, checking_interest_id = (
            con.execute(
                "SELECT id FROM scheduled_transaction WHERE name=?", (name,)
            ).fetchone()[0]
            for name in ("salary_alice", "ss_alice", growth_rule_name("Asset_Checking", "default"))
        )

        # Alice-owned rule: nothing fires after her final_month.
        after = con.execute(
            "SELECT COUNT(*) FROM transfer_event "
            "WHERE reason_id=? AND date > '2027-01-31'", (salary_alice_id,),
        ).fetchone()[0]
        assert after == 0
        before = con.execute(
            "SELECT COUNT(*) FROM transfer_event "
            "WHERE reason_id=? AND date <= '2027-01-31'", (salary_alice_id,),
        ).fetchone()[0]
        assert before > 0

        # ss_benefit is explicitly gated by `person`='alice' too.
        ss_after = con.execute(
            "SELECT COUNT(*) FROM transfer_event "
            "WHERE reason_id=? AND date > '2027-01-31'", (ss_alice_id,),
        ).fetchone()[0]
        assert ss_after == 0

        # Joint checking interest is unaffected by Alice's lifespan.
        interest_after = con.execute(
            "SELECT COUNT(*) FROM transfer_event "
            "WHERE reason_id=? AND date > '2027-01-31'", (checking_interest_id,),
        ).fetchone()[0]
        assert interest_after > 0
    finally:
        con.close()


def test_person_lifespan_does_not_stop_solely_owned_asset_growth(
    test_data_dir: Path, tmp_path: Path, fund_asset_savings: Callable[..., None],
) -> None:
    """percent_of_balance is always asset growth (ADR-003 \u00a72.2: "an asset
    account's growth is a property of the account"), not a personal service
    tied to the living owner -- a solely-owned IRA/checking balance keeps
    earning interest/appreciating after its owner's death, unlike salary or
    ss_benefit which are genuinely gated by the owner's lifespan."""
    import pandas as pd

    repo = test_data_dir
    fund_asset_savings()
    accounts_csv = repo / "scenarios" / "retire_alice" / "accounts.csv"
    df = pd.read_csv(accounts_csv, dtype=str, keep_default_na=False)
    mask = df["name"] == "Asset_TraditionalIRA_Alice"
    df.loc[mask, "series"] = "brokerage_growth"
    df.loc[mask, "tax_character"] = "deferred"
    df.to_csv(accounts_csv, index=False)

    lifespan_csv = repo / "scenarios" / "retire_alice" / "lifespan.csv"
    lifespan_csv.write_text("person,final_month\nalice,2027-01-31\n")

    intent_path = load_scenario(
        repo, "retire_alice",
        intent_dir=tmp_path / "intent", loaded_by="test",
        allow_dirty_data=True,
    ).intent_path
    run_path = tmp_path / "r.sqlite3"
    run_scenario(
        intent_path, run_path,
        run_by="test", allow_dirty_data=True,
    )

    con = sqlite3.connect(run_path)
    try:
        growth_id = con.execute(
            "SELECT id FROM scheduled_transaction WHERE name = ?",
            (growth_rule_name("Asset_TraditionalIRA_Alice", "default"),),
        ).fetchone()[0]
        growth_after = con.execute(
            "SELECT COUNT(*) FROM transfer_event "
            "WHERE reason_id=? AND date > '2027-01-31'", (growth_id,),
        ).fetchone()[0]
        assert growth_after > 0
    finally:
        con.close()


def test_on_death_sweeps_full_balance_once(
    test_data_dir: Path, tmp_path: Path,
) -> None:
    """on_death (no ADR section number yet) fires exactly once, the month
    after the named person's lifespan.csv final_month, sweeping 100% of
    from_account into to_account -- e.g. liquidating a solely-owned asset."""
    repo = test_data_dir
    _shorten_scenario_end(repo, "2028-12-31")  # only 2027-02 events are asserted on
    lifespan_csv = repo / "scenarios" / "retire_alice" / "lifespan.csv"
    lifespan_csv.write_text("person,final_month\nalice,2027-01-31\n")

    scheduled_csv = repo / "scenarios" / "retire_alice" / "scheduled.csv"
    _append_scheduled(
        scheduled_csv,
        name="sell_roth_alice",
        from_account="Asset_Roth_Alice",
        from_lot="default",
        to_account="Asset_Checking",
        cadence="monthly",
        amount_rule_kind="on_death",
        person="alice",
        tax_character="tax_free",
        start="2026-01-31",
        priority="900",
    )

    intent_path = load_scenario(
        repo, "retire_alice",
        intent_dir=tmp_path / "intent", loaded_by="test",
        allow_dirty_data=True,
    ).intent_path
    run_path = tmp_path / "r.sqlite3"
    run_scenario(
        intent_path, run_path,
        run_by="test", allow_dirty_data=True,
    )

    con = sqlite3.connect(run_path)
    try:
        rule_id = con.execute(
            "SELECT id FROM scheduled_transaction WHERE name='sell_roth_alice'"
        ).fetchone()[0]
        roth_id, checking_id = (
            con.execute(
                "SELECT id FROM account WHERE name=?", (name,)
            ).fetchone()[0]
            for name in ("Asset_Roth_Alice", "Asset_Checking")
        )

        # Fires exactly once, the month after death (2027-01-31 -> 2027-02-28).
        events = con.execute(
            "SELECT date, amount FROM transfer_event WHERE reason_id=?", (rule_id,),
        ).fetchall()
        assert events == [("2027-02-28", 10_000_000)]  # $100,000.00 opening balance

        # Roth zeroed out, checking received the full amount, from that month on.
        roth_after = con.execute(
            "SELECT ast.balance FROM account_state ast "
            "JOIN simulation_step s ON s.id = ast.step_id "
            "WHERE ast.account_id=? AND s.month='2027-02-28'", (roth_id,),
        ).fetchone()[0]
        assert roth_after == 0

        checking_before, checking_after = (
            con.execute(
                "SELECT ast.balance FROM account_state ast "
                "JOIN simulation_step s ON s.id = ast.step_id "
                "WHERE ast.account_id=? AND s.month=?", (checking_id, m),
            ).fetchone()[0]
            for m in ("2027-01-31", "2027-02-28")
        )
        assert checking_after - checking_before >= 10_000_000
    finally:
        con.close()


def test_fixed_amount_applies_cpi_inflation(tmp_path: Path) -> None:
    """ADR-003 §2.1: fixed_amount grows with series/base_date, not
    a flat amount for the life of the run."""
    intent_path = _intent(tmp_path)
    run_path = tmp_path / "r.sqlite3"
    run_scenario(
        intent_path, run_path,
        run_by="test", allow_dirty_data=True,
    )

    con = sqlite3.connect(run_path)
    try:
        amounts = [
            row[0] for row in con.execute(
                "SELECT te.amount FROM transfer_event te "
                "JOIN scheduled_transaction st ON st.id = te.reason_id "
                "WHERE st.name = 'salary_alice' AND te.reason_kind = 'rule' "
                "ORDER BY te.date"
            ).fetchall()
        ]
        # First fire (base_date == scenario start) is unadjusted.
        assert amounts[0] == SALARY_ALICE_MONTHLY
        # Second fire, one month later, has grown by one month of CPI.
        assert amounts[1] == SALARY_ALICE_MONTHLY * _cpi_factor(1) // 1_000_000
        # No plateau: it keeps growing, not stuck at the base amount.
        assert amounts[-1] > amounts[1]
    finally:
        con.close()


def test_fixed_amount_annual_inflation_cadence_steps_once_per_year(test_data_dir: Path, tmp_path: Path) -> None:
    """ADR-003 §2.1: inflation_cadence='annual' holds flat within a calendar
    year and jumps once at each January crossing, unlike the default monthly
    compounding (e.g. Social Security COLA modeled as fixed_amount)."""
    import pandas as pd

    repo = test_data_dir
    sched_csv = repo / "scenarios" / "retire_alice" / "scheduled.csv"
    df = pd.read_csv(sched_csv, dtype=str, keep_default_na=False)
    df.loc[df["name"] == "salary_alice", "inflation_cadence"] = "annual"
    df.to_csv(sched_csv, index=False)

    intent_path = load_scenario(
        repo, "retire_alice",
        intent_dir=tmp_path / "intent", loaded_by="test",
        allow_dirty_data=True,
    ).intent_path
    run_path = tmp_path / "r.sqlite3"
    run_scenario(
        intent_path, run_path,
        run_by="test", allow_dirty_data=True,
    )

    con = sqlite3.connect(run_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT te.date, te.amount FROM transfer_event te "
            "JOIN scheduled_transaction st ON st.id = te.reason_id "
            "WHERE st.name = 'salary_alice' AND te.reason_kind = 'rule' "
            "ORDER BY te.date"
        ).fetchall()
        by_date = {r["date"]: r["amount"] for r in rows}
        # Flat for the rest of 2026 (base_date == scenario start, same year).
        assert by_date["2026-05-31"] == SALARY_ALICE_MONTHLY
        assert by_date["2026-12-31"] == SALARY_ALICE_MONTHLY
        # Steps up once at the January 2027 crossing, then flat through 2027.
        assert by_date["2027-01-31"] > SALARY_ALICE_MONTHLY
        assert by_date["2027-01-31"] == by_date["2027-12-31"]
        # A second annual step by January 2028.
        assert by_date["2028-01-31"] > by_date["2027-01-31"]
    finally:
        con.close()


def test_ytd_preroll_posts_balance_neutral_catchup(test_data_dir: Path, tmp_path: Path) -> None:
    """ADR-003 §4.11: ytd_preroll on a fixed_amount/monthly row posts a single
    balance-neutral transfer_event at step 0, dated before scenario.start."""
    import pandas as pd

    repo = test_data_dir
    sched_csv = repo / "scenarios" / "retire_alice" / "scheduled.csv"
    df = pd.read_csv(sched_csv, dtype=str, keep_default_na=False)
    df.loc[df["name"] == "salary_alice", "ytd_preroll"] = "1"
    df.to_csv(sched_csv, index=False)

    intent_path = load_scenario(
        repo, "retire_alice",
        intent_dir=tmp_path / "intent", loaded_by="test",
        allow_dirty_data=True,
    ).intent_path
    run_path = tmp_path / "r.sqlite3"
    run_scenario(
        intent_path, run_path,
        run_by="test", allow_dirty_data=True,
    )

    con = sqlite3.connect(run_path)
    con.row_factory = sqlite3.Row
    try:
        checking_id = con.execute(
            "SELECT id FROM account WHERE name='Asset_Checking'"
        ).fetchone()[0]
        opening_checking = con.execute(
            "SELECT balance FROM account_state ast "
            "JOIN simulation_step s ON s.id = ast.step_id "
            "WHERE s.step_index = 0 AND ast.account_id = ?", (checking_id,),
        ).fetchone()[0]

        salary_alice_id = con.execute(
            "SELECT id FROM scheduled_transaction WHERE name='salary_alice'"
        ).fetchone()[0]
        rows = con.execute(
            "SELECT date, amount, reason_kind FROM transfer_event "
            "WHERE reason_kind = 'ytd_rule' AND reason_id = ?", (salary_alice_id,),
        ).fetchall()
        assert [(row["date"], row["amount"]) for row in rows] == [
            ("2026-01-31", SALARY_ALICE_MONTHLY),
            ("2026-02-28", SALARY_ALICE_MONTHLY),
            ("2026-03-31", SALARY_ALICE_MONTHLY),
            ("2026-04-30", SALARY_ALICE_MONTHLY),
        ]

        # Balance-neutral: checking's step-0 balance is unaffected by the catch-up.
        assert opening_checking == EXPECTED_CHECKING[1]
    finally:
        con.close()


def test_year_to_date_csv_posts_balance_neutral_catchup(test_data_dir: Path, tmp_path: Path) -> None:
    """ADR-003 §4.11: year_to_date.csv rows post balance-neutral transfer_event
    rows at step 0, queryable by tax_character/account like any other flow."""
    repo = test_data_dir

    intent_path_base = load_scenario(
        repo, "retire_alice",
        intent_dir=tmp_path / "intent_base", loaded_by="test",
        allow_dirty_data=True,
    ).intent_path
    run_path_base = tmp_path / "base.sqlite3"
    run_scenario(
        intent_path_base, run_path_base,
        run_by="test", allow_dirty_data=True,
    )

    ytd_csv = repo / "scenarios" / "retire_alice" / "year_to_date.csv"
    ytd_csv.write_text(
        "name,from_account,to_account,to_lot,amount,tax_character,description\n"
        "prior_brokerage_growth,Income_CapitalGain,Asset_Brokerage_Joint,default,"
        "1500.00,ltcg,pre-simulation appreciation\n"
    )
    intent_path = load_scenario(
        repo, "retire_alice",
        intent_dir=tmp_path / "intent", loaded_by="test",
        allow_dirty_data=True,
    ).intent_path
    run_path = tmp_path / "r.sqlite3"
    run_scenario(
        intent_path, run_path,
        run_by="test", allow_dirty_data=True,
    )

    con_base = sqlite3.connect(run_path_base)
    con = sqlite3.connect(run_path)
    con.row_factory = sqlite3.Row
    try:
        brokerage_id = con.execute(
            "SELECT id FROM account WHERE name='Asset_Brokerage_Joint'"
        ).fetchone()[0]
        opening_brokerage = con.execute(
            "SELECT balance FROM account_state ast "
            "JOIN simulation_step s ON s.id = ast.step_id "
            "WHERE s.step_index = 0 AND ast.account_id = ?", (brokerage_id,),
        ).fetchone()[0]
        opening_brokerage_base = con_base.execute(
            "SELECT balance FROM account_state ast "
            "JOIN simulation_step s ON s.id = ast.step_id "
            "WHERE s.step_index = 0 AND ast.account_id = ?", (brokerage_id,),
        ).fetchone()[0]

        row = con.execute(
            "SELECT date, amount, tax_character FROM transfer_event "
            "WHERE reason_kind = 'ytd'"
        ).fetchone()
        assert row["date"] == "2026-04-30"
        assert row["amount"] == 150_000
        assert row["tax_character"] == "ltcg"

        # Balance-neutral: identical to the run without year_to_date.csv.
        assert opening_brokerage == opening_brokerage_base
    finally:
        con.close()
        con_base.close()


def test_tax_tail_reconciles_final_year_without_extending_simulation_data(
    test_data_dir: Path, tmp_path: Path, fund_asset_savings: Callable[..., None],
) -> None:
    """The scenario's declared final year (2040) gets a real tax_year_summary
    reconciliation (which normally fires the following April, past the
    scenario's end), but no simulation_step/account_state/transfer_event rows
    are written past scenario.end -- the tail is invisible to simulation data."""
    repo = test_data_dir
    fund_asset_savings()
    sched_csv = repo / "scenarios" / "retire_alice" / "scheduled.csv"
    _append_scheduled(
        sched_csv,
        name="tax_federal_prev_year",
        from_account="Asset_Checking",
        to_account="Expense_Taxes_Federal",
        cadence="annual",
        month_of_year="4",
        amount_rule_kind="tax_accumulator",
        tax_character="none",
        start="2027-04-30",
        priority="400",
        description="federal income tax forecast",
        meta_json="{}",
        params_json='{"year":"previous","scope":"federal"}',
    )

    intent_path = load_scenario(
        repo, "retire_alice",
        intent_dir=tmp_path / "intent", loaded_by="test",
        allow_dirty_data=True,
    ).intent_path
    run_path = tmp_path / "r.sqlite3"
    run_scenario(
        intent_path, run_path,
        run_by="test", allow_dirty_data=True,
    )

    con = sqlite3.connect(run_path)
    try:
        # retire_alice's scenario.csv ends 2040-12-31: its own reconciliation
        # would normally fire 2041-04-30, past the scenario end.
        liability_2040 = con.execute(
            "SELECT amount FROM tax_year_summary "
            "WHERE year = 2040 AND bucket = 'federal_tax_liability'"
        ).fetchone()
        assert liability_2040 is not None

        # No simulation data leaks past scenario.end.
        assert con.execute(
            "SELECT COUNT(*) FROM simulation_step WHERE month > '2040-12-31'"
        ).fetchone()[0] == 0
        assert con.execute(
            "SELECT COUNT(*) FROM transfer_event WHERE date > '2040-12-31'"
        ).fetchone()[0] == 0
        last_step_month = con.execute(
            "SELECT MAX(month) FROM simulation_step"
        ).fetchone()[0]
        assert last_step_month == "2040-12-31"
    finally:
        con.close()


def test_running_unresolved_vary_row_raises(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
    tmp_path: Path,
) -> None:
    """ADR-006: a recipe run directly (the 'vary' step skipped) must fail
    loudly instead of firing the placeholder amount/date."""
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="45000",
        vary_target="amount", vary_distribution="uniform",
        vary_low="40000", vary_high="50000",
    )
    intent_path = load_scenario(
        test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True,
    ).intent_path
    run_path = tmp_path / "r.sqlite3"
    with pytest.raises(EngineError, match="vary_distribution"):
        run_scenario(intent_path, run_path, run_by="test", allow_dirty_data=True)


def test_running_unresolved_vary_row_outside_scenario_range_still_raises(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
    tmp_path: Path,
) -> None:
    """ADR-006: the guard must not depend on the row's own month ever being
    simulated -- a row dated after the scenario ends is never visited by the
    monthly loop, so it must be rejected before that loop starts."""
    append_calendar_row(
        date="2050-01-31", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="45000",
        vary_target="amount", vary_distribution="uniform",
        vary_low="40000", vary_high="50000",
    )
    intent_path = load_scenario(
        test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True,
    ).intent_path
    run_path = tmp_path / "r.sqlite3"
    with pytest.raises(EngineError, match="vary_distribution"):
        run_scenario(intent_path, run_path, run_by="test", allow_dirty_data=True)


def test_running_unresolved_vary_row_of_unsupported_kind_still_raises(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
    tmp_path: Path,
) -> None:
    """ADR-006: the guard must not depend on the row's kind being one the
    engine actually applies -- an unsupported kind is skipped before ever
    reaching ``_apply_calendar_event``, so it must be rejected up front.

    ``vary_target='date'`` (not 'amount') because the loader restricts an
    amount target to the kinds that have a real amount column to vary, which
    excludes every kind the engine doesn't support in the first place.
    """
    append_calendar_row(
        date="2027-02-28", kind="swap_aux_table", name="unsupported_kind_vary",
        vary_target="date", vary_distribution="uniform",
        vary_low="2027-01-01", vary_high="2027-12-31",
    )
    intent_path = load_scenario(
        test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True,
    ).intent_path
    run_path = tmp_path / "r.sqlite3"
    with pytest.raises(EngineError, match="vary_distribution"):
        run_scenario(intent_path, run_path, run_by="test", allow_dirty_data=True)
