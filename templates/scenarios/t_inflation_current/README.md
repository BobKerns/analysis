# t_inflation_current

Today's inflation rates, held to the end of 2029, then returning to the long run.

Builds on `t_inflation_ensemble`.

## Purpose

Starts the persistent inflation series at their latest 12-month rates (to August 2026), holds them to the end of 2029, and then lets each return to its long-run level at its own persistence (`vary_process_start` `current`). The hold is a political judgment, not a statistical one. Medical care is left at its long-run level: its low current rate is taken as a short-lived correction. Series with little persistence (food, apparel, energy, the Part B premium) keep the ensemble's rows.

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
