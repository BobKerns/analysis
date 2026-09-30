"""ADR-006 Phase 1: pure distribution/draw tests, no I/O."""

from __future__ import annotations

import pytest

from analysis.sim.distributions import (
    DistributionError,
    TruncatedNormal,
    Uniform,
    member_rng,
)

N = 2_000


def test_uniform_draws_stay_within_bounds() -> None:
    dist = Uniform(low=40_000, high=50_000)
    rng = member_rng(seed=1, member_index=0, row_name="checking_rate_vary")
    draws = [dist.draw(rng) for _ in range(N)]
    assert all(dist.low <= d <= dist.high for d in draws)
    assert min(draws) < 41_000  # actually exercises the low end
    assert max(draws) > 49_000  # and the high end


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
