# [ADR-007](ADR-007-sensitivity-analysis.md) implementation plan: sensitivity analysis

**Status: not started.**

Companion to [ADR-007](ADR-007-sensitivity-analysis.md). Phases are ordered so each ends with a green test suite and a shippable state. As in [ADR-006's plan](ADR-006-implementation-plan.md), `run_scenario`/`open_run_file` are never modified. Every phase adds code *around* them.

Ordering rationale:

- OAT generation and its basic report (Phases 0–3) come first: they're the primary tool and give useful answers on their own.
- Attribution (Phase 4) builds on OAT batches.
- The Monte Carlo correlation report (Phase 5) only needs existing ADR-006 batches, so it could move earlier, but it's the secondary tool.
- The notebook (Phase 6) needs every analysis function to exist.
- Parallel members (Phase 7) changes no results, so it's last.

Tests reuse `tests/conftest.py`'s fixtures: `append_calendar_row` and `load_test_copy` for recipes with `vary_*` rows, plus `test_data_dir`, `intent_dir` and `runs_dir`. They don't build new ad hoc setup.

## Phase 0 — Schema and quantiles (pure, no I/O)

1. `schema.py`: `ensemble_manifest` gains `mode TEXT NOT NULL DEFAULT 'monte_carlo' CHECK (mode IN ('monte_carlo','sensitivity'))`, and `ensemble_draw` gains `quantile REAL`. `SCHEMA_VERSION` 26 → 27. ADR-002 is amended to match, in the same way ADR-006's was.
2. `registries.py`: `ENSEMBLE_MODES = ("monte_carlo", "sensitivity")` and `DEFAULT_SENSITIVITY_QUANTILES = (0.1, 0.9)`.
3. `distributions.py`:
   - `Uniform.quantile(q)` / `Uniform.quantile_int(q)` (the date target, rounded to a day ordinal like `central_int`).
   - `TruncatedNormal.quantile(q)`: `NormalDist(mean, sd).inv_cdf(Φ(a) + q × (Φ(b) − Φ(a)))`, with `Φ(a)` = 0 / `Φ(b)` = 1 for a missing bound.
   - `q` outside `[0, 1]` raises `DistributionError`.
   - `parse_quantiles(text) -> tuple[float, ...]`: comma-separated values in `[0, 1]`, strictly increasing, rejecting 0.5 (the base covers the center).
4. Tests (`tests/test_distributions.py`):
   - `quantile(0)`/`quantile(1)` hit the bounds.
   - Quantiles are monotonic in `q`.
   - `Uniform.quantile(0.5) == central()`.
   - A symmetric `TruncatedNormal.quantile(0.5) == mean`.
   - An asymmetric clip shifts quantiles the right way.
   - A one-sided clip (`clip_low == mean`) gives `quantile(0) == mean`.
   - `quantile_int` stays inside the date range.
   - Empirical check: the fraction of 10k `draw()`s at or below `quantile(q)` is ≈ `q`.
   - `parse_quantiles` rejects malformed lists.

Exit: pure functions, nothing wired up. Existing intent and run files are recreated by `sim load`/`sim run` as on any schema bump.

## Phase 1 — OAT member resolution

1. `ensemble.py`: `resolve_member(..., overrides: dict[str, float] | None = None, mode: str = "monte_carlo")`.
   - A row named in `overrides` resolves to `dist.quantile(q)` (or `quantile_int` for a date), recording `ensemble_draw.quantile = q`.
   - Every other row resolves as member 0 does today: `central()`, with `quantile` NULL.
   - With `overrides` given, `member_rng` is never called.
   - `mode` is written to `ensemble_manifest.mode`.
   - An `overrides` key that isn't an unresolved `vary_*` row raises `EnsembleError`, so a typo in `--only` can't silently do nothing.
   - The dates-then-amounts order, ramp expansion and the `one_off_transfer` positivity check are unchanged.
2. `ensemble.py`: `oat_plan(row_names, quantiles, only=None) -> list[dict[str, float]]`. Index 0 is `{}` (the base), followed by one `{name: q}` per (row, quantile) in a stable order: rows by name, quantiles ascending. An `only` name not in `row_names` raises.
3. `write_batch_manifest` gains `mode`, plus `quantiles` and `rows` for sensitivity batches. `read_batch_manifest` treats a missing `mode` as `monte_carlo`, which covers existing batches.
4. `loader.latest_ensemble_batch(recipe_dir, mode=None)` filters on the manifest's `mode` when one is given. The member-0 fallback (`_latest_ensemble_member_zero`) stays mode-agnostic, since member 0 is the same resolved intent either way.
5. Tests (`tests/test_ensemble.py`):
   - An overridden row lands at its quantile, and every other row lands at `central()`.
   - The OAT base member's `calendar_event` rows equal a Monte Carlo member 0's, row for row.
   - `ensemble_draw.quantile` is set only on the overridden row.
   - A ramp row resolved at a quantile still lands on its (perturbed) target.
   - Unknown override and `only` names raise.
   - `oat_plan`'s length is `1 + k × m`.
   - `latest_ensemble_batch(mode=...)` picks the right batch when both kinds exist.
   - Old manifests without `mode` still read as `monte_carlo`.

Exit: an OAT batch's member intents can be generated in Python, but there's no CLI yet.

## Phase 2 — `sim run --sensitivity`

1. `cli.py`: split `_run_ensemble` into two parts:
   - batch-directory reservation plus a member runner that takes a list of member specs (`index`, `overrides | None`);
   - the existing Monte Carlo path, which builds `range(member_count)` specs from `--ensemble-size`/`meta_json` and the seed exactly as today.

   This refactor needs no behavior change, and the existing `test_sim_cli.py` ensemble tests guard it.
2. `sim run` gains `--sensitivity`, `--only NAME[,NAME...]` and `--quantiles 0.1,0.9`, with these errors:
   - `--sensitivity` on a scenario with no unresolved `vary_*` rows → "nothing to perturb";
   - `--sensitivity` with `--ensemble-size` → an OAT batch's size is fixed;
   - `--only`/`--quantiles` without `--sensitivity`.

   A sensitivity batch doesn't require `scenario.seed`.
3. `sim ensemble summary` and `ensemble_widgets` call `latest_ensemble_batch(..., mode="monte_carlo")`, so a fan chart is never drawn over an OAT batch. When `--batch` explicitly names a sensitivity batch, it fails with a message pointing at `sim ensemble sensitivity`.
4. Tests (`tests/test_sim_cli.py`):
   - A recipe with two varying rows and default quantiles produces 5 members with the right manifests.
   - `--only` and `--quantiles` change the member count accordingly.
   - Each error above.
   - `sim ensemble summary` skips a newer sensitivity batch in favor of the latest Monte Carlo one.
   - A failing OAT member is reported with its intent path, and the batch continues.

Exit: `sim run <recipe> --sensitivity` produces a complete OAT batch.

## Phase 3 — OAT report: years, units, tornado table

1. New `src/analysis/sim/sensitivity.py`:
   - `reporting_months(frame) -> list[month]`: the last simulated month of each calendar year, plus the final month if it isn't a December (ADR-007 §4).
   - `oat_deltas(batch_dir, dollars, relative) -> DataFrame`: long form with columns (row, quantile, year, value, delta, base). Relative Δ is NaN where the base is within $1,000 of zero. Computed per member with that member's own deflator, using `net_worth_frame()`'s `nominal`/`cpi_constant` columns.
   - `min_net_worth` to date, as a second measure column.
   - `oat_table(deltas, year)`: per row, `value@q`, `Δ@q`, `swing`, `share`, `flags`, sorted by absolute swing.
   - `rank_by_year(deltas)`: the parameter × year rank (or share) grid.
   - Flags:
     - `asymmetric`: the ratio of the absolute outer Δs is outside [0.5, 2]. This is a module constant, adjustable once there are real results.
     - `bend`: needs ≥ 4 quantiles. Fires when the largest slope between adjacent rungs is more than 3× the median slope, or the slope changes sign.
   - The display formatting of `value@q` (dates, dollars, percent from micros) reuses the existing calendar-reporting formatters rather than adding new ones. Find them with `ripwire --exemplar` before writing anything.
2. `cli.py`: `sim ensemble sensitivity <recipe> [--batch] [--mode] [--dollars] [--relative] [--years] [--negligible]`. For an OAT batch it prints:
   - the default year set (first full year, every fifth year, final year), or `--years`;
   - one table per year;
   - the rank-by-year grid;
   - rows under `--negligible` in every year, as pin candidates.

   A Monte Carlo batch prints "not yet supported" until Phase 5.
3. Tests (`tests/test_sensitivity.py`, plus CLI tests):
   - `reporting_months` on a mid-year start and end.
   - On a recipe varying one savings rate, Δ has the right sign and grows over time.
   - A retirement-date row has Δ = 0 in years before its earliest quantile date.
   - The relative-Δ near-zero guard.
   - The nominal and CPI-constant relative Δs match for a non-inflation row, and differ when the CPI series row is the one perturbed.
   - The flags fire on synthetic Δ frames built to trigger them, and don't fire on linear ones.
   - CLI output includes every column and the pin-candidate list.

Exit: the terminal answers "which parameters matter, and when" for any OAT batch.

## Phase 4 — Attribution

1. `sensitivity.py`: `attribution(base: RunConnection, member: RunConnection, months, *, by, dollars) -> DataFrame`, with columns (year, key, delta) plus an `unattributed` row.
   - `by='category'`: cumulative net flow per income/expense `subkind` through each reporting month, perturbed minus base. Built on `transfer_event` joined to `account`, excluding `reason_kind='ytd'`. Signs are chosen so that the rows sum to Δ net worth: income outflow is +, expense inflow is −. Do this in one query per run file, not per month; the existing `flow_by_account` view groups by year, which is close but not month-exact.
   - `by='flow_account'`: the same, per account.
   - `by='holding_account'`: per asset/liability balance Δ, from `balance_by_account`.
   - CPI-constant: deflate each member's cumulative flows at Y by that member's own CPI at Y (ADR-007 §6.1), so the rows still sum to the CPI-constant Δ.
   - `unattributed` = Δ net worth − Σ rows. It's kept as a row, never dropped, so a non-zero value is visible.
2. `cli.py`: `sim ensemble sensitivity ... --attribute NAME [--by ...]` prints one table per printed year, with one column per quantile.
3. Tests:
   - For a perturbed salary-related row, the rows sum to Δ exactly, and `unattributed == 0` in both dollar modes.
   - For a perturbed growth-rate row, the change lands in `capital_gain`/`interest`.
   - For an inflation-row perturbation, CPI-constant rows still sum to the CPI-constant Δ.
   - `holding_account` rows sum to Δ net worth.
   - **Exploratory, before writing the rest of the phase:** run `unattributed` over the `retire_alice` prototype with every account kind present. If it isn't zero, find which balance changes bypass `transfer_event` and either include them or document them, before trusting the identity.

Exit: "why" is one flag away for any parameter in an OAT batch.

## Phase 5 — Monte Carlo correlation report

1. `sensitivity.py`:
   - `draws_frame(batch_dir) -> DataFrame` (member × row), from each succeeded member's `ensemble_draw`, excluding member 0. Dates are converted to ordinals and amounts to numbers.
   - `correlation_table(draws, outcomes_by_year, negligible)`, per row and year:
     - Spearman ρ via `rank().corr()`;
     - a Fisher-z 95% CI using the variance `1.06 / (n − 3)`;
     - SRRC from `numpy.linalg.lstsq` on standardized ranks;
     - per year, R².
   - Classification per row: `pin_candidate` if the upper bound of ρ² is below `negligible` in every year; `inconclusive` if ρ² is below `negligible` in every year but the CI is too wide; otherwise `keep`.
   - `required_members(negligible) -> int`: inverts the CI half-width formula, for the "too few members" warning.
2. `cli.py`: the Monte Carlo branch of `sim ensemble sensitivity` prints, per printed year:
   - ρ with its CI, SRRC, and R²;
   - the classification, with a warning when n is below `required_members`;
   - a warning when R² is below 0.7.
3. `docs/guides/ensembles.md`: a "Which varying rows can I drop?" section with the member-count table from ADR-007 §6.2. It's explicitly separate from the existing fan-chart sizing table.
4. Tests (synthetic frames, no simulation needed):
   - Outcome = 3·x₁ + 0·x₂ + noise at n = 400: x₂ is a `pin_candidate`, x₁ is `keep`, R² > 0.9.
   - The same at n = 30: x₂ is `inconclusive`.
   - A pure interaction outcome (x₁·x₂, both centered) gives a low R².
   - `required_members(0.05)` is ≈ 100.
   - On one real small batch, member 0 is excluded.

Exit: any existing ADR-006 batch can be checked for droppable dimensions.

## Phase 6 — `notebooks/sensitivity.ipynb`

1. `notebook_ui/pickers.py` (next to `DollarModePicker`, in the same style):
   - `DeltaScalePicker` (`ToggleButtons`: absolute / relative);
   - `ParameterPicker` (`Dropdown`, labeled "name — target, range");
   - `ReportYearPicker` (`SelectionSlider`);
   - `HeatmapValuePicker` (share / signed swing);
   - `AttributionPicker` (category / flow account / holding account).
2. New `notebook_ui/sensitivity_widgets.py`. Each widget is a `*_view` function (pure data → figure) plus a `*_widget` wrapper that wires the pickers. Follows `notebooks/README.md`'s conventions: return the box, and let `Connections.show` own the first draw. The widgets are:
   - heatmap (first);
   - tornado;
   - Δ over time;
   - attribution;
   - response curve (only when the batch has ≥ 4 quantiles);
   - for a Monte Carlo batch: the ρ grid with CI whiskers and the R² strip.

   A batch picker, scoped to the recipe's batches of either mode, chooses which set to show.
3. `notebooks/sensitivity.ipynb`, plus an entry in `notebooks/README.md`.
4. Tests (`tests/test_notebook_ui_pickers.py`, a new `tests/test_sensitivity_widgets.py`):
   - each picker's options and value trait;
   - each `*_view` on small synthetic frames, without a kernel, the way `test_ensemble_widgets.py` does it;
   - the response-curve widget is omitted for a two-quantile batch.

Exit: the full ADR-007 §9 notebook.

## Phase 7 — Parallel members (`--jobs N`)

1. `cli.py`: the Phase 2 member runner dispatches specs to a `ProcessPoolExecutor(max_workers=N)` when `--jobs > 1`. Each worker calls `resolve_member` and then `run_scenario`, and returns `(index, run_path | exception text)`.
   - Batch reservation and `write_batch_manifest` stay in the parent.
   - Progress is printed in completion order; the final summary is sorted by index.
   - `--jobs 0` means `os.cpu_count()`. The default is 1, which keeps the in-process path with no pool.
2. Tests:
   - A Monte Carlo and an OAT batch run with `--jobs 2` produce the same per-member results as `--jobs 1`. Compare `ensemble_draw` and net-worth frames, not file bytes, since timestamps differ.
   - A failing member under `--jobs 2` is reported and doesn't take down the pool.
   - Workers must be importable under the `spawn` start method, which is the macOS default: no lambdas or closures passed to the pool.

Exit: large correlation-grade batches are practical to iterate on.

## Risks

- **Heuristic thresholds** (the asymmetry ratio, the bend factor, R² ≥ 0.7, the $1,000 near-zero guard) are first guesses. They're kept as named module constants and should be revisited once there are real batches. None of them changes a number, only a flag.
- **The attribution identity could have gaps:** any balance change that bypasses `transfer_event`, such as a future price-path mechanism. The `unattributed` row makes a gap visible rather than silent, and Phase 4 checks it against the full prototype before relying on it.
- **A ladder with many rows gets expensive:** `1 + k × m` members, so 6 quantiles × 8 rows = 49 runs. `--only` is the intended way to use ladders: a two-quantile pass first, then a ladder on the rows that showed asymmetry. The user guide should say so.
- **A quantile date can fall outside the scenario range**, as an ADR-006 draw can. It's the same failure mode, with the same reporting.
