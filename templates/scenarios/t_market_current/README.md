# t_market_current

Today's cash yield, held to the end of 2029, then returning to the long run.

Builds on `t_market_ensemble`.

## Purpose

Starts cash at today's yield, holds it to the end of 2029, then lets it return to its level at its persistence. Bonds and stocks have no current start: with no persistence it would be gone in a year. Today's 4.0% is a placeholder until a monthly T-bill series is in `historical_data/` (#287).

A template scenario: data repos read it from the analysis code repository's `templates/scenarios/` unless they have a scenario of the same name, which replaces it whole. See docs/guides/historical-calibration.md, "Template scenarios".
