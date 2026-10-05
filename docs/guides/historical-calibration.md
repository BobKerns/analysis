# Historical calibration: what numbers to put in

<!-- cspell:ignore CUUR CWUR SUUR SEEB DPCCRG DPCERG DPCERGM PCEPI histret Asize -->

The [ensembles guide](ensembles.md) explains how a varying rate, a
correlation and tail dependence work. This guide is about the numbers:
what history says a rate's median, spread and correlation should be, and
where to get the data to check or redo it.

All of it comes from public statistics, through
[`scripts/historical_calibration.py`](../../scripts/historical_calibration.py),
which downloads the series and computes every table below. The command
that produced each table is printed under it, so you can run the same thing
on other series or years.

## Reading the tables

Every rate here is an **annual rate**: for a price index, December over the
previous December, the way BLS reports annual inflation. Returns from
Damodaran are calendar-year returns. A table's year range counts the years
those rates end in, so "1966–1982" starts with the change from December 1965
to December 1966.

| Column | Meaning |
| --- | --- |
| Compound % | The single rate that compounds to the same total over the years shown (the geometric mean of the growth factors). |
| Median %, SD, Min %, Max % | Of the single-year rates, or with `--window N`, of the rolling N-year compound rates. |
| Persistence | Correlation of one year's rate with the next year's (single years only). |
| ρ (from τ) | Copula correlation with the reference series, from Kendall's τ by ρ = sin(πτ/2). |
| Pearson | Ordinary correlation with the reference, for comparison. |
| Gap vs ref | Compound rate minus the reference's, over the years both have. |

Correlations and persistence are left blank for fewer than eight years,
where they're noise.

## Turning history into `vary_*` settings

A varying `set-rate` row is drawn once per member and then holds until the
next row for that series. So the uncertainty it needs is the uncertainty of
a **multi-year average rate**, not of a single year. Single years scatter
much more than decades do: since 1984, CPI-U's single-year rates have an SD
of 1.5 points, but its rolling 10-year compound rates have an SD of 0.6.
Using the single-year SD for a rate held 20 years would make the spread of
outcomes far too wide. Match the window to how long the row holds.

- **`vary_median`:** the median of the rolling N-year compound rates. The
  full period's compound rate is close to it, and is the better choice when
  the window covers most of the period. Use the compound rate rather than
  the average of single years: an average of annual rates runs above the
  rate that actually compounds, by about half the variance.
- **`vary_sd`:** the SD of the rolling N-year compound rates. Overlapping
  windows share most of their years, so a period of 40 years holds only
  four independent decades; treat the SD as a rough guide, not a
  measurement.
- **`vary_low`/`vary_high`:** these clip the draw, and default to 3 SDs out.
  The historical min and max are a check on that range, not good clip
  bounds: clipping at them rules out anything worse than what happened.
- **`vary_skew`:** when Max sits much further above the median than Min
  sits below it, as for energy and for CPI-U across the postwar period,
  the high side needs a longer tail.
- **`vary_correlation`:** ρ (from τ), from a table with the same window.
  The simulator's correlations are copula correlations, and ρ from τ is
  their direct estimate. Pearson is pulled around by a few extreme years,
  and with 10-year windows the ρ is higher than with single years because
  noise averages out of both series.
- **A later row correlated with an earlier one:** Persistence, the
  correlation of one year's rate with the next, is an upper bound for rows
  a year apart. Rows several years apart are less correlated than that,
  and the script doesn't measure it directly.

## A long-run rate with episodes on top

History doesn't look like one rate drawn from one range. It looks like a
long-run rate, interrupted by episodes of a few years far above it, that
end and give way to the long-run rate again. The table below lists every
stretch since 1914 of consecutive years above 5%:

| Series  | Years     | Length | Compound % |
| ------- | --------- | -----: | ---------: |
| CPI-U   | 1916–1919 |      4 |       16.4 |
| CPI-U   | 1941–1942 |      2 |        9.5 |
| CPI-U   | 1946–1947 |      2 |       13.4 |
| CPI-U   | 1950–1951 |      2 |        6.0 |
| CPI-U   | 1969–1970 |      2 |        5.9 |
| CPI-U   | 1973–1975 |      3 |        9.3 |
| CPI-U   | 1977–1981 |      5 |       10.1 |
| CPI-U   | 1990      |      1 |        6.1 |
| CPI-U   | 2021–2022 |      2 |        6.7 |
| Core    | 1968–1970 |      3 |        5.9 |
| Core    | 1974–1981 |      8 |        9.0 |
| Core    | 1990      |      1 |        5.2 |
| Core    | 2021–2022 |      2 |        5.6 |
| Medical | 1946–1948 |      3 |        7.0 |
| Medical | 1951      |      1 |        5.8 |
| Medical | 1966–1970 |      5 |        6.5 |
| Medical | 1973–1993 |     21 |        8.4 |
| Medical | 2002      |      1 |        5.0 |
| Medical | 2007      |      1 |        5.2 |
| Shelter | 1968–1970 |      3 |        8.0 |
| Shelter | 1973–1975 |      3 |        8.6 |
| Shelter | 1977–1981 |      5 |       12.5 |
| Shelter | 1984–1985 |      2 |        5.6 |
| Shelter | 1990      |      1 |        5.2 |
| Shelter | 2022–2023 |      2 |        6.8 |

```sh
uv run scripts/historical_calibration.py --period "Postwar=1948-" --runs-above 5 \
  CPI-U=CUUR0000SA0 Core=CUUR0000SA0L1E Medical=CUUR0000SAM Shelter=CUUR0000SAH1
```

Since 1948 CPI-U had six such stretches in 78 years, each one to five years
long, at compound rates of about 6% to 10%. The Great Inflation was three
bursts (1969–1970, 1973–1975 and 1977–1981) with years of 3% to 5% in
between, not 17 years at one rate. Medical care was the exception: above 5%
for 21 straight years, from 1973 to 1993.

