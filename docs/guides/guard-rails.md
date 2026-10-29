# Guard rails: is the plan spending more than it should?

> To read a formatted `.md` file:
> - In Jupyter: right-click on it and select `Open With / Markdown Preview`
> - In VSCode: Command `Markdown Preview Enhanced: Open Preview` or ⌘⇧V

A **guard rail** is a line a scenario's spending, or its withdrawals from
its savings, should stay on one side of. The best-known ones are rules of
thumb for retirement: "take no more than 4% a year", "cut back when the
withdrawal rate climbs 20% past where it started". A rail doesn't change
what the simulation does. It is a way to read a run: for each year, how far
inside the line the plan stayed, or how far past it went, and how that adds
up over the years.

This guide explains the two questions a rail answers, the usual rails and
how to write them, and how to read what the reports show. The examples use
the prototype scenario, `retire_alice`, where Alice retires at the end of
2030 and the household bridges seven years to Bill's Social Security at
70. The column-by-column reference is
[ADR-003 §4.11](../architecture/ADR-003-registries-and-csv-schemas.md).

## Two questions

Every rail is checked two ways, because a year past the line can mean two
different things.

- **Is this year's rate a problem?** The year's **margin** is the room left
  before the rail: positive inside it, negative past it. A ceiling's margin
  is the threshold less what was spent; a floor's is what was spent less the
  threshold. Either way, negative means past the rail.
- **Is it using room left from earlier years?** The **cumulative margin**
  adds the margins up since the rail began. Positive is room banked,
  negative is overdrawn. A year past a ceiling can still be fine
  cumulatively, spending what lean years saved, and a year inside it can
  still be repaying an earlier overdraft.

Excess and shortfall don't simply cancel, though. Money spent past a rail
early would have kept growing had it stayed invested, so an early overdraft
costs more than its sum by the time it is repaid. The reports can also
carry the cumulative margin at the portfolio's own yearly return, which
shows that cost. It is still only a guide: spending differently would have
changed the path itself, which only another simulation could show. Read
the cumulative margins as a quick indication of how things are playing out.

## The usual rails

**Bengen's 4% rule** (1994): withdraw 4% of the portfolio in the first
year of retirement, then the same amount raised with inflation each year,
whatever the markets do. Historically that lasted thirty years in every
U.S. period Bengen tested. As a rail it is a ceiling on withdrawals, a
percent of the portfolio when the rail starts, CPI-indexed:

```text
name,measure,side,threshold,basis,start
four_percent,withdrawals,above,4%,initial_portfolio,2031-01
```

**Guyton and Klinger's guardrails** (2006) start from a higher withdrawal
rate, say 5%, and watch the *current* rate, this year's withdrawals over
the portfolio at the start of the year. When it rises 20% past the initial
rate (6%), spending is cut 10%; when it falls 20% below it (4%), spending
can rise 10%. The upper rail is trouble; the lower one is good news, so it
is a **positive** rail:

```text
name,measure,side,threshold,basis,sense,start
gk_upper,withdrawals,above,6%,portfolio,negative,2031-01
gk_lower,withdrawals,below,4%,portfolio,positive,2031-01
```

