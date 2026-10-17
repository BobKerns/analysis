# User Guide: Building and Editing Scenarios

> To read a formatted `.md` file:
> - In Jupyter: right-click on it and select `Open With / Markdown Preview`
> - In VSCode: Command `Markdown Preview Enhanced: Open Preview` or ⌘⇧V

This guide shows how to make common changes to a scenario, step by step, with
real examples, and how to look at what a run produced. It assumes you already
have a data repo. If you don't, see [templates/README.md](../templates/README.md) first.

For the reasons behind these designs, see the documents in
`docs/architecture/`. [ADR-003](architecture/ADR-003-registries-and-csv-schemas.md) lists every CSV column in detail. This guide
does not repeat all of that; it points to [ADR-003](architecture/ADR-003-registries-and-csv-schemas.md) when you need the full list.

## 1. Scenarios build on each other

A **scenario** is one full model of your household finances. It has accounts,
balances, income, expenses, and rules that move money between accounts. It
runs from a start date to an end date.

You do not build every what-if scenario from scratch. One scenario is your
main model. By convention, it is named `base`. Every other scenario is
**derived** from `base`, or from another derived scenario. A scenario says
which one it derives from in its own `scenario.csv`, using the
`parent_scenario` column.

A derived scenario only needs to list what is different. Its CSV files
(`accounts.csv`, `account_values.csv`, `scheduled.csv`, `calendar.csv`, and
others) are combined with its parent's files. Each row has a key, such as a
name, or an account plus a lot. If you add a row with a new key, it is added.
If you add a row with a key that already exists in the parent, it replaces
the parent's row. Anything you do not mention stays the same as the parent's.
If you later change `base`, every scenario that does not override that part
picks up the change automatically, the next time it loads.

```mermaid
flowchart TB
    base["base\n(your main model)"]
    cut["current-cut\noverrides: SS benefit cut in scheduled.csv"]
    hsa["hsa_2027\noverrides: one HSA account_values row"]
    sell["sell_stock\noverrides: a calendar.csv row"]
    base --> cut
    base --> hsa
    base --> sell
```

Each box above only shows what it changes. Everything else comes from `base`.
A fix to `base` reaches every scenario below it, the next time that scenario
loads.

A scenario can also combine several others. List more than one parent in
`parent_scenario`, separated by colons: the main parent first, then
**mix-ins**, such as `base:current-cut:hsa_2027`. The main parent is loaded in full.
Each mix-in then adds its own changes on top, stopping at the first scenario
already included (here, `base`), so a mix-in derived from `base` contributes
just what it changes. When two mix-ins change the same row, the later one
wins. Making the combination sensible is up to you.

Create a derived scenario with:

```sh
sim scenario create <name> <parent> [<mixin> ...]
```

This creates the new scenario's folder, with empty CSV files ready for you to
add rows to, and a `series/` folder (with a short `README.md`) for any growth
or inflation series it changes. It checks that the scenario loads, and makes it your current
scenario. You then add only the rows that need to be different — the rest of
this guide shows what to put in them.

You can also start from what the scenario inherits: `sim account edit`,
`sim account lot edit`, `sim scheduled edit` and `sim calendar edit` (and
`sim calendar tag add|rm`) on an inherited item write the edited row into
the derived scenario as an override, and leave the parent alone. A derived
scenario can't remove what it inherits, so `sim account delete`,
`sim calendar delete` and `sim person lifespan clear` refuse an inherited
account, row or lifespan (delete it in the scenario that defines it). `sim scheduled delete` on an inherited rule ends
it instead, with an override whose end is the month before the scenario
starts. Deleting the derived scenario's override brings back the inherited
row. Renames always reach every scenario sharing the name.

A scenario made this way is never an ensemble, even when its parent is
one: it runs once, with each varying row at its central value. Use
`sim ensemble create` (§4.10) for an ensemble.

Add a `README.md` in the scenario's own folder to describe what it's for —
its title and first paragraph show up wherever a scenario's description
does (the scenario picker, `scenario_overview.ipynb`), and travel with every
run file, so "what was this scenario for again" is answered without going
back to the data repo. Without a `README.md`, `scenario.csv`'s own
`description` column is used instead.

To see which scenarios the data repo has:

```sh
sim scenario list            # names only, one per line, for scripts
sim scenario list --verbose  # a table with each one's parents, dates, seed, size and description
sim ensemble list            # just the ensembles (also takes --verbose)
```

The verbose table marks your current scenario with `*`, and shows each
scenario's start and end months, seed and ensemble size, in `[brackets]`
when the scenario inherits the value rather than setting it itself. A blank
size means the scenario is not an ensemble.

To rename a scenario (or an ensemble; the two commands are the same):

```sh
sim scenario rename <old> <new>
```

This moves its folder and repoints everything that names it: the scenarios
built on it (as parent or mix-in), your current scenario, and links to it
in the data repo's `README.md` files. Runs you already made keep the old
name.

Renaming anything else a scenario can share works the same way: the
`rename` commands for accounts, chains, scheduled rules, calendar rows,
series and lots change the name in every scenario that shares it, not just
the one you name, so a scenario built on another never ends up naming
something that is no longer there:

```sh
sim account rename <old> <new>
sim account lot rename <account> <old> <new>
sim chain rename <old> <new>
sim scheduled rename <old> <new>
sim calendar rename <old> <new>      # a row's name, and rows correlated with it
sim series rename <old> <new>        # its series/ files too
```

Everything below works the same way, whether you are editing `base` directly,
or editing a derived scenario's own files.

## 2. Glossary

This is the place to look up any word the guides use. The lists cover the
words this project uses in its own specific way, the simulation and
statistics words the ensemble features rely on, and the general finance/tax
words this project relies on but does not change the meaning of. Where a
project word could be confused with a general word, that is pointed out.

### Words this simulator uses in its own way

