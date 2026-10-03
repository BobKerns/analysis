# [ADR-008](ADR-008-correlated-parameters.md) implementation plan: correlated parameters

**Status: implemented.** Phase 0 (`8f77956`): schema, the `latent_factor` kind, validation, load warnings. Phase 1 (`6c2fe37`): `correlation.py` and `value_at`. Phase 2 (`912c27e`): `resolve_member` integration and provenance. Phase 3: CLI authoring, the parameters table, and docs.

**Beyond the plan:** factors are left out of the Monte Carlo correlation report's inputs, and the report notes a correlated batch, since its per-row numbers assumed independence until the follow-up, [#38](https://github.com/BobKerns/analysis/issues/38), made it rank and pin correlated rows by SRRC.

Companion to [ADR-008](ADR-008-correlated-parameters.md). Each phase ends with a green suite. As in ADR-006 and ADR-007, `run_scenario` changes only to treat `latent_factor` as inert. Tests reuse `tests/conftest.py`'s fixtures (`append_calendar_row`, `load_test_copy`, the batch fixtures).

## Phase 0 — Schema, registry, loader validation

1. `schema.py`: `calendar_event.vary_correlate_with TEXT`, `calendar_event.vary_correlation REAL`, `ensemble_draw.z REAL`; `SCHEMA_VERSION` 29 → 30. ADR-002 DDL to match.
2. `registries.py`: `latent_factor` in `CALENDAR_EVENT_KINDS`; the two columns in the calendar header.
3. `loader.py`:
   - A `latent_factor` row: needs a `name`; may leave `date` blank (the scenario start is stored as a placeholder); has no `vary_distribution` or other `vary_*` parameter except the two correlation columns.
   - `vary_correlate_with` and `vary_correlation` are given together, ρ ∈ [-1, 1].
   - Over the merged calendar (after inheritance): every parent names a varying row or a factor; there are no cycles (report the cycle's names).
   - **Warnings** (new): `LoadResult.warnings: list[str]`, printed as `WARN:` lines (ADR-004 §6) by `sim check`, `sim load`, `sim run` and `sim calendar add`'s validating load, without failing. `sim load`'s default run prints each warning once. First use: a factor with no dependents, which may be work in progress or meant for child scenarios.
   - **Merge key:** a `latent_factor` row's calendar overlay key is its `name` (ADR-008 §4), so two factors don't collide on their blank event fields, and a child scenario's factor overrides its parent's by name.
4. `engine.py`: `latent_factor` is inert. It's skipped, and not counted in `skipped_calendar`.
5. Tests: each rejection; an unused factor loads with a warning, printed once by `sim load` (which runs) and by `sim check`; two factors in one scenario both survive the merge, and a child scenario's same-named factor overrides the parent's; a valid two-level tree (factor → factor → rows) loads; inheritance (a child recipe's row naming a parent scenario's factor); a recipe with factors still runs as an ensemble, and an ordinary scenario is unaffected.

## Phase 1 — Copula math (pure, no I/O)

1. `distributions.py`: `value_at(u)` per family:
   - continuous: `quantile(u)`;
   - date (`value_at_int`): `uniform` is `low + floor(u · days)`, clamped, so every day is equally likely as in `draw_int`; others are `round(quantile(u))`.
2. A new `correlation.py` (or a section of `distributions.py`):
   - `resolve_scores(tree, noise) -> dict[name, z]`, in topological order, applying `z = ρ·z_parent + √(1−ρ²)·ε`;
   - `expected_rank_correlation(ρ) = (6/π)·asin(ρ/2)`;
   - `implied_correlation(tree, a, b)`: the product of ρ along the path, 0 for different trees.
3. Tests:
   - **Each row's own distribution is preserved:** over 5,000 simulated members, a child's empirical quantiles match its own `quantile` for uniform, skewed `truncated_normal` and `triangular`, within sampling tolerance.
   - **Correlation:** the recorded normal scores' correlation between parent and child, and between two siblings, matches the specified or implied ρ, within tolerance. For continuous values (amounts, long date ranges), the value-level Spearman also matches `expected_rank_correlation`. A short date range's tied values are tested on scores, not values (ADR-008 §1).
   - ρ = 0 gives independent rows; ρ = ±1 makes the child a monotone function of the parent (Spearman ±1).
   - Draws stay within bounds.

## Phase 2 — `resolve_member` integration and provenance

1. Build each member's trees from the intent's `calendar_event` rows (varying rows plus factors).
2. Compute the scores first: each name's noise is `Φ⁻¹(member_rng(seed, member, name).random())`. Member 0 has every score 0. An OAT override sets that row's score to `Φ⁻¹(q)` (its children aren't moved, per ADR-008 §5).
3. Then the existing dates-then-amounts passes: a row in a tree uses `value_at(Φ(z))`; a row outside every tree keeps `draw` / `draw_int` / `central` exactly as now.
4. Provenance: `ensemble_draw.z` for rows in trees, plus an `ensemble_draw` row per factor (`vary_target = 'factor'`).
5. Tests:
   - **Byte-identical resolution for a recipe with no correlation columns**, compared against the current `resolve_member` output;
   - a correlated recipe's member 0 equals the uncorrelated member 0;
   - a resolved child is within bounds and its `z` is recorded;
   - adding an unrelated tree doesn't change an existing tree's draws (keyed streams);
   - ramps still work for a correlated `set_rate`;
   - over a 200-member batch, the correlation of two correlated rows' recorded `ensemble_draw.z` is within tolerance of ρ.

## Phase 3 — Authoring, reporting, docs

1. CLI: `sim calendar add latent-factor NAME [--description] [--vary-correlate-with --vary-correlation]`; `--vary-correlate-with` / `--vary-correlation` on every other `add`.
2. `sensitivity.parameters`: `correlate_with` and `ρ` columns, and factor rows (kind `latent_factor`, target `factor`).
3. Docs: ADR-006 §2 and §3 brief "Amended by #30" notes and column rows; ADR-003 §4.8 columns and the `latent_factor` kind; ADR-002 DDL; `docs/guides/ensembles.md` "Correlated parameters" section, covering choosing ρ, factors, and the product rule; the sensitivity notebook's parameters text.
4. File the two follow-up sub-issues on #30: the correlation-aware Monte Carlo report, and OAT factor perturbation.

## Risks

- **Choosing ρ:** authors may over-specify precision. The guide recommends coarse values and explains the product rule for siblings.
- **A child's ρ is relative to its parent, not to other rows.** Authors who think in pairwise correlations may be surprised that siblings correlate at the product. The parameters table should show each pair's implied correlation, or at least each row's tree, to make this visible.
- **The Monte Carlo report's per-row ρ is misleading for correlated inputs.** The report should note when a batch has correlated rows. [#38](https://github.com/BobKerns/analysis/issues/38) has since made it rank and pin them by SRRC.
