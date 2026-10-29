#!/usr/bin/env python3
"""Derived metrics and checks for a fund tear sheet.

Usage:
    python fund_calc.py fund-work/data.csv [--holdings fund-work/holdings.csv] [-o fund-work/calculations.csv]

data.csv columns: section,metric,period,value,unit,source_url,as_of
Conventions (metric names lowercase; percentages as numbers, e.g. 0.20 for 0.20%):
  section=fund        metric=expense_ratio_net | expense_ratio_gross | price | nav | aum | duration
                      | yield_7d | yield_30d | distribution_yield | turnover
  section=returns     metric=fund | fund_market | benchmark | benchmark2 | category    period=1y | 3y | 5y | 10y | since_inception
  section=distribution metric=capgain_per_share | nav_per_share   period=<YEAR>   (capital-gains distributions as % of NAV)
  section=fund        metric=peak_price (with as_of = peak date) | shares_outstanding (millions)
  section=flows       metric=net_flow   period=<period label>   ($M; summed and listed)
  section=fund        metric=top10_share_published (issuer-published top-10 share, %, overrides the partial-file estimate)
  section=calendar    metric=fund | benchmark   period=<YEAR>   (calendar-year total return %; gives best/worst year and years beaten)
                      (annualized total return %, with as_of = period end date)
  section=alt         metric=expense_ratio      period=<TICKER>    (alternatives to compare cost)
  section=compare     metric=yield_7d | tbill_3m | state_exempt_pct  period=<TICKER or 'tbill'>   (money-fund yield comparison;
                      T-bill: use the bond-equivalent (investment) yield, not the discount rate; T-bills are 100% state-exempt)
  section=mix         metric=<asset type, e.g. treasury | agency | repo>   value=%   (echoed; checks sum to ~100)
  section=maturity    metric=wam_days | wal_days   (echoed with SEC caps 60/120)
  section=assume      metric=growth_rate (default 7) | horizon_years (default 10,20) | amount (default 10000)
  section=tax         metric=state_exempt_pct  (share of income from US gov obligations, %)
  section=tax         metric=state_rate   period=<label>   (one row per marginal rate of the tax state, %; gives the
                      state tax-equivalent yield bands; leave out when no tax state is set)
  section=underlying  metric=weight | expense_ratio   period=<TICKER>   (fund-of-funds: weighted cost of the underlying funds)

  growth_rate: set an illustrative rate that fits the asset mix and say so (e.g. ~7 stocks, ~5 balanced, ~4 bonds;
  money funds: the current 7-day yield). If unset, money funds default to their 7-day yield and bond funds to their 30-day yield.

holdings.csv columns: name,ticker,weight_pct,sector,as_of
"""
# cspell:ignore capgain datecheck Δrate Σweight
import argparse
import csv
import os
import sys
from collections import defaultdict