| Word | What it means here |
| --- | --- |
| **Scenario** | One full model of your household finances. Stored as a folder under `scenarios/<name>/`. |
| **Derived scenario** | A scenario that has `parent_scenario` set. It starts from its parent (and any mix-ins) and only lists what is different. See Part 1. |
| **Lot** | One holding inside an account. A brokerage account can have several lots: different funds, or shares bought on different dates. Every account has at least one lot. It is named `default` unless you split the account into more than one lot. |
| **Chain** | A named, ordered list of places money can come from, or go to. Think: try this place first, then that place. Every account (and every lot) already has its own one-step chain, automatically. You only write a chain file, `chains/source/<name>.csv` or `chains/destination/<name>.csv`, when you want more than one step. See Part 4.6 and [the chains guide](guides/chains.md). |
| **Series** | A named set of numbers a scenario can refer to by name: a growth or interest rate (`rate__*`), or an inflation projection (`inflation__*`). These files live in that scenario's own `series/` folder. This is a different idea from *asset class*, below. |
| **`aux/`** | Shared tax-law facts every scenario uses unless it says otherwise: tax brackets, RMD tables, filing status. |
| **`series/`** | A scenario's own growth-rate and inflation assumptions. Unlike `aux/`, there is no shared copy at the top of the data repo — each scenario has its own, inherited from its parent the same way `accounts.csv` is. |
| **`prices/`** | Shared security price histories every scenario uses unless it says otherwise. |
| **Itemization** | A CSV file under `itemizations/<name>.csv`. It lists dollar amounts in a `Value` column, and the loader adds them up into one total. Other files can use that total by writing `@name` instead of a plain number (for example `opening_cost_basis = @home_basis`). **This is a different thing from an *itemized deduction*, a tax term — see the next table.** |
| **`tax_character`** | A label on a money movement that says how it is taxed: `ordinary`, `ltcg`, `stcg`, `tax_free`, `tax_deferred_out`, `return_of_capital`, `none`, and a few others. This label decides which tax bucket the movement is counted in. |
| **Asset class** | Used only for the portfolio overview (`sim allocation`): `stock_us`, `stock_intl`, `stock` (when the US/international split is not known), `bond`, `cd`, `cash`, `real_estate`, `other`. Set for each symbol in `allocations.csv`. Not related to `tax_character`. |
| **Calendar event** | One dated change in `calendar.csv`. It can change a rate, change an amount, end a rule, or make a one-time transfer. See Part 4.8. |
| **Scheduled transaction / rule** | One row in `scheduled.csv`. It is a rule that moves money, either on a regular schedule or just once. |
| **Debit / credit** | The two sides of one money movement. A **debit** takes money out of a lot: a payment, a withdrawal, or the "from" side of any transfer. A **credit** puts money into a lot: the "to" side. For an asset lot, a debit lowers the balance and a credit raises it. For a liability (a loan or a card), a debit is borrowing more, so what you owe goes up, and a credit is paying it down. The refill check runs before each debit, which is why the chain flowcharts label refill "before a debit". These are everyday banking senses, not the bookkeeping ones, where debiting an asset account *raises* it. |
| **Refill / overflow** | A lot's own rule for keeping its balance in a range. If a payment would drop it below `minimum_balance`, money is drawn from `refill_chain` first. If it goes above `maximum_balance`, the extra is swept into `overflow_chain` at the end of the month. Each lot can have its own setting. |
| **RMD subject account** | The IRA or 401(k) account a Required Minimum Distribution is calculated from. If that account has more than one lot, you must name the account in `params_json`, and `from_account` must be a chain that lists only that account's lots. |

### Simulation words used by ensembles

These come up in [Ensembles](guides/ensembles.md); [How the simulator draws
random futures](guides/simulation-basics.md) explains them from the
beginning.

