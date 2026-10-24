# t_inflation_series

Long-run inflation rates for every inflation series.

Builds on `t_inflation_factors`.

## Purpose

One rate per inflation series in `series/`, the long-run values for a 30-year projection: recent decades weighted more, and past episodes not averaged in. CPI-U itself stays in the shared `aux/inflation__cpi.csv`. Every future runs at these rates until a scenario varies them.

A template scenario: data repos read it from the analysis code repository's `templates/scenarios/` unless they have a scenario of the same name, which replaces it whole. See docs/guides/historical-calibration.md, "Template scenarios".
