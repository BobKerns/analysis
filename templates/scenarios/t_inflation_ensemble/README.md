# t_inflation_ensemble

A year-by-year path for every inflation series, around a drawn long-run level.

Builds on `t_inflation_series`.

## Purpose

One `ar1` process row per inflation series, from 2027-01, each at ρ 1.0 to its factor. Each member draws a long-run level and moves around it every year, with the series' persistence and shock size. An ensemble of 80 members.

A template scenario: data repos read it from the analysis code repository's `templates/scenarios/` unless they have a scenario of the same name, which replaces it whole. See docs/guides/historical-calibration.md, "Template scenarios".
