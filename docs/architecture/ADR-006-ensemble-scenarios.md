# ADR-006: Ensemble scenarios — parameter variation and Monte Carlo runs

- **Status:** Accepted
- **Date:** 2026-09-29
- **Supersedes:** —
- **Related:** [ADR-001](ADR-001-overall-architecture.md) (overall architecture), [ADR-002](ADR-002-storage-schema.md) (storage schema, `scenario.seed` — "reserved for ensembles; unused in v1"), [ADR-003](ADR-003-registries-and-csv-schemas.md) (registries, CSV layouts, calendar events §3, scenario hierarchy §4.5), [ADR-004](ADR-004-reproducibility-and-activation.md) (reproducibility, provenance).

## Context

Every scenario today is one deterministic prediction. We want to ask "what if this rate, or this date, or this one-off amount, is actually somewhere in a range" and see the *spread* of outcomes: generate many variants of one scenario, each with specific parameters redrawn from a distribution, run all of them, and combine the results (mean, median, standard deviation, range) rather than reading one number.

Two design questions turned out to matter more than the rest:

**Where does the perturbation happen?** Not against a run file. A run file's entire reason for existing is "the fixed, immutable result of simulating a fixed intent" — mutating one mid-lifecycle (even before simulation starts) makes "patch already-simulated ledger data" a reachable mistake later, not just a theoretical one, since a change to an early month's input would need every later month recomputed to stay consistent, which is exactly what running already does. **Perturbation happens once, at the intent stage, before any run exists.** `run_scenario`/`open_run_file` need no ensemble-awareness at all: an ensemble member is an ordinary intent file, run by the ordinary, completely unmodified `run_scenario`.

**What authors "this value should be drawn, not fixed"?** Not a new file. `calendar.csv` already has `set_rate`, `set_amount` and `one_off_transfer` events that patch exactly the columns we want to vary, at exactly the granularity we want (one row, one target). Extending `calendar.csv` with optional "instead of a literal, draw one" columns means variation authoring gets calendar reporting for free, and "vary" reduces to resolving those columns and writing ordinary calendar rows with concrete numbers — no new engine mechanism, no separate spec file to keep in sync with what it targets.

## Decision

### 1. Pipeline: load → vary → run

```text
recipe scenario's CSVs  --sim load-->  recipe intent (calendar rows with unresolved
                                        vary_* columns; not meant to be run as-is)
                              |
                              | sim run <recipe>  (auto-detects unresolved rows)
                              v
        for each member: copy the recipe intent, resolve every vary_* row by
        drawing a value and writing it back as an ordinary calendar_event row
                              |
                              v
              member intent (state/intent/<recipe>/member_0001.sqlite3, ...)
                              |
                              | run_scenario(member_intent, member_run)  -- UNCHANGED
                              v
                member run   (state/runs/<recipe>/member_0001.sqlite3, ...)
```

`sim load` is completely unchanged: a recipe's CSVs load into one intent exactly like any scenario's. `run_scenario`/`open_run_file` are completely unchanged: they've never heard of ensembles and don't need to. All new logic is the **vary** step in between, and it only ever writes ordinary, already-supported calendar rows.

### 2. `calendar.csv` gains `name` and optional `vary_*` columns

`calendar.csv` rows have no author-facing stable identity today (which also blocks calendar-event tagging — nothing keys on a specific row). Both gaps close with the same, small, backward-compatible addition:

