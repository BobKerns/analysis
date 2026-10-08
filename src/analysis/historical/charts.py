"""Charts for docs/guides/century-of-rates.md: which US rates carry over from
one year to the next, drawn from the committed ``historical_data/``.

``analysis historical-charts DIR`` writes one SVG per chart. Each works on a
light or a dark page alike (transparent background, mid-tone ink), since the
guide is read on GitHub and in editors in either theme, and two copies of
every chart would show twice outside GitHub.
"""

from __future__ import annotations

# cspell:ignore CUUR CUSR fonttype hashsalt facecolor labelsize edgecolors datalim frameon

import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.axes import Axes  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

from analysis.frames import index_months, index_years  # noqa: E402
from analysis.historical.calibration import Fetcher, annual_rates, load_series  # noqa: E402

# Mid-tone ink and series colors read on both a white and a near-black page.
INK = "#868b88"
GRID = "#868b8840"
BAND = "#c9a96e33"
BAND_ALT = "#c9a96e1a"
SERIES = ["#3b82d9", "#e2662f", "#1ea676", "#d99a0b", "#d9668f"]

FIRST_YEAR, LAST_YEAR = 1928, 2025
GREAT_INFLATION = (1966, 1982)
# Nixon's wage and price controls, by phase, as (start, end, label) in
# fractional years: Phase I's freeze from 15 August 1971, Phase II from
# 14 November, Phase III from 11 January 1973, the second freeze from 13
# June, Phase IV from 12 August until controls lapsed on 30 April 1974.
CONTROL_PHASES = [
    (1971 + 7.5 / 12, 1971 + 10.5 / 12, "Freeze"),
    (1971 + 10.5 / 12, 1973 + 0.3 / 12, "Phase II"),
    (1973 + 0.3 / 12, 1973 + 5.4 / 12, "III"),
    (1973 + 5.4 / 12, 1973 + 7.4 / 12, "Freeze"),
    (1973 + 7.4 / 12, 1974 + 4 / 12, "Phase IV"),
]


@dataclass
class History:
    """Annual percent rates by calendar year, plus monthly CPI levels."""

    inflation: pd.Series
    tbill: pd.Series
    yield10: pd.Series
    stocks: pd.Series
    bonds: pd.Series
    cpi_monthly: pd.Series  # seasonally adjusted level, indexed by month start

    def labeled(self) -> list[tuple[str, pd.Series]]:
        return [("Inflation", self.inflation), ("10-yr yield", self.yield10),
                ("T-bill yield", self.tbill), ("Stock return", self.stocks),
                ("Bond return", self.bonds)]


def load_history(fetcher: Fetcher) -> History:
    specs = ["bls:CUUR0000SA0", "bls:CUSR0000SA0", "damodaran:tbill", "fred:DGS10@rate",
             "damodaran:sp500", "damodaran:tbond"]
    cpi, cpi_sa, tbill, y10, sp, bond = load_series(fetcher, specs)

    def years(s: pd.Series) -> pd.Series:
        s = s.astype(float)
        return s[(s.index >= FIRST_YEAR) & (s.index <= LAST_YEAR)]

    return History(inflation=years(annual_rates(cpi)), tbill=years(annual_rates(tbill)),
                   yield10=years(annual_rates(y10)), stocks=years(annual_rates(sp)),
                   bonds=years(annual_rates(bond)), cpi_monthly=cpi_sa.values.astype(float))


# ------------------------------------------------------------- statistics


def year_pairs(rates: pd.Series) -> list[tuple[int, float, float]]:
    """(year, rate that year, rate the next calendar year), skipping gaps."""
    return [(int(y), float(rates[y]), float(rates[y + 1])) for y in rates.index if y + 1 in rates.index]


def correlation(pairs: Sequence[tuple[float, float]]) -> float:
    n = len(pairs)
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pairs)
    sxx = sum((p[0] - mx) ** 2 for p in pairs)
    syy = sum((p[1] - my) ** 2 for p in pairs)
    return sxy / math.sqrt(sxx * syy)


def persistence_by_lag(rates: pd.Series, lags: int = 10) -> list[float]:
    """Correlation of each year's rate with the rate ``k`` calendar years later."""
    out = []
    for k in range(1, lags + 1):
        pairs = [(float(rates[y]), float(rates[y + k])) for y in rates.index if y + k in rates.index]
        out.append(correlation(pairs))
    return out


def rolling_persistence(rates: pd.Series, window: int = 20) -> pd.Series:
    """Year-to-next correlation over each trailing ``window`` years, indexed
    by the window's last year; a window missing any year is left out."""
    out = {}
    for end in rates.index:
        years = range(end - window + 1, end + 1)
        if all(y in rates.index for y in years):
            pairs = [(float(rates[y]), float(rates[y + 1])) for y in years[:-1]]
            out[int(end)] = correlation(pairs)
    return pd.Series(out, dtype=float)


