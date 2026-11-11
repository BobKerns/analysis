"""Municipal bond interest (#265): out of federal AGI, but in provisional
income and IRMAA's MAGI; a state taxes every state's bonds but its own."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from analysis.sim import registries
from analysis.sim.engine import run_scenario
from analysis.sim.loader import LoadResult, growth_rule_name
from analysis.sim.taxes import common

BROKERAGE = "Asset_Brokerage_Joint"


def _query(db: Path, sql: str, *args: Any) -> list[tuple]:
    with closing(sqlite3.connect(db)) as con:
        return con.execute(sql, args).fetchall()


NATIONAL = growth_rule_name(BROKERAGE, "default", prefix="tax_exempt_")
HOME = growth_rule_name(BROKERAGE, "ca", prefix="tax_exempt_ca_")


def _paid(run: Path, rule: str, year: int) -> int:
    return _query(run, (
        "SELECT COALESCE(SUM(t.amount), 0) FROM transfer_event t "
        "JOIN scheduled_transaction r ON r.id = t.reason_id "
        "WHERE t.reason_kind = 'rule' AND r.name = ? AND substr(t.date, 1, 4) = ?"), rule, str(year))[0][0]


def _summary(run: Path, year: int) -> dict[str, int]:
    return dict(_query(run, "SELECT bucket, amount FROM tax_year_summary WHERE year = ?", year))


@pytest.mark.usefixtures("muni_funds")
def test_muni_interest_is_exempt_federally_and_taxed_by_a_state_unless_its_own(
    load_test_copy: Callable[[], LoadResult], runs_dir: Path, add_state_tax: Callable[..., None],
) -> None:
    add_state_tax()  # California
    result = load_test_copy()
    payers = dict(_query(result.intent_path, (
        "SELECT s.name, a.name || ':' || s.tax_character FROM scheduled_transaction s "
        "JOIN chain c ON c.id = s.from_account JOIN account a ON a.id = c.account_id "
        "WHERE s.name IN (?, ?)"), NATIONAL, HOME))
    assert sorted(payers.values()) == ["muni_interest:tax_exempt", "muni_interest__CA:tax_exempt"]
    run = run_scenario(result.intent_path, runs_dir / "muni.sqlite3", run_by="TEST",
                       allow_dirty_data=True, allow_dirty_code=True)

    national, home = _paid(run, NATIONAL, 2027), _paid(run, HOME, 2027)
    assert national > 0 and home > 0
    summary = _summary(run, 2027)
    assert summary["federal_tax_exempt_interest"] == national + home
    assert summary["federal_magi"] == summary["federal_agi"] + national + home
    # California taxes the other states' bonds, not its own.
    assert summary["state_muni_interest"] == national
    # Reinvested, the interest bought into the lot.
    [(basis,)] = _query(run, (
        "SELECT ls.cost_basis FROM lot_state ls JOIN simulation_step s ON s.id = ls.step_id "
        "JOIN account_lot m ON m.id = ls.lot_id JOIN account a ON a.id = m.account_id "
        "WHERE a.name = ? AND m.lot = 'ca' AND s.month = '2027-12-31'"), BROKERAGE)
    assert basis == 100_000_00 + sum(_paid(run, HOME, year) for year in (2026, 2027))


@pytest.mark.usefixtures("muni_funds")
def test_a_move_makes_the_old_states_bonds_taxable(
    load_test_copy: Callable[[], LoadResult], runs_dir: Path, add_state_tax: Callable[..., None],
    add_residence: Callable[..., Path],
) -> None:
    add_state_tax()
    add_residence("2028-01", "FL")
    run = run_scenario(load_test_copy().intent_path, runs_dir / "moved.sqlite3", run_by="TEST",
                       allow_dirty_data=True, allow_dirty_code=True)
    assert _summary(run, 2027)["state_muni_interest"] == _paid(run, NATIONAL, 2027)
    assert _summary(run, 2028)["state_muni_interest"] == (
        _paid(run, NATIONAL, 2028) + _paid(run, HOME, 2028))


def test_exempt_interest_counts_in_provisional_income() -> None:
    tiers = {"tier1": 32_000_00, "tier2": 44_000_00}
    without = common.taxable_social_security(
        ordinary_income=20_000_00, ltcg_income=0, ss_income=30_000_00, **tiers)
    assert without == 1_500_00  # provisional $35,000: half of the $3,000 over
    with_muni = common.taxable_social_security(
        ordinary_income=20_000_00, ltcg_income=0, ss_income=30_000_00, exempt_interest=10_000_00,
        **tiers)
    assert with_muni > without


def test_each_states_bonds_are_an_asset_class_with_its_own_payer() -> None:
    assert "muni_CA" in registries.ASSET_CLASSES
    assert registries.muni_state("muni_CA") == "CA"
    assert registries.muni_state("muni") == ""
    assert registries.muni_state("bond") is None
    assert registries.payout_account("muni_NY").name == "muni_interest__NY"
    assert "muni_<ST>" in registries.asset_class_list()


def _payout_rules(intent: Path) -> dict[str, tuple[str, str]]:
    """The brokerage's payout rules: name to (params_json, tax_character)."""
    return {name: (params, character) for name, params, character in _query(intent, (
        "SELECT s.name, s.params_json, s.tax_character FROM scheduled_transaction s "
        "WHERE json_extract(s.params_json, '$.payout')"))}


