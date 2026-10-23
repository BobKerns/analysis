"""Result reports from the terminal (#187): ``sim account history`` and
``sim report income|expenses|gains|tax``, and the frames behind them."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim import reports
from analysis.sim.connection import RunConnection, open_run, open_scenario, year_end_values
from analysis.sim.engine import run_scenario
from analysis.sim.loader import LoadResult
from analysis.sim.types import DollarMode

BROKERAGE = "Asset_Brokerage_Joint"


def _invoke(*args: str, run_file: Path):
    """A report command on ``run_file`` alone: no data repo or scenario."""
    return CliRunner().invoke(main, ["sim", *args, "--runfile", str(run_file)],
                              env={"COLUMNS": "250"})


def test_income_by_category_keeps_other_and_adds_up(report_run: RunConnection) -> None:
    income = reports.flows_by_year(report_run, "income")
    assert list(income.columns) == [
        *reports.INCOME_REPORT_CATEGORIES, reports.ECONOMIC_INCOME,
        "tax", reports.DEFERRED_TAX_CHANGE, "after_tax_income", "spending",
        reports.NET_WORTH_CHANGE,
        reports.CASH_INCOME, reports.CASH_FLOW]
    events = report_run.items("transfer_event", from_kind="income")
    by_year = events.groupby(pd.to_datetime(events["date"]).dt.year)["amount"].sum() / 100
    assert income[reports.ECONOMIC_INCOME].to_numpy() == pytest.approx(
        by_year.reindex(income.index).to_numpy())
    # retire_alice's inheritance is other_income, which income_by_category drops.
    assert income.loc[2029, "other"] == pytest.approx(50_000)
    # Its FY2025 loss carryforward is a year-to-date row dated 2025-12-31,
    # posted in the run's first step: it counts in 2025, in either dollars.
    assert income.loc[2025, "investment"] == pytest.approx(-1_500)
    constant = reports.flows_by_year(report_run, "income", mode=DollarMode.CPI_CONSTANT)
    assert constant.loc[2025, reports.ECONOMIC_INCOME] == pytest.approx(-1_500, rel=0.05)


def test_cash_income_leaves_out_growth_and_cash_flow_adds_withdrawals(
    two_lot_run: RunConnection,
) -> None:
    income = reports.flows_by_year(two_lot_run, "income")
    # Brokerage growth stays invested: in the total, not in cash income.
    growth = income[reports.ECONOMIC_INCOME] - income[reports.CASH_INCOME]
    assert (growth.loc[2026:] > 0).all()
    events = two_lot_run.items("transfer_event", from_kind="income", to_name="Asset_Checking")
    in_2027 = events[events["date"].str.startswith("2027")]["amount"].sum() / 100
    assert income.loc[2027, reports.CASH_INCOME] >= in_2027 > 0
    # lot_b_draw moves $1,000 a month from the brokerage into checking.
    assert income.loc[2027, reports.CASH_FLOW] - income.loc[2027, reports.CASH_INCOME] \
        == pytest.approx(12_000)
    expenses = reports.flows_by_year(two_lot_run, "expenses")
    assert reports.CASH_INCOME not in expenses.columns
    year = reports.flows_for_year(two_lot_run, "income", 2027)
    assert reports.CASH_FLOW not in year.index


def test_expenses_by_tag_prefer_the_rules_tag_to_its_accounts(report_run: RunConnection) -> None:
    expenses = reports.flows_by_year(report_run, "expenses", by="tag:budget", untagged=True)
    assert list(expenses.columns)[-2:] == [reports.UNTAGGED, "total"]
    assert {"fun", "core", "home"} <= set(expenses.columns)
    by_category = reports.flows_by_year(report_run, "expenses")
    # The living rule's own tag wins over Expense_Living's, so core is healthcare alone.
    assert expenses["fun"].to_numpy() == pytest.approx(by_category["living"].to_numpy())
    assert expenses["core"].to_numpy() == pytest.approx(by_category["healthcare"].to_numpy())
    assert expenses["total"].to_numpy() == pytest.approx(by_category["total"].to_numpy())


def test_by_tag_leaves_untagged_flows_out_of_the_total_unless_asked(
    report_run: RunConnection,
) -> None:
    kept = reports.flows_by_year(report_run, "expenses", by="tag:budget", untagged=True)
    assert (kept[reports.UNTAGGED] > 0).any()
    expenses = reports.flows_by_year(report_run, "expenses", by="tag:budget")
    assert reports.UNTAGGED not in expenses.columns
    assert expenses["total"].to_numpy() == pytest.approx(
        (kept["total"] - kept[reports.UNTAGGED]).to_numpy())
    year = reports.flows_for_year(report_run, "expenses", 2030, by="tag:budget")
    assert reports.UNTAGGED not in year.index
    assert year.loc["total", "share"] == pytest.approx(1.0)


def test_income_by_tag_counts_only_tagged_cash_income_and_drops_cash_flow(
    report_run: RunConnection,
) -> None:
    kept = reports.flows_by_year(report_run, "income", by="tag:source", untagged=True)
    assert {reports.UNTAGGED, reports.CASH_INCOME, reports.CASH_FLOW} <= set(kept.columns)
    income = reports.flows_by_year(report_run, "income", by="tag:source")
    assert list(income.columns) == ["work", "total", reports.CASH_INCOME]
    assert income["total"].to_numpy() == pytest.approx(income["work"].to_numpy())
    # Only Alice's paychecks count as cash income now, not Social Security or interest.
    paid = income.loc[income["work"] > 0]
    assert (paid[reports.CASH_INCOME] > 0).all()
    assert (paid[reports.CASH_INCOME] <= paid["work"] + 0.005).all()
    assert (paid[reports.CASH_INCOME] < kept.loc[paid.index, reports.CASH_INCOME]).all()


def test_one_year_lists_only_the_groups_with_money_largest_first(
    report_run: RunConnection,
) -> None:
    year = reports.flows_for_year(report_run, "income", 2030, by="account")
    amounts = year["amount"].drop("total")
    assert (amounts > 0).all() and list(amounts) == sorted(amounts, reverse=True)
    assert year.loc["total", "amount"] == pytest.approx(amounts.sum())
    assert year.loc["total", "share"] == pytest.approx(1.0)
    categories = reports.flows_for_year(report_run, "income", 2030)
    assert list(categories.index) == [*reports.INCOME_REPORT_CATEGORIES, "total"]
    with pytest.raises(KeyError, match="no income in 1999"):
        reports.flows_for_year(report_run, "income", 1999)


def test_cpi_constant_flows_convert_each_month(report_run: RunConnection) -> None:
    nominal = reports.flows_by_year(report_run, "expenses")
    constant = reports.flows_by_year(report_run, "expenses", mode=DollarMode.CPI_CONSTANT)
    final = nominal.index[-1]
    assert constant.loc[final, "total"] < nominal.loc[final, "total"]


def test_gains_count_a_prior_years_catch_up_in_that_year(report_run: RunConnection) -> None:
    gains = reports.gains_by_year(report_run)
    assert gains.loc[2025, "unrealized"] == pytest.approx(-1_500)  # it never reaches cash
    constant = reports.gains_by_year(report_run, DollarMode.CPI_CONSTANT)
    assert constant.loc[2025].notna().all()
    # Expenses count by date too, so each year's coverage matches the expense report.
    expenses = reports.flows_by_year(report_run, "expenses")["total"]
    assert gains["expenses"].to_numpy() == pytest.approx(
        expenses.reindex(gains.index).fillna(0).to_numpy())


def test_tax_by_year_shows_the_federal_income_it_taxed_without_a_state_rule(report_run: RunConnection) -> None:
    tax = reports.tax_by_year(report_run)
    # No state tax rule, so no state columns.
    assert list(tax.columns) == ["fed_agi", "fed_deduction", "fed_income",
                                 "fed_tax", "total_tax", "effective_rate"]
    liability = report_run.tax_liability_by_year()
    assert tax.loc[2026, "total_tax"] == pytest.approx(liability[2026]) and liability[2026] > 0
    summary = report_run.items("tax_year_summary").set_index(["year", "bucket"])["amount"] / 100
    assert tax.loc[2026, "fed_agi"] == pytest.approx(summary[2026, "federal_adjusted_gross_income"])
    assert tax.loc[2026, "fed_income"] == pytest.approx(
        max(0, tax.loc[2026, "fed_agi"] - tax.loc[2026, "fed_deduction"]))
    assert tax.loc[2026, "effective_rate"] == pytest.approx(
        tax.loc[2026, "total_tax"] / tax.loc[2026, "fed_agi"])
    # The rate is a ratio, so CPI-constant dollars leave it alone.
    constant = reports.tax_by_year(report_run, DollarMode.CPI_CONSTANT)
    assert constant["effective_rate"].to_numpy() == pytest.approx(
        tax["effective_rate"].to_numpy(), nan_ok=True)
    assert constant.loc[2030, "fed_agi"] < tax.loc[2030, "fed_agi"]
    reconciliation = reports.tax_reconciliation(report_run)
    assert reconciliation.loc[2026, "fed_liability"] == pytest.approx(liability[2026])


def test_tax_by_year_adds_the_states_own_figures(
    add_state_tax, load_test_copy, runs_dir: Path, test_data_dir: Path,
) -> None:
    add_state_tax()
    run_file = run_scenario(load_test_copy().intent_path, runs_dir / "state.sqlite3",
                            run_by="TEST", allow_dirty_data=True, allow_dirty_code=True)
    with open_scenario(test_data_dir, "retire_alice") as scenario_conn, \
            open_run(run_file, scenario_conn, scenarios_dir=test_data_dir / "scenarios") as rc:
        tax = reports.tax_by_year(rc)
    assert {"state_agi", "state_deduction", "state_income", "state_tax"} <= set(tax)
    assert tax.loc[2026, "state_deduction"] == pytest.approx(11_080)
    assert tax.loc[2026, "total_tax"] == pytest.approx(tax.loc[2026, "state_tax"])


def test_a_market_drop_takes_back_that_years_gains(
    write_series, load_test_copy, runs_dir: Path, test_data_dir: Path,
) -> None:
    # A negative rate posts brokerage -> capital_gains: it counts negative (#248).
    write_series("brokerage_growth", "2026-01-31,6%\n2028-01-31,-20%\n2029-01-31,6%")
    run_file = run_scenario(load_test_copy().intent_path, runs_dir / "drop.sqlite3",
                            run_by="TEST", allow_dirty_data=True, allow_dirty_code=True)
    with open_scenario(test_data_dir, "retire_alice") as scenario_conn, \
            open_run(run_file, scenario_conn, scenarios_dir=test_data_dir / "scenarios") as rc:
        income = reports.flows_by_year(rc, "income", by="account")
        history = reports.account_history(rc, "capital_gains", yearly=True)
    assert income.loc[2028, "capital_gains"] < 0
    assert income.loc[2027, "capital_gains"] > 0
    # The growth account's balance is its running total, so its change is the flow.
    assert history["change"].to_numpy() == pytest.approx(
        income["capital_gains"].reindex(history.index).to_numpy())


def _story(income: pd.DataFrame) -> pd.DataFrame:
    """The income report's economic income and net worth columns, for the
    years net worth covers."""
    columns = [reports.ECONOMIC_INCOME,
               *(c for c in reports.NET_WORTH_COLUMNS if c in income.columns)]
    return income[columns].dropna(subset=[reports.NET_WORTH_CHANGE])


def _assert_rows_add_up(income: pd.DataFrame) -> pd.DataFrame:
    story = _story(income)
    assert not story.empty
    taken = story.drop(columns=[reports.ECONOMIC_INCOME, "after_tax_income",
                                reports.NET_WORTH_CHANGE], errors="ignore").sum(axis=1)
    assert story[reports.NET_WORTH_CHANGE].to_numpy() == pytest.approx(
        (story[reports.ECONOMIC_INCOME] - taken).to_numpy(), abs=0.01)
    if "after_tax_income" in story.columns:
        assert story["after_tax_income"].to_numpy() == pytest.approx(
            (story[reports.ECONOMIC_INCOME] - story["tax"]
             - story[reports.DEFERRED_TAX_CHANGE]).to_numpy(), abs=0.01)
    return story


def _year_end_changes(frame: pd.DataFrame) -> pd.Series:
    year_end = year_end_values(frame["nominal"]).astype(float)
    return year_end.diff().dropna()


def test_income_carries_economic_income_down_to_after_tax_net_worth(
    report_run: RunConnection,
) -> None:
    """#249: economic income less this year's tax, the change in tax owed
    later, and spending is the change in after-tax net worth, to the cent."""
    story = _assert_rows_add_up(reports.flows_by_year(report_run, "income"))
    # 2025 holds only a carried-forward record from before the scenario.
    assert story.index[0] == 2026
    later = _year_end_changes(report_run.after_tax_net_worth_frame())
    assert story.loc[later.index, reports.NET_WORTH_CHANGE].to_numpy() == pytest.approx(
        later.to_numpy(), abs=0.01)
    liability = report_run.tax_liability_by_year()
    computed = liability.index.intersection(story.index)
    assert story.loc[computed, "tax"].to_numpy() == pytest.approx(
        liability[computed].to_numpy())
    assert (story[reports.DEFERRED_TAX_CHANGE] != 0).any()
    expenses = reports.flows_by_year(report_run, "expenses")
    assert story["spending"].to_numpy() == pytest.approx(
        (expenses["total"] - expenses["taxes"]).reindex(story.index).to_numpy())


def test_pre_tax_income_carries_down_through_the_tax_paid(report_run: RunConnection) -> None:
    income = reports.flows_by_year(report_run, "income", after_tax=False)
    assert "tax" not in income.columns and reports.DEFERRED_TAX_CHANGE not in income.columns
    story = _assert_rows_add_up(income)
    later = _year_end_changes(report_run.net_worth_frame())
    assert story.loc[later.index, reports.NET_WORTH_CHANGE].to_numpy() == pytest.approx(
        later.to_numpy(), abs=0.01)
    taxes = reports.flows_by_year(report_run, "expenses")["taxes"]
    assert story["tax_paid"].to_numpy() == pytest.approx(taxes.reindex(story.index).to_numpy())


def test_cpi_constant_income_shows_what_inflation_took(report_run: RunConnection) -> None:
    for after_tax in (True, False):
        income = reports.flows_by_year(report_run, "income", mode=DollarMode.CPI_CONSTANT,
                                       after_tax=after_tax)
        story = _assert_rows_add_up(income)
        # Net worth is positive throughout, so inflation takes from it every year.
        assert (story["inflation"] > 0).all()
    assert "inflation" not in reports.flows_by_year(report_run, "income").columns


def test_income_without_a_tax_rule_counts_the_tax_paid(retire_alice_run: RunConnection) -> None:
    """retire_alice computes no tax: what its year-to-date withholding paid is
    the year's tax, so the row still adds up."""
    story = _assert_rows_add_up(reports.flows_by_year(retire_alice_run, "income"))
    taxes = reports.flows_by_year(retire_alice_run, "expenses")["taxes"]
    assert story["tax"].to_numpy() == pytest.approx(taxes.reindex(story.index).to_numpy())
    assert (story[reports.DEFERRED_TAX_CHANGE] == 0).all()


