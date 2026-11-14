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
shared default here) — see "Per-scenario overrides" below.

## Read from the code repository

The loader reads this folder itself, beneath a data repo's own `aux/`, so a
data repo needs none of these files: it adds a file only to change one, and a
same-named file there replaces this one whole. Don't copy them; a copy hides
the next update to the template. `sim check -v` names any data-repo file that
is still an exact copy.

The exception is `filing_status.csv`, shipped here with only its header: the
household's filing status is the data repo's to give, in its own
`aux/filing_status.csv`, one row per date (`2026-01-31,MFJ`). A scenario that
computes tax won't load without one.

## How a row is used

Every aux table has an `effective_date` column. The simulator picks the row whose
`effective_date` is the **latest one on or before the simulated month**. So a row
dated `2026-01-31` applies to all months from January 2026 until the next dated
row takes over.

When tax law changes (typically once a year), you add a new row with a new
effective date; you do **not** edit the old row. Past months keep using the old
values; future months pick up the new ones.

## Indexed for inflation

The law raises the brackets, the standard deduction and the long-term gain
bands every year with inflation. So a row of `federal_brackets.csv`,
`state_brackets__<state>.csv` or `ltcg_brackets.csv` can name an inflation
series in `floor_series`, and a row of `standard_deduction.csv` or
`state_standard_deduction__<state>.csv` one in `amount_series`. For each
later tax year the figure grows by that series from the row's
`effective_date` to the year's January, rounded down to a multiple of $50,
as the IRS rounds it. These files name `cpi`, so in an ensemble each
member's tax tables follow its own inflation. A blank series holds the
figure flat, and a new dated row (next year's published figures, or a law
change) starts again from its own figures. The Social Security thresholds
and the capital loss limit aren't indexed by law, so they have no such
column.

## Units (same as everywhere else in the data repo)

- **Money** is decimal **dollars**: `$31,500.00` → `31500.00`.
- **Rates** are **percents**, with the `%` sign required: `10%`, `9.3%`.
- **Factors** (`rmd_factors.csv`) are plain numbers: `26.5`.
- **Dates** are `YYYY-MM` (`2026-01`) or `YYYY-MM-DD`; either takes effect for
  the whole month (the loader pins it to month-end). `security_price` files
  under `prices/` are the exception: their `effective_date` is the exact day.

The loader converts these to integer cents and micros; nothing past it sees
dollars or percents.

## Filename pattern

- `aux/<kind>.csv` — a kind that has exactly one table
  (`federal_brackets`, `standard_deduction`, `rmd_factors`, …).
- `aux/<kind>__<series>.csv` — a kind with several named tables, separated by a
  **double underscore**. In this folder: `inflation__cpi.csv` and
  `state_brackets__<state>.csv` (state tax law is a shared fact, like the
  federal brackets beside it). `rate__*` and non-`cpi` `inflation__*` series
  are per-scenario, under `series/`; `security_price__*` is shared, under
  `prices/`.

The loader (`src/analysis/sim/loader.py`) parses the filename to figure out the
kind and series; the required columns per kind are listed in
`src/analysis/sim/registries.py` (`AUX_KIND_COLUMNS`) and [ADR-003](../../docs/architecture/ADR-003-registries-and-csv-schemas.md) §4.4.

## State deductions and credits

A state's own deductions and credits that its brackets and exclusions
don't cover go in `state_adjustments__<state>.csv`: a renter's deduction
or credit, a deduction for 529 contributions or long-term care premiums,
a public pension exclusion, personal exemptions and exemption credits,
and a deduction for federal tax paid. Each row is one line of the state
return, written to the run as `state_<name>` and shown by
`sim report tax --return state`.

