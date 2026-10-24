# t_financial_crisis

A 2008-style crash on top of the long-run rates: one bad year for stocks, then a recovery, with cash near zero and inflation low for years.

Builds on `t_inflation_series` and `t_market_series`.

## Purpose

A mix-in for a scenario on the templates (`base:t_financial_crisis`, or with an ensemble). It models a fiscal shock: a financial crash, then years of near-zero rates, as from 2008 to 2019.

- `financial_crisis_start`, a date marker, is uniform from 2031 to 2064, like `t_great_inflation`'s. Its median, the base case, is late 2047.
- `financial_crisis_end` is 5 to 10 years after the start, most likely 7. The zero-rate years ran from 2009 to 2015.
- The crash year, held for 12 months: US stocks fall 37% (from −50% to −15%), international stocks 43%, and energy 20%. Bonds gain 12%, as Treasuries did in 2008 (+20%).
- From a year after the start until the end, stocks recover at 11% (international 6%) and bonds earn 3%.
- From the start until the end, cash earns 0.5%, and prices run low: CPI-U at 1.7%, shelter 2.4%, medical care 3%, and tuition 3.5%. The Part B premium, prescriptions and auto insurance keep their long-run paths.
- The damage is sequence risk: selling in the crash year, then cash losing about a point a year to inflation. Stocks recover fully, as they did.
- At the end, every series it changed goes back to what governed it before the crash (`vary_resume`): the long-run process, resumed with its own shocks, or the series file's rate.

Place the episode by overriding the markers by name.

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

- [`t_great_inflation`](../t_great_inflation/README.md): a dysfunction: inflation runs for years (1966–1982)
- **`t_financial_crisis`** (this one): a fiscal shock: a crash, then years of near-zero rates (2008–2019)
- [`t_pandemic_spike`](../t_pandemic_spike/README.md): an external shock: a short price spike, then rate hikes (2021–2023)
