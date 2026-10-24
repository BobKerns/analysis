# t_empty

Nothing at all: no accounts, rules, rates or factors.

## Purpose

The root of the template hierarchy, and a starting point for a scenario built from nothing. It sets only a start and end date, since a scenario needs them; anything that builds on it can set its own. Whether a scenario has `t_empty` in its lineage or not changes nothing else.

A template scenario: data repos read it from the analysis code repository's `templates/scenarios/` unless they have a scenario of the same name, which replaces it whole. See docs/guides/historical-calibration.md, "Template scenarios".
