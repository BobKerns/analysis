# `aux/` — auxiliary lookup tables

These CSVs hold the slowly-changing tax-law and demographic facts the simulator
looks up by date: tax brackets, standard deductions, RMD factors and start age,
Social Security thresholds, filing statuses, and the CPI inflation series. They
live outside any one scenario so multiple scenarios share the same tax law by
default (a scenario testing a different assumption can override one file — see
"Per-scenario overrides" below).

Security prices live in `prices/` next door instead (shared, like this folder);
see `prices/README.md`. Growth-rate and inflation series are different: they are
per-scenario projections, so each scenario has its own `series/` folder (no
shared default here) — see "Per-scenario overrides" below. The prototype's root
scenario, `base`, builds on the template scenarios, which ship a default for
each inflation series and fund asset class that every scenario derived from it
inherits (CPI stays here, in `aux/inflation__cpi.csv`); see "Template
scenarios" in `docs/guides/historical-calibration.md`.

## How a row is used

Every aux table has an `effective_date` column. The simulator picks the row whose
`effective_date` is the **latest one on or before the simulated month**. So a row
dated `2026-01-31` applies to all months from January 2026 until the next dated
row takes over.

When tax law changes (typically once a year), you add a new row with a new
effective date; you do **not** edit the old row. Past months keep using the old
values; future months pick up the new ones.

## Units (same as everywhere else in the data repo)

- **Money** is decimal **dollars**: `$31,500.00` → `31500.00`.
- **Rates** are **percents**, with the `%` sign required: `10%`, `9.3%`.
- **Factors** (`rmd_factors.csv`) are plain numbers: `26.5`.
- **Dates** are `YYYY-MM-DD`, conventionally month-end (`2026-01-31`).

The loader converts these to integer cents and micros; nothing past it sees
dollars or percents.

## Filename pattern

- `aux/<kind>.csv` — a kind that has exactly one table
  (`federal_brackets`, `standard_deduction`, `rmd_factors`, …).
- `aux/<kind>__<series>.csv` — a kind with several named tables. In this
  folder: `inflation__cpi.csv` and `state_brackets__<state>.csv` (state tax
  law is a shared fact, like the federal brackets beside it). `rate__*` is
  per-scenario, under `series/`.

The loader (`src/analysis/sim/loader.py`) parses the filename to figure out the
kind and series; the required columns per kind are listed in
`src/analysis/sim/registries.py` (`AUX_KIND_COLUMNS`) and [ADR-003](../../docs/architecture/ADR-003-registries-and-csv-schemas.md) §4.4.

## Per-scenario overrides

A scenario can replace one of these files for itself (and its descendants) by
adding `scenarios/<name>/aux/<same filename>.csv` — the loader uses that file
in place of this shared one, whole file, not merged row by row. A derived
scenario without its own override inherits its parent's, all the way up to this
shared default ([ADR-003](../../docs/architecture/ADR-003-registries-and-csv-schemas.md) §4.1d).

## The files in this folder