def monthly_inflation(levels: pd.Series, first: int, last: int) -> tuple[list[float], list[float], list[float]]:
    """Fractional-year x, year-over-year %, and single-month annualized %
    for each month of ``first``..``last`` that has a level 12 months back."""
    yrs, mos = index_years(levels), index_months(levels)
    by_month = {(int(y), int(m)): float(v) for y, m, v in zip(yrs, mos, levels.to_numpy(), strict=True)}
    xs, yoy, single = [], [], []
    for y in range(first, last + 1):
        for m in range(1, 13):
            now, prev = by_month.get((y, m)), by_month.get((y, m - 1) if m > 1 else (y - 1, 12))
            year_ago = by_month.get((y - 1, m))
            if now is None or prev is None or year_ago is None:
                continue
            xs.append(y + (m - 1) / 12)
            yoy.append((now / year_ago - 1) * 100)
            single.append(((now / prev) ** 12 - 1) * 100)
    return xs, yoy, single


# ----------------------------------------------------------------- drawing


def _figure(height: float = 3.2, ncols: int = 1) -> tuple[Figure, list[Axes]]:
    fig, axes = plt.subplots(1, ncols, figsize=(8, height), squeeze=False)
    fig.patch.set_alpha(0)
    for ax in axes[0]:
        ax.set_facecolor("none")
        ax.tick_params(colors=INK, labelsize=9, length=0)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(INK)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
    return fig, list(axes[0])


def _percent_axis(ax: Axes) -> None:
    ax.yaxis.set_major_formatter(lambda v, _pos: f"{v:g}%")


def _save(fig: Figure, path: Path) -> None:
    fig.tight_layout()
    fig.savefig(path, format="svg", transparent=True, metadata={"Date": None})
    plt.close(fig)


def _bars(ax: Axes, rates: pd.Series) -> None:
    years = [int(y) for y in rates.index]
    values = rates.to_numpy()
    ax.bar(years, values, width=0.8, color=[SERIES[0] if v >= 0 else SERIES[1] for v in values])
    ax.axhline(0, color=INK, linewidth=0.8)
    _percent_axis(ax)


def _end_labels(ax: Axes, ends: list[tuple[float, float, str]], min_gap: float) -> None:
    """Label each line at its last point, nudged apart so none overlap."""
    placed: list[float] = []
    for x, y, text in sorted(ends, key=lambda e: e[1]):
        if placed and y - placed[-1] < min_gap:
            y = placed[-1] + min_gap
        placed.append(y)
        ax.annotate(text, (x, y), xytext=(6, 0), textcoords="offset points", va="center",
                    fontsize=9, color=INK)


def draw_inflation(h: History, path: Path) -> None:
    fig, (ax,) = _figure()
    ax.axvspan(GREAT_INFLATION[0] - 0.5, GREAT_INFLATION[1] + 0.5, color=BAND, linewidth=0)
    _bars(ax, h.inflation)
    ax.text(GREAT_INFLATION[0], float(h.inflation.max()), "Great Inflation", color=INK, fontsize=9, va="top")
    _save(fig, path)


def draw_yields(h: History, path: Path) -> None:
    fig, (ax,) = _figure()
    ends = []
    for (name, s), color in zip([("10-yr yield", h.yield10), ("T-bill", h.tbill)], SERIES, strict=False):
        ax.plot(s.index, s.to_numpy(), color=color, linewidth=2)
        ends.append((float(s.index[-1]), float(s.iloc[-1]), name))
    _end_labels(ax, ends, min_gap=1.0)
    peak = int(h.yield10.index[int(h.yield10.to_numpy().argmax())])
    ax.axvline(peak, color=INK, linewidth=0.8, linestyle=(0, (3, 3)))
    ax.text(peak + 1, float(h.yield10.max()), f"{peak} peak", color=INK, fontsize=9, va="top")
    ax.set_xlim(right=LAST_YEAR + 12)
    _percent_axis(ax)
    _save(fig, path)


def draw_stocks(h: History, path: Path) -> None:
    fig, (ax,) = _figure()
    _bars(ax, h.stocks)
    _save(fig, path)