| Column | Meaning |
| --- | --- |
| `effective_date` | When the rows take effect; the rows of one date are the whole set. |
| `name` | The line, in lower case (`renters_credit`). One row per line and filing status. |
| `step` | `subtraction` (off income before state AGI), `deduction` (off state AGI, after the standard or itemized deduction) or `credit` (off the tax, never below zero: each credit takes, in file order, only the tax left). |
| `base` | What it is figured from: `spending` or `income` carrying `tag`, `filers` (`amount` for each), `federal_tax` (the federal return's tax) or `federal_deduction` (its deduction). |
| `tag` | For `spending` and `income`: the tag on what counts, as `tax:rent`. Tag the rule that pays it, or the account it is paid into (`sim scheduled edit housing --tags tax:rent`). |
| `rate` | The share that counts (`50%`); blank is all of it. Not for `filers`. |
| `amount` | The cap; for `filers`, each filer's amount. Blank is no cap. |
| `per_person` | `yes`: the cap is each filer's. |
| `min_age` | Only filers who have reached this age by December 31 count. |
| `filing_status` | `MFJ`, `Single` or `MFS`; blank is every status. A row for the status wins over a blank one. |
| `income_limit` | Above this state AGI the line ends, unless `phaseout` or `phaseout_rate` says otherwise. |
| `phaseout` | It falls in a straight line to nothing over this many dollars past the limit. |
| `phaseout_rate` | Or it loses this share of each dollar over (each filer's, for `filers`). |
| `amount_series`, `income_limit_series` | Optional inflation series that index `amount` and `income_limit`, rounded down to the dollar. |

A spending or income row whose tag nothing in the scenario carries comes
out zero, and `sim check` says so. Rules figured from property value, or
from tax as a share of income (most circuit breakers), don't fit a row:
they need a household calculator (`docs/guides/tax-calculators.md`).

`__CA` holds California's 2025 personal and senior exemption credits
($153 each, phased out by $6 for each $2,500 of income over the limit) and
its renter's credit ($60, or $120 filing jointly, below an income limit).
California phases the exemption credits out by federal AGI; the model
tests state AGI. The renter's credit needs at least half a year of rent on
a California home; the model counts any rent tagged `tax:rent`.

## State retirement income exclusions

Most states leave some retirement income out of their income tax: a
pension, an IRA or 401(k) draw, sometimes interest, dividends and gains,
often only from an age and up to an amount per person, and sometimes only
below a household income. A `state_retirement_exclusion__<state>.csv`
beside the state's brackets says what its law leaves out. None ships: write
the one for your state from its revenue department's instructions for the
year. Each row is one exclusion, or one age tier of one:

| Column | Meaning |
| --- | --- |
| `effective_date` | When the rows take effect; the rows of one date are the whole set. |
| `name` | The exclusion. Rows with the same name are its age tiers: each person gets the one with the highest `min_age` they have reached. |
| `income` | What it covers, separated by `;`: `pension`, `retirement_account` (IRA and 401(k) draws), `interest`, `dividend`, `capital_gain`, `other`, or `all`. It uses them up in that order. |
| `min_age` | The age it starts at by December 31, such as `59.5`; blank for any age. |
| `amount` | The most it leaves out per person, in dollars; blank for all of it. |
| `amount_series` | Optional: an inflation series that grows `amount` each year, as above. |
| `filing_status` | Blank for every status, or the one status the row is for. |
| `income_limit` | Optional: household income above which the exclusion goes. |
| `phaseout` | Optional: with `income_limit`, it shrinks to nothing over this many dollars instead of going all at once. |
| `less_ss` | `yes` if the amount is reduced by the person's Social Security. |

Income is each person's by the owner of the account it comes out of (an
IRA's owner, a pension income account's), or else the account it goes
into; a joint account's is split between the spouses, so give a pension
income account its owner. Household income for the limit is the state's
income before the exclusions, which leaves out Social Security. Two
exclusions never leave out the same dollar.

An illustration, not any state's law: from 59½ each spouse leaves out up to
$20,000 of pension and IRA income; from 65 a further $10,000 of any income,
which goes away for a married couple above $100,000 by the time income
reaches $120,000:

```csv
effective_date,name,income,min_age,amount,filing_status,income_limit,phaseout,less_ss
2026-01,pension,pension;retirement_account,59.5,20000,,,,
2026-01,age,all,65,10000,MFJ,100000,20000,
2026-01,age,all,65,10000,Single,50000,10000,
```

## Per-scenario overrides

A scenario can replace one of these files for itself (and its descendants) by
adding `scenarios/<name>/aux/<same filename>.csv` — the loader uses that file
in place of this shared one, whole file, not merged row by row. A derived
scenario without its own override inherits its parent's, all the way up to this
shared default ([ADR-003](../../docs/architecture/ADR-003-registries-and-csv-schemas.md) §4.1d). Use this to model a different future tax
policy, or a different CPI path, on a per-scenario basis, without touching
every other scenario.

## The files in this folder

| File | What it is |
| --- | --- |
| `federal_brackets.csv` | US federal ordinary-income tax brackets. One row per (effective_date, filing_status, bracket_index). |
| `state_brackets__<state>.csv` | State ordinary-income tax brackets. Same columns as federal. Seven states ship; see "The states that ship" below. |
| `standard_deduction.csv` | Federal standard deduction by filing status. |
| `state_standard_deduction__<state>.csv` | The state's own standard deduction by filing status, for a state tax (`taxes/state.csv`), keyed by state like `state_brackets__<state>.csv`. `__CA` holds California's figures, from the same year as its brackets. Without one for the brackets' state, a state rule uses `standard_deduction.csv`'s federal figure, which for most states is far too high, and loading warns. Optional columns shrink it with income (#578): above `phaseout_start` of state AGI it falls to nothing at `phaseout_end`, or by `phaseout_rate` of each dollar over, but not below `floor`; with a rate, `phaseout_step` counts the income over in whole steps, a started one in full; with an end, `phaseout_steps` splits the span into that many equal steps; `phaseout_agi` `federal` tests federal AGI instead. `phaseout_start`, `phaseout_end` and `floor` may be indexed with `phaseout_start_series`, `phaseout_end_series` and `floor_series`, as `amount` is with `amount_series`; the start and end take the same series or none. |
| `state_social_security__<state>.csv` | Optional: how the state taxes Social Security, keyed by state like `state_brackets__<state>.csv`. Without it (California, and most states) a state rule taxes none. With it, the state starts from the federally taxable amount and leaves out `exclusion_percent` of it, for each filing status. Optional `phaseout_start` (federal AGI) starts taking the exclusion away: evenly down to nothing at `phaseout_end` (the same figure for a cliff, as in New Mexico), or by `phaseout_rate` of each dollar over (a credit in a flat-rate state such as Utah: the credit's phase-out rate divided by the tax rate). A state that follows the federal amount (Montana) writes `0%`. `phaseout_start_series`/`phaseout_end_series` index the limits like `floor_series`. Columns: `effective_date,filing_status,exclusion_percent,phaseout_start,phaseout_end,phaseout_rate`. Age tests (Colorado, Rhode Island) aren't modeled. Check the figures with the state's revenue department. |
| `state_surcharge__<state>.csv` | A state's surcharge on high taxable income, in the columns of its brackets, whose rates add to the brackets' rates. Optional, keyed by state like `state_brackets__<state>.csv`. `__CA` is California's Behavioral Health Services Tax: 1% of taxable income over $1 million, the same for every filing status and not indexed, so its `floor_series` is blank. |
| `state_local_tax__<state>.csv` | Optional: a local income tax (county, city, school district) on the state's taxable income, in the columns of its brackets: Maryland's county rate as one band, New York City's brackets, or a Portland-area tax as a 0% band below its threshold. Named for a county (`residence.csv` names the household's) or, for one table across the state, the state; paid by `taxes/local.csv` into its own `taxes_local` account, and shown as `local_tax` in `sim report tax`. Two local taxes on the same base, such as Portland's Metro and Multnomah County taxes, go in one table, their rates added. Needs a row for every filing status the state's brackets have. None ships. |
| `state_gains_tax__<state>.csv` / `state_gains_deduction__<state>.csv` | Optional: a state's tax on long-term capital gains apart from its income tax, keyed by state; see "A state's gains tax" below. `__WA` ships Washington's. |
| `state_hsa__<state>.csv` | How a state taxes a health savings account: `treatment` `taxable` or `federal`. Optional, keyed by state; without one the HSA follows the federal rules. `__CA` says `taxable`: California doesn't recognize HSAs and taxes one like a brokerage account, so a contribution isn't deducted, its dividends and interest are taxed the year they're paid, and a withdrawal is taxed on its gain over the account's cost basis. |
| `state_retirement_exclusion__<state>.csv` | Optional: the state's retirement income exclusions, keyed by state like `state_brackets__<state>.csv` and read by a state tax. None ships (California has none); see "State retirement income exclusions" below. |
| `state_adjustments__<state>.csv` | Optional: the state's own deductions and credits, keyed by state like `state_brackets__<state>.csv` and read by a state tax; each row is a named line of the return. `__CA` ships California's personal and senior exemption credits and its renter's credit, 2025's figures, and `__WA` Washington's credit for its gains tax from 2028; see "State deductions and credits" below. |
| `federal_deductions.csv` / `state_deductions.csv` | Optional, dated itemized-deduction totals (no filing-status split), added to the lines figured from spending (`itemized_limits.csv`). A tax jurisdiction uses whichever is greater, the itemized total or the standard deduction. `amount` can be `@name` (an itemizations/ rollup). They hold only what no line figures (a casualty loss, investment interest): medical bills, charity, property tax and mortgage interest go in tagged spending, where their floors and limits apply. `state_deductions.csv` falls back to `federal_deductions.csv`'s figure if the scenario has no `state_deductions.csv` at all; a state that figures no lines of its own (none but California's ships) then misses what the federal lines figure, so give it its own total, or `itemized_limits_aux` `itemized_limits` on `taxes/state.csv` to figure the federal lines for it (loading warns). |
| `itemized_limits.csv` | The limits on the itemized deductions figured from the year's spending, by filing status: `salt_cap` (state and local taxes), which falls by `salt_phaseout_rate` of AGI over `salt_phaseout_start` but not below `salt_floor`; `mortgage_limit` (the debt whose interest counts); `charity_floor` and `charity_limit` (percents of AGI); `medical_floor` (a percent of AGI). A blank cell is no limit. Ships the 2025 law's (OBBBA) from 2026: the SALT cap's 2028 and 2029 figures are the statute's 1% a year, unrounded, until the IRS publishes them. A data repo that wants only hand-typed itemized deductions puts one with just its header in its `aux/`; that turns off a state's lines too. |
| `state_itemized_limits__<state>.csv` | Optional: a state's own `itemized_limits`, keyed by state. Without one the state figures no lines from spending, only the hand-typed total. A state never deducts an income tax. `__CA` ships California's: no SALT cap, $1,000,000 of mortgage debt ($500,000 separate), charity up to 50% of AGI, medical over 7.5%. |
| `filing_status.csv` | The household's filing status from each effective date, one row per change. Empty here: put the household's in the data repo's `aux/`. A joint filer becomes Single the year after a death without a row. |
| `ss_taxation_thresholds.csv` | Provisional-income thresholds for Social Security benefit taxation (50% tier and 85% tier). These are **not** indexed for inflation — they have stayed at the 1984/1993 amounts. Leave alone unless Congress changes them. |
| `inflation__cpi.csv` | Annual inflation rate (CPI = Consumer Price Index, the standard cost-of-living measure published by the US Bureau of Labor Statistics) used to grow non-indexed amounts, and by the CPI-constant-dollar views/reports regardless of what any scenario's own CSVs reference. Kept here, rather than in `series/`, so that lookup always has a default; a scenario can still override it. |
| `rmd_factors.csv` | IRS Uniform Lifetime Table divisors used to compute Required Minimum Distributions (Pub. 590-B Table III, 2022 on), ages 72 to 130 (120 and over all 2.0). One row per (effective_date, age). |
| `rmd_start_age.csv` | First age at which an RMD is required: 73 here. SECURE 2.0 sets it by birth year (73 if born 1951–1959, 75 if born 1960 or later), but this table is keyed by date, so a household born in 1960 or later puts its own `rmd_start_age.csv` with 75 in its `aux/`. |
| `capital_loss_limit.csv` | How much of a year's net capital loss offsets ordinary income, by filing status (IRC §1211(b)): $3,000, or $1,500 married filing separately, unchanged since 1978 and not inflation-indexed. The rest carries forward to later years. Read by a tax jurisdiction only in a year with a net capital loss. The home-sale exclusion is no longer an aux table: it is the `maximum` of a home-sale chain's `excluded_gain` step. |
| `irmaa_tiers.csv` | Medicare's income-related surcharges (IRMAA): for each filing status, the MAGI floor of each tier (the tier applies above it) and its monthly Part B and Part D add-ons, from CMS's fact sheet for the year. Read by an `irmaa` rule, which charges the tier set by MAGI from two years earlier. Each row names optional growth series for its floor and each add-on (`series/inflation__<name>.csv`); blank holds the figure flat. Add the next year's rows each November, when CMS publishes them. |
| `energy_credits.csv` | Federal energy-efficiency credit/rebate rate and cap, by category (`heat_pump`, `induction_range`, `ev_charger`, ...) and effective date. **Expiring credits need an explicit zero-rate row on their expiration date** -- the lookup takes the latest `effective_date <= month`, so an omitted row means "still in effect," not "expired." OBBBA (2025) ended 25C/25D for property placed in service after 2025-12-31 and 30C after 2026-06-30; the shipped rows reflect that. Not yet wired to a call site -- see `engine._lookup_energy_credit`. |
| `ltcg_brackets.csv` | Federal long-term capital gain brackets — see "Long-term capital gain rates" below. |
| `life_table.csv` | SSA's period life table: `q_female` and `q_male`, the chance of dying before the next birthday at each age 0–119. Read only by a lifespan with `vary_distribution` `life_table`, which draws the age at death from it around the median you give. Refresh it from a newer Trustees Report with `scripts/extract_ssa_life_table.py`, keeping the old rows: each vintage is dated December of its year. |
| `life_table__<name>.csv` | Another country's life table, in the same columns, for a lifespan whose `lifespan.csv` `life_table` cell names it (blank uses `life_table.csv`), per scenario. `scripts/blend_life_tables.py` mixes two. `__jp` is Japan's 2023 table from the Human Life-Table Database, extracted with `scripts/extract_hld_life_table.py`; it ends at an open "105 and over" group, extrapolated to 108 with a death rate that keeps rising, and no one reaches 110 (the few recorded cases past it are largely record errors). Use a country's own table rather than shifting `life_table.csv` older or younger: the shift keeps the US spread, and the US tail past 95 is much fatter than Japan's. |

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
> and `prototype_data/aux/state_brackets__CA.csv` with the latest IRS and
> CA FTB figures. Use the most recent IRS Revenue Procedure for federal and FTB's
> annual indexing announcement (in "Tax News") for California. Filing statuses:
> MFJ, Single, MFS. Leave out CA's Behavioral Health Services Tax (the extra
> 1% over $1M): it's in `state_surcharge__CA.csv`, which isn't indexed. Then mirror the three files into `templates/aux/`
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
- `templates/prices/README.md` — security prices.