def test_depreciation_is_an_investment_loss_not_spending(
    depreciating_car: str, load_test_copy: Callable[[], LoadResult], runs_dir: Path,
    test_data_dir: Path,
) -> None:
    run = run_scenario(load_test_copy().intent_path, runs_dir / "car.sqlite3",
                       run_by="TEST", allow_dirty_data=True, allow_dirty_code=True)
    with open_scenario(test_data_dir, "retire_alice") as scenario_conn, \
            open_run(run, scenario_conn, scenarios_dir=test_data_dir / "scenarios") as rc:
        income = reports.flows_by_year(rc, "income")
        _assert_rows_add_up(income)
        by_account = reports.flows_by_year(rc, "income", by="account")
        lost = rc.items("transfer_event", to_name="depreciation")
        expenses = reports.flows_by_year(rc, "expenses")
    by_year = lost.groupby(pd.to_datetime(lost["date"]).dt.year)["amount"].sum() / 100
    assert (by_year > 0).all()
    # Counted against economic income like a market drop, not as spending.
    assert by_account["depreciation"].to_numpy() == pytest.approx(
        -by_year.reindex(by_account.index).fillna(0).to_numpy())
    assert "depreciation" not in expenses.columns
    assert "depreciation" not in income.columns


def test_business_income_is_net_of_business_expenses(
    business_scenarios: Path, load_test_scenario: Callable[[str], Path], runs_dir: Path,
    test_data_dir: Path,
) -> None:
    """A business's costs come off its income, not out of spending (#249):
    the business column is the year's Schedule C net, a loss when negative."""
    run = run_scenario(load_test_scenario("alice_business"), runs_dir / "business.sqlite3",
                       run_by="TEST", allow_dirty_data=True, allow_dirty_code=True)
    with open_scenario(test_data_dir, "alice_business") as scenario_conn, \
            open_run(run, scenario_conn, scenarios_dir=business_scenarios) as rc:
        income = reports.flows_by_year(rc, "income", mode=DollarMode.NOMINAL)
        story = _assert_rows_add_up(income)
        net = reports.tax_by_year(rc)["business_net"]
        expenses = reports.flows_by_year(rc, "expenses")
    assert (net < 0).any()
    assert income.loc[net.index, "business"].to_numpy() == pytest.approx(net.to_numpy())
    assert story["spending"].to_numpy() == pytest.approx(
        (expenses["total"] - expenses["taxes"] - expenses["business"])
        .reindex(story.index).to_numpy())