A scenario can model that as three rows on the same series: the long-run
rate, an episode that starts at a varying date, and a return row a fixed
number of years later. This continues the `inflation_drivers` recipe from
the ensembles guide, as a separate scenario so it can be compared with the
recipe, or used as a mix-in:

```sh
sim scenario create great_inflation inflation_drivers
sim calendar add set-rate 2030-01-31 medical 8.2 --aux-kind inflation \
  --scenario great_inflation --name medical_episode \
  --vary-target date --vary-distribution uniform \
  --vary-low 2028-01-31 --vary-high 2036-01-31
sim calendar add set-rate 2035-01-31 medical 4.2 --aux-kind inflation \
  --scenario great_inflation --name medical_return \
  --vary-target date --vary-distribution uniform \
  --vary-low 2033-01-31 --vary-high 2041-01-31 \
  --vary-correlate-with medical_episode --vary-correlation 1.0
```

- The episode rate, 8.2%, is medical care's compound rate over the Great
  Inflation; the return rate, 4.2%, is its long-run rate since 1984.
- The two date ranges are the same length, five years apart, and
  correlated at 1.0. Both draws then land at the same point in their
  ranges, so the episode lasts exactly five years in every member and only
  its start varies.
- The return lands on the long-run median, not on the rate that member drew
  for `medical_vary`: it reverts toward the middle, as history did. A row
  varies either its date or its amount, so the episode's rate and the
  return's rate are fixed here.
- A date range that runs past the scenario's end makes the episode
  optional. With `--vary-high 2048-01-31` on a scenario ending in 2040,
  about 13 of the range's 20 years fall inside the scenario, so the
  episode starts in about 65% of members; in the rest it never starts. The
  ranges here were checked on the prototype scenario, where they ran as
  described. Member 0, the base case, uses the middle of each range, so it
  has an episode when the middle falls inside the scenario.

Separate episode scenarios, one per historical pattern (a Great
Inflation-style stretch, a short post-pandemic spike), can each be a mix-in
on the same base, so `base:great_inflation` and `base:pandemic_spike` share
everything else.

## Inflation metrics compared

Different indexes price different baskets. A plan should grow each expense
with the index whose basket matches it, and its income with whatever it's
legally tied to.

- **CPI-U:** all urban consumers. The headline index.
- **CPI-W:** urban wage earner and clerical worker households, about 30% of
  the CPI-U population, excluding retirees. It's the index Social Security
  COLAs follow, so it's what benefits grow with.
- **R-CPI-E:** BLS's research index reweighted to the spending of
  households 62 and older, from December 1982. BLS cautions that its
  weights rest on a small sample and that it prices the same outlets and
  items as CPI-U, so treat conclusions from it as tentative.
- **C-CPI-U (chained CPI):** accounts for substitution between items, from
  December 1999. It has indexed federal tax brackets since 2017, so it's
  what bracket thresholds grow with.
- **PCE price index:** BEA's measure, with broader coverage than CPI,
  including spending made on households' behalf, such as by employers and
  Medicare. From 1959; core PCE excludes food and energy.
- **Medical care**, and its services and commodities parts.

Against CPI-U, since R-CPI-E began and since C-CPI-U began:

| Series              | Years     |   N | Compound % | Median % |  SD | Min % | Max % | Persistence | ρ (from τ) | Pearson | Gap vs ref |
| ------------------- | --------- | --: | ---------: | -------: | --: | ----: | ----: | ----------: | ---------: | ------: | ---------: |
| CPI-U               | 1983–2025 |  43 |        2.8 |      2.7 | 1.5 |   0.1 |   7.0 |        0.34 |       1.00 |    1.00 |       0.00 |
| CPI-W               | 1983–2025 |  43 |        2.8 |      2.7 | 1.6 |  -0.5 |   7.8 |        0.24 |       0.99 |    0.99 |      -0.06 |
| R-CPI-E             | 1983–2025 |  43 |        3.0 |      2.8 | 1.4 |   0.5 |   6.7 |        0.44 |       0.99 |    0.99 |       0.16 |
| C-CPI-U             | 2000–2025 |  26 |        2.3 |      1.9 | 1.5 |   0.2 |   6.5 |        0.37 |       0.98 |    0.99 |      -0.27 |
| PCE                 | 1983–2025 |  43 |        2.4 |      2.2 | 1.3 |   0.1 |   6.0 |        0.46 |       0.97 |    0.98 |      -0.40 |
| Core                | 1983–2025 |  43 |        2.9 |      2.6 | 1.2 |   0.8 |   5.7 |        0.75 |       0.77 |    0.79 |       0.06 |
| Core-PCE            | 1983–2025 |  43 |        2.5 |      2.1 | 1.1 |   1.1 |   5.1 |        0.76 |       0.85 |    0.83 |      -0.38 |
| Medical             | 1983–2025 |  43 |        4.3 |      3.9 | 2.0 |   0.5 |   9.6 |        0.76 |       0.54 |    0.39 |       1.45 |
| Medical-services    | 1983–2025 |  43 |        4.5 |      4.1 | 2.0 |  -0.5 |   9.9 |        0.71 |       0.52 |    0.37 |       1.66 |
| Medical-commodities | 1983–2025 |  43 |        3.5 |      3.1 | 2.5 |  -2.5 |   8.4 |        0.67 |       0.49 |    0.36 |       0.66 |
| R-CPI-E-medical     | 1983–2025 |  43 |        4.3 |      3.8 | 2.2 |   1.2 |  11.3 |        0.80 |       0.51 |    0.40 |       1.48 |

