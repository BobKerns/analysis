"""ADR-006 Phase 1 / ADR-007 Phase 0 / #31: pure distribution, draw, quantile and
moment tests, no I/O."""

from __future__ import annotations

import pytest

from analysis.sim.distributions import (
    DistributionError,
    Triangular,
    TruncatedNormal,
    Uniform,
    member_rng,
    moments,
    parse_quantiles,
    resolve_truncated_normal_bounds,
)

N = 2_000


def test_uniform_draws_stay_within_bounds() -> None:
    dist = Uniform(low=40_000, high=50_000)
    rng = member_rng(seed=1, member_index=0, row_name="checking_rate_vary")
    draws = [dist.draw(rng) for _ in range(N)]
    assert all(dist.low <= d <= dist.high for d in draws)
    assert min(draws) < 41_000  # actually exercises the low end
    assert max(draws) > 49_000  # and the high end


def test_uniform_draw_int_gives_each_integer_equal_probability() -> None:
    """A discrete 3-value range (e.g. a 3-day date span): each value must
    land roughly equally often. round(draw(rng)) instead would give the two
    endpoints only half the share of the middle value -- 25/50/25 instead of
    roughly 33/33/33."""
    dist = Uniform(low=0, high=2)
    counts = {0: 0, 1: 0, 2: 0}
    for i in range(N):
        rng = member_rng(seed=1, member_index=i, row_name="date_vary")
        counts[dist.draw_int(rng)] += 1
    for value in (0, 1, 2):
        share = counts[value] / N
        assert 0.28 <= share <= 0.38, (value, share, counts)


def test_truncated_normal_draws_stay_within_clip_bounds() -> None:
    low, high = 0, 60_000
    dist = TruncatedNormal(median=30_000, sd=20_000, clip_low=low, clip_high=high)
    rng = member_rng(seed=1, member_index=0, row_name="rate_vary")
    draws = [dist.draw(rng) for _ in range(N)]
    assert all(low <= d <= high for d in draws)


def test_truncated_normal_one_sided_clip() -> None:
    dist = TruncatedNormal(median=0, sd=1, clip_low=-0.5)
    rng = member_rng(seed=1, member_index=0, row_name="one_sided")
    draws = [dist.draw(rng) for _ in range(N)]
    assert all(d >= -0.5 for d in draws)


def test_truncated_normal_narrow_far_window_draws_without_retrying() -> None:
    """Inverse-CDF sampling (#31): a window rejection sampling could almost
    never hit is no harder than any other."""
    dist = TruncatedNormal(median=1_000.5, sd=1, clip_low=1_000, clip_high=1_001)
    rng = member_rng(seed=1, member_index=0, row_name="narrow")
    draws = [dist.draw(rng) for _ in range(N)]
    assert all(1_000 <= d <= 1_001 for d in draws)


@pytest.mark.parametrize("median", [45_000, 60_000, 30_000])
def test_truncated_normal_median_must_be_strictly_inside(median: float) -> None:
    """A median can't sit on a bound (#31): half the mass would have to be
    on it. The old "vary_low == vary_mean for a one-sided draw" is now skew."""
    with pytest.raises(DistributionError, match="strictly between"):
        TruncatedNormal(median=median, sd=5_000, clip_low=45_000 if median == 45_000 else 30_000,
                        clip_high=60_000)


def test_resolve_truncated_normal_bounds_infers_missing_side_at_3sd() -> None:
    mean, sd, low, high = resolve_truncated_normal_bounds(45_000, 5_000, None, None)
    assert (mean, sd, low, high) == (45_000, 5_000, 30_000, 60_000)


def test_resolve_truncated_normal_bounds_keeps_given_low_infers_high() -> None:
    mean, sd, low, high = resolve_truncated_normal_bounds(45_000, 5_000, 40_000, None)
    assert (mean, sd, low, high) == (45_000, 5_000, 40_000, 60_000)


