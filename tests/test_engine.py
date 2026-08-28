"""Tests for analysis.sim.engine."""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import date
from pathlib import Path

from analysis.sim.engine import run_scenario
from analysis.sim.loader import load_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = REPO_ROOT / "prototype_data"

# Net cents into Asset_Checking per month from fixed_amount rules, before CPI
# growth (salary_alice + salary_bob) - (living + housing + healthcare). All
# five rows share base_date = scenario start and series = cpi
# (prototype_data/aux/inflation__cpi.csv: flat 2.5% APR), so each grows by the
# same monthly-compounded factor. The mortgage payment isn't inflated.
SALARY_ALICE_MONTHLY = 800000
SALARY_BOB_MONTHLY = 750000
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
            + SALARY_BOB_MONTHLY * factor // 1_000_000
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

        # Inflation indexes are persisted monthly for each inflation series.
        n_inflation = con.execute(
            "SELECT COUNT(*) FROM inflation_index_state"
        ).fetchone()[0]
        assert n_inflation == len(steps)
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
        # draws via the automatic settle pass, ADR-003 §2.9).
        rmd_2038 = con.execute(
            "SELECT amount FROM transfer_event WHERE step_id=? AND reason_id=?",
            (steps[151]["id"], rmd_id),
        ).fetchone()[0]
        assert rmd_2038 == 702_471
        ira_2038 = con.execute(
            "SELECT balance FROM account_state WHERE step_id=? AND account_id=?",
            (steps[151]["id"], ira_id),
        ).fetchone()[0]
        assert ira_2038 == 15_032_392
        rmd_2039 = con.execute(
            "SELECT amount FROM transfer_event WHERE step_id=? AND reason_id=?",
            (steps[163]["id"], rmd_id),
        ).fetchone()[0]
        assert rmd_2039 == 589_505
        rmd_2040 = con.execute(
            "SELECT amount FROM transfer_event WHERE step_id=? AND reason_id=?",
            (steps[175]["id"], rmd_id),
        ).fetchone()[0]
        assert rmd_2040 == 486_590
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
        # Per-rule firing counts after calendar events expand:
        #   salary_alice retires 2028-06-30 (step 25, fires 0..25 = 26)
        #   salary_bob   retires 2030-12-31 (step 55, fires 0..55 = 56)
        #   one_off_transfer 2029-03-31 synthesizes 1 fixed_amount rule.
        # The asset_checking_liquidity settle pass (ADR-003 §2.9) posts its own
        # 'refill' legs outside reason_kind='rule', so it's checked separately.
        per_rule = dict(con.execute(
            "SELECT st.name, COUNT(*) FROM transfer_event te "
            "JOIN scheduled_transaction st ON st.id = te.reason_id "
            "WHERE te.reason_kind = 'rule' "
            "GROUP BY st.name"
        ).fetchall())
        assert per_rule["salary_alice"] == 26
        assert per_rule["salary_bob"] == 56
        assert per_rule["mortgage_pmt"] == 2 * 176  # 2 legs
        assert per_rule["living"] == 176
        assert per_rule["housing"] == 176
        assert per_rule["healthcare"] == 176
        assert per_rule["growth_asset_checking"] == 176
        assert per_rule["growth_asset_brokerage_joint"] == 176
        assert per_rule["ss_alice"] == 176 - 70
        assert per_rule["ss_bob"] == 176 - 135
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


def _clone_prototype(tmp_path: Path) -> Path:
    """Copy retire_alice prototype so tests can mutate scheduled.csv."""
    dst = tmp_path / "data"
    shutil.copytree(PROTOTYPE, dst)
    return dst


def _append_scheduled(path: Path, **fields: str) -> None:
    """Append one scheduled.csv row, addressing columns by name."""
    import pandas as pd

    from analysis.sim.registries import SCHEDULED_CSV_HEADER

    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    unknown = set(fields) - set(SCHEDULED_CSV_HEADER)
    assert not unknown, f"unknown scheduled.csv columns: {sorted(unknown)}"
    df.loc[len(df)] = [fields.get(column, "") for column in SCHEDULED_CSV_HEADER]
    df.to_csv(path, index=False)


