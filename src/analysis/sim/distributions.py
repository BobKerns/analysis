"""ADR-006: ensemble variation distributions -- pure functions, no I/O.

Three families (`registries.ENSEMBLE_DISTRIBUTIONS`): `uniform`,
`truncated_normal` (optionally skewed / heavy-tailed, #31) and `triangular`.
Every family exposes ``quantile(q)``, which sensitivity analysis (ADR-007)
and correlated draws (#30) are built on.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, field
from statistics import NormalDist

__all__ = [
    "DistributionError", "Uniform", "TruncatedNormal", "Triangular", "Distribution",
    "Moments", "moments", "member_rng", "tail_rng",
    "DEFAULT_TRUNCATED_NORMAL_CUTOFF_SD", "resolve_truncated_normal_bounds",
    "parse_quantiles",
]

DEFAULT_TRUNCATED_NORMAL_CUTOFF_SD = 3.0
"""How far (in standard normal scores) a `truncated_normal` bound nobody
specified is placed (ADR-006) -- the classic three-sigma range, ~99.7%
coverage, so the clip only trims the extreme tails."""


class DistributionError(RuntimeError):
    """A distribution's parameters make no valid draw possible."""


_STANDARD = NormalDist()


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

    def value_at(self, u: float) -> float:
        """The value a correlated draw's probability ``u`` maps to (ADR-008)."""
        return self.quantile(_clamp_unit(u))

    def value_at_int(self, u: float) -> int:
        """A date as a correlated draw resolves it: each whole day in
        ``[round(low), round(high)]`` equally likely, matching ``draw_int``
        (not ``quantile_int``'s rounding, which gives the end days half
        weight)."""
        low, high = round(self.low), round(self.high)
        return min(high, low + int(_clamp_unit(u) * (high - low + 1)))


def _shape(z: float, skew: float, tailweight: float) -> float:
    """Sinh-arcsinh transform of a standard normal score (Jones & Pewsey
    2009): identity at ``skew=0, tailweight=1``. An extreme shape that
    overflows is a ``DistributionError``, not a crash."""
    try:
        return math.sinh((math.asinh(z) + skew) / tailweight)
    except OverflowError:
        raise DistributionError(_EXTREME_SHAPE) from None


def _unshape(x: float, skew: float, tailweight: float) -> float:
    """Inverse of ``_shape``: the standard normal score of a standardized value."""
    try:
        return math.sinh(tailweight * math.asinh(x) - skew)
    except OverflowError:
        raise DistributionError(_EXTREME_SHAPE) from None


_EXTREME_SHAPE = (
    "truncated_normal: vary_skew/vary_tailweight are too extreme to compute "
    "(keep skew within about ±5 and tail weight within about 0.2 to 5)"
)


def _check_finite(**values: float | None) -> None:
    """Reject inf/NaN parameters (a CSV ``inf`` parses as a float) up front."""
    for name, value in values.items():
        if value is not None and not math.isfinite(value):
            raise DistributionError(f"truncated_normal: {name} must be a finite number (got {value})")


