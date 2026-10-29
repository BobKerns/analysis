#!/usr/bin/env python3
"""Asset-allocation breakdown of a fund, for the tear sheet and for import into other tools.

Usage:
    python allocation.py fund-work/data.csv --ticker VTWNX [--holdings fund-work/holdings.csv]
                         [--out-dir fund-work]

Categories (the simulator's asset classes, analysis.sim.registries.ASSET_CLASSES, in its order, plus unclassified; percentages of the fund's net assets, summing to 100):
    stock_us        stocks of companies domiciled in the US
    stock_intl      stocks of companies domiciled outside the US (ADRs count as international)
    stock           stocks whose US/international split couldn't be determined (ideally 0)
    bond            bonds of any kind (government, agency, corporate, mortgage, TIPS, foreign bonds)
    cd              certificates of deposit held directly (rare in funds; CDs inside a money fund are cash)
    cash            cash, money-market instruments, T-bills and repo held as cash management;
                    a money market fund is 100% cash
    real_estate     real estate held directly (REITs are stocks, not real_estate)
    other           anything else (commodities, derivatives' net exposure, business development
                    companies, private funds); name it in the notes
    unclassified    weight whose asset type couldn't be determined at all (an underlying fund with no
                    breakdown, holdings not covered by the file); ideally 0

Inputs, in order of preference (the first one available is used, and named as the method):
 1. reported          data.csv rows  section=allocation  metric=<category>  value=%  (as_of, source_url);
                      put who reported it in the unit column: 'issuer', or e.g. 'Morningstar via Schwab'
 2. look-through      data.csv rows  section=underlying  metric=weight  period=<FUND>   (fund of funds)
                      plus           section=lookthrough metric=<category> period=<FUND> value=%
                      (each underlying fund's own allocation)
 3. holdings-computed holdings.csv with columns name,ticker,weight_pct,asset_type,country[,sector,as_of]
                      asset_type: stock | preferred | bond | cash | other ; country: ISO code or name ('US' = United States)

Optional finer grain (written to the export when present):
    section=region  metric=<us | developed_ex_us | emerging | ...>   value=%   unit='% of equity' or '% of fund'
    section=sector  metric=<GICS sector, e.g. information_technology>  value=%   unit='% of equity; GICS' (basis; scheme)

When both a reported split and a full holdings file are given, the reported split is used and the holdings
are computed as a cross-check (written to the JSON; warns if US or foreign differ by more than 2 points).

Outputs: <out-dir>/<TICKER>-allocation.csv (ticker,as_of,category,pct,method,source), <TICKER>-allocation.json, and
<TICKER>-allocations-sim.csv: rows in the simulator's allocations.csv format (symbol,as_of,asset_class,weight;
as_of YYYY-MM; weight like 12.3456%, at most 4 decimals, adjusted to add to exactly 100%; zero classes left out),
written only when unclassified is 0
(--out-dir defaults to the data file's folder; point it at the output folder beside the tear sheet)
Money market funds: pass --money-market (100% cash by rule); section=mix rows go into the JSON as cash_detail_pct.
"""
# cspell:ignore asofs htot mmix srcs
import argparse
import csv
import json
import os
import sys
from collections import defaultdict

# The simulator's asset classes (analysis.sim.registries.ASSET_CLASSES), in its display order, plus
# "unclassified", which the simulator also shows but which can't be written to allocations.csv.
SIM_CLASSES = ["stock_us", "stock_intl", "stock", "bond", "cd", "cash", "real_estate", "other"]
CATS = SIM_CLASSES + ["unclassified"]
US = {"us", "usa", "united states", "united states of america", "u.s.", "u.s.a."}