| Series              | Years     |   N | Compound % | Median % |  SD | Min % | Max % | Persistence | ρ (from τ) | Pearson | Gap vs ref |
| ------------------- | --------- | --: | ---------: | -------: | --: | ----: | ----: | ----------: | ---------: | ------: | ---------: |
| CPI-U               | 2000–2025 |  26 |        2.6 |      2.3 | 1.5 |   0.1 |   7.0 |        0.30 |       1.00 |    1.00 |       0.00 |
| CPI-W               | 2000–2025 |  26 |        2.5 |      2.3 | 1.7 |  -0.5 |   7.8 |        0.22 |       0.99 |    0.99 |      -0.01 |
| R-CPI-E             | 2000–2025 |  26 |        2.7 |      2.3 | 1.4 |   0.5 |   6.7 |        0.38 |       0.99 |    0.99 |       0.12 |
| C-CPI-U             | 2000–2025 |  26 |        2.3 |      1.9 | 1.5 |   0.2 |   6.5 |        0.37 |       0.98 |    0.99 |      -0.27 |
| PCE                 | 2000–2025 |  26 |        2.2 |      2.1 | 1.3 |   0.1 |   6.0 |        0.38 |       0.97 |    0.99 |      -0.34 |
| Core                | 2000–2025 |  26 |        2.4 |      2.2 | 1.1 |   0.8 |   5.7 |        0.59 |       0.74 |    0.84 |      -0.15 |
| Core-PCE            | 2000–2025 |  26 |        2.1 |      1.8 | 1.0 |   1.1 |   5.1 |        0.61 |       0.87 |    0.91 |      -0.44 |
| Medical             | 2000–2025 |  26 |        3.3 |      3.3 | 1.1 |   0.5 |   5.2 |        0.10 |       0.35 |    0.13 |       0.72 |
| Medical-services    | 2000–2025 |  26 |        3.6 |      3.6 | 1.3 |  -0.5 |   5.9 |        0.15 |       0.34 |    0.12 |       1.01 |
| Medical-commodities | 2000–2025 |  26 |        2.3 |      2.4 | 1.7 |  -2.5 |   4.8 |       -0.02 |       0.24 |    0.06 |      -0.30 |
| R-CPI-E-medical     | 2000–2025 |  26 |        3.2 |      3.3 | 1.1 |   1.2 |   5.0 |        0.27 |       0.35 |    0.15 |       0.63 |

```sh
uv run scripts/historical_calibration.py --reference CPI-U=CUUR0000SA0 \
  --period "Since 1983=1983-" --period "Since 2000=2000-" \
  CPI-U=CUUR0000SA0 CPI-W=CWUR0000SA0 "R-CPI-E=rcpie:All items" \
  C-CPI-U=SUUR0000SA0 PCE=bea:DPCERG Core=CUUR0000SA0L1E Core-PCE=bea:DPCCRG \
  Medical=CUUR0000SAM Medical-services=CUUR0000SAM2 \
  Medical-commodities=CUUR0000SAM1 "R-CPI-E-medical=rcpie:Medical care"
```

The gap between R-CPI-E and CPI-W is the drift of a retiree's costs against
their COLA. Since 1983 it's been 0.22 points a year, at ρ 0.97 (from the
same command with `--reference CPI-W=CWUR0000SA0`). Small, but it compounds:
over 30 years, a COLA-indexed benefit buys about 6% less of a retiree's
basket.

Medical care's gap above CPI-U has narrowed: 1.45 points a year since 1983,
0.72 since 2000. Its correlation with CPI-U is moderate and weakening too
(ρ 0.54, then 0.35), so medical inflation is largely its own uncertainty,
not just the general rate. A medical rate belongs under a core factor at a
low ρ, or under a factor of its own. The R-CPI-E medical index tracks the
CPI-U one closely; the older basket's extra medical weight shows up in
R-CPI-E's all-items index, not in a different medical rate.

C-CPI-U runs about 0.3 points a year below CPI-U, and PCE about 0.4 below.
Tax brackets indexed to C-CPI-U creep down against expenses that follow
CPI-U.

## Long-run rates

Single years, then rolling 10-year compound rates, since 1984 and since
1948. The reference is core CPI (less food and energy), the usual choice
for a core-inflation factor.

| Series           | Years     |   N | Compound % | Median % |   SD | Min % | Max % | Persistence | ρ (from τ) | Pearson | Gap vs ref |
| ---------------- | --------- | --: | ---------: | -------: | ---: | ----: | ----: | ----------: | ---------: | ------: | ---------: |
| CPI-U            | 1984–2025 |  42 |        2.8 |      2.7 |  1.5 |   0.1 |   7.0 |        0.33 |       0.77 |    0.80 |      -0.03 |
| Core             | 1984–2025 |  42 |        2.8 |      2.5 |  1.2 |   0.8 |   5.7 |        0.74 |       1.00 |    1.00 |       0.00 |
| Food             | 1984–2025 |  42 |        3.0 |      2.7 |  1.9 |  -0.5 |  10.4 |        0.30 |       0.52 |    0.58 |       0.14 |
| Energy           | 1984–2025 |  42 |        2.4 |      2.3 | 10.5 | -21.3 |  29.3 |       -0.18 |      -0.02 |    0.13 |      -0.40 |
| Shelter          | 1984–2025 |  42 |        3.4 |      3.2 |  1.4 |   0.3 |   7.5 |        0.71 |       0.82 |    0.83 |       0.61 |
| Medical          | 1984–2025 |  42 |        4.2 |      3.8 |  2.0 |   0.5 |   9.6 |        0.76 |       0.60 |    0.54 |       1.39 |
| Medical-services | 1984–2025 |  42 |        4.4 |      4.1 |  2.0 |  -0.5 |   9.9 |        0.70 |       0.59 |    0.49 |       1.61 |
| Apparel          | 1984–2025 |  42 |        0.6 |      0.3 |  2.3 |  -3.9 |   5.8 |        0.35 |       0.73 |    0.73 |      -2.29 |
| Tuition          | 1984–2025 |  42 |        5.2 |      5.2 |  2.2 |   1.4 |  10.0 |        0.93 |       0.49 |    0.40 |       2.40 |
| T-bill           | 1984–2025 |  42 |        3.4 |      3.5 |  2.7 |   0.0 |   9.9 |        0.88 |       0.75 |    0.61 |       0.59 |
| T-bond           | 1984–2025 |  42 |        6.0 |      7.0 |  9.9 | -17.8 |  25.7 |       -0.09 |       0.14 |    0.02 |       3.17 |
| Stocks           | 1984–2025 |  42 |       11.7 |     15.8 | 16.5 | -36.5 |  37.2 |       -0.06 |       0.03 |    0.02 |       8.85 |

