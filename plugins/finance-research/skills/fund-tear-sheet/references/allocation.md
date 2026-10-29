# Allocations section and export

Every fund tear sheet has an **Allocations** section, right after the Snapshot. It gives the fund's asset mix in fixed categories, and is also written out as a machine-readable file so it can be imported into planning software. Run `scripts/allocation.py` to produce it. Never hand-total the numbers.

## Categories (the retirement simulator's asset classes; percent of net assets; sum to 100)
These are exactly the simulator's `registries.ASSET_CLASSES`, in its display order, so the export loads into its `allocations.csv` without mapping. `unclassified` is the one extra bucket. The simulator shows it too, but it can't be written to `allocations.csv`, so it must be resolved before export.

| Key | Meaning |
| --- | --- |
| `stock_us` | stocks of US-domiciled companies |
| `stock_intl` | stocks of companies domiciled outside the US, including ADRs and US-listed foreign companies |
| `stock` | stocks whose US/international split couldn't be determined (ideally 0) |
| `bond` | all bonds: Treasury, agency, mortgage-backed, corporate, TIPS, foreign bonds |
| `cd` | certificates of deposit held directly (rare in funds; CDs inside a money fund are `cash`) |
| `cash` | cash and money-market instruments held for liquidity; a money market fund is 100% cash |
| `real_estate` | real estate held directly. REITs are stocks, not `real_estate` |
| `other` | anything else, named in a note (commodities, net derivative exposure, business development companies, private funds) |
| `unclassified` | weight whose asset type couldn't be determined at all; must be 0 for the simulator export |

## Classification rules
- **Prefer the issuer's own breakdown;** an aggregator's (Morningstar via a broker site) is the fallback, labelled as such, with its date. When sources disagree on the US/foreign split, report the range in the note and use the dated, more authoritative one. Fact sheets usually report asset classes, and often US vs. international. Map them onto the keys. A breakdown of the equity portion only, by "domestic / international", is applied to the fund's stock share.
- **Domicile:** use the country the issuer, or Morningstar, assigns to each holding. Companies incorporated abroad for tax reasons but run from the US (e.g. some Irish-domiciled US firms) follow that assignment. Say which convention the source uses. ADRs are foreign.
- **Fund of funds (target-date, allocation):** look through to each underlying fund's own breakdown, weighted by its share. The underlying funds' fact sheets often lack a cash or US/foreign split, so an aggregator's breakdown of each underlying fund (AAII, Morningstar via a broker), labelled and dated, is the normal source here. Breakdowns that don't add to 100 are rescaled by the script, with a warning. The allocation's date is the date of the fund's own weights, and the oldest input date is reported alongside it. A total-stock-market fund isn't 100% US, since it holds some cash and a few foreign-domiciled names, so use each underlying fund's figures, not assumptions. Run with `section=underlying` and `section=lookthrough` rows.
- **Money market funds:** 100% `cash`, even though they hold T-bills and repo. Planning tools treat them as cash.
- **Bond funds:** fund-level cash goes to `cash` and the rest to `bond`. A finer split (government / corporate / foreign / inflation-protected) goes in the export's region and sector maps when the issuer reports it.
- **Equity funds:** fund-level cash goes to `cash`, including cash backing futures if the fund reports it that way. Privately held stocks (e.g. a stake in an unlisted company) are stocks: classify them by domicile, and add a note that they're valued by the manager.
- **Issuer fallback:** if the fund page is unreachable, the issuer's own SEC holdings schedule (N-PORT, or the quarterly holdings or N-CSR schedule) is still issuer data. Build the split from its country and asset-class subtotals, and label it `issuer-reported (SEC schedule, <date>)`.
- **Net other assets / liabilities:** net them against cash, and say so ("cash 1.0% = 4.0% money funds − 3.0% net other liabilities").
- **Business development companies, closed-end funds and other listed fund vehicles** go to `other`, named in the note. **Private preferred or common stock** goes to stock by domicile; give it the sector "private_unclassified" when no sector is assigned.
- **Aggregator line items:** Morningstar's "Preferred" counts as stock (by domicile if known, otherwise `stock`). Its "Other" goes to `other` unless you can identify it.
- **Labels that contradict holdings:** if a source's region label conflicts with the actual holdings (e.g. "Europe Emerging" for companies domiciled in developed Europe), use the holdings-based classification, and note the conflict. Pass the full holdings file alongside a reported split and the script reports the cross-check.
- **Securities-lending collateral** isn't the fund's own exposure, so exclude it from cash. Use the fund's net cash as reported.
- **Leverage or short positions** can push totals above or below 100. Report the net exposure the issuer states, and explain it.

## On the tear sheet
A compact table showing Category · % · method (issuer-reported, reported by an aggregator such as "Morningstar via Schwab", look-through, holdings-computed, or the money-market rule) · as-of date · source. Add one sentence if anything is unusual: unknown or unclassified weight, a stale date, or a domicile convention that matters. Lead with the headline split ("About 33% stocks (20% US, 13% international), 64% bonds, 3% cash as of Sep 30, 2026").

## In Detail (machine-readable)
- **Allocation (CSV):** a fenced ```csv block with the contents of `<TICKER>-allocation.csv` (`ticker,as_of,category,pct,method,source`), so it can be copied straight into another tool.
- **Simulator rows:** a fenced ```csv block with `<TICKER>-allocations-sim.csv`. These are rows in the simulator's own `allocations.csv` format (`symbol,as_of,asset_class,weight`; `as_of` as YYYY-MM; weights like `12.3456%`, summing to exactly 100%; zero classes left out), ready to append to the data repo's `allocations.csv` or to pass to `sim symbol add --weight`. The script writes this file only when `unclassified` is 0; otherwise say why it's missing.
- **Allocation (JSON):** a fenced ```json block with `<TICKER>-allocation.json`. It includes `region_pct` and `sector_pct` when available.
- **Regions** (when reported): us, developed_ex_us, emerging, frontier_other. Use the issuer's split, and record its basis (`unit`: "% of equity" or "% of fund"). Map finer regions with MSCI's market classification: Canada, Europe developed, Japan and Australasia go to developed_ex_us; Latin America, Asia emerging, and the emerging parts of Europe, the Middle East and Africa go to emerging. Keep the original region table in Detail.
- **Sectors** (when reported, for equity or equity-heavy funds): GICS sectors as snake_case keys (information_technology, health_care, financials, consumer_discretionary, communication_services, industrials, consumer_staples, energy, utilities, real_estate, materials). Record the basis and the scheme in the `unit` column, e.g. "% of equity; GICS" or "% of fund; ICB". If only another scheme (ICB, Morningstar) is available and current, use its own sector names in snake_case rather than forcing a GICS mapping. Prefer a current figure in another scheme over a stale GICS one.

The script writes `<TICKER>-allocation.csv`, `<TICKER>-allocation.json` and (when clean) `<TICKER>-allocations-sim.csv` into `--out-dir`, which should be the output folder beside the tear sheet. In a Claude doc, their contents go in the Detail tab's code blocks. For money market funds, run with `--money-market`; their `mix` rows (T-bills, agency, repo) appear in the JSON as `cash_detail_pct`.
