"""A lot's yearly payouts: dividends, interest and gain distributions, paid out,
taxed and reinvested each month, or untaxed in a retirement lot (#468, #547,
ADR-005 §5)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from analysis.sim.accounts import load_accounts_view
from analysis.sim.connection import RunConnection, ScenarioConnection
from analysis.sim.engine import run_scenario
from analysis.sim.loader import LoadError, LoadResult, growth_rule_name

BROKERAGE = "Asset_Brokerage_Joint"
GROWTH = growth_rule_name(BROKERAGE, "fund")
DIVIDENDS = growth_rule_name(BROKERAGE, "fund", prefix="dividends_")
# A fund's non-qualified dividends (#547): its bond slice's, here.
ORDINARY_DIVIDENDS = growth_rule_name(BROKERAGE, "fund", prefix="ordinary_dividends_")


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
            "WHERE name LIKE 'growth_%' OR name LIKE 'dividends_%' OR name LIKE 'interest_%' "
            "OR name LIKE 'ordinary_dividends_%'")
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
    assert not [n for n in rules if n.startswith(("dividends_", "interest_", "ordinary_dividends_"))]
    assert "less" not in rules[GROWTH][1]


@pytest.mark.usefixtures("without_prototype_defaults", "blended_fund", "yields")
def test_a_fund_pays_each_class_yield_split_by_how_it_is_taxed(
    load_test_copy: Callable[[], LoadResult],
) -> None:
    result = load_test_copy()
    assert not result.warnings
    rules = _rules(result.intent_path)
    assert rules[DIVIDENDS] == (
        None, {"blend": [["", [["stock_us_yield", 600_000]]]], "payout": True},
        "qualified_dividend")
    assert rules[ORDINARY_DIVIDENDS] == (
        None, {"blend": [["", [["bond_yield", 400_000]]]], "payout": True}, "ordinary")
    # A fund's bond slice pays a dividend, not interest (#547): only a bond
    # or cash held directly pays interest.
    payers = dict(_query(result.intent_path, (
        "SELECT s.name, a.name FROM scheduled_transaction s JOIN chain c ON c.id = s.from_account "
        "JOIN account a ON a.id = c.account_id WHERE s.name IN (?, ?)"), DIVIDENDS, ORDINARY_DIVIDENDS))
    assert payers == {DIVIDENDS: "dividends", ORDINARY_DIVIDENDS: "ordinary_dividends"}
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
    assert _first_month(run, ORDINARY_DIVIDENDS) == (month, 40_000)    # $300,000 x 40% x 4% / 12
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
        "GROUP BY r.name"), DIVIDENDS, ORDINARY_DIVIDENDS, str(year)))
    summary = dict(_query(run, "SELECT bucket, amount FROM tax_year_summary WHERE year = ?", year))
    assert paid[DIVIDENDS] > 0 and paid[ORDINARY_DIVIDENDS] > 0
    # Dividends stack on ordinary income at the capital gains rates; interest
    # is ordinary income.
    assert summary["federal_ltcg_taxed"] >= paid[DIVIDENDS]
    assert summary["federal_ordinary_income"] >= paid[ORDINARY_DIVIDENDS]


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
    assert list(payouts.values()) == [("brokerage_growth_yield", {"payout": True}, "ordinary")]
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
    month, interest = _first_month(run, ORDINARY_DIVIDENDS)
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
        "WHERE t.reason_kind = 'rule' AND r.name = ? AND substr(s.month, 1, 4) = ?"),
        ORDINARY_DIVIDENDS, str(year))[0][0]
    assert gains.loc[year, "unrealized"] == pytest.approx(-paid / 100)
    assert gains.loc[year, "taxable"] >= paid / 100


@pytest.mark.usefixtures("without_prototype_defaults")
def test_cash_at_a_negative_rate_pays_no_interest_and_loses_value(
    test_data_dir: Path, set_brokerage_lots: Callable[[list[dict[str, str]]], None],
    write_series: Callable[[str, str], Path], load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    """Cash pays all its return as interest, but a negative rate is no
    negative interest: nothing is taxed, and the fall is an unrealized loss
    carried by the lot's growth rule."""
    set_brokerage_lots([{"lot": "default", "series": "cash", "opening_balance": "300000.00",
                         "opening_cost_basis": "300000.00"}])
    write_series("cash", "2026-01-31,-1.2%")
    run = run_scenario(load_test_copy().intent_path, runs_dir / "negative.sqlite3", run_by="TEST",
                       allow_dirty_data=True, allow_dirty_code=True)
    interest = growth_rule_name(BROKERAGE, "default", prefix="interest_")
    assert not _query(run, (
        "SELECT 1 FROM transfer_event t JOIN scheduled_transaction r ON r.id = t.reason_id "
        "WHERE t.reason_kind = 'rule' AND r.name = ? AND t.amount != 0"), interest)
    month, fall = _first_month(run, growth_rule_name(BROKERAGE, "default"))
    # 1.2% a year on $300,000 is $300 a month, posted from the lot.
    assert fall == pytest.approx(30_000, rel=0.01)
    with RunConnection(run) as ran:
        gains = ran.gains_by_category()
    assert gains.loc[int(month[:4]), "unrealized"] < 0


