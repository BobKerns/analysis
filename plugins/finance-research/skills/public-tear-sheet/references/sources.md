# Public data sources for tear sheets

Where to find each kind of data, best source first. Network access varies by session. Some hosts are blocked for the fetch tool or the shell, and others fail at the site. When one fails, move down the list rather than retrying it. A URL reached through a web search result usually fetches even when a typed-in URL is refused, so search for the page, then fetch the result link.

## Identity and fiscal calendar
- Company IR site (search `<company> investor relations`): fiscal year end, segments, press releases.
- SEC EDGAR filing index (search `<ticker> 10-K sec.gov`): legal name, CIK, fiscal year end on the 10-K cover.

## Latest results and guidance
1. **Earnings release**: the 8-K exhibit 99.1 on sec.gov (search `<company> reports results fiscal <quarter> <year>`). It has revenue, non-GAAP KPIs, EPS, platform or segment mix, cash and debt, and next-quarter and full-year guidance, all in one page.
2. The same release on businesswire.com / globenewswire.com / prnewswire.com, or the company IR news page.
3. Earnings call transcripts (Motley Fool, Seeking Alpha, Webull) for management commentary. Paraphrase rather than quote at length.

## Multi-year financials (4 fiscal years: display 3, use the 4th for growth)
1. **SEC XBRL API**: `data.sec.gov/api/xbrl/companyconcept/CIK##########/us-gaap/<Concept>.json` or `companyfacts`. It's the cleanest structured data, but often blocked in sandboxed sessions. Try it once.
2. **The 10-K itself** (Item 8, or the "Selected financial data" and MD&A tables). It's authoritative and always fetchable from sec.gov through a search result. Long, so fetch with a targeted prompt or an offset.
3. **Aggregators** (reachability varies by session; try two before giving up): StockAnalysis `/stocks/<ticker>/financials/`, Macrotrends, Google Finance, Motley Fool quote pages, Yahoo Finance.
4. Cross-check the latest year against the company's own release. If an aggregator disagrees with the filing, the filing wins.

## Market data (price, market cap, 52-week range, shares)
- Google Finance, StockAnalysis `/stocks/<ticker>/` and `/market-cap/`, Heygotrade, Motley Fool, Investing.com, CNN Markets, Yahoo. Record the price **and its date**, and prefer a stated close over an intraday quote.
- Shares outstanding: the 10-Q/10-K cover page is authoritative. Otherwise market cap ÷ price, labelled "implied".
- Enterprise value: market cap + total debt − cash and short-term investments, from the latest balance sheet. Compute it yourself (calc.py) and say so.

## Analyst consensus and price targets
- Quote pages (Heygotrade, MarketBeat, StockAnalysis `/forecast/`), and news summaries.
- Providers disagree on analyst count and average target. Report the range across 2 sources.
- Consensus EPS/revenue estimates are often paywalled. If you can't fetch them, write "consensus estimates not available from free sources", don't guess.

## Peers
- StockAnalysis industry and list pages (e.g. `/list/gaming-stocks/`) give market cap and revenue for many names in one fetch. Compute market cap / revenue yourself.
- The 10-K "Competition" section names competitors in the company's own words.
- Check whether a would-be peer is still public. Take-privates (e.g. EA in Aug 2026) make useful valuation benchmarks: deal value and per-share price from news of the closing.

## M&A activity
- The company's own press releases and 8-Ks (Item 2.01 completion of acquisition), and 10-K notes on business combinations.
- News for deal values. Use "undisclosed" only when no source gives one.
- Precedent transactions with multiples are rarely free. Say "not available from free sources" rather than inventing multiples.

## Management and ownership
- **Proxy statement (DEF 14A)**: executive officers, board, chair or lead director, beneficial ownership table (5% holders, insiders as a group). The ownership table sits late in the document, often past the point where the fetch tool truncates, even with an offset. When that happens, search for `<company> beneficial ownership 5% holders` or the holders' Schedule 13G/13D filings, or use an aggregator's institutional-holders page (dated). Control terms (dual-class sunset, staggered board, supermajority provisions) are also in the 10-K's "Description of securities" exhibit (usually ex. 4.x), which is short and fetches whole. Failing all of these, write it as an open question with the proxy link.
- **Takeover factors**: dual-class terms and sunset dates (10-K "Description of capital stock" exhibit, proxy), staggered board or rights plans (proxy, 8-K), activist 13D filings, sector take-private premiums (news of announced deals).
- 13F/13G-based holder lists on aggregator sites as a fallback, dated.
- Insider transactions: Form 4 summaries on quote sites.

## Catalysts and news
- Company press releases for scheduled events (earnings dates, launches, investor days).
- Reputable news for the rest. Prefer primary reporting over aggregators, and date every item.

## Private companies
- Funding: company press releases, reputable tech or business news, SEC Form D (amount raised, not valuation).
- Valuation: only as "last reported round valuation, date, per source".
- Headcount: company site or reputable news. LinkedIn figures are approximate.
- Revenue: only if the company or reputable reporting disclosed it, with date and source.

## Skeptic's view and earnings quality
Run several differently-worded searches, because critical takes rarely rank first:
- `<company> bear case`, `<company> short report`, `<company> downgrade <month year>`
- `<company> growth breakdown` / "organic growth" / "excluding" (acquisitions, FX, gas, pass-through or resale revenue)
- `<company> gross margin decline`, `<company> one-time` / "non-recurring"
- named brokers: `<company> Jefferies` / "Morgan Stanley" / "Bernstein" note; PitchBook or Forge valuation for private companies
- for a competitor's read-through, the competitor's own latest earnings call: does management mention the company, and do retention or growth figures show damage?

If the searches turn up nothing, say "no independent view found". Never invent one.