| Word | What it means here | Learn more |
| --- | --- | --- |
| <a id="g-monte-carlo"></a>**Monte Carlo** | Answering a question about uncertainty by drawing many random cases, working each one out, and looking at the spread of results. Named after the casino. | [Wikipedia](https://en.wikipedia.org/wiki/Monte_Carlo_method) |
| <a id="g-ensemble"></a>**Ensemble** | Many copies (**members**) of one scenario, each with its uncertain inputs drawn differently, run to show the spread of outcomes. The scenario that defines the ranges is the **recipe**. | [Ensembles](guides/ensembles.md) |
| <a id="g-member-0"></a>**Member 0** | The member with every uncertain input at its median: the ensemble's one deterministic base case. | [Ensembles](guides/ensembles.md#member-0-is-always-a-deterministic-base-case) |
| <a id="g-sensitivity-run"></a>**Sensitivity run** | A batch that moves one uncertain row (or factor) at a time to chosen percentiles while every other row stays at its median, to see what that one input does. | [Ensembles](guides/ensembles.md#which-varying-rows-matter-and-when-sensitivity) |
| <a id="g-prng"></a>**Pseudo-random number generator** | A deterministic algorithm whose output behaves like random draws. Started from the same seed, it produces the same sequence every time. | [Wikipedia](https://en.wikipedia.org/wiki/Pseudorandom_number_generator) |
| <a id="g-seed"></a>**Seed** | The number a recipe's random draws all start from. The same seed reproduces the same members exactly. | [Random numbers come from a seed](guides/simulation-basics.md#random-numbers-come-from-a-seed) |
| <a id="g-rng-stream"></a>**RNG stream** | The separate random-number sequence each uncertain row gets, keyed by the seed, the member and the row's name. Adding or changing one row never shifts another row's draws, so comparisons between scenarios show the change, not fresh noise. | [Why every row gets its own stream](guides/simulation-basics.md#why-every-row-gets-its-own-stream) |
| <a id="g-common-random-numbers"></a>**Common random numbers** | Giving two scenarios being compared the same random draws, so the difference between them is the change you made rather than luck. Streams and a shared seed provide it. | [Why every row gets its own stream](guides/simulation-basics.md#why-every-row-gets-its-own-stream) |
| <a id="g-latent-factor"></a>**Latent factor** | A named shared influence ("core inflation") that rows correlate with, but that isn't itself a value in the model. It has no units: in each future it says only how high or low things run, as a percentile. | [Ensembles](guides/ensembles.md#correlated-parameters) |
| <a id="g-correlation-tree"></a>**Correlation tree** | Rows and factors linked by `vary_correlate_with`, each with one parent and a correlation ρ to it. The top of a tree is its **root**. | [Ensembles](guides/ensembles.md#correlated-parameters) |
| <a id="g-copula"></a>**Copula** | The rule that ties correlated rows' random draws together without changing any one row's own range or shape. The default **Gaussian copula** makes linked rows move together; the **Student-t copula** also makes their extremes coincide. | [A visual introduction](https://twiecki.io/blog/2018/05/03/copulas/) |
| <a id="g-tail-dependence"></a>**Tail dependence (ν)** | How strongly extremes coincide in a tree using the Student-t copula. Set as `vary_tail_dependence` on the tree's root; lower ν means stronger. | [Ensembles](guides/ensembles.md#joint-extremes-tail-dependence) |

### Statistics words used by ensembles

Mean, median and standard deviation are here too, for completeness. Most
entries link to a section of one of OpenStax's free online statistics
textbooks, which explain each idea with worked examples; [*Introductory
Statistics*](https://openstax.org/books/introductory-statistics-2e/pages/1-introduction)
is the place to start.
Asking an AI assistant is another quick way in: "explain Kendall's tau to
someone new to statistics" works well, and pasting the paragraph where you
met a word gets an answer about how it's used here.

| Word | What it means here | Learn more |
| --- | --- | --- |
| <a id="g-distribution"></a>**Distribution** | The full description of an uncertain quantity: which values can happen, and how likely each is. Every varying row has one, chosen with `vary_distribution`. | [OpenStax ch. 5](https://openstax.org/books/introductory-statistics-2e/pages/5-introduction) |
| <a id="g-density"></a>**Probability density** | The curve drawn over a distribution's range: high where values are common, low where they're rare. The chance of landing between two values is the area under the curve between them, so the total area is always exactly 1 (something has to happen). A wider curve must therefore be a lower one. | [OpenStax §5.1](https://openstax.org/books/introductory-business-statistics-2e/pages/5-1-properties-of-continuous-probability-density-functions) |
| <a id="g-cdf"></a>**Cumulative probability (CDF)** | For a value x, the chance of landing at or below x: the area under the density to the left of x. It climbs from 0 at the bottom of the range to 1 at the top. | [OpenStax §5.1](https://openstax.org/books/introductory-business-statistics-2e/pages/5-1-properties-of-continuous-probability-density-functions) |
| <a id="g-percentile"></a>**Percentile, quantile** | The value with a given share of outcomes below it: 90% of outcomes fall below the 90th percentile. A quantile is the same thing written as a fraction (0.9). Finding a quantile is the CDF run backwards, and it's how the simulator turns a random fraction into a value. | [OpenStax §2.3](https://openstax.org/books/introductory-statistics-2e/pages/2-3-measures-of-the-location-of-the-data) |
| <a id="g-median"></a>**Median** | The 50th percentile: half the outcomes fall below it and half above. | [OpenStax §2.3](https://openstax.org/books/introductory-statistics-2e/pages/2-3-measures-of-the-location-of-the-data) |
| <a id="g-mean"></a>**Mean** | The average. For a lopsided (skewed) distribution it sits away from the median, toward the long tail. | [OpenStax ch. 2](https://openstax.org/books/introductory-statistics-2e/pages/2-introduction) |
| <a id="g-sd"></a>**Standard deviation (SD), variance** | The typical distance of values from their mean. Variance is the SD squared; the variances of independent pieces add up, which is why shares of variation are written as squares (ρ², R²). | [OpenStax §2.7](https://openstax.org/books/statistics/pages/2-7-measures-of-the-spread-of-the-data) |
| <a id="g-uniform"></a>**Uniform distribution** | Every value in a range equally likely: a flat density. The raw random fraction each stream supplies is uniform between 0 and 1. | [OpenStax ch. 5](https://openstax.org/books/introductory-statistics-2e/pages/5-introduction) |
| <a id="g-triangular"></a>**Triangular distribution** | A density shaped like a triangle: zero at the low and high ends, peaking at the most likely value (the **mode**). | [Ensembles](guides/ensembles.md#setting-one-up) |
| <a id="g-binomial"></a>**Binomial distribution** | The number of successes in a fixed number of independent yes-or-no tries, such as heads in 100 coin flips. | [OpenStax §4.2](https://openstax.org/books/introductory-business-statistics-2e/pages/4-2-binomial-distribution) |
| <a id="g-normal"></a>**Normal (Gaussian) distribution** | The bell curve. It appears whenever many small independent effects add up: count the heads in 100 coin flips, repeat many times, and the counts pile into a bell shape (a [Galton board](https://en.wikipedia.org/wiki/Galton_board) shows this with falling beads). About 68% of values lie within 1 SD of the center, 95% within 2 and 99.7% within 3. Its tails thin out very fast: beyond 4 SD is about 1 in 16,000. | [OpenStax ch. 6](https://openstax.org/books/introductory-statistics-2e/pages/6-introduction), [binomial to normal](https://openstax.org/books/introductory-business-statistics-2e/pages/6-3-estimating-the-binomial-with-the-normal-distribution) |
| <a id="g-clt"></a>**Central limit theorem** | Sums and averages of many independent pieces come out close to normal, whatever shape the pieces have. It's why the bell curve is everywhere. | [OpenStax ch. 7](https://openstax.org/books/introductory-statistics-2e/pages/7-introduction) |
| <a id="g-truncated-normal"></a>**Truncated normal** | A normal cut off at a low and a high bound, with the curve raised so its area is still 1. | [Ensembles](guides/ensembles.md#setting-one-up) |
| <a id="g-score"></a>**Score (z)** | A position measured in standard deviations of the standard normal: z = 0 is the median, z = +1.28 the 90th percentile. The simulator links rows by working on their scores, then turns each score back into a value from that row's own range. | [OpenStax ch. 6](https://openstax.org/books/introductory-statistics-2e/pages/6-introduction) |
| <a id="g-skew"></a>**Skew** | Lopsidedness: one tail longer than the other. Positive skew means a longer high tail. | [Ensembles](guides/ensembles.md#setting-one-up) |
| <a id="g-tails"></a>**Tails, heavy tails** | A distribution's far ends, where the extremes live. A heavy-tailed distribution makes extremes more likely than a normal does. | [Ensembles](guides/ensembles.md#setting-one-up) |
| <a id="g-student-t"></a>**Student-t distribution, degrees of freedom (ν)** | A bell curve with heavier tails: what you get from a normal whose spread is itself uncertain, scaled up or down at random. ν sets how heavy the tails are: ν = 1 very heavy, ν = 5 noticeably heavier than normal, above about 30 nearly normal. (Textbooks meet it for small samples; the simulator uses the same curve for joint extremes.) | [OpenStax §8.2](https://openstax.org/books/introductory-statistics-2e/pages/8-2-a-single-population-mean-using-the-student-t-distribution) |
| <a id="g-correlation"></a>**Correlation (ρ)** | A number from −1 to 1 for how strongly two quantities move together: 1 in lockstep, 0 no tendency, −1 opposite. ρ² is roughly the share of one's variation the other accounts for. | [OpenStax §13.1](https://openstax.org/books/introductory-business-statistics-2e/pages/13-1-the-correlation-coefficient-r) |
| <a id="g-persistence"></a>**Persistence, autocorrelation** | The correlation between a quantity and its own value some time later, usually the next year. Near 1, this year's value carries over (interest rates); near 0, each year is a fresh draw (stock returns). | [A century of rates](guides/century-of-rates.md#how-much-does-this-year-predict-next-year) |
| <a id="g-mean-reversion"></a>**Mean reversion** | A tendency for an unusually high or low value to be followed by one closer to the long-run average. An inflation spike is usually followed by a smaller one, then by ordinary years. | [A century of rates](guides/century-of-rates.md#how-much-does-this-year-predict-next-year) |
| <a id="g-rank-correlation"></a>**Rank correlation** | Correlation computed on the values' order rather than the values themselves, so it's unchanged by stretching or reshaping either quantity's range. | [Kendall's τ](https://www.socscistatistics.com/tutorials/kendall-tau/) |
| <a id="g-kendall-tau"></a>**Kendall's τ** | A rank correlation. Take every pair of members: the pair agrees if both rows put the two members in the same order, and disagrees otherwise. τ is the share that agree minus the share that disagree. For the simulator's copulas τ = (2/π)·asin(ρ), so the correlation check reads ρ back from τ. | [Tutorial](https://www.socscistatistics.com/tutorials/kendall-tau/) |
| <a id="g-r2"></a>**R²** | The share of the outcome's variation that a regression on the inputs explains, from 0 to 1. | [OpenStax §12.3](https://openstax.org/books/introductory-statistics-2e/pages/12-3-the-regression-equation) |
| <a id="g-srrc"></a>**SRRC, PRCC** | Two measures of one input's effect on an outcome with the other inputs held fixed, both on ranks. SRRC (standardized rank regression coefficient) is the input's regression weight; PRCC (partial rank correlation) is its correlation with the outcome after removing what the other inputs explain. | [Ensembles](guides/ensembles.md#which-varying-rows-can-i-drop-monte-carlo-correlation) |
| <a id="g-standard-error"></a>**Sampling error, standard error** | A result computed from a limited number of members wobbles from batch to batch. The standard error is the typical size of that wobble. It shrinks with the square root of the count: four times the members halves it. | [OpenStax ch. 7](https://openstax.org/books/introductory-statistics-2e/pages/7-introduction) |
| <a id="g-confidence-interval"></a>**Confidence interval** | A range around a result, built so that across many batches, 95% of such ranges (for a 95% interval) contain the true value. | [OpenStax ch. 8](https://openstax.org/books/introductory-business-statistics-2e/pages/8-introduction) |

### General finance/tax words this tool uses

| Word | What it means, and how this tool uses it |
| --- | --- |
| **Cost basis** | What you originally paid for something (plus the cost of home improvements, for a house). The simulator tracks this per lot, in `opening_cost_basis`, for taxable assets other than cash. It updates this number as money moves in or out. |
| **Capital gain/loss** | The sale price minus the cost basis. **Long-term** gains (`ltcg`, held more than a year) usually get a lower tax rate. **Short-term** gains (`stcg`, held a year or less) are taxed as ordinary income. See Part 3. |
| **RMD (Required Minimum Distribution)** | The smallest amount the IRS requires you to withdraw each year from a pre-tax retirement account, once you reach a certain age. A real tax rule; also the name of a rule kind in this tool (`rmd`). |
| **Traditional vs. Roth** | A Traditional account delays tax until you withdraw money (`tax_deferred_out`). A Roth account is already taxed, so withdrawals come out tax-free (`tax_free`). Set with `tax_treatment` in `accounts.csv`. |
| **Filing status** | `MFJ` (married filing jointly), `Single`, or `MFS` (married filing separately). This tool does not model Head of Household. Filing status changes automatically after a spouse's death, based on `lifespan.csv`; see Part 4.7. |
| **Standard deduction** | A flat amount the IRS lets you subtract from income before tax is calculated. This simulator always uses the standard deduction; it does not yet model choosing to itemize deductions instead. |
| **Itemized deduction** | An IRS alternative to the standard deduction: instead of the flat amount, you add up specific expenses (mortgage interest, large medical bills, and so on) and use that total if it's bigger. See Part 4.9 for how to add one for a given year. This is a different idea from this tool's *itemization* files, above, which just add up a list of dollar amounts for any purpose — the similar name is confusing, but it predates this guide. |
| **Marginal tax bracket** | The tax rate on your *next* dollar of ordinary income. From `federal_brackets.csv` / `state_brackets__CA.csv`. |
| **Provisional income** | The IRS's combined-income number that decides how much of your Social Security is taxable. From `ss_taxation_thresholds.csv`. |

## 3. How tax is handled

Tax is not calculated as each rule fires. Instead, it is calculated once per
tax year, by looking back over everything that happened that year (the
`transfer_event` ledger).

- **Ordinary income** includes: salary, pension, interest, the taxable part of
  Social Security, taxable withdrawals from Traditional accounts
  (`tax_deferred_out`), and short-term gains (`stcg`).
- **Long-term gains** (`ltcg`) use the rates in `aux/ltcg_brackets.csv`, if
  that file exists. These rates apply on top of your ordinary taxable income.
  If the file does not exist, long-term gains are taxed the same as ordinary
  income.
- **Filing status** comes from the `filing_status` aux table for that tax
  year. One exception: MFJ or MFS automatically changes to Single, starting
  the year after a primary person's `final_month` in `lifespan.csv`. You do
  not need to add a matching row to `filing_status.csv` yourself; see Part 4.7.
- **Deductions**: the standard deduction, from `standard_deduction.csv`,
  unless you add itemized deductions for that year — see §4.9. The simulator
  then uses whichever is greater. (See the glossary above — *itemization* and
  *itemized deduction* mean two different things in this guide.)
- **State tax** works the same way as federal tax, using
  `state_brackets__CA.csv` (or your own state's file, in `aux/`).
- **RMDs** are calculated from the account's lot balances on the previous
  December 31, divided by the IRS factor for that age. The factor comes from
  `rmd_factors.csv` and `rmd_start_age.csv`.

**One important thing to know before you model a sale (Part 4.5):** the
simulator can automatically split a withdrawal into "return of capital" and
"gain" (`ltcg` or `stcg`). But it only does this when the withdrawal is drawn
**through a chain**. This includes a lot's own one-step chain, used by a
refill or overflow rule, or a chain file you write yourself. A plain transfer
straight from one account to another — a row in `scheduled.csv`, or a
`one_off_transfer` calendar event that names the account directly, not a
chain — uses whatever `tax_character` you write on that row, exactly as
written. It does not calculate the split for you. This only matters for
taxable, non-cash lots, such as a brokerage account. For cash accounts,
tax-deferred accounts, and tax-free accounts, the correct `tax_character` is
already fixed, so this does not matter.

## 4. Common tasks

### 4.1 Add a new account

```sh
sim account add "Asset_Savings_Emergency" asset cash --owner joint
```

This adds a row to `accounts.csv`. If you add `--opening-balance`, it also
adds a matching row to `account_values.csv`. If you run the command with no
arguments, it asks you questions and shows the valid choices for kind,
subkind, and owner. Afterward, run `sim account check` — it tells you about
anything that still needs your attention. Run `sim account sync` to add any
missing `account_values.csv` rows, with a balance of zero, ready for you to
fill in with `sim account lot edit` (Part 4.2).

### 4.2 Add, change or delete a lot

Every account is really a set of **lots**. Lots can differ in symbol, purchase
date, and cost basis. An account you have not split has one lot, named
`default`.

```mermaid
flowchart LR
    subgraph acct["Asset_Brokerage_Joint"]
        a["lot: growth (symbol AAA)"]
        b["lot: inherited_2029 (symbol AAA, acquired 2029-03-15)"]
    end
```

Each lot is one row of `account_values.csv`. The `sim account lot` commands
add, change, delete and list them, so you don't have to edit that file by
hand (though you still can):

```sh
sim account lot list Asset_Brokerage_Joint
sim account lot add Asset_Brokerage_Joint inherited_2029 \
    --symbol AAA --acquired 2029-03-15 --quantity 500 --opening-cost-basis 50000
sim account lot edit Asset_Brokerage_Joint inherited_2029 --opening-cost-basis 52000
sim account lot delete Asset_Brokerage_Joint inherited_2029
```

Given a price for `AAA` (see below; otherwise add `--opening-balance`), the
`add` above writes this row:

```csv
account_name,lot,symbol,acquired,quantity,series,opening_balance,opening_cost_basis
Asset_Brokerage_Joint,inherited_2029,AAA,2029-03-15,500,,,50000.00
```

- Run `add` or `edit` without the account and lot to be asked instead. `add`
  asks only what applies to the account: a cost basis only for a taxable
  account, and the refill and overflow settings (Part 4.6) only if you want
  them. `edit` lists the lots to pick from, then shows the lot's fields; name
  one to change it, and `write` when you are done. `add` prints the
  equivalent command, to reuse in a script.
- Renaming a lot (`edit --new-lot`, or its `lot` field) also renames it
  everywhere the scenario, and any scenario built on it, names it. `edit`
  won't rename a lot the scenario inherits; `sim account lot rename`
  renames one wherever it is defined. Deleting
  one lists the rows that move money in or out of it, and removes them if you
  say yes. If the lot is a chain's only step, change or delete the chain
  first.
- In a scenario that builds on another (Part 1), `edit` on an inherited lot
  writes a copy of the whole lot, changed, into this scenario, and leaves
  the parent alone. A scenario can't remove a row it inherits, so `delete`
  on an inherited lot sets it to zero here instead. These commands treat
  that zeroed lot as deleted: `list` says so, and `add` can bring the lot
  back. `list` also says which lots are inherited and which this scenario
  changed.

- For an **investment account**, you must always name the lot, everywhere
  the account is used: in the chain files, in `scheduled.csv`'s
  `from_lot`/`to_lot` columns, and in `calendar.csv`'s `lot_1`/`lot_2`
  columns. If you leave the lot blank, the scenario will fail to load, and
  the error message will list the account's real lots.
- You can leave `opening_balance` blank if you give `symbol` and `quantity`
  instead. The balance is then calculated from a price. Run
  `sim prices fetch` to get prices; it saves them to
  `prices/security_price__<SYMBOL>.csv`, and `sim prices show` lists what
  it saved. Run `sim prices check` to compare a
  balance you typed in against quantity times price. Part 4.11 covers
  adding a symbol the data repo doesn't have yet.
- Once you have a run, `sim account holdings <account>` shows every lot's
  balance, cost basis, and unrealized gain.

### 4.3 Change an estimated expense for the whole simulation

If you are editing `base`, or you do not need the old number anywhere else,
just change the number in the existing `scheduled.csv` row — for example, its
`amount` column. Every scenario that does not override that row picks up the
change automatically.

If you are in a derived scenario and want the change to apply only there, add
a row with the **same `name`** as the parent's row. The new row replaces the
whole parent row (rows are matched by `name`). The easiest way is
`sim scheduled edit`, which writes that row for you:

```sh
sim scheduled edit groceries_monthly --amount 650.00   # change one field
sim scheduled edit groceries_monthly                   # or pick fields to change
```

`sim scheduled add --kind <kind>` adds a rule of any kind (a Social Security
benefit, an RMD, a mortgage, ...), asking for what that kind needs, and
`sim scheduled delete` removes one.

### 4.4 Change an estimated expense starting at a later date

Do not add a second row to `scheduled.csv` for this. Instead, add a
`set_amount` event to `calendar.csv`. It changes the rule's amount from that
date onward, without changing anything before that date:

```csv
date,kind,scheduled_name,amount
2032-01-31,set_amount,groceries_monthly,650.00
```

This is exactly what `calendar.csv` is for: a dated, one-time change to a
rule that is otherwise steady. You do not need to work out start and end
dates for a second, competing row.

### 4.5 Selling an asset

There are two real ways to do this today, depending on what you are selling.

**Selling a home**: move the home's whole balance, on the sale date, into
a destination chain whose steps say how each part of the price is taxed.
The first step recovers the home's cost basis, the second takes the IRS
home-sale exclusion, and the third takes the taxable gain. A step only needs
a `tax_character` where the character changes:

```csv
# chains/destination/home_sale.csv
account_name,maximum,tax_character,basis_of
sale_proceeds,,home_sale,Asset_Home
sale_proceeds,500000.00,excluded_gain,
sale_proceeds,,ltcg,
```

```csv
# chains/destination/sale_proceeds.csv: pay off the mortgage, then checking
account_name
Liability_Mortgage
Asset_Checking
```

```csv
# calendar.csv
date,kind,account_1,account_2,description
2030-06-30,transfer_balance,Asset_Home,home_sale,sell the home
```

The basis step (`basis_of` the home, `tax_character` `home_sale`) takes as
much of the price as the home's basis, which needs an `opening_cost_basis` in
`account_values.csv`. Leave its `maximum` and `percent` blank. The exclusion
is the `excluded_gain` step's `maximum`: $500,000 married filing jointly,
$250,000 single, if you meet the IRS ownership and use tests. A mortgage
step takes only what is still owed. If the home sells for less than its
basis, the loss isn't deductible, and nothing more happens. With
`sim chain add` and `sim chain step add`, the columns are `--tax-character`
and `--basis-of`.

The same shape sells an investment through a destination chain: give the
basis step `tax_character` `return_of_capital` and `basis_of` the lot. A sale
below its basis then records a capital loss. The yearly tax calculation nets
it against that year's gains, deducts up to $3,000 from ordinary income
(`aux/capital_loss_limit.csv`), and carries the rest into later years.

A remodel adds to the home's basis: pay it through a destination chain whose
step has `tax_character` `capital_improvement` and `basis_of` the home.

**Selling a brokerage lot** (or anything else that is taxable and not cash):
there is no dedicated "sell" rule for this yet. Model it as a one-time
transfer out of that lot. Route the transfer through a named chain that
lists that lot — even a chain file with just one line — instead of naming
the account directly:

```csv
# chains/source/sell_the_2029_lot.csv
account_name,lot
Asset_Brokerage_Joint,inherited_2029
```

```csv
# scheduled.csv — a one-off row (a calendar.csv one_off_transfer works too)
name,from_account,from_lot,to_account,to_lot,cadence,amount_rule_kind,amount,tax_character,start,end,priority
sell_2029_lot,sell_the_2029_lot,,Asset_Checking,,one_off,fixed_amount,75000.00,none,2030-06-30,2030-06-30,500
```

A `one_off_transfer` event in `calendar.csv` can do the same: its
`account_1`/`account_2` columns may name a chain just as `scheduled.csv`'s
`from_account`/`to_account` can. The chain's name, used in `from_account`, is
its filename without `.csv`, exactly as written: so
`chains/source/sell_the_2029_lot.csv` is the chain `sell_the_2029_lot`. It is
in `source/` because money is drawn from it. A chain name has no spaces. Leave `from_lot` blank — naming a chain already says which lot to
use. Set `tax_character` to `none`: this value is ignored once the money is
drawn through a chain, but the column must still have some value the loader
recognizes; it cannot be left empty. What actually triggers the automatic
return-of-capital/gain split (Part 3) is drawing the money through a chain,
not the `tax_character` value you write. Naming the lot directly, on a plain
transfer, would instead use whatever `tax_character` you wrote, with no
automatic split.

### 4.6 Chains as default policies

A chain is a named, reusable answer to the question "where does this money
come from" (or "where does extra money go"). This means a rule does not need
to repeat the same list of accounts every time. There are two places you will
use a chain:

1. **As the source or destination of a rule.** In `scheduled.csv`,
   `from_account`/`to_account` can name a chain instead of a plain account
   (Part 4.5), and so can a calendar transfer's `account_1`/`account_2`,
   except a `transfer_balance`'s source. A transfer names a chain on one
   side at most.
2. **As a lot's own rule for refilling or overflowing.** This is set
   directly on the lot's row in `account_values.csv` (with
   `sim account lot edit`, or by hand), not as a separate rule:

```mermaid
flowchart LR
    checking["Asset_Checking\nminimum_balance = $2,000"]
    reserve["Asset_Checking:reserve lot"]
    savings["Asset_Savings"]
    reserve -- "refill_chain (below minimum, before the payment)" --> checking
    checking -- "overflow_chain (above maximum, month end)" --> savings
```

If a payment would drop a lot below `minimum_balance`, the simulator draws
money from `refill_chain` first, before the payment happens. If a lot goes
above `maximum_balance`, the extra is swept into `overflow_chain` at the end
of the month. `refill_chain`/`overflow_chain` can each name a chain, or name
an account directly. If you name an account directly, use
`refill_lot`/`overflow_lot` to say which of that account's lots to use — the
same account-plus-lot pairing used everywhere else in this tool. Every
account and lot already has its own automatic one-step chain, so you only
need to write a chain file when you want more than one step — for example,
"try savings first, then this brokerage lot, then that one" — with each step taking a `percent` or a `maximum` amount before
whatever is left moves on to the next step.

A chain drawn from (a `from_account`, a `refill_chain`) goes in
`chains/source/<name>.csv`; a chain paid into (a `to_account`, an
`overflow_chain`) goes in `chains/destination/<name>.csv`.

`sim chain list` shows the scenario's chains and what uses each one, and
`sim chain show` draws them as trees; `--graph` draws one chain's flowchart in
the terminal, and `--mermaid` prints the flowchart's Mermaid source for a
README. To show the chart in place, `--graph` needs a terminal that shows
images; iTerm2, kitty, Ghostty, WezTerm or VS Code's integrated terminal are
recommended. Anywhere else it writes the chart to a PNG and prints its path
([Setup](SETUP.md#drawing-chain-flowcharts-in-the-terminal) says why and how).
`sim chain add` with no arguments builds a chain by asking what it is for;
`sim chain step add|edit|move|rm`, `rename` and `delete` change one.
[The chains guide](guides/chains.md) explains how steps, percents and
maximums, chains inside chains, and the refill and overflow limits work
together, with worked examples and recipes.

### 4.7 Lifespan assumptions

`lifespan.csv` has one optional row for each person: `person` and
`final_month`. After that month, every scheduled rule tied to that person
stops firing — this includes rules where the person is set directly, and
rules for accounts that person owns (salary, their share of expenses, and so
on). Growth rules (`percent_of_balance`) are an exception: a balance keeps
earning interest, or growing in value, after its owner dies.

Setting `final_month` is also all you need to do for filing status to change
correctly after a spouse's death: MFJ or MFS automatically becomes Single,
starting the following year (Part 3). You do not need to add a matching row
to `filing_status.csv`.

`sim person lifespan set <person> <YYYY-MM>` writes a person's row into the
scenario's own `lifespan.csv`, and `sim person lifespan clear <person>`
removes it. `sim person list` shows each person's final month and which
scenario sets it.

The people themselves are in `persons.csv`, at the top of the data repo:
`id`, `display` name, `birthdate` and an optional `role` (`primary` for a
spouse whose death changes the filing status). `sim person add <id>` and
`sim person edit <id>` change it, asking for any field you don't give as an
option. The file is shared, so a change there reaches every scenario, and is
written only if every scenario still loads.

### 4.8 Calendar for more complex changes

`calendar.csv` is for anything that is not a steady, recurring rule: a
dated, one-time change. These are the kinds (plus `latent_factor`, which is
only for ensembles, Part 4.10):

| Kind | Columns used | Use it for |
| --- | --- | --- |
| `set_rate` | `series`, `amount` (a percent with its sign, e.g. `4%`) | "checking starts earning 4% in 2027" |
| `set_amount` | `scheduled_name`, `amount` (dollars), optional `param_date` | "spending drops to $4,500/mo on retirement" (Part 4.4) |
| `retire_scheduled` | `scheduled_name` | "salary stops on retirement" |
| `one_off_transfer` | `account_1`/`lot_1`, `account_2`/`lot_2`, `amount`, `tax_character` | a windfall, a gift, a settlement; either side may be a chain (Part 4.5) |
| `transfer_balance` | `account_1`/`lot_1`, `account_2`/`lot_2`, `tax_character`; `amount` left blank | rolling a 401(k) into an IRA: moves everything the lot holds at the start of that month |
| `swap_aux_table` | `series` (the table replaced; blank for one without a series), `params_json` naming the kind and the replacement | moving to another state: from that month, the state tax brackets are another state's |

Leave `tax_character` blank on a `one_off_transfer` or `transfer_balance` out
of a brokerage lot and the move is a sale: the lot's share of basis comes out
as `return_of_capital`, the rest as a capital gain, and the lot's basis drops.
From other accounts a blank means `none`, which suits a rollover between
tax-deferred accounts. Set `tax_character` to override either.

A rule that should start on a future date needs no calendar event: give its
`scheduled.csv` row that `start` date.

For a move, put the new state's brackets beside the old ones, as
`aux/state_brackets__WA.csv` say, and swap them in on the moving date:

```csv
date,kind,series,params_json
2031-07,swap_aux_table,CA,"{""aux_kind"": ""state_brackets"", ""source_series"": ""WA""}"
```

From July 2031 on, everything that read the `CA` brackets reads the `WA`
ones instead, including their later dated changes. With two states'
brackets in `aux/`, the state `tax_accumulator` rule must say which it reads:
`"bracket_series": "CA"` in its `params_json`. Do the same for
`state_deductions` if you use it. Any aux table works the same way: a new
federal bracket table on the date a law takes effect, or another inflation
series.

### 4.9 Modeling itemized deductions (for example, a big medical year)

By default, tax is calculated using the standard deduction — a flat amount
that depends only on filing status (Part 3). If a year has enough deductible
expenses (a large medical bill is the usual case), you can add a
`federal_deductions.csv` file to `aux/`. It has one dated row per year:

```csv
# aux/federal_deductions.csv
effective_date,amount
2029-01-31,42000.00
```

For that tax year, the simulator uses whichever is greater: this amount, or
the standard deduction. You do not need to compare them yourself. This file
is shared by every scenario by default; add your own copy under a scenario's
own `aux/` folder if only that scenario should have it (Part 2, `aux/`).

Instead of a plain number, `amount` can be `@name`, pointing at an
`itemizations/<name>.csv` file that lists the actual expenses and adds them
up for you:

```csv
# itemizations/medical_2029.csv
Item,Value
surgery,32000.00
prescriptions,3000.00
```

```csv
# aux/federal_deductions.csv
effective_date,amount
2029-01-31,@medical_2029
```

State tax uses the same idea, in `state_deductions.csv`. If you don't add
one, the state figure automatically uses whatever is in
`federal_deductions.csv` — most states don't need their own file.

This is a simple, whole-dollar comparison. It does not model anything more
detailed, such as medical expenses only counting above a share of your
income.

### 4.10 Running an ensemble (a range of outcomes, not one number)

Every scenario so far gives you one prediction. If you're not sure about a
rate, a date, or a one-off amount, an **ensemble** generates many variants
of a scenario — each with that value redrawn from a range you choose — and
shows you the *spread* of outcomes instead of a single number:

```sh
sim ensemble create inflation_1 base
sim calendar add set-rate 2027-01-31 mutual_fund_retirement 4.0 \
  --scenario inflation_1 --name fund_vary \
  --vary-target amount --vary-distribution truncated_normal \
  --vary-low 2.5 --vary-high 5.5
sim run inflation_1
```

Run `sim calendar add` with no kind to be asked for each of these a question
at a time instead; see [the guided mode](guides/ensembles.md#guided-mode).
`sim calendar edit` changes a row you have already written; see
[changing a row](guides/ensembles.md#changing-a-row).

See [docs/guides/ensembles.md](guides/ensembles.md) for the full
how-to, including how many members is reasonable for your case (it depends
mainly on how many things you're varying at once).

### 4.11 Add a stock or fund

A symbol (a ticker such as `ABCDX`) can have three things recorded about it
at the data repo root, shared by every scenario:

- **its name**, in `securities.csv`. Holdings views show it beside the
  symbol.
- **what it holds**, in `allocations.csv`: its share in each asset class.
  These drive `sim allocation` and how a fund lot grows (at the blend of its
  classes' rate series). Without them the symbol shows as `unclassified`.
- **its prices**, in `prices/security_price__ABCDX.csv`. A price is needed
  only when a lot gives `quantity` instead of `opening_balance`.

One command records all three:

```sh
sim symbol add ABCDX --name "A Balanced Fund" \
  --weight stock_us=60% --weight bond=40%
```

It writes the name and the weights, then fetches the prices (as
`sim prices fetch ABCDX` would). Run `sim symbol add` with no symbol to be
asked for each instead. The weights must add up to 100%; `--as-of 2027-01`
dates a set, and adding another set with a later date follows a fund whose
mix changes over time, such as a target-date fund. Leave out `--weight` if
you don't know the mix yet, and add it later; an AI can look it up (below).
To change a name or a set of weights already written, edit the file.

If the price provider has no prices for the symbol (a 401(k)'s collective
trust fund often has no public ticker), the name and weights are still
written, and you can type the prices into
`prices/security_price__ABCDX.csv` yourself: `effective_date,price`, one row
per month-end, the date as `YYYY-MM-DD` and the price in dollars, such as
`2026-09-30,41.275`.

Then put it in an account: a lot with `symbol` and `quantity`, which
`sim account lot add` asks for (Part 4.2). You don't have to do that right
away: `sim prices fetch` with no symbol keeps the prices of every symbol in
`securities.csv` or `allocations.csv` up to date, whether or not a lot holds
it yet, so you can add one ahead of time and watch its price.

Symbols are upper case, like the tickers they are. The commands upper-case
whatever you type, and the loader rejects a lower-case symbol written into a
file by hand.

To check it:

- `sim symbol list` shows every symbol, its latest weights, its prices and
  the lots holding it, with a missing allocation or price marked.
- `sim prices show` lists the stored prices of every symbol, with the
  latest price and how far it has moved since the last month-end and
  year-end; `sim prices show ABCDX` lists one symbol's prices.
- `sim prices check` compares each lot's stated balance with quantity times
  price.
- `sim allocation --by security` shows how the symbol's lots split.

#### Asking an AI to fill it in

An AI assistant can look up a fund's published breakdown for you, and a
price history the fetch can't get. Give it the formats, because the loader
rejects anything else. To get the weights as a command you can run:

```text
Look up the most recent published asset breakdown of the fund ABCDX and
write it as one command:
  sim symbol add ABCDX --name "<its name>" --weight CLASS=PERCENT ...
- CLASS is one of: stock_us, stock_intl, stock, bond, cd, cash,
  real_estate, other. Use stock only when the US/international split is
  not published.
- PERCENT has its % sign, such as 62.5%, at most four decimal places. The
  weights must add up to exactly 100%.
- Add --as-of YYYY-MM, the month the breakdown describes. For a
  target-date fund, give one command per year of its published glide path,
  each with its own --as-of, and --name only on the first.
Tell me the source and date of the breakdown you used.
```

For prices, ask for "month-end closing prices of ABCDX from YYYY-MM to
YYYY-MM as `effective_date,price` rows, the date as YYYY-MM-DD and the price
in dollars with no `$` sign."

Check the answer against the fund's own fact sheet before you save it. An
AI's figures can be out of date or simply wrong, and nothing in the
simulator can tell.

## 5. Looking at your results

A **run** is what happens when the simulator plays a scenario forward, month
by month, from its start date to its end date. The result is a single, dated
file under `state/runs/`. It holds every account balance, every money
movement, and every year's tax figures for the whole run. You can look at it
in a notebook, from the command line, or by asking an AI to read it for you.
Every notebook, and most commands (including the ones in the next table),
default to a scenario's latest run, so you rarely need to name the file
yourself. A few, like `sim explain` below, need the exact file — look in
`state/runs/` for the newest one with that scenario's name in it.

Run a scenario with:

```sh
sim run <scenario>
```

or run every scenario in the data repo at once with `sim all`. If you added
the VS Code tasks (see below), you don't need to type either command: use the
**Run All Scenarios** task.

### A quick first look

After a run, `sim explain <runfile>` prints a short summary: which scenario it
is, whether it finished successfully, and the balance of every account and
lot at the end of the run. This is the fastest way to check "did this do
roughly what I expected" before opening a notebook.

### The notebooks

Each notebook opens with a scenario picker, then shows one topic at a time.
None of them need you to write any code.

| Notebook | What it shows |
| --- | --- |
| `scenario_overview.ipynb` | The scenario's setup, without needing a run: people, accounts, chains, scheduled rules, calendar events. Good for checking a scenario is set up the way you intended, before running it. |
| `accounts.ipynb` | Balance history for any account over time, account setup details, the lots and holdings inside an account, which accounts hold a symbol, and the portfolio by asset class. |
| `net_worth.ipynb` | Net worth over time, income by category, expenses by category, realized vs. unrealized gains, and after-tax income. |
| `scenario_compare.ipynb` | Puts two or more scenarios side by side: what differs between them, net worth over time for each, and where they cross over (break-even). |
| `events.ipynb` | Every dollar movement in a run, filterable by reason (which rule fired, a refill/overflow, a catch-up fact), tax character, account, and date range, with a running balance for one account and each refill linked to the debit that triggered it — good for auditing "why did this happen." |
| `ensemble.ipynb` | For an ensemble recipe (Part 4.10): net worth's mean/median over quintile bands shaded out to the 5th–95th percentiles, plus a year-end table of mean/median/SD and the 5th, 20th, ... 95th percentiles. |

Open one with `analysis jupyter` (or the **Jupyter** VS Code task, below), or
in VS Code directly if it runs well enough there for your machine
([notebooks/README.md](../notebooks/README.md) has notes on this).

### The same views from the terminal

Several of the same views also work as a plain command, with no notebook or
kernel needed. This is the better choice over SSH, in a script, or on a
machine too small for a notebook comfortably.

| To see | Run |
| --- | --- |
| accounts and their opening balances | `sim account list` |
| the lots in one account | `sim account holdings <account>` (balances from a run), `sim account lot list <account>` (as entered) |
| the accounts holding a symbol | `sim account holders <symbol>` |
| the portfolio by asset class | `sim allocation` |
| scheduled transactions | `sim scheduled list` |
| one-time transfers | `sim transfer list` |
| a scenario's description, lineage, dates, seed, ensemble size, reserve and latest run | `sim scenario show [scenario]` |
| persons and their lifespans | `sim person list` |
| calendar rows of every kind, with where each is defined | `sim calendar list [--kind KIND] [--tag TAG]` |
| year-to-date rows | `sim ytd list` |
| itemizations and their totals | `sim itemization list`, `sim itemization show <name>` |
| a run's year-end net worth, and its lots | `sim explain <runfile>` |

[notebooks/README.md](../notebooks/README.md) keeps the full, up-to-date list, including which
notebook views don't have a terminal command yet.

### VS Code tasks

If your workspace has these set up (Command Palette → "Tasks: Run Task"),
three common jobs are one click instead of a typed command:

- **Jupyter** — opens JupyterLab (same as `analysis jupyter`).
- **Fetch Prices** — runs `sim prices fetch` (Part 4.2).
- **Run All Scenarios** — runs every scenario in the data repo (`sim all`).

### Asking an AI about your results

A run file is a real database (SQLite), and you can ask an AI assistant to
read it for you in plain English — you do not need to know any SQL, or even
that SQL is what's happening underneath. This works the same way as
[docs/sqlite-guide.md](sqlite-guide.md) describes for the separate Amazon spending data.

Questions that work well are specific, and name what you're asking about:

- "In the `sell_stock` scenario's latest run, what was net worth at the end
  of each year?"
- "Show every lot in `Asset_Brokerage_Joint` and its balance at the end of
  the run."
- "Compare federal tax due for 2030 between the `base` and `current-cut`
  scenarios."
- "Why did `Asset_Checking` go negative in March 2031?"

Naming the scenario (and the run file, if you have more than one run for it)
helps the most. A vague question like "how's my retirement looking?" forces
the AI to guess what you mean; a specific one gets a specific, checkable
answer.

An AI can also fill in data files for you, such as a new fund's
asset-class mix; see Part 4.11.

## See also

- [templates/README.md](../templates/README.md) — full CSV column reference and conventions.
- [`docs/architecture/ADR-003-registries-and-csv-schemas.md`](architecture/ADR-003-registries-and-csv-schemas.md) — authoritative
  schema, including the aux/prices/series/itemizations override mechanism (§4.1d)
  and the data-repo structure diagrams (§4.0).
- [`docs/architecture/ADR-005-lots-and-per-lot-state.md`](architecture/ADR-005-lots-and-per-lot-state.md) — lots in depth.
- [notebooks/README.md](../notebooks/README.md) — the same information as interactive widgets.
- [`docs/guides/century-of-rates.md`](guides/century-of-rates.md) — a century of inflation, interest rates and stock returns: what carries over from one year to the next.
- [`docs/guides/chains.md`](guides/chains.md) — chains step by step: drawing and sweeping, shares, chains inside chains, refill and overflow.
- [`docs/guides/ensembles.md`](guides/ensembles.md) — ranges of outcomes instead of one number (Part 4.10); [ADR-006](architecture/ADR-006-ensemble-scenarios.md) has the full design.
