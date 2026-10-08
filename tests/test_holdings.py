"""Holdings: the lots in an account, the accounts holding a symbol, and their
command-line and notebook views (ADR-005 phase 4)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import matplotlib
import pandas as pd
import pytest
from click.testing import CliRunner

matplotlib.use("Agg")

from analysis.cli import main
from analysis.sim.connection import RunConnection, ScenarioConnection
from analysis.sim.holdings import (
    account_holdings,
    holding_accounts,
    current_month,
    holding_months,
    holding_symbols,
    holdings_totals,
    lots_frame,
    security_names,
    symbol_holders,
)
from analysis.sim.notebook_ui.holdings_widgets import (
    account_holdings_view,
    account_holdings_widget,
    symbol_holders_view,
    symbol_holders_widget,
)

BROKERAGE = "Asset_Brokerage_Joint"


def test_account_holdings_lists_each_lot_at_the_final_month(two_lot_run: RunConnection) -> None:
    final = holding_months(two_lot_run)[-1]
    frame = account_holdings(two_lot_run, BROKERAGE, month=final[:7])
    assert list(frame["lot"]) == ["a", "b"]
    assert list(frame["symbol"]) == ["AAA", "BBB"]
    assert frame["as_of"].nunique() == 1 and frame["as_of"].iloc[0] == final
    a = frame.set_index("lot").loc["a"]
    assert a["quantity"] > 1000.0  # the 2029 inheritance bought more shares
    assert a["price"] == pytest.approx(a["balance"] / a["quantity"])
    assert a["cost_basis"] == 110_000.0  # $60,000 plus the $50,000 inheritance
    assert a["unrealized_gain"] == pytest.approx(a["balance"] - 110_000.0)


def test_totals_add_lots_and_lose_basis_if_any_is_unknown() -> None:
    frame = pd.DataFrame({
        "quantity": [1.0, 2.0], "balance": [100.0, 50.0], "cost_basis": [60.0, 30.0],
    })
    assert holdings_totals(frame, quantity=True) == {
        "balance": 150.0, "cost_basis": 90.0, "unrealized_gain": 60.0, "quantity": 3.0,
    }
    frame.loc[1, "cost_basis"] = float("nan")
    totals = holdings_totals(frame)
    assert totals["balance"] == 150.0 and pd.isna(totals["cost_basis"]) and pd.isna(totals["unrealized_gain"])


def test_month_can_be_chosen_by_prefix_and_must_exist(two_lot_run: RunConnection) -> None:
    first = holding_months(two_lot_run)[0]
    assert account_holdings(two_lot_run, BROKERAGE, month=first[:7])["as_of"].iloc[0] == first
    with pytest.raises(ValueError, match="no month '1999-01'"):
        account_holdings(two_lot_run, BROKERAGE, month="1999-01")


def test_unknown_account_lists_the_accounts_that_have_lots(two_lot_run: RunConnection) -> None:
    with pytest.raises(KeyError, match="Asset_Checking"):
        account_holdings(two_lot_run, "Nope")
    with pytest.raises(KeyError, match="no asset or liability account"):
        account_holdings(two_lot_run, "Income_Salary_Alice")  # not a balance-sheet account


def test_symbol_holders_finds_the_lots_holding_a_symbol(two_lot_run: RunConnection) -> None:
    holders = symbol_holders(two_lot_run, "BBB")
    assert list(holders["account"]) == [BROKERAGE] and list(holders["lot"]) == ["b"]
    assert symbol_holders(two_lot_run, "ZZZ").empty


def test_holders_span_accounts(two_lot_run: RunConnection, test_data_dir: Path,
                               update_account_value) -> None:
    update_account_value("Asset_Roth_Alice", "default", symbol="BBB", quantity="10")
    with ScenarioConnection(test_data_dir, "retire_alice") as scenario:
        holders = symbol_holders(scenario, "BBB")
    assert sorted(zip(holders["account"], holders["lot"], strict=True)) == [
        (BROKERAGE, "b"), ("Asset_Roth_Alice", "default"),
    ]
    assert set(holders["as_of"]) == {"opening"}


def test_scenario_source_shows_opening_values_and_refuses_a_month(
    test_data_dir: Path, two_lot_run_file: Path,
) -> None:
    with ScenarioConnection(test_data_dir, "retire_alice") as scenario:
        frame = account_holdings(scenario, BROKERAGE)
        assert list(frame["balance"]) == [100_000.0, 200_000.0]
        assert holding_months(scenario) == []
        with pytest.raises(ValueError, match="needs a run"):
            lots_frame(scenario, month="2026-05")


def test_widgets_offer_every_account_and_the_symbols_of_investment_and_cash_lots(
    two_lot_run: RunConnection, test_data_dir: Path, update_account_value,
) -> None:
    assert holding_accounts(two_lot_run) == sorted([
        "Asset_Checking", "Asset_Home", BROKERAGE, "Asset_Roth_Alice", "Asset_Roth_Bill",
        "Asset_Savings", "Asset_TraditionalIRA_Alice", "Liability_Mortgage",
    ])
    assert holding_symbols(two_lot_run) == ["AAA", "BBB"]
    update_account_value("Asset_Checking", "", symbol="CASH")
    update_account_value("Asset_Home", "", symbol="HOUSE")  # neither investment nor cash
    with ScenarioConnection(test_data_dir, "retire_alice") as scenario:
        assert "CASH" in holding_symbols(scenario)
        assert "HOUSE" not in holding_symbols(scenario)
        assert not symbol_holders(scenario, "HOUSE").empty  # the data function stays general
        _, table = symbol_holders_view(scenario, "HOUSE")
        assert list(table.index)[:-1] == []  # the widget's table leaves it out
        _, cash = symbol_holders_view(scenario, "CASH")
        assert list(cash.index)[0] == ("Asset_Checking", "default")


def test_holdings_command_shows_lots_and_totals(two_lot_run_file: Path, test_data_dir: Path) -> None:
    result = CliRunner().invoke(main, [
        "sim", "account", "holdings", BROKERAGE, "--scenario", "retire_alice",
        "--data-dir", str(test_data_dir), "--runfile", str(two_lot_run_file),
    ], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "holdings of Asset_Brokerage_Joint" in result.output
    assert "AAA" in result.output and "BBB" in result.output and "Total" in result.output
    assert "Alpha Growth Fund" in result.output and "Beta Income Fund" in result.output


def test_holdings_command_defaults_to_the_latest_run_and_can_show_opening_values(
    two_lot_run_file: Path, test_data_dir: Path,
) -> None:
    runner = CliRunner()
    base = ["sim", "account", "holdings", BROKERAGE, "--scenario", "retire_alice",
            "--data-dir", str(test_data_dir)]
    latest = runner.invoke(main, base, env={"COLUMNS": "200"})
    assert latest.exit_code == 0 and "run " in latest.output, latest.output
    opening = runner.invoke(main, [*base, "--opening"], env={"COLUMNS": "200"})
    assert opening.exit_code == 0 and "opening values" in opening.output, opening.output
    assert "$300,000.00" in opening.output  # 100,000 + 200,000
    bad = runner.invoke(main, [*base, "--opening", "--month", "2026-05"])
    assert bad.exit_code != 0 and "cannot be combined" in bad.output


def test_holdings_command_errors_are_clear(two_lot_run_file: Path, test_data_dir: Path) -> None:
    runner = CliRunner()
    unknown = runner.invoke(main, [
        "sim", "account", "holdings", "Nope", "--scenario", "retire_alice",
        "--data-dir", str(test_data_dir), "--runfile", str(two_lot_run_file),
    ])
    assert unknown.exit_code != 0 and "no asset or liability account 'Nope'" in unknown.output
    bad_month = runner.invoke(main, [
        "sim", "account", "holdings", BROKERAGE, "--scenario", "retire_alice",
        "--data-dir", str(test_data_dir), "--runfile", str(two_lot_run_file),
        "--month", "1999-01",
    ])
    assert bad_month.exit_code != 0 and "no month '1999-01'" in bad_month.output


def test_holders_command_lists_accounts_and_reports_none(
    two_lot_run_file: Path, test_data_dir: Path,
) -> None:
    runner = CliRunner()
    base = ["--scenario", "retire_alice", "--data-dir", str(test_data_dir),
            "--runfile", str(two_lot_run_file)]
    found = runner.invoke(main, ["sim", "account", "holders", "AAA", *base], env={"COLUMNS": "200"})
    assert found.exit_code == 0, found.output
    assert "accounts holding AAA (Alpha Growth Fund)" in found.output
    assert BROKERAGE in found.output
    none = runner.invoke(main, ["sim", "account", "holders", "ZZZ", *base])
    assert none.exit_code == 0 and "no lot holds 'ZZZ'" in none.output


def test_account_holdings_view_has_a_heading_and_a_total_row(two_lot_run: RunConnection) -> None:
    heading, table = account_holdings_view(two_lot_run, BROKERAGE)
    assert heading.startswith("**Asset_Brokerage_Joint** as of ")
    assert list(table.index) == ["a", "b", "Total"]
    assert table.loc["Total", "balance"] == pytest.approx(table.loc[["a", "b"], "balance"].sum())
    assert "account" not in table.columns  # implied by the heading
    assert table.loc["Total", "symbol"] == "" and table.loc["Total", "acquired"] == ""


def test_symbol_holders_view_totals_quantity(two_lot_run: RunConnection) -> None:
    heading, table = symbol_holders_view(two_lot_run, "AAA", month=holding_months(two_lot_run)[-1][:7])
    assert heading.startswith("**AAA** (Alpha Growth Fund) as of ")
    assert table.index[0] == (BROKERAGE, "a")
    assert table.index[-1] == ("Total", "")  # a blank lot, not NaN
    assert table.iloc[-1]["quantity"] == table.iloc[0]["quantity"] > 1000.0


def test_account_holdings_widget_redraws_when_the_account_or_month_changes(
    two_lot_run: RunConnection,
) -> None:
    widget = account_holdings_widget(two_lot_run, BROKERAGE)
    selectors, _out = widget.children
    account_picker, month_picker = selectors.children
    assert account_picker.value == BROKERAGE and month_picker.value == current_month(holding_months(two_lot_run))
    account_picker.value = "Asset_Roth_Alice"  # must not raise
    month_picker.value = holding_months(two_lot_run)[0]
    assert (account_picker.value, month_picker.value) == (
        "Asset_Roth_Alice", holding_months(two_lot_run)[0],
    )


def test_symbol_holders_widget_redraws_when_the_symbol_changes(two_lot_run: RunConnection) -> None:
    widget = symbol_holders_widget(two_lot_run)
    selectors, _out = widget.children
    symbol_picker = selectors.children[0]
    assert list(symbol_picker.options) == [
        ("AAA - Alpha Growth Fund", "AAA"), ("BBB - Beta Income Fund", "BBB"),
    ]
    symbol_picker.value = "BBB"  # must not raise
    assert symbol_picker.value == "BBB"


def test_widgets_say_so_when_there_is_nothing_to_show(retire_alice_run: RunConnection) -> None:
    assert len(symbol_holders_widget(retire_alice_run).children) == 1  # no lot has a symbol


def test_holdings_carry_the_security_name_when_securities_csv_lists_it(
    two_lot_run: RunConnection, test_data_dir: Path, update_account_value,
) -> None:
    frame = account_holdings(two_lot_run, BROKERAGE).set_index("lot")
    assert frame.loc["a", "name"] == "Alpha Growth Fund"
    assert frame.loc["b", "name"] == "Beta Income Fund"
    assert security_names(two_lot_run) == {"AAA": "Alpha Growth Fund", "BBB": "Beta Income Fund"}

    update_account_value("Asset_Roth_Alice", "default", symbol="ZZZ", quantity="1")  # not listed
    with ScenarioConnection(test_data_dir, "retire_alice") as scenario:
        unnamed = symbol_holders(scenario, "ZZZ")
        assert unnamed["name"].isna().all()
        heading, _ = symbol_holders_view(scenario, "ZZZ")
        assert heading.startswith("**ZZZ** as of opening")
        picker = symbol_holders_widget(scenario).children[0].children[0]
        assert ("ZZZ", "ZZZ") in picker.options


def test_holdings_default_to_the_current_month_within_the_run() -> None:
    # #125: a run's last month is decades out and its opening values go stale.
    months = ["2026-05-31", "2026-06-30", "2026-07-31"]
    assert current_month(months, today=date(2026, 6, 15)) == "2026-06-30"
    assert current_month(months, today=date(2020, 1, 1)) == "2026-05-31"
    assert current_month(months, today=date(2040, 1, 1)) == "2026-07-31"


def test_account_holdings_without_a_month_shows_the_current_one(two_lot_run: RunConnection) -> None:
    frame = account_holdings(two_lot_run, BROKERAGE)
    assert frame["as_of"].iloc[0] == current_month(holding_months(two_lot_run))
