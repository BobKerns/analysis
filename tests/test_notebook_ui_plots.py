"""Tests for analysis.sim.notebook_ui.networth_plots and .accounts_plots."""

from __future__ import annotations

import shutil
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

from analysis.sim.accounts_plot import render_balance_history_figure
from analysis.sim.connection import RunConnection, open_run
from analysis.sim.engine import run_scenario
from analysis.sim.loader import load_scenario
from analysis.sim.notebook_ui.accounts_plots import account_history_widget
from analysis.sim.notebook_ui.networth_plots import (
    after_tax_widget,
    expenses_widget,
    gains_widget,
    income_widget,
    net_worth_widget,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = REPO_ROOT / "prototype_data"


def _build_run(tmp_path: Path, scenario: str = "retire_alice") -> Path:
    dest = tmp_path / "data"
    shutil.copytree(PROTOTYPE, dest)
    intent_path = load_scenario(
        dest, scenario,
        intent_dir=tmp_path / "intent", loaded_by="test",
        allow_dirty_data=True,
    ).intent_path
    return run_scenario(
        intent_path, tmp_path / "runs" / "r.sqlite3",
        run_by="test", data_mode="prototype", allow_dirty_data=True,
    )


def test_net_worth_widget_builds_without_error(tmp_path: Path) -> None:
    with open_run(_build_run(tmp_path)) as rc:
        widget = net_worth_widget(rc)
        assert len(widget.children) == 2


def test_income_widget_toggles_dollar_basis(tmp_path: Path) -> None:
    with open_run(_build_run(tmp_path)) as rc:
        widget = income_widget(rc)
        plot_mode, chart_out, table_out = widget.children
        assert plot_mode.value == "nominal"
        plot_mode.value = "cpi_constant"  # must not raise
        assert plot_mode.value == "cpi_constant"


def test_expenses_widget_includes_caveat(tmp_path: Path) -> None:
    with open_run(_build_run(tmp_path)) as rc:
        widget = expenses_widget(rc)
        assert len(widget.children) == 4


def test_gains_widget_builds_without_error(tmp_path: Path) -> None:
    with open_run(_build_run(tmp_path)) as rc:
        widget = gains_widget(rc)
        plot_mode, chart_out, table_out = widget.children
        plot_mode.value = "nominal"


def test_after_tax_widget_builds_without_error(tmp_path: Path) -> None:
    with open_run(_build_run(tmp_path)) as rc:
        widget = after_tax_widget(rc)
        plot_mode, chart_out, table_out = widget.children
        plot_mode.value = "nominal"


def test_account_history_widget_defaults_to_kind(tmp_path: Path) -> None:
    with open_run(_build_run(tmp_path)) as rc:
        assert isinstance(rc, RunConnection)
        widget = account_history_widget(rc, "asset", "Asset balances")
        picker = widget.children[1]
        assert all(rc.account_kinds()[name] == "asset" for name in picker.options)


def test_balance_history_adds_total_for_multiple_accounts() -> None:
    import pandas as pd

    index = pd.to_datetime(["2026-01-31", "2026-02-28"])
    balances = pd.DataFrame({"Checking": [100, 120], "Savings": [50, 80]}, index=index)

    fig = render_balance_history_figure(balances, ["Checking", "Savings"], "Assets")

    assert fig is not None
    assert [line.get_label() for line in fig.axes[0].lines] == [
        "Checking", "Savings", "Total",
    ]
    assert list(fig.axes[0].lines[-1].get_ydata()) == [150, 200]


def test_cpi_constant_expense_balances_preserve_zero_monthly_flow(
    tmp_path: Path, monkeypatch
) -> None:
    with open_run(_build_run(tmp_path)) as rc:
        nominal = rc.balance_pivot("expense")
        stopped = nominal.copy()
        stopped.loc[stopped.index >= "2027-01-31", "Expense_Living"] = (
            stopped.loc["2026-12-31", "Expense_Living"]
        )

        monkeypatch.setattr(rc, "balance_pivot", lambda kind: stopped)
        converted = rc.cpi_balance_pivot("expense")

        assert converted.loc["2027-01-31":, "Expense_Living"].diff().iloc[1:].eq(0).all()