## Long-term capital gain rates

`ltcg_brackets.csv` has the same shape as `federal_brackets.csv`, but a band's `floor` is the
taxable income at which its rate starts, with the gain stacked on top of ordinary taxable
income: the 0% band ends where the 15% band's floor begins, and so on (the shipped figures
are the 2026 thresholds; check them against the current IRS tables). It is optional and only
affects federal tax: without it long-term gains are taxed as ordinary income. Short-term
gains (`stcg`, for a lot held a year or less) are taxed federally as ordinary income.

## The states that ship

Seven states' brackets and standard deductions ship here, each pair named for
its state: California (`__CA`), the District of Columbia (`__DC`), Idaho
(`__ID`), Kansas (`__KS`), Nebraska (`__NE`), North Carolina (`__NC`) and
Washington (`__WA`).
A state tax (`taxes/state.csv`) reads the tables of the state the household
lives in, from the scenario's `residence.csv`; it won't load without one.
`sim tax state` writes it, asking for the state by name.
`state_brackets__none.csv` and `state_standard_deduction__none.csv` are a 0%
table for a state with no income tax, which `sim tax state` and
`sim tax move` choose for such a state, so a move there needs no tables. The tables carry each state's 2026 law, checked
against its revenue department's 2026 publications in November 2026. A
state's personal exemption, where it has one, is folded into the standard
deduction. None of the others taxes Social Security, so only Washington's
needs a `state_social_security` table.