| Series           | Years     |   N | Compound % | Median % |  SD | Min % | Max % | Persistence | ρ (from τ) | Pearson | Gap vs ref |
| ---------------- | --------- | --: | ---------: | -------: | --: | ----: | ----: | ----------: | ---------: | ------: | ---------: |
| CPI-U            | 1984–2025 |  33 |        2.8 |      2.5 | 0.6 |   1.6 |   3.7 |           — |       0.93 |    0.93 |      -0.03 |
| Core             | 1984–2025 |  33 |        2.8 |      2.3 | 0.7 |   1.8 |   4.2 |           — |       1.00 |    1.00 |       0.00 |
| Food             | 1984–2025 |  33 |        3.0 |      2.7 | 0.5 |   1.6 |   3.6 |           — |       0.71 |    0.77 |       0.14 |
| Energy           | 1984–2025 |  33 |        2.4 |      1.9 | 2.4 |  -0.9 |   7.6 |           — |      -0.23 |   -0.33 |      -0.40 |
| Shelter          | 1984–2025 |  33 |        3.4 |      3.1 | 0.7 |   2.2 |   4.5 |           — |       0.95 |    0.91 |       0.61 |
| Medical          | 1984–2025 |  33 |        4.2 |      3.9 | 1.3 |   2.6 |   7.1 |           — |       0.65 |    0.77 |       1.39 |
| Medical-services | 1984–2025 |  33 |        4.4 |      4.2 | 1.3 |   2.7 |   7.2 |           — |       0.65 |    0.75 |       1.61 |
| Apparel          | 1984–2025 |  33 |        0.6 |      0.2 | 1.0 |  -1.1 |   2.7 |           — |       0.45 |    0.74 |      -2.29 |
| Tuition          | 1984–2025 |  33 |        5.2 |      5.6 | 1.6 |   2.6 |   8.3 |           — |       0.57 |    0.53 |       2.40 |
| T-bill           | 1984–2025 |  33 |        3.4 |      2.7 | 1.9 |   0.3 |   6.5 |           — |       0.89 |    0.77 |       0.59 |
| T-bond           | 1984–2025 |  33 |        6.0 |      6.1 | 3.0 |   0.1 |  12.6 |           — |       0.65 |    0.52 |       3.17 |
| Stocks           | 1984–2025 |  33 |       11.7 |     11.9 | 5.3 |  -1.4 |  19.0 |           — |       0.69 |    0.62 |       8.85 |

| Series           | Years     |   N | Compound % | Median % |  SD | Min % | Max % | Persistence | ρ (from τ) | Pearson | Gap vs ref |
| ---------------- | --------- | --: | ---------: | -------: | --: | ----: | ----: | ----------: | ---------: | ------: | ---------: |
| CPI-U            | 1948–2025 |  69 |        3.4 |      2.7 | 2.0 |   1.2 |   8.7 |           — |       0.98 |    0.98 |       0.02 |
| Core             | 1958–2025 |  59 |        3.6 |      3.1 | 2.0 |   1.8 |   8.1 |           — |       1.00 |    1.00 |       0.00 |
| Food             | 1948–2025 |  69 |        3.4 |      2.8 | 2.0 |   0.5 |   8.7 |           — |       0.91 |    0.92 |       0.08 |
| Energy           | 1958–2025 |  59 |        3.8 |      2.6 | 3.8 |  -0.9 |  13.9 |           — |       0.48 |    0.68 |       0.18 |
| Shelter          | 1953–2025 |  64 |        4.1 |      3.6 | 2.3 |   1.6 |   9.6 |           — |       0.98 |    0.99 |       0.66 |
| Medical          | 1948–2025 |  69 |        4.9 |      4.1 | 2.2 |   2.6 |  10.0 |           — |       0.92 |    0.94 |       1.47 |
| Medical-services | 1948–2025 |  69 |        5.3 |      4.5 | 2.2 |   2.7 |  10.3 |           — |       0.94 |    0.94 |       1.83 |
| Apparel          | 1948–2025 |  69 |        1.5 |      1.0 | 1.8 |  -1.1 |   4.6 |           — |       0.83 |    0.86 |      -2.07 |
| Tuition          | 1978–2025 |  39 |        5.9 |      5.7 | 2.1 |   2.6 |   9.7 |           — |       0.77 |    0.78 |       2.37 |
| T-bill           | 1948–2025 |  69 |        4.0 |      4.0 | 2.6 |   0.3 |   9.2 |           — |       0.94 |    0.86 |       0.73 |
| T-bond           | 1948–2025 |  69 |        4.9 |      4.9 | 3.3 |   0.1 |  13.7 |           — |       0.37 |    0.28 |       1.71 |
| Stocks           | 1948–2025 |  69 |       11.6 |     11.9 | 5.1 |  -1.4 |  20.1 |           — |       0.21 |    0.08 |       7.31 |

