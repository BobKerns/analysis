# [ADR-006](ADR-006-ensemble-scenarios.md) implementation plan: ensemble scenarios

**Status: implemented.** All five phases below have shipped: `calendar.csv` variation/validation (Phase 0, `src/analysis/sim/loader.py`, `src/analysis/sim/engine.py`), draws (Phase 1, `src/analysis/sim/distributions.py`), member resolution (Phase 2, `src/analysis/sim/ensemble.py`'s `resolve_member`), `sim run <recipe> --ensemble-size N`'s ensemble-generation path plus `sim ensemble create`/`sim calendar add` (Phase 3, `src/analysis/sim/cli.py`), and aggregation/reporting via `sim ensemble summary` and `notebooks/ensemble.ipynb` (Phase 4, `src/analysis/sim/ensemble.py`, `src/analysis/sim/notebook_ui/ensemble_widgets.py`). The phase breakdown below is kept as a record of the design sequencing, not a pending TODO list.

**Later refinement (post-ship):** Phase 0/1's separate `vary_clip_low`/`vary_clip_high` columns were merged into `vary_low`/`vary_high` (now shared with `uniform`), with any of `vary_mean`/`vary_sd`/`vary_low`/`vary_high` inferable from the other three (`distributions.resolve_truncated_normal_bounds`) -- `truncated_normal` is never actually unbounded. See [ADR-006](ADR-006-ensemble-scenarios.md) §2/§3 for the current column set; mentions of `vary_clip_low`/`vary_clip_high` below are historical.

Companion to [ADR-006](ADR-006-ensemble-scenarios.md). Phases are ordered so each ends with a green test suite and a shippable state. `run_scenario`/`open_run_file` are never modified by this plan — every phase after Phase 0 adds code *around* them, not inside them.

## Phase 0 — `calendar.csv` gains `name` and the `vary_*` columns

1. `schema.py`: add `name TEXT` and the `vary_*` columns (`vary_target`, `vary_distribution`, `vary_low`, `vary_high`, `vary_mean`, `vary_sd`, `vary_clip_low`, `vary_clip_high`, `vary_ramp_months`) to `calendar_event` — all nullable; existing data is unaffected. `SCHEMA_VERSION` bump.
2. `registries.py`: extend `CALENDAR_HEADER`; `ENSEMBLE_DISTRIBUTIONS = {"uniform", "truncated_normal"}`; `VARY_TARGETS = {"amount", "date"}`.
3. Loader (`_validate_calendar`): when `vary_distribution` is set, `vary_target` is required and in `VARY_TARGETS`; the columns required by that distribution are present and well-formed (mirrors how `_validate_scheduled`/`_validate_calendar` already check per-`kind` requirements); `vary_ramp_months` is only allowed with `vary_target='amount'` and `kind='set_rate'`; `name` is required whenever `vary_distribution` is set (needed to target the row later) and unique within the scenario.
4. `engine.py` (`_apply_calendar_event`): one defensive check — a row with `vary_distribution` still set raises `EngineError` naming the row, instead of firing its placeholder value. This is the only change inside the existing engine/run path in this whole plan.
5. Tests: every malformed `vary_*` combination is a clear load error; an ordinary (non-varying) `calendar.csv` round-trips exactly as before; a recipe intent run directly (skipping "vary") raises the new `EngineError` instead of silently using a placeholder.

Done. Exit: suite green; no behavior change for any scenario without `vary_*` columns filled in.

## Phase 1 — Distributions and draws (pure functions, no I/O)

1. New `src/analysis/sim/distributions.py`: `Uniform(low, high)`, `TruncatedNormal(mean, sd, clip_low=None, clip_high=None)`, each with `.draw(rng: random.Random)`. Rejection sampling for the truncated case, capped attempts (a clear error past the cap, not a silent hang).
2. `member_rng(seed: int, member_index: int, row_name: str) -> random.Random`: seed derived via `hashlib.sha256(f"{seed}:{member_index}:{row_name}".encode())`, so draws are independent per (member, row) and reproducible.
3. Tests: draws stay within `low`/`high` (uniform) and within clip bounds (truncated normal) over many samples; the same `(seed, member_index, name)` always reproduces the same draw; changing one row's name/params doesn't change another's draws.

Done. Exit: pure, fully unit-testable; nothing wired to a scenario yet.

## Phase 2 — Resolving one member's intent

1. New `src/analysis/sim/ensemble.py`: `resolve_member(recipe_intent_path, member_path, *, seed, member_index, member_count, recipe_tree_id) -> Path`:
   - Copies `recipe_intent_path` to `member_path`.
   - Reads every `calendar_event` row with `vary_distribution IS NOT NULL`.
   - **Pass 1 (dates):** for each `vary_target='date'` row, draw on `member_rng(seed, member_index, name)`, `UPDATE ... SET date = ?, vary_distribution = NULL`.
   - **Pass 2 (amounts/rates):** for each `vary_target='amount'` row, draw a value (or, with `vary_ramp_months` set, a target); apply directly, or — for a ramp — read the series' latest value as of this row's own `date` from `aux_table`, compute `vary_ramp_months` linearly-interpolated points, and `INSERT` them as ordinary `set_rate`-shaped calendar_event rows (clearing `vary_distribution` on the original row too).
   - Writes `ensemble_manifest` (one row) and `ensemble_draw` (one row per resolved row) into the copy.
2. Tests: a `set_amount`/`one_off_transfer`/`set_rate` row of each `vary_target` resolves to the right column with a value in range; a ramp's generated rows are monotonic between the base value and the drawn target and land on it at the right month; `ensemble_manifest`/`ensemble_draw` are correct; pass ordering — an amount variation on the same row family as a date variation always sees the resolved date, never the placeholder.

Done. Exit: one member intent can be fully resolved from a recipe intent in Python; still no CLI surface, and `run_scenario` has not been touched or even imported.

## Phase 3 — `sim run` generates the ensemble

1. `cli.py`: after the normal `load_scenario`, check for any unresolved `vary_distribution` row in the loaded intent. If none: `sim run` behaves exactly as today (regression-checked against the existing `test_sim_cli.py` run tests). If any:
   - Resolve `member_count` (`--ensemble-size N` if given, else `scenario.meta_json.ensemble_size`; a clear error naming which is missing if neither is set; since #169 neither set means not an ensemble, which runs its base case instead, ADR-006 §7) and the ensemble seed (`scenario.seed`; a clear error if unset).
   - Create `state/intent/<recipe>/` and `state/runs/<recipe>/`.
   - For each member: `resolve_member` (Phase 2), then the ordinary, unmodified `run_scenario(member_intent_path, member_run_path, ...)`. Echo progress.
   - Report a failed member (don't retry, matching `sim all`'s per-scenario tolerance) by index and its intent file path; keep going.
2. Tests: a small in-repo fixture recipe (2–3 members, one `vary_target='date'` row and one `vary_target='amount'` row) produces distinct, correctly-varying member intents and runs; `--ensemble-size` overrides `meta_json`; a deliberately-failing member (e.g. a drawn date outside the scenario's own range) is reported with its intent path and doesn't stop the other members.

Done. Exit: `sim run <recipe>` is the whole user-facing entry point for generating and running an ensemble; nothing else new to invoke.

## Phase 4 — Aggregation and reporting

1. `src/analysis/sim/ensemble.py` (or a sibling): `load_ensemble(runs_dir) -> dict[str, RunConnection]`; `summary(frame, value_col, group_col="month") -> DataFrame` returning mean/median/std/min/max, built the same way `scenario_compare`'s aggregation already is.
2. `sim ensemble summary <recipe> [--metric net_worth]`: prints the aggregate table plus the failure count/paths from Phase 3's run (read back from `ensemble_manifest` across members, or a small run-log written alongside the ensemble folder).
3. `notebooks/ensemble.ipynb`: a scenario picker scoped to ensemble recipes, a percentile-band ("fan") chart, and the same summary table. Follows `notebooks/README.md`'s widget conventions (return the box; let `Connections.show` own the first draw).
4. Tests: `summary` on a small fixture ensemble matches a hand-computed mean/median/SD; the CLI command's output includes every expected column and the failure count; the notebook helpers (`*_view` functions) are unit-tested without a kernel, the same way `allocation_view`/`events_view` are.

Done. Exit: mean/median/SD/range are one command or one notebook cell away for any ensemble that's been run.

## Follow-up — year-by-year process paths ([#115](https://github.com/BobKerns/analysis/issues/115))

1. `schema.py`: `calendar_event.vary_process TEXT`, `vary_persistence REAL`, `vary_innovation_sd INTEGER`; `SCHEMA_VERSION` 32 → 33. ADR-002 DDL and ADR-003 §4.8 to match; the columns in the calendar header; `registries.VARY_PROCESSES = {"ar1"}`.
2. `loader.py`: the §2 checks on a process row, including a level whose lower bound stays above −100%.
3. `ensemble.py`: after the date pass, each process row's step dates (yearly, to the scenario's end or the next authored `set_rate` for its series); `process_path` turns its level and that year's shocks into yearly rates in growth-factor form; the row takes year 0 and inserts the rest as ordinary `set_rate` rows carrying its payload. `ensemble_draw` records the level.
4. CLI and guided entry: `--vary-process`, `--vary-persistence`, `--vary-innovation-sd` (in percent) on `set-rate`; `calendar edit` keeps them. The parameters table gains a `process` column.
5. `analysis historical`: a `Shock SD` column beside Persistence; the calibration guide says how to set a process row from them.
6. Tests (`tests/test_rate_paths.py`): the path's flat base case, stationary spread, persistence, median compounding to the level and overflow guard; the loader and CLI rejections; step dates and cutoffs; shocks from the keyed streams; the level draw unchanged by adding a process; an end-to-end ensemble.

## Risks

- **Reject-sampling a `truncated_normal` with implausible bounds** could be slow or exhaust its attempt cap. Mitigated by Phase 1's cap-then-error; worth a `sim check`-time sanity warning if `clip_low`/`clip_high` are within, say, 0.5 SD of `mean`.
- **`state/intent/<recipe>/` or `state/runs/<recipe>/` colliding with an ordinary scenario of the same name** that already has a flat intent/run file under the non-ensemble convention. Worth an explicit check in Phase 3 rather than a silent overwrite.
- **Disk usage**: `member_count` full intent files *and* full run files, not diffs. Fine for the sizes discussed (hundreds); worth flagging if a use case wants thousands.