| File | What it is |
| --- | --- |
| `federal_brackets.csv` | US federal ordinary-income tax brackets. One row per (effective_date, filing_status, bracket_index). |
| `state_brackets__<state>.csv` | State ordinary-income tax brackets. Same columns as federal. |
| `standard_deduction.csv` | Federal standard deduction by filing status. |
| `state_standard_deduction__<state>.csv` | The state's own standard deduction by filing status, for a state `tax_accumulator` rule, keyed by state like `state_brackets__<state>.csv`. `__CA` holds California's figures, from the same year as its brackets. Without one for the brackets' state, a state rule uses `standard_deduction.csv`'s federal figure, which for most states is far too high, and loading warns. |
| `federal_deductions.csv` / `state_deductions.csv` | Optional, dated itemized-deduction totals (no filing-status split). If present, the `tax_accumulator` rule uses whichever is greater, this or the standard deduction. `amount` can be `@name` (an itemizations/ rollup); a big one-off medical year is the motivating case. `state_deductions.csv` falls back to `federal_deductions.csv`'s figure if the scenario has no `state_deductions.csv` at all -- most states never need their own file. |
| `filing_status.csv` | The household's filing status from each effective date, one row per change (here MFJ). The code repository's `templates/aux/` ships it empty. |
| `ss_taxation_thresholds.csv` | Provisional-income thresholds for Social Security benefit taxation (50% tier and 85% tier). These are **not** indexed for inflation — they have stayed at the 1984/1993 amounts. Leave alone unless Congress changes them. |
| `inflation__cpi.csv` | Annual inflation rate (CPI = Consumer Price Index, the standard cost-of-living measure published by the US Bureau of Labor Statistics) used to grow non-indexed amounts, and by the CPI-constant-dollar views regardless of what any scenario's own CSVs reference. Kept here, rather than in `series/`, so that lookup always has a default. |
| `rmd_factors.csv` | IRS Uniform Lifetime Table divisors used to compute Required Minimum Distributions. One row per (effective_date, age). |
| `rmd_start_age.csv` | First age at which an RMD is required (currently 73; rises to 75 in 2033 under SECURE 2.0). |
| `ltcg_brackets.csv` | Federal long-term capital gain brackets — see "Long-term capital gain rates" below. |
| `life_table.csv` | SSA's period life table: `q_female` and `q_male`, the chance of dying before the next birthday at each age 0–119. Read only by a lifespan with `vary_distribution` `life_table`, which draws the age at death from it around the median you give. Refresh it from a newer Trustees Report with `scripts/extract_ssa_life_table.py`, keeping the old rows: each vintage is dated December of its year. |
| `capital_loss_limit.csv` | How much of a year's net capital loss offsets ordinary income, by filing status (IRC §1211(b)): $3,000, or $1,500 married filing separately, not inflation-indexed. The rest carries forward to later years. |
| `irmaa_tiers.csv` | Medicare's income-related surcharges (IRMAA): for each filing status, the MAGI floor of each tier (the tier applies above it) and its monthly Part B and Part D add-ons, from CMS's fact sheet for the year. Read by an `irmaa` rule, which charges the tier set by MAGI from two years earlier. Each row names optional growth series for its floor and each add-on (`series/inflation__<name>.csv`); blank holds the figure flat. Add the next year's rows each November, when CMS publishes them. |

### Filing status codes

We use the IRS codes, restricted to three for this project:

- `MFJ` — Married Filing Jointly (also covers Qualifying Surviving Spouse).
- `Single` — single filer.
- `MFS` — Married Filing Separately.

Head of Household (`HoH`) is **not** modeled. If you ever need it, add rows for
`HoH` to `filing_status.csv` and to every bracket/deduction table.

### What is an RMD?

A **Required Minimum Distribution** is the amount the IRS forces you to withdraw
each year from a pre-tax retirement account (Traditional IRA, Traditional 401(k),
etc.) once you reach the RMD start age. The amount is computed as

> `RMD = prior_year_end_balance / factor`

where `factor` comes from `rmd_factors.csv` (the IRS Uniform Lifetime Table,
keyed by your age that year), and the start age comes from `rmd_start_age.csv`.

Background reading:

