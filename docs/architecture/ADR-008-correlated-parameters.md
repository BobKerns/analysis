# ADR-008: Correlated parameters in ensembles

- **Status:** Proposed
- **Date:** 2026-10-03
- **Supersedes:** —
- **Related:** [ADR-006](ADR-006-ensemble-scenarios.md) (ensemble scenarios: `vary_*` rows, keyed RNG streams, member 0), [ADR-007](ADR-007-sensitivity-analysis.md) (sensitivity analysis, which assumes independent draws), [ADR-003](ADR-003-registries-and-csv-schemas.md) §4.8 (`calendar.csv`), issues [#30](https://github.com/BobKerns/analysis/issues/30) (this design; decisions recorded there) and [#31](https://github.com/BobKerns/analysis/issues/31) (`quantile()` on every family, the median as center).

## Context

ADR-006 draws every varying row independently. Reality isn't like that:
- inflation and interest rates move together;
- stocks and bonds are correlated, sometimes negatively;
- the same series in 2030 and 2040 isn't two unrelated guesses.

Independent draws make the ensemble's spread wrong: they understate it when uncertainties compound (two rates that are both high in bad years), and overstate it when they offset.

Two things this design must not lose:
- **Each row's authored distribution.** A row's range is the author's belief about that value *over all futures* ("inflation is 2–4%"). That belief is what can be checked against history. Correlation should change how rows move *together*, not what any one of them looks like.
- **ADR-006's stability properties:** keyed RNG streams (adding a row never changes other rows' draws), bounds that a draw can't escape, and a deterministic member 0.

## Decision

### 1. Gaussian copula

For each member, every row in a correlation tree (§2) gets a **standard normal score** `z`. The scores are correlated as specified. Each row's value is then its own distribution's quantile at `u = Φ(z)`:

```
value = dist.value_at(Φ(z))
```

Because `u` is uniform on its own, **each row's distribution, taken alone, is exactly what was authored**: same median, spread, shape and bounds. Only the joint behavior changes. Given a parent's value, a child's distribution shifts toward the parent's side, narrows, and is skewed against its bounds as a consequence. All of that is sized so the authored distribution holds overall.

**Bounds never move.** A correlated draw can't leave its row's authored range, so none of the edge cases that moving bounds would bring can arise: negative transfer amounts, dates outside the scenario, a distribution centered off its peak.

**ρ means the same thing for every family.** The copula correlation ρ between two normal scores gives a rank (Spearman) correlation between the two rows' *values* of `(6/π)·asin(ρ/2)`, whatever their distributions are. That's within 0.02 of ρ everywhere. So "ρ = 0.6" predictably means "rank correlation about 0.58" for a uniform date and a skewed rate alike. Reports (§6) compare achieved rank correlations against this.

### 2. Structure: one parent per row, no cycles

Each row (or factor, §3) may name **at most one** parent, with a correlation ρ ∈ [-1, 1]:

```
z_child = ρ · z_parent + √(1 − ρ²) · ε_child
```

`ε_child` is the child's own standard normal noise. The result is a forest of trees, which:
- **can't specify an impossible correlation matrix.** Every tree gives a valid joint distribution, so authors never meet a "not positive semi-definite" error;
- **multiplies correlations along paths.** Two children of one parent, at ρ₁ and ρ₂, correlate at ρ₁·ρ₂. To make two siblings correlate at 0.6, give each about 0.77;
- **handles anti-correlation** with negative ρ;
- **covers correlation over time:** a 2040 growth-rate row depending on the 2030 row of the same series.

**Several parents per row, or a full correlation matrix, are deferred.** One parent plus latent factors is the standard factor model, and it covers every case raised so far. Anything richer needs more economic insight than is usually available: stock-bond correlation has even changed sign across decades. Coarse values (±0.3, ±0.6, ±0.9) are about what anyone can justify.

### 3. Latent factors: a `latent_factor` calendar kind

A shared influence that isn't itself a modeled value ("the inflation regime", "the market") is a **latent factor**: a `calendar.csv` row of kind `latent_factor`, with a `name` and an optional `description`. It has no date, amount or series meaning. Its value is a standard normal score, and its dependents correlate with it. A factor may itself depend on another factor, which gives a hierarchy.

This keeps "variation is authored in `calendar.csv`", and gets calendar reporting for free. It's not a separate `factors.csv` with its own unobvious usage. And a factor is never created implicitly from an unknown name, where a typo would silently create one.

### 4. Authoring and validation

Two new `calendar.csv` columns, valid on a varying row and on a `latent_factor` row:

| column | notes |
| --- | --- |
| `vary_correlate_with` | the parent: another varying row's or a factor's `name` |
| `vary_correlation` | ρ, in [-1, 1]; required with `vary_correlate_with` and only with it |

A `latent_factor` row's `date` may be blank. The loader stores the scenario start as a placeholder, because the `calendar_event.date` column is required and nothing reads it.

