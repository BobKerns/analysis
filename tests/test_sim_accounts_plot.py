"""Tests for analysis.sim.accounts_plot."""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")

import pandas as pd

from analysis.sim.accounts_plot import render_balance_history_figure, trim_to_last_nonzero


def test_trim_to_last_nonzero_keeps_one_month_past_last_nonzero() -> None:
    idx = pd.date_range("2026-01-31", periods=6, freq="ME")
    df = pd.DataFrame(
        {"A": [100, 200, 0, 0, 0, 0], "B": [50, 0, 30, 0, 0, 0]}, index=idx,
    )

    trimmed = trim_to_last_nonzero(df, ["A", "B"])

    assert list(trimmed.index) == list(idx[:4])  # through the April zero


def test_trim_to_last_nonzero_treats_missing_as_zero() -> None:
    idx = pd.date_range("2026-01-31", periods=4, freq="ME")
    df = pd.DataFrame({"A": [100, None, None, None]}, index=idx)

    trimmed = trim_to_last_nonzero(df, ["A"])

    assert list(trimmed.index) == list(idx[:2])


def test_trim_to_last_nonzero_all_zero_returns_unchanged() -> None:
    idx = pd.date_range("2026-01-31", periods=3, freq="ME")
    df = pd.DataFrame({"A": [0, 0, 0]}, index=idx)

    trimmed = trim_to_last_nonzero(df, ["A"])

    assert list(trimmed.index) == list(idx)


def test_trim_to_last_nonzero_only_considers_selected_columns() -> None:
    idx = pd.date_range("2026-01-31", periods=4, freq="ME")
    df = pd.DataFrame({"A": [100, 0, 0, 0], "B": [0, 0, 0, 900]}, index=idx)

    trimmed = trim_to_last_nonzero(df, ["A"])

    assert list(trimmed.index) == list(idx[:2])


def test_render_balance_history_figure_total_plots_raw_balance() -> None:
    idx = pd.date_range("2026-01-31", periods=4, freq="ME")
    df = pd.DataFrame({"A": [100.0, 150.0, 130.0, 130.0]}, index=idx)

    fig = render_balance_history_figure(df, ["A"], "title")

    line = fig.axes[0].lines[0]
    assert list(line.get_ydata()) == [100.0, 150.0, 130.0, 130.0]


def test_render_balance_history_figure_monthly_plots_month_over_month_change() -> None:
    """First month has no prior value to diff against -- the visible line
    effectively starts one month later."""
    idx = pd.date_range("2026-01-31", periods=4, freq="ME")
    df = pd.DataFrame({"A": [100.0, 150.0, 130.0, 130.0]}, index=idx)

    fig = render_balance_history_figure(df, ["A"], "title", value_mode="monthly")

    line = fig.axes[0].lines[0]
    ydata = list(line.get_ydata())
    assert ydata[0] != ydata[0]  # NaN: no prior month for the first point
    assert ydata[1:] == [50.0, -20.0, 0.0]
