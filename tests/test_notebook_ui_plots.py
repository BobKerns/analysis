"""Tests for analysis.sim.notebook_ui.networth_plots and .accounts_plots."""

from __future__ import annotations

from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")

from analysis.sim.types import DollarMode
from analysis.sim.notebook_ui.accounts_plots import render_balance_history_figure
from analysis.sim.connection import RunConnection, open_run
from analysis.sim.notebook_ui.accounts_plots import account_history_widget
from analysis.sim.notebook_ui.pickers import AccountPicker
from analysis.sim.notebook_ui.networth_plots import (
    after_tax_widget,
    expenses_widget,
    gains_widget,
    income_widget,
    net_worth_widget,
)


def test_net_worth_widget_builds_without_error(retire_alice_run: RunConnection) -> None:
    widget = net_worth_widget(retire_alice_run)
    assert len(widget.children) == 2


def test_income_widget_toggles_dollar_basis(retire_alice_run_file: Path,
                                            data_dir: Path,
                                            scenarios_dir: Path) -> None:
    with open_run(retire_alice_run_file, "retire_alice",
                  data_dir=data_dir,
                  scenarios_dir=scenarios_dir) as rc:
        widget = income_widget(rc)
        dollar_mode, chart_out, table_out = widget.children
        assert dollar_mode.value == DollarMode.NOMINAL
        dollar_mode.value = DollarMode.CPI_CONSTANT  # must not raise
        assert dollar_mode.value == DollarMode.CPI_CONSTANT


def test_expenses_widget_includes_caveat(retire_alice_run_file: Path,
                                         data_dir: Path,
                                         scenarios_dir: Path) -> None:
    with open_run(retire_alice_run_file, "retire_alice",
                  data_dir=data_dir,
                  scenarios_dir=scenarios_dir) as rc:
        widget = expenses_widget(rc)
        assert len(widget.children) == 4


def test_gains_widget_builds_without_error(retire_alice_run_file: Path,
                                           data_dir: Path,
                                           scenarios_dir: Path) -> None:
    with open_run(retire_alice_run_file, "retire_alice",
                  data_dir=data_dir,
                  scenarios_dir=scenarios_dir) as rc:
        widget = gains_widget(rc)
        dollar_mode, chart_out, table_out = widget.children
        dollar_mode.value = DollarMode.NOMINAL


def test_after_tax_widget_builds_without_error(retire_alice_run_file: Path,
                                               data_dir: Path,
                                               scenarios_dir: Path) -> None:
    with open_run(retire_alice_run_file, "retire_alice",
                  data_dir=data_dir,
                  scenarios_dir=scenarios_dir) as rc:
        widget = after_tax_widget(rc)
        dollar_mode, chart_out, table_out = widget.children
        dollar_mode.value = DollarMode.NOMINAL


def test_account_history_widget_defaults_to_kind(retire_alice_run_file: Path,
                                                 data_dir: Path,
                                                 scenarios_dir: Path) -> None:
    with open_run(retire_alice_run_file, "retire_alice",
                  data_dir=data_dir,
                  scenarios_dir=scenarios_dir) as rc:
        assert isinstance(rc, RunConnection)
        widget = account_history_widget(rc, "asset", "Asset balances")
        picker = widget.children[1]
        assert picker.kind_picker.value == "asset"
        assert all(rc.account_kinds()[name] == "asset" for name in picker.account_picker.options)


def test_account_picker_can_be_displayed(retire_alice_run_file: Path,
                                         data_dir: Path,
                                         scenarios_dir: Path) -> None:
    with open_run(retire_alice_run_file, "retire_alice",
                  data_dir=data_dir,
                  scenarios_dir=scenarios_dir) as rc:
        picker = AccountPicker(rc, ("Asset_Checking",))

        assert picker._model_id
        assert picker.accounts == ["Asset_Checking"]


def test_empty_account_picker_keeps_a_real_select_multiple(
    retire_alice_run_file: Path, data_dir: Path, scenarios_dir: Path,
) -> None:
    with open_run(retire_alice_run_file, "retire_alice",
                  data_dir=data_dir,
                  scenarios_dir=scenarios_dir) as rc:
        picker = AccountPicker(rc, (), kind="asset")

        assert picker.account_picker.options == ()
        assert picker.account_picker.value == ()
        picker.kind_picker.value = "liability"
        assert picker.account_picker.options


def test_account_history_widget_kind_switch_repopulates_accounts(retire_alice_run_file: Path,
                                                                 data_dir: Path,
                                                                 scenarios_dir: Path) -> None:
    with open_run(retire_alice_run_file, "retire_alice",
                  data_dir=data_dir,
                  scenarios_dir=scenarios_dir) as rc:
        widget = account_history_widget(rc, "asset", "Balances")
        picker = widget.children[1]
        picker.kind_picker.value = "expense"
        assert all(rc.account_kinds()[name] == "expense" for name in picker.account_picker.options)


def test_balance_history_adds_total_for_multiple_accounts() -> None:
    import pandas as pd

    index = pd.to_datetime(["2026-01-31", "2026-02-28"])
    balances = pd.DataFrame({"Checking": [100, 120], "Savings": [50, 80]}, index=index)

    fig = render_balance_history_figure(balances, ["Checking", "Savings"], "Assets")

    assert fig is not None
    assert [line.get_label() for line in fig.axes[0].lines] == [
        "Checking", "Savings", "Total",
    ]
    assert np.asarray(fig.axes[0].lines[-1].get_ydata()).tolist() == [150, 200]


def test_cpi_constant_expense_balances_preserve_zero_monthly_flow(
    monkeypatch,
    retire_alice_run_file: Path,
    data_dir: Path,
    scenarios_dir: Path
) -> None:
    with open_run(retire_alice_run_file, "retire_alice",
                  data_dir=data_dir,
                  scenarios_dir=scenarios_dir) as rc:
        nominal = rc.balance_pivot("expense")
        stopped = nominal.copy()
        stopped.loc[stopped.index >= "2027-01-31", "Expense_Living"] = (
            stopped.loc["2026-12-31", "Expense_Living"]
        )

        monkeypatch.setattr(rc, "balance_pivot", lambda kind: stopped)
        converted = rc.cpi_balance_pivot("expense")

        assert converted.loc["2027-01-31":, "Expense_Living"].diff().iloc[1:].eq(0).all()
