# ADR-009: Pooled ensembles — one result set drawn from several ensembles

- **Status:** Accepted
- **Date:** 2026-10-28
- **Supersedes:** —
- **Amended:** see [Amendments](#amendments) at the end.
- **Related:** [ADR-006](ADR-006-ensemble-scenarios.md) (ensembles: keyed draws, batches, member 0, pairing, the series store and the draws database), [ADR-004](ADR-004-reproducibility-and-activation.md) §11-12 (what can't have changed; the background builder), [ADR-007](ADR-007-sensitivity-analysis.md) (sensitivity), [ADR-003](ADR-003-registries-and-csv-schemas.md) §4.5 (`scenario.csv`), issues [#292](https://github.com/BobKerns/analysis/issues/292) (this design; decisions recorded there) and [#340](https://github.com/BobKerns/analysis/issues/340) (a compound-episode template and the history weights).

## Context

The templates model each kind of trouble (a Great Inflation-style run, a financial crisis followed by years of near-zero rates, a short price spike) as its own ensemble built on a common one, so each can be looked at, and compared with the trouble-free ensemble, on its own. The question a household asks is different: across all the worlds it might be in, with each kind of trouble as likely as history suggests, how bad are the bad futures? That is a mixture: some members from each ensemble, in proportion to how likely that world is.

The members already exist as files. Simulating them again would cost the whole batch; taking them as they are costs nothing. What needs deciding is which members to take, so the mixture is a fair sample, keeps the pairing that makes ensembles comparable (ADR-006 §8), and keeps one deterministic member 0 (ADR-006 §9).

## Decision

### 1. A pool is a scenario that names its components

A **pool** is a scenario folder holding only `scenario.csv`, `README.md` and, optionally, `guard_rails.csv` (dotfiles, such as `.disable`, aside). Its `scenario.csv` has no parent, dates or seed; its `meta_json` has a `pool` key naming the component ensembles and their weights (`lineage.is_pool`):

```json
{"pool": {"components": {"e_great_inflation": "15%", "e_financial_crisis": "30%",
                         "e_pandemic_spike": "55%"},
          "base": "e_great_inflation"},
 "ensemble_size": 240, "reserve": 250000}
```

- **`components`** is an object of name to weight, each a percent with its `%` above 0, adding up to exactly 100%, as `vary_outcomes` does (ADR-006 §3); or a list of names for equal shares. At least two, none named twice, never the pool itself.
- **`base`** names the component whose member 0 is the pool's (§4); default the first.
- **`ensemble_size`** is the pool's member count; default the smallest component's (§2 says why it can't be larger). **`reserve`** is the reports' reserve (ADR-006 §8), in place of the base component's. No other `meta_json` key is read, so another is an error.

A pool has no lineage and nothing to load. The loader refuses it, with a pointer to `sim run`, and refuses a scenario that names a pool as a parent or a mix-in (`loader._refuse_pools`). Its checks (`pool.pool_issues`, run by `sim check` and by every edit, §8) are the definition above and, for each component: it exists, it is an ensemble (ADR-006 §7) with a seed and not a pool or mix-in only, and its lineage has no cycle; all components share one start and one end month, so the pooled months line up; and every component's size is at least the pool's.

`sim ensemble pool create NAME COMPONENT[=PCT]... [--size N] [--base C] [--description D]` writes one (with a README naming the components and their shares, a line that follows later edits and renames), checks it, and makes it current; `sim ensemble pool edit` with no options shows its fields (components, base, size, reserve, description) and asks which to change, and otherwise writes what it is given, `''` restoring a default (for the description, the generated `Pooled from a + b`, which also follows a change of components, in the README too). Replacing the components drops a base that is no longer among them, so the first is the base. Listings show a pool's parents as `pool of a 15%, b 30%, ...`, its span from its components, its seed as theirs when they share one (else `mixed`), and its size.

### 2. Pool member *j* is member *j* of one component

The pool's members are **index-preserving**: member *j* of the pool is member *j* of one component, the same file. Member 0 comes from the base (§4); each later member comes from the component furthest behind its share of the members so far, by the integer deficit `weight × (j + 1) − count × total`, ties to the earlier component (`pool.assign_members`). Every component's count stays within one member of its share at every point, so any run of consecutive members is close to the weights, and each component gets its share rounded to whole members: fixed counts, not a random component per member (stratified sampling, which takes the spread between components out of the sampling noise).

Taking members 1 to *n* of each component instead would repeat futures. Components built on one ensemble share its seed (ADR-006 §7), and draws are keyed by (seed, member, row name) (ADR-006 §3), so member *k* of each draws the same value for every row they share: market levels, lifespans, every year's shocks. On the prototype's `retire_alice` extended to 2060, member *k*'s CPI-constant net worth correlated 0.71 to 0.89 across three episode components, so 80 same-index members from each were worth about 1.1 to 1.2 × 80 independent ones: the variance of the pooled mean was 2.3 to 2.6 times as large, of `p05` 1.1 to 1.4 times, of `cvar05` 1.0 to 1.1 times. Index-preserving members repeat nothing.

They also keep the pairing. Member *j* of the pool and member *j* of the common parent drew the same values for every row they share, so `sim ensemble compare e0 pool` gives per-future differences and `P(ahead)` with no new code (ADR-006 §8). Components with different seeds still pool, but the pool then pairs with nothing.

The cost: some of the pool's high indices fall in each component, so every component needs at least the pool's size. Three components of 240 make a pool of up to 240 from 720 simulated members. When the components are run anyway, to look at each, that costs nothing more.

### 3. A weight is a probability

A weight is the probability of that world, given that the household is in one of the pool's worlds. A component's own probability that its trouble starts inside the horizon is a second one, built into the component (a start marker uniform over decades leaves some members with no episode at all), so a member's chance of its episode is the product of the two. The weights move the pooled tail more than anything else in a pool: on the prototype, equal weights gave a pooled `cvar05` of $216k against $506k at 15/30/55, since nearly all the worst futures come from the Great Inflation component. The pooled tail is not an average of the components' tails (the worst futures of the worst component all land in the pool's tail), which is the point of pooling, and why the weight is the number to decide on. Trying other weights is cheap: it links the files again and rebuilds the stores in seconds (§5). `docs/guides/ensembles.md` sets out what history says about them.

### 4. Member 0 and the seed

Member 0 is the base component's member 0, so the pool, like every ensemble, has exactly one deterministic member (ADR-006 §9), and everything that opens one run per scenario (`scenario_run_path`, `sim networth`, the notebook pickers) works on a pool. The other components' member 0s are left out: they would put several deterministic members into the pooled statistics. The pool's seed is its components' when they share one, recorded with the batch (§5); none when they differ.

### 5. Building the batch

`sim run POOL` (`cli._run_pool`) checks the pool (§1), then brings each component's latest Monte Carlo batch up to date: one that couldn't have changed (`freshness.unchanged_run`, ADR-004 §11) is reused, any other is run, and `--rebuild` runs them all (it is refused on anything but a pool, and `--sensitivity` on a pool). Like a load, it refuses dirty data (the pool's folder or any component's lineage, ADR-004 §3) without `--allow-dirty-data`. A component's latest result must then be a Monte Carlo batch (`pool.latest_pool_sources`): one whose varying rows were removed runs as a single run, and the older batch behind it is stale, so it is refused rather than pooled. It then makes the pool's batch, `state/runs/<pool>/<timestamp>/` (`pool.build_pool_batch`):

- Each `member_NNNN.sqlite3` is **hard-linked** from its component's batch, copied where a link can't go (another filesystem). A finished member file never changes, so a link reads exactly as a copy, costs no space (a member is about 2 MB), and outlives retention pruning the component's batch (ADR-004 §10). So are the member intent files, from `state/intent/<component>/<batch>/`. A component batch with fewer members than the pool fails the build, naming it.
- `batch_manifest.json` has `mode` `monte_carlo`, the member count, each member's status as its component's batch recorded it (a failed member stays failed, its error kept, and the reports count it), and a `pool` record: the definition (components and weights, base, size, reserve), the size built, the data repo's tree, each component's weight, batch, seed and `scenario_tree_id`, its members, and `member_components`, the component of each member by index.
- The draws database (§7) and the series store (ADR-006 §8) are built from the linked files, as for any batch.

**What can't have changed** (`freshness.unchanged_pool`, ADR-004 §11): the pool's definition is the one recorded, at the size `sim all` would build; the data repo is clean, with the tree recorded; and each component is unchanged by §11 itself, with the recorded batch still its latest. `sim all` builds pools after everything else, reusing a component it built or found unchanged in the same pass and failing a pool at once over a component that failed there, and the background builder (ADR-004 §12) queues pools after ensembles. A pool's data scope (`provenance.data_stamps`, ADR-004) is its own folder and every component's lineage, so a component's edit changes the pool's tree and a dirty component makes the pool dirty: the builder waits on it, and a pool that failed is tried again once the component changes.

### 6. Reports

A pool's batch is an ordinary batch of members, so `sim ensemble summary|compare`, their `--graph`, `sim compare` (member 0) and the notebook widgets read it as they read any ensemble. Two additions:

- **The summary** prints, under its header, the pool's members by component with their weights and shared seed (`pool of 3, seed 7: e_great_inflation 36 (15%), ...`), and which components the worst 5% of members came from, ranked by each member's lowest CPI-constant net worth over the run (after-tax unless `--pre-tax`; net worth even with `--account`, since an account only some components have is 0 in the rest), the worst point of a lifetime rather than where it ends (`worst 5% (12) by lowest CPI-constant net worth: 6 e_pandemic_spike, 5 e_great_inflation, ...`). The pooled tail means rest on those members, so a small count from a component is a reason for more members. There is no other breakdown by component: each component's own report is one command away.
- **The reserve** is the pool's own, when it sets one, else its base component's (`series_db._pool_meta`).
- **Guard rails** (ADR-003 §4.11) are read along the base component's lineage and then the pool's own `guard_rails.csv` (`lineage.reading_order`), so a pool inherits its base component's rails by name and can add or override them. A single-run report on a pool's member (`sim report guard-rails`, `sim explain`) judges it by the pool's rails (`guard_rails.run_scenario_name`), and the pool's check (§1) checks them against the names each component's lineage defines, since each member's run has only its own component's accounts, tags and calendar rows: a rail naming something one component lacks is refused rather than failing on that component's members. Completing a pool's rail names follows the same order; its other names (accounts, lots, tags, calendar rows) are every component's (`names._lineage_dirs`), as its members hold any of them.

An account or lot only some components have is 0 in the members without it, as a scenario comparison treats an account one side lacks (#174), so the account choices are every component's. Comparing a pool with one of its own components pairs each member that came from that component with itself, a difference of exactly 0, which `P(ahead)` counts as not ahead: correct, and easy to misread.

### 7. The draws database, and sensitivity

The draws database (ADR-006 §6) is kept per component. `member` gains `component`, the ensemble each member was drawn from (the batch's own name outside a pool), and `node`, `node_pair` and `draw_check` are keyed by component and node, since one name can carry different distributions in two components. Each component's nodes are described from its member 0 when that is in the batch, else its lowest-indexed member, and its checks run over its own drawn members: they are a fair sample of the component, so the checks stay valid. `batch.ensemble_name` is the pool, `master_seed` the shared seed and `recipe_tree_id` NULL. `draws_wide` has the component beside the member and a column per node any component draws, NULL where its component lacks one. `sim ensemble draws` groups its checks and pairs under each component's name, and `--node` lists a node's draws per component; the notebook's Draws section has a Component dropdown, shown only for a pool (`draws_db.component_frames`). A database gathered before this layout is gathered again when read (`draws_db.draws_db_current`).

**Sensitivity** (ADR-007) is refused on a pool, with a pointer to its components: a node only one component draws would be correlated with outcomes over that component's members alone, confounded with which world the member is in. Run on each component, it answers the question properly.

### 8. Editing around pools

- **Rename.** Renaming a component rewrites every pool that names it (the key or list entry, keeping its place and weight, and `base`), and a description still as `pool create` made it (`Pooled from a + b`) follows, in `scenario.csv` and the README. A pool renames, copies, disables and deletes like any scenario.
- **Delete.** Deleting a component is refused while a pool names it, as deleting a parent is (ADR-003 §4.5).
- **Edits are checked through pools.** A staged edit (ADR-003 §7) checks a pool with `pool_issues` instead of loading it, both as the edited scenario and as one affected by the edit: a pool is affected when it or any component's lineage is touched, so an edit that leaves a component no longer an ensemble, or on another span, is refused naming the pool. `sim scenario|ensemble edit` on a pool points at `sim ensemble pool edit`, and pools are never offered as parents.

## Consequences

- Positive: a mixture of worlds costs no simulation beyond the components themselves; every report, chart and notebook works on it unchanged; it pairs with its components' parent; trying other weights takes seconds.
- Negative: every component needs at least the pool's member count, so components that exist only to feed a pool cost their count times the pool's size in simulation; a pool's members can't be reweighted (importance sampling) without weighted statistics in every report.
- Limits:
  - **One component, one world.** A member has only the episodes its component carries, and history often has two or more in 30 years (`docs/guides/ensembles.md`), so a pool of single-episode components has a probably optimistic deepest tail. A component built from two templates, the second dated from the first, adds a compound world without code: `t_crisis_then_spike` ([#340](https://github.com/BobKerns/analysis/issues/340)) is one.
  - **A pool of plans assumes the plan is fixed in advance.** Weighting alternative plans answers "what's my risk if I don't yet know which plan I'll follow", but only when the choice doesn't depend on how the future goes; a choice that responds to the path is a responsive strategy ([#326](https://github.com/BobKerns/analysis/issues/326)), not a mixture. To choose between plans, compare them; a pool hides the choice.
  - No pools of pools, and no sensitivity batch as a component.

## Open questions deferred

- Weighted members (importance sampling, oversampling the worst component), if more members ever isn't enough.
- Weights that vary by member, such as a probability of trouble that depends on the year.

## Amendments

Each entry changed this ADR's design. The sections it names were rewritten in place to describe the current design; git history has the earlier text.

- [#340](https://github.com/BobKerns/analysis/issues/340): a compound episode template exists, so pool members can meet two episodes (Consequences, Limits).
