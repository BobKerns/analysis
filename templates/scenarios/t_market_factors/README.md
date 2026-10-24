# t_market_factors

Interest-rate and stock-market latent factors, under the inflation factors.

Builds on `t_inflation_factors`.

## Purpose

`interest_rates` hangs under `core_inflation`, with cash and bonds under it, because short-term yields follow inflation over a decade. Stocks get a separate root, `market`, since their returns barely follow inflation or bonds.

A template scenario: data repos read it from the analysis code repository's `templates/scenarios/` unless they have a scenario of the same name, which replaces it whole. See docs/guides/historical-calibration.md, "Template scenarios".
