---
name: public-tear-sheet
description: Build an investment research tear sheet (one-pager, company profile, fact sheet, snapshot, "current picture") on a named public company or private startup from free public sources such as SEC filings, investor-relations releases, free quote pages, funding news and partner announcements, with no S&P Capital IQ, FactSet, Bloomberg or other paid data connector. Use whenever someone wants to understand a company as an investor, including startups whose customers, suppliers and competitors are public companies, or asks for a tear sheet, company overview, valuation snapshot, or how a company affects the public names around it. Also covers IB/M&A, corp dev target and sales/BD briefing variants on request. Delivered as an editable Claude doc by default.
---

# Public-Source Tear Sheet

Build a company tear sheet from public sources, and show where every number came from. The default reader is an investor. For a public company that means the stock. For a private company it means what the company tells you about the public market: who it takes share from, whose revenue it is, and what its valuation implies. A paid terminal hands you clean, pre-computed fields. Here you assemble them yourself from filings and web pages that sometimes disagree, go stale or fail to load. The skill's value lies in that discipline: clear sources, dates and arithmetic, and visible gaps.

<!-- defaults:start (rendered from defaults/public-tear-sheet.md.j2; edit the template, then run: analysis research-plugin render --neutral) -->
## Defaults for this installation
- **Tabs:** Overview, Tear sheet, Price chart, Detail, Glossary.
- **Translated tabs:** none.
- If the user asks for no overview, no price chart or no glossary on a particular sheet, leave that tab out.
<!-- defaults:end -->

If a paid financial-data connector *is* connected and enabled in the session (S&P Capital IQ / Kensho, FactSet, etc.), prefer the skill built for it. This skill is for when there is none.

## Step 1 — Inputs

