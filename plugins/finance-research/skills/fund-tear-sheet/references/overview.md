# Overview tab

## Purpose
The Overview is an on-ramp for a reader who isn't familiar with funds or investing. It doesn't simplify by hiding numbers: it shows the *same* key numbers as the tear sheet, says in plain words what each means, and points to where it sits on the tear sheet — so reading the overview teaches the reader to read the real page.

Write it last, from the finished tear sheet, so every number matches exactly. "Matches" means the same value; adding a unit the table header carries is fine, rounding differently or recomputing is not.

**Length:** 450–700 words. A translated overview follows its language's `translate-<lang>.md` for length and style.

## Structure (English Overview tab)
Open with a 2–3 sentence summary: what kind of fund this is, in everyday words ("a basket of the 100 largest non-financial companies on the Nasdaq exchange, bought and sold like a single stock"). Then five questions; for each give a plain answer with the real number(s), **How to read it:** (what it measures, what's ordinary vs unusual for this kind of fund), and **On the tear sheet:** (where to find it).

1. **What's inside?** The main holdings or asset types, and how concentrated (e.g. "the top 10 companies are about half the fund").
2. **What does it cost?** Expense ratio in plain dollars ("$20 a year per $10,000") and, from fund_calc, what that adds up to over 10 or 20 years compared with a cheaper alternative.
3. **How has it done, and against what?** Annualized returns over the standard periods next to its benchmark, same end date. For money funds, the current yield next to Treasury bills instead.
4. **How bumpy is the ride?** The worst drop or worst calendar year if sourced, interest-rate sensitivity for bond-heavy funds, or (money funds) what keeps the $1.00 price steady and what would make the yield fall.
5. **What should someone know before owning it?** The one or two risks or quirks that matter most (concentration, fee, taxes, glide path, theme), with dates for anything time-sensitive.

Adapt the wording to the type: a target-date fund's question 1 is "what's the mix and where is it heading"; a money fund's question 3 is "what does it pay, and is that good".

Bold each financial term on first use, and only financial terms. Every bolded term must have a Glossary entry.

## Simplify without drifting
Plain language is where facts quietly change. Check each overview sentence against the tear sheet for these:
- **Time-sensitive figures keep their date.** Odds, prices, forecasts and consensus carry "as of" in the text ("7-day SEC yield 4.02% as of Oct 27"; "holdings as of Sep 30"). Never present an old reading in the present tense.
- **Qualifiers survive.** "0.20% after a fee waiver that ends Apr 2027" must not become "costs 0.20%"; "yield as of Oct 27" must not become "yields". The same goes for "about", "reportedly", "estimated", "before taxes" and "so far".
- **Attribution stays attribution.** If a source attributes a move to something, say "one analysis attributes…". Don't promote it to a cause ("because investors worry…").
- **Same word, same measure.** When the tear sheet has two similar measures (7-day SEC yield vs distribution yield; NAV return vs market-price return; gross vs net expense ratio), name each one distinctly every time, in every language.

## Tone
Write as a knowledgeable friend explaining over coffee, not a textbook. Concrete dollar examples ("per $10,000") help more than percentages alone. Short sentences, concrete comparisons, no condescension. Assume an intelligent adult who is new to the vocabulary, not new to thinking. Never give advice on whether to buy, sell, switch or hold.

## Markdown fallback
Write `<ticker>-overview.md` beside the tear sheet, and `<ticker>-overview-<lang>.md` for each translated language.