| column | required | notes |
| --- | --- | --- |
| `name` | for a row with `vary_distribution` set; optional otherwise | stable identifier, unique within the scenario |
| `vary_target` | with `vary_distribution` | `amount` or `date` — which of *this row's own* columns is drawn instead of literal. Exactly one target per row — a `one_off_transfer` whose amount and date are both uncertain is two authoring passes (§5), not one row with two draws. |
| `vary_distribution` | to mark this row as varying | `uniform` or `truncated_normal`; blank means an ordinary literal row, unaffected by any of this |
| `vary_low`, `vary_high` | for `uniform` | bounds, in the target column's own *storage* unit: the row's own `amount` unit for `vary_target='amount'` (micros for `set_rate`, cents for `set_amount`/`one_off_transfer` — never decimal dollars, matching how that row's own `amount` cell is already written), an ISO date string for `vary_target='date'` |
| `vary_mean`, `vary_sd` | for `truncated_normal` | same storage-unit rule as `vary_low`/`vary_high`; `vary_target='amount'` only — `truncated_normal` is rejected with `vary_target='date'` (`vary_sd` is a numeric spread, not a date span) |
| `vary_clip_low`, `vary_clip_high` | optional, `truncated_normal` only | reject-and-redraw outside these bounds; omit either for one-sided or unbounded |
| `vary_ramp_months` | optional, `vary_target=amount` on a `kind=set_rate` row only | see §4 |

`vary_target='amount'` is only valid on a kind whose `amount` column means something to vary (`set_rate`, `set_amount`, `one_off_transfer`); `retire_scheduled` has no amount column, so only `vary_target='date'` is valid there. A distribution's own bound columns are the only ones it may set — `vary_clip_low` alongside `uniform`, or `vary_low` alongside `truncated_normal`, is rejected as a parameter the draw would silently ignore. Bounds must be orderable where that means something: `vary_low <= vary_high`, `vary_clip_low <= vary_clip_high`, `vary_sd > 0`.

A row's own `date`/`amount` column still holds a placeholder when it's the vary target (schema requires a value; nothing reads it). `_apply_calendar_event` gains one defensive check: firing a row that still has `vary_distribution` set is always a mistake — a recipe run directly, or a resolution step skipped — so it raises rather than silently using the placeholder. This check also runs once, up front, over every `calendar_event` row before the monthly loop starts, so a row dated outside the scenario's range or of a kind the engine doesn't otherwise apply can't skip the check by never being visited. This is the only engine change in this ADR.

### 3. Distributions

Two to start; the registry is closed but adding a third later (triangular, lognormal, ...) is additive, not a redesign:

- **`uniform(low, high)`** — equal probability across the range. The natural choice for a date range, per the motivating case.
- **`truncated_normal(mean, sd, clip_low=None, clip_high=None)`** — a normal draw, rejected and redrawn outside the clip bounds (`random.gauss` plus a bounds check, capped at a few thousand attempts before it's a clear runtime error rather than an infinite loop — no new dependency).

Skew/kurtosis controls and other families are explicitly out of scope for v1 (flagged as useful flexibility to keep in mind, not a present requirement).

Every draw uses an **independent RNG stream per (member, row name)**, derived by hashing `(ensemble seed, member index, row name)` through `hashlib.sha256` into a `random.Random` seed. One shared stream in call order would mean adding or removing one variation silently shifts every other variation's draws for every member; keyed streams don't.

**Two passes, in this order: every `vary_target=date` row first, then every `vary_target=amount`/rate row.** A ramp's reference point, or an amount meant to reflect "whatever the ramped rate is as of this (possibly also-varying) date," needs the date side already resolved before the amount side draws — resolving dates first makes that always true rather than order-dependent by accident.

### 4. Ramps: one authored row, several generated rows

A `kind=set_rate` row with `vary_ramp_months` set draws its **target** the same way any `amount` variation does, then **expands**: instead of writing one resolved `set_rate` event, the vary step writes `vary_ramp_months` of them, one per month, linearly interpolated from the series' current value (its latest aux row as of this event's own `date`) up to the drawn target, landing on it at `date + vary_ramp_months`. This is the same "expanding" shape ADR-003 §3 already uses for `life_transition` (one authored row, several synthesized effects) — not a new category of calendar event, and not a new way of reading `aux_table`: every generated row is an ordinary dated `set_rate`, looked up exactly like any other rate series (latest row on or before the month). The smoothing is entirely in what the generator writes, not in how anything reads.

This also resolves "future stock prices": the engine never simulates a price path day to day (`security_price` is consulted once, to derive an opening balance — [ADR-005](ADR-005-lots-and-per-lot-state.md) §6). Varying a lot's future price expectation is varying its growth-rate assumption, so `set_rate` (jump or ramp) covers it; no separate stock-price mechanism is needed. Modeling a sudden jump is rarely the realistic prediction, which is the whole reason the ramp exists.

### 5. Generation

A new module resolves one recipe intent into `member_count` member intents:

1. Copy the recipe's intent file to `state/intent/<recipe>/member_NNNN.sqlite3`.
2. Query that copy's own `calendar_event` rows for `vary_distribution IS NOT NULL`.
3. Pass 1 (dates): for each `vary_target='date'` row, draw on its own RNG stream (§3) and `UPDATE calendar_event SET date = ?, vary_distribution = NULL WHERE name = ?`.
4. Pass 2 (amounts/rates): for each `vary_target='amount'` row, draw a value (and, for a ramp, the target — §4); either `UPDATE ... SET amount = ?, vary_distribution = NULL` or, for a ramp, that plus `INSERT`ing the additional interpolated rows.
5. Record what happened (§6) in the same copy.
6. Hand the finished, ordinary intent file to `run_scenario`, unmodified, writing `state/runs/<recipe>/member_NNNN.sqlite3`.

Clearing `vary_distribution` as each row resolves means a finished member intent has no unresolved rows left — by the time `_apply_calendar_event` ever sees these rows, they're indistinguishable from any other authored calendar event.

### 6. Provenance per member

Two new tables, added to `SNAPSHOT_TABLES` like any other intent table, so they flow into the run file the same way everything else does — no special-casing anywhere in `open_run_file`:

```sql
CREATE TABLE ensemble_manifest (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    ensemble_name   TEXT NOT NULL,      -- the recipe scenario's name
    member_index    INTEGER NOT NULL,
    member_count    INTEGER NOT NULL,
    seed            INTEGER NOT NULL,   -- this member's derived seed
    recipe_tree_id  TEXT NOT NULL,      -- the recipe's own scenario_tree_id at generation time
    generated_at    TEXT NOT NULL
);

CREATE TABLE ensemble_draw (
    calendar_event_name TEXT NOT NULL,
    vary_target          TEXT NOT NULL,  -- 'amount' or 'date'
    drawn_value           TEXT NOT NULL, -- in the target column's own unit
    PRIMARY KEY (calendar_event_name)
);
```

Present (with a real row / rows) only in a member's own intent and run files; absent (empty) in the recipe's own intent, and in every ordinary, non-ensemble scenario. This is what makes a member self-documenting without diffing databases, and the raw material for later "does a higher draw correlate with a lower terminal net worth" diagnostics.

**Member intent files are kept, not discarded**, specifically so a failed member's exact input is there to inspect — the recipe scenario plus the concrete draws that produced it, with nothing to reconstruct from a log.

### 7. `sim run` generates the ensemble when a recipe has one

`sim run <recipe>` loads the scenario as usual. If the loaded intent has any unresolved `vary_distribution` row, it does not produce a single run — it determines `member_count` from `--ensemble-size N` if given, else `scenario.meta_json.ensemble_size` (an error if neither is set), and the ensemble's own seed from `scenario.seed` (reserved for exactly this since ADR-002 v1; an error if unset). It creates `state/intent/<recipe>/` and `state/runs/<recipe>/`, generates and runs each member (§5), and reports progress. **A member that fails is reported, not retried** — the summary names the failure count and each failed member's intent file path, so the specific input that failed is one open-file away.

If a scenario has no unresolved rows, `sim run` behaves exactly as it does today; no existing scenario's behavior changes.

### 8. Aggregation and reporting

No new engine or storage concept: `member_count` member run files are that many ordinary `RunConnection`s, and combining them is the same `items(...)` DataFrames already used everywhere, concatenated and grouped by month —

```python
frames = [rc.items("net_worth").assign(member=i) for i, rc in enumerate(members)]
by_month = pd.concat(frames).groupby("month")["net_worth"].agg(["mean", "median", "std", "min", "max"])
```

— the same shape of aggregation `scenario_compare.ipynb` already does across a handful of named variants, just over many random ones. A new `notebooks/ensemble.ipynb` plots the standard Monte Carlo fan (percentile bands over time); `sim ensemble summary <recipe>` is the terminal equivalent from day one, printing the same table plus the failure count from §7, so this doesn't join the CLI-gap backlog in `TODO.md`.

## Amendments applied alongside this ADR

- [ADR-002](ADR-002-storage-schema.md): `calendar_event` gains `name` and the `vary_*` columns; new intent-layer tables `ensemble_manifest`, `ensemble_draw` (added to `SNAPSHOT_TABLES`); `SCHEMA_VERSION` bump; `schema_overview` rows.
- [ADR-003](ADR-003-registries-and-csv-schemas.md): §4.8 `calendar.csv` gains `name` and the `vary_*` columns (§2); §3 notes that a row with `vary_distribution` set is resolved before it can fire, and §3.1 documents the ramp-as-expansion behavior (§4).
- [ADR-004](ADR-004-reproducibility-and-activation.md): a member's provenance is the recipe's `scenario_tree_id` (unchanged meaning, copied via the intent copy) plus `ensemble_manifest` for which member and what it drew — not a new parallel determinism mechanism.

## Consequences

- Positive: `run_scenario`/`open_run_file` need no ensemble-awareness at all; a member is an ordinary intent-then-run pair readable by every existing tool; variation authoring reuses calendar reporting instead of inventing a parallel, invisible mechanism; a failed member's exact input is a file on disk, not something to reconstruct.
- Negative: `calendar.csv` needs the schema addition before any of this works; an ensemble's disk footprint is `member_count` full intent files *and* `member_count` full run files (neither a diff); wall-clock time for `sim run` on a recipe scales linearly with `member_count`.
- Not attempted: nested ensembles (a recipe whose parent is itself a recipe); varying a target that isn't expressible via `set_rate`/`set_amount`/`one_off_transfer` (an opening balance, say) — add a case when a real need appears, per this project's own registries-grow-one-line-at-a-time convention.

## Open questions deferred

- Additional distribution families (triangular, lognormal, skew/kurtosis controls) — useful flexibility, not a present requirement.
- Correlated draws across variations — today every variation is independent; no rationale identified yet for coupling two.
- A variation whose value should be computed from *another* variation's resolved draw (beyond the date-before-amount ordering in §3) — not needed yet; the two-pass rule covers every case raised so far.