@dataclass(frozen=True)
class TruncatedNormal:
    """A normal -- or, with ``skew``/``tailweight``, a sinh-arcsinh normal
    (Jones & Pewsey 2009) -- restricted to ``[clip_low, clip_high]`` (#31).

    Specified by its **median**: the median of the *truncated* distribution,
    so the base case (``central()``, ADR-006 §9) is exactly the authored
    value even when the bounds are asymmetric. ``sd`` is the scale of the
    underlying distribution before clipping. ``skew`` (0 = symmetric, > 0
    longer right tail) and ``tailweight`` (1 = normal, < 1 heavier tails,
    > 1 lighter) shape it; at their defaults this is exactly a normal.

    The underlying variable is ``location + sd * sinh((asinh(Z) + skew) /
    tailweight)`` with ``Z ~ N(0, 1)``; ``location`` is solved so that the
    truncated median lands on ``median``. CDF and quantile are closed-form,
    so draws use inverse-CDF sampling: exact for any shape, never a retry.
    """

    median: float
    sd: float
    clip_low: float | None = None
    clip_high: float | None = None
    skew: float = 0.0
    tailweight: float = 1.0
    location: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        _check_finite(vary_median=self.median, vary_sd=self.sd, vary_low=self.clip_low,
                      vary_high=self.clip_high, vary_skew=self.skew,
                      vary_tailweight=self.tailweight)
        if not self.sd > 0:
            raise DistributionError(f"truncated_normal: vary_sd must be positive (got {self.sd})")
        if not self.tailweight > 0:
            raise DistributionError(
                f"truncated_normal: vary_tailweight must be positive (got {self.tailweight})"
            )
        if self.clip_low is not None and self.clip_high is not None and self.clip_low >= self.clip_high:
            raise DistributionError("truncated_normal: vary_low must be below vary_high")
        if (self.clip_low is not None and self.median <= self.clip_low) or (
            self.clip_high is not None and self.median >= self.clip_high
        ):
            raise DistributionError(
                "truncated_normal: vary_median must lie strictly between vary_low and "
                "vary_high (a median can't sit on a bound; use vary_skew for a "
                "one-sided spread)"
            )
        object.__setattr__(self, "location", self._solve_location())

    # -- the underlying (untruncated) distribution at a given location --------

    def _z(self, x: float | None, location: float, default: float) -> float:
        """The standard normal score of ``x`` (``default`` for a missing bound)."""
        if x is None:
            return default
        return _unshape((x - location) / self.sd, self.skew, self.tailweight)

    def _truncated_z(self, q: float, location: float) -> float | None:
        """The standard normal score of the truncated ``q``-quantile, or
        ``None`` when the window holds no representable probability (it sits
        so far into a tail that the CDF has saturated). Works from whichever
        tail the window is in -- survival probabilities for a window above
        the center -- so a far-out window keeps its precision."""
        za = self._z(self.clip_low, location, -math.inf)
        zb = self._z(self.clip_high, location, math.inf)
        if za >= 0:  # wholly above the center: use the upper tail
            sa, sb = _STANDARD.cdf(-za), (0.0 if zb == math.inf else _STANDARD.cdf(-zb))
            mass = sa - sb
            if mass <= 0:
                return None
            s_q = sa - q * mass
            if s_q <= 0:
                return zb
            return za if s_q >= 1 else -_STANDARD.inv_cdf(s_q)
        pa = 0.0 if za == -math.inf else _STANDARD.cdf(za)
        pb = 1.0 if zb == math.inf else _STANDARD.cdf(zb)
        mass = pb - pa
        if mass <= 0:
            return None
        p = pa + q * mass
        if p <= 0:
            return za
        return zb if p >= 1 else _STANDARD.inv_cdf(p)

    def _truncated_quantile(self, q: float, location: float) -> float | None:
        z = self._truncated_z(q, location)
        if z is None:
            return None
        if z == -math.inf or z == math.inf:
            raise DistributionError(f"truncated_normal: quantile({q}) is unbounded")
        value = location + self.sd * _shape(z, self.skew, self.tailweight)
        # Guard float round-off at the edges.
        if self.clip_low is not None:
            value = max(value, self.clip_low)
        if self.clip_high is not None:
            value = min(value, self.clip_high)
        return value

    def _solve_location(self) -> float:
        """The location whose *truncated* median is ``median``.

        For the normal and lighter tails (``tailweight >= 1``) the truncated
        median rises steadily with the location, from ``clip_low`` to
        ``clip_high``. With heavy tails it needn't: far out, the window sits
        in a tail whose shape no longer changes much, so some medians are
        unreachable and the relationship can even turn back. So: scan a wide
        range of locations for the real crossings, take the one nearest the
        untruncated location (the least-distorted fit), and bisect it; if
        there is none, say which medians this shape can reach.
        """
        untruncated = self.median - self.sd * _shape(0.0, self.skew, self.tailweight)
        if self.clip_low is None and self.clip_high is None:
            return untruncated

        def gap(location: float) -> float | None:
            value = self._truncated_quantile(0.5, location)
            return None if value is None else value - self.median

        width = 0.0
        if self.clip_low is not None and self.clip_high is not None:
            width = self.clip_high - self.clip_low
        span = max(50 * self.sd, 10 * width)
        steps = 400
        brackets: list[tuple[float, float]] = []
        gaps: list[float | None] = []
        for _ in range(8):  # widen the scan (x4 each time) before giving up
            grid = [untruncated + span * (2 * i / steps - 1) for i in range(steps + 1)]
            gaps = [gap(x) for x in grid]
            brackets = [
                (grid[i], grid[i + 1]) for i in range(steps)
                if (g0 := gaps[i]) is not None and (g1 := gaps[i + 1]) is not None
                and (g0 == 0 or g0 * g1 < 0)
            ]
            if brackets or gaps[0] is None or gaps[-1] is None:
                break  # found, or the scan already runs past what floats can represent
            span *= 4
        if not brackets:
            reached = [g + self.median for g in gaps if g is not None]
            if not reached:
                # Every sampled location left the window with no representable
                # probability (#35): nothing was reachable at all, so there's
                # no range to report -- still a clean validation error.
                raise DistributionError(
                    f"truncated_normal: vary_median {self.median:g} can't be placed: at every "
                    "location tried, the bounds hold no representable probability for this "
                    "vary_sd and shape; widen the bounds, change vary_sd, or bring vary_skew "
                    "toward 0 and vary_tailweight toward 1"
                )
            if self.tailweight >= 1:
                # Normal or lighter tails reach every interior median in
                # principle, so this is numeric: the fit would need the
                # distribution centered so far from the window (dozens of
                # standard scores) that its probability there underflows --
                # a median hugging a bound relative to sd.
                raise DistributionError(
                    f"truncated_normal: vary_median {self.median:g} is too close to a bound "
                    f"for this vary_sd (it can be placed between {min(reached):g} and "
                    f"{max(reached):g}); reduce vary_sd or move the median inward"
                )
            raise DistributionError(
                f"truncated_normal: vary_median {self.median:g} isn't reachable with this "
                f"vary_skew/vary_tailweight inside vary_low..vary_high (its truncated "
                f"median only reaches {min(reached):g} to {max(reached):g}); move the "
                "median, widen the bounds, or bring vary_skew toward 0 or vary_tailweight "
                "toward 1 (lower tail weight means heavier tails, which make this worse)"
            )
        lo, hi = min(brackets, key=lambda b: abs((b[0] + b[1]) / 2 - untruncated))
        g_lo = gap(lo)
        assert g_lo is not None  # lo is a bracket end, where the gap was defined
        if g_lo == 0:
            return lo
        for _ in range(200):
            mid = (lo + hi) / 2
            if mid in (lo, hi):
                break
            g_mid = gap(mid)
            if g_mid is None or g_mid == 0:
                return mid
            if (g_mid < 0) == (g_lo < 0):
                lo, g_lo = mid, g_mid
            else:
                hi = mid
        return (lo + hi) / 2

    # -- the truncated distribution ------------------------------------------

    def draw(self, rng: random.Random) -> float:
        """An inverse-CDF draw: exact for any shape, no rejection loop."""
        u = rng.random()
        while u == 0.0:  # quantile(0) is a bound; keep draws in the open interval
            u = rng.random()
        return self.quantile(u)

    def central(self) -> float:
        """The authored median -- exactly the truncated distribution's median
        (ADR-006 §9: the recipe's deterministic member 0)."""
        return self.median

    def quantile(self, q: float) -> float:
        """The ``q``-quantile of the *truncated* distribution (ADR-007 §3):
        always inside the clip bounds, and honoring a one-sided clip."""
        _check_quantile(q)
        value = self._truncated_quantile(q, self.location)
        if value is None:  # pragma: no cover - the solved location always holds mass
            raise DistributionError("truncated_normal: the bounds hold no probability")
        return value

    def value_at(self, u: float) -> float:
        """The value a correlated draw's probability ``u`` maps to (ADR-008)."""
        return self.quantile(_clamp_unit(u))