def num(v):
    try:
        return float(str(v).replace(",", "").replace("$", "").replace("%", "").strip())
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data")
    ap.add_argument("--holdings")
    ap.add_argument("-o", "--out")
    ap.add_argument("--fund-of-funds", action="store_true", help="holdings are funds: skip company-concentration metrics")
    a = ap.parse_args()
    out = a.out or os.path.join(os.path.dirname(a.data), "calculations.csv")

    fund, alt, comp, assume, tax, und, cal = {}, {}, {}, {}, {}, {}, {}
    state_rates = []
    fund_asof, comp_asof = {}, {}
    dist = {}
    flows = {}
    mix, mat = {}, {}
    rets = defaultdict(dict)       # metric -> period -> (value, as_of)
    with open(a.data, newline="") as f:
        for r in csv.DictReader(f):
            s, m, p, v = (r.get("section") or "").lower().strip(), (r.get("metric") or "").lower().strip(), (r.get("period") or "").strip(), num(r.get("value"))
            if v is None:
                continue
            asof = (r.get("as_of") or "").strip()
            if s == "fund":
                fund[m] = v
                fund_asof[m] = asof
            elif s == "returns":
                rets[m][p.lower()] = (v, asof)
            elif s == "alt":
                alt[p] = v
            elif s == "compare":
                comp[(m, p)] = v
                comp_asof[(m, p)] = asof
            elif s == "assume":
                assume[m] = v
            elif s == "tax" and m == "state_rate":
                state_rates.append(v)
            elif s == "tax":
                tax[m] = v
            elif s == "mix":
                mix[m] = v
            elif s == "maturity":
                mat[m] = v
            elif s == "flows":
                flows[p] = v
            elif s == "distribution":
                dist.setdefault(p, {})[m] = v
            elif s == "calendar":
                cal.setdefault(m, {})[p] = v
            elif s == "underlying":
                und.setdefault(p, {})[m] = v

    rows, warns = [], []

    def add(metric, value, formula, comps):
        rows.append({"metric": metric, "value": value, "formula": formula, "components": comps})

    # Returns vs benchmark
    for p, (fv, fd) in rets.get("fund", {}).items():
        for other in ("benchmark", "benchmark2", "category"):
            if p in rets.get(other, {}):
                ov, od = rets[other][p]
                if fd and od and fd != od:
                    warns.append(f"{p}: fund return as of {fd} vs {other} as of {od} — not comparable; use the same end date")
                    continue
                add(f"excess_vs_{other}_{p}", f"{fv - ov:+.2f} pts/yr", "fund − " + other, f"{fv} − {ov} (as of {fd or 'n/a'})")
                yrs = {"1y": 1, "3y": 3, "5y": 5, "10y": 10}.get(p)
                if yrs and yrs > 1:
                    cum_f, cum_o = (1 + fv / 100) ** yrs - 1, (1 + ov / 100) ** yrs - 1
                    add(f"cumulative_{p}_fund_vs_{other}", f"{cum_f*100:.1f}% vs {cum_o*100:.1f}%", "(1+r)^n − 1", f"r={fv}%,{ov}%, n={yrs}")
        if p in rets.get("fund_market", {}):
            mv, md = rets["fund_market"][p]
            add(f"market_minus_nav_{p}", f"{mv - fv:+.2f} pts/yr", "market-price return − NAV return", f"{mv} − {fv}")

    # Calendar years
    if cal.get("fund"):
        yrs = sorted(cal["fund"])
        best = max(yrs, key=lambda y: cal["fund"][y])
        worst = min(yrs, key=lambda y: cal["fund"][y])
        add("best_calendar_year", f"{best}: {cal['fund'][best]:+.1f}%", "max of calendar returns", f"{len(yrs)} years")
        add("worst_calendar_year", f"{worst}: {cal['fund'][worst]:+.1f}%", "min of calendar returns", f"{len(yrs)} years")
        growth = 1.0
        for y in yrs:
            growth *= 1 + cal["fund"][y] / 100
        add("cumulative_calendar_years", f"{(growth-1)*100:+.1f}% over {yrs[0]}–{yrs[-1]}", "Π(1+r) − 1", "")
        if cal.get("benchmark"):
            both = [y for y in yrs if y in cal["benchmark"]]
            beat = sum(1 for y in both if cal["fund"][y] > cal["benchmark"][y])
            add("years_beating_benchmark", f"{beat} of {len(both)}", "count fund > benchmark", f"{both[0]}–{both[-1]}" if both else "")

    # Capital-gains distributions as % of NAV
    for y in sorted(dist):
        d = dist[y]
        if d.get("capgain_per_share") is not None and d.get("nav_per_share"):
            add(f"capgain_pct_nav_{y}", f"{d['capgain_per_share'] / d['nav_per_share'] * 100:.1f}%", "capital-gains distribution ÷ NAV", f"{d['capgain_per_share']}/{d['nav_per_share']} (taxable accounts only)")

    # Fee drag
    er = fund.get("expense_ratio_net", fund.get("expense_ratio_gross"))
    if "expense_ratio_net" in fund and "expense_ratio_gross" in fund and fund["expense_ratio_gross"] > fund["expense_ratio_net"]:
        warns.append(f"fee waiver: gross {fund['expense_ratio_gross']}% vs net {fund['expense_ratio_net']}% — state the waiver end date")
    amt = assume.get("amount", 10000.0)
    default_g = fund.get("yield_7d") or fund.get("yield_30d") or 7.0
    g = assume.get("growth_rate", default_g) / 100
    horizons = [int(assume["horizon_years"])] if "horizon_years" in assume else [10, 20]
    if er is not None:
        add("annual_cost_per_amount", f"${amt * er / 100:,.0f} per ${amt:,.0f} per year", "amount × expense ratio", f"{amt} × {er}%")
        for n in horizons:
            gross = amt * (1 + g) ** n
            net = amt * (1 + g - er / 100) ** n
            add(f"fee_drag_{n}y", f"${gross - net:,.0f}", f"value with no fee − value after ER, {g*100:.1f}% gross return, {n}y", f"${gross:,.0f} − ${net:,.0f}")
        for t, aer in alt.items():
            for n in horizons:
                v_self = amt * (1 + g - er / 100) ** n
                v_alt = amt * (1 + g - aer / 100) ** n
                add(f"cost_vs_{t}_{n}y", f"${v_alt - v_self:+,.0f} for {t}", f"same {g*100:.1f}% gross return, ER {er}% vs {aer}%", f"${v_alt:,.0f} vs ${v_self:,.0f}")
        add("assumption", f"{g*100:.1f}% gross annual return, ${amt:,.0f}", "illustrative only", "not a forecast")

    # Fund of funds: weighted cost of underlying funds
    if und:
        w = [(t, d.get("weight"), d.get("expense_ratio")) for t, d in und.items()]
        w = [x for x in w if x[1] is not None and x[2] is not None]
        tw = sum(x[1] for x in w)
        if w:
            wer = sum(x[1] * x[2] for x in w) / tw
            add("underlying_weighted_expense_ratio", f"{wer:.3f}%", "Σ(weight × ER) ÷ Σweight", f"{len(w)} funds, weights sum {tw:.1f}%")
            if er is not None:
                add("wrapper_cost_vs_underlying", f"{er - wer:+.3f} pts", "fund ER − weighted underlying ER", f"{er} − {wer:.3f}")
            if abs(tw - 100) > 2:
                warns.append(f"underlying weights sum to {tw:.1f}%")

    # Money-fund portfolio
    if mix:
        tot = sum(mix.values())
        add("asset_mix", "; ".join(f"{k} {v:.1f}%" for k, v in sorted(mix.items(), key=lambda kv: -kv[1])), "as reported", f"sum {tot:.1f}%")
        if abs(tot - 100) > 2:
            warns.append(f"asset mix sums to {tot:.1f}%")
    for k, cap in (("wam_days", 60), ("wal_days", 120)):
        if k in mat:
            add(k, f"{mat[k]:.0f} days", f"SEC cap {cap}", "")

    # Drawdown from peak, AUM cross-check, flows
    px = fund.get("price") or fund.get("nav")
    if px and fund.get("peak_price"):
        add("below_peak", f"{(px / fund['peak_price'] - 1) * 100:+.1f}%", "price ÷ peak − 1", f"{px} vs peak {fund['peak_price']} on {fund_asof.get('peak_price','?')}")
    if fund.get("shares_outstanding") and fund.get("nav"):
        implied = fund["shares_outstanding"] * fund["nav"]
        add("aum_implied", f"${implied:,.0f}M", "shares outstanding × NAV", f"{fund['shares_outstanding']}M × {fund['nav']}")
        if fund.get("aum") and abs(implied / fund["aum"] - 1) > 0.05:
            warns.append(f"reported AUM {fund['aum']} differs >5% from shares × NAV {implied:,.0f}: one figure is stale; prefer the computed one and give its date")
    if flows:
        add("net_flows_total", f"${sum(flows.values()):+,.0f}M", "sum of listed periods", "; ".join(f"{k}: {v:+,.0f}" for k, v in flows.items()))

    # ETF premium/discount
    if fund.get("price") and fund.get("nav"):
        pd = (fund["price"] / fund["nav"] - 1) * 100
        add("premium_discount", f"{pd:+.2f}%", "price/NAV − 1", f"{fund['price']}/{fund['nav']}")
        if abs(pd) > 0.5:
            warns.append("premium/discount above 0.5% — check that price and NAV share a date")

    # Duration sensitivity
    if fund.get("duration"):
        d = fund["duration"]
        add("rate_sensitivity", f"≈{-d:.1f}% if rates +1 pt; ≈{d:+.1f}% if −1 pt", "−duration × Δrate (first-order)", f"duration {d}")

    # Money-fund comparison and state tax
    y7 = fund.get("yield_7d")
    tb = comp.get(("tbill_3m", "tbill"))
    yd = fund_asof.get("yield_7d", "")
    def datecheck(key):
        cd = comp_asof.get(key, "")
        if yd and cd and yd != cd:
            warns.append(f"{key[1]} {key[0]} is as of {cd} but the fund's 7-day yield is as of {yd}: compare same-date figures or say the dates differ")
        return cd
    if y7 is not None and tb is not None:
        cd = datecheck(("tbill_3m", "tbill"))
        add("yield_vs_tbill", f"{y7 - tb:+.2f} pts", "7-day SEC yield − 3-month T-bill", f"{y7} ({yd or '?'}) − {tb} ({cd or '?'})")
    for (m, t), v in comp.items():
        if m == "yield_7d" and y7 is not None:
            cd = datecheck((m, t))
            add(f"yield_vs_{t}", f"{y7 - v:+.2f} pts", "7-day SEC yield difference", f"{y7} ({yd or '?'}) − {v} ({cd or '?'})")
    if y7 is not None and "state_exempt_pct" in tax and not state_rates:
        warns.append("no tax,state_rate rows: state tax-equivalent bands not computed (no tax state set)")
    if y7 is not None and "state_exempt_pct" in tax and state_rates:
        rates = sorted(set([0.0, *state_rates]))
        ex = tax["state_exempt_pct"] / 100
        bands = []
        for st in rates:  # the tax state's marginal rates (illustrative bands)
            # taxable-equivalent yield vs a fully state-taxable alternative, at a given state marginal rate
            tey = y7 * (1 - (1 - ex) * st / 100) / (1 - st / 100) if st else y7
            bands.append(f"{st:g}%: {tey:.2f}%")
        add("state_tax_equivalent_yield", "; ".join(bands), "yield × (1 − taxable share × t) ÷ (1 − t), by state marginal rate t", f"7-day {y7}%, exempt share {tax['state_exempt_pct']}%")
        # comparison funds and T-bills on the same state bands
        for (m, t), v in comp.items():
            if m not in ("yield_7d", "tbill_3m"):
                continue
            exs = 100.0 if m == "tbill_3m" else comp.get(("state_exempt_pct", t))
            if exs is None:
                warns.append(f"no state_exempt_pct for {t}: its state band not computed")
                continue
            b = []
            for st in rates:
                tey = v * (1 - (1 - exs / 100) * st / 100) / (1 - st / 100) if st else v
                b.append(f"{st:g}%: {tey:.2f}%")
            add(f"state_tax_equivalent_yield_{t}", "; ".join(b), "same formula", f"{v}%, exempt share {exs}%")
        add("state_tax_note", "illustrative bands for a taxable account only; no assumption about the reader's bracket", "federal tax applies to all of it; inside an IRA/401(k) the state exemption doesn't apply (traditional-IRA withdrawals are taxed as ordinary income; qualified Roth withdrawals are tax-free)", "")

    # Holdings concentration
    if a.holdings and a.fund_of_funds:
        warns.append("fund of funds: concentration metrics skipped; describe the look-through holdings of the underlying funds instead")
    if a.holdings and not a.fund_of_funds:
        with open(a.holdings, newline="") as f:
            hs = [(r.get("name"), num(r.get("weight_pct")), (r.get("sector") or "").strip(), r.get("as_of")) for r in csv.DictReader(f)]
        hs = [h for h in hs if h[1] is not None]
        hs.sort(key=lambda h: -h[1])
        tot = sum(h[1] for h in hs)
        if hs:
            add("holdings_rows", len(hs), "rows in holdings file", f"weights sum {tot:.1f}%")
            if tot < 95:
                warns.append(f"holdings weights sum to {tot:.1f}% — file is partial (e.g. top 10 only); effective number of holdings is an upper bound (missing holdings would add concentration)")
            if fund.get("top10_share_published") is not None:
                add("top10_share_published", f"{fund['top10_share_published']:.1f}%", "issuer-published", f"as of {fund_asof.get('top10_share_published','?')}")
            for n in (5, 10, 25):
                if len(hs) >= n:
                    add(f"top{n}_share", f"{sum(h[1] for h in hs[:n]):.1f}%", f"sum of top {n} weights", "")
            hhi = sum((h[1] / 100) ** 2 for h in hs)
            add("effective_number_of_holdings", f"{1/hhi:.0f}", "1 ÷ Σ(weight²)", f"HHI {hhi:.4f}")
            sec = defaultdict(float)
            for h in hs:
                sec[h[2] or "unclassified"] += h[1]
            for k, v in sorted(sec.items(), key=lambda kv: -kv[1])[:8]:
                add(f"sector_{k}", f"{v:.1f}%", "sum of weights", "")
            dates = {h[3] for h in hs if h[3]}
            if len(dates) > 1:
                warns.append(f"holdings rows carry different as-of dates: {sorted(dates)}")

    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["metric", "value", "formula", "components"])
        w.writeheader()
        w.writerows(rows)
    for r in rows:
        print(f"{r['metric']:<36} {str(r['value']):>34}   {r['formula']}  [{r['components']}]")
    for x in warns:
        print("WARN", x)
    print(f"\nwrote {len(rows)} rows to {out}; {len(warns)} warnings", file=sys.stderr)


if __name__ == "__main__":
    main()
