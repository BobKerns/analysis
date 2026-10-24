# t_market_ensemble

A year-by-year path for cash, bonds and stocks, around a drawn long-run level.

Builds on `t_market_series`.

## Purpose

One `ar1` process row per market series, from 2027-01. Stocks and bonds have no persistence, so each year is a fresh draw around the level; cash yields are persistent. An ensemble of 80 members.

A template scenario: data repos read it from the analysis code repository's `templates/scenarios/` unless they have a scenario of the same name, which replaces it whole. See docs/guides/historical-calibration.md, "Template scenarios".