@dataclass(frozen=True)
class Triangular:
    """Linearly rising from ``low`` to a peak at ``mode``, then falling to
    ``high`` (#31): bounded, skewed whenever the mode is off-center, and
    easy to state ("probably around X, could be as high as Y"). Valid for a
    date target, like ``Uniform``. Its center is its median."""

    low: float
    mode: float
    high: float

    def __post_init__(self) -> None:
        if not self.low < self.high:
            raise DistributionError("triangular: vary_low must be below vary_high")
        if not self.low <= self.mode <= self.high:
            raise DistributionError("triangular: vary_mode must lie within vary_low..vary_high")

    def quantile(self, q: float) -> float:
        _check_quantile(q)
        width = self.high - self.low
        at_mode = (self.mode - self.low) / width
        if q <= at_mode:
            return self.low + math.sqrt(q * width * (self.mode - self.low))
        return self.high - math.sqrt((1 - q) * width * (self.high - self.mode))

    def quantile_int(self, q: float) -> int:
        """``quantile`` rounded to a day ordinal, for a date target."""
        return round(self.quantile(q))

    def draw(self, rng: random.Random) -> float:
        return self.quantile(rng.random())

    def draw_int(self, rng: random.Random) -> int:
        return round(self.draw(rng))

    def value_at(self, u: float) -> float:
        """The value a correlated draw's probability ``u`` maps to (ADR-008)."""
        return self.quantile(_clamp_unit(u))

    def value_at_int(self, u: float) -> int:
        """A date as a correlated draw resolves it, rounded like ``draw_int``."""
        return round(self.value_at(u))

    def central(self) -> float:
        """The median (ADR-006 §9's base case), not the mode."""
        return self.quantile(0.5)

    def central_int(self) -> int:
        return round(self.central())


