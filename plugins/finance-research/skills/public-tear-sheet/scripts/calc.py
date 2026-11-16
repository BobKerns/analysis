#!/usr/bin/env python3
"""Derived metrics and arithmetic checks for a public-source tear sheet.

Usage:
    python calc.py tear-sheet-work/data.csv [-o tear-sheet-work/calculations.csv]

Input CSV columns (header required):
    section,metric,period,value,unit,source_url,as_of

Conventions (metric names are lowercase; unknown metrics are ignored):
  section=financials  period=FY2024 / FY2025 / LTM ...
      (or give YTD-CUR and YTD-PRIOR periods, e.g. 6 months 2026 and 6 months 2025,
       and LTM is built as latest FY + YTD-CUR - YTD-PRIOR for each metric that has all three)
      revenue, kpi (company's headline non-GAAP top line, e.g. net bookings),
      gross_profit, operating_income, ebitda, net_income, eps_diluted,
      ocf, capex, fcf, rnd
  section=balance     period=<date or quarter label>   (latest row per metric wins)
      total_debt, cash, sti (short-term investments)
  section=market      period=<as-of date>
      price, shares, market_cap
  section=guidance    period=FY2027 ...
      revenue_low, revenue_high, kpi_low, kpi_high, eps_low, eps_high
  section=consensus   period=FY2027 ...   (analyst consensus when there is no guidance)
      same metric names; a single figure may be given as both _low and _high
  ebitda is assumed GAAP-derived; for company-adjusted EBITDA use metric ebitda_adj
  section=segment     metric=<segment name>  period=FY2025 ...  value=segment revenue
  section=peer        metric=market_cap | revenue | ev | gross_profit | ebitda | price | eps   period=<TICKER>   (never the subject company)
  section=private     metric=valuation | run_rate   period=<date>   (latest round post-money; company-claimed run-rate)

Values in a consistent unit (e.g. $M) except per-share values (price, EPS).
Output: metric,value,formula,components  -- plus WARN lines on stdout.
"""
# cspell:ignore ebitda gsrc segs
import argparse
import os
import csv
import re
import sys
from collections import defaultdict


def period_key(p):
    m = re.search(r"(\d{2,4})", p or "")
    y = int(m.group(1)) if m else 0
    if y < 100:
        y += 2000
    return (y, 1 if "LTM" in (p or "").upper() else 0)


