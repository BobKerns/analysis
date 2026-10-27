"""Life-table lifespans (#222): an age at death drawn from a period life
table, shifted so its median is the age the author believes.

The table (aux kind ``life_table``) gives ``q_x``, the probability that
someone who has just turned ``x`` dies before turning ``x + 1``, by sex. The
adjustment is an **age set-forward** ``s``: the person is read from the table
as if ``s`` years older (negative is a set-back). The loader solves ``s`` so
the table's median, conditioned on being alive at the scenario start, is the
row's ``age_at_death``. A mortality multiplier was considered and dropped:
adult mortality follows the Gompertz law closely, under which multiplying
every death rate is the same as a set-forward, so with the median pinned it
changes almost nothing (see ADR-006 §3a).

Ages here are whole months since the birth month, as ``final_month_for_age``
counts them: month ``m`` is the month the person reaches ``m / 12`` years.
Within each year of age the hazard is constant (annual ``q`` becomes monthly
survival ``(1 - q) ** (1/12)``); a fractional set-forward interpolates ``q``
between the two ages it falls between. Pure functions, no I/O beyond the
intent's ``aux_table``.
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import date

from analysis.sim import registries
from analysis.sim.dates import parse_day, parse_month
from analysis.sim.distributions import DistributionError

__all__ = [
    "LifeTable", "LifeTableAge", "LifeTableError", "life_table_from_rows",
    "life_table_for", "life_table_in", "months_since_birth", "person_table", "table_file",
    "solve_set_forward", "equivalent_multiplier",
]

LIFE_TABLE_KIND = "life_table"

# The widest set-forward or set-back the solver tries. Past these the median
# is beyond what any reading of the table gives (a set-back of 100 years
# reads infant mortality for the rest of one's life).
SET_FORWARD_RANGE = (-100.0, 120.0)


class LifeTableError(ValueError):
    """A life-table lifespan the table can't produce."""


@dataclass(frozen=True)
class LifeTable:
    """One sex's ``q_x`` by whole age, from 0. Past the last age, ``q`` is 1."""

    q: tuple[float, ...]

    def at(self, age: float) -> float:
        """``q`` at a fractional age, linear between whole ages; 1 past the
        table, and the youngest row's before it."""
        if age < 0:
            return self.q[0]
        whole = math.floor(age)
        frac = age - whole
        here = self.q[whole] if whole < len(self.q) else 1.0
        if frac == 0:
            return here
        nxt = self.q[whole + 1] if whole + 1 < len(self.q) else 1.0
        return (1 - frac) * here + frac * nxt


def life_table_from_rows(rows: list[tuple[int, float]]) -> LifeTable:
    """A table from ``(age, q)`` rows, which must cover 0..n, each age once."""
    by_age = dict(rows)
    ages = sorted(by_age)
    if len(ages) != len(rows) or ages != list(range(len(ages))):
        raise LifeTableError("a life table's ages must run 0, 1, 2, ... once each, with no gaps")
    return LifeTable(tuple(by_age[a] for a in ages))


def table_file(name: str | None) -> str:
    """The aux file a life table comes from (#315): ``life_table.csv`` for
    the default, ``life_table__<name>.csv`` for a named one."""
    return f"{LIFE_TABLE_KIND}__{name}.csv" if name else f"{LIFE_TABLE_KIND}.csv"


def life_table_for(
    payloads: list[tuple[str, dict]], sex: str, on: str, name: str | None = None,
) -> LifeTable:
    """``sex``'s (``f``/``m``) table from one life table's aux payloads
    (``name``'s, for the message), each with its ``effective_date``: the
    vintage in effect on ``on`` (the latest on or before it, like every aux
    table), or the earliest if every vintage is later."""
    if not payloads:
        raise LifeTableError(f"no aux {table_file(name)} in this scenario's lineage")
    dates = sorted({d for d, _ in payloads})
    eligible = [d for d in dates if d <= on]
    effective = eligible[-1] if eligible else dates[0]
    key = registries.LIFE_TABLE_Q_COLUMNS[sex] + "_micros"
    return life_table_from_rows([
        (int(p["age"]), p[key] / 1_000_000) for d, p in payloads if d == effective
    ])


def life_table_in(
    con: sqlite3.Connection, sex: str, on: str, name: str | None = None,
) -> LifeTable:
    """``life_table_for`` over an intent's or run file's ``aux_table``: the
    default table, or the one ``name``s (#315)."""
    return life_table_for(
        [(d, json.loads(p)) for d, p in con.execute(
            "SELECT effective_date, payload_json FROM aux_table WHERE kind = ? AND series IS ?",
            (LIFE_TABLE_KIND, name or None),
        )],
        sex, on, name,
    )


def person_table(con: sqlite3.Connection, person: str) -> str | None:
    """The life table ``person``'s lifespan.csv row names (#315), or None for
    the default: no row, a blank cell, or a file from before the column."""
    columns = {row[1] for row in con.execute("PRAGMA table_info(person_lifespan)")}
    if "life_table" not in columns:
        return None
    found = con.execute(
        "SELECT life_table FROM person_lifespan WHERE person = ?", (person,)).fetchone()
    return (found[0] or None) if found else None