```sh
uv run scripts/historical_calibration.py --reference Core=CUUR0000SA0L1E \
  --period "Since 1984=1984-" --period "Postwar=1948-" [--window 10] \
  CPI-U=CUUR0000SA0 Core=CUUR0000SA0L1E Food=CUUR0000SAF1 Energy=CUUR0000SA0E \
  Shelter=CUUR0000SAH1 Medical=CUUR0000SAM Medical-services=CUUR0000SAM2 \
  Apparel=CUUR0000SAA Tuition=CUUR0000SEEB \
  T-bill=damodaran:tbill T-bond=damodaran:tbond Stocks=damodaran:sp500
```

Which period to use is a judgment. Since 1984 is one regime: low, stable
inflation, plus 2021–2022. The postwar period includes the Great Inflation,
so its 10-year SD (2.0 points for CPI-U) is more than three times the
recent one (0.6). A long-run row calibrated to the recent period, with the
1970s added as an episode, keeps the two apart.

## Historical episodes

The same series in each episode, against core CPI. Short episodes (two or
three years) have no correlation or persistence; use their compound rates
and ranges as the levels an episode row runs at.

### The Great Inflation, 1966–1982

| Series           | Years     |   N | Compound % | Median % |   SD | Min % | Max % | Persistence | ρ (from τ) | Pearson | Gap vs ref |
| ---------------- | --------- | --: | ---------: | -------: | ---: | ----: | ----: | ----------: | ---------: | ------: | ---------: |
| CPI-U            | 1966–1982 |  17 |        6.8 |      6.2 |  3.4 |   3.0 |  13.3 |        0.57 |       0.95 |    0.93 |       0.26 |
| Core             | 1966–1982 |  17 |        6.6 |      6.2 |  3.0 |   3.0 |  12.2 |        0.53 |       1.00 |    1.00 |       0.00 |
| Food             | 1966–1982 |  17 |        6.6 |      4.6 |  5.0 |   0.5 |  20.3 |        0.30 |       0.47 |    0.38 |       0.08 |
| Energy           | 1966–1982 |  17 |        9.0 |      7.1 |  9.6 |   1.3 |  37.5 |        0.38 |       0.80 |    0.77 |       2.45 |
| Shelter          | 1966–1982 |  17 |        7.7 |      7.2 |  4.4 |   2.4 |  17.5 |        0.52 |       0.93 |    0.91 |       1.16 |
| Medical          | 1966–1982 |  17 |        8.2 |      8.8 |  2.7 |   3.3 |  12.6 |        0.62 |       0.74 |    0.74 |       1.61 |
| Medical-services | 1966–1982 |  17 |        8.8 |      9.0 |  2.5 |   3.7 |  13.2 |        0.55 |       0.72 |    0.70 |       2.28 |
| Apparel          | 1966–1982 |  17 |        4.3 |      4.2 |  1.8 |   1.6 |   8.7 |       -0.03 |       0.49 |    0.60 |      -2.27 |
| Tuition          | 1978–1982 |   5 |       10.8 |     12.0 |  2.0 |   8.5 |  12.7 |           — |          — |       — |       1.68 |
| T-bill           | 1966–1982 |  17 |        7.1 |      6.4 |  2.9 |   4.1 |  14.0 |        0.80 |       0.81 |    0.68 |       0.50 |
| T-bond           | 1966–1982 |  17 |        5.1 |      2.9 |  9.2 |  -5.0 |  32.8 |        0.09 |      -0.32 |   -0.27 |      -1.42 |
| Stocks           | 1966–1982 |  17 |        6.8 |     10.8 | 17.6 | -25.9 |  37.0 |       -0.13 |       0.02 |   -0.05 |       0.21 |

- Every price category ran hot together: shelter at ρ 0.93 with core,
  energy 0.80, medical 0.74. This is the joint extreme that tail
  dependence (`vary_tail_dependence`) exists to produce.
- Energy's single-year SD was 9.6 points, with a 37.5% year; it needs a
  right skew.
- Nominal stock returns compounded at 6.8%, the same as CPI-U: no real
  return over 17 years. T-bonds compounded at 5.1%, below inflation.
- Apparel ran below core, and tuition's series starts only in 1978.

### Calm baseline, 1984–2007

| Series           | Years     |   N | Compound % | Median % |   SD | Min % | Max % | Persistence | ρ (from τ) | Pearson | Gap vs ref |
| ---------------- | --------- | --: | ---------: | -------: | ---: | ----: | ----: | ----------: | ---------: | ------: | ---------: |
| CPI-U            | 1984–2007 |  24 |        3.1 |      3.0 |  1.2 |   1.1 |   6.1 |        0.27 |       0.55 |    0.63 |      -0.02 |
| Core             | 1984–2007 |  24 |        3.1 |      2.7 |  1.1 |   1.1 |   5.2 |        0.86 |       1.00 |    1.00 |       0.00 |
| Food             | 1984–2007 |  24 |        3.1 |      2.8 |  1.3 |   1.5 |   5.6 |        0.28 |       0.47 |    0.49 |      -0.04 |
| Energy           | 1984–2007 |  24 |        3.3 |      2.6 | 10.0 | -19.7 |  18.1 |       -0.11 |      -0.32 |   -0.21 |       0.20 |
| Shelter          | 1984–2007 |  24 |        3.7 |      3.4 |  1.0 |   2.2 |   6.0 |        0.69 |       0.79 |    0.84 |       0.60 |
| Medical          | 1984–2007 |  24 |        5.3 |      5.0 |  1.8 |   2.8 |   9.6 |        0.84 |       0.78 |    0.84 |       2.21 |
| Medical-services | 1984–2007 |  24 |        5.6 |      5.5 |  1.8 |   2.9 |   9.9 |        0.80 |       0.72 |    0.77 |       2.44 |
| Apparel          | 1984–2007 |  24 |        0.6 |      0.5 |  2.3 |  -3.2 |   5.1 |        0.63 |       0.82 |    0.83 |      -2.47 |
| Tuition          | 1984–2007 |  24 |        6.8 |      6.5 |  1.5 |   4.6 |  10.0 |        0.84 |       0.67 |    0.71 |       3.67 |
| T-bill           | 1984–2007 |  24 |        5.0 |      5.0 |  2.2 |   1.0 |   9.9 |        0.80 |       0.79 |    0.80 |       1.92 |
| T-bond           | 1984–2007 |  24 |        8.8 |      9.6 |  9.5 |  -8.2 |  25.7 |       -0.29 |       0.35 |    0.34 |       5.67 |
| Stocks           | 1984–2007 |  24 |       12.2 |     13.2 | 15.6 | -22.0 |  37.2 |        0.10 |       0.03 |    0.08 |       9.14 |