def test_resolve_truncated_normal_bounds_with_shape_places_bounds_asymmetrically() -> None:
    """With skew, the default ±3 standard-normal-score bounds land unevenly
    around the median: a longer high tail reaches further up."""
    median, sd, low, high = resolve_truncated_normal_bounds(45_000, 5_000, None, None, skew=0.5)
    assert median == 45_000 and sd == 5_000
    assert high - median > median - low


def test_resolve_truncated_normal_bounds_requires_sd_with_shape() -> None:
    with pytest.raises(DistributionError, match="vary_sd is required"):
        resolve_truncated_normal_bounds(45_000, None, 30_000, 60_000, skew=0.5)


def test_resolve_truncated_normal_bounds_infers_mean_and_sd_from_low_and_high() -> None:
    mean, sd, low, high = resolve_truncated_normal_bounds(None, None, 30_000, 60_000)
    assert (mean, sd, low, high) == (45_000, 5_000, 30_000, 60_000)


def test_resolve_truncated_normal_bounds_infers_sd_from_mean_and_one_bound() -> None:
    mean, sd, low, high = resolve_truncated_normal_bounds(45_000, None, None, 60_000)
    assert (mean, sd, low, high) == (45_000, 5_000, 30_000, 60_000)


def test_resolve_truncated_normal_bounds_requires_both_low_and_high_for_mean() -> None:
    with pytest.raises(DistributionError, match="vary_median requires"):
        resolve_truncated_normal_bounds(None, 5_000, 30_000, None)


def test_resolve_truncated_normal_bounds_requires_a_bound_for_sd() -> None:
    with pytest.raises(DistributionError, match="vary_sd requires"):
        resolve_truncated_normal_bounds(45_000, None, None, None)


def test_resolve_truncated_normal_bounds_rejects_asymmetric_low_high_without_sd() -> None:
    with pytest.raises(DistributionError, match="aren't symmetric"):
        resolve_truncated_normal_bounds(45_000, None, 40_000, 70_000)


def test_member_rng_is_reproducible() -> None:
    rng1 = member_rng(seed=42, member_index=3, row_name="checking_rate_vary")
    rng2 = member_rng(seed=42, member_index=3, row_name="checking_rate_vary")
    dist = Uniform(low=0, high=1)
    assert dist.draw(rng1) == dist.draw(rng2)


def test_member_rng_differs_by_member_index() -> None:
    dist = Uniform(low=0, high=1_000_000)
    a = dist.draw(member_rng(seed=42, member_index=0, row_name="x"))
    b = dist.draw(member_rng(seed=42, member_index=1, row_name="x"))
    assert a != b


def test_member_rng_differs_by_row_name() -> None:
    """Adding/removing one row's variation must never perturb another's draws."""
    dist = Uniform(low=0, high=1_000_000)
    a = dist.draw(member_rng(seed=42, member_index=0, row_name="row_a"))
    b = dist.draw(member_rng(seed=42, member_index=0, row_name="row_b"))
    assert a != b


# --- ADR-007 §3: quantiles -------------------------------------------------


def test_uniform_quantile_hits_bounds_and_center() -> None:
    dist = Uniform(low=40_000, high=50_000)
    assert dist.quantile(0) == 40_000
    assert dist.quantile(1) == 50_000
    assert dist.quantile(0.5) == dist.central()
    assert dist.quantile(0.1) == pytest.approx(41_000)


def test_uniform_quantile_int_stays_in_date_range() -> None:
    dist = Uniform(low=738_000, high=738_364)
    assert dist.quantile_int(0) == 738_000
    assert dist.quantile_int(1) == 738_364
    assert all(738_000 <= dist.quantile_int(q / 20) <= 738_364 for q in range(21))


