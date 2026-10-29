# t_inflation_current

Today's inflation rates, held until federal fiscal year 2030 begins, then returning to the long run.

Builds on `t_inflation_ensemble`.

## Purpose

Starts the persistent inflation series at their latest 12-month rates (to August 2026), holds them until the `normalization_start` date marker, and then lets each return to its long-run level at its own persistence (`vary_process_start` `current`). The hold is a political judgment, not a statistical one: the budget and the fiscal calendar are the only fixed cues, so `normalization_start` is triangular around the start of fiscal 2030 (October 2029), from three months before to six after. Its median, the base case, is the end of October 2029. Override the marker by name to move every held series at once. Medical care is left at its long-run level: its low current rate is taken as a short-lived correction. Series with little persistence (food, apparel, energy, the Part B premium) keep the ensemble's rows.

A template scenario: data repos read it from the analysis code repository's `templates/scenarios/` unless they have a scenario of the same name, which replaces it whole. See [Template scenarios](../../../docs/guides/historical-calibration.md#template-scenarios) in the historical calibration guide.

## The templates

- [`t_empty`](../t_empty/README.md): nothing; the root
  - [`t_inflation_factors`](../t_inflation_factors/README.md): inflation's latent factors
    - [`t_inflation_series`](../t_inflation_series/README.md): long-run inflation rates
      - [`t_inflation_ensemble`](../t_inflation_ensemble/README.md): year-by-year inflation paths
        - **`t_inflation_current`** (this one): today's inflation rates, held, then returning
    - [`t_market_factors`](../t_market_factors/README.md): interest-rate and stock factors
      - [`t_market_series`](../t_market_series/README.md): long-run cash, bond and stock rates
        - [`t_market_ensemble`](../t_market_ensemble/README.md): year-by-year market paths
          - [`t_market_current`](../t_market_current/README.md): today's cash yield, held, then returning

Episodes, each a mix-in building on `t_inflation_series` and `t_market_series`:

- [`t_great_inflation`](../t_great_inflation/README.md): a dysfunction: inflation runs for years (1966–1982)
- [`t_financial_crisis`](../t_financial_crisis/README.md): a fiscal shock: a crash, then years of near-zero rates (2008–2019)
- [`t_pandemic_spike`](../t_pandemic_spike/README.md): an external shock: a short price spike, then rate hikes (2021–2023)

And one that puts two of them in one lifetime:

- [`t_crisis_then_spike`](../t_crisis_then_spike/README.md): a crisis, then a spike 1 to 10 years after its near-zero rates end (2008–2023)