def test_income_by_tag_leaves_the_net_worth_columns_out_unless_untagged(
    report_run: RunConnection,
) -> None:
    tagged = reports.flows_by_year(report_run, "income", by="tag:source")
    assert "total" in tagged.columns
    assert not set(reports.NET_WORTH_COLUMNS) & set(tagged.columns)
    whole = reports.flows_by_year(report_run, "income", by="tag:source", untagged=True)
    by_category = reports.flows_by_year(report_run, "income")
    assert whole[reports.NET_WORTH_CHANGE].to_numpy() == pytest.approx(
        by_category[reports.NET_WORTH_CHANGE].to_numpy(), nan_ok=True)
    year = reports.flows_for_year(report_run, "income", 2030)
    assert not set(reports.NET_WORTH_COLUMNS) & set(year.index)


def test_an_empty_scenario_has_no_net_worth_change(
    empty_scenario_runs: tuple[RunConnection, RunConnection],
) -> None:
    _, empty = empty_scenario_runs
    income = reports.flows_by_year(empty, "income")
    assert income.empty or income[reports.NET_WORTH_CHANGE].isna().all()


def test_lot_history_adds_basis_and_quantity(two_lot_run: RunConnection) -> None:
    lot = reports.account_history(two_lot_run, BROKERAGE, lot="b", yearly=True)
    assert list(lot.columns) == ["balance", "change", "cost_basis", "unrealized", "quantity"]
    assert (lot["unrealized"] == lot["balance"] - lot["cost_basis"]).all()
    account = reports.account_history(two_lot_run, BROKERAGE, start="2027-01-31",
                                      end="2027-03-31")
    assert list(account.columns) == ["balance", "change"]
    assert list(account.index) == ["2027-01", "2027-02", "2027-03"]
    with pytest.raises(KeyError, match="unknown lot 'z'.*lots: a, b"):
        reports.account_history(two_lot_run, BROKERAGE, lot="z")
    with pytest.raises(KeyError, match="unknown account"):
        reports.account_history(two_lot_run, "Nope")


