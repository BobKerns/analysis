"""The ensemble charts as plain matplotlib figures (#163, #167)."""

from __future__ import annotations

import matplotlib
import pandas as pd
import pytest

matplotlib.use("Agg")

import matplotlib.pyplot as plt

from analysis.sim.ensemble import summary_with_percentiles
from analysis.sim.ensemble_plots import (
    ensemble_compare_figure,
    ensemble_fan_chart,
    ensemble_fan_figure,
    figure_bytes,
)


def _monthly_summary() -> pd.DataFrame:
    index = pd.date_range("2026-01-31", periods=6, freq="ME")
    return pd.DataFrame({
        "mean": [100.0, 110, 120, 130, 140, 150],
        "median": [99.0, 109, 119, 129, 139, 149],
        "std": [5.0, 5, 5, 5, 5, 5],
        "min": [90.0, 95, 100, 105, 110, 115],
        "max": [110.0, 120, 130, 140, 150, 160],
    }, index=index)


def test_ensemble_fan_chart_builds_without_error() -> None:
    ax = ensemble_fan_chart(_monthly_summary())
    assert len(ax.lines) == 3  # mean, median, zero rule
    assert len(ax.collections) == 1  # fill_between range band


def test_ensemble_fan_chart_shades_quintile_bands_darker_toward_the_middle() -> None:
    members = pd.DataFrame(
        [[float(m * 10 + month) for m in range(11)] for month in range(6)],
        index=pd.date_range("2026-01-31", periods=6, freq="ME"),
    )
    monthly = summary_with_percentiles(members)
    ax = ensemble_fan_chart(monthly)
    assert len(ax.collections) == 5  # p05-p20, p20-p40, p40-p60, p60-p80, p80-p95
    _handles, labels = ax.get_legend_handles_labels()
    assert labels == ["5–95%", "20–80%", "40–60%", "mean", "median"]
    # Lighter (closer to white) at the edges, darkest in the middle band,
    # and symmetric about it.
    lightness = [sum(c.get_facecolor()[0][:3]) for c in ax.collections]
    assert lightness[0] > lightness[1] > lightness[2]
    assert lightness[0] == pytest.approx(lightness[4])
    assert lightness[1] == pytest.approx(lightness[3])


def test_ensemble_fan_chart_zero_rule_does_not_distort_a_far_off_range() -> None:
    """The zero rule is drawn with axhline, which (left alone) would feed
    into autoscale and pull the whole chart down to include 0 even when the
    data never gets near it -- the exact distortion a fan chart is trying to
    avoid. The axis limits must come out identical to a chart with no rule
    at all when 0 is nowhere close."""
    far_from_zero = pd.DataFrame({
        "mean": [950_000.0, 1_020_000], "median": [945_000.0, 1_015_000],
        "std": [5_000.0, 5_000], "min": [900_000.0, 950_000], "max": [1_000_000.0, 1_100_000],
    }, index=pd.date_range("2026-01-31", periods=2, freq="ME"))

    fig1, ax1 = plt.subplots()
    ax1.fill_between(far_from_zero.index, far_from_zero["min"], far_from_zero["max"])
    ax1.plot(far_from_zero.index, far_from_zero["mean"])
    ax1.plot(far_from_zero.index, far_from_zero["median"])
    expected_ylim = ax1.get_ylim()

    ax2 = ensemble_fan_chart(far_from_zero)
    assert ax2.get_ylim() == expected_ylim
    assert expected_ylim[0] > 0  # 0 genuinely isn't in (or near) this range


def test_ensemble_fan_chart_zero_rule_shows_when_range_crosses_zero() -> None:
    crosses_zero = pd.DataFrame({
        "mean": [50.0, -10], "median": [45.0, -15],
        "std": [5.0, 5], "min": [10.0, -80], "max": [100.0, 50],
    }, index=pd.date_range("2026-01-31", periods=2, freq="ME"))
    ax = ensemble_fan_chart(crosses_zero)
    ylim = ax.get_ylim()
    assert ylim[0] < 0 < ylim[1]
    zero_rules = [line for line in ax.lines if list(line.get_ydata()) == [0, 0]]
    assert len(zero_rules) == 1


def test_ensemble_fan_chart_zero_rule_skipped_on_log_scale() -> None:
    far_from_zero = pd.DataFrame({
        "mean": [950_000.0, 1_020_000], "median": [945_000.0, 1_015_000],
        "std": [5_000.0, 5_000], "min": [900_000.0, 950_000], "max": [1_000_000.0, 1_100_000],
    }, index=pd.date_range("2026-01-31", periods=2, freq="ME"))
    fig, ax = plt.subplots()
    ax.set_yscale("log")
    ensemble_fan_chart(far_from_zero, ax=ax)
    assert len(ax.lines) == 2  # mean, median -- no zero rule added at all


def test_ensemble_compare_figure_skips_the_paired_panel_when_unpaired() -> None:
    months = pd.date_range("2027-01-31", periods=3, freq="ME")
    baseline = pd.DataFrame({"member_0000": [1.0, 2.0, 3.0], "member_0001": [2.0, 3.0, 4.0]},
                            index=months)
    paired = ensemble_compare_figure("a", "b", baseline, baseline + 1,
                                     [("member_0000", "member_0000")], "nominal")
    unpaired = ensemble_compare_figure("a", "b", baseline, baseline + 1, [], "nominal")
    assert (len(paired.axes), len(unpaired.axes)) == (3, 2)
    with_reserve = ensemble_compare_figure("a", "b", baseline, baseline + 1, [], "nominal", 2.5)
    assert len(with_reserve.axes) == 3
    assert "reserve" in with_reserve.axes[-1].get_title()


def test_ensemble_fan_figure_draws_a_reserve_line_and_a_log_scale_on_request() -> None:
    plt.close("all")  # other tests here draw with pyplot
    plain = ensemble_fan_figure(_monthly_summary(), "net worth fan chart (nominal) (a)")
    (ax,) = plain.axes
    assert ax.get_title() == "net worth fan chart (nominal) (a)"
    assert ax.get_yscale() == "linear"
    assert "reserve" not in ax.get_legend_handles_labels()[1]

    with_reserve = ensemble_fan_figure(_monthly_summary(), "t", reserve=95.0, log_scale=True)
    (ax,) = with_reserve.axes
    assert ax.get_yscale() == "log"
    assert "reserve" in ax.get_legend_handles_labels()[1]
    # Scaled before drawing, so the dollar formatter survived the log scale.
    assert ax.yaxis.get_major_formatter()(1000, 0) == "$1,000"
    assert plt.get_fignums() == []  # never a pyplot figure


def test_figure_bytes_renders_png_or_svg() -> None:
    fig = ensemble_fan_figure(_monthly_summary(), "t")
    assert figure_bytes(fig).startswith(b"\x89PNG\r\n\x1a\n")
    assert b"<svg" in figure_bytes(fig, "svg")
    small, large = figure_bytes(fig, dpi=50), figure_bytes(fig, dpi=200)
    assert int.from_bytes(small[16:20]) < int.from_bytes(large[16:20])
