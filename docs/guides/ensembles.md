# Ensembles: ranges of outcomes, not one number

An ordinary scenario gives you one prediction: this rate, this date, this
amount, run forward to one result. An **ensemble** asks the question "what
if I'm not sure about that rate/date/amount — what's the *spread* of
outcomes if it's actually somewhere in a range?" It generates many variants
of one scenario, each with a chosen value or two redrawn from a
distribution, runs all of them, and shows the range (mean, median, standard
deviation, percentile bands) instead of a single figure.

This is a practical how-to. For the full design and column reference, see
[ADR-006](../architecture/ADR-006-ensemble-scenarios.md).

## Setting one up

**1. Create the recipe**, derived from an existing scenario (usually your
main one):

```sh
sim ensemble create inflation_1 base
```

This prints a seed and a default member count (`30` unless you pass
`--size`) and records both on the new scenario — `sim run`/`sim all` will
use them without you having to repeat `--ensemble-size` every time.

**2. Mark one or more calendar rows as varying.** An ensemble recipe is an
ordinary scenario until at least one `calendar.csv` row has
`--vary-distribution` set. Each varying row is one **dimension** of the
ensemble (more on why that matters below). Two examples:

A growth rate drawn from a range instead of fixed at one value:

```sh
sim calendar add set-rate 2027-01-31 mutual_fund_retirement 4.0 \
  --scenario inflation_1 --name fund_vary \
  --vary-target amount --vary-distribution truncated_normal \
  --vary-low 2.5 --vary-high 5.5
```

A retirement date uncertain within a year:

```sh
sim calendar add retire-scheduled 2029-06-30 salary_bob \
  --scenario inflation_1 --name retire_bob_vary \
  --vary-target date --vary-distribution uniform \
  --vary-low 2029-01-01 --vary-high 2029-12-31
```