- Inflation averaged about 3%, with an SD of 1.2 points.
- Medical care ran 2.2 points a year above core, and tuition 3.7.
- Energy moved against core (ρ −0.32): an energy price swing in a calm
  period was its own event.

### The financial crisis and its aftermath, 2008–2019

| Series           | Years     |   N | Compound % | Median % |   SD | Min % | Max % | Persistence | ρ (from τ) | Pearson | Gap vs ref |
| ---------------- | --------- | --: | ---------: | -------: | ---: | ----: | ----: | ----------: | ---------: | ------: | ---------: |
| CPI-U            | 2008–2019 |  12 |        1.7 |      1.8 |  0.8 |   0.1 |   3.0 |       -0.14 |       0.65 |    0.33 |      -0.17 |
| Core             | 2008–2019 |  12 |        1.9 |      1.9 |  0.4 |   0.8 |   2.3 |       -0.12 |       1.00 |    1.00 |       0.00 |
| Food             | 2008–2019 |  12 |        1.9 |      1.6 |  1.9 |  -0.5 |   5.9 |       -0.33 |       0.00 |   -0.03 |       0.06 |
| Energy           | 2008–2019 |  12 |       -0.2 |      2.0 | 10.6 | -21.3 |  18.2 |       -0.11 |       0.10 |   -0.05 |      -2.04 |
| Shelter          | 2008–2019 |  12 |        2.4 |      2.7 |  1.1 |   0.3 |   3.6 |        0.78 |       0.50 |    0.62 |       0.53 |
| Medical          | 2008–2019 |  12 |        3.0 |      3.1 |  0.8 |   1.8 |   4.6 |       -0.32 |       0.42 |    0.20 |       1.14 |
| Medical-services | 2008–2019 |  12 |        3.2 |      3.2 |  0.9 |   1.6 |   5.1 |       -0.13 |       0.65 |    0.29 |       1.32 |
| Apparel          | 2008–2019 |  12 |        0.1 |     -0.5 |  1.9 |  -2.0 |   4.6 |        0.03 |       0.37 |    0.32 |      -1.80 |
| Tuition          | 2008–2019 |  12 |        3.5 |      3.5 |  1.0 |   2.2 |   5.5 |        0.78 |      -0.28 |   -0.25 |       1.65 |
| T-bill           | 2008–2019 |  12 |        0.6 |      0.1 |  0.8 |   0.0 |   2.1 |        0.72 |       0.31 |    0.35 |      -1.25 |
| T-bond           | 2008–2019 |  12 |        4.0 |      2.9 |  9.2 | -11.1 |  20.1 |       -0.47 |      -0.05 |   -0.04 |       2.14 |
| Stocks           | 2008–2019 |  12 |        9.0 |     14.2 | 18.8 | -36.5 |  32.1 |       -0.43 |      -0.14 |   -0.08 |       7.16 |

- Low inflation (1.7% a year) and near-zero T-bill returns, 0.6% a year
  compounded: cash lost about 1 point a year to inflation for 12 years.
- Single-year persistence turned negative for most price series (not
  shelter or tuition): years alternated rather than trending.

### Post-pandemic inflation, 2021–2023

| Series           | Years     |   N | Compound % | Median % |   SD | Min % | Max % | Persistence | ρ (from τ) | Pearson | Gap vs ref |
| ---------------- | --------- | --: | ---------: | -------: | ---: | ----: | ----: | ----------: | ---------: | ------: | ---------: |
| CPI-U            | 2021–2023 |   3 |        5.6 |      6.5 |  2.0 |   3.4 |   7.0 |           — |          — |       — |       0.57 |
| Core             | 2021–2023 |   3 |        5.0 |      5.5 |  1.0 |   3.9 |   5.7 |           — |          — |       — |       0.00 |
| Food             | 2021–2023 |   3 |        6.4 |      6.3 |  3.9 |   2.7 |  10.4 |           — |          — |       — |       1.39 |
| Energy           | 2021–2023 |   3 |       10.8 |      7.3 | 16.1 |  -2.0 |  29.3 |           — |          — |       — |       5.75 |
| Shelter          | 2021–2023 |   3 |        5.9 |      6.2 |  1.7 |   4.1 |   7.5 |           — |          — |       — |       0.89 |
| Medical          | 2021–2023 |   3 |        2.2 |      2.2 |  1.8 |   0.5 |   4.0 |           — |          — |       — |      -2.84 |
| Medical-services | 2021–2023 |   3 |        2.0 |      2.5 |  2.3 |  -0.5 |   4.1 |           — |          — |       — |      -2.99 |
| Apparel          | 2021–2023 |   3 |        3.2 |      2.9 |  2.4 |   1.0 |   5.8 |           — |          — |       — |      -1.82 |
| Tuition          | 2021–2023 |   3 |        2.7 |      2.7 |  0.6 |   2.1 |   3.3 |           — |          — |       — |      -2.35 |
| T-bill           | 2021–2023 |   3 |        2.4 |      2.1 |  2.6 |   0.0 |   5.3 |           — |          — |       — |      -2.58 |
| T-bond           | 2021–2023 |   3 |       -6.6 |     -4.4 | 11.0 | -17.8 |   3.9 |           — |          — |       — |     -11.59 |
| Stocks           | 2021–2023 |   3 |        9.9 |     26.1 | 26.2 | -18.0 |  28.5 |           — |          — |       — |       4.87 |