@pytest.mark.parametrize(("muni", "exempt"), [("97%", True), ("40%", False)])
def test_a_fund_paying_its_own_yield_is_exempt_where_mostly_muni_bonds(
    set_brokerage_lots: Callable[[list[dict[str, str]]], None], write_series: Callable[[str, str], Path],
    test_data_dir: Path, load_test_copy: Callable[[], LoadResult], muni: str, exempt: bool,
) -> None:
    """IRC §852(b)(5): a fund at least half municipal bonds pays
    exempt-interest dividends, its muni share's; a fund less than half pays
    none."""
    set_brokerage_lots([{"lot": "default", "symbol": "FND", "opening_balance": "100000.00",
                         "opening_cost_basis": "100000.00"}])
    rest = f"{100 - int(muni.rstrip('%'))}%"
    (test_data_dir / "allocations.csv").write_text(
        f"symbol,as_of,asset_class,weight\nFND,,muni,{muni}\nFND,,cash,{rest}\n")
    for series, rate in (("muni", "4%"), ("cash", "3%"), ("FND_yield", "3%")):
        write_series(series, f"2026-01-31,{rate}")
    rules = _payout_rules(load_test_copy().intent_path)
    characters = sorted(character for _, character in rules.values())
    assert characters == (["ordinary", "tax_exempt"] if exempt else ["ordinary"])
    if exempt:
        [params] = [p for p, c in rules.values() if c == "tax_exempt"]
        assert json.loads(params)["blend"] == [["", [["FND_yield", 970_000]]]]


@pytest.mark.usefixtures("muni_funds")
@pytest.mark.parametrize(("state", "taxed"), [("CA", "national"), ("DC", "none")])
def test_territory_bonds_are_exempt_everywhere_and_dc_taxes_no_muni_bonds(
    load_test_copy: Callable[[], LoadResult], runs_dir: Path, add_state_tax: Callable[..., None],
    retire_alice_csv: Callable[[str], Path], write_series: Callable[[str, str], Path],
    state: str, taxed: str,
) -> None:
    add_state_tax(state)
    with retire_alice_csv("account_lots.csv").open("a") as f:
        f.write("Asset_Brokerage_Joint,pr,,,,,muni_PR,50000.00,50000.00" + "," * 9 + "\n")
    write_series("muni_PR", "2026-01-31,4%")
    write_series("muni_PR_yield", "2026-01-31,3%")
    run = run_scenario(load_test_copy().intent_path, runs_dir / f"{state}.sqlite3", run_by="TEST",
                       allow_dirty_data=True, allow_dirty_code=True)
    puerto_rico = _paid(run, growth_rule_name(BROKERAGE, "pr", prefix="tax_exempt_pr_"), 2027)
    assert puerto_rico > 0
    expected = _paid(run, NATIONAL, 2027) if taxed == "national" else 0
    assert _summary(run, 2027).get("state_muni_interest", 0) == expected
