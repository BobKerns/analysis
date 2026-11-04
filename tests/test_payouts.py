"""A taxable lot's yearly payouts: dividends and interest, paid out, taxed and
reinvested each month (#468, ADR-005 §5)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from analysis.sim.accounts import load_accounts_view
from analysis.sim.connection import RunConnection, ScenarioConnection
from analysis.sim.engine import run_scenario
from analysis.sim.loader import LoadResult, growth_rule_name

BROKERAGE = "Asset_Brokerage_Joint"
GROWTH = growth_rule_name(BROKERAGE, "fund")
DIVIDENDS = growth_rule_name(BROKERAGE, "fund", prefix="dividends_")
INTEREST = growth_rule_name(BROKERAGE, "fund", prefix="interest_")


def _query(db: Path, sql: str, *args: Any) -> list[tuple]:
    con = sqlite3.connect(db)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def _rules(intent: Path) -> dict[str, tuple[str | None, dict, str]]:
    """Every growth and payout rule: name to (series, params, tax_character)."""
    return {
        name: (series, json.loads(params or "{}"), character)
        for name, series, params, character in _query(
            intent,
            "SELECT name, series, params_json, tax_character FROM scheduled_transaction "
            "WHERE name LIKE 'growth_%' OR name LIKE 'dividends_%' OR name LIKE 'interest_%'")
    }


def _first_month(run: Path, rule: str) -> tuple[str, int]:
    """A rule's first month and its amount then."""
    return _query(run, (
        "SELECT s.month, SUM(t.amount) FROM transfer_event t "
        "JOIN scheduled_transaction r ON r.id = t.reason_id "
        "JOIN simulation_step s ON s.id = t.step_id "
        "WHERE t.reason_kind = 'rule' AND r.name = ? "
        "GROUP BY s.month ORDER BY s.month LIMIT 1"), rule)[0]


def _lot(run: Path, month: str) -> tuple[int, int]:
    return _query(run, (
        "SELECT ls.balance, ls.cost_basis FROM lot_state ls "
        "JOIN simulation_step s ON s.id = ls.step_id "
        "JOIN account_lot m ON m.id = ls.lot_id JOIN account a ON a.id = m.account_id "
        "WHERE a.name = ? AND m.lot = 'fund' AND s.month = ?"), BROKERAGE, month)[0]


@pytest.fixture
def yields(write_series: Callable[[str, str], Path]) -> None:
    """Yields for ``blended_fund``'s classes: stocks pay 2% of their 8%,
    bonds all 4% of theirs."""
    write_series("stock_us_yield", "2026-01-31,2%")
    write_series("bond_yield", "2026-01-31,4%")


@pytest.mark.usefixtures("without_prototype_defaults", "blended_fund")
def test_without_yield_series_a_lot_pays_nothing(load_test_copy: Callable[[], LoadResult]) -> None:
    rules = _rules(load_test_copy().intent_path)
    assert not [n for n in rules if n.startswith(("dividends_", "interest_"))]
    assert "less" not in rules[GROWTH][1]


@pytest.mark.usefixtures("without_prototype_defaults", "blended_fund", "yields")
def test_a_fund_pays_each_class_yield_split_by_how_it_is_taxed(
    load_test_copy: Callable[[], LoadResult],
) -> None:
    result = load_test_copy()
    assert not result.warnings
    rules = _rules(result.intent_path)
    assert rules[DIVIDENDS] == (None, {"blend": [["", [["stock_us_yield", 600_000]]]]},
                                "qualified_dividend")
    assert rules[INTEREST] == (None, {"blend": [["", [["bond_yield", 400_000]]]]}, "ordinary")
    assert sorted(map(json.dumps, rules[GROWTH][1]["less"])) == sorted(
        json.dumps({"blend": [["", [[paid, weight]]]]})
        for paid, weight in (("stock_us_yield", 600_000), ("bond_yield", 400_000)))


