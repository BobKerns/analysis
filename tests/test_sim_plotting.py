"""Smoke tests for analysis.sim.plotting (non-interactive Agg backend)."""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")

import pandas as pd

from analysis.sim.plotting import plot_income_expense_net, plot_stacked_bar, plot_timeseries


def test_plot_timeseries_returns_axes_with_legend() -> None:
    idx = pd.date_range("2026-01-31", periods=3, freq="ME")
    series = {"nominal": pd.Series([1.0, 2.0, 3.0], index=idx)}

    ax = plot_timeseries(series, title="net worth")

    assert ax.get_title() == "net worth"
    assert ax.get_legend() is not None


def test_plot_stacked_bar_returns_axes() -> None:
    df = pd.DataFrame({"a": [1, 2], "b": [3, 4]}, index=pd.Index([2026, 2027], name="year"))

    ax = plot_stacked_bar(df, ["a", "b"], ["#111111", "#222222"], title="stacked")

    assert ax.get_title() == "stacked"


def test_plot_income_expense_net_returns_axes() -> None:
    net = pd.DataFrame(
        {"income": [100.0], "expenses": [40.0], "net": [60.0]},
        index=pd.Index([2026], name="year"),
    )

    ax = plot_income_expense_net(net, title="net income")

    assert ax.get_title() == "net income"