- IRS, [Retirement plan and IRA required minimum distributions FAQs](https://www.irs.gov/retirement-plans/retirement-plan-and-ira-required-minimum-distributions-faqs)
- IRS Publication 590-B, Appendix B (Uniform Lifetime Table)
- SECURE 2.0 Act of 2022 — moved start age from 72 → 73 (2023) → 75 (2033).

Roth IRAs do not have RMDs for the original owner. Roth 401(k) RMDs were
eliminated starting 2024 by SECURE 2.0.

### What is a Roth IRA / Roth 401(k)?

A **Roth** account is a retirement account funded with money you've **already
paid income tax on**. In exchange, all future growth and all qualified
withdrawals come out **tax-free**. This is the mirror image of a Traditional
IRA / Traditional 401(k), where you get a tax deduction now but pay ordinary
income tax on every dollar you withdraw later.

Two practical consequences for retirement modeling:

- Roth balances are not subject to RMDs during the original owner's lifetime,
  so they can keep compounding tax-free indefinitely.
- A withdrawal from a Roth account does not add to that year's taxable income,
  which means it also doesn't push you into a higher bracket or make more of
  your Social Security taxable.

Background: IRS, [Roth IRAs](https://www.irs.gov/retirement-plans/roth-iras).

#### When a Roth conversion can help

A **Roth conversion** moves money from a Traditional IRA / 401(k) into a Roth
IRA. The converted amount is added to that year's ordinary income and taxed at
your marginal rate; after that it grows and is withdrawn tax-free, with no
future RMD on the original owner.

The classic "sweet spot" is the gap between retiring (salary stops) and the
year Social Security plus RMDs ramp taxable income back up. In that window
ordinary income can be quite low, so a conversion is taxed in low brackets
instead of the higher brackets it would otherwise hit later.

Worked example, very rough, using the 2026-effective figures in
`federal_brackets.csv` and `standard_deduction.csv`, MFJ:

- Retire in 2026; defer Social Security until Full Retirement Age in 2027.
  No salary, modest brokerage interest/dividends, say $20,000 of ordinary
  income.
- 2026 MFJ standard deduction is $31,500, so taxable income before any
  conversion is already $0.
- The 12% bracket runs up to $96,950 of taxable income. Converting roughly
  `$96,950 + $31,500 - $20,000 = $108,450` from Traditional IRA to Roth fills
  the 10% and 12% brackets exactly, with federal tax of about $11,000 on the
  conversion. (California taxes the same dollars at its own rates; see
  `state_brackets__CA.csv`, right here in `aux/`.)
- Once Social Security starts and RMDs begin at age 73, the same dollars
  withdrawn from the Traditional IRA would land in the 22% bracket or higher
  *and* would push more Social Security into the 85%-taxable tier
  (`ss_taxation_thresholds.csv`). Paying 12% now to avoid 22%+ later is the
  arbitrage.

Things the simulator should help you check before actually doing this:

- Does the conversion push you over an **IRMAA** Medicare-premium cliff two
  years later? (Medicare looks back at MAGI from two years prior; an `irmaa`
  rule and `irmaa_tiers.csv` model it.)
- Does it cost you ACA premium subsidies in any pre-Medicare year?
- Is there enough cash *outside* the IRA to pay the conversion tax? Paying
  the tax from the IRA itself wipes out most of the benefit.

This is illustrative, not advice — the right amount each year depends on your
full tax picture and is exactly the kind of thing the scenario engine is for.

## Updating the tax tables (the easy way)

Once a year — after the IRS publishes the next year's inflation adjustments
(usually a Revenue Procedure released in October or November) and after
California's Franchise Tax Board (FTB — the state's income-tax agency, the
state-level equivalent of the IRS) publishes its indexed amounts (usually
October) — these tables need a new row per filing status.

The cleanest way is to ask the AI assistant. A prompt that works:

> Update `prototype_data/aux/federal_brackets.csv`,
> `prototype_data/aux/standard_deduction.csv`,
> `prototype_data/aux/state_brackets__CA.csv`, and
> `prototype_data/aux/filing_status.csv` with the latest IRS and CA FTB
> figures. Use the most recent IRS Revenue Procedure for federal and FTB's
> annual indexing announcement (in "Tax News") for California. Filing statuses:
> MFJ, Single, MFS. Exclude CA's Mental Health / Behavioral Health Services Tax
> (the extra 1% over $1M). Then mirror the four files into `templates/aux/`
> and run `uv run pytest`.

Authoritative sources the AI should pull from:

- **Federal:** IRS Revenue Procedure for the relevant tax year
  (e.g. Rev. Proc. 2024-40 for TY2025, Rev. Proc. 2025-32 for TY2026). Search
  irs.gov for "Rev. Proc. 20XX-NN inflation adjustments".
- **California:** Franchise Tax Board (FTB) Tax News "Indexing" article
  (e.g. "2025 Indexing" in the October 2025 edition at
  <https://www.ftb.ca.gov/about-ftb/newsroom/tax-news/>).
  The full rate schedules also appear in the annual 540 booklet
  (`https://www.ftb.ca.gov/forms/<year>/<year>-540-booklet.html`) once published
  in late December.
- **RMD factors / start age:** IRS Publication 590-B and the most recent
  SECURE Act amendments.

## Updating by hand

1. Append new rows with the new `effective_date`. Do not edit existing rows
   (past months still need the old values).
2. Re-run `uv run pytest` from the repo root — there are loader tests that will
   catch missing columns, bad date formats, and unknown filing statuses.
3. If you added a brand-new file kind (not just a new series), you also have to
   register it in `src/analysis/sim/registries.py` (`AUX_KIND_COLUMNS`) and
   teach the loader.

## See also

- [`docs/architecture/ADR-003-registries-and-csv-schemas.md`](../../docs/architecture/ADR-003-registries-and-csv-schemas.md) — authoritative
  column lists and the kind/series naming rules, including the per-scenario
  override mechanism (§4.1d).
- [`docs/architecture/ADR-002-storage-schema.md`](../../docs/architecture/ADR-002-storage-schema.md) — how aux rows land in SQLite.
- `templates/README.md` — the data repo as a whole.
- `prices/README.md` — security prices.

## Long-term capital gain rates

`ltcg_brackets.csv` has the same shape as `federal_brackets.csv`, but a band's `floor` is the
taxable income at which its rate starts, with the gain stacked on top of ordinary taxable
income: the 0% band ends where the 15% band's floor begins, and so on (the shipped figures
are the 2026 thresholds; check them against the current IRS tables). It is optional and only
affects federal tax: without it long-term gains are taxed as ordinary income. Short-term
gains (`stcg`, for a lot held a year or less) are always taxed as ordinary income.