- CPI-U peaked at 7.0% in 2021 and compounded 5.6% a year over three years.
- Medical care ran below core (2.2% against 5.0%), the opposite of its
  usual gap; it isn't safe to assume medical costs lead every inflation.
- T-bonds lost 6.6% a year.

### Tariff episodes, 2018–2019 and 2025

| Series           | Years     |   N | Compound % | Median % |   SD | Min % | Max % | Persistence | ρ (from τ) | Pearson | Gap vs ref |
| ---------------- | --------- | --: | ---------: | -------: | ---: | ----: | ----: | ----------: | ---------: | ------: | ---------: |
| CPI-U            | 2018–2019 |   2 |        2.1 |      2.1 |  0.3 |   1.9 |   2.3 |           — |          — |       — |      -0.12 |
| Core             | 2018–2019 |   2 |        2.2 |      2.2 |  0.1 |   2.2 |   2.3 |           — |          — |       — |       0.00 |
| Food             | 2018–2019 |   2 |        1.7 |      1.7 |  0.2 |   1.6 |   1.8 |           — |          — |       — |      -0.52 |
| Energy           | 2018–2019 |   2 |        1.5 |      1.6 |  2.7 |  -0.3 |   3.4 |           — |          — |       — |      -0.69 |
| Shelter          | 2018–2019 |   2 |        3.2 |      3.2 |  0.0 |   3.2 |   3.2 |           — |          — |       — |       1.00 |
| Medical          | 2018–2019 |   2 |        3.3 |      3.3 |  1.8 |   2.0 |   4.6 |           — |          — |       — |       1.06 |
| Medical-services | 2018–2019 |   2 |        3.9 |      3.9 |  1.7 |   2.6 |   5.1 |           — |          — |       — |       1.64 |
| Apparel          | 2018–2019 |   2 |       -0.6 |     -0.6 |  0.8 |  -1.2 |  -0.1 |           — |          — |       — |      -2.84 |
| Tuition          | 2018–2019 |   2 |        2.5 |      2.5 |  0.3 |   2.3 |   2.7 |           — |          — |       — |       0.26 |
| T-bill           | 2018–2019 |   2 |        2.0 |      2.0 |  0.1 |   2.0 |   2.1 |           — |          — |       — |      -0.18 |
| T-bond           | 2018–2019 |   2 |        4.7 |      4.8 |  6.8 |  -0.0 |   9.6 |           — |          — |       — |       2.48 |
| Stocks           | 2018–2019 |   2 |       12.1 |     13.5 | 25.1 |  -4.2 |  31.2 |           — |          — |       — |       9.88 |

| Series           | Years     |   N | Compound % | Median % |  SD | Min % | Max % | Persistence | ρ (from τ) | Pearson | Gap vs ref |
| ---------------- | --------- | --: | ---------: | -------: | --: | ----: | ----: | ----------: | ---------: | ------: | ---------: |
| CPI-U            | 2025–2025 |   1 |        2.7 |      2.7 |   — |   2.7 |   2.7 |           — |          — |       — |       0.04 |
| Core             | 2025–2025 |   1 |        2.6 |      2.6 |   — |   2.6 |   2.6 |           — |          — |       — |       0.00 |
| Food             | 2025–2025 |   1 |        3.1 |      3.1 |   — |   3.1 |   3.1 |           — |          — |       — |       0.43 |
| Energy           | 2025–2025 |   1 |        2.3 |      2.3 |   — |   2.3 |   2.3 |           — |          — |       — |      -0.34 |
| Shelter          | 2025–2025 |   1 |        3.2 |      3.2 |   — |   3.2 |   3.2 |           — |          — |       — |       0.53 |
| Medical          | 2025–2025 |   1 |        3.2 |      3.2 |   — |   3.2 |   3.2 |           — |          — |       — |       0.52 |
| Medical-services | 2025–2025 |   1 |        3.5 |      3.5 |   — |   3.5 |   3.5 |           — |          — |       — |       0.89 |
| Apparel          | 2025–2025 |   1 |        0.6 |      0.6 |   — |   0.6 |   0.6 |           — |          — |       — |      -2.04 |
| Tuition          | 2025–2025 |   1 |        2.9 |      2.9 |   — |   2.9 |   2.9 |           — |          — |       — |       0.26 |
| T-bill           | 2025–2025 |   1 |        4.2 |      4.2 |   — |   4.2 |   4.2 |           — |          — |       — |       1.57 |
| T-bond           | 2025–2025 |   1 |        7.8 |      7.8 |   — |   7.8 |   7.8 |           — |          — |       — |       5.16 |
| Stocks           | 2025–2025 |   1 |       17.8 |     17.8 |   — |  17.8 |  17.8 |           — |          — |       — |      15.14 |

At the level of these broad categories the tariff years were unremarkable:
CPI-U was 2.1% a year in 2018–2019 and 2.7% in 2025. Tariffs land on
particular imported goods, which these aggregates average away, and 2025 is
a single year. For a supply-shock row, run the script on the specific BLS
series that match the goods a plan buys.