def months_since_birth(birthdate: str | date, month: str | date) -> int:
    """Whole months from the birth month to ``month``'s month."""
    born = birthdate if isinstance(birthdate, date) else parse_day(birthdate)
    at = month if isinstance(month, date) else parse_month(month)
    return (at.year - born.year) * 12 + at.month - born.month


@dataclass
class _Curve:
    """Conditional death probabilities by month: ``deaths[k]`` is the chance
    the final month is ``start + k``, given alive in month ``start``."""

    start: int
    cdf: list[float]


def _build_curve(table: LifeTable, start: int, set_forward: float) -> _Curve:
    """The conditional distribution of the final month, from month
    ``start`` (months since birth) to the last month at
    ``registries.MAX_AGE_AT_DEATH``, which takes whatever probability remains."""
    last = round(registries.MAX_AGE_AT_DEATH * 12)
    cdf: list[float] = []
    alive = 1.0
    cache: dict[int, float] = {}
    for m in range(start, last):
        year = m // 12
        p = cache.get(year)
        if p is None:
            q = min(1.0, max(0.0, table.at(year + set_forward)))
            p = cache[year] = (1.0 - q) ** (1 / 12) if q < 1 else 0.0
        alive *= p
        cdf.append(1.0 - alive)
    cdf.append(1.0)
    return _Curve(start, cdf)


def _median_months(curve: _Curve) -> float:
    """The continuous median in months since birth: deaths spread evenly
    through each month, so the median month (the discrete one) holds the
    point where the CDF crosses one half."""
    prev = 0.0
    for k, f in enumerate(curve.cdf):
        if f >= 0.5:
            within = (0.5 - prev) / (f - prev) if f > prev else 0.0
            return curve.start + k + within
        prev = f
    return curve.start + len(curve.cdf)


def solve_set_forward(table: LifeTable, start: int, median_months: int) -> float:
    """The set-forward (years) that puts the conditional median final month
    at ``median_months``. Solved so the continuous median falls mid-month,
    making that month the discrete median. Raises ``LifeTableError`` when
    no set-forward in ``SET_FORWARD_RANGE`` reaches it."""
    if median_months < start:
        raise LifeTableError("its median falls before the scenario starts")
    target = median_months + 0.5
    lo, hi = SET_FORWARD_RANGE
    # A larger set-forward means more deaths sooner: the median falls.
    if _median_months(_build_curve(table, start, hi)) > target:
        raise LifeTableError("its median is too soon for the table to reach")
    if _median_months(_build_curve(table, start, lo)) < target:
        raise LifeTableError("its median is too late for the table to reach")
    for _ in range(60):
        mid = (lo + hi) / 2
        if _median_months(_build_curve(table, start, mid)) > target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def equivalent_multiplier(table: LifeTable, start: int, median_months: int) -> float | None:
    """The mortality multiplier (every ``q`` times it, capped at 1) that
    gives the same median with no set-forward: what an underwriter's table
    rating would be, for reference only. ``None`` when none in 1%..10,000% does."""
    target = median_months + 0.5

    def median(m: float) -> float:
        return _median_months(_build_curve(LifeTable(tuple(min(1.0, q * m) for q in table.q)), start, 0.0))

    lo, hi = 0.01, 100.0
    if not median(hi) <= target <= median(lo):
        return None
    for _ in range(50):
        mid = math.sqrt(lo * hi)
        if median(mid) > target:
            lo = mid
        else:
            hi = mid
    return math.sqrt(lo * hi)


@dataclass
class LifeTableAge:
    """A ``life_table`` lifespan's distribution over ages at death (#222),
    in years, each a whole month: the person's table, read ``set_forward``
    years older, conditioned on being alive in month ``start`` (months since
    birth). Exposes what ``_resolve_value`` and ``moments`` use."""

    table: LifeTable
    start: int
    set_forward: float
    _curve: _Curve = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._curve = _build_curve(self.table, self.start, self.set_forward)

    @property
    def low(self) -> float:
        return self.start / 12

    @property
    def high(self) -> float:
        return (self._curve.start + len(self._curve.cdf) - 1) / 12

    def quantile(self, q: float) -> float:
        """The age (whole months, in years) of the first month by which at
        least a ``q`` share have died."""
        if not 0.0 <= q <= 1.0:
            raise DistributionError(f"quantile {q} must be in [0, 1]")
        cdf = self._curve.cdf
        lo, hi = 0, len(cdf) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if cdf[mid] >= q:
                hi = mid
            else:
                lo = mid + 1
        return (self._curve.start + lo) / 12

    def value_at(self, u: float) -> float:
        return self.quantile(min(1.0, max(0.0, u)))

    def draw(self, rng: random.Random) -> float:
        return self.quantile(rng.random())

    def central(self) -> float:
        return self.quantile(0.5)

    def monthly_cdf(self) -> tuple[int, tuple[float, ...]]:
        """The month (since birth) the curve starts at, and the chance the
        final month is that month or earlier, month by month from it to the
        last, where it reaches 1 (#312's charts)."""
        return self._curve.start, tuple(self._curve.cdf)
