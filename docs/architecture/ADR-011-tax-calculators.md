# ADR-011: Tax calculators

- **Status:** Accepted
- **Date:** 2026-11-10
- **Supersedes:** —
- **Amended:** see [Amendments](#amendments) at the end.
- **Related:** [ADR-003](ADR-003-registries-and-csv-schemas.md) §2.6 (what the built-in calculators compute), §4.0 (the data repo's `taxes/` code), §4.15 (`taxes/<jurisdiction>.csv`), §4.16 (`residence.csv`); [ADR-002](ADR-002-storage-schema.md) (`tax_jurisdiction`, `tax_source`, `tax_year_summary`); [ADR-004](ADR-004-reproducibility-and-activation.md) §2 (tax code is data); [ADR-010](ADR-010-responsive-strategies.md) §4 (the import and snapshot this shares); issues [#506](https://github.com/BobKerns/analysis/issues/506) (the design, in phases), [#507](https://github.com/BobKerns/analysis/issues/507) (the API and jurisdiction files), [#508](https://github.com/BobKerns/analysis/issues/508) (a state from federal AGI, the local tax), [#509](https://github.com/BobKerns/analysis/issues/509) (residence), [#510](https://github.com/BobKerns/analysis/issues/510) (household calculators), [#511](https://github.com/BobKerns/analysis/issues/511) (state deductions that follow spending).

## Context

Income tax rules differ by state, change often, and depend on details a fixed model can't anticipate: a renter deduction, a public pension exclusion, a credit for long-term care premiums. A registry of rule kinds can't hold them all. As with responsive strategies (ADR-010), the answer is code: a few calculators built in, and a household's own beside its data. Tax code must keep the same guarantees as the rest of a run: reproducible ([ADR-004](ADR-004-reproducibility-and-activation.md)), checked when a scenario loads, and unable to touch the ledger.

## Decision

### 1. A calculator is a class

A calculator is a subclass of `tax_api.TaxCalculator` marked with `@register`. It names its `kind`, the `jurisdiction` it computes (`federal`, `state` or `local`), the jurisdictions whose returns it builds on (`upstream`: `federal` for a state that starts from federal AGI), a frozen dataclass `Params`, and `prepare(year)`. Its docstring is what `sim tax kinds` shows.

A scenario's `taxes/<jurisdiction>.csv` (ADR-003 §4.15) names the kind that computes it, with `params_json`. A household's kind computes one of the three jurisdictions; a new jurisdiction (another state's non-resident return) is not supported, since it would change the settlement order, `ctx.tax`'s scopes and the reports.

### 2. What a calculator sees: `TaxYear`

For each tax year a jurisdiction settles or estimates, the engine builds a `TaxYear` and asks the calculator to `prepare` it once. The prepared year answers `compute(extra)` as often as asked, with more ordinary income or long-term gain on top (`Extra`, which also says how much of the gain is a home's, which a state's gains tax leaves out, [#489](https://github.com/BobKerns/analysis/issues/489)) for the what-ifs: `fill_bracket`, the after-tax drawdown, IRMAA's MAGI and a strategy's `ctx.tax` all ask it, so none can disagree with the settlement.

A `TaxYear` is read-only:

- **`params`**, the jurisdiction's `params_json` as the calculator's `Params`, in stored units (cents, millionths).
- **Income items** (`items`, `year_items`, `total`, `tagged`): the year's transfers of one tax character from one account to another, summed, and the years before it for carryovers. Each item's accounts carry their kind, subkind, owner and tags; the item carries the tags of the rule or calendar row that posted it.
- **Spending items** (`spending`, `year_spending`, `spent`): the year's payments into expense accounts, summed by payer, expense account, character, and the posting rule's tags and `person`. `spent("tax:rent")` is the year's rent.
- **Mortgages and taxes paid** ([#542](https://github.com/BobKerns/analysis/issues/542)): `mortgages`/`year_mortgages()`, each `mortgage_payment` debt's interest and balance by month (the mortgage debt limit's base), with its interest's tags; and `taxes_paid`/`tax_paid(*jurisdictions)`, the state and local income tax paid in a calendar year, by the payments' dates (the SALT deduction's income taxes): each such jurisdiction's settlements, and what else was paid into its destination account but for spending with an itemized tag, `tax:rental` or the `property_tax` character, which its own line counts or none should, and but for an account the federal tax is paid into too. Spending a business pays (from a `business` account, or of the `business_expense` character) carries no implied tag. The federal return's memo has `income_tax_deducted`, the state and local income tax among the deduction taken.
- **Tags.** An item's `tags` are its rule's, and, in each role the rule has no tag of, its account's: the expense account for spending, the income account for income. So an account tagged `tax:rent` makes every payment into it rent, and a rule into that account tagged `tax:repair` is a repair instead, as the reports' `--by tag:ROLE` groups them. Two rule kinds imply a tag when neither the rule nor the account has a `tax:` one: a `mortgage_payment`'s interest is `tax:mortgage_interest`, an `irmaa` surcharge `tax:medical`.
- **Capital losses**, an HSA's withdrawal **gains over basis** (`hsa_gains`, which only a state that taxes an HSA reads, [#497](https://github.com/BobKerns/analysis/issues/497)), **persons** (each with the final month of their lifespan, if the scenario gives one) and the **household** (`ownership.Household`: who it is, and how much of an account was a decedent's, the rule the basis step-up uses too), the **filing status**, and the **residence** (where the household lives on the year's December 31, `residence.csv`).
- **Tables**: `table`, `indexed` (grown to January by their series), `dated`, `keyed`, `has_table`, `series_of`, as of the year's end.
- **Growth**: `grown(amount, series, since)`, a calculator's own figure that the law set for year `since`, grown by an inflation series to January of this year as `indexed` grows a table's ([#568](https://github.com/BobKerns/analysis/issues/568)), so a household's credit keeps its worth over a long run. Rounding is the calculator's.
- **Earlier years**: `prior(line)`, this jurisdiction's own return line from the year before, for carryovers, and `line_in(line, year)` from any earlier year, for a carryover's history.
- **Upstream returns**: `upstream("federal")`, the same year of a jurisdiction the calculator builds on, prepared on the same ledger. It takes the same `Extra`, so a what-if moves both returns.

### 3. What it returns: `Return`

A `Return` has named lines in cents, each written to `tax_year_summary` as `<jurisdiction>_<line>`, and a memo kept for the engine and for jurisdictions built on it, not written. Every calculator fills `agi`, `taxable_income`, `ordinary_taxable`, `tax`, `nol_carryover` and `capital_loss_carryover`. The engine reads four memo lines: `drawn_ordinary` and `drawn_gain` (what the year drew from traditional money and the gain its sales realized, leaving out reinvested gain distributions, #547), so the after-tax drawdown doesn't tax them twice, `drawn_real_estate_gain` (real estate's part of `drawn_gain`, taken off the drawdown's real estate gain likewise, #489), and `hsa_taxed` (1 when the jurisdiction taxes an HSA as a brokerage account, #497), so the drawdown prices an HSA's gain over basis instead of its balance; without it an HSA's balance is traditional money. A federal calculator's optional `magi` line (`Return.magi`, AGI when left out) is the MAGI `irmaa` and `fill_bracket` read: AGI plus tax-exempt interest (#265).

### 4. Purity

A calculator is a pure function of its `TaxYear`: no clock, files, network, unseeded random numbers, set or hash order, and nothing kept between years or runs. That is what makes a run reproducible and a calculator testable on a year built by hand. `sim tax check` loads a scenario and runs it twice, each in a fresh Python process with its own string-hash seed, and compares every line of every return, reporting the first that differs. It catches the usual mistakes, not all.

### 5. The API and its version

The API is `analysis.sim.tax_api` and the calculator classes and bases in `analysis.sim.taxes`, which a household's calculator may subclass: `federal.Federal`, `state_tables.StateTables` (and its year, `StateTablesYear`), `local.LocalBands`, and `state_base`'s `StateFromFederal`, `StateFromIncome`, `StateYear`, `Adjustment`, `Income` and `Basis`. A state calculator usually builds on `StateTables`, overriding a step of its year (`additions`, `subtractions`, `deductions`, `deductions_at`, `itemized_lines`, `other_deductions`, `tax`, `credits`, `credit_lines`, `excise`, `refundable_credits`) and extending its `Params`. `deductions_at` gives `deductions`' lines at the year's income, for itemized lines whose limits move with AGI ([#542](https://github.com/BobKerns/analysis/issues/542)); the built-in's adds `itemized_lines` (the itemized lines from spending, an `Itemized`, or None for none) to what `deductions` returned, the deduction taken being the larger of its own and the itemized total, so a subclass whose `deductions` figures its own Schedule A overrides `itemized_lines` to return None, or its property tax counts twice; `other_deductions` returns named deductions taken after AGI that may depend on the year's income; `credit_lines` named credits, each written to the return as what it took, in order, after `credits`' total, until the tax is used up (API 2, [#511](https://github.com/BobKerns/analysis/issues/511)). `excise` is a tax apart from the income tax, on a base of its own, added to the tax after the credits: its lines and the tax (API 3, [#489](https://github.com/BobKerns/analysis/issues/489); the built-in's is a state's `state_gains_tax`, Washington's tax on long-term gains). `refundable_credits` returns named credits the state pays whatever the tax, such as a property tax circuit breaker: each is written in full (a name the return already has is a `TaxError`) and taken off last, so the tax can end below zero, which the April settlement pays back as it does any overpayment (API 4, [#568](https://github.com/BobKerns/analysis/issues/568)). The built-ins have none. `analysis.sim.taxes.common` is the built-ins' own helpers, which can change without notice.

`tax_api.API_VERSION` is bumped when a calculator written against the previous version could break, or one written for this version needs what the previous lacks. A module may set `API_VERSION` to the version it was written for; a newer one than the code provides is a load error.

### 6. Parameters and tags

`Params` fields take the units strategies' do (`param_units`, shared with ADR-010): `Dollars` (written `"4000.00"`, received as cents), `Percent` (written `"50%"`, received as millionths), `Tag`, and plain JSON types. The loader checks `params_json` against the kind's `Params`: an unknown key, a wrong type, a bad unit or a missing required field is a load error naming the file, and the stored `params_json` is in stored units. The built-in calculators' params are typed the same way, so a misspelled key is refused rather than ignored.

A `Tag` param names a tag the calculator reads. The loader refuses a scenario where no account, scheduled rule or calendar row carries it, whether the file gives the tag or the param's default stands, so a deduction never silently comes out zero.

### 7. Loading, storage and provenance

**Where the code lives.** Built-in kinds are modules of `analysis.sim.taxes`. A household's are `taxes/*.py` at the data repo's root, shared by every scenario (a scenario folder's `taxes/*.csv` are its jurisdiction files). A module whose name starts with `_` is a helper the others import and registers nothing. A household kind with a built-in's name is a load error: rename it, or promote it.

**Importing** is ADR-010 §4's, shared code (`analysis.sim.plugin_code`): the modules are imported from their source text as a synthetic package named after a hash of all of them, checking a load imports it once, and each run imports a fresh copy and unloads it when it ends. A module that fails to import, or asks for a newer API, is a load error for a scenario whose jurisdictions name a household kind, and a `sim check` warning for one that doesn't.

**Checked at load** (ADR-003 §4.15): a known kind that computes the file's jurisdiction, an `upstream` column that is blank or the kind's own, params its `Params` accepts, and the tags its `Tag` params name.

**The snapshot.** When a jurisdiction names a household kind, the loader copies every module of `taxes/` into the intent's `tax_source` (module, sha256, source), a snapshot table the run file keeps. The engine imports from it, never from the data repo, so a run is self-contained, an ensemble worker needs no data directory, and a run file carries the exact code it ran. Built-in kinds store no source.

**Provenance** ([ADR-004](ADR-004-reproducibility-and-activation.md) §2): `taxes/` is inside the data scope, so editing a calculator changes `scenario_tree_id` and the data stamp, and `sim all` rebuilds what it affects.

### 8. Commands

- `sim tax kinds [KIND]`: the kinds, built-in and household, each with the tax it computes, the returns it builds on, its docstring, and its parameters with their units, defaults and docs (those of a `Params` it extends included).
- `sim tax check [SCENARIO]`: the determinism check (§4).
- `sim tax list|add|edit|delete`, `sim tax state`, `sim tax move`: the jurisdiction files and the residence (ADR-003 §4.15, §4.16).

A guide with a worked example is [docs/guides/tax-calculators.md](../guides/tax-calculators.md).

## Consequences

### Positive

- A state the simulator doesn't ship, or a household's own deduction, is a small class in the data repo, and a run records the exact code it ran.
- Spending and income carry their tags to the calculator, so a deduction that follows what a household pays (rent, property tax, premiums) is written against the ledger, not typed in by hand.
- Every calculator's params are checked at load, the built-ins' included.

### Negative / accepted

- Python can't prove a calculator is pure; `sim tax check` catches the usual mistakes, not all.
- Household code runs with the simulator's privileges: the same trust as the household's CSVs.
- The classes a household calculator subclasses are part of the API, so changing a built-in calculator's steps may need an `API_VERSION` bump.
- A spending item is summed by rule only for rules with tags or a person; other payments into an expense account sum together, which keeps a year quick to build.

## Open questions deferred

- A jurisdiction beyond federal, state and local (a non-resident state return, the credit for tax paid to another state).
- Estimated quarterly payments, probably a strategy reading `ctx.tax`.

## Amendments

Each entry changed this ADR's design. The sections it names were rewritten in place to describe the current design; git history has the earlier text.

- [#511](https://github.com/BobKerns/analysis/issues/511): API 2: a state year's `other_deductions` and `credit_lines` steps; a state's own deductions and credits are rows of the built-in's `state_adjustments` table (§5, deferred questions).
- [#277](https://github.com/BobKerns/analysis/issues/277): a `Person` carries its `final_month`, and a `TaxYear` its `household` and any earlier year's line (`line_in`); the built-in calculators end a decedent's share of the loss carryovers after the year of the death (§2).
- [#547](https://github.com/BobKerns/analysis/issues/547): `drawn_gain` leaves out reinvested long-term gain distributions, which draw no gain out of a lot (§3).
- [#497](https://github.com/BobKerns/analysis/issues/497): `TaxYear.hsa_gains` (an HSA withdrawal's gain over its basis) and the memo line `hsa_taxed` the after-tax drawdown reads (§2, §3); additive, so no API bump.
- [#265](https://github.com/BobKerns/analysis/issues/265): `Return.magi`, the `magi` line or AGI, which `irmaa` and `fill_bracket` read (§2).
- [#489](https://github.com/BobKerns/analysis/issues/489): API 3: a state year's `excise` step, a tax apart from the income tax added after credits, and `Extra.real_estate_ltcg`, the part of a what-if's gain that is a home's, and the memo line `drawn_real_estate_gain` (§2, §3, §5).
- [#568](https://github.com/BobKerns/analysis/issues/568): API 4: a state year's `refundable_credits` step, which can take the tax below zero, and `TaxYear.grown`, a calculator's own figure grown with inflation (§2, §5).
- [#542](https://github.com/BobKerns/analysis/issues/542): `TaxYear.mortgages` and `TaxYear.taxes_paid` for itemized deductions, and the `tax:` tags a `mortgage_payment` and an `irmaa` rule imply; a state year's `deductions_at` step figures deductions at the year's income, and its `itemized_lines` step the itemized lines from spending (§2, §5); additive, so no API bump.
