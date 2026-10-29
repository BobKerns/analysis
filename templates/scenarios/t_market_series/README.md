# t_market_series

Long-run rates for cash, bonds and stocks.

Builds on `t_market_factors`.

## Purpose

One rate per fund asset class in `series/`: deliberately below their history since 1984, which rode falling interest rates and rising valuations.

A template scenario: data repos read it from the analysis code repository's `templates/scenarios/` unless they have a scenario of the same name, which replaces it whole. See [Template scenarios](../../../docs/guides/historical-calibration.md#template-scenarios) in the historical calibration guide.

## The templates

- [`t_empty`](../t_empty/README.md): nothing; the root
  - [`t_inflation_factors`](../t_inflation_factors/README.md): inflation's latent factors
    - [`t_inflation_series`](../t_inflation_series/README.md): long-run inflation rates
      - [`t_inflation_ensemble`](../t_inflation_ensemble/README.md): year-by-year inflation paths
        - [`t_inflation_current`](../t_inflation_current/README.md): today's inflation rates, held, then returning
    - [`t_market_factors`](../t_market_factors/README.md): interest-rate and stock factors
      - **`t_market_series`** (this one): long-run cash, bond and stock rates
        - [`t_market_ensemble`](../t_market_ensemble/README.md): year-by-year market paths
          - [`t_market_current`](../t_market_current/README.md): today's cash yield, held, then returning

Episodes, each a mix-in building on `t_inflation_series` and `t_market_series`:

- [`t_great_inflation`](../t_great_inflation/README.md): a dysfunction: inflation runs for years (1966–1982)
- [`t_financial_crisis`](../t_financial_crisis/README.md): a fiscal shock: a crash, then years of near-zero rates (2008–2019)
- [`t_pandemic_spike`](../t_pandemic_spike/README.md): an external shock: a short price spike, then rate hikes (2021–2023)

And one that puts two of them in one lifetime:

- [`t_crisis_then_spike`](../t_crisis_then_spike/README.md): a crisis, then a spike 1 to 10 years after its near-zero rates end (2008–2023)
