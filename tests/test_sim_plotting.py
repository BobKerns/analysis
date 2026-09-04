"""Smoke tests for analysis.sim.plotting (non-interactive Agg backend)."""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")

import pandas as pd

from analysis.sim.plotting import (
    include_zero,
    plot_income_expense_net,
    plot_stacked_bar,
    plot_timeseries,
)


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


def test_include_zero_gives_headroom_above_a_near_flat_positive_series() -> None:
    """A low-volatility series far from zero must not end up flush against
    the top spine once zero is forced into view (that's what made a line
    like 'Home' effectively disappear)."""
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots()
    ax.plot(range(10), [700_000 + i for i in range(10)])
    include_zero(ax)

    lo, hi = ax.get_ylim()
    assert lo == 0.0
    assert hi > 700_009 + 1_000  # real headroom above the data max, not flush
    plt.close(fig)


def test_include_zero_anchors_top_for_all_negative_series() -> None:
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots()
    ax.plot(range(5), [-100.0, -105.0, -110.0, -108.0, -112.0])
    include_zero(ax)

    lo, hi = ax.get_ylim()
    assert hi == 0.0
    assert lo < -112.0  # headroom below the data min
    plt.close(fig)


def test_include_zero_pads_both_ends_when_data_spans_zero() -> None:
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots()
    ax.plot(range(4), [-50.0, 20.0, -10.0, 40.0])
    include_zero(ax)

    lo, hi = ax.get_ylim()
    assert lo < -50.0
    assert hi > 40.0
    plt.close(fig)
