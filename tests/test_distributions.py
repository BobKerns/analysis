"""ADR-006 Phase 1 / ADR-007 Phase 0: pure distribution/draw/quantile tests, no I/O."""

from __future__ import annotations

import pytest

from analysis.sim.distributions import (
    DistributionError,
    TruncatedNormal,
    Uniform,
    member_rng,
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
    dist = TruncatedNormal(mean=30_000, sd=20_000, clip_low=0, clip_high=60_000)
    rng = member_rng(seed=1, member_index=0, row_name="rate_vary")
    draws = [dist.draw(rng) for _ in range(N)]
    assert all(dist.clip_low <= d <= dist.clip_high for d in draws)


def test_truncated_normal_one_sided_clip() -> None:
    dist = TruncatedNormal(mean=0, sd=1, clip_low=0)
    rng = member_rng(seed=1, member_index=0, row_name="one_sided")
    draws = [dist.draw(rng) for _ in range(N)]
    assert all(d >= 0 for d in draws)


def test_truncated_normal_exhausted_attempts_raises() -> None:
    # clip bounds far outside where a mean=0, sd=1 draw could ever land.
    dist = TruncatedNormal(mean=0, sd=1, clip_low=1_000, clip_high=1_001, max_attempts=50)
    rng = member_rng(seed=1, member_index=0, row_name="impossible")
    with pytest.raises(DistributionError, match="no draw landed inside"):
        dist.draw(rng)


def test_resolve_truncated_normal_bounds_infers_missing_side_at_3sd() -> None:
    mean, sd, low, high = resolve_truncated_normal_bounds(45_000, 5_000, None, None)
    assert (mean, sd, low, high) == (45_000, 5_000, 30_000, 60_000)


def test_resolve_truncated_normal_bounds_keeps_given_low_infers_high() -> None:
    mean, sd, low, high = resolve_truncated_normal_bounds(45_000, 5_000, 40_000, None)
    assert (mean, sd, low, high) == (45_000, 5_000, 40_000, 60_000)


def test_resolve_truncated_normal_bounds_single_tailed_when_low_equals_mean() -> None:
    """Setting vary_low to vary_mean 'uses up' the lower side at the peak;
    the still-unspecified high side ramps out to +3sd, giving a one-sided
    (single-tailed) distribution rather than a degenerate point."""
    mean, sd, low, high = resolve_truncated_normal_bounds(45_000, 5_000, 45_000, None)
    assert (mean, sd, low, high) == (45_000, 5_000, 45_000, 60_000)


def test_resolve_truncated_normal_bounds_infers_mean_and_sd_from_low_and_high() -> None:
    mean, sd, low, high = resolve_truncated_normal_bounds(None, None, 30_000, 60_000)
    assert (mean, sd, low, high) == (45_000, 5_000, 30_000, 60_000)


def test_resolve_truncated_normal_bounds_infers_sd_from_mean_and_one_bound() -> None:
    mean, sd, low, high = resolve_truncated_normal_bounds(45_000, None, None, 60_000)
    assert (mean, sd, low, high) == (45_000, 5_000, 30_000, 60_000)


def test_resolve_truncated_normal_bounds_requires_both_low_and_high_for_mean() -> None:
    with pytest.raises(DistributionError, match="vary_mean requires"):
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
    TruncatedNormal(mean=0, sd=1, clip_low=-3, clip_high=3),
    TruncatedNormal(mean=0, sd=1, clip_low=-0.5, clip_high=3),
])
def test_quantile_is_monotonic_and_in_bounds(dist: Uniform | TruncatedNormal) -> None:
    values = [dist.quantile(q / 50) for q in range(51)]
    assert values == sorted(values)
    low = dist.low if isinstance(dist, Uniform) else dist.clip_low
    high = dist.high if isinstance(dist, Uniform) else dist.clip_high
    assert values[0] == pytest.approx(low) and values[-1] == pytest.approx(high)


def test_symmetric_truncated_normal_median_is_mean() -> None:
    dist = TruncatedNormal(mean=100, sd=10, clip_low=70, clip_high=130)
    assert dist.quantile(0.5) == pytest.approx(100)


def test_asymmetric_clip_shifts_the_median_away_from_the_clipped_side() -> None:
    dist = TruncatedNormal(mean=0, sd=1, clip_low=-0.5, clip_high=3)
    assert dist.quantile(0.5) > 0


def test_one_sided_clip_at_mean_starts_at_mean() -> None:
    dist = TruncatedNormal(mean=10, sd=2, clip_low=10, clip_high=16)
    assert dist.quantile(0) == 10
    assert dist.quantile(0.1) > 10


@pytest.mark.parametrize("q", [0.1, 0.25, 0.9])
def test_truncated_normal_quantile_matches_empirical_draws(q: float) -> None:
    dist = TruncatedNormal(mean=0, sd=1, clip_low=-1, clip_high=2)
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
        TruncatedNormal(mean=0, sd=1, clip_low=-3, clip_high=3).quantile(q)


def test_parse_quantiles_accepts_increasing_list() -> None:
    assert parse_quantiles("0.1,0.9") == (0.1, 0.9)
    assert parse_quantiles("0, 0.25,0.75,1") == (0.0, 0.25, 0.75, 1.0)


@pytest.mark.parametrize("text", ["", "0.9,0.1", "0.1,0.1", "0.1,0.5,0.9", "1.2", "a,b"])
def test_parse_quantiles_rejects_malformed(text: str) -> None:
    with pytest.raises(DistributionError):
        parse_quantiles(text)
