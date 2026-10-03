# ADR-007: Sensitivity analysis — which uncertain parameters actually matter, and when

- **Status:** Accepted
- **Date:** 2026-10-01
- **Supersedes:** —
- **Amended by:** [#31](https://github.com/BobKerns/analysis/issues/31): `truncated_normal` quantiles follow its new sinh-arcsinh shape, `triangular` added, and the base case is every family's median (§3); the parameters view shows shape and implied moments (§9). [#42](https://github.com/BobKerns/analysis/issues/42): `--jobs` now defaults to one worker per available CPU, not 1 (§8). [#39](https://github.com/BobKerns/analysis/issues/39): an OAT batch also perturbs [ADR-008](ADR-008-correlated-parameters.md) latent factors, each moving its whole family to their conditional medians, and reports them labeled as factors (§2, §5, §6.1, §7, §9).
- **Related:** [ADR-006](ADR-006-ensemble-scenarios.md) (ensemble scenarios — `vary_*` columns, `resolve_member`, `ensemble_manifest`/`ensemble_draw`, member 0), [ADR-002](ADR-002-storage-schema.md) (storage schema), [ADR-004](ADR-004-reproducibility-and-activation.md) (reproducibility, provenance).

## Context

ADR-006 answers "given these uncertain inputs, what's the spread of outcomes?" It doesn't answer the questions that usually come before and after that:

- **Before a Monte Carlo:** which of the inputs I'm unsure about actually move the outcome? A retirement date, a fund's growth rate, an inflation assumption or a one-off home sale might move net worth by six figures across its plausible range, or barely at all. The answer points at which cash flow, date or investment choice in *this* scenario deserves attention.
- **After a Monte Carlo:** which varying parameters are contributing so little that they could be pinned to a literal? `docs/guides/ensembles.md` already explains that each extra independent dimension thins out coverage for the same member count, so dropping dimensions that don't matter makes the ensemble more informative, not just cheaper.

The answers are **scenario-specific**. A growth-rate assumption that dominates a scenario where most assets are invested may be irrelevant in one that has drawn them down. A result belongs to one recipe at one `recipe_tree_id`, and is never cached or reused across scenarios.

They're also **time-specific**. A parameter's effect isn't one number. A fund's rate of return contributes until that fund is exhausted and then stops; a retirement date matters from that date onward; a growth rate's effect compounds, so it's small early and large late. A single "terminal net worth" sensitivity hides all of this, and can rank parameters in exactly the wrong order for a short-term question. **The reporting date is therefore a dimension of every sensitivity result**, not an option (§4).

Four design questions mattered most.

**Where does the list of parameters, and how far to perturb each, come from?** Not a new spec file. A recipe's `vary_*` rows already name each uncertain parameter (`calendar_event.name`), say which column is uncertain (`vary_target`), and give its plausible range. That's exactly the input sensitivity analysis needs, so authoring a parameter for sensitivity and authoring it for Monte Carlo are the same act. The workflow is: mark every candidate row as varying, run sensitivity, pin the rows that don't matter back to literals, then run the Monte Carlo on what's left. This uses one recipe, one set of rows and one CLI.

**How far is "perturb"?** A fixed delta (±10%, ±1pp, ±6 months) has two problems:

- **Swings aren't comparable.** Is ±1pp on a growth rate a bigger or smaller uncertainty than ±6 months on a retirement date? The ranking would reflect how the deltas were chosen.
- **A fixed delta that happens to cross a transition makes the sensitivity itself unstable.** Examples are a date crossing a year boundary or a tax bracket, or an amount that tips a fund into exhaustion one year earlier. In those cases the result depends sharply on the exact delta chosen.

Instead, each row is perturbed to **quantiles of its own distribution** (p10 and p90 by default). Every parameter then moves by an equally plausible amount. Because the quantile list can be widened into a ladder (p10, p25, p75, p90, …), a transition inside the range shows up as a kink in the response rather than silently distorting a two-point estimate (§3).

**One-at-a-time, or over a Monte Carlo?** Both, for different purposes:

- **One-at-a-time (OAT)** is the primary tool. With `k` rows and `m` quantiles it costs `1 + k × m` runs. It's deterministic, gives easy-to-read signed deltas, and shows asymmetry and transitions. It can't see interactions.
- **Rank correlation over a Monte Carlo batch** is secondary. It costs no new runs because every ADR-006 member already records what it drew (`ensemble_draw`). It's the right tool for "which dimensions of *this ensemble* can I drop?", and for decision-shaped questions such as "which fund should we draw down faster?". It accounts for everything varying at once, but needs considerably more members than a fan chart does (§6.2), and with a small `k`.

**How fast is iteration?** Members run sequentially today (`_run_ensemble`'s `for` loop). OAT batches are small, but a correlation-grade Monte Carlo batch is in the hundreds. Members are fully independent (separate intent and run files, with no shared state), so running them in parallel is straightforward (§8).

## Decision

### 1. Two modes, one report command

```text
                    recipe intent (vary_* rows)
                     |                       |
   sim run <recipe> --sensitivity      sim run <recipe>          (ADR-006, unchanged)
                     |                       |
     OAT batch: 1 + k×m members      Monte Carlo batch: N members
        (mode='sensitivity')         (mode='monte_carlo')
                     \                       /
                      sim ensemble sensitivity <recipe> [--batch B]
                       -> OAT table per year       or  -> rank-correlation table per year
```

`sim ensemble sensitivity` reads the batch's `mode` (§5) and picks the estimator. Units and reporting years are chosen at **report** time, not run time. Every member run file holds the full monthly history, so a single batch answers every year and every unit without rerunning anything.

### 2. OAT generation reuses `resolve_member`

`resolve_member` today resolves each `vary_*` row to `central()` (member 0) or to a draw from `member_rng` (members 1+). It gains one optional argument, `overrides: dict[str, float] | None`, mapping a row name to a **quantile in [0, 1]**. For a row named in `overrides`, the value is that quantile of the row's distribution (§3); every other row resolves exactly as member 0's does. Nothing else in `resolve_member` changes: dates before amounts, ramp expansion (ADR-006 §4, applied to the perturbed target), `ensemble_manifest`/`ensemble_draw` provenance, and the `one_off_transfer` positivity check.

An OAT batch for rows `r1 … rk` and quantiles `q1 … qm` is:

| member | resolution |
| --- | --- |
| `member_0000` | every row `central()`, the same as ADR-006 member 0, so `resolve_scenario_name`'s member-0 fallback works on either kind of batch |
| `member_0001` … `member_000m` | `r1` at `q1` … `qm`, everything else central |
| … | … |
| last `m` members | `rk` at `q1` … `qm`, everything else central |

No RNG is consulted anywhere in an OAT batch, so it's fully deterministic. `scenario.seed` isn't required, is ignored if set, and isn't recorded: a sensitivity member's `ensemble_manifest.seed` is NULL (§5). Members are still ordinary intent files run by the unmodified `run_scenario`, and the same failure reporting as ADR-006 §7 applies: reported, not retried, with the intent file kept.

**Latent factors** ([ADR-008](ADR-008-correlated-parameters.md) §3) are swept too, because with correlated rows the more useful question is often "what if the whole family moves?" (say, "what if inflation runs hot generally?"), not "what if this one row moves?". `overrides` may name a factor. Its score is set to Φ⁻¹(q), and every descendant, through any sub-factors, moves to its **conditional median** given the factor: a score of (the product of ρ along the path) × the factor's score, mapped through the row's own `value_at` (ADR-008 §5). At q = 0 or 1 the score is infinite, so each descendant lands exactly on its bound (or stays at its median through a ρ of 0). Rows outside the factor's tree stay central. A factor's member records the quantile on the factor's own `ensemble_draw` row and NULL on its descendants' rows, which record their conditional `z`, so each OAT member still names exactly one perturbed node. Every factor that at least one varying row depends on is swept by default, at the same quantiles; one that moves nothing would only repeat the base, so it's skipped.

Overriding an ordinary row still moves that row alone, even when other rows depend on it, so a row's entry keeps one meaning: "this row alone". The family question belongs to a factor. If it's wanted for a row with dependents, put a factor above them.

`--only NAME[,NAME...]` restricts the sweep to a subset of rows and factors; the rest stay central. Naming a factor that no varying row depends on is an error rather than a batch of base-case copies.

### 3. Quantiles, not deltas, and ladders to find transitions

`distributions.py` gains `quantile(q)` on each family, using only the stdlib (`statistics.NormalDist`), so no new dependency:

- **`Uniform`:** `low + q × (high − low)`. For a date target there's a matching `quantile_int` that rounds to a day ordinal, like `central_int`.
- **`TruncatedNormal`:** the quantile of the *truncated* distribution: the underlying (sinh-arcsinh, [ADR-006](ADR-006-ensemble-scenarios.md) §3) distribution's quantile at `F(a) + q × (F(b) − F(a))`, where `a`, `b` are the clip bounds and `F` is the underlying CDF. Both are closed-form via `statistics.NormalDist`. The perturbation therefore always stays in bounds and respects one-sided truncation.
- **`Triangular`:** closed-form, with `quantile_int` for a date target.

The default is `--quantiles 0.1,0.9`, which is "plausibly low / plausibly high" rather than the extremes. `0,1` gives the full authored range, but `truncated_normal`'s clip bounds default to 3 standard scores out and are rarely representative. There must be at least two values, in `[0, 1]`, strictly increasing, and not `0.5`, since the base already covers the center.

**Transitions.** Two signals in the report flag that a row's range contains a kink:

- **Asymmetry**, which is visible even with the default two quantiles: `|Δ_high| / |Δ_low|` far from 1. Some asymmetry is normal (taxes, compounding), so this is a prompt to look, not a verdict.
- **Non-monotonic or sharply bent response across a ladder:** `--quantiles 0.1,0.25,0.4,0.6,0.75,0.9` costs 6 runs per row instead of 2, and gives a response curve (Δ against quantile) per reporting year. A transition shows up as a step or a slope change between two adjacent rungs. That identifies roughly *where* in the range it happens, which is often the interesting finding in itself ("retiring after June costs a year of the higher bracket").

The base case is `central()`, which is every family's median, `quantile(0.5)` (for `truncated_normal`, the authored `vary_median`, exact even with asymmetric bounds). So the OAT base *is* ADR-006's member 0, byte-for-byte the same resolved intent, and it sits at the middle of every quantile ladder.

### 4. Reporting dates and units

**Reporting dates.** Every result is computed at a grid of **annual** reporting dates: the last simulated month of each calendar year in the scenario's range, plus the scenario's final month if it isn't a December. Monthly reporting would be possible but adds noise and volume without changing the picture at planning horizons. Year-end aligns with tax-year effects. The grid is fixed rather than configurable for now; a finer grid is an additive change if a real need appears.

**Measure.** Net worth at each reporting date (`--measure net_worth`, the default), or `min_net_worth` to date (`--measure min_net_worth`): the lowest net worth from the start through that year, which captures "how close do I get to running out" and is a common short-term question. Both come from `net_worth_frame()`, and the registry is closed and grows one line at a time, like ADR-006's distributions.

**Units.** These are two independent choices that compose, matching the existing `DollarModePicker` rather than duplicating it:

| | absolute Δ | relative Δ (% of base) |
| --- | --- | --- |
| **nominal** | dollars | Δ / base, in nominal dollars |
| **CPI-constant** | today's dollars | Δ / base, in CPI-constant dollars |

The two percentage cells are usually identical, because base and perturbed member share a CPI path, so the deflator cancels. **They differ exactly when the perturbed row is itself the inflation assumption**, which is why they're kept separate rather than collapsed into a single "%" option. Relative Δ is reported as blank (not ∞) when the base's net worth at that date is within $1,000 of zero.

The CLI default is CPI-constant dollars. Across a 20-year horizon a nominal swing is mostly the inflation assumption itself, which hides the ranking you're looking for.

### 5. Provenance: `ensemble_manifest.mode`

`ensemble_manifest` gains `mode TEXT NOT NULL DEFAULT 'monte_carlo'` (`'monte_carlo'` or `'sensitivity'`). `ensemble_draw` gains `quantile REAL`, which is NULL for a random draw, the override quantile for an OAT-perturbed row or factor (§2), and NULL for a row resolved to `central()` or moved to a conditional median by a perturbed factor. It also gains `distribution TEXT`, the row's `vary_distribution`, which resolving clears from `calendar_event`; without it a batch couldn't say how a row was drawn (schema 28; NULL in older files). `ensemble_manifest.seed` becomes nullable: NULL for a sensitivity member, which draws nothing, rather than a made-up seed that would suggest it was drawn. A sensitivity member is self-documenting in the same way an ADR-006 member is.

`batch_manifest.json` gains `mode`, plus `quantiles` and `rows` (the perturbed rows and factors) for a sensitivity batch. It's written provisionally as soon as the batch directory is reserved, and rewritten with the failures at the end, so an interrupted batch still reads as the right mode. `batch_mode` also falls back to the members' own `ensemble_manifest.mode` when there's no batch manifest. Both kinds of batch live side by side under `state/runs/<recipe>/<timestamp>/`. `latest_ensemble_batch` gains an optional `mode` filter. `sim ensemble summary` and the fan chart use `mode='monte_carlo'`, because a fan chart over an OAT batch is meaningless. `sim ensemble sensitivity` takes the latest batch of either mode unless `--batch`/`--mode` says otherwise.

### 6. Reports

#### 6.1 OAT

One table per reporting year, one row per perturbed varying row or latent factor, sorted by `impact` descending:

| column | meaning |
| --- | --- |
| `name`, `target` | the row, and whether its `date` or `amount` was moved; `factor` for a latent factor, whose value is its score (`z=+1.28`) |
| `value@q` for each quantile | the resolved values, in display units (dates, dollars, percent), not storage units |
| `Δ@q` for each quantile | metric(row at q) − metric(base), signed |
| `swing` | Δ at the highest quantile − Δ at the lowest. Signed, for direction only |
| `impact` | max(0, every Δ) − min(0, every Δ): the spread of the row's outcomes *including the base*. This is the importance measure |
| `share` | impact / Σ of the ordinary rows' impacts, at that year |
| `flags` | `asymmetric`, and `bend` (with a ladder of ≥ 4 quantiles) when the response changes slope sharply between rungs (§3). `incomplete` when a member for one of the batch's quantiles failed, and `undefined` when a relative Δ is blank because the base is near zero: such a row's swing, impact and share are blank, never zero, so it can't be read as negligible or become a pin candidate |

**Factor rows** sit in the same table, ranking and grid as ordinary rows, sorted by impact with them and labeled `(factor)`, so "the inflation regime as a whole" can be compared directly with any single row. A factor's effect reaches the outcome only through its rows, so its impact is left out of the `share` denominator, which would otherwise count that effect twice. Its own share is over the ordinary rows' total, so it can exceed what any one row has and, rarely, 100%. When the ordinary rows' total is zero (a batch that perturbed only factors, or rows that only matter jointly), shares are over the factors' total instead. A factor is never a pin candidate, because it has no literal value to pin.

Importance is `impact`, not `|swing|`. A row whose low and high quantiles both move the outcome the same way (+$100k at p10 and +$100k at p90, say: a non-monotonic response with the base at a trough) has a swing of zero, but it plainly matters. Ranking, share and pin candidates by swing would rank it last and could drop it. Including the base's zero in the spread also means a one-sided row is measured from where it actually starts.

By default the CLI prints a few years: the first full year, every fifth year, and the final year (`--years 2030,2040,…` overrides this). It also prints one compact **rank-by-year** grid with rows as parameters, columns as reporting years, and cells as rank (or share). A parameter whose rank climbs or falls sharply across the horizon is the time-dependence this ADR exists to surface.

**Attribution: why a parameter matters, not just how much.** The model's structure is known, so a perturbed member's Δ can be decomposed *exactly* rather than estimated statistically. Every balance change in a run file is a double-entry `transfer_event` between an asset or liability account and an income or expense account. Growth is no exception: it posts from the built-in `GROWTH_COUNTERPARTY_ACCOUNTS`, such as `capital_gains` and `deferred_gains`. So for any reporting year Y:

```text
Δ net worth(Y) = Σ over income/expense accounts c of [cumulative net flow_c(Y) in perturbed − in base]
```

Opening balances are set directly in lot state rather than posted as transfers (ADR-005 §6). They're identical between the two members, so they cancel. `reason_kind='ytd'` and `'ytd_rule'` events record pre-simulation history without moving balances, so they're excluded from the sums. The report breaks `Δ@q` down along one of three axes:

| axis | rows | answers |
| --- | --- | --- |
| `category` (default) | income/expense `subkind`: `salary`, `ss_benefit`, `capital_gain`, `interest`, `taxes_federal`, `living`, … | "a later retirement date: +salary, +compounding, −taxes, −SS (delayed)" |
| `flow_account` | individual income/expense accounts | the same, one level finer: which salary, which growth counterparty |
| `holding_account` | asset/liability balance Δ by account | where the money ends up, or which fund is drained sooner. This is where a fund-exhaustion transition is visible directly |

For the two flow axes, an `unattributed` remainder (Δ net worth minus the sum of the rows) is computed and shown whenever it's non-zero. By the identity it should always be zero, so a non-zero value is a ledger bug worth knowing about, not a modeling result. **CPI-constant attribution stays exact:** each member's cumulative flows at Y are deflated by *that member's own* CPI at Y, the same deflator its net worth uses. When the inflation rate is the perturbed row, the base and the member deflate the *same* opening balance by different CPI paths. That difference gets its own `opening_balances` row ("what higher inflation did to the purchasing power of what you started with"), so the rows still sum to the CPI-constant Δ. In nominal dollars, and for any other row, it's zero and omitted. A cent or two of residue from the stored CPI-constant rounding is treated as zero (`UNATTRIBUTED_TOLERANCE`).

Attribution works for a factor the same way: its member's Δ decomposes exactly, whichever of its rows moved. The CLI shows attribution for one row or factor at a time (`--attribute NAME [--by category|flow_account|holding_account]`), because a full table for every row is too much for a terminal. `--relative` divides by the base's net worth that year, as in the notebook. `--measure min_net_worth` is rejected with `--attribute`: attribution decomposes net worth itself, and a running minimum has no flow decomposition. The notebook (§9) is the main interface for it.

#### 6.2 Correlation over a Monte Carlo batch

For each varying row and each reporting year, the report gives the Spearman rank correlation `ρ` between the row's `ensemble_draw.drawn_value` and net worth across succeeded members. Member 0 is excluded because it isn't a draw (ADR-006 §9). It uses Spearman rather than Pearson because outcomes are often monotonic but not linear in a parameter. The implementation is pandas `rank().corr()`, so no new dependency.

**Each ρ carries a 95% confidence interval** (Fisher z-transform, using the Spearman variance correction `1.06 / (n − 3)`). The interval is what makes this usable for pruning: a row is reported as a **pin candidate only when the upper bound of its `ρ²` interval is below the threshold** (`--negligible`, default 5%) at *every* reporting year, **and that year's R² meets the trust threshold (§6.2 below)**. "Small but too noisy to tell" is reported as *inconclusive*, never as negligible, and so is "small, but R² is too low to trust a marginal correlation". So is a row with no estimable correlation in any year (too few members, or a draw that never varies): every row gets a status. `--negligible` must be strictly between 0 and 1. A row must be negligible at every year, not just at the terminal year, because a parameter that only matters in the short term still matters.

This is what sets the member count. The CI's half-width on ρ near zero is roughly `1.96 × √(1.06 / (n − 3))`:

| members | ± on ρ | smallest `ρ²` you can call negligible |
| --- | --- | --- |
| 30 | 0.39 | none in practice |
| 100 | 0.20 | ~5% |
| 200 | 0.14 | ~2% |
| 400 | 0.10 | ~1% |

A fan-chart-sized batch (30–100) is too small to prune with any confidence. **Pruning needs ~100 members at the 5% threshold, and 200+ for finer distinctions or for `k ≥ 5`**, where each parameter's share is naturally smaller. The report states the member count and warns when it's too small for the requested threshold. `docs/guides/ensembles.md` gains a paragraph saying so when this ships, so the guide's existing fan-chart sizing table isn't mistaken for correlation sizing.

**Standardized rank-regression coefficients (SRRC) and R².** Alongside each per-row ρ, the report fits one multiple regression per reporting year: rank-transformed, standardized net worth regressed on all rows' rank-transformed, standardized draws (`numpy.linalg.lstsq`; numpy is now declared as a direct dependency rather than relied on through pandas). The two outputs serve different purposes:

- **SRRCᵢ** is each parameter's weight on the outcome, holding the others fixed. Because the draws are independent by construction (ADR-006 §3), SRRCᵢ ≈ ρᵢ and SRRCᵢ² approximates parameter i's share of the outcome variance.
- **R²** is the single most useful number in this report: *how much of the outcome's variation is explained by monotonic, additive parameter effects at all.* It's **adjusted** R² (`1 − (1 − R²)(n − 1)/(n − k − 1)` for n members and k rows), because in-sample R² climbs toward 1 as k approaches n whatever the outcome, which would let noise pass the trust gate. With no residual degrees of freedom (n ≤ k + 1) it's undefined, and the year is untrusted.
  - **R² ≥ 0.7:** the per-row rankings and pin candidates can be trusted.
  - **R² below that:** a substantial part of the outcome comes from interactions or transitions that no per-parameter number captures. The report says so, and suggests the OAT ladder (§3) or Morris (deferred). **No row can be a pin candidate in such a year.** With a pure interaction (outcome = x₁·x₂), each row's marginal ρ is near zero even though together they decide the outcome, so a small ρ is no evidence that a row doesn't matter.

  R² is per year, so it can be high in the long run and low around a transition year, or the reverse.

Pin-candidate status (above) uses ρ's confidence interval, not SRRC's, gated on R². With independent draws they agree to within sampling noise, and ρ's interval is the simpler, better-understood one.

### 7. CLI

```sh
sim run <recipe> --sensitivity [--only a,b] [--quantiles 0.1,0.9] [--jobs N]
sim ensemble sensitivity <recipe> [--batch B] [--mode sensitivity|monte_carlo]
                                  [--dollars cpi_constant|nominal] [--relative]
                                  [--years 2030,2040] [--negligible 0.05]
                                  [--attribute NAME [--by category|flow_account|holding_account]]
```

`--sensitivity` on a scenario with no unresolved `vary_*` rows is an error ("nothing to perturb"), not a silent single run. `--sensitivity` together with `--ensemble-size` is also an error, because an OAT batch's size is always `1 + k × m`, where `k` counts the swept factors (§2) as well as the rows.

### 8. Parallel members

`sim run` (both modes) gains `--jobs N`. It defaults to 0, one worker per CPU available to the process (`os.process_cpu_count()`, which honors CPU affinity), since members are independent and a batch is otherwise idle-bound on one core (#42; it was 1 when first added). `--jobs 1` keeps the in-process path with no pool. Members are dispatched to a `concurrent.futures.ProcessPoolExecutor`, with each worker running `resolve_member` and then `run_scenario` for one member. Members share no files: each has its own intent and run file, and the recipe intent is only read. Batch-directory reservation and `write_batch_manifest` stay in the parent process. Progress lines are printed as members complete (not in index order), and the final summary is sorted by index.

`run_scenario`'s provenance step (`provenance.py`) doesn't modify the worktree or its index. Its `write-tree` uses a private temporary `GIT_INDEX_FILE`, and the tree objects it writes are content-addressed and safe to write concurrently. Concurrent members therefore don't contend for anything in git.

This is sequenced **after** OAT and reporting (§9). It doesn't change any result, and OAT batches are small. It's what makes 200+-member correlation batches practical to iterate on.

### 9. Notebook: `notebooks/sensitivity.ipynb`

This is a separate notebook from `ensemble.ipynb`. It reuses existing pickers, and adds new ones to `notebook_ui/pickers.py` for reuse elsewhere:

- `DollarModePicker` (existing): nominal / CPI-constant.
- **`DeltaScalePicker`** (new, `ToggleButtons`): absolute Δ / relative Δ (%). Together with `DollarModePicker` this gives all four combinations from §4 in two controls, with no scrolling.
- **`ParameterPicker`** (new, `Dropdown`): the batch's varying rows by `name`, labeled with target and range ("`fund_growth` — amount, 2.5%–5.5%").
- **`ReportYearPicker`** (new, `SelectionSlider` over the batch's reporting years): the year a tornado or ranking is drawn at.
- **`HeatmapValuePicker`** (new, `ToggleButtons`): share / signed swing for the overview heatmap.
- **`AttributionPicker`** (new, `ToggleButtons`): category / flow account / holding account (§6.1).

Widgets (`notebook_ui/sensitivity_widgets.py`):

0. **Parameters** (first, for either kind of batch): `sensitivity.parameters` lists the batch's varying rows. For each it gives what the row acts on, which column varies, the distribution, range, center (the base case, its median), its shape (a truncated normal's SD and any skew or tail weight, a triangular's mode), the implied moments (mean, SD, skewness, excess kurtosis, #31), and the author's description. A sensitivity batch adds the value used at each quantile, and a Monte Carlo batch adds the range actually drawn. It's read from the batch's own member files, so it describes what was run, not the recipe as it stands now.
1. **Overview heatmap** (the first chart): parameters × reporting years, latent factors labeled `(factor)` here and in the tornado, with rows sorted by peak share. A `HeatmapValuePicker` (new, `ToggleButtons`) chooses between share, which shows who dominates when, and signed swing, which uses a diverging scale centered on zero and responds to the units pickers. One glance shows which parameter matters when; the widgets below drill into one parameter or one year.
2. **Tornado at a year:** `ReportYearPicker` + units pickers. Bars are `Δ@q_lo`/`Δ@q_hi` per parameter, sorted by impact at the selected year. Dragging the slider shows the ranking reshuffle over time.
3. **Δ over time for one parameter:** `ParameterPicker` + units pickers. One line per quantile (Δ against reporting year), with a zero line. A fund-exhaustion plateau, a retirement-date step, or a compounding growth-rate curve each read directly off this chart.
4. **Response curve** (only for a ladder batch): `ParameterPicker` + `ReportYearPicker`, plotting Δ against quantile. This is where a transition inside a range is visible as a step.

5. **Attribution** (§6.1): `ParameterPicker` + `ReportYearPicker` + units pickers + **`AttributionPicker`** (new, `ToggleButtons`: category / flow account / holding account). A stacked bar per quantile shows `Δ@q` decomposed into its rows, with the total marked, plus `unattributed` in a warning color if it's ever non-zero. Placed directly under widget 3, so "this parameter starts mattering in 2034" is followed immediately by "because that's when the brokerage account runs dry." With "% of base" selected, every part is divided by the base's net worth that year, so the bars match the other charts' units.

For a `monte_carlo` batch the same notebook shows the ρ-by-year grid with CI whiskers and pin-candidate / inconclusive markers, using the same `ReportYearPicker`. Above it is an **R²-by-year** strip, so the trust level of each year's column can be read at a glance.

## Consequences

- Positive: no new authoring surface. The rows you'd vary in a Monte Carlo are the rows you analyze, with the same ranges. Annual reporting makes time-dependent importance visible instead of averaging it away. Quantile perturbation keeps swings comparable and turns "the delta crossed a transition" from a silent distortion into a visible finding. OAT batches are deterministic and cheap. Correlation pruning comes with an honest "inconclusive" instead of a false "negligible". `run_scenario`/`open_run_file` remain untouched, as in ADR-006.
- Negative: a parameter has to be authored as a `vary_*` row, with a `name` and a range, before it can be analyzed. The ranges determine the ranking, so a carelessly wide range inflates a row's apparent importance, which is unavoidable and why quantiles default to p10/p90. OAT can't see interactions: two parameters that only matter together can each look small. Correlation pruning needs hundreds of members. Schema changes: three new columns (`ensemble_manifest.mode`, `ensemble_draw.quantile`, `ensemble_draw.distribution`) and a nullable `ensemble_manifest.seed`, across two schema bumps (27, then 28).
- Not attempted: parameters that aren't expressible as `set_rate`/`set_amount`/`one_off_transfer`/`retire_scheduled` rows (opening balances, allocation weights), the same boundary as ADR-006. Automatically pinning negligible rows. Cross-scenario sensitivity summaries. A configurable reporting grid.

## Open questions deferred

- **Morris elementary effects**: an interaction-aware screening method that costs `r × (k + 1)` runs (r ≈ 10–20) and would reuse `overrides` directly, by moving one row at a time from random starting points rather than always from the center. It's the natural next step if OAT and the correlation report disagree about a parameter, which hints at an interaction.
- **`--with-dependents`**: an option for an overridden ordinary row to drag its own descendants to their conditional medians, as a factor does. Factors cover the "whole family" question (§2), and keeping a row's entry as "this row alone" keeps the table unambiguous. Revisit if inserting a factor above a row's dependents turns out to be friction, for instance for a series correlated with itself over time.
- **Sobol indices**: a full variance decomposition that separates each parameter's solo effect from its interaction effects. It costs `N × (k + 2)` runs, with N in the hundreds or more, so it's only practical after `--jobs` and only for small `k`.
- **Ad-hoc perturbation of literal rows** (`--perturb name=±10%` without marking the row varying): convenient, but it reintroduces the fixed-delta problems in the Context section. Revisit if marking rows varying turns out to be friction in practice.
- **Trajectory PCA + regression, and PLS:** treat each Monte Carlo member's vector of year-end net worth as one observation. PCA across members finds the dominant *shapes* of variation, typically an overall level (lifetime income − expenses) and an early-versus-late timing component. Regressing each member's component scores on its draws then says which parameters drive the level and which drive the timing. Partial least squares does both steps at once, finding input-weighted axes that best explain the output vector. Both are promising complements to the heatmap; worth judging once real batches exist. PCA or ICA on the *inputs* is not: the inputs are independent draws by construction, so there's no structure to find, and ICA would at best rediscover the `vary_*` rows already recorded in `ensemble_draw`.
- **Decision-variable sweeps** (a retirement date at every quarter across a range): a "which choice is best?" question rather than "which uncertainty matters?". A ladder (§3) is already most of the mechanism; what's missing is presenting it as a choice rather than an uncertainty.
