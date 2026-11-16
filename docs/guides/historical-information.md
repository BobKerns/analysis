# How much information is in the historical series?

> To read a formatted `.md` file:
> - In Jupyter: right-click on it and select `Open With / Markdown Preview`
> - In VSCode: Command `Markdown Preview Enhanced: Open Preview` or ⌘⇧V

<!-- cspell:ignore prequential prequentially -->

The [historical calibration guide](historical-calibration.md) chooses how
each template series moves from year to year: stock and bond returns with no
memory, yields with a persistence near 0.9, inflation with a modest
persistence plus [episodes](historical-calibration.md#historical-episodes)
on top. This page checks those choices from a different direction, with a
compression test: how few bits does it take to write down each series, and
how much do the models used in the templates save over a description with
no memory at all? Anything a model can't explain shows up as bits it can't
save, and a generic compressor gives an independent check on whether there
is order left that the models miss.

Everything here comes from the data committed under
[`historical_data/`](../../historical_data/README.md), through
[`scripts/historical_compression.py`](../../scripts/historical_compression.py).
It reads no network, takes about twenty seconds, and prints the tables
below as Markdown, so anyone can rerun it:

```sh
uv run scripts/historical_compression.py
uv run scripts/historical_compression.py --start-year 2000 --end-year 2025
```

`--start-year` and `--end-year` keep only the years between them (inclusive),
for every series including monthly CPI.
The script declares its own dependencies in a PEP 723 header, with this
checkout as one of them, so `uv run` builds its environment without the
project's `.venv`.

## What it shows

- **Stock and bond returns are incompressible.** No model beats "one level,
  no memory" (stocks 9.7 bits a year, bonds 8.4), and xz on the real order is
  no shorter than on a shuffle. Every year is news, which is why the
  templates give `stock_us` and `bond` a persistence of 0.
- **Yields carry real memory.** AR(1) saves about 1.3 to 1.6 bits a year on
  the T-bill and 10-year yields (7.0 to 5.7 and 5.4 bits a year). Their
  persistence is near 0.9, and the saving, ½·log₂(1/(1−φ²)), grows fast as
  φ nears 1. The templates give `cash` 0.85.
- **Annual inflation's memory is worth only 0.3 bits a year** (7.4 to 7.1).
  A persistence of 0.65 sounds strong but buys little. Laplace or Student-t
  shocks save as much again, because inflation's shocks are heavy-tailed: a
  few years are far from the rest. That is the case for the templates'
  separate episodes over a single bigger shock.
- **Monthly CPI hides structure the AR(1) misses, before about 1980.** Over
  1947–2026, xz finds 1.0 bits a month of order, against AR(1)'s 0.28. The
  gap is mostly in 1947–1979 (xz 1.5, AR(1) 0.27); it narrows over
  1980–2025 (0.44 against 0.26) and nearly closes over 2000–2025 (0.18
  against 0.17), where the AR(1) has it all. Two likely causes: a slow
  regime under fast monthly noise (the 1970s), which is the episode
  structure the templates model separately; and the early index's coarse
  rounding (one decimal on an index near 25), which repeats month-to-month
  values a compressor can exploit.
- **Generic compressors are poor absolute estimators** on series this short:
  their byte-level overhead makes them 50 to 70% longer than the model
  codes. Only their real-to-shuffled ratio means anything here.

## How the test works

Each series is rounded (to 0.1 point for the yearly rates, 0.01 point for
the monthly CPI changes), so it has an exact length in bits once a model
assigns each value a probability. The length under a model is the
*minimum description length*: the bits to code the data given the model,
plus about ½·log₂ n bits for each fitted parameter, the precision n data
points justify. A model that saves fewer bits than its parameters cost
comes out longer, so AR(2) and AR(3) mostly lose to AR(1).

The descriptions tried:

| Description | What it assumes |
| --- | --- |
| one level, no memory (iid normal) | Every year a fresh draw around one mean |
| AR(1), AR(2), AR(3) normal | Each value leans on the last one, two or three, with normal shocks |
| AR(1) Laplace shocks | The same AR(1), with heavier-tailed shocks |
| AR(1) Student-t shocks | Heavier tails still, the tail weight (ν) chosen from a grid |
| AR(1), shock SD from the last 10 years (36 months) | The shock size follows recent volatility, coded *prequentially*: each value with an SD estimated only from values already sent |

The compressors are zlib, bz2 and xz (raw LZMA2, without the `.xz`
container's header), each run on the rounded series as 16-bit integers in
its real order and on 50 shuffled copies. A shuffle keeps the values and
destroys any order in time, so a real-to-shuffled ratio below 1 means the
compressor found some order; near 1, none.

## Tables

The yearly series run 1928–2025 (the 10-year yield from 1962); monthly
CPI-U from February 1947 to the latest committed month.

### Inflation, 1928–2025: 98 years, rounded to 0.1 point

| Description | params | bits total | bits per year |
| --- | ---: | ---: | ---: |
| one level, no memory (iid normal) | 2 | 724 | 7.39 |
| AR(1) normal | 3 | 695 | 7.09 |
| AR(2) normal | 4 | 698 | 7.12 |
| AR(3) normal | 5 | 702 | 7.17 |
| AR(1) Laplace shocks | 3 | 667 | 6.81 |
| AR(1) Student-t shocks (nu=2.5) | 4 | 671 | 6.85 |
| AR(1), shock SD from the last 10 years | 3 | 695 | 7.09 |

| Compressor | real order, bits | shuffled, bits | real / shuffled |
| --- | ---: | ---: | ---: |
| zlib | 1,288 | 1,298 | 0.992 |
| bz2 | 1,408 | 1,477 | 0.953 |
| xz (raw) | 1,200 | 1,259 | 0.953 |

### 10-yr yield, 1962–2025: 64 years, rounded to 0.1 point

| Description | params | bits total | bits per year |
| --- | ---: | ---: | ---: |
| one level, no memory (iid normal) | 2 | 448 | 7.00 |
| AR(1) normal | 3 | 345 | 5.39 |
| AR(2) normal | 4 | 349 | 5.45 |
| AR(3) normal | 5 | 351 | 5.49 |
| AR(1) Laplace shocks | 3 | 344 | 5.37 |
| AR(1) Student-t shocks (nu=7) | 4 | 347 | 5.42 |
| AR(1), shock SD from the last 10 years | 3 | 342 | 5.34 |

| Compressor | real order, bits | shuffled, bits | real / shuffled |
| --- | ---: | ---: | ---: |
| zlib | 904 | 922 | 0.981 |
| bz2 | 944 | 947 | 0.997 |
| xz (raw) | 792 | 860 | 0.920 |

### T-bill yield, 1928–2025: 98 years, rounded to 0.1 point

| Description | params | bits total | bits per year |
| --- | ---: | ---: | ---: |
| one level, no memory (iid normal) | 2 | 689 | 7.03 |
| AR(1) normal | 3 | 562 | 5.74 |
| AR(2) normal | 4 | 558 | 5.70 |
| AR(3) normal | 5 | 554 | 5.65 |
| AR(1) Laplace shocks | 3 | 554 | 5.65 |
| AR(1) Student-t shocks (nu=5) | 4 | 564 | 5.75 |
| AR(1), shock SD from the last 10 years | 3 | 582 | 5.94 |

| Compressor | real order, bits | shuffled, bits | real / shuffled |
| --- | ---: | ---: | ---: |
| zlib | 1,208 | 1,182 | 1.022 |
| bz2 | 1,208 | 1,238 | 0.976 |
| xz (raw) | 1,064 | 1,131 | 0.941 |

### Stock return, 1928–2025: 98 years, rounded to 0.1 point

| Description | params | bits total | bits per year |
| --- | ---: | ---: | ---: |
| one level, no memory (iid normal) | 2 | 951 | 9.71 |
| AR(1) normal | 3 | 955 | 9.74 |
| AR(2) normal | 4 | 957 | 9.76 |
| AR(3) normal | 5 | 960 | 9.80 |
| AR(1) Laplace shocks | 3 | 962 | 9.81 |
| AR(1) Student-t shocks (nu=50) | 4 | 958 | 9.77 |
| AR(1), shock SD from the last 10 years | 3 | 951 | 9.71 |

| Compressor | real order, bits | shuffled, bits | real / shuffled |
| --- | ---: | ---: | ---: |
| zlib | 1,552 | 1,560 | 0.995 |
| bz2 | 1,816 | 1,793 | 1.013 |
| xz (raw) | 1,600 | 1,601 | 1.000 |

### Bond return, 1928–2025: 98 years, rounded to 0.1 point

| Description | params | bits total | bits per year |
| --- | ---: | ---: | ---: |
| one level, no memory (iid normal) | 2 | 824 | 8.41 |
| AR(1) normal | 3 | 827 | 8.44 |
| AR(2) normal | 4 | 831 | 8.48 |
| AR(3) normal | 5 | 832 | 8.49 |
| AR(1) Laplace shocks | 3 | 815 | 8.32 |
| AR(1) Student-t shocks (nu=2.5) | 4 | 822 | 8.39 |
| AR(1), shock SD from the last 10 years | 3 | 835 | 8.52 |

| Compressor | real order, bits | shuffled, bits | real / shuffled |
| --- | ---: | ---: | ---: |
| zlib | 1,416 | 1,463 | 0.968 |
| bz2 | 1,584 | 1,630 | 0.972 |
| xz (raw) | 1,408 | 1,440 | 0.977 |

### CPI-U month-over-month (SA), 1947-02–2026-08: 954 months, rounded to 0.01 point

| Description | params | bits total | bits per month |
| --- | ---: | ---: | ---: |
| one level, no memory (iid normal) | 2 | 6,814 | 7.14 |
| AR(1) normal | 3 | 6,548 | 6.86 |
| AR(2) normal | 4 | 6,550 | 6.87 |
| AR(3) normal | 5 | 6,528 | 6.84 |
| AR(1) Laplace shocks | 3 | 6,420 | 6.73 |
| AR(1) Student-t shocks (nu=3) | 4 | 6,421 | 6.73 |
| AR(1), shock SD from the last 36 months | 3 | 6,505 | 6.82 |

| Compressor | real order, bits | shuffled, bits | real / shuffled |
| --- | ---: | ---: | ---: |
| zlib | 9,256 | 9,926 | 0.932 |
| bz2 | 9,176 | 9,738 | 0.942 |
| xz (raw) | 8,128 | 9,094 | 0.894 |

### Monthly CPI by period

The bits each description saves a month over "one level, no memory", from
runs with `--start-year`/`--end-year`:

| Period | Months | AR(1) saves | xz finds (shuffled − real) |
| --- | ---: | ---: | ---: |
| 1947–2026 | 954 | 0.28 | 1.01 |
| 1947–1979 | 395 | 0.27 | 1.51 |
| 1980–2025 | 551 | 0.26 | 0.44 |
| 2000–2025 | 311 | 0.17 | 0.18 |

## Limits

- **Short series.** Ninety-eight yearly values can't distinguish fine
  differences between models: a gap of a few bits in total is noise. The
  conclusions above rest on gaps of tens to hundreds of bits.
- **Rounding sets the scale.** Every total depends on the rounding step;
  a finer step adds the same bits per value to every description, so compare
  descriptions within a table, not across tables.
- **The models are the templates' own.** A model outside this list (a
  regime switch, a link between series) might find more. The compressors are
  the check that nothing large is left, and they are blunt.
