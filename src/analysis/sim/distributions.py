"""ADR-006: ensemble variation distributions -- pure functions, no I/O.

Two families to start (`registries.ENSEMBLE_DISTRIBUTIONS`); a third
(triangular, lognormal, ...) is a one-line addition, not a redesign.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass

__all__ = [
    "DistributionError", "Uniform", "TruncatedNormal", "member_rng",
    "DEFAULT_TRUNCATED_NORMAL_CUTOFF_SD", "resolve_truncated_normal_bounds",
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
