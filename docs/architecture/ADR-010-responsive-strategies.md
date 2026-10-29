# ADR-010: Responsive strategies

- **Status:** Accepted
- **Date:** 2026-10-28
- **Supersedes:** —
- **Amended:** see [Amendments](#amendments) at the end.
- **Related:** [ADR-003](ADR-003-registries-and-csv-schemas.md) §4.1b (`allocations.csv`), §4.7 (chains), §4.12–§4.13 (`strategies.csv`, `targets.csv`); [ADR-002](ADR-002-storage-schema.md) (`strategy`, `strategy_source`, `allocation_target`, `strategy_decision`); [ADR-004](ADR-004-reproducibility-and-activation.md) §2 (strategy code is data); issues [#326](https://github.com/BobKerns/analysis/issues/326) (the design, in stages) and [#339](https://github.com/BobKerns/analysis/issues/339) (stage 1a), [#305](https://github.com/BobKerns/analysis/issues/305) (guard rails, which later stages trigger on).

## Context

Every decision in a scenario is fixed when it is written. A chain draws its steps in file order whatever the portfolio looks like; a rule spends the same amount whatever happened last year. A household doesn't behave that way. It takes a withdrawal from whatever is overweight, trims spending after a bad year, sells to stay in a tax bracket.

Those policies are small programs, and their variety is open-ended, so a registry of rule kinds can't hold them. They need to be code the household writes, kept beside its data, with a few common ones built in. Running household code inside the engine must not cost what the simulator relies on:

- **Reproducibility** ([ADR-004](ADR-004-reproducibility-and-activation.md)): a run records exactly what produced it, and runs again the same way.
- **Integrity of the ledger**: lots, basis, tax character, refill and the negative-balance rule stay the engine's business.
- **Being explainable**: a person can see each decision and why it was made.

## Decision

### 1. A strategy is a class with hooks

A strategy is a Python class, a subclass of `strategy_api.Strategy` marked with `@register`. It names its `kind`, declares a frozen dataclass `Params`, and overrides the hooks it uses. Each hook reads a read-only `Context` of the run as of the current month and answers with a `Decision` (moves, a one-line note, and the numbers behind it) or `None`, "no opinion". The engine posts the moves through its own code. A strategy never touches engine state.

`strategies.csv` (ADR-003 §4.12) says which strategies a scenario runs: a name, a kind, the chain it owns, optional `start`/`end` months, `enabled`, and `params_json`.

### 2. Where the hooks attach

| Hook | When it is asked | Stage |
| --- | --- | --- |
| `draw(ctx, chain, need)` | Every draw through a source chain the strategy owns: a rule's debit, a refill look-ahead, a calendar transfer, an `rmd` rule's chain | 1 |
| `place(ctx, chain, amount)` | Every sweep into a destination chain it owns: an overflow, a deposit through the chain | 1 |

Later stages add `year_start`, `scale` (spending), `month_start`, `on_breach` (#305's guard rails) and `year_end` (sells, gifts). The base class will define each with a do-nothing default, so a stage adds callers, never signatures.

A draw is decided from the month's state at the moment it is asked (Bob, 2026-10-21): the balances as they stand, not a forecast. That is a model a person can follow, and it is the model a household actually uses.

### 3. The API (`analysis.sim.strategy_api`)

This module is the only part of the simulator a strategy imports. Engine internals change behind it; `API_VERSION` (now 1) changes only when a strategy written for the previous version could break. A module may set `API_VERSION` to the version it was written for, and one newer than the code is refused at load.

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

Views are frozen dataclasses and read-only mappings. Future series values, future calendar rows and other ensemble members are not reachable.

`ChainView` gives the owned chain's name, role, steps (each with its source, `maximum` and `percent`) and leaves: the lots its steps reach, nested chains included, in step order. That order is the household's own preference, and a natural tie-breaker.

**What the engine does with a decision.** For `draw`:

1. Each move must be a `Take` naming a leaf of the owned chain with a positive whole number of cents, and the takes may not add up to more than the need. Anything else fails the run (§3.1).
2. Each take posts through the same leaf path the chain's steps use: the leaf's own refill look-ahead and minimum balance, Roth basis and the five-year rule, a taxable lot's basis and gain legs. A take bigger than the leaf can spare posts what it can.
3. Whatever is still outstanding falls through to the chain's own steps, as if the strategy hadn't been asked. A strategy can't leave a debit unpaid by mistake.

`place` mirrors this with `Put`, each destination's maximum balance applying as it does for a step. Step caps (`maximum`, `percent`) bind the chain's own steps, not the strategy's moves; a strategy that wants to honor them reads them from `ChainView`.

Outside `start`/`end`, or when `enabled` is false, the chain behaves as if no strategy owned it.

#### 3.1 Errors and determinism

An exception in a strategy, a move it isn't allowed, or memory or detail that isn't JSON fails the run, with a message naming the strategy, its kind, the hook and the month. There is no silent fallback, which would hide a bug in a 400-member ensemble.

A strategy must be a pure function of its context, parameters and memory: no clock, files, network or unseeded random numbers. Python can't enforce that, so three things back it up: the views are read-only and the moves are checked; memory is serialized after every call, which catches state that can't be recorded; and `sim strategy check` runs a scenario twice, each time in a new Python process with a different string-hash seed (so set order and `hash()` vary as they would between ensemble workers), and compares every decision, reporting the first difference.

### 4. Loading, storage and provenance

**Where the code lives.** Built-in kinds are modules of `analysis.sim.strategies`. The household's are `strategies/*.py` at the data repo's root, shared by every scenario like `allocations.csv`; a module whose name starts with `_` is a helper the others import (`from ._helpers import x`) and registers nothing. A data-repo kind with a built-in's name is a load error: promote the module or rename it, never shadow.

**Importing.** The modules are imported from their source text as a synthetic package named after a hash of all of them (`_strategies_<12 hex>`), and relative imports between them work. Checking a load imports that package once. Each run imports a fresh copy under a name of its own and unloads it when it ends, so module-level state (a cache, a counter) never carries from one run or ensemble member to the next, and two versions of a module never meet. A module that fails to import, or asks for a newer API, is a load error carrying its traceback for a scenario with strategies, and a `sim check` warning for one without.

**The snapshot.** When a scenario's enabled strategies include a data-repo kind, the loader copies every module of `strategies/` into the intent's `strategy_source` (module, sha256, source): helpers are imported by name, so the snapshot is the whole folder. The engine imports from the snapshot, never from the data repo, so `run_scenario(intent, run)` stays self-contained, an ensemble worker needs no data directory, and a run file carries the exact code it ran. Built-in kinds store no source: the code version covers them.

**Checked at load** (ADR-003 §4.12, §4.13): a known kind; a chain that exists, has steps (not an account's own leaf chain), and whose role fits a hook the kind implements; one enabled strategy per chain, and no name (or target set, date and class) twice in one file; `start` no later than `end`; `params_json` a JSON object the kind's `Params` accepts; a destination chain without step `tax_character`/`basis_of` at any depth, which depend on the step order a strategy would override (#199); and every asset lot in a strategy's `Tag` portfolio classed by `allocations.csv` (§5). A disabled row is checked too, except for chain ownership.

**Stored.** The intent gains `strategy` (one row per enabled strategy, `params_json` in stored units), `strategy_source` and `allocation_target`; all three are snapshot tables, copied into run files. A run file adds `strategy_decision`, one row per hook that answered with a decision (a `None` writes nothing): what it was asked, what its own moves posted (`chosen_cents`), what the chain's steps then covered (`fallback_cents`), its note and detail, and its memory when it differs from the strategy's last row (so a change made while answering `None` shows on the next decision). Every transfer leg a move posted (a withdrawal's basis and gain legs alike) points at that row through `transfer_event.strategy_decision_id`, and keeps its own `reason_kind` (`rule`, `refill`, `overflow`), so income, expense and tax reports see the same flows as before.

**Provenance** ([ADR-004](ADR-004-reproducibility-and-activation.md) §2). `strategies/` is inside the data scope, so editing a strategy changes `scenario_tree_id` and the data stamp like any other data edit, and `sim all` and background builds rebuild what it affects.

### 5. Allocation

`ctx.allocation`, `sim allocation` and the load-time portfolio check class a lot the same way, through one function (`asset_classes.class_weights`), so they can't disagree:

1. The lot's symbol's `allocations.csv` set in force: the latest `as_of` on or before the month, else the earliest.
2. Else the lot's `<account>:<lot>` key in `allocations.csv`: a lot with no symbol (one that grows at a rate series) is classed by a row whose `symbol` is, say, `Asset_IRA_Alice:stable` (Bob, 2026-10-21). A ticker has no colon, so the two can't collide. Price fetching, symbol completion and symbol listings skip these keys, and renaming the account or the lot rewrites them (keeping the old key too while a scenario the rename doesn't reach still has the old name, since the file is shared).
3. Else the class the symbol means (`CASH`, `CD`) or the account subkind's (cash, real estate, ...).
4. Else `unclassified`.

A lot in a strategy's portfolio that ends up `unclassified` is a load error naming the row to add (Bob: "resolve by adding to allocations.csv"), since it would skew every share.

**Targets.** `targets.csv` (ADR-003 §4.13) holds named sets of class weights, each dated by `as_of` like `allocations.csv`'s, so a glide path is more rows. The set in force is the latest on or before the month. A class a set leaves out has target 0%: holding it counts as overweight.

### 6. The built-in `rebalance`

Parameters: `portfolio` (a tag), `targets` (a target set), `band` (default 5%) and `step` (default 1%).

- While every class of the portfolio is within `band` of its target, it answers `None`: the chain's own order, the household's tax-driven preference, decides. This is tolerance-band rebalancing as practiced.
- Beyond the band, the amount moves in slices of `step` (at least $1). Each slice comes from (for `draw`) or goes to (for `place`) the leaf that most reduces the squared distance between the portfolio's class dollars and the targets' share of the portfolio after the whole amount has moved. Ties go to the leaf earlier in the chain. A blended fund moves several classes at once, which the distance accounts for. A leaf outside the portfolio moves no class, so it is used only when no portfolio lot helps.
- The note reads like `bond 53.6% vs 40.0% target (band 5%): took $961 from Asset_TraditionalIRA_Alice:default`; `detail` holds the shares before and after and the targets.

Greedy slicing is easy to explain ("each slice came from whatever helped most"), deterministic, and within one slice of the exact optimum; a solver would be harder to explain and add a dependency.

### 7. Commands

`edit` is guided and names complete, as elsewhere (ADR-003 §7):

| Command | What it does |
| --- | --- |
| `sim strategy kinds [KIND]` | The kinds, built-in and data-repo, each with its docstring, hooks and parameters (unit, default, the field's docstring) |
| `sim strategy list [SCENARIO]` | The scenario's strategies, and the scenario each is inherited from |
| `sim strategy add [NAME]` | Asks for the kind, the chain (of a fitting role) and each parameter not given as `--param NAME=VALUE` |
| `sim strategy edit [NAME]` | Guided with no options; options change just those fields. Editing an inherited strategy writes a whole-row override; `--disable` switches it off for this scenario and its descendants. The kind can't change |
| `sim strategy delete [NAME]` | Deletes the scenario's own row (restoring an inherited one it overrode); refuses an inherited one, pointing at `--disable` |
| `sim strategy log [SCENARIO\|RUNFILE]` | A run's decisions: month, strategy, hook, asked, chosen, chain steps, note (`--strategy`, `--year`) |
| `sim strategy check [SCENARIO]` | Loads the scenario and runs it twice, comparing every decision |
| `sim target list\|set\|delete` | `targets.csv`; `set` takes `CLASS=PERCENT` like `sim symbol add`, and sets a class the scenario inherits but leaves out to 0% |
| `sim allocation --drift` | Each year-end of a run: the portfolio's share in each class less its target, in percentage points, and the largest. `--targets` and `--portfolio` default to the run's strategy's |
| `sim explain` | Adds each strategy's decision count and what it moved, against what it was asked |

Each edit goes through a staged edit, so a strategy the loader would reject is never written.

### 8. Later stages, and #305

Stage 1b adds a drift chart to the notebooks and class shares to the ensemble series store. Stage 2 adds spending (`year_start`, `scale` on rows targeted by tag, baselines recorded so reports can compare spending against the budgeted baseline) and `vary_*` on strategy parameters. Stage 3 adds triggers and `year_end` actions with their own `reason_kind`.

Issue #305 owns guard rails, their measures (`sim/measures.py`) and their post-hoc evaluation; guard rails stay out of `scenario_tree_id`. A later stage adds a `trigger` column to `strategies.csv` naming a rail and its margin (`<rail>:annual` or `<rail>:cumulative`). Only a triggered rail enters the intent and is evaluated in the engine, through the same causal measures #305's reports use, with breach rows linked to the decisions that answered them. A rebalancing `draw` moves money from portfolio lots to checking, so #305's "net withdrawals from a tagged portfolio" counts it correctly.

## Consequences

### Positive

- A household's policy is code it can read, test and change, kept beside its data, and a run records the exact code it ran.
- The engine keeps the ledger: a strategy chooses among moves the chain already allows, and its moves post through the same paths as a step's.
- Every decision is a row with a note, and every leg it caused points at it, so "why did the IRA pay for this" has an answer.
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