@pytest.mark.usefixtures("without_prototype_defaults", "blended_fund", "yields")
def test_reinvested_payouts_are_taxable_gains_that_cover_no_spending(
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    """A reinvested payout is a taxable gain the year it's paid, but it never
    reaches cash, so it pays for none of the year's expenses."""
    run = run_scenario(load_test_copy().intent_path, runs_dir / "coverage.sqlite3", run_by="TEST",
                       allow_dirty_data=True, allow_dirty_code=True)
    year = int(_first_month(run, DIVIDENDS)[0][:4])
    paid = _query(run, (
        "SELECT SUM(t.amount) FROM transfer_event t JOIN scheduled_transaction r ON r.id = t.reason_id "
        "WHERE t.reason_kind = 'rule' AND r.name IN (?, ?) AND substr(t.date, 1, 4) = ?"),
        DIVIDENDS, ORDINARY_DIVIDENDS, str(year))[0][0]
    with RunConnection(run) as ran:
        row = ran.gains_by_category().loc[year]
    assert row["taxable"] >= paid / 100
    assert row["distribution_coverage"] == pytest.approx(
        row["realized_total"] - paid / 100 - row["expenses"])


@pytest.mark.usefixtures("without_prototype_defaults", "blended_fund")
def test_a_negative_slice_leaves_a_funds_dividends_whole(
    test_data_dir: Path, write_series: Callable[[str, str], Path],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    """Half bonds paying 4%, half cash at -1.2%: the bonds' payout is paid
    in full, and the cash's fall stays an unrealized loss on the growth rule,
    so the fund's total return is unchanged."""
    (test_data_dir / "allocations.csv").write_text(
        "symbol,as_of,asset_class,weight\nFND,,bond,50%\nFND,,cash,50%\n")
    write_series("bond_yield", "2026-01-31,4%")
    write_series("cash", "2026-01-31,-1.2%")
    run = run_scenario(load_test_copy().intent_path, runs_dir / "slice.sqlite3", run_by="TEST",
                       allow_dirty_data=True, allow_dirty_code=True)
    month, interest = _first_month(run, ORDINARY_DIVIDENDS)
    # $150,000 of bonds at 4% a year: $500 a month.
    assert interest == 50_000
    # The fund returns 1.4% a year, $350 a month: $150 less than it paid.
    assert _first_month(run, GROWTH) == (month, 15_000)


def test_an_all_interest_lot_keeps_its_rate_in_the_account_view(
    write_series: Callable[[str, str], Path], set_brokerage_lots: Callable[[list[dict[str, str]]], None],
    test_data_dir: Path,
) -> None:
    """A cash lot in a brokerage pays all its return as interest; the
    interest rule's series is still the account's rate."""
    set_brokerage_lots([{"lot": "default", "series": "cash", "opening_balance": "300000.00",
                         "opening_cost_basis": "300000.00"}])
    with ScenarioConnection(test_data_dir, "retire_alice") as scenario:
        [row] = [r for r in load_accounts_view(scenario) if r.name == BROKERAGE]
    assert row.rate_series == "cash"


@pytest.mark.usefixtures("without_prototype_defaults", "blended_fund", "yields")
def test_a_blended_funds_payouts_are_not_shown_as_its_rate(test_data_dir: Path) -> None:
    """A fund growing at its blend has a growth rule with no series; its
    payout rules mustn't stand in for its total-return rate."""
    with ScenarioConnection(test_data_dir, "retire_alice") as scenario:
        [row] = [r for r in load_accounts_view(scenario) if r.name == BROKERAGE]
    assert row.rate_series is None or "yield" not in row.rate_series


LT_DISTRIBUTIONS = growth_rule_name(BROKERAGE, "fund", prefix="lt_distributions_")
ST_DISTRIBUTIONS = growth_rule_name(BROKERAGE, "fund", prefix="st_distributions_")


@pytest.fixture
def distributions(write_series: Callable[[str, str], Path]) -> None:
    """Gain distributions for ``blended_fund``'s stocks (#547): 1.2% of their
    8% long-term, 0.6% short-term; bonds distribute none."""
    write_series("stock_us_cg_yield", "2026-01-31,1.2%")
    write_series("stock_us_stcg_yield", "2026-01-31,0.6%")


@pytest.mark.usefixtures("without_prototype_defaults", "blended_fund", "yields", "distributions")
def test_gain_distributions_come_out_of_total_return_taxed_by_their_term(
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    """Long-term distributions are long-term gains; short-term ones are
    ordinary (non-qualified) dividends. Both are reinvested with basis, and
    the total return is unchanged."""
    result = load_test_copy()
    assert not result.warnings
    rules = {name: rule for name, *rule in _query(
        result.intent_path,
        "SELECT s.name, s.series, params_json, tax_character, a.name FROM scheduled_transaction s "
        "JOIN chain c ON c.id = s.from_account JOIN account a ON a.id = c.account_id")}
    assert rules[LT_DISTRIBUTIONS] == [
        None, '{"blend":[["",[["stock_us_cg_yield",600000]]]],"payout":true}', "ltcg", "gain_distributions"]
    assert rules[ST_DISTRIBUTIONS] == [
        None, '{"blend":[["",[["stock_us_stcg_yield",600000]]]],"payout":true}', "ordinary",
        "ordinary_dividends"]
    assert len(json.loads(rules[GROWTH][1])["less"]) == 4
    run = run_scenario(result.intent_path, runs_dir / "distributions.sqlite3", run_by="TEST",
                       allow_dirty_data=True, allow_dirty_code=True)
    month, growth = _first_month(run, GROWTH)
    assert _first_month(run, LT_DISTRIBUTIONS) == (month, 18_000)   # $300,000 x 60% x 1.2% / 12
    assert _first_month(run, ST_DISTRIBUTIONS) == (month, 9_000)    # $300,000 x 60% x 0.6% / 12
    assert growth + 30_000 + 40_000 + 18_000 + 9_000 == 160_000
    assert _lot(run, month) == (30_000_000 + 160_000, 20_000_000 + 97_000)


@pytest.mark.usefixtures("blended_fund", "distributions")
def test_long_term_distributions_are_taxed_as_long_term_gains(report_run_file: Path) -> None:
    run = report_run_file
    year = _query(run, "SELECT MIN(year) FROM tax_year_summary")[0][0]
    [(paid,)] = _query(run, (
        "SELECT SUM(t.amount) FROM transfer_event t "
        "JOIN scheduled_transaction r ON r.id = t.reason_id "
        "JOIN simulation_step s ON s.id = t.step_id "
        "WHERE t.reason_kind = 'rule' AND r.name = ? AND substr(s.month, 1, 4) = ?"),
        LT_DISTRIBUTIONS, str(year))
    summary = dict(_query(run, "SELECT bucket, amount FROM tax_year_summary WHERE year = ?", year))
    assert paid > 0
    assert summary["federal_ltcg_taxed"] >= paid


def _set_retirement_lot(retire_alice_csv: Callable[[str], Path], old: str, new: str) -> None:
    lots = retire_alice_csv("account_lots.csv")
    text = lots.read_text()
    assert old in text
    lots.write_text(text.replace(old, new))


def _quantity(run: Path, account: str, month: str) -> int:
    return _query(run, (
        "SELECT ls.quantity_micros FROM lot_state ls JOIN simulation_step s ON s.id = ls.step_id "
        "JOIN account_lot m ON m.id = ls.lot_id JOIN account a ON a.id = m.account_id "
        "WHERE a.name = ? AND s.month = ?"), account, month)[0][0]


@pytest.mark.usefixtures("without_prototype_defaults", "blended_fund", "yields", "distributions")
def test_a_retirement_lot_holding_shares_reinvests_its_payouts_untaxed(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    """An IRA and a Roth holding FND shares (#547): their payouts come from
    the untaxed payout accounts and buy shares, but add no basis, so the
    Roth's basis stays its contributions. A Roth tracking dollars gets none."""
    _set_retirement_lot(retire_alice_csv, "Asset_TraditionalIRA_Alice,default,traditional_ira,,,,,500000.00,",
                        "Asset_TraditionalIRA_Alice,default,traditional_ira,FND,,5000,,500000.00,")
    _set_retirement_lot(retire_alice_csv, "Asset_Roth_Alice,default,roth_ira,,,,,100000.00,,",
                        "Asset_Roth_Alice,default,roth_ira,FND,,1000,,100000.00,50000.00,")
    # Bill's Roth holds FND too, but in dollars: it grows at the blend, no payouts.
    _set_retirement_lot(retire_alice_csv, "Asset_Roth_Bill,default,roth_ira,,,,,80000.00,",
                        "Asset_Roth_Bill,default,roth_ira,FND,,,,80000.00,")
    result = load_test_copy()
    rules = {name: (character, payer) for name, character, payer in _query(
        result.intent_path,
        "SELECT s.name, tax_character, a.name FROM scheduled_transaction s "
        "JOIN chain c ON c.id = s.from_account JOIN account a ON a.id = c.account_id")}
    ira_dividends = growth_rule_name("Asset_TraditionalIRA_Alice", "default", prefix="dividends_")
    roth_lt = growth_rule_name("Asset_Roth_Alice", "default", prefix="lt_distributions_")
    assert rules[ira_dividends] == ("deferred", "deferred_payouts")
    assert rules[roth_lt] == ("tax_free", "tax_free_payouts")
    bill = [n for n in rules if n.endswith("_roth_bill")]
    assert bill == [growth_rule_name("Asset_Roth_Bill", "default")]
    run = run_scenario(result.intent_path, runs_dir / "sheltered.sqlite3", run_by="TEST",
                       allow_dirty_data=True, allow_dirty_code=True)
    month, growth = _first_month(run, growth_rule_name("Asset_TraditionalIRA_Alice", "default"))
    payouts = sum(_first_month(run, growth_rule_name("Asset_TraditionalIRA_Alice", "default", prefix=p))[1]
                  for p in ("dividends_", "ordinary_dividends_", "lt_distributions_", "st_distributions_"))
    # $500,000 at the 6.4% blend, whatever part of it is paid out.
    assert growth + payouts == 266_666
    # The payouts buy shares at $100; appreciation buys none.
    assert _quantity(run, "Asset_TraditionalIRA_Alice", month) == 5000 * 1_000_000 + payouts * 100
    roth_payouts = sum(_first_month(run, growth_rule_name("Asset_Roth_Alice", "default", prefix=p))[1]
                       for p in ("dividends_", "ordinary_dividends_", "lt_distributions_", "st_distributions_"))
    assert _quantity(run, "Asset_Roth_Alice", month) == 1000 * 1_000_000 + roth_payouts * 100
    [(roth_basis,)] = _query(run, (
        "SELECT ls.cost_basis FROM lot_state ls JOIN simulation_step s ON s.id = ls.step_id "
        "JOIN account_lot m ON m.id = ls.lot_id JOIN account a ON a.id = m.account_id "
        "WHERE a.name = 'Asset_Roth_Alice' AND s.month = ?"), month)
    assert roth_basis == 5_000_000
    # The run treats them as growth, and so does the loader's label.
    labels = dict(_query(result.intent_path, (
        "SELECT name, json_extract(meta_json, '$.builtin') FROM account "
        "WHERE name IN ('deferred_payouts', 'tax_free_payouts')")))
    assert labels == {"deferred_payouts": "sheltered_payout", "tax_free_payouts": "sheltered_payout"}
    # Without shares the lots get no payouts, and nothing else changes:
    # the same balances, and the same taxable and unrealized gains, since a
    # sheltered payout is unrealized growth like the appreciation it replaces.
    _set_retirement_lot(retire_alice_csv, ",traditional_ira,FND,,5000,", ",traditional_ira,FND,,,")
    _set_retirement_lot(retire_alice_csv, ",roth_ira,FND,,1000,", ",roth_ira,FND,,,")
    dollars = run_scenario(load_test_copy().intent_path, runs_dir / "dollars.sqlite3", run_by="TEST",
                           allow_dirty_data=True, allow_dirty_code=True)
    final = "SELECT SUM(balance) FROM lot_state WHERE step_id = (SELECT MAX(id) FROM simulation_step)"
    assert _query(run, final) == _query(dollars, final)
    with RunConnection(run) as ran, RunConnection(dollars) as plain:
        pd.testing.assert_frame_equal(ran.gains_by_category(), plain.gains_by_category())


@pytest.mark.usefixtures("without_prototype_defaults", "blended_fund")
def test_a_series_ending_in_cg_with_a_yield_is_refused(
    test_data_dir: Path, write_series: Callable[[str, str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    """``bond_cg_yield`` would be both a series ``bond_cg``'s yield and
    ``bond``'s gain distributions (#547): a lot growing at ``bond_cg`` with
    that file is refused."""
    (test_data_dir / "allocations.csv").write_text(
        "symbol,as_of,asset_class,weight\nFND,,stock_us,100%\n")
    write_series("stock_us_cg", "2026-01-31,5%")
    write_series("stock_us_cg_yield", "2026-01-31,1%")
    accounts = test_data_dir / "scenarios" / "retire_alice" / "accounts.csv"
    accounts.write_text(accounts.read_text().replace(",brokerage_growth,", ",stock_us_cg,"))
    lots = test_data_dir / "scenarios" / "retire_alice" / "account_lots.csv"
    lots.write_text(lots.read_text().replace(",FND,", ",,"))
    with pytest.raises(LoadError, match=r"'stock_us_cg' ends in '_cg'"):
        load_test_copy()