The cuts and raises themselves are not modeled yet: a rail only reports.
Strategies that respond to a rail are planned
([#326](https://github.com/BobKerns/analysis/issues/326)).

**A budget**: spending in today's dollars, a ceiling, or a floor (a spending
level the household doesn't want to fall under):

```text
name,measure,side,threshold,basis,start
budget,spending,above,85000.00,cpi_constant,2031-01
floor,spending,below,60000.00,cpi_constant,2031-01
```

**An annuity-like floor** in fixed dollars uses `basis` `nominal`: the same
dollars every year, losing value to inflation as a fixed annuity does.

## Spending or withdrawals?

**Spending** is what the household spends: every expense except income
tax, business expenses and a vehicle's depreciation, the same `spending` as
`sim report income` shows. Mortgage interest is spending; principal isn't
(it goes into the home). `select` narrows it by tag, so a rail can watch
only discretionary spending:

```text
name,measure,select,side,threshold,basis
fun,spending,budget:fun,above,12000.00,cpi_constant
```

**Withdrawals** are what the portfolio pays out, less income that lands in
it other than its own interest, dividends and gains. The 4% rule and the
guardrails limit withdrawals, not spending: Social Security landing in
checking pays part of the spending, and only the rest comes out of
savings. Moving money between two portfolio accounts isn't a withdrawal.

The **portfolio** is every asset account but the house, vehicles and other
assets: cash, investments, retirement and HSA accounts. Including cash is
what makes income landing in checking count. `portfolio` changes it by
tag: `portfolio:core` keeps only accounts tagged so, and
`-exclude:emergency` leaves out the emergency fund.

## When a rail applies

A rail covers the scenario's months unless `start` or `end` says
otherwise. Either can be a month (`2031-01`) or relative to a
`date_marker` calendar row, such as `retire` or `retire+5y`, so a rail
follows a retirement date that a variant moves. Rails are evaluated by
calendar year; a year the rail covers only part of is measured against
that part of the year's threshold. After the household's last death every
rail stops (when everyone has a lifespan).

Rails inherit like scheduled rules: a child scenario has its parent's
rails, and a row with the same name replaces one.

## Adding and changing rails

```sh
sim guard-rail list
sim guard-rail add bengen        # asks for each field
sim guard-rail add bengen --measure withdrawals --side above --basis initial_portfolio --threshold 4%
sim guard-rail edit gk_upper --threshold 6.5%
sim guard-rail edit gk_upper     # shows the fields; name one to change it
sim guard-rail delete budget
```

Rails aren't part of a run, so changing one reruns nothing: the next
report reads the rail as it is now.

## Reading the results

```sh
sim report guard-rails                 # every rail, annual and cumulative
sim report guard-rails --carried       # adds the margin carried at the return
sim report guard-rails --rail budget   # one rail: its measure and threshold too
sim explain                            # lists each stretch of years past a rail
sim compare retire_alice VARIANT --section guard-rails
```

In `retire_alice`, the bridge years show plainly. Withdrawals run past the
4% rule from 2031 to 2036, and past the upper guardrail too: the household
is drawing the savings hard until Social Security starts. From 2037 they
fall back inside, and by 2038 they are under the lower rail, room to
spend more. Cumulatively the 4% rule is still overdrawn at the end of the
run, carried at the return a little more so: the bridge was paid for with
money that would have grown. The budget is crossed in the first years,
then made up; its cumulative margin turns positive in 2039.

Under the table, a summary gives each rail, annual and cumulative: the
years past it, the worst margin and its year, the margin at the end, and
the **RMS overshoot**, the root mean square of how far past the rail the
years were, a year inside counting as zero. It mixes how often with how
far, and squaring makes one big miss count more than several small ones.

The `net_worth.ipynb` notebook's **Guard rails** section draws the same:
for each measure, the year's spending or withdrawals against each rail on
top, shaded where past it, and each rail's cumulative margin below. A rail
to stay inside is solid and warm; a positive rail is dashed, cool and
hatched; the dotted lines are the margins carried at the portfolio's
return.

To see how a change to the plan moves the rails, `sim compare BASELINE
VARIANT --section guard-rails` prints each rail's margins in every scenario
by year, with each variant's change (variant less baseline, so a positive
change is more room). The `scenario_compare.ipynb` notebook's **Guard
rails** section shows the same tables, under a chart with a row per rail,
annual and cumulative margins side by side, a line per scenario, then each
scenario's summary.

## Across an ensemble

An ensemble (the [ensembles guide](ensembles.md)) runs the plan through
many futures, so a rail is no longer crossed or not: some members cross it,
some don't. Each member is measured exactly as a single run is, against the
recipe's rails as they are now (editing one reruns nothing), and then, for
each rail, year and form, annual and cumulative:

- **past**: the share of members past the rail that year;
- **ever past**: the share that have been past it in some year so far, of
  every member it has covered so far, those whose household has ended too;
- **RMS overshoot**: the root mean square of how far past the members are,
  those inside counting as zero.

```sh
sim ensemble guard-rails RECIPE           # every rail
sim ensemble guard-rails RECIPE --graph   # shares over RMS, annual beside cumulative
sim ensemble compare RECIPE VARIANT       # adds each rail's change
```

A share alone says how often, not how badly; the RMS overshoot says both at
once, which is why the two are always shown together. Squaring makes one
member $40,000 past count as much as sixteen members $10,000 past, so a few
bad futures show up even when the share is small. And because members
inside count as zero, 20% of members each $10,000 past gives an RMS of
$4,470. In finance these are the lower partial moments: the share is the
zeroth, the RMS the square root of the second, the downside deviation
behind the Sortino ratio.

For the cumulative form, the share past in the last year is the share of
members that end the plan overdrawn. The summary under the tables gives,
for each rail and form, the share of member-years past, the share of
members ever past, that share at the end, and the RMS over every
member-year. A rail stops at each member's household's last death, so the
later years' shares count only the members still covered.

`ensemble.ipynb`'s **Guard rails** section shows the same, charts and
tables, with the notebook's shared dollars and years. Its **Compare several
ensembles on guard rails** table sets a baseline against any number of
recipes that pair with it: per rail, each recipe's change in the shares and
the RMS year by year, then each recipe's summary beside its change.
