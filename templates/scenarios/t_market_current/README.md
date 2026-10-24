# t_market_current

Today's cash yield, held to the end of 2029, then returning to the long run.

Builds on `t_market_ensemble`.

## Purpose

Starts cash at today's yield, holds it to the end of 2029, then lets it return to its level at its persistence. Bonds and stocks have no current start: with no persistence it would be gone in a year. Today's 4.0% is a placeholder until a monthly T-bill series is in `historical_data/` (#287).

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
          - **`t_market_current`** (this one): today's cash yield, held, then returning