def test_chain_draw_single_leaf_account_respects_threshold() -> None:
    """_chain_draw on a bare leaf chain (a real account) draws only down to
    its threshold_cents floor, and never draws the target from itself."""
    from analysis.sim.engine import ChainGraph, _chain_draw

    balances = {1: 100_000, 2: 500_000}
    account_kinds = {1: "asset", 2: "asset"}
    account_tax_treatment = {1: "taxable", 2: "taxable"}
    account_subkind = {1: "checking", 2: "savings"}
    account_name = {1: "Checking", 2: "Savings"}
    graph = ChainGraph({}, {10: 2}, {}, {}, {}, {})
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE transfer_event(id INTEGER PRIMARY KEY, step_id, date, "
        "from_account, to_account, amount, tax_character, reason_kind, "
        "reason_id, parent_event_id)"
    )
    try:
        taken = _chain_draw(
            con, balances, account_kinds, account_tax_treatment, account_subkind,
            account_name, step_id=1, month_s="2026-05-31", graph=graph,
            chain_id=10, amount_needed=1_000_000, target_account_id=1,
            threshold_cents=100_000,
        )
        assert taken == 400_000  # 500_000 available above the 100_000 floor
        assert balances[2] == 100_000
        assert balances[1] == 500_000
        row = con.execute(
            "SELECT from_account, to_account, amount, tax_character, reason_kind "
            "FROM transfer_event"
        ).fetchone()
        assert row == (2, 1, 400_000, "none", "refill")
    finally:
        con.close()


def test_chain_draw_never_draws_target_from_itself() -> None:
    from analysis.sim.engine import ChainGraph, _chain_draw

    balances = {1: 100_000}
    graph = ChainGraph({}, {10: 1}, {}, {}, {}, {})
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE transfer_event(id INTEGER PRIMARY KEY, step_id, date, "
        "from_account, to_account, amount, tax_character, reason_kind, "
        "reason_id, parent_event_id)"
    )
    try:
        taken = _chain_draw(
            con, balances, {1: "asset"}, {1: "taxable"}, {1: "checking"},
            {1: "Checking"}, step_id=1, month_s="2026-05-31", graph=graph,
            chain_id=10, amount_needed=50_000, target_account_id=1,
        )
        assert taken == 0
        assert con.execute("SELECT COUNT(*) FROM transfer_event").fetchone()[0] == 0
    finally:
        con.close()


def test_chain_draw_walks_multi_step_chain_in_order_with_per_leg_tax_character() -> None:
    """A chain with an interior step and two leaves drains them in
    step_order, and each leg's tax_character is derived from the source
    account it actually drew from (ADR-003 §2.9's core correctness goal)."""
    from analysis.sim.engine import ChainGraph, _chain_draw

    # 1=target checking, 2=taxable savings, 3=tax_deferred IRA.
    balances = {1: 0, 2: 200_000, 3: 1_000_000}
    account_kinds = {1: "asset", 2: "asset", 3: "asset"}
    account_tax_treatment = {1: "taxable", 2: "taxable", 3: "tax_deferred"}
    account_subkind = {1: "checking", 2: "savings", 3: "traditional_ira"}
    account_name = {1: "Checking", 2: "Savings", 3: "IRA"}
    # chain 100 = interior chain listing leaf chains 2 (savings) then 3 (IRA);
    # leaf chains share ids with their account for simplicity in this test.
    graph = ChainGraph(
        members={100: [(2, 0), (3, 0)]},
        leaf_account={2: 2, 3: 3},
        minimum_of={}, refill_chain_of={}, maximum_of={}, overflow_chain_of={},
    )
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE transfer_event(id INTEGER PRIMARY KEY, step_id, date, "
        "from_account, to_account, amount, tax_character, reason_kind, "
        "reason_id, parent_event_id)"
    )
    try:
        taken = _chain_draw(
            con, balances, account_kinds, account_tax_treatment, account_subkind,
            account_name, step_id=1, month_s="2026-05-31", graph=graph,
            chain_id=100, amount_needed=500_000, target_account_id=1,
        )
        assert taken == 500_000
        assert balances[2] == 0        # savings drained first, fully
        assert balances[3] == 700_000  # IRA supplies the remaining 300_000
        assert balances[1] == 500_000
        legs = con.execute(
            "SELECT from_account, amount, tax_character FROM transfer_event "
            "ORDER BY id"
        ).fetchall()
        assert legs == [
            (2, 200_000, "none"),            # taxable savings -> no withdrawal tax
            (3, 300_000, "tax_deferred_out"),  # IRA -> ordinary income on withdrawal
        ]
    finally:
        con.close()


def test_withdrawal_tax_character_rejects_taxable_non_cash_account() -> None:
    from analysis.sim.engine import EngineError, _withdrawal_tax_character

    try:
        _withdrawal_tax_character(
            3, {3: "taxable"}, {3: "brokerage_taxable"}, {3: "Brokerage"},
        )
    except EngineError as e:
        assert "Brokerage" in str(e)
    else:
        raise AssertionError("expected EngineError for taxable brokerage withdrawal")