```sh
uv run scripts/historical_calibration.py --reference Core=CUUR0000SA0L1E \
  CPI-U=CUUR0000SA0 Core=CUUR0000SA0L1E Food=CUUR0000SAF1 Energy=CUUR0000SA0E \
  Shelter=CUUR0000SAH1 Medical=CUUR0000SAM Medical-services=CUUR0000SAM2 \
  Apparel=CUUR0000SAA Tuition=CUUR0000SEEB \
  T-bill=damodaran:tbill T-bond=damodaran:tbond Stocks=damodaran:sp500
```

With no `--period`, the script prints the episodes above and the full
history.

## Where history is a weak guide

- **Short samples.** The Great Inflation is 17 annual rates; the
  post-pandemic episode is three. An SD from 17 points is uncertain by about
  ±17%, and ρ more so.
- **Regime changes.** The Fed's focus on low, stable inflation since the
  early 1980s, and the growing share of services in spending, both make the
  1970s an imperfect template.
- **Measurement changes.** CPI-U switched owners' housing costs to rental
  equivalence in January 1983 (CPI-W in January 1985), so shelter and
  all-items rates before 1983 measured something different; the calm
  baseline starts in 1984 so that no year straddles the switch. BLS also
  moved most basic indexes to geometric means in 1999. Its R-CPI-U-RS
  research series restates CPI-U from 1978 with current methods.
- **Missing months.** BLS didn't collect October 2025 prices during the
  lapse in appropriations. December-over-December rates don't need that
  month; annual averages computed from monthly data skip 2025 unless BLS
  published its own average.
- **Different horizons.** Every correlation here is between calendar-year
  rates, or between 10-year windows. A correlation measured over one
  horizon doesn't carry over to another.

## Sources

Verified and retrieved on 2026-10-05:

| Source | Covers | Frequency | Script |
| --- | --- | --- | --- |
| [BLS CPI flat files](https://download.bls.gov/pub/time.series/cu/) (`cu` CPI-U, [`cw`](https://download.bls.gov/pub/time.series/cw/) CPI-W, [`su`](https://download.bls.gov/pub/time.series/su/) C-CPI-U) | All CPI series by item and area. CPI-U and CPI-W all items from 1913, core from 1957, medical care from 1935, shelter from 1952, tuition from 1977, C-CPI-U from December 1999. | Monthly, plus annual averages | `bls:ID`, e.g. `bls:CUUR0000SAM`; ids are in each survey's `.series` file |
| [BLS R-CPI-E](https://www.bls.gov/cpi/research-series/r-cpi-e-home.htm) | CPI for Americans 62 and older: all items and eight major groups, from December 1982 | Monthly | `rcpie:COLUMN`, e.g. `rcpie:Medical care` |
| [BEA NIPA flat files](https://apps.bea.gov/national/Release/TXT/) | PCE price index (`DPCERG`) and core PCE (`DPCCRG`), from 1959, and every other monthly NIPA series | Monthly | `bea:CODE`; codes are in `SeriesRegister.txt` |
| [Damodaran, historical returns](https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/histretSP.html) | Annual returns on the S&P 500, small caps, 3-month T-bills (average yield over the year), 10-year T-bonds, Baa corporate bonds, real estate and gold, 1928–2025 (updated January 5, 2026) | Annual | `damodaran:COLUMN` (`sp500`, `small_cap`, `tbill`, `tbond`, `baa_corp`, `real_estate`, `gold`) |
| [Federal Reserve H.15](https://www.federalreserve.gov/releases/h15/) | Interest rates and Treasury yields, with daily, weekly, monthly and annual averages through the Data Download Program | Daily to annual | `csv:FILE` (reduce to `date,rate`) |
| [CMS National Health Expenditure data](https://www.cms.gov/data-research/statistics-trends-and-reports/national-health-expenditure-data/historical) | Health spending since 1960 | Annual | `csv:FILE` |

Not verified, because their hosts were unreachable from where this guide
was written:

- [FRED](https://fred.stlouisfed.org/) carries most of the series above
  under one interface (`fred:ID`; the script's FRED support is tested only
  against a stubbed download).
- [Shiller's long-run data](https://shillerdata.com/): stock prices,
  dividends, earnings, CPI and long rates since 1871.
- [SSA COLA history](https://www.ssa.gov/oact/cola/colaseries.html), and
  the Medicare Part B premium history from CMS's annual premium
  announcements.
- [Freddie Mac's mortgage rate survey](https://www.freddiemac.com/pmms).
- Expectations: the [Philadelphia Fed's Survey of Professional
  Forecasters](https://www.philadelphiafed.org/surveys-and-data/real-time-data-research/survey-of-professional-forecasters)
  and the [Cleveland Fed's inflation
  expectations](https://www.clevelandfed.org/indicators-and-data/inflation-expectations).

Any of these can be fed to the script as a two-column CSV: `date,value` for
an index level, or `date,rate` for a rate already in percent.

## Running the script

```sh
uv run scripts/historical_calibration.py [--reference SERIES] [--period NAME=FIRST-LAST ...] \
  [--window N] [--basis dec|average] [--runs-above PCT] [--format markdown|csv] \
  SERIES ...
```

- A series is `[LABEL=]SOURCE:ID`, with sources `bls`, `bea`, `rcpie`,
  `damodaran`, `fred` and `csv`; a bare id is a BLS series. Add `@rate` or
  `@index` to override whether values are rates or levels.
- A period's years may be open-ended (`Recent=2021-`). Without `--period`
  the script uses the episodes in this guide and the full history.
- Downloads are cached under `out/historical/`, with each file's retrieval
  date beside it; `--refresh` downloads again. BLS refuses requests without
  a contact email in the User-Agent, so the script sends `--contact`,
  `$HISTORICAL_CALIBRATION_CONTACT` or `git config user.email`.
- `--runs-above` lists stretches over every year a series has, whatever
  `--period` says.
