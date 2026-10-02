"""ADR-006: ensemble variation distributions -- pure functions, no I/O.

Two families to start (`registries.ENSEMBLE_DISTRIBUTIONS`); a third
(triangular, lognormal, ...) is a one-line addition, not a redesign.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from statistics import NormalDist

__all__ = [
    "DistributionError", "Uniform", "TruncatedNormal", "member_rng",
    "DEFAULT_TRUNCATED_NORMAL_CUTOFF_SD", "resolve_truncated_normal_bounds",
    "parse_quantiles",
]

DEFAULT_TRUNCATED_NORMAL_CUTOFF_SD = 3.0
"""Standard deviations used to fill in a `truncated_normal` bound nobody
specified (ADR-006) -- the classic three-sigma range, ~99.7% coverage on the
unclipped side, so rejection sampling rarely has to retry."""


class DistributionError(RuntimeError):
    """A distribution's parameters make no valid draw possible."""


@dataclass(frozen=True)
class Uniform:
    """Equal probability across [low, high] -- the natural choice for a date
    range (ADR-006 §3)."""

    low: float
    high: float

    def draw(self, rng: random.Random) -> float:
        return rng.uniform(self.low, self.high)

    def draw_int(self, rng: random.Random) -> int:
        """Discrete uniform draw over the inclusive integer range
        ``[round(low), round(high)]`` -- for a date target (ADR-006 SS3),
        where ``low``/``high`` are day ordinals. Rounding a continuous
        ``draw()`` to the nearest integer would not give each day equal
        probability: the two endpoint days would only catch half the width
        an interior day does.
        """
        return rng.randint(round(self.low), round(self.high))

    def central(self) -> float:
        """Midpoint of ``[low, high]`` -- always in range by construction
        (ADR-006 §8: the recipe's deterministic member 0)."""
        return (self.low + self.high) / 2

    def central_int(self) -> int:
        return round(self.central())

    def quantile(self, q: float) -> float:
        """The ``q``-quantile of ``[low, high]`` (ADR-007 §3) -- the value an
        OAT sensitivity member perturbs this row to."""
        _check_quantile(q)
        return self.low + q * (self.high - self.low)

    def quantile_int(self, q: float) -> int:
        """``quantile`` rounded to a day ordinal, for a date target."""
        return round(self.quantile(q))


@dataclass(frozen=True)
class TruncatedNormal:
    """A normal draw, rejected and redrawn outside [clip_low, clip_high]
    (either bound may be omitted for one-sided or unbounded)."""

    mean: float
    sd: float
    clip_low: float | None = None
    clip_high: float | None = None
    max_attempts: int = 10_000

    def draw(self, rng: random.Random) -> float:
        for _ in range(self.max_attempts):
            value = rng.gauss(self.mean, self.sd)
            if self.clip_low is not None and value < self.clip_low:
                continue
            if self.clip_high is not None and value > self.clip_high:
                continue
            return value
        raise DistributionError(
            f"truncated_normal(mean={self.mean}, sd={self.sd}, "
            f"clip_low={self.clip_low}, clip_high={self.clip_high}): no draw "
            f"landed inside the clip bounds in {self.max_attempts} attempts"
        )

    def central(self) -> float:
        """``mean``, clamped into ``[clip_low, clip_high]`` (ADR-006 §8: the
        recipe's deterministic member 0). The clamp guards the one case
        where this could land outside the bounds: ``resolve_truncated_normal_bounds``
        only checks mean/low/high for mutual consistency when inferring one
        from the others, not when an author gives all three explicitly.
        """
        value = self.mean
        if self.clip_low is not None:
            value = max(value, self.clip_low)
        if self.clip_high is not None:
            value = min(value, self.clip_high)
        return value

    def quantile(self, q: float) -> float:
        """The ``q``-quantile of the *truncated* distribution (ADR-007 §3):
        ``inv_cdf(Phi(a) + q * (Phi(b) - Phi(a)))`` -- always inside the clip
        bounds, and honoring a one-sided clip."""
        _check_quantile(q)
        normal = NormalDist(self.mean, self.sd)
        lo = 0.0 if self.clip_low is None else normal.cdf(self.clip_low)
        hi = 1.0 if self.clip_high is None else normal.cdf(self.clip_high)
        p = lo + q * (hi - lo)
        # inv_cdf rejects exactly 0 and 1; those quantiles are the bounds
        # themselves (or unbounded, which a resolved row never is).
        if p <= 0.0:
            if self.clip_low is None:
                raise DistributionError("truncated_normal: quantile(0) is unbounded")
            return self.clip_low
        if p >= 1.0:
            if self.clip_high is None:
                raise DistributionError("truncated_normal: quantile(1) is unbounded")
            return self.clip_high
        value = normal.inv_cdf(p)
        # Guard float round-off at the edges.
        if self.clip_low is not None:
            value = max(value, self.clip_low)
        if self.clip_high is not None:
            value = min(value, self.clip_high)
        return value


