"""Each person's age-at-death distribution, as numbers and as a chart (#312).

For every person with a sex, the life table (#222) gives the chance of dying
in each month from the scenario start on. A ``life_table`` lifespan reads the
table with the set-forward the loader solved, so the curve is the one the
ensemble draws from; any other person gets the plain table, with the age the
scenario assumes marked on it. With two or more such persons the household
curve is the chance that at least one is still alive, month by month: the
plan runs until the last death. The lifespans are taken as independent, as
the ensemble draws them unless a correlation row says otherwise.

Plain matplotlib figures, no pyplot or widgets, like ``ensemble_plots``: the
notebook shows them inline and ``sim person show --graph`` in the terminal.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import date

import matplotlib.colors as mcolors
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter, PercentFormatter

from analysis.sim.dates import parse_day, parse_month
from analysis.sim.ensemble import REPORT_PERCENTILES
from analysis.sim.life_table import LifeTableAge, LifeTableError, life_table_in, months_since_birth
from analysis.sim.plotting import FRAME_ALPHA
from analysis.sim.registries import LIFE_TABLE_DISTRIBUTION

__all__ = [
    "Household", "PersonLifespan", "household", "lifespan_figure", "person_lifespans",
]

@dataclass(frozen=True)
class PersonLifespan:
    """One person's age-at-death distribution from the scenario start.

    ``cdf[k]`` is the chance the person's final month is ``k`` months after
    the scenario's first month or earlier; ``dist`` answers quantiles in
    years of age. ``adjusted`` says the table is read with the lifespan's
    solved ``set_forward``; otherwise it is the plain table, and
    ``assumed_age`` is the scenario's fixed (or base-case) age, if any."""

    person: str
    display: str
    sex: str
    birthdate: date
    start: date
    end: date
    dist: LifeTableAge
    adjusted: bool
    set_forward: float
    assumed_age: float | None
    vary: str | None

    @property
    def cdf(self) -> tuple[float, ...]:
        return self.dist.monthly_cdf()[1]

    @property
    def age_now(self) -> float:
        """Age in years at the scenario start, in whole months."""
        return self.dist.start / 12

    @property
    def age_at_end(self) -> float:
        """Age in years at the end of the scenario's last month: where the
        charts stop, since nothing past it reaches the plan."""
        return (months_since_birth(self.birthdate, self.end) + 1) / 12

    def quantile(self, q: float) -> float:
        return self.dist.quantile(q)

    def yearly(self) -> list[tuple[int, float, float]]:
        """``(age, chance of dying at that age, chance of having died by
        its end)`` for each whole year of age from the current one: the
        chart's PDF and CDF, and ``sim person show``'s table."""
        first, cdf = self.dist.monthly_cdf()
        by_end: dict[int, float] = {}
        for k, f in enumerate(cdf):
            by_end[(first + k) // 12] = f  # the last month of each age wins
        rows: list[tuple[int, float, float]] = []
        prev = 0.0
        for age, f in by_end.items():
            rows.append((age, f - prev, f))
            prev = f
        return rows


def person_lifespans(con: sqlite3.Connection) -> tuple[list[PersonLifespan], list[str]]:
    """Every person with a sex in the intent (or run file) behind ``con``,
    in persons.csv order, and a note for each one with a sex that can't be
    drawn (no life table in the lineage, say)."""
    start_text, end_text = con.execute('SELECT start, "end" FROM scenario').fetchone()
    start, end = parse_month(start_text), parse_month(end_text)
    lifespans = {
        row[0]: tuple(row)[1:] for row in con.execute(
            "SELECT person, age_at_death, vary_distribution, life_table_set_forward "
            "FROM person_lifespan"
        )
    }
    found: list[PersonLifespan] = []
    notes: list[str] = []
    for person, display, birthdate, sex in map(tuple, con.execute(
        "SELECT id, display, birthdate, sex FROM person WHERE sex IS NOT NULL AND sex != '' "
        "ORDER BY rowid"
    )):
        if not birthdate:
            # A household pseudo-person may have a sex but no birthdate.
            notes.append(f"{person}: no birthdate, so no age to read the life table at")
            continue
        age, vary, set_forward = lifespans.get(person, (None, None, None))
        adjusted = vary == LIFE_TABLE_DISTRIBUTION and set_forward is not None
        try:
            table = life_table_in(con, sex, start_text)
            dist = LifeTableAge(table, months_since_birth(birthdate, start),
                                set_forward if adjusted else 0.0)
        except LifeTableError as e:
            notes.append(f"{person}: {e}")
            continue
        found.append(PersonLifespan(
            person=person, display=display, sex=sex, birthdate=parse_day(birthdate),
            start=start, end=end, dist=dist, adjusted=adjusted,
            set_forward=set_forward if adjusted else 0.0,
            assumed_age=None if adjusted else age, vary=vary,
        ))
    return found, notes


@dataclass(frozen=True)
class Household:
    """The chance, month by month from the scenario start, that each person,
    at least one, and all of them are alive, the lifespans independent."""

    start: date
    persons: tuple[PersonLifespan, ...]
    alive: tuple[tuple[float, ...], ...]
    any_alive: tuple[float, ...]
    all_alive: tuple[float, ...]

    def last_death_quantile(self, q: float) -> date:
        """The month by which a ``q`` share of households have seen their
        last death: where the chance someone is alive falls to ``1 - q``."""
        for k, p in enumerate(self.any_alive):
            if 1 - p >= q:
                return _add_months(self.start, k)
        return _add_months(self.start, len(self.any_alive) - 1)


def household(persons: list[PersonLifespan]) -> Household:
    """``persons``' joint survival, all from the same scenario start."""
    length = max(len(p.cdf) for p in persons)
    alive = tuple(
        tuple(1 - (p.cdf[k] if k < len(p.cdf) else 1.0) for k in range(length))
        for p in persons
    )
    return Household(
        start=persons[0].start, persons=tuple(persons), alive=alive,
        any_alive=tuple(1 - math.prod(1 - a[k] for a in alive) for k in range(length)),
        all_alive=tuple(math.prod(a[k] for a in alive) for k in range(length)),
    )


def _add_months(d: date, months: int) -> date:
    total = d.year * 12 + d.month - 1 + months
    return date(total // 12, total % 12 + 1, 1)


def _decimal_year(d: date) -> float:
    return d.year + (d.month - 1) / 12


# ---------- the chart ----------

_BASE = mcolors.to_rgb("steelblue")


def _band_colors() -> list[tuple[tuple[float, float], tuple[float, float, float], str]]:
    """``((low q, high q), color, label)`` for each fan band, outermost
    first: the ensemble fan chart's bands (5–95%, 20–80%, 40–60%) and its
    colors, opaque blends of steelblue with white, darker inward."""
    edges = REPORT_PERCENTILES
    pairs = [(edges[i], edges[-1 - i]) for i in range(len(edges) // 2)]
    out = []
    for depth, (lo, hi) in enumerate(pairs):
        strength = 0.5 + 0.4 * depth / max(len(pairs) - 1, 1)
        r, g, b = (1 - strength * (1 - c) for c in _BASE)
        out.append(((lo, hi), (r, g, b), f"{lo * 100:g}–{hi * 100:g}%"))
    return out


def _band_of(q: float) -> tuple[float, float, float] | None:
    """The color of the innermost band a cumulative share ``q`` falls in."""
    color = None
    for (lo, hi), c, _ in _band_colors():
        if lo <= q <= hi:
            color = c
    return color


def _draw_pdf(ax: Axes, p: PersonLifespan, peak: float) -> None:
    """Bars for the chance of dying at each age, colored by the fan band
    the age's middle falls in (pale gray outside 5–95%), up to ``peak``,
    the highest bar of any person's, so the persons' charts compare."""
    rows = [r for r in p.yearly() if r[0] < p.age_at_end]
    for age, chance, by_end in rows:
        middle = by_end - chance / 2
        color = _band_of(middle) or (0.88, 0.88, 0.88)
        ax.bar(age + 0.5, chance, width=1.0, color=color, edgecolor=_BASE, linewidth=0.4)
    for _band, color, label in _band_colors():
        ax.bar(0, 0, color=color, label=label)  # legend entries only
    _mark_ages(ax, p)
    ax.set_title(f"{p.display}: chance of dying at each age")
    ax.set_xlabel("age")
    ax.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=1))
    ax.grid(True, axis="y", alpha=0.3)
    ax.set_xlim(p.age_now, p.age_at_end)
    ax.set_ylim(0, peak * 1.05)
    ax.legend(framealpha=FRAME_ALPHA, fontsize="small")


def _draw_cdf(ax: Axes, p: PersonLifespan) -> None:
    """The chance of having died by each age, over the fan's bands."""
    for (lo, hi), color, _ in _band_colors():
        ax.axvspan(p.quantile(lo), p.quantile(hi), color=color, linewidth=0)
    first, cdf = p.dist.monthly_cdf()
    ages = [(first + k + 1) / 12 for k in range(len(cdf))]
    ax.plot([p.age_now, *ages], [0.0, *cdf], color="midnightblue", linewidth=2,
            label="died by this age")
    ax.plot([p.age_now, *ages], [1.0, *(1 - f for f in cdf)], color="darkgreen",
            linewidth=1.2, linestyle="--", label="still alive")
    _mark_ages(ax, p)
    ax.set_title(f"{p.display}: chance of having died by each age")
    ax.set_xlabel("age")
    ax.set_ylim(0, 1)
    ax.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    ax.grid(True, alpha=0.3)
    ax.set_xlim(p.age_now, p.age_at_end)
    ax.legend(framealpha=FRAME_ALPHA, fontsize="small", loc="center left")


def _mark_ages(ax: Axes, p: PersonLifespan) -> None:
    """The median as a solid line; the scenario's own age, when the table
    isn't adjusted to it, as a dashed orange one."""
    ax.axvline(p.quantile(0.5), color="midnightblue", linewidth=1,
               label=f"median {p.quantile(0.5):.1f}")
    if p.assumed_age is not None:
        ax.axvline(p.assumed_age, color="darkorange", linewidth=1.2, linestyle="--",
                   label=f"scenario's age {p.assumed_age:g}")


def _draw_household(ax: Axes, h: Household) -> None:
    """Each person's chance of being alive, by calendar year, with the
    chance that at least one is, over the fan of the last death's year."""
    for (lo, hi), color, label in _band_colors():
        ax.axvspan(_decimal_year(h.last_death_quantile(lo)),
                   _decimal_year(h.last_death_quantile(hi)), color=color, linewidth=0,
                   label=f"last death {label}")
    end = h.persons[0].end
    length = min(len(h.any_alive), (end.year - h.start.year) * 12 + end.month - h.start.month + 1)
    years = [_decimal_year(h.start) + k / 12 for k in range(length + 1)]
    # Each curve starts at its value on the first day, before any month's deaths.
    any_alive, all_alive = (1.0, *h.any_alive), (1.0, *h.all_alive)
    ax.plot(years, any_alive[:length + 1], color="midnightblue", linewidth=2.5,
            label="at least one alive")
    ax.plot(years, all_alive[:length + 1], color="purple", linewidth=1.5, linestyle=":",
            label="all alive" if len(h.persons) > 2 else "both alive")
    for p, alive in zip(h.persons, h.alive, strict=True):
        ax.plot(years, (1.0, *alive)[:length + 1], linewidth=1, linestyle="--", label=f"{p.display} alive")
    median = h.last_death_quantile(0.5)
    ax.axvline(_decimal_year(median), color="midnightblue", linewidth=1,
               label=f"median last death {median.year}")
    ax.set_title("Household: chance someone is still alive")
    ax.set_xlabel("year")
    ax.set_ylim(0, 1)
    ax.set_xlim(years[0], years[-1])
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0f}"))
    ax.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    ax.grid(True, alpha=0.3)
    ax.legend(framealpha=FRAME_ALPHA, fontsize="small", loc="lower left")


def lifespan_figure(persons: list[PersonLifespan], *, show_household: bool = True) -> Figure:
    """One row per person, its PDF beside its CDF, and with two or more
    persons and ``show_household``, a last row across both columns for the
    household's chance that at least one is alive."""
    if not persons:
        raise ValueError("no persons with a sex to draw")
    joint = show_household and len(persons) > 1
    rows = len(persons) + (1 if joint else 0)
    fig = Figure(figsize=(12, 3.8 * rows), layout="constrained")
    grid = fig.add_gridspec(rows, 2)
    peak = max(chance for p in persons for _age, chance, _by in p.yearly())
    for i, p in enumerate(persons):
        _draw_pdf(fig.add_subplot(grid[i, 0]), p, peak)
        _draw_cdf(fig.add_subplot(grid[i, 1]), p)
    if joint:
        _draw_household(fig.add_subplot(grid[rows - 1, :]), household(persons))
    return fig
