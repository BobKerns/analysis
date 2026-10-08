"""analysis.historical.charts (``analysis historical-charts``): the statistics
behind the century-of-rates charts, and that every chart draws."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from analysis.historical import charts


def by_year(values: dict[int, float]) -> pd.Series:
    return pd.Series(values, dtype=float)


def test_year_pairs_skip_a_missing_year():
    pairs = charts.year_pairs(by_year({2000: 1.0, 2001: 2.0, 2003: 4.0, 2004: 5.0}))
    assert pairs == [(2000, 1.0, 2.0), (2003, 4.0, 5.0)]


def test_correlation_of_a_line_is_one_and_of_its_mirror_minus_one():
    assert charts.correlation([(1, 2), (2, 4), (3, 6)]) == pytest.approx(1.0)
    assert charts.correlation([(1, 6), (2, 4), (3, 2)]) == pytest.approx(-1.0)


def test_persistence_by_lag_tells_a_trend_from_an_alternation():
    trend = by_year({1990 + i: float(i) for i in range(20)})
    flips = by_year({1990 + i: (-1.0) ** i for i in range(20)})
    assert charts.persistence_by_lag(trend, lags=3) == pytest.approx([1.0, 1.0, 1.0])
    assert charts.persistence_by_lag(flips, lags=2)[0] == pytest.approx(-1.0)


def test_rolling_persistence_needs_every_year_of_the_window():
    rates = by_year({y: float(y % 3) for y in range(1950, 1960)} | {y: float(y % 3) for y in range(1961, 1975)})
    rolled = charts.rolling_persistence(rates, window=5)
    assert list(rolled.index) == [1954, 1955, 1956, 1957, 1958, 1959] + list(range(1965, 1975))


def test_monthly_inflation_rates_one_month_and_twelve():
    months = pd.date_range("2000-01-01", periods=25, freq="MS")
    levels = pd.Series([100 * 1.01 ** i for i in range(25)], index=months)
    xs, yoy, single = charts.monthly_inflation(levels, 2001, 2001)
    assert xs[0] == pytest.approx(2001.0) and len(xs) == 12
    assert yoy[0] == pytest.approx((1.01 ** 12 - 1) * 100)
    assert single[5] == pytest.approx((1.01 ** 12 - 1) * 100)


def test_every_chart_draws_an_svg(tmp_path):
    years = range(1928, 2026)
    wave = by_year({y: 3 + 2 * math.sin(y / 5) for y in years})
    noise = by_year({y: 10 * math.sin(y * 2.1) for y in years})
    months = pd.date_range("1967-01-01", "1976-12-01", freq="MS")
    history = charts.History(inflation=wave, tbill=wave, yield10=wave.loc[1962:], stocks=noise, bonds=noise,
                             cpi_monthly=pd.Series([30 * 1.004 ** i for i in range(len(months))], index=months))
    paths = charts.write_charts(history, tmp_path / "out")
    assert sorted(p.name for p in paths) == sorted(charts.CHARTS)
    for path in paths:
        assert path.read_text().lstrip().startswith("<?xml")
