# Private Company: Investment Research Tear Sheet

## Purpose
This is for an investor who can't buy the stock (yet) but needs to understand the company, because it moves the public names around it. A large startup is a customer of some public companies, a competitor to others, and a valuation mark for a whole sector, and its IPO would be a market event. The question this sheet answers is: what does this company tell me about the public market, and what would change the picture?

What the company sells, and to whom, shows who it competes with. What it buys, and from whom, shows who it enriches. Those two lists, tied to tickers, are the signature of this sheet.

**Length:** about 1,000–1,300 words in the body, excluding sources. Rough split: snapshot and business ~250, funding ~150, claims vs. scrutiny ~200, read-through ~350, catalysts and risks ~250.

## Source plan
Log each result to `tear-sheet-work/data.csv`. See `sources.md` (Private companies).

1. **Status**: public or private *today*, any IPO filing or statement from the CEO about timing, and recent tender offers or secondary sales (reported prices are a valuation signal; label the source).
2. **Funding and valuation**: rounds with date, amount, post-money valuation and lead investors (company releases, reputable news). Note strategic investors that are public companies (e.g. a chipmaker or hyperscaler in the round), since their stake is a read-through.
3. **Scale claims**: run-rate revenue, growth, customer counts, net retention, profitability or FCF claims, and headcount. Take them as the company stated them, each with date and source.
4. **What it sells**: products, pricing model, who buys, and named competitors, especially public ones.
5. **What it buys and who it partners with**: cloud and compute providers, chip suppliers, data and infrastructure vendors, distribution partners and marketplaces, and named large partnerships or commitments (multi-year cloud deals, GPU purchases). Look in company announcements, partner releases, and the public partner's own 10-K or earnings call, which sometimes names large customers.
6. **Scrutiny**: independent views on the claims and the mark. Examples: analyst notes decomposing growth (core vs. resale or pass-through), reported gross-margin trends, valuation models from PitchBook or similar, secondary-market discounts, and critical reporting.
7. **Catalysts**: IPO signals, product events, and earnings dates of the most exposed public names. Include only events still in the future.

## Sections (priority order)

1. **Snapshot**: lead with the bottom line for an investor, e.g. "Private at a $190B mark; its >$7B run-rate makes it the largest independent buyer of X and the main share-taker from Y. An IPO isn't expected before 2027 per the CEO." Then a compact table:
   - status;
   - last round (date, amount, post-money, leads);
   - claimed run-rate and growth (as stated, dated);
   - customers;
   - headcount range;
   - public strategic investors.
2. **Business**: 3–4 sentences on what it sells, to whom and how it charges, and what's changing.
3. **Valuation and funding history**: table of Date | Round | Amount | Post-money | Leads. Show an implied multiple of claimed run-rate only when both figures are sourced and close in date. Label it "implied, on company-claimed run-rate". Compare it with public peers' EV/revenue (calc.py) in one sentence. A run-rate is forward-looking (latest month or quarter × 12), so compare it with peers' forward or latest-quarter-annualized revenue where available, or say that the comparison flatters the private company.
4. **Claims vs. scrutiny**: a short table of Company claim | Independent view | Source. Cover growth quality, margins, and the valuation mark. If no independent view could be found for a claim, say so; that's informative too.
5. **Market read-through** (the signature section). Two short tables, each row with its source:
   - **Sells against:** Company or ticker | What overlaps | Implication. Examples: share loss, pricing pressure, validation of a category. Make each implication specific to that company, with evidence from its own latest results (growth, retention, management comments) where available. "Competitive pressure" alone isn't an implication.
   - **Buys from or partners with:** Company or ticker | What it buys or does together | Implication. Examples: revenue exposure, concentration, contract size if reported.

   Then 1–2 sentences on the startup as a valuation mark. What does its round imply for listed peers' multiples, and what would its IPO do to the sector's supply of growth stock?
6. **Catalysts and signals to watch** (future-dated only): dated list covering IPO filings or statements, funding or tender news, major launches or events, and the exposed public names' next earnings.
7. **Key risks**: 3–5 bullets. Typical ones: claims that are unaudited, dependence on one cloud or chip supplier, competition from its own suppliers, a mark-down risk if the next round prices lower, and regulatory exposure.
8. **Sources and data notes**: every figure dated, with company claims marked unaudited.

## Thin or conflicting data
- **Same name, different company.** Small startups often share names, and aggregators sometimes merge or duplicate profiles. Before using any aggregator profile, check that its founders, location and product match the company you identified. Investor lists and funding totals are the fields most often crossed over. If a profile can't be matched with confidence, leave its figures out and say so in Detail.
- **Aggregator figures** (CB Insights, Tracxn, PitchBook previews, Crunchbase) are often undated, unsourced or contradictory. Use a figure on the tear sheet only if it's dated and traceable to a round announcement or reputable report. Otherwise keep it off page one entirely, not even mentioned as rejected, and put it in Detail, labelled "unverified aggregator figure", with the site and the date you saw it. When aggregators disagree, show the range and which site said what.
- **Indirect stakes:** a public company that backs the fund that invested is an indirect exposure. Label it that way (`indirect, via <fund>; Boeing is anchor LP`), and say if it's uncertain whether that particular fund made the investment.
- **Small exposures still count:** if the only real public read-through is to a microcap, say so plainly and quantify it relative to that company's revenue. The large names it brushes against get one line saying the effect isn't material.
- **Expected dates:** list an event whose date isn't announced as `expected ~<month>, not yet announced (based on prior-year timing)`.

## Cut order if over length
Cut in this order: risks to 3 bullets → business to 2 sentences → funding table to the last 4 rounds. Never cut the snapshot, the claims-vs-scrutiny table, or the market read-through.