Distribution = Uniform | TruncatedNormal | Triangular


@dataclass(frozen=True)
class Moments:
    """A distribution's implied summary, for showing authors what their
    parameters mean (#31)."""

    mean: float
    sd: float
    skewness: float
    excess_kurtosis: float


def moments(
    dist: Distribution, points: int = 4_000, *, whole_days: bool = False, rounded: bool = False,
) -> Moments:
    """Mean, SD, skewness and excess kurtosis, integrated numerically over
    the quantile function (midpoint rule) -- one method for every family,
    including truncated and shaped ones with no tidy closed form.

    ``whole_days`` describes a date target as it is actually drawn, in whole
    days rather than a continuum: a ``Uniform`` date gives each day in
    ``[round(low), round(high)]`` equal weight (``draw_int``), any other
    family rounds a continuous draw (``draw_int``/``quantile_int``). It
    matters for a short span -- two days drawn equally have SD 0.5 day,
    not a continuous uniform's 0.29.

    ``rounded`` describes an amount target as stored: resolved amounts are
    rounded to whole cents or micros (``resolve_member``), whatever the
    family. Negligible for any realistic spread, but exact."""
    if whole_days and isinstance(dist, Uniform):
        values = [float(day) for day in range(round(dist.low), round(dist.high) + 1)]
    else:
        values = [dist.quantile((i + 0.5) / points) for i in range(points)]
        if whole_days or rounded:
            values = [float(round(v)) for v in values]
    count = len(values)
    mean = math.fsum(values) / count
    centered = [v - mean for v in values]
    variance = math.fsum(c * c for c in centered) / count
    sd = math.sqrt(variance)
    if sd == 0:
        return Moments(mean, 0.0, 0.0, 0.0)
    skewness = math.fsum(c**3 for c in centered) / count / sd**3
    kurtosis = math.fsum(c**4 for c in centered) / count / variance**2 - 3.0
    return Moments(mean, sd, skewness, kurtosis)