def draw_scatter(h: History, path: Path) -> None:
    fig, axes = _figure(height=3.9, ncols=2)
    for ax, (title, rates), color in zip(axes, [("Inflation", h.inflation), ("Stock return", h.stocks)],
                                         SERIES, strict=False):
        pairs = year_pairs(rates)
        xs = [p[1] for p in pairs]
        ys = [p[2] for p in pairs]
        lo, hi = min(xs + ys), max(xs + ys)
        ax.plot([lo, hi], [lo, hi], color=INK, linewidth=0.8, linestyle=(0, (3, 3)))
        r = correlation([(x, y) for x, y in zip(xs, ys, strict=True)])
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sum((x - mx) ** 2 for x in xs)
        ax.plot([lo, hi], [my + slope * (lo - mx), my + slope * (hi - mx)], color=INK, linewidth=1.5)
        ax.scatter(xs, ys, s=18, color=color, alpha=0.75, edgecolors="none")
        ax.set_title(f"{title}: this year vs next", color=INK, fontsize=10, loc="left")
        ax.text(0.98, 0.04, f"correlation {r:.2f}", transform=ax.transAxes, ha="right", color=INK, fontsize=9)
        ax.set_xlabel("this year", color=INK, fontsize=9)
        ax.set_ylabel("next year", color=INK, fontsize=9)
        ax.grid(axis="x", color=GRID, linewidth=0.8)
        ax.set_aspect("equal", adjustable="datalim")
        _percent_axis(ax)
        ax.xaxis.set_major_formatter(lambda v, _pos: f"{v:g}%")
    _save(fig, path)


def draw_persistence_by_lag(h: History, path: Path) -> None:
    fig, (ax,) = _figure(height=3.4)
    lags = list(range(1, 11))
    ends = []
    for (name, rates), color in zip(h.labeled(), SERIES, strict=False):
        values = persistence_by_lag(rates)
        ax.plot(lags, values, color=color, linewidth=2, marker="o", markersize=4)
        ends.append((10.0, values[-1], name))
    _end_labels(ax, ends, min_gap=0.07)
    ax.axhline(0, color=INK, linewidth=0.8)
    ax.set_xticks(lags)
    ax.set_xlim(0.7, 12.4)
    ax.set_xlabel("years later", color=INK, fontsize=9)
    ax.set_ylabel("correlation", color=INK, fontsize=9)
    _save(fig, path)


def draw_inflation_memory(h: History, path: Path) -> None:
    fig, (ax,) = _figure()
    ends = []
    for (name, rates), color in zip([("Inflation", h.inflation), ("Stock return", h.stocks)], SERIES,
                                    strict=False):
        rolled = rolling_persistence(rates)
        ax.plot(rolled.index, rolled.to_numpy(), color=color, linewidth=2)
        ends.append((float(rolled.index[-1]), float(rolled.iloc[-1]), name))
    _end_labels(ax, ends, min_gap=0.12)
    for year, label in [(1979, "Volcker"), (2012, "2% target")]:
        ax.axvline(year, color=INK, linewidth=0.8, linestyle=(0, (3, 3)))
        ax.text(year + 0.7, 0.98, label, color=INK, fontsize=9, va="top")
    ax.axhline(0, color=INK, linewidth=0.8)
    ax.set_ylim(-0.6, 1.0)
    ax.set_xlim(right=LAST_YEAR + 14)
    ax.set_ylabel("year-to-next correlation,\ntrailing 20 years", color=INK, fontsize=9)
    _save(fig, path)


def draw_price_controls(h: History, path: Path) -> None:
    fig, (ax,) = _figure(height=3.4)
    xs, yoy, single = monthly_inflation(h.cpi_monthly, 1968, 1976)
    for i, (start, end, label) in enumerate(CONTROL_PHASES):
        ax.axvspan(start, end, color=BAND if i % 2 == 0 else BAND_ALT, linewidth=0)
        ax.text(start + 0.02, 24.5 - 2.2 * (i % 3), label, color=INK, fontsize=8, va="top")
    ax.bar(xs, single, width=1 / 12 * 0.75, color=SERIES[3], label="single month, annualized")
    ax.plot(xs, yoy, color=SERIES[0], linewidth=2, label="year over year")
    ax.axhline(0, color=INK, linewidth=0.8)
    ax.set_ylim(top=25)
    legend = ax.legend(loc="upper left", frameon=False, fontsize=9)
    for text in legend.get_texts():
        text.set_color(INK)
    _percent_axis(ax)
    _save(fig, path)


CHARTS = {
    "inflation.svg": draw_inflation,
    "yields.svg": draw_yields,
    "stocks.svg": draw_stocks,
    "this-year-vs-next.svg": draw_scatter,
    "persistence-by-lag.svg": draw_persistence_by_lag,
    "inflation-memory.svg": draw_inflation_memory,
    "price-controls.svg": draw_price_controls,
}


def write_charts(history: History, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    with plt.rc_context({"svg.fonttype": "none", "svg.hashsalt": "century-of-rates",
                         "font.family": "sans-serif"}):
        paths = []
        for name, draw in CHARTS.items():
            draw(history, out_dir / name)
            paths.append(out_dir / name)
    return paths
