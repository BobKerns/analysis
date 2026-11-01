# ADR-010: Responsive strategies

- **Status:** Accepted
- **Date:** 2026-10-28
- **Supersedes:** —
- **Amended:** see [Amendments](#amendments) at the end.
- **Related:** [ADR-003](ADR-003-registries-and-csv-schemas.md) §4.1b (`allocations.csv`), §4.7 (chains), §4.12–§4.14 (`strategies/`, `targets.csv`, `strategy_vary/`); [ADR-002](ADR-002-storage-schema.md) (`strategy`, `strategy_source`, `strategy_vary`, `allocation_target`, `strategy_decision`); [ADR-006](ADR-006-ensemble-scenarios.md) §3b (varying a parameter); [ADR-004](ADR-004-reproducibility-and-activation.md) §2 (strategy code is data); issues [#326](https://github.com/BobKerns/analysis/issues/326) (the design, in stages), [#339](https://github.com/BobKerns/analysis/issues/339) (stage 1a), [#387](https://github.com/BobKerns/analysis/issues/387) (spending) and [#421](https://github.com/BobKerns/analysis/issues/421) (year-end actions), [#305](https://github.com/BobKerns/analysis/issues/305) (guard rails, which later stages trigger on).

## Context

Every decision in a scenario is fixed when it is written. A chain draws its steps in file order whatever the portfolio looks like; a rule spends the same amount whatever happened last year. A household doesn't behave that way. It takes a withdrawal from whatever is overweight, trims spending after a bad year, sells to stay in a tax bracket.

Those policies are small programs, and their variety is open-ended, so a registry of rule kinds can't hold them. They need to be code the household writes, kept beside its data, with a few common ones built in. Running household code inside the engine must not cost what the simulator relies on:

- **Reproducibility** ([ADR-004](ADR-004-reproducibility-and-activation.md)): a run records exactly what produced it, and runs again the same way.
- **Integrity of the ledger**: lots, basis, tax character, refill and the negative-balance rule stay the engine's business.
- **Being explainable**: a person can see each decision and why it was made.

## Decision

### 1. A strategy is a class with hooks

A strategy is a Python class, a subclass of `strategy_api.Strategy` marked with `@register`. It names its `kind`, declares a frozen dataclass `Params`, and overrides the hooks it uses. Each hook reads a read-only `Context` of the run as of the current month and answers with a `Decision` (moves, a one-line note, and the numbers behind it) or `None`, "no opinion". The engine posts the moves through its own code. A strategy never touches engine state.

A scenario's `strategies/` folder (ADR-003 §4.12) says which strategies it runs, one file each, named for the strategy: a kind, what it owns (a chain, the scheduled rules carrying a tag, or both; a kind that acts only at year end owns neither, and acts on the lots its parameters name), optional `start`/`end` months, `enabled`, and `params_json`. Like a chain's file, a later scenario's file for a strategy replaces the earlier one's whole. An ensemble can vary a strategy's parameters from `strategy_vary/<name>.csv` (ADR-003 §4.14, ADR-006 §3b).

### 2. Where the hooks attach

| Hook | When it is asked | Stage |
| --- | --- | --- |
| `draw(ctx, chain, need)` | Every draw through a source chain the strategy owns: a rule's debit, a refill look-ahead, a calendar transfer, an `rmd` rule's chain | 1 |
| `place(ctx, chain, amount)` | Every sweep into a destination chain it owns: an overflow, a deposit through the chain | 1 |
| `year_start(ctx)` | In the strategy's first active month and each January after, once the month's calendar events and inflation indexes have moved and before any rule posts | 2 |
| `scale(ctx, rule, amount)` | Before each posting of a scheduled rule it owns, with the amount the rule would post | 2 |
| `year_end(ctx)` | Each December it is active, after every rule has posted that month (`rmd` and `fill_bracket` included) and before the month-end settlement (overflow sweeps, lot state) | 3a |

Stage 3b adds `month_start` and `on_breach` (#305's guard rails). The base class defines each hook with a do-nothing default, so a stage adds callers, never signatures.

A draw is decided from the month's state at the moment it is asked (Bob, 2026-10-21): the balances as they stand, not a forecast. That is a model a person can follow, and it is the model a household actually uses. Spending is decided once a year (`year_start` sets the year's course in the strategy's memory) and applied to each posting (`scale`), as a household sets a budget and then lives by it. Year-end actions come after the year's last rule, a stated constraint rather than a priority: a sale or a harvest is sized against the year's income as complete as the run knows it. A run that ends before December has no year-end call in its last year.

### 3. The API (`analysis.sim.strategy_api`)

This module is the only part of the simulator a strategy imports. Engine internals change behind it; `API_VERSION` (now 3: 2 added the spending hooks and context, 3 the year-end hook, its moves and `ctx.tax`) changes only when a strategy written for the previous version could break. A module may set `API_VERSION` to the version it was written for, and one newer than the code is refused at load.

**Parameters.** `Params` fields are typed with units that follow the CSV rules: `Percent` (written `"5%"`, received as millionths), `Dollars` (`"1200.00"`, received as cents), `Tag` (an account tag `role:name` some account carries), `TargetSet` (a set in `targets.csv`), `Account` (an account the scenario defines); any other field is a plain JSON value. A field may be optional (`Dollars | None = None`), taking `null` for none. The loader converts and checks them against `Params`, so a strategy never parses a string, and a bad value is a load error naming the row.

**The context** (`Context`) shows the run up to the current month and nothing after it:

| Member | What it gives |
| --- | --- |
| `month`, `year` | The current month end |
| `lots` | Every asset and liability lot as a `LotView`: account, lot, kind, subkind, lot subkind, tax treatment, owner, the account's tags, symbol, balance, cost basis, minimum balance, `available` (balance less minimum), Roth qualified, `acquired` (for the holding period, `is_long_term`), `taxable_sale` (a sale realizes a taxable gain: a taxable, non-cash lot with a basis) and `gain` (balance less basis) |
| `tagged(tag)` | The asset lots of the accounts carrying a tag |
| `allocation(lots)` | An `Allocation`: dollars and shares per asset class, and each lot's class weights, as of this month (§5) |
| `targets(set)` | A target set's shares in force this month |
| `memory` | The strategy's own dict, kept across calls; JSON values only |
| `end` | The scenario's last month: the plan's horizon, not future data |
| `cpi(since)` | The CPI factor from a past month end to now, in millionths |
| `people` | Each person's role, birthdate, age in whole years, whether alive, and the final month a lifespan gives |
| `budget()` | The strategy's own rules' spending over the twelve months from this one, at this month's prices: each rule's amount indexed to now, times the times it posts in them (within its start, end and person's lifespan, and the run's end). Twelve months rather than the calendar year, so a run that starts in May sees a whole year's plan; a run ending mid-year budgets only the months it has left |
| `tax` | This year's income tax (§3.2) |
| `annual_return(lots, year)` | A finished year's investment return on the lots: the growth posted to them that year over their balance at its start (the run's opening balance in its first year); `None` when they held nothing or the year is before the run. Asking for the current year or a later one is an error |

Views are frozen dataclasses and read-only mappings. Future series values, future calendar rows and other ensemble members are not reachable.

`ChainView` gives the owned chain's name, role, steps (each with its source, `maximum` and `percent`) and leaves: the lots its steps reach, nested chains included, in step order. That order is the household's own preference, and a natural tie-breaker.

**What the engine does with a decision.** For `draw`:

1. Each move must be a `Take` naming a leaf of the owned chain with a positive whole number of cents, and the takes may not add up to more than the need. Anything else fails the run (§3.1).
2. Each take posts through the same leaf path the chain's steps use: the leaf's own refill look-ahead and minimum balance, Roth basis and the five-year rule, a taxable lot's basis and gain legs. A take bigger than the leaf can spare posts what it can.
3. Whatever is still outstanding falls through to the chain's own steps, as if the strategy hadn't been asked. A strategy can't leave a debit unpaid by mistake.

`place` mirrors this with `Put`, each destination's maximum balance applying as it does for a step. Step caps (`maximum`, `percent`) bind the chain's own steps, not the strategy's moves; a strategy that wants to honor them reads them from `ChainView`.

For `scale`, the `amount` is the **budget**: what the rule would post this month, already inflation-indexed. The answer is one `Spend(cents)`: zero skips the posting, and more than the budget raises it. `None` posts the budget. Either way the posting is recorded with its budget and what was delivered (§4), so every report can compare the two. The `RuleView` gives the rule's name, kind, tags, source and destination, its written amount and its inflation series. Only `fixed_amount` rules can be owned: a mortgage, an IRMAA charge or a tax payment isn't the household's to trim.

`year_start` moves nothing; a decision it returns is recorded with its note and detail, so the log says "cut spending 10%" in the year it happened.

For `year_end`, the moves are `Transfer` and `Realize`, posted at once and in order, so a later move sees the balances an earlier one left:

- `Transfer(source, to, amount, in_kind=False)` moves money out of an asset lot into another asset lot, or into an expense account by name (a gift). It posts **as a `one_off_transfer` with a blank `tax_character` does** (ADR-003 §3): out of a taxable lot it is a sale, with its return-of-capital and gain legs; from a traditional lot into a Roth lot a conversion (`tax_deferred_out`); out of a Roth lot a distribution under the five-year rule; the source's minimum balance and refill apply as for that row. `in_kind=True`, only from a lot with `taxable_sale` into an expense, gives the shares themselves: their basis leaves with them pro rata and no gain is realized, as a gift of appreciated stock is taxed (the recipient takes over the basis).
- `Realize(lot, amount)` sells and rebuys within a lot with `taxable_sale`: a basis leg and a gain leg (negative for a loss) from the lot to itself, so the balance is unchanged and the basis moves to the sale value. `sale_gain(balance, basis, amount)` gives the gain to the cent. This is gain harvesting, and loss harvesting.

A move posts no more than its lot holds. A move the hook doesn't allow (a `Take`, a lot that isn't an asset, an `in_kind` into an asset, a `Realize` on a lot with no taxable gain) fails the run. Every leg a year-end move posts has `reason_kind = 'strategy'` and `reason_id` its decision, and points at it through `strategy_decision_id`; income, expense, gains and tax reports see the legs as any others: a cash gift is spending, a harvest a realized gain.

Two simplifications, both documented for the reader (Bob, 2026-11-01):

- **Holding period.** A lot has one acquired date, which a `Realize` keeps. The rebought shares really start a new holding period, so a sale of them within a year would be short-term; the model calls it long-term.
- **Wash sales.** Rebuying the same fund within 30 days disallows a harvested loss, so households switch to a similar fund. A loss `Realize` models that switch: the lot keeps its growth, as if the replacement tracks it.

Gift tax isn't modeled: the lifetime exemption is far above what the personas give, and the annual exclusion is a gift strategy's parameter. A charitable gift's deduction isn't either.

Outside `start`/`end`, or when `enabled` is false, the chain behaves as if no strategy owned it, and owned rules post their budget with no baseline row.

#### 3.1 Errors and determinism

An exception in a strategy, a move it isn't allowed, or memory or detail that isn't JSON fails the run, with a message naming the strategy, its kind, the hook and the month. There is no silent fallback, which would hide a bug in a 400-member ensemble.

A strategy must be a pure function of its context, parameters and memory: no clock, files, network or unseeded random numbers. Python can't enforce that, so three things back it up: the views are read-only and the moves are checked; memory is serialized after every call, which catches state that can't be recorded; and `sim strategy check` runs a scenario twice, each time in a new Python process with a different string-hash seed (so set order and `hash()` vary as they would between ensemble workers), and compares every decision, reporting the first difference.

#### 3.2 The tax view

`ctx.tax` reads this year's income tax through the estimator the April settlement uses (`_year_tax_estimate`, shared with `fill_bracket` and the after-tax drawdown), so a strategy and the settlement can't disagree. Each answer is about the year as it stands when the hook is asked, with more income on top; in December, after every rule, `estimate()` is what the following April settles, unless something posts between.

| Member | What it gives |
| --- | --- |
| `filing_status` | This year's federal filing status (it changes after a death) |
| `ytd` | This year's income so far by tax character, in cents |
| `estimate(ordinary=0, ltcg=0, scope="federal")` | A `TaxEstimate` with that much more ordinary income and long-term gain: `tax`, `taxable_income`, `ordinary_taxable`, `agi` (IRMAA's MAGI) |
| `marginal(ordinary=0, ltcg=0, next="ordinary", scope=..., step=$100)` | The tax on the next `step` cents of `next` income as a rate: gains pushed into a higher band and Social Security made taxable included, as `fill_bracket`'s `max_marginal_rate` counts them |
| `bracket_top(rate, scope=...)` | The ordinary taxable income where this year's bracket at `rate` ends |
| `ltcg_band_top(rate)` | The taxable income where this year's long-term gains band at `rate` ends; `0%`'s is the most a gain can be realized under tax-free |
| `irmaa_floors()` | This year's IRMAA tier floors for the filing status, empty without `irmaa_tiers` |
| `last_year` | Last year's `tax_year_summary`, or None before it is settled |

A scope reads its one `tax_accumulator` rule; asking a scenario with none (or two) is a `StrategyError`, which fails the run. Extra long-term gain doesn't net against a capital-loss carryover, so in a year with one, `estimate` overstates what a harvest costs.

### 4. Loading, storage and provenance

**Where the code lives.** Built-in kinds are modules of `analysis.sim.strategies`. The household's are `strategies/*.py` at the data repo's root, shared by every scenario like `allocations.csv`; a module whose name starts with `_` is a helper the others import (`from ._helpers import x`) and registers nothing. A data-repo kind with a built-in's name is a load error: promote the module or rename it, never shadow.

**Importing.** The modules are imported from their source text as a synthetic package named after a hash of all of them (`_strategies_<12 hex>`), and relative imports between them work. Checking a load imports that package once. Each run imports a fresh copy under a name of its own and unloads it when it ends, so module-level state (a cache, a counter) never carries from one run or ensemble member to the next, and two versions of a module never meet. A module that fails to import, or asks for a newer API, is a load error carrying its traceback for a scenario with strategies, and a `sim check` warning for one without.

**The snapshot.** When a scenario's enabled strategies include a data-repo kind, the loader copies every module of `strategies/` into the intent's `strategy_source` (module, sha256, source): helpers are imported by name, so the snapshot is the whole folder. The engine imports from the snapshot, never from the data repo, so `run_scenario(intent, run)` stays self-contained, an ensemble worker needs no data directory, and a run file carries the exact code it ran. Built-in kinds store no source: the code version covers them.

**Checked at load** (ADR-003 §4.12, §4.13): a known kind; a chain or a `rules` tag, or both, for a kind with a `draw`, `place` or `scale` hook (a kind with only `year_end` owns neither); a chain that exists, has steps (not an account's own leaf chain), and whose role fits a hook the kind implements; a `rules` tag some scheduled rule carries, only on `fixed_amount` rules, for a kind with a `scale` hook; one enabled strategy per chain and per rule, one row per strategy file, and no target set, date and class twice in `targets.csv`; `start` no later than `end`; `params_json` a JSON object the kind's `Params` accepts (an `Account` naming an account the scenario defines); what the kind's own `check(params, scenario)` finds, a class method that sees each account's kind and subkind and how many `tax_accumulator` rules each scope has (`ScenarioView`), for needs a parameter's unit can't state: `harvest` with `gains`, and a cash `gift` with `max_tax`, need exactly one federal tax rule, and a `gift`'s `to` must be an expense account of subkind `gift`; a destination chain without step `tax_character`/`basis_of` at any depth, which depend on the step order a strategy would override (#199); and every asset lot in a strategy's `Tag` portfolio classed by `allocations.csv` (§5). A disabled row is checked too, except for chain ownership. A `strategy_vary/` file is checked against its strategy's kind: each parameter one it takes as a percent, dollars or a number, its distribution valid, and the authored value within its range.

**Stored.** The intent gains `strategy` (one row per enabled strategy: its chain, its rule tag, `params_json` in stored units), `strategy_source`, `strategy_vary` (each varied parameter, which an ensemble member resolves into `params_json`) and `allocation_target`; all four are snapshot tables, copied into run files. A run file adds `strategy_decision`, one row per hook that answered with a decision (a `None` writes nothing): what it was asked, what its own moves posted (`chosen_cents`), what the chain's steps then covered (`fallback_cents`), its note and detail, and its memory when it differs from the strategy's last row (so a change made while answering `None` shows on the next decision). Every transfer leg a move posted (a withdrawal's basis and gain legs alike) points at that row through `transfer_event.strategy_decision_id`, and keeps its own `reason_kind` (`rule`, `refill`, `overflow`), so income, expense and tax reports see the same flows as before. A `scale` row names its rule (`scheduled_transaction_id`). Each posting of an owned rule while its strategy is active adds a `spending_baseline` row: the rule, the strategy, the decision if `scale` gave one, and the **budgeted** and **delivered** cents. A skipped posting still has its row, with nothing delivered. Budgeted spending for any grouping is delivered spending plus what the owned rules in it left unspent.

**Provenance** ([ADR-004](ADR-004-reproducibility-and-activation.md) §2). `strategies/` is inside the data scope, so editing a strategy changes `scenario_tree_id` and the data stamp like any other data edit, and `sim all` and background builds rebuild what it affects.

### 5. Allocation

`ctx.allocation`, `sim allocation` and the load-time portfolio check class a lot the same way, through one function (`asset_classes.class_weights`), so they can't disagree:

1. The lot's symbol's `allocations.csv` set in force: the latest `as_of` on or before the month, else the earliest.
2. Else the lot's `<account>:<lot>` key in `allocations.csv`: a lot with no symbol (one that grows at a rate series) is classed by a row whose `symbol` is, say, `Asset_IRA_Alice:stable` (Bob, 2026-10-21). A ticker has no colon, so the two can't collide. Price fetching, symbol completion and symbol listings skip these keys, and renaming the account or the lot rewrites them (keeping the old key too while a scenario the rename doesn't reach still has the old name, since the file is shared).
3. Else the class the symbol means (`CASH`, `CD`) or the account subkind's (cash, real estate, ...).
4. Else `unclassified`.

A lot in a strategy's portfolio that ends up `unclassified` is a load error naming the row to add (Bob: "resolve by adding to allocations.csv"), since it would skew every share.

**Targets.** `targets.csv` (ADR-003 §4.13) holds named sets of class weights, each dated by `as_of` like `allocations.csv`'s, so a glide path is more rows. The set in force is the latest on or before the month. A class a set leaves out has target 0%: holding it counts as overweight.

### 6. Built-in kinds

#### 6.1 `rebalance`

Parameters: `portfolio` (a tag), `targets` (a target set), `band` (default 5%) and `step` (default 1%).

- While every class of the portfolio is within `band` of its target, it answers `None`: the chain's own order, the household's tax-driven preference, decides. This is tolerance-band rebalancing as practiced.
- Beyond the band, the amount moves in slices of `step` (at least $1). Each slice comes from (for `draw`) or goes to (for `place`) the leaf that most reduces the squared distance between the portfolio's class dollars and the targets' share of the portfolio after the whole amount has moved. Ties go to the leaf earlier in the chain. A blended fund moves several classes at once, which the distance accounts for. A leaf outside the portfolio moves no class, so it is used only when no portfolio lot helps.
- The note reads like `bond 53.6% vs 40.0% target (band 5%): took $961 from Asset_TraditionalIRA_Alice:default`; `detail` holds the shares before and after and the targets.

Greedy slicing is easy to explain ("each slice came from whatever helped most"), deterministic, and within one slice of the exact optimum; a solver would be harder to explain and add a dependency.

#### 6.2 Spending: `threshold`, `guardrail`, `ceiling_floor`

Each owns the rules carrying its `rules` tag and moves their level by one yearly factor, set in `year_start` and applied in `scale` (delivered = budget × factor, rounded to cents). The plan's mix across the rules is kept and only its level moves. Each takes `portfolio`, the account tag whose asset lots it reads.

| Kind | Parameters | Rule |
| --- | --- | --- |
| `threshold` | `minimum` (dollars of its first month, raised with CPI; default 0), `skip_after_loss` (default false) | Floor and upside: the owned rules (tag only the optional ones) post in full in a year that starts with the portfolio at or above the minimum and, with `skip_after_loss`, after a year whose return wasn't negative; otherwise not at all |
| `guardrail` | `upper` 20%, `lower` 20%, `adjust` 10%, `skip_inflation_after_loss` true, `final_years` 15 | Guyton-Klinger. The first year sets the initial rate, the budget over the portfolio. Each January the rate is this year's spending over the portfolio. After a negative year, with the rate above the initial, the year's inflation raise is skipped: the raise the owned rules' budget got since last January, never more than inflation, so a rule with a fixed amount loses nothing. Then, above the initial rate by `upper`, spending is cut by `adjust` (not in the scenario's last `final_years`); below it by `lower`, it is raised by `adjust` |
| `ceiling_floor` | `rate` (default 0%: the first year's budget over the portfolio, so it starts on plan), `ceiling` 5%, `floor` 2.5% | Dynamic spending: aim at `rate` of the portfolio (the share for the owned rules alone, which sets their first year outright, so a strategy owning only optional rules leaves it at 0%), with the change from last year's spending, after inflation, held between −`floor` and +`ceiling` |

The rates compare spending to the portfolio relative to where they started, so owning only the optional rules still triggers as owning all spending would: the budget grows with inflation either way. What differs is the size of a cut, which falls on the owned rules alone. That is floor and upside, the shape Bob described ("make certain spending optional depending on circumstance or even history").

**A cut is proportional.** The one factor scales every owned rule alike: a 10% cut makes a $5,000 trip $4,500. That is easy to read and to describe, though not always what a household would do: a trip is taken or not. When it isn't wanted, the choice is explicit rather than a mode of the built-ins (Bob, 2026-10-31): a strategy per item (each its own tag; `threshold` skips its rules whole), or a data-repo strategy whose `scale` decides rule by rule. A strategy owns only what its tag names, never "all spending" by default, so a Part B premium or a tax payment is never scaled by surprise.

#### 6.3 Year-end: `harvest`, `gift`

Both act each December on the taxable lots of their `portfolio` tag and own nothing.

| Kind | Parameters | Rule |
| --- | --- | --- |
| `harvest` | `portfolio`, `gains` true, `band` 0%, `max_marginal_rate` (blank: the band's rate), `losses` false, `loss_minimum` $1,000 | With `gains`, realizes long-term gains until taxable income reaches the top of the long-term gains `band` (found by bisection on `estimate`, since a gain can make Social Security taxable), largest gain share first (the most basis raised per dollar sold), and stops sooner where the next $100 of gain would cost more than `max_marginal_rate`. A year that stops at once says why: "the first $100 of gain would cost 18.5%". With `losses`, each lot down by `loss_minimum` or more is sold and rebought at its loss instead; a year that harvests losses harvests no gains, which would use them up. The engine's capital-loss rules ($3,000 against ordinary income, the rest carried) do the rest |
| `gift` | `to` (an expense account, subkind `gift`), `amount` (dollars of its first month, raised with CPI), `portfolio`, `minimum` 0 (raised alike), `in_kind` true, `max_tax` (blank: no limit) | Gives `amount`, or what the portfolio holds above `minimum` if that is less, nothing when it holds no more. `in_kind` gives the most-appreciated shares first (that gain is never taxed; cash lots give cash). Otherwise it gives cash, least-appreciated first, and gives less where the sale would add more than `max_tax` to the year's federal tax (a lot sold at a loss counts as no gain there, since `estimate` doesn't net losses, so the cap errs low). Its `year_start` notes the first month, from which CPI raises the amounts. Each recipient gets a strategy of its own |

Notes read like `realized $24,178 of long-term gain into the 0% band (room $24,178)` and `gave $19,000 in kind to Expense_Family`.

### 7. Commands

`edit` is guided and names complete, as elsewhere (ADR-003 §7):

| Command | What it does |
| --- | --- |
| `sim strategy kinds [KIND]` | The kinds, built-in and data-repo, each with its docstring, hooks and parameters (unit, default, the field's docstring) |
| `sim strategy list [SCENARIO]` | The scenario's strategies, and the scenario each is inherited from |
| `sim strategy add [NAME]` | Writes `strategies/NAME.csv`. Asks for the kind, what it owns (a chain of a fitting role for a kind that draws or places, a rule tag for one that scales; blank for none when it does both) and each parameter not given as `--param NAME=VALUE`. `--chain` and `--rules` give them |
| `sim strategy edit [NAME]` | Guided with no options; options change just those fields. Editing an inherited strategy writes the scenario's own file for it, a whole override; `--disable` switches it off for this scenario and its descendants. The kind can't change |
| `sim strategy delete [NAME]` | Deletes the scenario's own file and its own `strategy_vary/` file for the strategy (restoring an inherited one it overrode, varied as its own scenarios vary it); refuses an inherited one, pointing at `--disable` |
| `sim strategy log [SCENARIO\|RUNFILE]` | A run's decisions: month, strategy, hook, asked, chosen, chain steps, note (`--strategy`, `--year`); a `scale` decision's note starts with its rule |
| `sim strategy check [SCENARIO]` | Loads the scenario and runs it twice, comparing every decision |
| `sim target list\|set\|delete` | `targets.csv`; `set` takes `CLASS=PERCENT` like `sim symbol add`, and sets a class the scenario inherits but leaves out to 0% |
| `sim allocation --drift` | Each year-end of a run: the portfolio's share in each class less its target, in percentage points, and the largest. `--targets` and `--portfolio` default to the run's strategy's |
| `sim explain` | Adds each strategy's decision count and what it moved, against what it was asked; a year-end strategy, what its moves moved and the gain they realized |
| `sim report spending` | By year: budgeted, delivered, their difference and `of_plan` (the level, first), then `redirected` and `filled:GROUP` (the shape); below it the RMS of the yearly differences, `redirected` on average and at its worst, and the cosine similarity. `--by category\|account\|tag:ROLE` groups as `sim report expenses` does; CPI-constant dollars by default |
| `sim ensemble spending [RECIPE]` | Per year across members: delivered spending's 5th, 50th and 95th percentiles and its CVaR (worst 5% and 20%), the median plan, the median `of_plan` and `cut`, the share of members below plan. Below it, over the run: each member's worst whole year (median and CVaR across members), its `of_plan`, the years cut on average and the members ever cut. `--category` one expense category; CPI-constant dollars by default; `--chart` draws the spread by year against the median plan |
| `sim ensemble drift [RECIPE]` | Per year end across members: percentiles and the largest of each member's largest drift (or `--asset-class`'s, signed), and with a band the share of members past it then and at some month so far. `--targets` and `--portfolio` default as above; `--chart` draws the spread over every month |
| `sim compare --section spending` | Each scenario's delivered spending by year with each variant's difference, then each run against its own plan: delivered and budgeted in all, `of_plan`, RMS, the lowest whole year, `redirected`. Shown by default when any run has a spending strategy |
| `sim compare --section strategies` | Each scenario's strategies and what they moved, then each run's largest drift by year end against one target set (a strategy's in any run, the baseline's first), so a strategy on and off sit side by side. Shown by default when any run has strategies or targets; the `differences` section then also lists differing strategies and target weights |

**Drift** (`sim/drift.py`) is a class's share less its target, in percentage points; above zero is overweight. A month with nothing held has no drift. Class dollars come from each asset lot's balance split by `class_weights` (§5), the same function the strategies' context uses, worked out once per stretch of months between two `allocations.csv` `as_of` dates. A drift view defaults to a strategy's `targets` and `portfolio`, and shades that strategy's `band` (its parameter, else its kind's default).

**Notebooks.** `accounts.ipynb` draws one run's drift, a line per class with the band and a tick for each month a strategy decided; `scenario_compare.ipynb` overlays the compared runs' drift (a panel per class and one of the largest) over what each scenario's strategies did; `ensemble.ipynb` draws the spread of drift across members (`drift_charts.py`, widget-free, so the CLI draws the same figures).

**Ensembles.** The series store keeps each member's month-end dollars per asset class, for every asset lot and for each account tag's asset lots (a strategy's portfolio is a tag), and the batch's target sets and strategies with their bands ([ADR-006](ADR-006-ensemble-scenarios.md) §8, format 3). Drift against any set is worked out at read time, as guard rails are, so a store needs no rebuild to show another target set.

**Spending against the plan** (`reports.spending_by_year`, `spending_report`). Delivered spending is what `sim report expenses` counts, and budgeted adds back what the strategies left unspent, from `spending_baseline`, for the owned rules that pay straight into an expense account (an owned rule into an asset or a chain moves money, so its shortfall isn't spending). The level is the headline (Bob, 2026-10-21); the shape is a diagnostic beside it, never alone, since a proportional cut has a perfect shape. **Redirected** is half the sum of the differences between each group's share of delivered and of budgeted spending (total variation distance): "10% of the dollars went somewhere other than the plan's mix", small groups counting fully. **Filled** is a group's delivered over its budget. Cosine similarity is shown too, labelled secondary: on spending mixes it sits near 1 and follows the largest groups (plan 60/30/10, delivered 70/20/10: cosine 0.983, redirected 10%).

**Across an ensemble** (`EnsembleSeries.spending`, `reports.ensemble_spending_table`, `ensemble_spending_summary`). The series store keeps each member's delivered and budgeted spending by month and expense category (format 5), so any category or all of them, in either dollars, is read without the member files. The worst year counts whole years only: a run starting in May spends part of its first year, which would otherwise always be the worst. `net_worth.ipynb` draws one run's spending against its plan (`spending_charts.py`, widget-free like the drift charts), `scenario_compare.ipynb` the compared runs', and `ensemble.ipynb` the spread across members.

**On vs off.** A strategy's effect is a comparison: a child scenario switches it off (`sim strategy edit NAME --disable` writes the override file), and `sim compare` or `scenario_compare.ipynb` set the two runs side by side, the net worth and break-even sections saying what it costs or gains.

Each edit goes through a staged edit, so a strategy the loader would reject is never written.

**Gains.** `sim report gains` adds a `harvested` column when a run's strategies harvested gains: realized and taxed, but outside the realized-cash total, since no cash moved.

### 8. Later stages, and #305

Stage 3b adds `month_start`, triggers on guard rails (`on_breach`, a `breach_event` table) and `ctx.history`; it is designed in [#326](https://github.com/BobKerns/analysis/issues/326)'s stage 3 plan.

Issue #305 owns guard rails, their measures (`sim/measures.py`) and their post-hoc evaluation; guard rails stay out of `scenario_tree_id`. A later stage adds a `trigger` column to a strategy's file naming a rail and its margin (`<rail>:annual` or `<rail>:cumulative`). Only a triggered rail enters the intent and is evaluated in the engine, through the same causal measures #305's reports use, with breach rows linked to the decisions that answered them. A rebalancing `draw` moves money from portfolio lots to checking, so #305's "net withdrawals from a tagged portfolio" counts it correctly.

## Consequences

### Positive

- A household's policy is code it can read, test and change, kept beside its data, and a run records the exact code it ran.
- The engine keeps the ledger: a strategy chooses among moves the chain already allows, and its moves post through the same paths as a step's.
- Every decision is a row with a note, and every leg it caused points at it, so "why did the IRA pay for this" has an answer.
- Spending set by a strategy is always reported against the plan it changed, so a cut can't pass for a cheaper plan.
- A strategy is opt-in per chain and per scenario, so a variant with `enabled` false compares strategy off against on.

### Negative / accepted

- Python can't prove a strategy is deterministic; `sim strategy check` catches the usual mistakes, not all.
- Data-repo code runs with the simulator's privileges. It is the household's own code in its own repository, which is the same trust as its CSVs.
- A strategy that fails fails the run. In an ensemble one bad member fails its batch; that is preferred to a silent fallback.
- `rebalance` greedy slices are within one `step` of the optimum, not at it.

## Open questions deferred

- Randomness for a strategy that wants it (`ctx.rng()`, keyed like the ensemble's streams).
- A `trigger` column and the guard-rail evaluation it needs (stage 3, with #305).
- Whether `place` should see a destination's remaining room before the engine applies `maximum_balance`.

## Amendments

Each entry changed this ADR's design. The sections it names were rewritten in place to describe the current design; git history has the earlier text.

- [#359](https://github.com/BobKerns/analysis/issues/359): allocation drift charts for a run, across an ensemble (the series store keeps class dollars per member) and for runs side by side; `sim ensemble drift`, `sim compare --section strategies` (§7, §8).
- [#387](https://github.com/BobKerns/analysis/issues/387): spending strategies: `year_start` and `scale` hooks, rules owned by tag, `end`/`cpi`/`people`/`budget`/`annual_return` in the context (API version 2), the recorded baseline, built-ins `threshold`, `guardrail` and `ceiling_floor`, `sim report spending` (§1–§4, §6.2, §7, §8).
- [#408](https://github.com/BobKerns/analysis/issues/408): spending across an ensemble's members (series store format 5), `sim ensemble spending`, `sim compare --section spending`, spending charts in the notebooks; a cut stays proportional, with separate strategies or a custom one for anything else (§6.2, §7, §8).
- [#408](https://github.com/BobKerns/analysis/issues/408): a strategy is one file, `strategies/<name>.csv`; `strategy_vary/<name>.csv` varies its parameters across an ensemble (§1, §4, §8).
- [#421](https://github.com/BobKerns/analysis/issues/421): year-end actions: the `year_end` hook with `Transfer` and `Realize` moves and `reason_kind` `strategy`, `ctx.tax`, `Account` and optional parameters, a kind's load `check` (`ScenarioView`), `LotView`'s `acquired`, `taxable_sale` and `gain` (API version 3), a year-end kind owns nothing, built-ins `harvest` and `gift`, the `harvested` gains column (§1–§4, §6.3, §7, §8).