def test_chain_sweep_allocates_fixed_then_percent_of_remainder() -> None:
    from analysis.sim.engine import ChainGraph, _chain_sweep

    balances = {1: 1_000_000, 2: 0, 3: 0, 4: 0}
    account_kinds = dict.fromkeys(balances, "asset")
    account_tax_treatment = dict.fromkeys(balances, "taxable")
    account_subkind = {1: "checking", 2: "checking", 3: "savings", 4: "hsa"}
    account_name = {1: "Income_SS", 2: "Checking", 3: "Savings", 4: "HSA"}
    graph = ChainGraph(
        members={100: [(2, 0, 100_000, None), (3, 0, None, 500_000),
                       (4, 0, None, 1_000_000)]},
        leaf_account={2: 2, 3: 3, 4: 4},
        minimum_of={}, refill_chain_of={}, maximum_of={2: None, 3: None, 4: None},
        overflow_chain_of={},
    )
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE transfer_event(id INTEGER PRIMARY KEY, step_id, date, "
        "from_account, to_account, amount, tax_character, reason_kind, "
        "reason_id, parent_event_id)"
    )
    try:
        swept = _chain_sweep(
            con, balances, account_kinds, account_tax_treatment, account_subkind,
            account_name, step_id=1, month_s="2026-05-31", graph=graph,
            chain_id=100, amount_available=1_000_000, source_account_id=1,
        )
        assert swept == 1_000_000
        assert balances[1] == 0
        assert {account_id: balances[account_id] for account_id in (2, 3, 4)} == {
            2: 100_000, 3: 450_000, 4: 450_000,
        }
        assert [row[2] for row in con.execute(
            "SELECT from_account, to_account, amount FROM transfer_event ORDER BY id"
        )] == [100_000, 450_000, 450_000]
    finally:
        con.close()


def test_ytd_preroll_expands_interior_chain_month_by_month() -> None:
    from analysis.sim.engine import ChainGraph, _apply_ytd_catchups

    graph = ChainGraph(
        members={100: [(2, 0, 25_000, None), (3, 0, None, 1_000_000)]},
        leaf_account={2: 2, 3: 3},
        minimum_of={}, refill_chain_of={}, maximum_of={}, overflow_chain_of={},
    )
    txn = [
        8, "social_security", 1, 100, "monthly", None, "fixed_amount",
        None, "rwk", None, None, None, 100_000, None, "ss_benefit",
        "2026-01-31", None, 200, None, None, 1, None,
    ]
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE transfer_event(id INTEGER PRIMARY KEY, step_id, date, "
        "from_account, to_account, amount, tax_character, reason_kind, reason_id)"
    )
    try:
        _apply_ytd_catchups(con, 1, date(2026, 5, 31), [txn], [], graph)
        rows = con.execute(
            "SELECT date, to_account, amount FROM transfer_event ORDER BY id"
        ).fetchall()
        assert rows == [
            ("2026-01-31", 2, 25_000), ("2026-01-31", 3, 75_000),
            ("2026-02-28", 2, 25_000), ("2026-02-28", 3, 75_000),
            ("2026-03-31", 2, 25_000), ("2026-03-31", 3, 75_000),
            ("2026-04-30", 2, 25_000), ("2026-04-30", 3, 75_000),
        ]
    finally:
        con.close()


def test_settle_order_settles_dependency_before_dependent() -> None:
    """An account whose refill_chain reaches another policy account's own
    account must settle after that account (ADR-003 §2.9's topological
    settle pass, independent of authoring/CSV order)."""
    from analysis.sim.engine import ChainGraph, _settle_order

    # Account 1 (checking) refills from chain 10 -> leaf account 2 (savings).
    # Account 2 (savings) itself has a policy: refills from chain 20 -> leaf
    # account 3 (IRA). So 2 must settle before 1.
    graph = ChainGraph(
        members={},
        leaf_account={10: 2, 20: 3},
        minimum_of={1: 100, 2: 100},
        refill_chain_of={1: 10, 2: 20},
        maximum_of={1: None, 2: None},
        overflow_chain_of={1: None, 2: None},
    )
    order = _settle_order(graph)
    assert order.index(2) < order.index(1)


def test_negative_rate_depreciates_asset_into_expense(tmp_path: Path) -> None:
    """ADR-003 §2.2: a negative rate series swaps the legs, so value leaves the
    asset for the built-in `depreciation` expense account, declining-balance."""
    import pandas as pd

    repo = _clone_prototype(tmp_path)
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
        run_by="test", data_mode="prototype", allow_dirty_data=True,
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
            "WHERE st.name = 'growth_asset_car' AND te.reason_kind = 'rule' "
            "ORDER BY te.date"
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