def _check_quantile(q: float) -> None:
    if not 0.0 <= q <= 1.0:
        raise DistributionError(f"quantile must be in [0, 1] (got {q})")


def parse_quantiles(text: str) -> tuple[float, ...]:
    """Parse ``--quantiles`` (ADR-007 §3): at least two comma-separated
    values in ``[0, 1]``, strictly increasing, excluding ``0.5`` -- the base
    member already covers the center. One quantile alone gives no low/high
    pair to compare."""
    try:
        values = tuple(float(part) for part in text.split(","))
    except ValueError as exc:
        raise DistributionError(f"quantiles: not a comma-separated list of numbers: {text!r}") from exc
    if len(values) < 2:
        raise DistributionError(f"quantiles: at least two values are required (got {text!r})")
    for q in values:
        _check_quantile(q)
        if q == 0.5:
            raise DistributionError(
                "quantiles: 0.5 is the base member's own center; omit it"
            )
    if any(b <= a for a, b in zip(values, values[1:], strict=False)):
        raise DistributionError(f"quantiles must be strictly increasing (got {text!r})")
    return values


def resolve_truncated_normal_bounds(
    mean: float | None, sd: float | None, low: float | None, high: float | None,
    *, cutoff: float = DEFAULT_TRUNCATED_NORMAL_CUTOFF_SD,
) -> tuple[float, float, float, float]:
    """Fill in whichever of (mean, sd, low, high) an author left blank, so a
    `truncated_normal` row is never actually unbounded on a side nobody
    specified (ADR-006) -- unlike `uniform`, mean/sd alone already fully
    describe a distribution, so any of the four may be inferred from the
    others rather than required outright:

    - ``mean`` missing: the midpoint of ``low`` and ``high`` (both required
      in that case).
    - ``sd`` missing: the distance from ``mean`` to whichever of
      ``low``/``high`` is given, divided by ``cutoff`` (if both are given
      with an explicit ``mean``, they must imply the same ``sd``).
    - ``low``/``high`` still missing after the above: ``mean -/+ cutoff * sd``.

    Raises ``DistributionError`` if that still isn't enough to resolve every
    value, or if ``low``/``high`` disagree about the implied ``sd``.
    """
    if mean is None:
        if low is None or high is None:
            raise DistributionError(
                "truncated_normal: vary_mean requires both vary_low and "
                "vary_high when omitted"
            )
        mean = (low + high) / 2

    if sd is None:
        candidates = []
        if low is not None:
            candidates.append((mean - low) / cutoff)
        if high is not None:
            candidates.append((high - mean) / cutoff)
        if not candidates:
            raise DistributionError(
                "truncated_normal: vary_sd requires vary_low or vary_high "
                "when omitted"
            )
        if len(candidates) == 2 and abs(candidates[0] - candidates[1]) > 1e-9:
            raise DistributionError(
                "truncated_normal: vary_low and vary_high aren't symmetric "
                "around vary_mean, so vary_sd can't be inferred from them -- "
                "specify vary_sd explicitly"
            )
        sd = candidates[0]

    if low is None:
        low = mean - cutoff * sd
    if high is None:
        high = mean + cutoff * sd
    return mean, sd, low, high


def member_rng(seed: int, member_index: int, row_name: str) -> random.Random:
    """An independent, reproducible RNG stream per (member, row name).

    Hashing the triple (rather than sharing one stream in call order) means
    adding or removing one row's variation never perturbs any other row's
    draws for any member (ADR-006 §3).
    """
    digest = hashlib.sha256(f"{seed}:{member_index}:{row_name}".encode()).digest()
    return random.Random(int.from_bytes(digest, "big"))
