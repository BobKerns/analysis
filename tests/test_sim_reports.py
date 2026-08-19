"""Tests for analysis.sim.reports."""

from __future__ import annotations

from pathlib import Path

from analysis.sim.connection import open_run
from analysis.sim.engine import run_scenario
from analysis.sim.loader import load_scenario
from analysis.sim.reports import (
    account_balances,
    expenses_by_category,
    income_by_category,
    net_income,
    net_worth_series,
    year_end_net_worth,
    yearly_totals,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = REPO_ROOT / "prototype_data"


def _run(tmp_path: Path) -> Path:
    intent_path = load_scenario(
        PROTOTYPE, "retire_alice",
        intent_dir=tmp_path / "intent", loaded_by="test",
        allow_dirty_data=True,
    ).intent_path
    run_path = tmp_path / "runs" / "r.sqlite3"
    return run_scenario(
        intent_path, run_path,
        run_by="test", data_mode="prototype", allow_dirty_data=True,
    )


def test_net_worth_series_and_year_end(tmp_path: Path) -> None:
    with open_run(_run(tmp_path)) as rc:
        nw = net_worth_series(rc)
        assert list(nw.columns) == ["nominal", "cpi_constant"]
        assert nw.index.is_monotonic_increasing

        year_end = year_end_net_worth(nw)
        assert (year_end.index == sorted(year_end.index)).all()
        assert set(nw.index.year) >= set(year_end.index)


def test_income_and_expenses_by_category_roll_up_to_net_income(tmp_path: Path) -> None:
    with open_run(_run(tmp_path)) as rc:
        income = income_by_category(rc)
        expenses = expenses_by_category(rc)

        assert "total" in income.columns
        assert "total" in expenses.columns

        income_yearly = yearly_totals(income)
        expenses_yearly = yearly_totals(expenses)
        net = net_income(income_yearly, expenses_yearly)

        assert list(net.columns) == ["income", "expenses", "net"]
        assert (net["net"] == net["income"] - net["expenses"]).all()


def test_account_balances_pivots_by_month_and_account(tmp_path: Path) -> None:
    with open_run(_run(tmp_path)) as rc:
        balance_pivot, account_kinds = account_balances(rc)

        assert "Asset_Checking" in balance_pivot.columns
        assert account_kinds["Asset_Checking"] == "asset"
        assert balance_pivot.index.is_monotonic_increasing