def test_tax_accumulator_computes_federal_and_writes_summary(tmp_path: Path) -> None:
    """tax_accumulator computes prior-year federal tax and writes summary rows.

    It emits a payment transfer only when computed due is positive.
    """
    repo = _clone_prototype(tmp_path)
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
        run_by="test", data_mode="prototype", allow_dirty_data=True,
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


def test_person_lifespan_stops_associated_transfers(tmp_path: Path) -> None:
    """A person's final_month stops transfers tied to them (via explicit
    `person` column or account owner) but leaves joint-account transfers
    running for the survivor (ADR-003 §4.10)."""
    repo = _clone_prototype(tmp_path)
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
        run_by="test", data_mode="prototype", allow_dirty_data=True,
    )

    con = sqlite3.connect(run_path)
    try:
        salary_alice_id, ss_alice_id, checking_interest_id = (
            con.execute(
                "SELECT id FROM scheduled_transaction WHERE name=?", (name,)
            ).fetchone()[0]
            for name in ("salary_alice", "ss_alice", "growth_asset_checking")
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


def test_fixed_amount_applies_cpi_inflation(tmp_path: Path) -> None:
    """ADR-003 §2.1: fixed_amount grows with series/base_date, not
    a flat amount for the life of the run."""
    intent_path = _intent(tmp_path)
    run_path = tmp_path / "r.sqlite3"
    run_scenario(
        intent_path, run_path,
        run_by="test", data_mode="prototype", allow_dirty_data=True,
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


def test_fixed_amount_annual_inflation_cadence_steps_once_per_year(tmp_path: Path) -> None:
    """ADR-003 §2.1: inflation_cadence='annual' holds flat within a calendar
    year and jumps once at each January crossing, unlike the default monthly
    compounding (e.g. Social Security COLA modeled as fixed_amount)."""
    import pandas as pd

    repo = _clone_prototype(tmp_path)
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
        run_by="test", data_mode="prototype", allow_dirty_data=True,
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


def test_ytd_preroll_posts_balance_neutral_catchup(tmp_path: Path) -> None:
    """ADR-003 §4.11: ytd_preroll on a fixed_amount/monthly row posts a single
    balance-neutral transfer_event at step 0, dated before scenario.start."""
    import pandas as pd

    repo = _clone_prototype(tmp_path)
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
        run_by="test", data_mode="prototype", allow_dirty_data=True,
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
        assert len(rows) == 1
        # scenario start = 2026-05-31 -> 4 elapsed months (Jan-Apr), dated 2026-04-30.
        assert rows[0]["date"] == "2026-04-30"
        assert rows[0]["amount"] == SALARY_ALICE_MONTHLY * 4

        # Balance-neutral: checking's step-0 balance is unaffected by the catch-up.
        assert opening_checking == EXPECTED_CHECKING[1]
    finally:
        con.close()


def test_year_to_date_csv_posts_balance_neutral_catchup(tmp_path: Path) -> None:
    """ADR-003 §4.11: year_to_date.csv rows post balance-neutral transfer_event
    rows at step 0, queryable by tax_character/account like any other flow."""
    repo = _clone_prototype(tmp_path)

    intent_path_base = load_scenario(
        repo, "retire_alice",
        intent_dir=tmp_path / "intent_base", loaded_by="test",
        allow_dirty_data=True,
    ).intent_path
    run_path_base = tmp_path / "base.sqlite3"
    run_scenario(
        intent_path_base, run_path_base,
        run_by="test", data_mode="prototype", allow_dirty_data=True,
    )

    ytd_csv = repo / "scenarios" / "retire_alice" / "year_to_date.csv"
    ytd_csv.write_text(
        "name,from_account,to_account,amount,tax_character,description\n"
        "prior_brokerage_growth,Income_CapitalGain,Asset_Brokerage_Joint,"
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
        run_by="test", data_mode="prototype", allow_dirty_data=True,
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
    tmp_path: Path,
) -> None:
    """The scenario's declared final year (2040) gets a real tax_year_summary
    reconciliation (which normally fires the following April, past the
    scenario's end), but no simulation_step/account_state/transfer_event rows
    are written past scenario.end -- the tail is invisible to simulation data."""
    repo = _clone_prototype(tmp_path)
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
        run_by="test", data_mode="prototype", allow_dirty_data=True,
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
        assert liability_2040[0] > 0

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
