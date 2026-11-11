#!/usr/bin/env python3
"""How much information is in each historical series? A compression test (#286).

For each series in the committed `historical_data/` (inflation, the 10-year
and T-bill yields, stock and bond returns, all yearly, and monthly CPI-U
changes), prints the code length in bits under a few descriptions of the
series: minimum description length, the data coded given the model plus
about ½·log₂ n bits per fitted parameter. Then three generic compressors on
the series in its real order and on shuffled copies (mean of the shuffles):
a compressor that does better on the real order has found some order in
time. docs/guides/historical-information.md reads the results.

Reads only `historical_data/`, never the network. The output is Markdown.

Usage: .venv/bin/python scripts/historical_compression.py [--start-year Y] [--end-year Y]
  e.g. --start-year 2000 for every series since 2000, monthly CPI included;
  --end-year 1979 for the years before it
"""
# cspell:ignore resid logpdf prequential
from __future__ import annotations

import argparse
import bz2
import lzma
import math
import random
import zlib
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

from analysis.frames import index_years
from analysis.historical.calibration import Fetcher
from analysis.historical.charts import load_history

LOG2E = 1 / math.log(2)
HISTORICAL_DATA = Path(__file__).resolve().parents[1] / "historical_data"


def gauss_bits(resid: np.ndarray, step: float) -> float:
    """Ideal code length of values rounded to `step`, Gaussian with the residuals' own SD."""
    sd = resid.std(ddof=0)
    return float(len(resid) * (math.log2(sd / step) + 0.5 * math.log2(2 * math.pi * math.e)))


def laplace_bits(resid: np.ndarray, step: float) -> float:
    b = np.abs(resid - np.median(resid)).mean()
    return float(len(resid) * (math.log2(2 * b * math.e / step)))


def student_bits(resid: np.ndarray, step: float) -> tuple[float, float]:
    """Best Student-t code length and its nu (scale fit for each nu on a grid)."""
    best = (math.inf, 0.0)
    for nu in [2.5, 3, 4, 5, 7, 10, 20, 50, 1000]:
        _, loc, scale = stats.t.fit(resid, f0=nu)
        nll = -stats.t.logpdf(resid, nu, loc, scale).sum() * LOG2E
        bits = nll - len(resid) * math.log2(step)
        best = min(best, (bits, nu))
    return best


def ar_resid(x: np.ndarray, p: int) -> np.ndarray:
    """Least-squares AR(p) residuals (first p values coded with the mean model)."""
    if p == 0:
        return x - x.mean()
    rows = np.column_stack([x[p - k - 1:len(x) - k - 1] for k in range(p)] + [np.ones(len(x) - p)])
    coef, *_ = np.linalg.lstsq(rows, x[p:], rcond=None)
    head = x[:p] - x.mean()
    return np.concatenate([head, x[p:] - rows @ coef])


def adaptive_bits(x: np.ndarray, step: float, window: int) -> float:
    """Prequential (predictive) code: each value is coded with an AR(1) fit
    and a shock SD estimated only from the previous `window` values, so a
    calm stretch is coded more cheaply than a wild one (volatility regimes).
    The first `window` values use the whole-series iid code; no parameter
    cost for the SDs, since every estimate comes from data already sent."""
    r = ar_resid(x, 1)
    head = r[:window]
    bits = gauss_bits(head, step) if len(head) > 1 else 0.0
    c = 0.5 * math.log2(2 * math.pi)
    for t in range(window, len(r)):
        sd = max(r[t - window:t].std(ddof=1), step)
        bits += math.log2(sd / step) + c + (r[t] / sd) ** 2 / 2 * LOG2E
    return float(bits)


def param_bits(k: int, n: int) -> float:
    """MDL's cost of k real parameters, each to the precision n data points justify."""
    return 0.5 * k * math.log2(n)


