# Public data sources for funds

Network reach varies by session: some hosts are blocked for the fetch tool, and issuer sites often render with JavaScript and return little. When a page fails, move down the list; a URL that appears in a web search result usually fetches even when a typed URL is refused. Issuer **fact sheet PDFs** are the most reliably fetchable source of current figures.

## Issuer documents (best)
- **Fact sheet** (PDF, usually monthly or quarterly): expense ratio, assets, holdings count, top holdings, sector mix, returns vs benchmark, yields, manager. Search `<ticker> fact sheet pdf`.
- **Fund page** on the issuer site (Invesco, ARK, Schwab, iShares and the other large issuers): daily NAV/price, yields, holdings downloads. ARK publishes daily full holdings (CSV) on its site. When it's blocked, use aggregators (TipRanks, ETF.com, StockAnalysis) with their as-of dates, and note disagreements.
- **Summary prospectus / prospectus**: objective, principal strategies and risks, fee table (gross/net, waivers, 12b-1, loads), managers and tenure, share classes, glide path for target-date funds. Search `<fund name> summary prospectus`.
- **Annual / semiannual shareholder report (N-CSR)** and the tailored shareholder report: audited financial highlights (expense ratio, turnover, distributions), manager commentary.
- **Capital-gains distribution estimates**: issuers post estimated year-end distributions each Oct–Nov (usually as PDFs); search `<issuer> estimated capital gains distributions <year>`.
- **Prospectus supplements (SEC 497)**: announced mergers, fee changes, manager changes, glide-path changes. EDGAR search `<fund name> 497`.
- **Distribution history / tax information**: capital-gains distributions by year; "% of income from US government obligations" for state-tax treatment (issuer tax center, published each January).

## SEC filings
- **N-PORT** (monthly holdings, filed quarterly, public with a lag): full holdings with values. EDGAR search `<fund name> N-PORT`.
- **N-MFP** (money market funds, monthly): every holding, WAM, WAL, yields, liquidity. Issuer sites usually present the same data more readably. When EDGAR and the issuer site are blocked, WAM/WAL and the asset mix are usually on the issuer's monthly fact sheet PDF or its "monthly holdings" PDF; Crane Data and the fund's annual report are fallbacks.
- **485BPOS** filings contain prospectus updates (fee changes, manager changes).

## Index providers (index funds)
- Methodology documents: Nasdaq (Nasdaq-100), S&P Dow Jones, CRSP (used by some large index-fund families), FTSE Russell, Bloomberg (bond indexes). Search `<index> methodology pdf`.

## Market data and aggregators
- ETF price, premium/discount, volume, AUM: issuer page; StockAnalysis `/etf/<ticker>/` (often blocked in sandboxes), ETF.com, Yahoo Finance, Google Finance, Morningstar quote pages (often partially paywalled).
- Mutual fund NAV and returns: issuer page; Yahoo Finance; AAII fund pages; Morningstar (attribute ratings and analyst views, with date).
- Daily money-fund yields when issuer pages are blocked: YieldFinder, Crane Data, MarketWatch quote pages. Label them third-party and give the date.
- Treasury bill yields (for money-fund comparison; use the bond-equivalent/investment yield, not the discount rate): US Treasury daily par yield curve (treasury.gov), FRED (series DTB3).

## Independent views (skeptic's pass)
Search several phrasings: `<ticker> criticism`, `<fund> underperformed benchmark`, `<fund> outflows <year>`, `<fund> manager change`, `<ticker> concentration risk`, `<fund> active share`, `<fund> capital gains distribution <year>`, Morningstar analyst report summaries (attributed), Bogleheads threads only as pointers to primary data, never as the source of a figure.

## Freshness
- Prices and money-fund yields: within 2 trading days.
- Holdings: say the as-of date; month-old is normal for most funds, daily for ARK.
- Returns: standardized returns at the last month- or quarter-end are normal; say which.
- Expense ratio: latest prospectus date.
