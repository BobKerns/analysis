# t_inflation_factors

Inflation's latent factors and their correlations, and nothing else.

## Purpose

A latent factor for each inflation series, linked into one tree under `core_inflation`, with each link's ρ from history. No rates and no varying rows: scenarios that vary an inflation series hang their rows off these factors, so they share one correlation structure. The market factors (`t_market_factors`) hang under this tree too.

A template scenario: data repos read it from the analysis code repository's `templates/scenarios/` unless they have a scenario of the same name, which replaces it whole. See docs/guides/historical-calibration.md, "Template scenarios".