@pytest.mark.parametrize("dist", [
    Uniform(low=-5, high=5),
    TruncatedNormal(median=0, sd=1, clip_low=-3, clip_high=3),
    TruncatedNormal(median=0, sd=1, clip_low=-0.5, clip_high=3),
    TruncatedNormal(median=0, sd=1, clip_low=-3, clip_high=3, skew=0.8, tailweight=0.6),
    Triangular(0, 1, 10),
])
def test_quantile_is_monotonic_and_in_bounds(dist: Uniform | TruncatedNormal | Triangular) -> None:
    values = [dist.quantile(q / 50) for q in range(51)]
    assert values == sorted(values)
    low = dist.clip_low if isinstance(dist, TruncatedNormal) else dist.low
    high = dist.clip_high if isinstance(dist, TruncatedNormal) else dist.high
    assert values[0] == pytest.approx(low) and values[-1] == pytest.approx(high)


def test_symmetric_truncated_normal_median() -> None:
    dist = TruncatedNormal(median=100, sd=10, clip_low=70, clip_high=130)
    assert dist.quantile(0.5) == pytest.approx(100)
    assert dist.location == pytest.approx(100)


@pytest.mark.parametrize(("low", "high", "skew", "tailweight"), [
    (-0.5, 3, 0.0, 1.0),     # asymmetric bounds
    (-3, 0.2, 0.0, 1.0),
    (-1, 4, 0.7, 1.0),       # skewed
    (-4, 3, -0.5, 0.6),      # skewed left, heavy-tailed
    (-2, 6, 0.4, 1.5),       # light-tailed
])
def test_median_is_exact_whatever_the_bounds_and_shape(
    low: float, high: float, skew: float, tailweight: float,
) -> None:
    """#31: the authored median is the truncated distribution's median, so
    the base case (central) is exactly the authored value."""
    dist = TruncatedNormal(median=0, sd=1, clip_low=low, clip_high=high,
                           skew=skew, tailweight=tailweight)
    assert dist.quantile(0.5) == pytest.approx(0, abs=1e-9)
    assert dist.central() == 0


def test_default_shape_is_exactly_the_normal() -> None:
    from statistics import NormalDist

    dist = TruncatedNormal(median=0, sd=1)
    for q in (0.05, 0.25, 0.5, 0.9):
        assert dist.quantile(q) == pytest.approx(NormalDist().inv_cdf(q))


def test_skew_and_tailweight_move_the_moments_the_right_way() -> None:
    symmetric = moments(TruncatedNormal(median=0, sd=1, clip_low=-6, clip_high=6))
    right = moments(TruncatedNormal(median=0, sd=1, clip_low=-6, clip_high=6, skew=0.5))
    heavy = moments(TruncatedNormal(median=0, sd=1, clip_low=-20, clip_high=20, tailweight=0.7))
    assert abs(symmetric.skewness) < 1e-6 and abs(symmetric.excess_kurtosis) < 0.01
    assert right.skewness > 0.3 and right.mean > 0  # longer high tail pulls the mean up
    assert heavy.excess_kurtosis > 0.5


@pytest.mark.parametrize("q", [0.1, 0.5, 0.9])
def test_shaped_quantile_matches_empirical_draws(q: float) -> None:
    dist = TruncatedNormal(median=6, sd=2, clip_low=0, clip_high=20, skew=0.6, tailweight=0.8)
    rng = member_rng(seed=3, member_index=0, row_name="shaped")
    draws = [dist.draw(rng) for _ in range(10_000)]
    fraction = sum(d <= dist.quantile(q) for d in draws) / len(draws)
    assert fraction == pytest.approx(q, abs=0.02)


def test_truncated_normal_rejects_bad_shape() -> None:
    with pytest.raises(DistributionError, match="tailweight must be positive"):
        TruncatedNormal(median=0, sd=1, tailweight=0)
    with pytest.raises(DistributionError, match="sd must be positive"):
        TruncatedNormal(median=0, sd=0)


# --- triangular (#31) -----------------------------------------------------------


def test_triangular_quantiles_and_center() -> None:
    dist = Triangular(low=0, mode=2, high=10)
    assert dist.quantile(0) == 0 and dist.quantile(1) == 10
    assert dist.quantile(0.2) == pytest.approx(2)  # the mode, where 20% of the mass lies below
    assert dist.central() == pytest.approx(dist.quantile(0.5))
    assert 2 < dist.central() < 5  # the median sits between the mode and the midpoint