1. **Company**: name or ticker. Resolve to legal name, ticker (or "private"), exchange, and fiscal year end. Many companies don't use calendar years (Take-Two ends March 31; Costco, the Sunday nearest Aug 31), and labelling depends on it. Watch for 52/53-week years and uneven quarters (Costco's Q4 is 16 weeks). Check whether the company is public *today*, since IPOs and take-privates change what's available. A private company that doesn't publish a fiscal year end doesn't need one: label figures by the date they were reported. For small private companies, confirm the identity too: founders, HQ and product should match across sources, because same-named startups are common.
2. **Template**: investment research is the default. Choose by listing status:
   - Public company → `references/equity-research.md`
   - Private company or startup → `references/private-company.md`

   Use another audience only when the request clearly calls for it:
   - "pitchbook", "M&A profile" → `references/ib-ma.md` (company in transaction context)
   - "we're looking at acquiring" → `references/corp-dev.md` (acquisition target from one acquirer's side)
   - "prep me for a meeting / sales call" → `references/sales-bd.md` (rep preparing for a meeting)

   Don't ask which audience unless the request is truly ambiguous between these.
3. **Optional context**: comps the user names, who the acquirer is, what the rep sells. Use it in the synthesis sections if it's offered. Don't interrogate for it.

Then read the one reference file for the chosen audience, and `references/sources.md` for where to find each kind of data.

## Step 2 — Open the doc first

Deliver as a Claude doc through the docs connector, which is editable, commentable, and exports to Word or PDF. Follow the connector's own instructions: create the doc *before* researching, with title, byline (as-of date · user), and one `pending` block per section from the reference file. Create it with the tabs in the defaults, in that order:
- **Overview**: plain-language on-ramp (`references/overview.md`).
- **Tear sheet**: the budgeted page from the reference file.
- **Price chart**: the share price over time (`references/price-chart.md`); a private company has none.
- **Detail**: the depth that doesn't fit on one page.
- **Glossary**: every financial term the doc uses (`references/glossary-seed.md`).
- **Translated tabs**, if the defaults list a language: a copy of each tab above in that language, nested as the defaults say.

Plant a pending block on each tab at birth. Fill the Tear sheet, Price chart and Detail first. Write the Overview and Glossary last, from the finished tear sheet, so every number matches; then the translated tabs from the finished English ones.

Then open it so the person watches it fill. Research each section and fill it as one call, in reading order. Draw charts last.

**Links between tabs** (the "as of" line under the title, a "detail: …" pointer, a Glossary pointer, the language links below) are tab links: the docs connector's link to that tab (a mention of the tab), never a file name. A link to `<name>-glossary.md` in a doc opens a raw file instead of the tab.

**Language links.** When the defaults list a translated language, the first line of each tab links to the same tab in every other language, labelled `<language label>: <that tab's name>` (the names are in the defaults; back to English it's "English: Detail"), so a reader switching language stays on the same tab.

**Write every tab as Markdown**, never as plain text, and read each tab back once it's filled: a literal `**`, `*` or `##` on the page means it went in as text, so replace that block as Markdown.

If the docs connector isn't available, write one Markdown file per tab: `<name>-overview.md`, `-tear-sheet.md`, `-price.md` (with the chart's data in `<name>-prices.csv`), `-detail.md`, `-glossary.md`, and for each translated language the same names with `-<lang>` before `.md` (`<name>-tear-sheet-ja.md`). Links between tabs are then relative links to those files. If the user explicitly asked for Word, load the docx skill and produce a .docx. Never let format choice delay the research.

## Step 3 — Gather, section by section

Work through the reference file's source plan. Some lessons from real runs:

- **Fetch the page you cite.** A search snippet is not a source. Open the filing, release or quote page and take the number from it. If a fetch fails or a site is blocked, try the next source in `sources.md` rather than citing an unfetched page. A verbatim repost of a press release (a wire service, a partner's newsroom) is fine when the original won't load; cite the repost and name the original issuer.
- **The company's own filings outrank everything.** For financials, guidance, segments, management and ownership, use the earnings release (8-K exhibit 99.1), 10-K or 10-Q, and proxy (DEF 14A). Aggregators are fine for earlier years and market data, but take the latest fiscal year and latest quarter from the company's own release or 10-K. Aggregators often reclassify line items (operating income especially), and the reader will check the headline year against the filing. Which ones are reachable varies by session, so try a couple (`sources.md`) and note which you used.
- **Keep reported precision.** When a source says ">$7B run-rate" or "over 80% growth", repeat it that way. Don't compute a precise growth rate, or a change in growth rate, from two rounded floors, because it will contradict the company's own figure.
- **GAAP vs. adjusted EPS.** When GAAP EPS is depressed by amortization or impairments (common after big acquisitions), a P/E on GAAP guidance can be hundreds of times earnings and tells the reader nothing. Show it once, labelled, and lead with P/E on adjusted (non-GAAP) consensus or company guidance. Name the basis in the label, and never mix GAAP guidance with non-GAAP consensus in one comparison.
- **Check quote freshness.** Aggregator quote pages sometimes serve cached data months old. Confirm the quote's date is within a few days of today before using it.
- **Consensus is not guidance.** When the company gives no guidance (Costco, many retailers), forward figures are analyst consensus. Label them that way everywhere, and compare like with like (net sales consensus vs. net sales, not total revenue).
- **Market data has an as-of date.** Record the price, its date, and the source. Market cap and multiples are only as fresh as the price.
- **Sources will disagree**, on analyst consensus especially (counts and averages vary by provider). Report the range and the providers rather than picking one silently.
- **Long filings get truncated.** Proxies and 10-Ks often exceed one fetch. Use the fetch tool's offset to read on, or go straight to the section you need (beneficial ownership is usually late in the proxy).
- **Company-specific KPIs matter.** Many companies report a non-GAAP top line (net bookings, ARR, GMV, same-store sales). Use it where it's what management guides to, and label it as the company's non-GAAP measure next to the GAAP figure.
- **Look for the skeptic's case.** Sourcing discipline is not the same as insight. For every headline claim (growth, margins, run-rate, a valuation mark), spend at least one search on the critical view: analyst or short-seller pushback, independent valuation models, and the quality of revenue. Quality of revenue covers pass-through or resale revenue, one-offs, acquisitions inflating growth, gross-margin trend, and channel stuffing. Report what you find next to the claim, sourced. A tear sheet that only repeats the company's framing, however well cited, misses what an investor most needs.
- **Persist as you go.** After each source, append raw values to `tear-sheet-work/data.csv` (`section,metric,period,value,unit,source_url,as_of`). The file, not your memory of the conversation, is the source of truth when filling sections. This matters in long sessions where early numbers fall out of view.

### Last pass: the past week
Research takes a while, and the most recent items are the ones most likely to be missing. Before computing and writing, run a final sweep for the last 7 days:
- `<company> stock` / `<ticker> news` with this week's dates;
- analyst rating and target changes;
- the latest closing price, refreshed if your quote is more than 2 trading days old;
- any new filing (8-K) or press release.

Fold anything material into the relevant section.

## Step 4 — Compute, then check

Run `scripts/calc.py tear-sheet-work/data.csv` to compute derived metrics. It produces growth, margins, net debt, EV, EV/revenue, P/E and FCF margin, with the formula and components for each. Use its output rather than doing arithmetic in prose. It flags:
- growth or margins that can't be computed (missing prior year or denominator),
- segment shares that don't sum to ~100%,
- multiples on negative earnings (shown as "not meaningful", not a negative P/E).

If a check fails, recompute from raw components. If it still disagrees, show "n/a" with a one-line reason. A quiet arithmetic error costs a tear sheet more credibility than a gap.

## Step 5 — Write

- **Answer the question they asked.** The lead is an analytical bottom line on the user's actual question: "worth a deeper look?", "adding to my position", "meeting next Thursday". Say what the evidence points to and the main thing that would change it. It is analysis, not advice: never tell the user to buy, sell, size or time a trade.
- **Two layers: tight page, deep tab.** The tear sheet carries the conclusions and the numbers a reader decides from. The Detail tab carries the evidence behind them and anything else worth knowing. It has no word budget, but it's organized under headings, sourced the same way, and contains no padding. Typical contents:
  - KPI tables too wide for page one (regional or segment comps, with and without distortions like gas or FX; membership or subscriber series; cohort and retention figures);
  - the skeptic's evidence in full (an analyst's growth decomposition, margin history, independent valuation models);
  - the full funding history or deal list;
  - side-by-side comparisons with the closest peer;
  - longer ecosystem notes per company;
  - open questions and data gaps.

  When cutting the tear sheet to budget, move material to Detail rather than deleting it, unless it's genuinely uninformative. Each page-one section whose evidence lives in Detail ends with a short pointer ("detail: regional comps").
- **Hold the length budget.** Each audience file gives a word budget for the body, *excluding* the sources list. Count with `python scripts/wordcount.py <file>` (body before the Sources heading, ignoring table syntax and URLs) before finishing, and if you're over, apply the file's cut order, moving what you cut to Detail. A tear sheet that runs to five pages has stopped being one. Depth belongs in the Detail tab, not in longer page-one paragraphs.

- **Lead with the point.** The doc's first sentence and each section's first sentence state the finding with its number, not "This section covers…".
- **Rewrite for the audience.** A filing's business description is an input, never output. Investment research wants what's *changing* and who else it affects; IB wants pitchbook prose; corp dev wants an acquirer's lens; sales wants plain language.
- **Tie relationships to tickers.** When a customer, supplier, partner or competitor is listed, give its ticker and say in a few words what the relationship implies for it. A name alone tells an investor nothing.
- **Label provenance honestly.** Every figure links to its source near the table. Anything from general knowledge (founding year, franchise names, a well-known history) is marked as background. Third-party projections are marked as such, not as guidance. Synthesis sections (strategic fit, conversation starters, risks) are your analysis and should read as such.
- **Fiscal periods:** label the fiscal period plus calendar months once ("Q1 FY27 (Apr–Jun 2026)"). Don't mix FY and LTM in one column.
- **Shapes get pictures.** In a Claude doc, five or more values of one measure over time (e.g. revenue by year plus guidance) become a chart, not a table row. In the Markdown or Word fallback, use a compact table. Use a table for items × attributes of different kinds.
- **Management and ownership are allowed here** (unlike the S&P skill), because the proxy and IR pages are primary sources. Cite them. If the 5% holder table can't be retrieved, write that as an open question with the link instead of filling it from memory.
- **Not advice.** End with a one-line note that this is a research summary, not investment advice, alongside the sources list.

- **Say what it is, not what it isn't.** Open with what the company does. Don't open a tab, a section or a glossary line by contrasting it with another kind of security; the reader knows what they asked about.

### Overview, Glossary and translations
After the tear sheet, Price chart and Detail are final, read `references/overview.md` and `references/glossary-seed.md`.
- **Overview:** five plain questions, each answered with the real number, a "how to read it" note, and where to find it on the tear sheet.
- **Glossary:** every bolded or financial term used on the Overview and Tear sheet, each with a "Here" line showing this company's actual figure.
- **Translated tabs:** for each language in the defaults, read its `references/translate-<lang>.md` and write a copy of every English tab in that language, from the finished English tab, with the same numbers unconverted and English terms in parentheses.

Before finishing, check that every number in the overviews and translated tabs appears identically on the tear sheet, and that no date, qualifier or attribution was lost in simplifying or translating (see "Simplify without drifting" in `references/overview.md`). When the user later comments "add X to the glossary" on any word, add the entry in the same format, and to each translated glossary.

## Step 6 — Hand off

One short line in chat with the doc link and what the person can do (edit, comment, share). Mention any gap that changes what they got, such as no ownership table or stale price. Don't recap the doc. Mention that the overview tabs are the place to start for a newcomer, and that Detail holds the supporting evidence.

## Private companies

Use `references/private-company.md`. Public data is thin. Lean on press releases (funding rounds, acquisitions), the company's own site, regulatory filings where they exist (Form D for raises, state filings, S-1 if one was filed), and reputable news. Partner announcements and the public partners' own filings and earnings calls are a rich, underused source on what a startup buys.

- Mark "Private company" in the header. Skip market data, multiples and consensus.
- Any valuation is "last reported round valuation, [date], per [source]", never an estimate of your own.
- Replace the template's multi-year financial table with what the company discloses: run-rate revenue, growth claims, customer counts, funding history. Date and source each one, and note that they're unaudited company claims.
- `calc.py` adds little here. Skip it unless you have figures to combine.
- Headcount estimates disagree, so give the range and the sources.

## Attribution

Audience templates are adapted from S&P Global's `tear-sheet` skill (Apache License 2.0; see `LICENSE`). Data sourcing, delivery and validation were rewritten for public sources.
