"""Result reports from the terminal (#187): ``sim account history`` and
``sim report income|expenses|gains|tax``, and the frames behind them."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim import reports
from analysis.sim.connection import RunConnection
from analysis.sim.types import DollarMode

BROKERAGE = "Asset_Brokerage_Joint"


def _invoke(*args: str, run_file: Path):
    """A report command on ``run_file`` alone: no data repo or scenario."""
    return CliRunner().invoke(main, ["sim", *args, "--runfile", str(run_file)],
                              env={"COLUMNS": "250"})


def test_income_by_category_keeps_other_and_adds_up(report_run: RunConnection) -> None:
    income = reports.flows_by_year(report_run, "income")
    assert list(income.columns) == [*reports.INCOME_REPORT_CATEGORIES, "total"]
    events = report_run.items("transfer_event", from_kind="income")
    by_year = events.groupby(pd.to_datetime(events["month"]).dt.year)["amount"].sum() / 100
    assert income["total"].to_numpy() == pytest.approx(by_year.reindex(income.index).to_numpy())
    # retire_alice's inheritance is other_income, which income_by_category drops.
    assert income.loc[2029, "other"] == pytest.approx(50_000)


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


def test_tax_by_year_leaves_after_tax_income(report_run: RunConnection) -> None:
    tax = reports.tax_by_year(report_run)
    liability = report_run.tax_liability_by_year()
    assert tax.loc[2026, "total_tax"] == pytest.approx(liability[2026]) and liability[2026] > 0
    assert (tax["after_tax_income"] == tax["gross_income"] - tax["total_tax"]).dropna().all()
    expenses = reports.flows_by_year(report_run, "expenses")
    assert tax.loc[2027, "non_tax_expenses"] == pytest.approx(
        expenses.loc[2027, "total"] - expenses.loc[2027, "taxes"])
    reconciliation = reports.tax_reconciliation(report_run)
    assert reconciliation.loc[2026, "federal_liability"] == pytest.approx(liability[2026])


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
    assert "after_tax_surplus" in run("report", "tax")
    assert "federal_refund" in run("report", "tax", "--reconciliation")
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