`sim calendar add <kind> ...` validates the row by loading the whole
scenario before it commits the write (reverting it if it doesn't load), so
a bad combination — wrong series name, a range that doesn't make sense —
is caught immediately rather than at `sim run` time. See ADR-006 §2 for
every `vary_*` column and what each distribution needs.

**Choosing a distribution.** Three are available:

- **`uniform`** (`--vary-low`, `--vary-high`): anything in the range is
  equally plausible. Use it when you really have no idea where in the
  range the value will land.
- **`triangular`** (`--vary-low`, `--vary-mode`, `--vary-high`): the
  three-point estimate, "most likely X, no less than A, possibly as much
  as B". It's the one to reach for with a **date** that will probably land
  on time but could slip: a home sale that most likely closes in June,
  can't close before April, but could drag on to December. It's also the
  only skewed choice for a date. It suits a one-off amount you'd estimate
  the same way, like a roof replacement: "about $25k, at least $18k,
  maybe $40k".
- **`truncated_normal`** (`--vary-median`, `--vary-sd`, optionally
  `--vary-low`/`--vary-high`): values cluster around a center and thin out
  with distance. This suits rates. `--vary-median` is the center, and the
  base case is exactly that value. `--vary-sd` is the spread. Bounds you
  leave out default to 3 SDs out. Two optional shape controls:
  - `--vary-skew`: positive gives a longer high tail ("usually about 6%,
    but a big year is more likely than a terrible one"), negative a longer
    low tail. 0.3 to 1 is a noticeable but sensible skew.
  - `--vary-tailweight`: below 1 makes extreme values more likely than a
    normal would ("fat tails", like market returns), above 1 less likely.
    0.7 to 0.9 is noticeably fat-tailed.

  With either one set, `--vary-sd` is required. The notebook's
  **Parameters** table and ADR-007's parameters view show the mean, SD,
  skewness and excess kurtosis your settings imply, so you can check that
  they mean what you intended.

`vary_mean` was renamed to `vary_median` (it's the median after the
bounds clip the distribution, so the base case is exactly what you wrote).
A `calendar.csv` that still has a `vary_mean` value fails to load with a
message saying so; rename the column.

**3. Run it:**

```sh
sim run inflation_1
```

`sim run` detects the unresolved varying rows and generates+runs every
member instead of a single run, under `state/runs/inflation_1/<timestamp>/
member_NNNN.sqlite3`. Look at the result with `sim ensemble summary
inflation_1` (a terminal table) or `notebooks/ensemble.ipynb` (the Monte
Carlo "fan chart" — mean/median with the min–max range shaded behind them).

### Member 0 is always a deterministic base case

Every ensemble's `member_0000` resolves each varying row to its
distribution's central value, its median (a `uniform` row's midpoint, a
`truncated_normal` row's `vary_median`, a `triangular` row's median)
rather than a random draw. Every other
member draws as usual. This gives every ensemble one always-present,
always-in-bounds "what if the uncertain inputs landed right in the
middle" run — useful on its own, and it's what any tool that only knows how
to open one run per scenario (a notebook's scenario picker, for instance)
will show you if you point it at an ensemble recipe instead of an ordinary
scenario.

It's a deliberate, small statistical trade-off: member 0 isn't actually a
random draw, so a statistic computed across *every* member is very
slightly biased toward the center compared to the same number of fully
random draws. At the member counts this guide recommends (tens), that bias
is negligible next to the sampling noise you'd have anyway. See
[ADR-006 §9](../architecture/ADR-006-ensemble-scenarios.md) if you want the
full reasoning.

## How many members?

There's no single right answer — it depends on what you're trying to learn
and how much you're willing to wait — but one thing matters more than
anything else:

**The number of varying rows (dimensions) matters more than the number of
members alone.** One varying row sampled 30 times covers that range
reasonably densely. Five *independent* varying rows sampled 30 times each
cover a five-dimensional space far more thinly — the same member count
spreads across a much bigger combined set of possibilities, so your mean/
SD/percentile bands get noisier for the same effort. If a recipe has
several independent varying rows, lean toward a bigger `--size` than you
would for just one.

Rough starting points:

| Varying rows | Reasonable member count |
| --- | --- |
| 1 | 30 (the default) is fine for a first look |
| 2–4 | 50–100, especially if you care about the percentile bands rather than just the mean |
| 5+, or you need a stable tail estimate | 100+, and expect it to cost proportionally more wall-clock time |

**Cost scales linearly with member count** — each member is a full,
independent simulation, with its own intent file and run file (ADR-006's
own "Consequences" section). Time one ordinary run of the parent scenario
(`sim explain <runfile>` after an ordinary `sim run`, or just watch `sim
run` itself) and multiply by the member count you're considering to gauge
what an ensemble run will cost.

`--jobs N` runs members in parallel (`--jobs 0` uses one per CPU). The
members are independent, so the results are identical to a sequential
run; only the order of the progress lines changes. Divide the estimate
above by roughly the number of cores you give it.

**A quick way to tell if you need more:** run the ensemble, look at
`notebooks/ensemble.ipynb`'s fan chart, then rerun it with a bigger
`--ensemble-size`. If the bands move noticeably, the first count wasn't
enough yet to settle down; if they barely change, you've already got
enough members for what you're looking at. For a fast exploratory check
where you mainly want "is this even in the right ballpark," the default of
30 is usually enough; save a bigger, slower run for when you're ready to
report a specific number (a percentile, a probability of running out of
money by some date) rather than just eyeballing a chart.

## Which varying rows matter, and when? (sensitivity)

Before you run a big Monte Carlo, it's worth asking which of your
uncertain inputs actually move the outcome. A one-at-a-time sensitivity
run answers that cheaply, using the same recipe and the same varying rows
([ADR-007](../architecture/ADR-007-sensitivity-analysis.md)):

```sh
sim run inflation_1 --sensitivity
sim ensemble sensitivity inflation_1
```

`--sensitivity` runs one base member (every varying row at its central
value, the same as an ensemble's member 0) plus one member per row per
quantile. Each row is moved to the 10th and 90th percentile of its own
distribution by default. That's `1 + 2 × rows` runs, and no randomness.
Moving every row by an *equally plausible* amount, rather than a fixed
±10% or ±6 months, is what makes the rows' effects comparable.

The report shows, for a few years across the horizon (`--years` to pick
your own):

- each row's change in net worth at its low and high quantile; its
  **swing** (high minus low, for direction); its **impact** (how far its
  outcomes spread, counting the base, so a row that moves things the
  same way at both ends still counts); and its **share** of the total
  impact;
- a **rank-by-year** grid, because importance changes over time. A
  retirement date matters from that date on; a growth rate compounds, so it
  matters little early and a lot late; a fund's return stops mattering when
  the fund runs out;
- **pin candidates**: rows whose share stays under 5% (`--negligible`) in
  every year. They're candidates to turn back into ordinary literal rows
  before your Monte Carlo run.

Useful options:

- `--relative` reports changes as a percentage of the base. `--dollars
  nominal` switches from today's (CPI-constant) dollars.
- `--measure min_net_worth` looks at the lowest net worth up to each year
  ("how close do I get to running out") instead of net worth at year-end.
- `--attribute NAME` breaks one row's effect down exactly: by income/expense
  category (the default, e.g. salary vs. taxes vs. growth), by individual
  income/expense account (`--by flow_account`), or by where the money ends up
  (`--by holding_account`, which shows a fund running dry sooner).
- `--quantiles 0.1,0.25,0.4,0.6,0.75,0.9` gives a response curve instead of
  two points, for spotting a threshold *inside* a range (a date crossing a
  tax year, an amount that exhausts an account). Use it with `--only NAME`
  on the rows that look interesting; it costs 6 runs per row instead of 2.
  The report flags `asymmetric` (low and high effects very different) and,
  with a ladder, `bend` (a sharp change of slope between steps).

One-at-a-time can't see two rows that only matter *together*. The Monte
Carlo report below can, partly.

## Which varying rows can I drop? (Monte Carlo correlation)

`sim ensemble sensitivity` on a Monte Carlo batch (`--mode monte_carlo`,
or `--batch` naming one) reports, per year and per varying row, the rank
correlation between the row's draws and net worth, with a 95% confidence
interval, plus each row's regression weight and the year's **R²**:

- R² near 1 means the rows' individual effects explain the outcome, and the
  ranking can be trusted. Below 0.7, interactions or thresholds matter, and
  per-row numbers understate them. Look at those rows one at a time with a
  quantile ladder.
- A row is marked **pin candidate** only when even the *top* of its
  confidence interval is negligible in every year. A row that's small but
  too noisy to be sure about is **inconclusive**, not negligible.

**This needs many more members than a fan chart.** The interval is wide
at small member counts, so a 30-member ensemble can't call anything
negligible:

| Members | Smallest share you can call negligible |
| --- | --- |
| 30 | none in practice |
| 100 | about 5% (the default threshold) |
| 200 | about 2% |
| 400 | about 1% |

So for pruning, plan on about 100 members, and 200 or more with five or
more varying rows, where each row's share is naturally smaller. The report
warns when the batch is too small for the threshold you asked for.
