# `series/` — growth rates and security prices

These CSVs are the things a scenario's own files name by `series` or `symbol`:
per-account growth rates and other series-keyed reference data (state tax
brackets). They sit next to `aux/` (the tax-law facts every scenario shares by
default; see `aux/README.md`) but are grouped separately because they are
properly per-scenario projections rather than facts about the tax code. The
one exception is `inflation__cpi.csv`, which stays in `aux/` so CPI-constant
reporting always has a default.

## Filename pattern

`series/<kind>__<series>.csv`, a double underscore between the kind and the
series name (`rate__Asset_Checking.csv`, `state_brackets__CA.csv`). The loader
parses the filename the same way it does for `aux/`; the required columns per
kind are in `src/analysis/sim/registries.py` (`AUX_KIND_COLUMNS`) and
[ADR-003](../../docs/architecture/ADR-003-registries-and-csv-schemas.md) §4.4.

## Per-scenario overrides

A scenario can replace one of these files for itself (and its descendants) by
adding `scenarios/<name>/series/<same filename>.csv`, or add a file the shared
folder doesn't have at all. The loader uses the most specific file that exists
— the scenario's own, else its parent's, else this shared default — whole
file, not merged row by row ([ADR-003](../../docs/architecture/ADR-003-registries-and-csv-schemas.md) §4.1c).

## The files in this folder

| File | What it is |
| --- | --- |
| `state_brackets__CA.csv` | California state ordinary-income brackets. Same columns as `aux/federal_brackets.csv`. |
| `rate__Asset_Checking.csv` | Checking account's own growth (interest) rate, matching `accounts.csv`'s `series` column for `Asset_Checking`. |
| `rate__brokerage_growth.csv` | The brokerage account's growth rate. |

## See also

- [`docs/architecture/ADR-003-registries-and-csv-schemas.md`](../../docs/architecture/ADR-003-registries-and-csv-schemas.md) — authoritative column lists,
  the kind/series naming rules, and the per-scenario override mechanism (§4.1c).
- `aux/README.md` — the tax-law facts that stay shared by default.
- `templates/README.md` — the data repo as a whole.