def test_income_account_history_changes_are_the_flows(two_lot_run: RunConnection) -> None:
    history = reports.account_history(two_lot_run, "Income_Salary_Bill", yearly=True)
    income = reports.flows_by_year(two_lot_run, "income", by="account")
    assert history["change"].to_numpy() == pytest.approx(
        income["Income_Salary_Bill"].reindex(history.index).to_numpy())


def test_report_commands_print_their_tables(report_run_file: Path) -> None:
    def run(*args: str):
        result = _invoke(*args, run_file=report_run_file)
        assert result.exit_code == 0, result.output
        return result.output

    income = run("report", "income")
    assert "income by category, CPI-constant dollars" in income
    assert "economic_income" in income and reports.NET_WORTH_CHANGE in income
    assert "soc security" in income
    # retire_alice's copy runs no business: an all-zero column is left out.
    assert "business" not in run("report", "expenses")
    assert "inflation" in income and "tax_paid" not in income
    pre_tax = run("report", "income", "--pre-tax", "--dollars", "nominal")
    assert "tax_paid" in pre_tax and reports.DEFERRED_TAX_CHANGE not in pre_tax
    assert "inflation" not in pre_tax
    assert "report.sqlite3" in income
    by_budget = run("report", "expenses", "--year", "2030", "--by", "tag:budget")
    assert "expenses in 2030 by budget" in by_budget and reports.UNTAGGED not in by_budget
    assert reports.UNTAGGED in run("report", "expenses", "--year", "2030",
                                   "--by", "tag:budget", "--untagged")
    assert "no income is tagged in role budget" in run("report", "income", "--by", "tag:budget")
    assert "no income in 2030 is tagged in role budget" in run(
        "report", "income", "--year", "2030", "--by", "tag:budget")
    assert "gains, nominal dollars" in run("report", "gains", "--dollars", "nominal")
    tax = run("report", "tax")
    assert "tax and taxable income" in tax and "fed_income" in tax
    assert "gross_income" not in tax and "%" in tax
    assert "fed_refund" in run("report", "tax", "--reconciliation")
    history = run("account", "history", "Liability_Mortgage", "--yearly", "--dollars", "nominal")
    assert "Liability_Mortgage, nominal dollars" in history and "-$" in history


def test_report_command_errors_are_clear(report_run_file: Path) -> None:
    def fails(*args: str) -> str:
        result = _invoke(*args, run_file=report_run_file)
        assert result.exit_code != 0
        return result.output

    assert "--by must be" in fails("report", "income", "--by", "tag:")
    assert "--untagged needs --by tag:ROLE" in fails("report", "expenses", "--untagged")
    assert "no expenses in 1999" in fails("report", "expenses", "--year", "1999")
    assert "unknown account in run: 'Nope'" in fails("account", "history", "Nope")
    assert "cannot be combined with --runfile" in fails("report", "gains", "--scenario", "x")
    assert "--end must be on or after --start" in fails(
        "account", "history", BROKERAGE, "--start", "2028-01", "--end", "2027-01")


def test_tax_report_says_when_no_tax_was_computed(two_lot_run_file: Path) -> None:
    result = _invoke("report", "tax", run_file=two_lot_run_file)
    assert result.exit_code == 0, result.output
    assert "no tax was computed" in result.output

