# Investment Banking / M&A Tear Sheet

## Purpose
This profile puts the company in a transaction context: a potential target, a potential acquirer, or a pitchbook entry. It emphasizes strategic positioning, segment detail, relationships and M&A history. Valuation is framed in transaction multiples, not as an investment thesis.

**Length:** about 1,000–1,300 words (1–2 printed pages).

## Source plan
Log each result to `tear-sheet-work/data.csv`. See `sources.md`.

1. **Profile**: HQ, founding year, employees (10-K cover or Item 1), ownership type (public, PE-backed with sponsor, founder-controlled).
2. **Segments and financials**: segment revenue for 2 years (10-K segment note or the release), and 4 years of revenue, gross profit, operating income, net income, capex and FCF. Use EBITDA if the company reports it as non-GAAP, labelled; otherwise use operating income plus D&A from the cash-flow statement, labelled "computed". Add LTM if a 10-Q has been filed since the 10-K.
3. **Capital structure**: debt, cash, and credit rating (only if the company or a reputable report states it).
4. **Trading comps**: 3–5 listed peers (user's comps first, then the 10-K's competition section) with market cap, revenue, and growth. Compute EV/revenue. Forward multiples only when a free source gives them.
5. **M&A activity**: the company's own deals over 5 years (press releases, 8-K Item 2.01, 10-K business-combination notes) with deal values. Add precedent transactions in the specific sub-sector from news, only where a value is reported.
6. **Relationships and ownership**: major customers (10-K concentration disclosures), key partners and suppliers named in filings or releases, competitors, and the proxy's 5% holders.
7. Run `scripts/calc.py`.

## Sections (priority order)

1. **Header and lead**: name, ticker or "Private company", HQ, founded, employees, market cap or last reported valuation, EV, ownership type. Lead sentence: the company's strategic position in one line, plus its takeover profile in a clause (e.g. "widely held, no defenses, ~$40B EV puts it within reach of 3–4 strategics").
2. **Business overview**: a 4–6 sentence pitchbook paragraph covering revenue model, customer base at scale, named moats, and growth vectors. Rewrite it, don't paste.
3. **Revenue and segment breakdown**: Segment | Revenue | % of consolidated total | YoY. Use consolidated revenue as the denominator. Footnote pending divestitures and show pro-forma mix. If no segment data exists, describe the mix in 2–3 sentences instead.
4. **Financial summary**: 3 fiscal years plus LTM (if available). Rows: revenue, growth, gross margin, EBITDA (labelled source), margin, operating income, net income, capex, FCF, FCF margin. Below it, a capital structure sub-table: debt, cash, net debt, net debt/EBITDA, rating.
5. **Trading comps**: company plus peers in columns, with a peer median. List peer names even if multiples are partial.
6. **M&A activity** (the signature section): (a) the company's own deals as Date | Target or divested | Deal value | Type | Rationale, never "undisclosed" when a source reports a value; (b) precedents in the sub-sector, or "not available from free sources".
7. **Key relationships**: customers, suppliers, partners and competitors, each with a parenthetical saying what the relationship is.
8. **Ownership snapshot** (cut first): top holders and insider %, from the proxy; for PE-backed companies, the sponsor and year.
9. **Sources and data notes.**

## Cut order if over 2 pages
Cut in this order: ownership → precedent transactions → relationship descriptors → capital-structure prose → trading comps down to 3 peers. Never cut the overview, segments, financial summary, or the company's own M&A.