def num(v):
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except (ValueError, TypeError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data")
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--holdings")
    ap.add_argument("--out-dir")
    ap.add_argument("--money-market", action="store_true", help="money market fund: 100%% cash by rule")
    a = ap.parse_args()
    out_dir = a.out_dir or os.path.dirname(a.data) or "."

    reported, region, sector, mmix = {}, {}, {}, {}
    provenance, region_unit, sector_asof = "", "", ""
    weights_asof = set()
    und_w, look = {}, defaultdict(dict)
    asofs, srcs, sector_unit = set(), set(), ""
    with open(a.data, newline="") as f:
        for r in csv.DictReader(f):
            s = (r.get("section") or "").strip().lower()
            m = (r.get("metric") or "").strip().lower()
            p = (r.get("period") or "").strip()
            v = num(r.get("value"))
            if v is None:
                continue
            if s in ("allocation", "lookthrough", "region", "sector", "underlying", "mix"):
                if r.get("as_of"):
                    asofs.add(r["as_of"].strip())
                if r.get("source_url"):
                    srcs.add(r["source_url"].strip())
            if s == "allocation":
                reported[m] = v
                provenance = provenance or (r.get("unit") or "").strip()
            elif s == "underlying" and m == "weight":
                und_w[p] = v
                weights_asof.add((r.get("as_of") or "").strip())
            elif s == "lookthrough":
                look[p][m] = v
            elif s == "region":
                region[m] = v
                region_unit = region_unit or (r.get("unit") or "")
            elif s == "mix":
                mmix[m] = v
            elif s == "sector":
                sector[m] = v
                sector_asof = sector_asof or (r.get("as_of") or "").strip()
                sector_unit = sector_unit or (r.get("unit") or "")

    warns = []
    alloc, method = {}, None

    def from_holdings(path):
        h = {c: 0.0 for c in CATS}
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                w = num(r.get("weight_pct"))
                if w is None:
                    continue
                t = (r.get("asset_type") or "stock").strip().lower()
                c = (r.get("country") or "").strip().lower()
                if t in ("stock", "preferred"):
                    h["stock_us" if c in US else ("stock" if not c else "stock_intl")] += w
                elif t in ("bond", "cash", "other"):
                    h[t] += w
                else:
                    h["other"] += w
        tot = sum(h.values())
        if tot < 98.5:
            h["unclassified"] += 100 - tot
        return h, tot

    if a.money_market:
        method = "rule: money market fund = 100% cash"
        alloc = {c: 0.0 for c in CATS}
        alloc["cash"] = 100.0
    elif reported:
        method = f"reported by {provenance}" if provenance and provenance.lower() not in ("%", "pct", "issuer") else "issuer-reported"
        unknown = [k for k in reported if k not in CATS]
        if unknown:
            warns.append(f"unrecognized categories {unknown}: map them to {CATS} before running")
        alloc = {c: reported.get(c, 0.0) for c in CATS}
    elif und_w and look:
        method = "look-through"
        missing = [t for t in und_w if t not in look]
        if missing:
            warns.append(f"no look-through allocation for {missing}: their weight is counted as unclassified — find each fund's own breakdown")
        alloc = {c: 0.0 for c in CATS}
        for t, w in und_w.items():
            if t in look:
                sub = look[t]
                st = sum(sub.values())
                if abs(st - 100) > 0.5 and st > 0:
                    warns.append(f"{t}'s own allocation sums to {st:.1f}%: rescaled to 100 (say so, or use only its trustworthy lines)")
                for c in CATS:
                    alloc[c] += w * sub.get(c, 0.0) / (st if st > 0 else 100)
            else:
                alloc["unclassified"] += w
    elif a.holdings:
        method = "holdings-computed"
        alloc, tot = from_holdings(a.holdings)
        if tot < 98.5:
            warns.append(f"holdings cover only {tot:.1f}% of the fund: the remaining {100 - tot:.1f}% is put in unclassified — use an issuer-reported breakdown if one exists")
    else:
        print("no allocation inputs found (section=allocation, look-through rows, or --holdings)", file=sys.stderr)
        sys.exit(1)

    crosscheck = None
    if a.holdings and method and not method.startswith("holdings"):
        hc, htot = from_holdings(a.holdings)
        if htot >= 98.5:
            crosscheck = {c: round(hc[c], 2) for c in CATS}
            for c in ("stock_us", "stock_intl"):
                if abs(hc[c] - alloc.get(c, 0)) > 2:
                    warns.append(f"cross-check: holdings give {c} {hc[c]:.1f}% vs {alloc.get(c,0):.1f}% from the {method} split; report the range and say which you used")
        else:
            warns.append(f"holdings file covers {htot:.1f}% only; no cross-check")

    total = sum(alloc.values())
    if abs(total - 100) > 1.0:
        warns.append(f"allocation sums to {total:.1f}%, not 100: check inputs (leverage, short positions or a missing category)")
    if alloc.get("unclassified", 0) > 0.5:
        warns.append(f"unclassified is {alloc['unclassified']:.1f}%: find the missing breakdowns")
    if alloc.get("stock", 0) > 0.5:
        warns.append(f"stock (US/international split unknown) is {alloc['stock']:.1f}%: find domiciles or an issuer region breakdown")
    asofs.discard("")
    weights_asof.discard("")
    as_of = max(weights_asof) if weights_asof else (max(asofs) if asofs else "")
    oldest = min(asofs) if asofs else ""
    if len(asofs) > 1:
        warns.append(f"inputs carry several as-of dates {sorted(asofs)}: allocation dated {as_of} (the fund's own weights); oldest input {oldest} — say so in the note")

    rows = [{"ticker": a.ticker, "as_of": as_of, "category": c, "pct": round(alloc[c], 2), "method": method,
             "source": "; ".join(sorted(srcs))} for c in CATS]
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, f"{a.ticker}-allocation.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    doc = {"ticker": a.ticker, "as_of": as_of, "oldest_input_as_of": oldest, "method": method, "sources": sorted(srcs),
           "allocation_pct": {c: round(alloc[c], 2) for c in CATS}, "total_pct": round(total, 2)}
    if mmix:
        doc["cash_detail_pct"] = {k: round(v, 2) for k, v in mmix.items()}
    if region:
        doc["region_pct"] = {k: round(v, 2) for k, v in region.items()}
        doc["region_basis"] = region_unit or "unspecified"
    if sector:
        doc["sector_pct"] = {k: round(v, 2) for k, v in sorted(sector.items(), key=lambda kv: -kv[1])}
        doc["sector_basis"] = sector_unit or "unspecified"
        if sector_asof:
            doc["sector_as_of"] = sector_asof
    if crosscheck:
        doc["crosscheck_holdings_pct"] = crosscheck
    if warns:
        doc["warnings"] = warns
    sim_path = os.path.join(out_dir, f"{a.ticker}-allocations-sim.csv")
    if alloc.get("unclassified", 0) > 0.005:
        doc["sim_export"] = "not written: unclassified weight must be resolved first"
        if os.path.exists(sim_path):
            os.remove(sim_path)
    else:
        # integer 1e-4 % units, largest-remainder rounding so the set adds to exactly 100%
        raw = {c: alloc[c] * 10_000 for c in SIM_CLASSES}
        units = {c: int(raw[c]) for c in SIM_CLASSES}
        short = 1_000_000 - sum(units.values())
        for c in sorted(SIM_CLASSES, key=lambda c: -(raw[c] - units[c]))[:max(short, 0)]:
            units[c] += 1
        if short < 0:
            warns.append(f"inputs exceed 100% by {-short / 10_000:.4f} points; simulator rows not written")
            doc["sim_export"] = "not written: weights exceed 100%"
        else:
            month = (as_of or "")[:7]
            with open(sim_path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["symbol", "as_of", "asset_class", "weight"])
                for c in SIM_CLASSES:
                    if units[c]:
                        w.writerow([a.ticker.upper(), month, c, f"{units[c] / 10_000:.4f}".rstrip("0").rstrip(".") + "%"])
            doc["sim_export"] = os.path.basename(sim_path)
    with open(os.path.join(out_dir, f"{a.ticker}-allocation.json"), "w") as f:
        json.dump(doc, f, indent=2)

    print(f"{a.ticker} allocation ({method}, as of {as_of or '?'})")
    for c in CATS:
        print(f"  {c:<14} {alloc[c]:6.2f}%")
    print(f"  {'total':<14} {total:6.2f}%")
    for w_ in warns:
        print("WARN", w_)
    print(f"wrote {os.path.join(out_dir, a.ticker + '-allocation.csv')} and .json", file=sys.stderr)


if __name__ == "__main__":
    main()
