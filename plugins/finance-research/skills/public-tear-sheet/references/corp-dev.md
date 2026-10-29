# Corporate Development Tear Sheet

## Purpose
This is a target evaluation for an internal corp dev team weighing an acquisition. It is analytical, not promotional. It asks how the target's products, customers and technology overlap with or complement the acquirer's, and how acquirable the target is at all. If the user names the acquirer, tailor the strategic fit to that acquirer.

It does not include an integration plan or a diligence checklist. Those belong to the deal team once a look is approved, and written from public data they become generic. Acquirability is different. What makes the company easy or hard for *any* buyer to take over (control structure, size, regulation, sector premiums) is also what the market prices in, so it stays.

**Length:** about 1,000–1,300 words (1–2 printed pages).

## Source plan
Log each result to `tear-sheet-work/data.csv`. See `sources.md`.

1. **Target profile**: products, platform, customers, HQ, founded, employees (10-K Item 1, company site).
2. **Financials**: 4 years of revenue, gross profit, operating income, R&D, capex and FCF, plus debt and cash. R&D intensity matters to this audience.
3. **Segments and customers**: segment revenue, customer concentration (10-K "major customers" or risk factors), customer type, contract model, geographic mix.
4. **Relationships and competition**: competitors named in the 10-K, technology partners and integrations, and any existing relationship with the acquirer.
5. **Valuation and peers**: market cap, EV, and EV/revenue (and EV/gross profit when the company has a large pass-through revenue line, such as payments) vs. 3–5 peers, with peer median. Add sector take-private or acquisition premiums where news reports them.
6. **Control and takeover factors**: dual-class shares and their sunset, founder and insider voting power, large holders (proxy, 13D/13G), charter defenses (staggered board, poison pill) where disclosed, and regulatory exposure (antitrust overlap, licensing, foreign-ownership limits).
7. Run `scripts/calc.py`.

## Sections (priority order)

1. **Header and bottom line**: identifiers. Then the lead: one or two sentences answering the user's actual question. For example, "Worth a deeper look: strong strategic overlap with QuickBooks payroll, but founder voting control until 2028 makes any deal negotiated, not hostile." This is an analytical read of the evidence, not a recommendation to buy. Follow it with one sentence on what an acquirer *gets*, in neutral words rather than the company's tagline.
2. **Business and product overview**: one paragraph, 4–6 sentences, from an acquirer's point of view.
3. **Revenue mix and customer profile**: segment table if available, then 2–3 sentences on concentration (flag it if the top customers are over 30%), customer type, contract model and geography.
4. **Strategic fit analysis** (the signature section): three buckets of 2–3 sentences of reasoning each: customer overlap; technology/product complement; competitive displacement. Name the acquirer's own products where the user named the acquirer.
5. **Financial summary**: 3 fiscal years plus LTM if available. Rows: revenue, growth, gross margin, EBITDA or operating income (labelled GAAP or adjusted), R&D, R&D % of revenue, capex, FCF, FCF margin. Add a capital-structure sub-table (debt, cash, net debt or net cash).
6. **Valuation context** (required for public targets): company vs. peers multiples table with median, then one sentence placing the company in the range.
7. **Takeover profile** (labelled analysis, written for any buyer). 3–5 bullets, each a fact plus what it means for acquirability and pricing:
   - control (voting power, dual-class sunset date);
   - size (EV against the plausible universe of strategic and PE buyers, plus the financing that implies);
   - regulatory or antitrust exposure;
   - sector deal precedents and premiums;
   - defenses.

   Don't include integration plans, synergy estimates or diligence checklists.
8. **Sources and data notes.**

## Cut order if over length
Cut in this order: customer prose to 2 sentences → valuation prose (keep the table) → capital-structure sub-table merged into the financial table → strategic fit compressed to 2 sentences per bucket. Never cut the bottom line, the overview, the financial table, or the takeover profile.
