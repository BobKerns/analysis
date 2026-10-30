---
name: fund-tear-sheet
description: Build an investor tear sheet on a named mutual fund, ETF, money market fund, bond fund, target-date fund or thematic fund from free public sources such as issuer fact sheets, prospectuses, SEC N-PORT/N-MFP/N-CSR filings and quote pages. Use whenever someone asks about a fund or ETF ticker (QQQ, VMFXX, FDCAX, VTWNX, ARKK...) — what it holds, what it costs, how it has done, how risky it is, whether it's worth its fee, or wants a fund fact sheet, fund overview or comparison with alternatives — even if they don't say "tear sheet". For individual companies use the company tear-sheet skill instead. Delivered as an editable Claude doc by default.
---

# Fund Tear Sheet

A fund is a bundle of other securities plus a set of rules and costs. The questions differ from a company's: what's in the bundle, what does it cost, how well does it do what it promises, what risks come with it, and what are the cheaper or simpler alternatives. Build the answer from public sources, and show where every number came from and as of when — fund data goes stale fast (yields daily, holdings monthly or quarterly).

<!-- defaults:start (rendered from defaults/fund-tear-sheet.md.j2; edit the template, then run: analysis research-plugin render --neutral) -->
## Defaults for this installation
- **Tabs:** Overview, Tear sheet, Price chart, Detail, Glossary.
- **Translated tabs:** none.
- **Tax state: none set.** Where state tax matters (money market and bond funds, Treasury income), say that the treatment depends on the reader's state, and show no state rate bands.
- **Account type changes every tax point.** Every tax observation (state exemption, tax-equivalent yield, capital-gains distributions, qualified dividends, tax efficiency of ETFs vs mutual funds) applies only in a taxable account. Say so next to it: inside an IRA, 401(k) or other tax-deferred account these don't apply, because withdrawals from a traditional IRA are taxed as ordinary income, federal and state, whatever the fund earned; and qualified Roth withdrawals are tax-free. Don't assume which kind of account the user holds the fund in.
- If the user asks for no overview, no price chart or no glossary on a particular sheet, omit that tab.
<!-- defaults:end -->

## Step 1 — Identify the fund and its type
Resolve: full name, ticker(s) and share classes, issuer, structure (ETF, open-end mutual fund, money market fund, closed-end fund, unit trust), inception, and **type**. Read `references/fund-types.md` and use the section for that type — it says which questions matter, which sections the tear sheet has and what the skeptic's pass looks for:

- **Index equity** (QQQ, VUG, VOO) · **Active equity** (FDCAX) · **Money market** (VMFXX) · **Bond** (BND) · **Target-date / allocation** (VTWNX) · **Thematic or concentrated active** (ARKK) · **Leveraged / inverse** (TQQQ) — a fund can borrow from two sections (an active thematic ETF uses both).

Note share classes: the same portfolio often exists in several classes with different fees (Investor vs Admiral; retail vs institutional) or as an ETF and mutual fund (QQQ vs QQQM). The class the user named is the subject; mention cheaper classes of the same portfolio.

If the person names several funds, build one doc per fund unless they ask for a comparison — then one doc with a side-by-side tab.

## Step 2 — Open the doc first
Deliver as a Claude doc through the docs connector; follow the connector's own instructions. Create it before researching, with the tabs above, in that order (translated tabs nested as the defaults say), each with pending blocks for its sections; open it so the person watches it fill. Fill Tear sheet, Price chart and Detail first; write the Overview and Glossary last, from the finished tear sheet, then the translated tabs from the finished English ones.

**Links between tabs** (the "as of" line under the title, a "see Detail" note, a Glossary pointer, the language links below) are tab links: the docs connector's link to that tab (a mention of the tab), never a file name. A link to `<ticker>-glossary.md` in a doc opens a raw file instead of the tab.

**Language links.** When the defaults list a translated language, the first line of each tab links to the same tab in every other language, labelled `<language label>: <that tab's name>` (the names are in the defaults; back to English it's "English: Detail"), so a reader switching language stays on the same tab.

**Write every tab as Markdown**, never as plain text, and read each tab back once it's filled: a literal `**`, `*` or `##` on the page means it went in as text, so replace that block as Markdown.

If the docs connector isn't available, write Markdown files in the output folder, one per tab, with working files in a `fund-work/` subfolder beside them: `<ticker>-overview.md`, `-tear-sheet.md`, `-price.md` (with the chart's data in `<ticker>-prices.csv`), `-detail.md`, `-glossary.md`, and for each translated language the same names with `-<lang>` before `.md` (`<ticker>-tear-sheet-ja.md`). Links between tabs are then relative links to those files. If the user asked for Word, use the docx skill.

## Step 3 — Gather
Use `references/sources.md`. Lessons that apply to every fund:

- **The issuer's own documents outrank aggregators.** Fact sheet and fund page for current figures; summary prospectus for fees, objective and principal risks; annual/semiannual report (N-CSR) for audited figures, turnover and manager discussion; SEC N-PORT for full holdings; N-MFP for money market portfolios.
- **Every figure has an as-of date, and they differ.** Price is today's; yield may be 7-day or 30-day; holdings may be a month or a quarter old; returns are usually as of the last month- or quarter-end; expense ratio is from the latest prospectus. Put the date next to each, never one date for the whole sheet.
- **Returns: total return, annualized, with the period end.** Use the fund's own standardized returns (1/5/10 years or since inception) and its benchmark's for the same dates. Distinguish NAV return from market-price return for ETFs. Never compare periods ending on different dates.
- **Name the yield.** SEC yield (7-day for money funds, 30-day for bond funds) is standardized; distribution yield and "dividend yield" on quote sites are not. Say which one each figure is.
- **Expense ratio: gross vs net.** If there's a fee waiver, show both and when the waiver ends.
- **When sources disagree** (assets, top-10 share, year-to-date return, expense ratio), show the range and who says what, and recompute from primary inputs where possible (shares outstanding × NAV for assets). Name the return type for every return: NAV or market price, price-only or total return, and the as-of date. Year-to-date figures of different types can differ by 10 points.
- **Look for the skeptic's case**, per the type's section — closet indexing, concentration, fee drag, tracking error, style drift, capital-gains distributions, liquidity, yield vs Treasury bills. A tear sheet that only repeats the issuer's marketing misses what an investor most needs. If you find no independent view, say so.
- **Last pass: the past week.** Before writing, search for news on the fund or issuer in the last 7 days (manager changes, fee changes, closures, mergers, large outflows, index reconstitution) and refresh price/yield if older than 2 trading days.
- **Persist as you go** to `fund-work/data.csv` (`section,metric,period,value,unit,source_url,as_of`), and holdings to `fund-work/holdings.csv` (`name,ticker,weight_pct,sector,as_of`) when you have them.

### Price chart
Every fund except a money market fund gets a **Price chart** tab; read `references/price-chart.md`. Gather month-end closing prices (market price for an ETF, NAV for a mutual fund) for the last 10 years, or since inception if shorter, into `fund-work/prices.csv`. A money market fund's price is held at $1.00, so it has no Price chart tab; say so in one line on the tear sheet's Snapshot instead.

### Allocations
Every fund gets an **Allocations** section right after the Snapshot, in the retirement simulator's own asset classes (stock_us, stock_intl, stock, bond, cd, cash, real_estate, other) plus unclassified. These are exported as CSV and JSON, with regions and sectors when available, and as ready-to-load rows in the simulator's allocations.csv format. Read `references/allocation.md` for the classification rules. Gather the inputs (issuer breakdown, or look-through for funds of funds, or holdings with domicile) and run `scripts/allocation.py`.

## Step 4 — Compute, then check
Run `scripts/allocation.py fund-work/data.csv --ticker <TICKER> --out-dir <output folder> [--holdings …] [--money-market]`, then `scripts/fund_calc.py fund-work/data.csv [--holdings fund-work/holdings.csv]`. It computes excess return vs benchmark, the fee drag of the expense ratio on $10,000 over 10 and 20 years, the cost difference against a cheaper alternative, top-10 and top-N concentration and an effective number of holdings, ETF premium/discount, and tax-equivalent money-market yield bands. Use its output rather than doing arithmetic in prose; heed its warnings (mismatched return periods, gross/net fee confusion).

## Step 5 — Write
- **Answer the question they asked** in the lead — analytically, never advice. Never tell the user to buy, sell, switch, or how much to hold. Comparisons with alternatives are facts (cost, holdings overlap, structure), not recommendations.
- **Two layers: tight page, deep tab.** Tear sheet ~800–1,100 words (target-date and thematic funds up to 1,250, since they have more required sections). A short data-gaps note belongs in Detail, not the tear-sheet body (count with `scripts/wordcount.py`, body before Sources); Detail holds full holdings lists, longer performance tables, the skeptic's evidence in full, prospectus risk list, distribution history, open questions. Cut to Detail, don't delete. Detail and Glossary have no word limit, but no padding.
- **Label provenance**: every figure linked to its source near the table; anything from general knowledge marked background; third-party ratings (Morningstar stars, analyst ratings) attributed and dated.
- **Same multiple, same basis**: when comparing with alternatives, use the same date and the same return type.
- **Not advice**: end with one line saying this is a research summary, not investment advice.

- **Say what it is, not what it isn't.** Open with what the fund holds and does. Don't open a tab, a section or a glossary line by contrasting it with another kind of security; the reader knows what they asked about.

### Overview, Glossary and translations
After the tear sheet, Price chart and Detail are final, read `references/overview.md` and `references/glossary-seed.md`. Overview: five plain questions answered with the real numbers, a "how to read it" note and where to find it; Glossary: every term a newcomer wouldn't know, each with a "Here" line using this fund's actual figure. Check every number and date matches the tear sheet, and that no qualifier, date or attribution was lost (overview.md, "Simplify without drifting").

Then, for each translated language in the defaults, read its `references/translate-<lang>.md` and write a copy of every English tab in that language, from the finished English tab: numbers and dates unconverted, English terms in parentheses. Run the same checks on each.

## Step 6 — Hand off
One short line in chat with the doc link; mention that the overview tabs are the place to start for a newcomer and Detail holds the evidence; name any gap that changes what they got (stale holdings, no independent view). Don't recap.
