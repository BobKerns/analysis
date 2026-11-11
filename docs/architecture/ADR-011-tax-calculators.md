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

For each tax year a jurisdiction settles or estimates, the engine builds a `TaxYear` and asks the calculator to `prepare` it once. The prepared year answers `compute(extra)` as often as asked, with more ordinary income or long-term gain on top (`Extra`) for the what-ifs: `fill_bracket`, the after-tax drawdown, IRMAA's MAGI and a strategy's `ctx.tax` all ask it, so none can disagree with the settlement.

A `TaxYear` is read-only:

- **`params`**, the jurisdiction's `params_json` as the calculator's `Params`, in stored units (cents, millionths).
- **Income items** (`items`, `year_items`, `total`, `tagged`): the year's transfers of one tax character from one account to another, summed, and the years before it for carryovers. Each item's accounts carry their kind, subkind, owner and tags; the item carries the tags of the rule or calendar row that posted it.
- **Spending items** (`spending`, `year_spending`, `spent`): the year's payments into expense accounts, summed by payer, expense account, character, and the posting rule's tags and `person`. `spent("tax:rent")` is the year's rent.
- **Tags.** An item's `tags` are its rule's, and, in each role the rule has no tag of, its account's: the expense account for spending, the income account for income. So an account tagged `tax:rent` makes every payment into it rent, and a rule into that account tagged `tax:repair` is a repair instead, as the reports' `--by tag:ROLE` groups them.
- **Capital losses**, **persons**, the **filing status**, and the **residence** (where the household lives on the year's December 31, `residence.csv`).
- **Tables**: `table`, `indexed` (grown to January by their series), `dated`, `keyed`, `has_table`, `series_of`, as of the year's end.
- **Last year**: `prior(line)`, this jurisdiction's own return line from the year before, for carryovers.
- **Upstream returns**: `upstream("federal")`, the same year of a jurisdiction the calculator builds on, prepared on the same ledger. It takes the same `Extra`, so a what-if moves both returns.

### 3. What it returns: `Return`

A `Return` has named lines in cents, each written to `tax_year_summary` as `<jurisdiction>_<line>`, and a memo kept for the engine and for jurisdictions built on it, not written. Every calculator fills `agi`, `taxable_income`, `ordinary_taxable`, `tax`, `nol_carryover` and `capital_loss_carryover`. The engine reads two memo lines, `drawn_ordinary` and `drawn_gain` (what the year drew from traditional money and the gain it realized), so the after-tax drawdown doesn't tax them twice.

### 4. Purity

A calculator is a pure function of its `TaxYear`: no clock, files, network, unseeded random numbers, set or hash order, and nothing kept between years or runs. That is what makes a run reproducible and a calculator testable on a year built by hand. `sim tax check` loads a scenario and runs it twice, each in a fresh Python process with its own string-hash seed, and compares every line of every return, reporting the first that differs. It catches the usual mistakes, not all.

### 5. The API and its version

The API is `analysis.sim.tax_api` and the calculator classes and bases in `analysis.sim.taxes`, which a household's calculator may subclass: `federal.Federal`, `state_tables.StateTables` (and its year, `StateTablesYear`), `local.LocalBands`, and `state_base`'s `StateFromFederal`, `StateFromIncome`, `StateYear`, `Adjustment`, `Income` and `Basis`. A state calculator usually builds on `StateTables`, overriding a step of its year (`additions`, `subtractions`, `deductions`, `other_deductions`, `tax`, `credits`) and extending its `Params`. `other_deductions` returns named deductions taken after AGI that may depend on the year's income, and `credits` a total or named lines, each written to the return (API 2, [#511](https://github.com/BobKerns/analysis/issues/511)). `analysis.sim.taxes.common` is the built-ins' own helpers, which can change without notice.

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
- Itemized deductions figured from tagged spending (property tax, mortgage interest, charity, medical; [#542](https://github.com/BobKerns/analysis/issues/542)).

## Amendments

Each entry changed this ADR's design. The sections it names were rewritten in place to describe the current design; git history has the earlier text.

- [#511](https://github.com/BobKerns/analysis/issues/511): API 2: a state year's `other_deductions` step and named `credits`; a state's own deductions and credits are rows of the built-in's `state_adjustments` table (§5, deferred questions).