def test_triangular_quantile_matches_empirical_draws() -> None:
    dist = Triangular(low=0, mode=1, high=10)
    rng = member_rng(seed=4, member_index=0, row_name="tri")
    draws = [dist.draw(rng) for _ in range(10_000)]
    for q in (0.1, 0.5, 0.9):
        assert sum(d <= dist.quantile(q) for d in draws) / len(draws) == pytest.approx(q, abs=0.02)


def test_triangular_date_target_draws_whole_days_in_range() -> None:
    dist = Triangular(low=738_000, mode=738_030, high=738_300)
    rng = member_rng(seed=5, member_index=0, row_name="closing_date")
    draws = [dist.draw_int(rng) for _ in range(1_000)]
    assert all(isinstance(d, int) and 738_000 <= d <= 738_300 for d in draws)
    assert dist.quantile_int(0) == 738_000 and isinstance(dist.central_int(), int)


@pytest.mark.parametrize(("low", "mode", "high"), [(5, 1, 10), (0, 11, 10), (3, 3, 3)])
def test_triangular_rejects_bad_parameters(low: float, mode: float, high: float) -> None:
    with pytest.raises(DistributionError):
        Triangular(low, mode, high)


# --- moments ---------------------------------------------------------------------


def test_moments_match_known_values() -> None:
    uniform = moments(Uniform(0, 1))
    assert uniform.mean == pytest.approx(0.5) and uniform.sd == pytest.approx(12 ** -0.5, rel=1e-4)
    assert uniform.excess_kurtosis == pytest.approx(-1.2, abs=1e-4)
    triangle = moments(Triangular(0, 0, 1))
    assert triangle.skewness == pytest.approx(2 * 2 ** 0.5 / 5, abs=1e-3)
    assert triangle.excess_kurtosis == pytest.approx(-0.6, abs=1e-3)
    clipped = moments(TruncatedNormal(median=0, sd=1, clip_low=-3, clip_high=3))
    assert clipped.sd == pytest.approx(0.98658, abs=1e-4)
    assert clipped.excess_kurtosis == pytest.approx(-0.1712, abs=2e-3)


@pytest.mark.parametrize("q", [0.1, 0.25, 0.9])
def test_truncated_normal_quantile_matches_empirical_draws(q: float) -> None:
    dist = TruncatedNormal(median=0, sd=1, clip_low=-1, clip_high=2)
    rng = member_rng(seed=7, member_index=0, row_name="empirical")
    draws = [dist.draw(rng) for _ in range(10_000)]
    threshold = dist.quantile(q)
    fraction = sum(d <= threshold for d in draws) / len(draws)
    assert fraction == pytest.approx(q, abs=0.02)


@pytest.mark.parametrize("q", [-0.1, 1.1])
def test_quantile_rejects_out_of_range(q: float) -> None:
    with pytest.raises(DistributionError):
        Uniform(low=0, high=1).quantile(q)
    with pytest.raises(DistributionError):
        TruncatedNormal(median=0, sd=1, clip_low=-3, clip_high=3).quantile(q)


def test_parse_quantiles_accepts_increasing_list() -> None:
    assert parse_quantiles("0.1,0.9") == (0.1, 0.9)
    assert parse_quantiles("0, 0.25,0.75,1") == (0.0, 0.25, 0.75, 1.0)


@pytest.mark.parametrize("text", ["", "0.1", "0.9,0.1", "0.1,0.1", "0.1,0.5,0.9", "1.2", "a,b"])
def test_parse_quantiles_rejects_malformed(text: str) -> None:
    with pytest.raises(DistributionError):
        parse_quantiles(text)


def test_unreachable_median_is_a_clear_error() -> None:
    """With heavy tails the truncated median can't reach every value: a
    left-skewed, heavy-tailed shape can't put its median near the top of a
    narrow window. That's a contradictory spec, reported with the reachable
    range, not a silent misfit."""
    with pytest.raises(DistributionError, match="isn't reachable.*only reaches.*tailweight toward 1"):
        TruncatedNormal(median=0, sd=1, clip_low=-4, clip_high=1, skew=-0.5, tailweight=0.6)