def num(v):
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def fmt(x, kind):
    if x is None:
        return "n/a"
    if kind == "pct":
        return f"{x * 100:.1f}%"
    if kind == "x":
        return f"{x:.1f}x"
    if kind == "ps":
        return f"{x:.2f}"
    return f"{x:,.1f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data")
    ap.add_argument("-o", "--out")
    a = ap.parse_args()
    out_path = a.out or os.path.join(os.path.dirname(a.data), "calculations.csv")

    fin = defaultdict(dict)      # metric -> period -> value
    bal, mkt, seg, peer = {}, {}, defaultdict(dict), defaultdict(dict)
    guide = defaultdict(dict)
    ebitda_label = ["EBITDA"]
    priv = {}
    with open(a.data, newline="") as f:
        for r in csv.DictReader(f):
            s, m, p, v = (r.get("section") or "").strip().lower(), (r.get("metric") or "").strip(), (r.get("period") or "").strip(), num(r.get("value"))
            if v is None:
                continue
            ml = m.lower()
            if s == "financials":
                if ml == "ebitda_adj":
                    fin["ebitda"][p] = v
                    ebitda_label[0] = "adjusted EBITDA (company non-GAAP)"
                else:
                    fin[ml][p] = v
            elif s == "balance":
                bal[ml] = v
            elif s == "market":
                mkt[ml] = v
            elif s in ("guidance", "consensus"):
                guide[(s, p)][ml] = v
            elif s == "segment":
                seg[p][m] = v
            elif s == "private":
                priv[ml] = (v, p)
            elif s == "peer":
                peer[p][ml] = v

    rows, warns = [], []

    def add(metric, value, formula, comps, kind="num"):
        comps = re.sub(r"(\d+\.\d{3,})", lambda m: f"{float(m.group(1)):.2f}", comps)
        rows.append({"metric": metric, "value": fmt(value, kind), "formula": formula, "components": comps})

    # LTM from year-to-date figures
    for metric, series in list(fin.items()):
        if "LTM" in series or "YTD-CUR" not in series or "YTD-PRIOR" not in series:
            continue
        fy = sorted([p for p in series if p.upper().startswith("FY")], key=period_key)
        if fy:
            ltm = series[fy[-1]] + series["YTD-CUR"] - series["YTD-PRIOR"]
            series["LTM"] = ltm
            add(f"{metric}_LTM", ltm, f"{fy[-1]} + YTD-CUR - YTD-PRIOR", f"{series[fy[-1]]}+{series['YTD-CUR']}-{series['YTD-PRIOR']}")
        for k in ("YTD-CUR", "YTD-PRIOR"):
            series.pop(k, None)

    # Point guidance/consensus (single figure) -> low == high
    for g in guide.values():
        for alias in ("eps_diluted", "eps_adj"):
            if alias in g and "eps" not in g:
                g["eps"] = g.pop(alias)
        for m in list(g):
            if not m.endswith(("_low", "_high")):
                g.setdefault(f"{m}_low", g[m])
                g.setdefault(f"{m}_high", g[m])

    # FCF fill-in
    for p, o in fin.get("ocf", {}).items():
        if p not in fin["fcf"] and p in fin.get("capex", {}):
            c = abs(fin["capex"][p])
            fin["fcf"][p] = o - c
            add(f"fcf_{p}", o - c, "ocf - |capex|", f"{o}-{c}")

    rev = fin.get("revenue", {})
    periods = sorted(rev, key=period_key)
    fys = [p for p in periods if "LTM" not in p.upper()]

    # Growth for revenue and kpi
    for metric in ("revenue", "kpi", "eps_diluted"):
        series = fin.get(metric, {})
        ps = sorted([p for p in series if "LTM" not in p.upper()], key=period_key)
        for prev, cur in zip(ps, ps[1:], strict=False):
            if period_key(cur)[0] - period_key(prev)[0] != 1:
                warns.append(f"{metric}: gap between {prev} and {cur}; growth not computed")
                continue
            if series[prev] <= 0:
                add(f"{metric}_growth_{cur}", None, "(cur-prior)/prior", "prior <= 0: not meaningful")
                continue
            add(f"{metric}_growth_{cur}", series[cur] / series[prev] - 1, "(cur-prior)/prior", f"{series[cur]}/{series[prev]}", "pct")
        if ps and metric == "revenue" and len(ps) < 4:
            warns.append(f"revenue: only {len(ps)} fiscal years; 4 needed to show growth on 3 displayed years")

    # Margins and intensities
    for name, num_metric in (("gross_margin", "gross_profit"), ("operating_margin", "operating_income"),
                             ("ebitda_margin", "ebitda"), ("net_margin", "net_income"),
                             ("fcf_margin", "fcf"), ("rnd_pct_revenue", "rnd"), ("capex_pct_revenue", "capex")):
        for p, n in fin.get(num_metric, {}).items():
            r = rev.get(p)
            if not r:
                warns.append(f"{name} {p}: no revenue for period")
                continue
            val = abs(n) / r if num_metric == "capex" else n / r
            add(f"{name}_{p}", val, f"{num_metric}/revenue", f"{n}/{r}", "pct")
    for p, f_ in fin.get("fcf", {}).items():
        e = fin.get("ebitda", {}).get(p)
        if e and e > 0:
            add(f"fcf_conversion_{p}", f_ / e, "fcf/ebitda", f"{f_}/{e}", "pct")

    # Capital structure
    debt, cash, sti = bal.get("total_debt"), bal.get("cash"), bal.get("sti", 0.0)
    net_debt = None
    if debt is not None and cash is not None:
        net_debt = debt - cash - sti
        add("net_debt", net_debt, "total_debt - cash - sti", f"{debt}-{cash}-{sti}")
        last = (fys or [None])[-1]
        e = fin.get("ebitda", {}).get("LTM") or (fin.get("ebitda", {}).get(last) if last else None)
        if e:
            add("net_debt_to_ebitda", net_debt / e if e > 0 else None, "net_debt/ebitda", f"{net_debt}/{e}" + ("" if e > 0 else " (ebitda<=0: n/m)"), "x")

    # Market
    price, shares, mcap = mkt.get("price"), mkt.get("shares"), mkt.get("market_cap")
    if mcap is None and price and shares:
        mcap = price * shares
        add("market_cap", mcap, "price*shares", f"{price}*{shares}")
    if shares is None and price and mcap:
        shares = mcap / price
        add("shares_implied", shares, "market_cap/price", f"{mcap}/{price}")
    elif price and shares and mcap and abs(price * shares / mcap - 1) > 0.05:
        warns.append(f"market_cap {mcap} differs >5% from price*shares {price * shares:.1f}; check dates/units")
    ev = None
    if mcap is not None and net_debt is not None:
        ev = mcap + net_debt
        add("enterprise_value", ev, "market_cap + net_debt", f"{mcap}+{net_debt}")

    # Multiples
    base = ev if ev is not None else mcap
    base_name = "ev" if ev is not None else "market_cap"
    trailing_p = "LTM" if "LTM" in rev else ((fys or [None])[-1])
    if base and trailing_p and rev.get(trailing_p):
        add(f"{base_name}_to_revenue_{trailing_p}", base / rev[trailing_p], f"{base_name}/revenue", f"{base}/{rev[trailing_p]}", "x")
    gp_series = fin.get("gross_profit", {})
    gp_p = "LTM" if "LTM" in gp_series else ((sorted(gp_series, key=period_key) or [None])[-1])
    if base and gp_p in gp_series and gp_series[gp_p] > 0:
        add(f"{base_name}_to_gross_profit_{gp_p}", base / gp_series[gp_p], f"{base_name}/gross_profit", f"{base}/{gp_series[gp_p]}", "x")
    eb = fin.get("ebitda", {})
    eb_p = "LTM" if "LTM" in eb else ((sorted(eb, key=period_key) or [None])[-1])
    if base and eb_p in eb:
        e = eb[eb_p]
        add(f"{base_name}_to_ebitda_{eb_p}", base / e if e > 0 else None, f"{base_name}/{ebitda_label[0]}", f"{base}/{e}" + ("" if e > 0 else " (<=0: n/m)"), "x")
    eps = fin.get("eps_diluted", {})
    eps_p = "LTM" if "LTM" in eps else (sorted(eps, key=period_key) or [None])[-1]
    if price and eps_p in eps:
        e = eps[eps_p]
        add(f"pe_{eps_p}", price / e if e > 0 else None, "price/eps", f"{price}/{e}" + ("" if e > 0 else " (eps<=0: not meaningful)"), "x")
    for (gsrc, gp_), g in guide.items():
        gp = f"{gsrc}_{gp_}"
        for lo_k, hi_k, label in (("revenue_low", "revenue_high", "revenue"), ("kpi_low", "kpi_high", "kpi"), ("ebitda_adj_low", "ebitda_adj_high", "ebitda_adj"), ("ebitda_low", "ebitda_high", "ebitda")):
            if lo_k in g and hi_k in g:
                mid = (g[lo_k] + g[hi_k]) / 2
                add(f"{label}_mid_{gp}", mid, "(low+high)/2", f"({g[lo_k]}+{g[hi_k]})/2")
                if base:
                    add(f"{base_name}_to_{label}_{gp}", base / mid, f"{base_name}/{gsrc} midpoint", f"{base}/{mid}", "x")
                last = (fys or [None])[-1]
                prior = fin.get(label, {}).get(last) if last else None
                if prior and gsrc == "consensus":
                    warns.append(f"{label} growth on {gp} uses consensus vs reported {last}: confirm both are the same measure (e.g. net sales vs total revenue)")
                if prior and prior > 0:
                    add(f"{label}_growth_{gp}", mid / prior - 1, f"midpoint/{last} - 1", f"{mid}/{prior}", "pct")
        if price and "eps_low" in g and "eps_high" in g:
            lo, hi = g["eps_low"], g["eps_high"]
            if lo > 0:
                if lo == hi:
                    add(f"pe_on_{gp}", price / lo, "price/eps", f"{price}/{lo}", "x")
                else:
                    add(f"pe_on_{gp}", None, "price/eps range", f"{price/hi:.0f}x-{price/lo:.0f}x on EPS {lo}-{hi}")
                    rows[-1]["value"] = f"{price/hi:.1f}x-{price/lo:.1f}x"
            else:
                add(f"pe_on_{gp}", None, "price/eps range", f"EPS guide {lo}-{hi} includes <=0: not meaningful")

    # Segments
    for p, segs in seg.items():
        total = rev.get(p)
        if not total:
            warns.append(f"segments {p}: no consolidated revenue; shares not computed")
            continue
        s = sum(segs.values())
        for name, v in segs.items():
            add(f"segment_share_{name}_{p}", v / total, "segment/consolidated revenue", f"{v}/{total}", "pct")
        if abs(s / total - 1) > 0.03:
            warns.append(f"segments {p}: sum {s:,.1f} vs consolidated {total:,.1f} ({s/total-1:+.1%}); eliminations or missing segment — don't show a total row")

    # Private: implied multiple on company-claimed run-rate
    if "valuation" in priv and "run_rate" in priv:
        (val, vd), (rr, rd) = priv["valuation"], priv["run_rate"]
        add("implied_valuation_to_claimed_run_rate", val / rr, "post-money / company-claimed run-rate (implied)", f"{val} ({vd}) / {rr} ({rd})", "x")
        if vd[:7] != rd[:7]:
            warns.append(f"valuation dated {vd} and run-rate dated {rd}: label the implied multiple with both dates")

    # Peers (EV preferred; market cap used and labelled when no EV row)
    ratios = defaultdict(list)
    for t, d in sorted(peer.items()):
        lbl = "ev" if "ev" in d else "mcap"
        b = d.get("ev", d.get("market_cap"))
        if d.get("price") and d.get("eps"):
            add(f"peer_{t}_pe", d["price"] / d["eps"] if d["eps"] > 0 else None, "price/eps", f"{d['price']}/{d['eps']}", "x")
            if d["eps"] > 0:
                ratios[("price", "eps")].append(d["price"] / d["eps"])
        for denom in ("revenue", "gross_profit", "ebitda"):
            if b and d.get(denom):
                r = b / d[denom] if d[denom] > 0 else None
                add(f"peer_{t}_{lbl}_to_{denom}", r, f"{lbl}/{denom}", f"{b}/{d[denom]}", "x")
                if r is not None:
                    ratios[(lbl, denom)].append(r)
    for (lbl, denom), vals in ratios.items():
        vals.sort()
        n = len(vals)
        med = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
        add("peer_median_pe" if lbl == "price" else f"peer_median_{lbl}_to_{denom}", med, "median of peers", f"n={n}", "x")
    if len({lbl for lbl, _ in ratios} - {"price"}) > 1:
        warns.append("peers mix EV-based and market-cap-based multiples; compare like with like")

    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["metric", "value", "formula", "components"])
        w.writeheader()
        w.writerows(rows)
    for r in rows:
        print(f"{r['metric']:<40} {r['value']:>14}   {r['formula']}  [{r['components']}]")
    for w_ in warns:
        print("WARN", w_)
    print(f"\nwrote {len(rows)} rows to {out_path}; {len(warns)} warnings", file=sys.stderr)


if __name__ == "__main__":
    main()
