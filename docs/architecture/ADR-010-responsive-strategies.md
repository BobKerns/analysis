# ADR-010: Responsive strategies

- **Status:** Accepted
- **Date:** 2026-10-28
- **Supersedes:** —
- **Amended:** see [Amendments](#amendments) at the end.
- **Related:** [ADR-003](ADR-003-registries-and-csv-schemas.md) §4.1b (`allocations.csv`), §4.7 (chains), §4.12–§4.13 (`strategies.csv`, `targets.csv`); [ADR-002](ADR-002-storage-schema.md) (`strategy`, `strategy_source`, `allocation_target`, `strategy_decision`); [ADR-004](ADR-004-reproducibility-and-activation.md) §2 (strategy code is data); issues [#326](https://github.com/BobKerns/analysis/issues/326) (the design, in stages), [#339](https://github.com/BobKerns/analysis/issues/339) (stage 1a) and [#387](https://github.com/BobKerns/analysis/issues/387) (spending), [#305](https://github.com/BobKerns/analysis/issues/305) (guard rails, which later stages trigger on).

## Context

Every decision in a scenario is fixed when it is written. A chain draws its steps in file order whatever the portfolio looks like; a rule spends the same amount whatever happened last year. A household doesn't behave that way. It takes a withdrawal from whatever is overweight, trims spending after a bad year, sells to stay in a tax bracket.

Those policies are small programs, and their variety is open-ended, so a registry of rule kinds can't hold them. They need to be code the household writes, kept beside its data, with a few common ones built in. Running household code inside the engine must not cost what the simulator relies on:

- **Reproducibility** ([ADR-004](ADR-004-reproducibility-and-activation.md)): a run records exactly what produced it, and runs again the same way.
- **Integrity of the ledger**: lots, basis, tax character, refill and the negative-balance rule stay the engine's business.
- **Being explainable**: a person can see each decision and why it was made.

## Decision

### 1. A strategy is a class with hooks

A strategy is a Python class, a subclass of `strategy_api.Strategy` marked with `@register`. It names its `kind`, declares a frozen dataclass `Params`, and overrides the hooks it uses. Each hook reads a read-only `Context` of the run as of the current month and answers with a `Decision` (moves, a one-line note, and the numbers behind it) or `None`, "no opinion". The engine posts the moves through its own code. A strategy never touches engine state.

`strategies.csv` (ADR-003 §4.12) says which strategies a scenario runs: a name, a kind, what it owns (a chain, the scheduled rules carrying a tag, or both), optional `start`/`end` months, `enabled`, and `params_json`.

### 2. Where the hooks attach

| Hook | When it is asked | Stage |
| --- | --- | --- |
| `draw(ctx, chain, need)` | Every draw through a source chain the strategy owns: a rule's debit, a refill look-ahead, a calendar transfer, an `rmd` rule's chain | 1 |
| `place(ctx, chain, amount)` | Every sweep into a destination chain it owns: an overflow, a deposit through the chain | 1 |
| `year_start(ctx)` | In the strategy's first active month and each January after, once the month's calendar events and inflation indexes have moved and before any rule posts | 2 |
| `scale(ctx, rule, amount)` | Before each posting of a scheduled rule it owns, with the amount the rule would post | 2 |

Later stages add `month_start`, `on_breach` (#305's guard rails) and `year_end` (sells, gifts). The base class defines each hook with a do-nothing default, so a stage adds callers, never signatures.

A draw is decided from the month's state at the moment it is asked (Bob, 2026-10-21): the balances as they stand, not a forecast. That is a model a person can follow, and it is the model a household actually uses. Spending is decided once a year (`year_start` sets the year's course in the strategy's memory) and applied to each posting (`scale`), as a household sets a budget and then lives by it.

### 3. The API (`analysis.sim.strategy_api`)

This module is the only part of the simulator a strategy imports. Engine internals change behind it; `API_VERSION` (now 2, which added the spending hooks and context) changes only when a strategy written for the previous version could break. A module may set `API_VERSION` to the version it was written for, and one newer than the code is refused at load.

**Parameters.** `Params` fields are typed with units that follow the CSV rules: `Percent` (written `"5%"`, received as millionths), `Dollars` (`"1200.00"`, received as cents), `Tag` (an account tag `role:name` some account carries), `TargetSet` (a set in `targets.csv`); any other field is a plain JSON value. The loader converts and checks them against `Params`, so a strategy never parses a string, and a bad value is a load error naming the row.

**The context** (`Context`) shows the run up to the current month and nothing after it:

| Member | What it gives |
| --- | --- |
| `month`, `year` | The current month end |
| `lots` | Every asset and liability lot as a `LotView`: account, lot, kind, subkind, lot subkind, tax treatment, owner, the account's tags, symbol, balance, cost basis, minimum balance, `available` (balance less minimum), Roth qualified |
| `tagged(tag)` | The asset lots of the accounts carrying a tag |
| `allocation(lots)` | An `Allocation`: dollars and shares per asset class, and each lot's class weights, as of this month (§5) |
| `targets(set)` | A target set's shares in force this month |
| `memory` | The strategy's own dict, kept across calls; JSON values only |
| `end` | The scenario's last month: the plan's horizon, not future data |
| `cpi(since)` | The CPI factor from a past month end to now, in millionths |
| `people` | Each person's role, birthdate, age in whole years, whether alive, and the final month a lifespan gives |
| `budget()` | The strategy's own rules' spending over the twelve months from this one, at this month's prices: each rule's amount indexed to now, times the times it posts in them (within its start, end and person's lifespan, and the run's end). Twelve months rather than the calendar year, so a run that starts in May sees a whole year's plan; a run ending mid-year budgets only the months it has left |
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

Outside `start`/`end`, or when `enabled` is false, the chain behaves as if no strategy owned it, and owned rules post their budget with no baseline row.

#### 3.1 Errors and determinism

An exception in a strategy, a move it isn't allowed, or memory or detail that isn't JSON fails the run, with a message naming the strategy, its kind, the hook and the month. There is no silent fallback, which would hide a bug in a 400-member ensemble.

A strategy must be a pure function of its context, parameters and memory: no clock, files, network or unseeded random numbers. Python can't enforce that, so three things back it up: the views are read-only and the moves are checked; memory is serialized after every call, which catches state that can't be recorded; and `sim strategy check` runs a scenario twice, each time in a new Python process with a different string-hash seed (so set order and `hash()` vary as they would between ensemble workers), and compares every decision, reporting the first difference.

### 4. Loading, storage and provenance

**Where the code lives.** Built-in kinds are modules of `analysis.sim.strategies`. The household's are `strategies/*.py` at the data repo's root, shared by every scenario like `allocations.csv`; a module whose name starts with `_` is a helper the others import (`from ._helpers import x`) and registers nothing. A data-repo kind with a built-in's name is a load error: promote the module or rename it, never shadow.

**Importing.** The modules are imported from their source text as a synthetic package named after a hash of all of them (`_strategies_<12 hex>`), and relative imports between them work. Checking a load imports that package once. Each run imports a fresh copy under a name of its own and unloads it when it ends, so module-level state (a cache, a counter) never carries from one run or ensemble member to the next, and two versions of a module never meet. A module that fails to import, or asks for a newer API, is a load error carrying its traceback for a scenario with strategies, and a `sim check` warning for one without.

**The snapshot.** When a scenario's enabled strategies include a data-repo kind, the loader copies every module of `strategies/` into the intent's `strategy_source` (module, sha256, source): helpers are imported by name, so the snapshot is the whole folder. The engine imports from the snapshot, never from the data repo, so `run_scenario(intent, run)` stays self-contained, an ensemble worker needs no data directory, and a run file carries the exact code it ran. Built-in kinds store no source: the code version covers them.

**Checked at load** (ADR-003 §4.12, §4.13): a known kind; a chain or a `rules` tag, or both; a chain that exists, has steps (not an account's own leaf chain), and whose role fits a hook the kind implements; a `rules` tag some scheduled rule carries, only on `fixed_amount` rules, for a kind with a `scale` hook; one enabled strategy per chain and per rule, and no name (or target set, date and class) twice in one file; `start` no later than `end`; `params_json` a JSON object the kind's `Params` accepts; a destination chain without step `tax_character`/`basis_of` at any depth, which depend on the step order a strategy would override (#199); and every asset lot in a strategy's `Tag` portfolio classed by `allocations.csv` (§5). A disabled row is checked too, except for chain ownership.

**Stored.** The intent gains `strategy` (one row per enabled strategy: its chain, its rule tag, `params_json` in stored units), `strategy_source` and `allocation_target`; all three are snapshot tables, copied into run files. A run file adds `strategy_decision`, one row per hook that answered with a decision (a `None` writes nothing): what it was asked, what its own moves posted (`chosen_cents`), what the chain's steps then covered (`fallback_cents`), its note and detail, and its memory when it differs from the strategy's last row (so a change made while answering `None` shows on the next decision). Every transfer leg a move posted (a withdrawal's basis and gain legs alike) points at that row through `transfer_event.strategy_decision_id`, and keeps its own `reason_kind` (`rule`, `refill`, `overflow`), so income, expense and tax reports see the same flows as before. A `scale` row names its rule (`scheduled_transaction_id`). Each posting of an owned rule while its strategy is active adds a `spending_baseline` row: the rule, the strategy, the decision if `scale` gave one, and the **budgeted** and **delivered** cents. A skipped posting still has its row, with nothing delivered. Budgeted spending for any grouping is delivered spending plus what the owned rules in it left unspent.

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

### 7. Commands

`edit` is guided and names complete, as elsewhere (ADR-003 §7):

| Command | What it does |
| --- | --- |
| `sim strategy kinds [KIND]` | The kinds, built-in and data-repo, each with its docstring, hooks and parameters (unit, default, the field's docstring) |
| `sim strategy list [SCENARIO]` | The scenario's strategies, and the scenario each is inherited from |
| `sim strategy add [NAME]` | Asks for the kind, what it owns (a chain of a fitting role for a kind that draws or places, a rule tag for one that scales; blank for none when it does both) and each parameter not given as `--param NAME=VALUE`. `--chain` and `--rules` give them |
| `sim strategy edit [NAME]` | Guided with no options; options change just those fields. Editing an inherited strategy writes a whole-row override; `--disable` switches it off for this scenario and its descendants. The kind can't change |
| `sim strategy delete [NAME]` | Deletes the scenario's own row (restoring an inherited one it overrode); refuses an inherited one, pointing at `--disable` |
| `sim strategy log [SCENARIO\|RUNFILE]` | A run's decisions: month, strategy, hook, asked, chosen, chain steps, note (`--strategy`, `--year`); a `scale` decision's note starts with its rule |
| `sim strategy check [SCENARIO]` | Loads the scenario and runs it twice, comparing every decision |
| `sim target list\|set\|delete` | `targets.csv`; `set` takes `CLASS=PERCENT` like `sim symbol add`, and sets a class the scenario inherits but leaves out to 0% |
| `sim allocation --drift` | Each year-end of a run: the portfolio's share in each class less its target, in percentage points, and the largest. `--targets` and `--portfolio` default to the run's strategy's |
| `sim explain` | Adds each strategy's decision count and what it moved, against what it was asked |
| `sim report spending` | By year: budgeted, delivered, their difference and `of_plan` (the level, first), then `redirected` and `filled:GROUP` (the shape); below it the RMS of the yearly differences, `redirected` on average and at its worst, and the cosine similarity. `--by category\|account\|tag:ROLE` groups as `sim report expenses` does; CPI-constant dollars by default |
| `sim ensemble drift [RECIPE]` | Per year end across members: percentiles and the largest of each member's largest drift (or `--asset-class`'s, signed), and with a band the share of members past it then and at some month so far. `--targets` and `--portfolio` default as above; `--chart` draws the spread over every month |
| `sim compare --section strategies` | Each scenario's strategies and what they moved, then each run's largest drift by year end against one target set (a strategy's in any run, the baseline's first), so a strategy on and off sit side by side. Shown by default when any run has strategies or targets; the `differences` section then also lists differing strategies and target weights |

**Drift** (`sim/drift.py`) is a class's share less its target, in percentage points; above zero is overweight. A month with nothing held has no drift. Class dollars come from each asset lot's balance split by `class_weights` (§5), the same function the strategies' context uses, worked out once per stretch of months between two `allocations.csv` `as_of` dates. A drift view defaults to a strategy's `targets` and `portfolio`, and shades that strategy's `band` (its parameter, else its kind's default).

**Notebooks.** `accounts.ipynb` draws one run's drift, a line per class with the band and a tick for each month a strategy decided; `scenario_compare.ipynb` overlays the compared runs' drift (a panel per class and one of the largest) over what each scenario's strategies did; `ensemble.ipynb` draws the spread of drift across members (`drift_charts.py`, widget-free, so the CLI draws the same figures).

**Ensembles.** The series store keeps each member's month-end dollars per asset class, for every asset lot and for each account tag's asset lots (a strategy's portfolio is a tag), and the batch's target sets and strategies with their bands ([ADR-006](ADR-006-ensemble-scenarios.md) §8, format 3). Drift against any set is worked out at read time, as guard rails are, so a store needs no rebuild to show another target set.

**Spending against the plan** (`reports.spending_by_year`, `spending_report`). Delivered spending is what `sim report expenses` counts, and budgeted adds back what the strategies left unspent, from `spending_baseline`, for the owned rules that pay straight into an expense account (an owned rule into an asset or a chain moves money, so its shortfall isn't spending). The level is the headline (Bob, 2026-10-21); the shape is a diagnostic beside it, never alone, since a proportional cut has a perfect shape. **Redirected** is half the sum of the differences between each group's share of delivered and of budgeted spending (total variation distance): "10% of the dollars went somewhere other than the plan's mix", small groups counting fully. **Filled** is a group's delivered over its budget. Cosine similarity is shown too, labelled secondary: on spending mixes it sits near 1 and follows the largest groups (plan 60/30/10, delivered 70/20/10: cosine 0.983, redirected 10%).

**On vs off.** A strategy's effect is a comparison: a child scenario switches it off (`sim strategy edit NAME --disable` writes the override row), and `sim compare` or `scenario_compare.ipynb` set the two runs side by side, the net worth and break-even sections saying what it costs or gains.

Each edit goes through a staged edit, so a strategy the loader would reject is never written.

### 8. Later stages, and #305

Still to come in stage 2: `vary_*` on strategy parameters (ensembles and sensitivity), spending across an ensemble's members (real spending's spread and the CVaR of its worst years) and in the notebooks and `sim compare`. Stage 3 adds triggers, `ctx.history`, `ctx.tax` (with the filing status) and `year_end` actions with their own `reason_kind`.

Issue #305 owns guard rails, their measures (`sim/measures.py`) and their post-hoc evaluation; guard rails stay out of `scenario_tree_id`. A later stage adds a `trigger` column to `strategies.csv` naming a rail and its margin (`<rail>:annual` or `<rail>:cumulative`). Only a triggered rail enters the intent and is evaluated in the engine, through the same causal measures #305's reports use, with breach rows linked to the decisions that answered them. A rebalancing `draw` moves money from portfolio lots to checking, so #305's "net withdrawals from a tagged portfolio" counts it correctly.

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