def compressed_bits(q: np.ndarray) -> dict[str, float]:
    data = q.astype(np.int16).tobytes()
    # xz in raw format, since the .xz container's header and checksum would
    # swamp a series this short.
    raw = lzma.compress(data, format=lzma.FORMAT_RAW,
                        filters=[{"id": lzma.FILTER_LZMA2, "preset": 9}])
    return {"zlib": len(zlib.compress(data, 9)) * 8, "bz2": len(bz2.compress(data, 9)) * 8,
            "xz (raw)": len(raw) * 8}


def report(name: str, x: np.ndarray, step: float, unit: str, span: str, shuffles: int) -> None:
    n = len(x)
    q = np.round(x / step)
    xq = q * step
    print(f"\n### {name}, {span}: {n} {unit}s, rounded to {step:g} point\n")
    print(f"| Description | params | bits total | bits per {unit} |")
    print("| --- | ---: | ---: | ---: |")
    rows = []
    for label, p in [("one level, no memory (iid normal)", 0), ("AR(1) normal", 1),
                     ("AR(2) normal", 2), ("AR(3) normal", 3)]:
        k = p + 2
        rows.append((label, k, gauss_bits(ar_resid(xq, p), step) + param_bits(k, n)))
    r1 = ar_resid(xq, 1)
    rows.append(("AR(1) Laplace shocks", 3, laplace_bits(r1, step) + param_bits(3, n)))
    tb, nu = student_bits(r1, step)
    rows.append((f"AR(1) Student-t shocks (nu={nu:g})", 4, tb + param_bits(4, n)))
    w = 10 if unit == "year" else 36
    # The adaptive code's parameters: AR(1)'s level and persistence, plus the
    # head's SD; the rolling SDs cost nothing.
    rows.append((f"AR(1), shock SD from the last {w} {unit}s", 3,
                 adaptive_bits(xq, step, w) + param_bits(1, n)))
    for label, k, bits in rows:
        print(f"| {label} | {k} | {bits:,.0f} | {bits / n:.2f} |")

    rng = random.Random(1)
    real = compressed_bits(q)
    shuffled = dict.fromkeys(real, 0.0)
    for _ in range(shuffles):
        s = q.copy()
        rng.shuffle(s)
        for k, v in compressed_bits(s).items():
            shuffled[k] += v / shuffles
    print("\n| Compressor | real order, bits | shuffled, bits | real / shuffled |")
    print("| --- | ---: | ---: | ---: |")
    for k in real:
        print(f"| {k} | {real[k]:,.0f} | {shuffled[k]:,.0f} | {real[k] / shuffled[k]:.3f} |")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--start-year", type=int, default=None,
                    help="leave out years (and months) before this one")
    ap.add_argument("--end-year", type=int, default=None,
                    help="leave out years (and months) after this one")
    ap.add_argument("--shuffles", type=int, default=50,
                    help="shuffled copies averaged for each compressor (default 50)")
    args = ap.parse_args()
    start, end = args.start_year, args.end_year

    h = load_history(Fetcher(HISTORICAL_DATA, None))
    for name, s in h.labeled():
        s = s.astype(float)
        if start is not None:
            s = s[s.index >= start]
        if end is not None:
            s = s[s.index <= end]
        if len(s) < 15:
            print(f"\n### {name}: only {len(s)} years, skipped")
            continue
        span = f"{int(s.index[0])}–{int(s.index[-1])}"
        report(name, s.to_numpy(), 0.1, "year", span, args.shuffles)

    # Month-over-month change in percent, each dated by the later month, so
    # --start-year 2000 starts with January 2000's change.
    cpi = h.cpi_monthly.astype(float)
    mom = pd.Series(np.diff(np.log(cpi.to_numpy())) * 100, index=cpi.index[1:])
    years = index_years(mom).to_numpy()
    keep = np.ones(len(mom), dtype=bool)
    if start is not None:
        keep &= years >= start
    if end is not None:
        keep &= years <= end
    mom = mom[keep]
    span = f"{mom.index[0]:%Y-%m}–{mom.index[-1]:%Y-%m}"
    report("CPI-U month-over-month (SA)", mom.to_numpy(), 0.01, "month", span, args.shuffles)


if __name__ == "__main__":
    main()
