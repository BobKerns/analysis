# t_great_inflation

A Great Inflation-style stretch on top of the long-run rates: years of high, persistent inflation, with stocks earning about inflation and bonds less.

Builds on `t_inflation_series` and `t_market_series`.

## Purpose

A mix-in for a scenario on the templates (`base:t_great_inflation`, or with an ensemble). It models dysfunction: policy that lets inflation run for years, as from 1966 to 1982.

- `great_inflation_start`, a date marker, is uniform from 2031 (after `normalization_start`) to 2064, so in a scenario ending in 2060 about one member in nine has no episode. Its median, the base case, is late 2047.
- `great_inflation_end` is 8 to 17 years after the start, most likely 12. History ran 17 years; its worst stretch, 1973 to 1981, ran 9.
- From the start, every inflation and market series runs an `ar1` path around its 1966–1982 compound rate. A persistent series starts from the rate in effect and climbs (`vary_process_start` `current`); one with no persistence (stocks, bonds, apparel) starts at its new level at once. CPI-U runs at 6.8%, shelter 7.7%, energy 9%, medical care 8.2%, cash 7.1%, bonds 5.1% and stocks 6.8%. So stocks earn about inflation, bonds less, and cash just keeps up.
- Where a series has no 1966–1982 history, its rate is inferred: CPI-W, R-CPI-E, chained CPI, services, tuition, the Part B premium, prescriptions, auto insurance and international stocks.
- At the end, every series goes back to what governed it before the stretch began (`vary_resume`): with the `_ensemble` templates, the long-run process, resumed from where the episode left it with its own shocks; with `base` alone, the series file's rate.

Place the episode by overriding the markers by name: a fixed date, or a narrower range.

A template scenario: data repos read it from the analysis code repository's `templates/scenarios/` unless they have a scenario of the same name, which replaces it whole. See [Template scenarios](../../../docs/guides/historical-calibration.md#template-scenarios) in the historical calibration guide.

## The templates

- [`t_empty`](../t_empty/README.md): nothing; the root
  - [`t_inflation_factors`](../t_inflation_factors/README.md): inflation's latent factors
    - [`t_inflation_series`](../t_inflation_series/README.md): long-run inflation rates
      - [`t_inflation_ensemble`](../t_inflation_ensemble/README.md): year-by-year inflation paths
        - [`t_inflation_current`](../t_inflation_current/README.md): today's inflation rates, held, then returning
    - [`t_market_factors`](../t_market_factors/README.md): interest-rate and stock factors
      - [`t_market_series`](../t_market_series/README.md): long-run cash, bond and stock rates
        - [`t_market_ensemble`](../t_market_ensemble/README.md): year-by-year market paths
          - [`t_market_current`](../t_market_current/README.md): today's cash yield, held, then returning

Episodes, each a mix-in building on `t_inflation_series` and `t_market_series`:

- **`t_great_inflation`** (this one): a dysfunction: inflation runs for years (1966–1982)
- [`t_financial_crisis`](../t_financial_crisis/README.md): a fiscal shock: a crash, then years of near-zero rates (2008–2019)
- [`t_pandemic_spike`](../t_pandemic_spike/README.md): an external shock: a short price spike, then rate hikes (2021–2023)

And one that puts two of them in one lifetime:

- [`t_crisis_then_spike`](../t_crisis_then_spike/README.md): a crisis, then a spike 1 to 10 years after its near-zero rates end (2008–2023)
