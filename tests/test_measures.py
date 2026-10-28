"""The yearly measures behind guard rails (#305), on hand-made series."""

from __future__ import annotations

import numpy as np
import pytest

from analysis.sim import measures

# Three years of months: 2030 from June (a partial first year), 2031, 2032.
YEARS = np.array([2030] * 7 + [2031] * 12 + [2032] * 12)
AXIS = measures.year_axis(YEARS)
MONTHS = len(YEARS)


def test_year_axis_maps_each_month_to_its_year() -> None:
    assert list(AXIS.years) == [2030, 2031, 2032]
    assert list(AXIS.last_month) == [6, 18, 30]
    assert AXIS.of_month[7] == 1


def test_yearly_sums_sum_each_row_by_year() -> None:
    values = np.vstack([np.ones(MONTHS), np.arange(MONTHS)])
    sums = measures.yearly_sums(values, AXIS)
    assert sums[0].tolist() == [7, 12, 12]
    assert sums[1].tolist() == [sum(range(7)), sum(range(7, 19)), sum(range(19, 31))]


def test_year_start_values_read_the_previous_december() -> None:
    values = np.arange(MONTHS, dtype=float)
    starts = measures.year_start_values(values, AXIS)[0]
    assert starts[:7].tolist() == [0.0] * 7  # the first year: the first month's value
    assert starts[7:19].tolist() == [6.0] * 12
    assert starts[19:].tolist() == [18.0] * 12


def test_value_before_window_is_the_month_before_it_starts() -> None:
    values = np.arange(MONTHS, dtype=float)
    active = np.zeros(MONTHS, dtype=bool)
    active[7:] = True
    assert measures.value_before_window(values, active)[0] == 6.0
    assert measures.value_before_window(values, np.ones(MONTHS, dtype=bool))[0] == 0.0
    assert np.isnan(measures.value_before_window(values, np.zeros(MONTHS, dtype=bool))[0])


def test_dietz_return_counts_withdrawals_as_made_mid_year() -> None:
    """A portfolio that grows 10% while paying out 10 of 100 over the year
    ends near 100: return ≈ (100 - 100 + 10) / (100 - 5)."""
    values = np.full(MONTHS, 100.0)
    out = np.zeros(MONTHS)
    out[7:19] = 10 / 12
    r = measures.dietz_returns(values, out, AXIS)[0]
    assert r[1] == pytest.approx(10 / 95)
    assert r[2] == 0.0


def _evaluate(measure: float, allowance: float, side: measures.Side = "above",
              active: np.ndarray | None = None, returns: np.ndarray | None = None) -> measures.RailYears:
    return measures.evaluate(
        np.full(MONTHS, measure), np.full(MONTHS, allowance), np.ones(MONTHS),
        np.ones(MONTHS, dtype=bool) if active is None else active, AXIS, side, returns,
    )


def test_margin_is_negative_past_the_rail_whichever_way_it_faces() -> None:
    ceiling = _evaluate(measure=11, allowance=10)
    assert ceiling.margin[0].tolist() == [-7, -12, -12]
    floor = _evaluate(measure=11, allowance=10, side="below")
    assert floor.margin[0].tolist() == [7, 12, 12]


def test_a_partial_year_counts_only_the_months_covered() -> None:
    """The first year's seven months are measured against seven months of
    threshold, not a whole year's."""
    rail = _evaluate(measure=1, allowance=2)
    assert rail.threshold[0].tolist() == [14, 24, 24]
    assert rail.measure[0].tolist() == [7, 12, 12]


def test_cumulative_margin_sums_the_years_since_the_rail_began() -> None:
    active = np.zeros(MONTHS, dtype=bool)
    active[7:] = True
    rail = _evaluate(measure=11, allowance=10, active=active)
    assert np.isnan(rail.margin[0, 0]) and np.isnan(rail.cumulative[0, 0])
    assert rail.cumulative[0, 1:].tolist() == [-12, -24]


def test_carried_grows_the_cumulative_margin_at_the_returns_given() -> None:
    returns = np.array([[0.0, 0.5, 0.5]])
    rail = _evaluate(measure=11, allowance=10, returns=returns)
    # -7, then -7 * 1.5 - 12, then that * 1.5 - 12
    assert rail.carried[0].tolist() == [-7, -22.5, -45.75]
    assert rail.cumulative[0].tolist() == [-7, -19, -31]


def test_a_rail_that_ends_leaves_later_years_uncovered() -> None:
    active = np.ones(MONTHS, dtype=bool)
    active[19:] = False
    rail = _evaluate(measure=11, allowance=10, active=active)
    assert np.isnan(rail.margin[0, 2]) and np.isnan(rail.cumulative[0, 2])
    assert np.isnan(rail.carried[0, 2])


def test_evaluation_is_causal() -> None:
    """The months through a December give the same years as the whole
    series: what the engine will rely on to evaluate a rail as it goes (#326)."""
    rng = np.random.default_rng(1)
    measure = rng.uniform(0, 20, (3, MONTHS))
    allowance = rng.uniform(5, 15, (3, MONTHS))
    factor = rng.uniform(0.9, 1.0, (3, MONTHS))
    active = rng.uniform(size=(3, MONTHS)) > 0.2
    returns = rng.uniform(-0.2, 0.2, (3, 3))
    whole = measures.evaluate(measure, allowance, factor, active, AXIS, "above", returns)
    cut = 19  # through December 2031
    axis = measures.year_axis(YEARS[:cut])
    prefix = measures.evaluate(measure[:, :cut], allowance[:, :cut], factor[:, :cut],
                               active[:, :cut], axis, "above", returns[:, :2])
    for name in ("measure", "threshold", "margin", "cumulative", "carried"):
        np.testing.assert_array_equal(getattr(prefix, name), getattr(whole, name)[:, :2])
    values = rng.uniform(50, 100, (3, MONTHS))
    np.testing.assert_array_equal(measures.year_start_values(values[:, :cut], axis),
                                  measures.year_start_values(values, AXIS)[:, :cut])
    np.testing.assert_array_equal(measures.dietz_returns(values[:, :cut], measure[:, :cut], axis),
                                  measures.dietz_returns(values, measure, AXIS)[:, :2])


def test_share_and_ever_past_across_rows() -> None:
    margin = np.array([[1.0, -1.0, 2.0], [-1.0, np.nan, 3.0], [1.0, 1.0, -2.0]])
    assert measures.share_past(margin).tolist() == pytest.approx([1 / 3, 1 / 2, 1 / 3])
    assert measures.ever_past(margin).tolist() == [
        [False, True, True], [True, True, True], [False, False, True]]


def test_rms_overshoot_counts_inside_as_zero_and_skips_uncovered_years() -> None:
    """20% of values $10,000 past gives about $4,470: frequency and depth
    together, as the proposal's example says."""
    margin = np.array([-10_000.0, 1.0, 2.0, 3.0, 4.0, np.nan])
    assert measures.rms_overshoot(margin) == pytest.approx(10_000 / np.sqrt(5))
    assert np.isnan(measures.rms_overshoot(np.array([np.nan])))


def test_episodes_are_runs_of_years_past() -> None:
    margin = np.array([-1.0, -3.0, 2.0, np.nan, -1.0])
    found = measures.episodes(margin, np.array([2030, 2031, 2032, 2033, 2034]))
    assert found == [measures.Episode(2030, 2031, 2031, -3.0), measures.Episode(2034, 2034, 2034, -1.0)]
    assert found[0].years == 2
