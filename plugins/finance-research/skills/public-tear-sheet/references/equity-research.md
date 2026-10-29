# Equity Research Tear Sheet

## Purpose
This is a dense snapshot for an investor evaluating the stock. It emphasizes valuation, forward guidance and consensus, financial trajectory, and what could move the shares. Everything should support or challenge an investment thesis.

**Length:** about one printed page, roughly 900–1,100 words including tables, excluding sources. Density is the convention. Prefer tables over prose, keep sentences short, and put detail you can't fit in the sources list, not in more paragraphs.

## Source plan
Write each result to `tear-sheet-work/data.csv` as you go. See `sources.md` for the sources behind each step.

1. **Identity and market data**: price with date, 52-week range, market cap, shares outstanding, fiscal year end.
2. **Latest earnings release**: quarter results, the company's headline KPI (including non-GAAP), segment or platform mix, cash, debt, and guidance for next quarter and full year.
3. **Multi-year financials**: 4 fiscal years of revenue, operating income, net income, diluted EPS, operating cash flow and free cash flow. You display 3 years plus guidance; the 4th is there for growth.
4. **Consensus and targets**: analyst rating, count, and average and range of price targets from at least two providers. Add forward estimates if any free source has them.
5. **Catalysts**: next earnings date (from the company), product or launch dates, pending deals.
6. **Peers**: 3–5 listed peers with market cap and revenue. Use the comps the user names first.
7. Run `scripts/calc.py`.

## Sections

### 1. Snapshot
The lead sentence is the thesis-relevant headline with a number (e.g. "Trades near its 52-week low three weeks before its largest launch"). If the user framed a question ("thinking about adding to my position"), the lead summarizes what the evidence says about it: valuation vs. history and peers, momentum, and what's priced in. It never tells them to buy, sell, size or time a trade. Follow it with a two-column table of Metric | Value:
- Ticker and exchange
- Price (with date)
- 52-week range
- Market cap
- Shares outstanding
- Cash and short-term investments
- Total debt
- EV
- EV/revenue (trailing, and on guidance)
- P/E trailing and on guidance; write "not meaningful" when earnings are ≤ 0
- Fiscal year end
- Next earnings date
- Consensus rating and target range

### 2. Business overview
2–3 sentences. For a well-known company, lead with what's *changing*, not a Wikipedia summary. Add a compact segment or platform mix table (Segment | Revenue or KPI | % of total) from the latest release if it's disclosed. Skip the table if it isn't, and never leave an empty one.

### 3. Recent results and guidance
Lead with guidance: it's the most actionable forward number. If the company gives no guidance, label the forward rows "Analyst consensus (company gives no guidance)" and use the monthly or quarterly operating releases (e.g. monthly comparable sales) as the most recent hard data. Use one table with these rows: next-quarter guidance, last-quarter actual (with guidance it was measured against: beat or miss), full-year guidance, and prior full-year actual. Columns are the company's headline KPI, GAAP revenue, EPS, and notes. Then 1–2 sentences on segment drivers and margin direction, attributed to management.

### 4. Catalysts and pipeline
This is a dated table of upcoming events, ordered by date, with a "why it matters" column. When one imminent event dominates the story (a flagship launch, a drug readout, a merger vote), add a compact **Confirmed vs. reported** table for it. It separates what the company has confirmed (date, price, platforms) from credible reports and from rumours, each with its source. Add one line on items already released this fiscal year. Label third-party projections as such.

### 5. Financial summary
A chart of the top line by fiscal year with guidance as a range, then a table with fiscal years as rows (newest first). Columns: operating income, net income, diluted EPS, operating cash flow, free cash flow. Add one sentence explaining big swings (impairments, acquisitions), marked as background if not from the filing.

### 6. Peers and valuation
Table: Company | Ticker | Market cap | Trailing revenue | Market cap/revenue (or EV/revenue) | Comparability note. Include recently taken-private peers at deal value when relevant. Lead sentence: where the company sits in the range. (Don't add the subject company as a peer row in `data.csv`, since it would enter the peer median.) Use the same multiple for the subject and its peers: if peers only have market cap, show the subject's market cap/revenue in that table too, and keep EV/revenue in the snapshot.

### 7. Ecosystem (required)
A compact table of 4–8 rows: Company or ticker | Relationship (major customer, supplier, partner, competitor, large private competitor) | What it implies. Use the 10-K's customer-concentration, supplier and competition disclosures, plus major announced partnerships. Include significant private players (a startup taking share counts). The reader uses this to see who else a company's results move, so keep it. Shorten rows to one line each rather than dropping the section.

### 8. Key risks
3–5 bullets. Include the strongest bear argument you found from an outside source (analyst downgrade, short report, critical reporting), sourced. Each bullet should be specific and quantified where possible (concentration, delays, leverage, insider selling, customer or platform dependence).

When it's relevant to pricing, add one **Takeover angle** bullet here or under catalysts. Relevant means sector take-privates or bids, a control structure that blocks or invites a deal, or activist stakes. Write it for any buyer, stating the fact and what it implies; the market prices this in.

### 9. Management and ownership (two lines)
One line naming the CEO and CFO, with any governance quirk (combined chair/CEO, management agreement, dual-class). One line on insider and largest-holder ownership from the proxy, or an open question with the proxy link. No table.

### 10. Sources and data notes
List the pages actually opened, with dates. Note which figures are computed here, which are non-GAAP, and add "not investment advice".

## Cut order if over length
Cut in this order:
1. Business overview to 2 sentences, and drop the mix table if the snapshot covers it.
2. Recent results notes column.
3. Catalysts to the next 4 events.
4. Financial summary table to 3 years and 4 columns.
5. Risks to 3 bullets.

Never cut the snapshot, the peers table, or the ecosystem table.
