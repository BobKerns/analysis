"""ADR-006: ensemble variation distributions -- pure functions, no I/O.

Two families to start (`registries.ENSEMBLE_DISTRIBUTIONS`); a third
(triangular, lognormal, ...) is a one-line addition, not a redesign.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass

__all__ = ["DistributionError", "Uniform", "TruncatedNormal", "member_rng"]


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


def member_rng(seed: int, member_index: int, row_name: str) -> random.Random:
    """An independent, reproducible RNG stream per (member, row name).

    Hashing the triple (rather than sharing one stream in call order) means
    adding or removing one row's variation never perturbs any other row's
    draws for any member (ADR-006 §3).
    """
    digest = hashlib.sha256(f"{seed}:{member_index}:{row_name}".encode()).digest()
    return random.Random(int.from_bytes(digest, "big"))