def test_far_out_window_keeps_precision() -> None:
    """A window deep in the upper tail still has well-defined quantiles
    (survival probabilities, not 1 - cdf)."""
    dist = TruncatedNormal(median=20.5, sd=1, clip_low=20, clip_high=21)
    assert dist.quantile(0.5) == pytest.approx(20.5, abs=1e-6)
    values = [dist.quantile(q / 10) for q in range(11)]
    assert values == sorted(values) and values[0] == 20 and values[-1] == 21


# --- PR #33 second review ---------------------------------------------------------


@pytest.mark.parametrize("kwargs", [
    {"skew": 1000.0},
    {"tailweight": 1e-6},
])
def test_extreme_shape_is_a_clean_error_not_a_crash(kwargs: dict[str, float]) -> None:
    """A finite but extreme shape overflowed math.sinh; it must surface as a
    DistributionError the loader and CLI already report."""
    with pytest.raises(DistributionError, match="too extreme to compute"):
        resolve_truncated_normal_bounds(0, 1, None, None, **kwargs)


@pytest.mark.parametrize("field", ["median", "sd", "skew", "tailweight"])
def test_non_finite_parameters_are_rejected(field: str) -> None:
    params = {"median": 0.0, "sd": 1.0, "clip_low": -3.0, "clip_high": 3.0, "skew": 0.0,
              "tailweight": 1.0}
    params[field] = float("inf")
    with pytest.raises(DistributionError, match="finite"):
        TruncatedNormal(**params)


def test_median_hugging_a_bound_reports_the_numeric_limit() -> None:
    """A plain normal can reach any interior median in principle, but one
    hugging a bound relative to sd needs a location beyond double precision:
    report that honestly (with what's reachable), not as a shape problem."""
    with pytest.raises(DistributionError, match="too close to a bound for this vary_sd"):
        TruncatedNormal(median=1, sd=1_000, clip_low=0, clip_high=3_001)
    # A sensible median near the same bound is fine.
    assert TruncatedNormal(median=100, sd=1_000, clip_low=0, clip_high=3_001).quantile(0.5) \
        == pytest.approx(100, abs=1e-6)


def test_date_moments_count_whole_days() -> None:
    """Dates are drawn in whole days: two days drawn equally have SD 0.5 day
    and excess kurtosis -2, not a continuous uniform's 0.29 and -1.2."""
    two_days = moments(Uniform(0, 1), whole_days=True)
    assert two_days.sd == pytest.approx(0.5) and two_days.excess_kurtosis == pytest.approx(-2.0)
    # Over a realistic span the difference vanishes.
    year = moments(Uniform(0, 364), whole_days=True)
    assert year.sd == pytest.approx(moments(Uniform(0, 364)).sd, rel=1e-2)


def test_amount_moments_follow_stored_rounding() -> None:
    """Amounts are stored rounded to whole cents/micros for every family, so
    a 0-to-1 uniform amount resolves to 0 or 1 (SD 0.5, not the continuous
    0.29). Over a realistic spread the rounding is invisible."""
    tiny = moments(Uniform(0, 1), rounded=True)
    assert tiny.mean == pytest.approx(0.5) and tiny.sd == pytest.approx(0.5)
    realistic = moments(Uniform(40_000, 50_000), rounded=True)
    assert realistic.sd == pytest.approx(moments(Uniform(40_000, 50_000)).sd, rel=1e-6)


def test_an_unplaceable_median_with_nothing_reachable_is_a_clean_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#35: if every sampled location has no representable probability in the
    window, there's no reachable range to report -- min()/max() of nothing
    raised ValueError instead of the promised DistributionError."""
    monkeypatch.setattr(TruncatedNormal, "_truncated_quantile", lambda self, q, location: None)
    with pytest.raises(DistributionError, match="can't be placed"):
        TruncatedNormal(median=0, sd=1, clip_low=-3, clip_high=3)