- **District of Columbia:** brackets the same for every filing status, not
  indexed. The standard deduction follows the federal one ($16,100, or
  $32,200 joint, for 2026), so it grows with `cpi`. The extra deduction at
  65 or blind isn't modeled.
- **Idaho:** 5.3% above a zero band, which is indexed. Idaho hasn't
  published its 2026 band, so the 2026 rows carry the 2025 one ($4,811, or
  $9,622 joint), about $7 of tax high, and grow it with `cpi` from there. The standard
  deduction follows the federal one. The deduction for some public pensions
  from 65 and the grocery credit aren't modeled.
- **Kansas:** 5.2%, then 5.58% over $23,000 ($46,000 joint), not indexed.
  The deduction is the standard deduction plus the personal exemption:
  $3,605 + $9,160 single, $8,240 + $18,320 joint, $4,120 + $9,160 separate.
  A household that itemizes gets the greater of that and its itemized
  deductions, so it loses the exemption Kansas allows on top: until
  exemptions are modeled (#488), add the exemption to a Kansas
  `state_deductions.csv` row.
  Kansas's own public pensions (KPERS, federal civil service, military) are
  exempt, and from 65 the standard deduction is larger; neither is modeled.
  A 2025 law cuts the rates when revenue allows; it didn't for 2026.
- **Nebraska:** 2.46%, 3.51%, then 4.55% for 2026 and 3.99% from 2027, the
  bands indexed. Standard deduction $8,850, or $17,700 joint, for 2026; the
  2027 rows use the $9,100 and $18,200 of Nebraska's draft 2027 estimated
  tax form, and estimate the 2027 floors by growing 2026's by the same
  ratio, to the nearest $10, until Nebraska publishes them. The personal
  exemption credit isn't modeled.
- **Washington:** two taxes. Its capital gains tax (RCW 82.87) is
  `state_gains_tax__WA.csv` and `state_gains_deduction__WA.csv`: 7% of the
  year's net long-term gain over the deduction, and from 2025 9.9% of the
  part over $1 million, which isn't indexed. The deduction was $250,000 for
  2022, $262,000 for 2023, $270,000 for 2024 and $278,000 for 2025, indexed
  each year; Washington hasn't published 2026's, so the 2026 rows carry
  2025's and grow it with `cpi`. A couple shares one deduction and one
  $1 million, so the `MFS` rows have half of each. A home's gain is exempt;
  see "A state's gains tax" below. Its income tax (ESSB 6346) starts in
  2028: `state_brackets__WA.csv` is 0% until then and 9.9% from 2028, over a
  $1 million standard deduction (`state_standard_deduction__WA.csv`, held
  flat: the law indexes it every other year from 2030). It taxes Social
  Security as the federal return does (`state_social_security__WA.csv`,
  0% excluded), and `state_adjustments__WA.csv` credits the gains tax
  against it. If a court strikes it down, a dated 0% row of
  `state_brackets__WA.csv` in the data repo ends it. The charitable and
  family-owned small business deductions aren't modeled.
- **North Carolina:** a flat 3.99% for 2026, 3.49% for 2027-2029, 3.24% for
  2030-2032 and 2.99% from 2033 (Session Law 2026-41). Standard deduction
  $12,750, or $25,500 joint, not indexed. The exclusion for government
  pensions vested before August 1989 (Bailey) isn't modeled.

## A state's capital gains

A state tax rule taxes gains as ordinary income unless the state's own tables, named for
the state like its brackets, say otherwise. None ship; check the state's revenue department
for its current rule before writing one.

- `state_ltcg_exclusion__<state>.csv`: `effective_date`, `exclusion`, `min_exclusion`,
  `max_exclusion`. The state leaves `exclusion` (a percent) of the year's net long-term
  gain out of its income, at least `min_exclusion` and at most `max_exclusion` dollars
  (blank for no minimum or no cap), never more than the gain: `50%` for Arkansas, `100%`
  for Missouri, `0%` with a `min_exclusion` for a flat amount.
- `state_ltcg_brackets__<state>.csv`: the state's own long-term gain rates, in the shape of
  `ltcg_brackets.csv`, stacked on top of the state's taxable income: Montana's lower rates,
  or Hawaii's cap (its brackets, with each rate over 7.25% written as 7.25%).
- `state_stcg_brackets__<state>.csv`: the same for net short-term gain, which is stacked
  just below the long-term gain (Massachusetts' 8.5%). A surtax on high income, such as
  Massachusetts' 4% over about $1 million, is a top row in each of the state's tables.

A move to another state (`sim tax move`, a row of `residence.csv`) takes these with it: the
new state's tables, or none if it has none.

## A state's gains tax

A tax on long-term gains apart from the income tax, as Washington's is, takes two tables
named for the state: `state_gains_tax__<state>.csv` in the columns of `state_brackets`, its
rates on the gain after its deduction, and `state_gains_deduction__<state>.csv` in the
columns of `state_standard_deduction`. Its base is the year's net long-term gain as Schedule
D line 15 has it (long-term gains less long-term losses and the loss carried in, which a
short-term loss doesn't offset), less a home's gain (from a `real_estate` account). The
tax is added to the state's tax after its credits, and the state's income taxes that base
in place of its net long-term gain. A `state_adjustments` credit with base `gains_tax`
credits it against the income tax.