@pytest.mark.usefixtures("without_prototype_defaults", "blended_fund", "yields")
def test_payouts_and_appreciation_add_to_the_total_return_and_payouts_add_basis(
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    run = run_scenario(load_test_copy().intent_path, runs_dir / "payouts.sqlite3", run_by="TEST",
                       allow_dirty_data=True, allow_dirty_code=True)
    month, growth = _first_month(run, GROWTH)
    assert _first_month(run, DIVIDENDS) == (month, 30_000)   # $300,000 x 60% x 2% / 12
    assert _first_month(run, INTEREST) == (month, 40_000)    # $300,000 x 40% x 4% / 12
    assert growth + 30_000 + 40_000 == 160_000               # the 6.4% blend, as before
    # Appreciation leaves the basis alone; reinvested payouts are purchases.
    balance, basis = _lot(run, month)
    assert (balance, basis) == (30_000_000 + 160_000, 20_000_000 + 70_000)


@pytest.mark.usefixtures("blended_fund", "yields")
def test_qualified_dividends_are_taxed_at_capital_gains_rates(report_run_file: Path) -> None:
    run = report_run_file
    year = _query(run, "SELECT MIN(year) FROM tax_year_summary")[0][0]
    paid = dict(_query(run, (
        "SELECT r.name, SUM(t.amount) FROM transfer_event t "
        "JOIN scheduled_transaction r ON r.id = t.reason_id "
        "JOIN simulation_step s ON s.id = t.step_id "
        "WHERE t.reason_kind = 'rule' AND r.name IN (?, ?) AND substr(s.month, 1, 4) = ? "
        "GROUP BY r.name"), DIVIDENDS, INTEREST, str(year)))
    summary = dict(_query(run, "SELECT bucket, amount FROM tax_year_summary WHERE year = ?", year))
    assert paid[DIVIDENDS] > 0 and paid[INTEREST] > 0
    # Dividends stack on ordinary income at the capital gains rates; interest
    # is ordinary income.
    assert summary["federal_ltcg_income"] >= paid[DIVIDENDS]
    assert summary["federal_ordinary_income"] >= paid[INTEREST]


def test_only_taxable_brokerage_lots_pay_out(
    retire_alice_csv: Callable[[str], Path], write_series: Callable[[str, str], Path],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    """A traditional IRA growing at ``stock``, whose yield the template series
    carry (#468), still grows untouched: only a taxable brokerage pays out. A
    lot growing at a series of no asset class pays that series' yield as
    ordinary income."""
    lots = retire_alice_csv("account_lots.csv")
    lots.write_text(lots.read_text().replace(
        "Asset_TraditionalIRA_Alice,default,traditional_ira,,,,,",
        "Asset_TraditionalIRA_Alice,default,traditional_ira,,,,stock,"))
    write_series("brokerage_growth_yield", "2026-01-31,2%")
    rules = _rules(load_test_copy().intent_path)
    payouts = {n: rule for n, rule in rules.items() if n.startswith(("dividends_", "interest_"))}
    assert list(payouts.values()) == [("brokerage_growth_yield", {}, "ordinary")]
    assert list(payouts) == [growth_rule_name(BROKERAGE, "default", prefix="interest_")]
    ira = rules[growth_rule_name("Asset_TraditionalIRA_Alice", "default")]
    assert ira[0] == "stock" and "less" not in ira[1]


@pytest.mark.usefixtures("without_prototype_defaults")
def test_a_return_below_its_yield_is_an_unrealized_loss_that_sells_no_shares(
    test_data_dir: Path, set_brokerage_lots: Callable[[list[dict[str, str]]], None],
    write_series: Callable[[str, str], Path], load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    """A bond fund whose price falls as much as it pays: 4% interest, 0%
    total return. The interest buys shares; the fall is an unrealized loss,
    which sells none, and the gains report nets it against the interest."""
    set_brokerage_lots([{"lot": "fund", "symbol": "BND", "quantity": "1000", "series": "bond",
                         "opening_balance": "300000.00", "opening_cost_basis": "300000.00"}])
    calendar = test_data_dir / "scenarios" / "retire_alice" / "calendar.csv"
    calendar.write_text(calendar.read_text().replace(
        ",Asset_Brokerage_Joint,default,", ",Asset_Brokerage_Joint,fund,"))
    write_series("bond", "2026-01-31,0%")
    write_series("bond_yield", "2026-01-31,4%")
    run = run_scenario(load_test_copy().intent_path, runs_dir / "loss.sqlite3", run_by="TEST",
                       allow_dirty_data=True, allow_dirty_code=True)
    month, interest = _first_month(run, INTEREST)
    # The fall posts as much back from the lot to capital_gains.
    assert _first_month(run, GROWTH) == (month, interest)
    quantity = _query(run, (
        "SELECT ls.quantity_micros FROM lot_state ls JOIN simulation_step s ON s.id = ls.step_id "
        "JOIN account_lot m ON m.id = ls.lot_id WHERE m.lot = 'fund' AND s.month = ?"), month)[0][0]
    assert quantity > 1000 * 1_000_000
    with RunConnection(run) as ran:
        gains = ran.gains_by_category()
    year = int(month[:4])
    paid = _query(run, (
        "SELECT SUM(t.amount) FROM transfer_event t JOIN scheduled_transaction r ON r.id = t.reason_id "
        "JOIN simulation_step s ON s.id = t.step_id "
        "WHERE t.reason_kind = 'rule' AND r.name = ? AND substr(s.month, 1, 4) = ?"), INTEREST, str(year))[0][0]
    assert gains.loc[year, "unrealized"] == pytest.approx(-paid / 100)
    assert gains.loc[year, "taxable"] >= paid / 100


def test_an_all_interest_lot_keeps_its_rate_in_the_account_view(
    write_series: Callable[[str, str], Path], set_brokerage_lots: Callable[[list[dict[str, str]]], None],
    test_data_dir: Path,
) -> None:
    """A cash lot in a brokerage pays all its return as interest and has no
    growth rule; the interest rule's series is still the account's rate."""
    set_brokerage_lots([{"lot": "default", "series": "cash", "opening_balance": "300000.00",
                         "opening_cost_basis": "300000.00"}])
    with ScenarioConnection(test_data_dir, "retire_alice") as scenario:
        [row] = [r for r in load_accounts_view(scenario) if r.name == BROKERAGE]
    assert row.rate_series == "cash"
