"""Yearly measures over a run's monthly series (#305), for guard rails and,
later, for responsive strategies' triggers (#326).

Everything here is numpy over arrays shaped ``(members, months)``: one row
per run (a single run is one row), one column per month end. Reports call it
once over every member of an ensemble; the engine can call it on the months a
run has stepped so far. Each function is **causal**: a year's value depends
only on months up to that year's end, so evaluating the months through some
December gives the same years as evaluating the whole run (a mid-year prefix
gives the year so far). ``tests/test_measures.py`` checks this.

The yearly view follows the published guidelines it serves (Bengen's 4%
rule, Guyton-Klinger's guardrails): calendar years, a year's flow against
a threshold that is a fixed amount, CPI-indexed, or a share of the portfolio
at the start of the year (its value at the previous December's end).
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Literal

import numpy as np

__all__ = [
    "Side", "YearAxis", "RailYears", "Episode",
    "year_axis", "yearly_sums", "year_start_values", "value_before_window",
    "dietz_returns", "evaluate", "past", "share_past", "ever_past", "rms_overshoot",
    "episodes",
]

type Side = Literal["above", "below"]


def _rows(values: np.ndarray) -> np.ndarray:
    """``values`` as float rows: a 1-d series becomes one row."""
    return np.atleast_2d(np.asarray(values, dtype=float))


@dataclass(frozen=True)
class YearAxis:
    """Which calendar year each month falls in."""

    years: np.ndarray  # the distinct years, in order
    of_month: np.ndarray  # each month's position in ``years``
    last_month: np.ndarray  # each year's last month (a position in the months)


def year_axis(years_of_months: np.ndarray) -> YearAxis:
    """The year axis of months whose calendar years are ``years_of_months``
    (ascending)."""
    month_years = np.asarray(years_of_months, dtype=int)
    years, of_month = np.unique(month_years, return_inverse=True)
    last = np.searchsorted(month_years, years, side="right") - 1
    return YearAxis(years=years, of_month=of_month, last_month=last)


def yearly_sums(values: np.ndarray, axis: YearAxis) -> np.ndarray:
    """Each row's months summed by calendar year: ``(rows, years)``."""
    rows = _rows(values)
    if not len(axis.years):
        return np.zeros((rows.shape[0], 0))
    # A year's months are contiguous: each sum runs from its first month.
    firsts = np.concatenate(([0], axis.last_month[:-1] + 1))
    return np.add.reduceat(rows, firsts, axis=1).astype(float)


def year_start_values(values: np.ndarray, axis: YearAxis) -> np.ndarray:
    """For each month, ``values`` at the end of the previous year: the month
    whose value a year-start threshold reads. The run's first year, with no
    previous December, takes the first month's value. ``(rows, months)``."""
    rows = _rows(values)
    prior_end = np.concatenate(([0], axis.last_month[:-1]))
    return rows[:, prior_end[axis.of_month]]


def value_before_window(values: np.ndarray, active: np.ndarray) -> np.ndarray:
    """Each row's value at the month before its window's first month (the
    first month itself when the window starts with the months); NaN for a
    row whose window is empty. ``(rows,)``."""
    rows = _rows(values)
    on = np.atleast_2d(np.asarray(active, dtype=bool))
    first = np.argmax(on, axis=1)
    at = np.clip(first - 1, 0, None)
    picked = rows[np.arange(rows.shape[0]), at]
    return np.where(on.any(axis=1), picked, np.nan)


def dietz_returns(values: np.ndarray, net_out: np.ndarray, axis: YearAxis) -> np.ndarray:
    """Each year's return on a portfolio holding ``values`` at month ends,
    with ``net_out`` taken out over the months (Dietz's method: flows count
    as if made mid-year). ``(rows, years)``; 0 where the capital is nil.

    The first year starts from the first month's end, the same simplification
    as ``year_start_values``."""
    rows = _rows(values)
    out = yearly_sums(net_out, axis)
    end = rows[:, axis.last_month]
    start = np.concatenate((rows[:, :1], end[:, :-1]), axis=1)
    capital = start - out / 2
    with np.errstate(divide="ignore", invalid="ignore"):
        r = (end - start + out) / capital
    return np.where(capital > 0, r, 0.0)


@dataclass(frozen=True)
class RailYears:
    """One rail evaluated year by year: arrays ``(rows, years)``, NaN in a
    year the rail doesn't cover.

    ``margin`` is the rail's distance from being crossed, so negative is past
    it, whichever way the rail faces. ``cumulative`` sums the margins since
    the rail began: room banked (positive) or overdrawn (negative).
    ``carried`` is ``cumulative`` carried forward each year at the
    portfolio's own return, what the overdraft (or the room) would have grown
    to: a first-order view of what the plain sum leaves out."""

    years: np.ndarray
    measure: np.ndarray
    threshold: np.ndarray
    margin: np.ndarray
    cumulative: np.ndarray
    carried: np.ndarray