The loader rejects:
- a parent that names no varying row or factor in the scenario, after inheritance merges the calendar (ADR-003 §4.5);
- a cycle;
- |ρ| > 1;
- a `latent_factor` row with `vary_distribution` or any other `vary_*` parameter.

**A factor with no dependents is a warning, not an error.** It may be work in progress, or defined in a parent scenario for its child scenarios to use, in which case it has no dependents in the parent itself. The loader gains a warnings channel for this: a `warnings` list on `LoadResult`, which `sim load` and `sim run` print. A warning doesn't stop the load. An unused factor resolves to its own draw and affects nothing.

`sim calendar add latent-factor NAME [--description …] [--vary-correlate-with … --vary-correlation …]` adds a factor. Every other `add` subcommand gains `--vary-correlate-with` and `--vary-correlation`.

### 5. Resolution (`resolve_member`)

1. **Scores first.** Compute `z` for every factor and every row in a tree, in topological order (parents first). A root's `z` is its own noise. Each `ε` is drawn on that name's own keyed stream, `member_rng(seed, member, name)` (ADR-006 §3), as `Φ⁻¹(rng.random())`, so a change to one tree never shifts another's draws.
2. **Then the existing passes**, dates first and then amounts. A row in a tree takes its value from `value_at(Φ(z))`; ramps expand from that value as before.
3. **Rows outside every tree draw exactly as today** (`draw()` / `draw_int()`), so existing recipes resolve byte-for-byte as before. (For every family except a `uniform` date, `draw` already equals `value_at(rng.random())`. Keeping the old call for untouched rows makes that exact for uniform dates too.)
4. **Member 0** sets every score to 0, so `u = 0.5`, the median, which is `central()` (#31). It's unchanged.

`value_at(u)` is the family's quantile, with dates in whole days as drawn. For a `uniform` date that means each day is equally likely, `low + floor(u · days)`, matching `draw_int`. For other families it's `round(quantile(u))`.

**One-at-a-time sensitivity (ADR-007) is unchanged** in this ADR: it moves one row with every other row at its center, which is still a well-defined "effect of this row alone". Perturbing a latent factor, which moves its whole family to their conditional medians, is a follow-up.

### 6. Provenance and reporting

- `ensemble_draw` gains **`z REAL`**: each row's normal score. It's NULL for a row outside every tree, Φ⁻¹(q) for an OAT override, and 0 for member 0. Each **factor** also gets an `ensemble_draw` row (`vary_target = 'factor'`, `drawn_value` = its `z`), so achieved correlations can be checked after the fact.
- Factor rows stay in member intent files as provenance. The engine treats `latent_factor` as inert: no effect, and not counted as an unsupported kind.
- The **parameters table** (ADR-007 §9) shows each row's parent and ρ, and lists the factors.

**Follow-ups, as sub-issues of #30:**
- **The Monte Carlo sensitivity report assumes independent inputs** (ADR-007 §6.2: SRRC ≈ ρ, pinning by marginal ρ). With correlated inputs it should rank and pin by SRRC or partial rank correlation (PRCC), and show the achieved rank correlations next to the expected `(6/π)·asin(ρ/2)` per pair.
- **One-at-a-time perturbation of latent factors.**

### 7. Schema

Schema 30:
- `calendar_event` gains `vary_correlate_with TEXT` and `vary_correlation REAL`;
- `ensemble_draw` gains `z REAL`;
- `latent_factor` joins `CALENDAR_EVENT_KINDS`.

## Consequences

- **Positive:**
  - Each row's authored distribution is exactly preserved, and correlation never moves a bound.
  - The tree structure can't produce an invalid correlation matrix.
  - Existing recipes and untouched rows resolve exactly as before.
  - ρ means a predictable rank correlation for any pair of families.
  - Everything is still authored in `calendar.csv`.
- **Negative:**
  - **No tail dependence.** A Gaussian copula makes joint extremes rarer than in reality, where crashes tend to hit everything at once. See the deferred Student-t copula.
  - Only tree-shaped correlation structures can be expressed.
  - Choosing ρ needs judgment that data rarely settles.
  - Until the Monte Carlo report follow-up lands, that report's per-row correlations overstate correlated rows' individual importance.
- **Not attempted:** several parents per row; correlation of a row with itself over time beyond what explicit rows give; correlating values that aren't `vary_*` rows.

## Open questions deferred

- **Student-t copula for tail dependence.** One extra parameter (degrees of freedom) makes joint extremes likelier. It's a drop-in change at the "correlated normal scores" step, but needs scipy for the t CDF and inverse CDF. Build the Gaussian copula first, check whether the missing joint extremes visibly matter, and add this only if they do.
- **A full correlation matrix** for rows that genuinely need more than a tree, with positive-semi-definite validation and a nearest-valid-matrix repair.
- **Estimating ρ from history** (e.g. from a series' own CSV data), rather than authoring it by judgment.
