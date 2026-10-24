# t_inflation_current

Today's inflation rates, held to the end of 2029, then returning to the long run.

Builds on `t_inflation_ensemble`.

## Purpose

Starts the persistent inflation series at their latest 12-month rates (to August 2026), holds them to the end of 2029, and then lets each return to its long-run level at its own persistence (`vary_process_start` `current`). The hold is a political judgment, not a statistical one. Medical care is left at its long-run level: its low current rate is taken as a short-lived correction. Series with little persistence (food, apparel, energy, the Part B premium) keep the ensemble's rows.

A template scenario: data repos read it from the analysis code repository's `templates/scenarios/` unless they have a scenario of the same name, which replaces it whole. See docs/guides/historical-calibration.md, "Template scenarios".
