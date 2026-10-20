"""Result reports from the terminal (#187): ``sim account history`` and
``sim report income|expenses|gains|tax``, and the frames behind them."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim import reports
from analysis.sim.connection import RunConnection, open_run, open_scenario
from analysis.sim.engine import run_scenario
from analysis.sim.types import DollarMode

BROKERAGE = "Asset_Brokerage_Joint"


def _invoke(*args: str, run_file: Path):
    """A report command on ``run_file`` alone: no data repo or scenario."""
    return CliRunner().invoke(main, ["sim", *args, "--runfile", str(run_file)],
                              env={"COLUMNS": "250"})


def test_income_by_category_keeps_other_and_adds_up(report_run: RunConnection) -> None:
    income = reports.flows_by_year(report_run, "income")
    assert list(income.columns) == [*reports.INCOME_REPORT_CATEGORIES, "total",
                                    reports.CASH_INCOME, reports.CASH_FLOW]
    events = report_run.items("transfer_event", from_kind="income")
    by_year = events.groupby(pd.to_datetime(events["date"]).dt.year)["amount"].sum() / 100
    assert income["total"].to_numpy() == pytest.approx(by_year.reindex(income.index).to_numpy())
    # retire_alice's inheritance is other_income, which income_by_category drops.
    assert income.loc[2029, "other"] == pytest.approx(50_000)
    # Its FY2025 loss carryforward is a year-to-date row dated 2025-12-31,
    # posted in the run's first step: it counts in 2025, in either dollars.
    assert income.loc[2025, "investment"] == pytest.approx(-1_500)
    constant = reports.flows_by_year(report_run, "income", mode=DollarMode.CPI_CONSTANT)
    assert constant.loc[2025, "total"] == pytest.approx(-1_500, rel=0.05)


def test_cash_income_leaves_out_growth_and_cash_flow_adds_withdrawals(
    two_lot_run: RunConnection,
) -> None:
    income = reports.flows_by_year(two_lot_run, "income")
    # Brokerage growth stays invested: in the total, not in cash income.
    growth = income["total"] - income[reports.CASH_INCOME]
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
    expenses = reports.flows_by_year(report_run, "expenses", by="tag:budget")
    assert list(expenses.columns)[-2:] == [reports.UNTAGGED, "total"]
    assert {"fun", "core", "home"} <= set(expenses.columns)
    by_category = reports.flows_by_year(report_run, "expenses")
    # The living rule's own tag wins over Expense_Living's, so core is healthcare alone.
    assert expenses["fun"].to_numpy() == pytest.approx(by_category["living"].to_numpy())
    assert expenses["core"].to_numpy() == pytest.approx(by_category["healthcare"].to_numpy())
    assert expenses["total"].to_numpy() == pytest.approx(by_category["total"].to_numpy())


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
    assert "income by category, CPI-constant dollars" in income and "social security" in income
    assert "report.sqlite3" in income
    assert "expenses in 2030 by budget" in run("report", "expenses", "--year", "2030",
                                               "--by", "tag:budget")
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
    assert "no expenses in 1999" in fails("report", "expenses", "--year", "1999")
    assert "unknown account in run: 'Nope'" in fails("account", "history", "Nope")
    assert "cannot be combined with --runfile" in fails("report", "gains", "--scenario", "x")
    assert "--end must be on or after --start" in fails(
        "account", "history", BROKERAGE, "--start", "2028-01", "--end", "2027-01")


def test_tax_report_says_when_no_tax_was_computed(two_lot_run_file: Path) -> None:
    result = _invoke("report", "tax", run_file=two_lot_run_file)
    assert result.exit_code == 0, result.output
    assert "no tax was computed" in result.output