def evaluate(
    measure: np.ndarray, allowance: np.ndarray, factor: np.ndarray, active: np.ndarray,
    axis: YearAxis, side: Side, returns: np.ndarray | None = None,
) -> RailYears:
    """A rail year by year.

    ``measure`` and ``allowance`` are each month's flow and the rail's
    threshold for that month, both nominal; ``factor`` converts a month's
    nominal dollars into the dollars shown (1 for nominal); ``active`` says
    which months the rail covers. All ``(rows, months)``. A year counts the
    months the rail covers in it, against that part of the year's threshold.
    ``returns`` (``(rows, years)``, in the dollars shown) carries the
    cumulative margin forward; without it ``carried`` equals ``cumulative``.
    """
    on = np.atleast_2d(np.asarray(active, dtype=bool))
    scale = _rows(factor) * on
    m = yearly_sums(_rows(measure) * scale, axis)
    t = yearly_sums(_rows(allowance) * scale, axis)
    covered = yearly_sums(on, axis) > 0
    margin = t - m if side == "above" else m - t
    margin = np.where(covered, margin, np.nan)
    cumulative = np.where(covered, np.cumsum(np.where(covered, margin, 0.0), axis=1), np.nan)
    carried = np.full_like(margin, np.nan)
    running = np.zeros(margin.shape[0])
    started = np.zeros(margin.shape[0], dtype=bool)
    for y in range(margin.shape[1]):
        growth = 1.0 if returns is None else 1.0 + returns[:, y]
        running = np.where(started, running * growth, running)
        here = covered[:, y]
        running = np.where(here, running + np.where(here, margin[:, y], 0.0), running)
        started |= here
        carried[:, y] = np.where(here, running, np.nan)
    return RailYears(
        years=axis.years,
        measure=np.where(covered, m, np.nan),
        threshold=np.where(covered, t, np.nan),
        margin=margin, cumulative=cumulative, carried=carried,
    )


def past(margin: np.ndarray) -> np.ndarray:
    """Where a margin is past its rail; False where the rail doesn't apply."""
    values = np.asarray(margin, dtype=float)
    return np.where(np.isnan(values), False, values < 0)


def share_past(margin: np.ndarray) -> np.ndarray:
    """Per year (column), the share of rows past the rail among the rows it
    applies to; NaN where it applies to none."""
    values = _rows(margin)
    applies = ~np.isnan(values)
    count = applies.sum(axis=0)
    with np.errstate(invalid="ignore"):
        return np.where(count > 0, past(values).sum(axis=0) / np.maximum(count, 1), np.nan)


def ever_past(margin: np.ndarray) -> np.ndarray:
    """Per row and year, whether the row has been past the rail in any year
    so far. ``(rows, years)``."""
    return np.logical_or.accumulate(past(_rows(margin)), axis=1)


def rms_overshoot(margin: np.ndarray, axis: int | None = None) -> np.ndarray | float:
    """The root mean square of how far past the rail each value is, counting
    values inside it as zero and leaving out years it doesn't apply to: the
    square root of the second lower partial moment. Squaring weighs a large
    miss more than several small ones."""
    values = np.asarray(margin, dtype=float)
    over = np.where(np.isnan(values), np.nan, np.clip(-values, 0.0, None))
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", "Mean of empty slice", RuntimeWarning)
        return np.sqrt(np.nanmean(over**2, axis=axis))


@dataclass(frozen=True)
class Episode:
    """A run of consecutive years past a rail."""

    first: int
    last: int
    worst_year: int
    worst: float  # the most negative margin in it

    @property
    def years(self) -> int:
        return self.last - self.first + 1


def episodes(margin: np.ndarray, years: np.ndarray) -> list[Episode]:
    """One row's runs of consecutive years past the rail."""
    values = np.asarray(margin, dtype=float).ravel()
    is_past = past(values)
    found: list[Episode] = []
    start: int | None = None
    for i in range(len(values) + 1):
        if i < len(values) and is_past[i]:
            if start is None:
                start = i
            continue
        if start is not None:
            span = values[start:i]
            worst = start + int(np.argmin(span))
            found.append(Episode(int(years[start]), int(years[i - 1]),
                                 int(years[worst]), float(values[worst])))
            start = None
    return found