def _clamp_unit(u: float) -> float:
    """A copula probability, kept inside [0, 1] against float round-off."""
    return min(1.0, max(0.0, u))


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
    median: float | None, sd: float | None, low: float | None, high: float | None,
    *, skew: float = 0.0, tailweight: float = 1.0,
    cutoff: float = DEFAULT_TRUNCATED_NORMAL_CUTOFF_SD,
) -> tuple[float, float, float, float]:
    """Fill in whichever of (median, sd, low, high) an author left blank, so a
    `truncated_normal` row is never actually unbounded on a side nobody
    specified (ADR-006, #31):

    - ``median`` missing: the midpoint of ``low`` and ``high`` (both required
      in that case).
    - ``sd`` missing: the distance from ``median`` to whichever of
      ``low``/``high`` is given, divided by ``cutoff`` (if both are given
      with an explicit ``median``, they must imply the same ``sd``). Only
      for the default shape: with ``skew`` or ``tailweight`` set, the
      distance to a bound no longer says what the scale is, so ``sd`` must
      be given.
    - ``low``/``high`` still missing after the above: the untruncated
      distribution's ``-/+ cutoff`` standard-normal-score points around the
      median -- ``median -/+ cutoff * sd`` for the default shape.

    Raises ``DistributionError`` if that still isn't enough to resolve every
    value, or if ``low``/``high`` disagree about the implied ``sd``.
    """
    _check_finite(vary_median=median, vary_sd=sd, vary_low=low, vary_high=high,
                  vary_skew=skew, vary_tailweight=tailweight)
    if not tailweight > 0:
        raise DistributionError(
            f"truncated_normal: vary_tailweight must be positive (got {tailweight})"
        )
    if median is None:
        if low is None or high is None:
            raise DistributionError(
                "truncated_normal: vary_median requires both vary_low and "
                "vary_high when omitted"
            )
        median = (low + high) / 2

    if sd is None:
        if skew != 0.0 or tailweight != 1.0:
            raise DistributionError(
                "truncated_normal: vary_sd is required with vary_skew or "
                "vary_tailweight (a bound no longer implies the scale)"
            )
        candidates = []
        if low is not None:
            candidates.append((median - low) / cutoff)
        if high is not None:
            candidates.append((high - median) / cutoff)
        if not candidates:
            raise DistributionError(
                "truncated_normal: vary_sd requires vary_low or vary_high "
                "when omitted"
            )
        if len(candidates) == 2 and abs(candidates[0] - candidates[1]) > 1e-9:
            raise DistributionError(
                "truncated_normal: vary_low and vary_high aren't symmetric "
                "around vary_median, so vary_sd can't be inferred from them -- "
                "specify vary_sd explicitly"
            )
        sd = candidates[0]

    center = median - sd * _shape(0.0, skew, tailweight)  # untruncated location
    if low is None:
        low = center + sd * _shape(-cutoff, skew, tailweight)
    if high is None:
        high = center + sd * _shape(cutoff, skew, tailweight)
    return median, sd, low, high


def member_rng(seed: int, member_index: int, row_name: str) -> random.Random:
    """An independent, reproducible RNG stream per (member, row name).

    Hashing the triple (rather than sharing one stream in call order) means
    adding or removing one row's variation never perturbs any other row's
    draws for any member (ADR-006 §3).
    """
    digest = hashlib.sha256(f"{seed}:{member_index}:{row_name}".encode()).digest()
    return random.Random(int.from_bytes(digest, "big"))


def tail_rng(seed: int, member_index: int, root: str) -> random.Random:
    """The stream for a t-copula tree's shared scale (ADR-008 §5), keyed by
    the tree's root. The root's own name already keys its noise, so this
    hashes a different string: one starting ``tail:``, which no
    ``member_rng`` key can equal, since those start with the integer seed."""
    digest = hashlib.sha256(f"tail:{seed}:{member_index}:{root}".encode()).digest()
    return random.Random(int.from_bytes(digest, "big"))
