# t_market_series

Long-run rates for cash, bonds and stocks.

Builds on `t_market_factors`.

## Purpose

One rate per fund asset class in `series/`: deliberately below their history since 1984, which rode falling interest rates and rising valuations.

A template scenario: data repos read it from the analysis code repository's `templates/scenarios/` unless they have a scenario of the same name, which replaces it whole. See docs/guides/historical-calibration.md, "Template scenarios".
