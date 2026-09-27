# `series/` — growth rates and security prices

These CSVs are the things a scenario's own files name by `series` or `symbol`:
per-account and per-symbol growth rates, security prices, and other
series-keyed reference data (state tax brackets). They sit next to `aux/`
(the tax-law facts every scenario shares by default; see `aux/README.md`) but
are grouped separately because they are properly per-scenario projections —
assumptions a scenario author picks and can reasonably want to vary — rather
than facts about the tax code. The one exception is `inflation__cpi.csv`,
which stays in `aux/` so CPI-constant reporting always has a default; see
`aux/README.md`.

## Filename pattern

`series/<kind>__<series>.csv`, a double underscore between the kind and the
series name (`rate__Asset_Brokerage.csv`, `security_price__VTI.csv`,
`state_brackets__CA.csv`). The loader parses the filename the same way it does
for `aux/`; the required columns per kind are in
`src/analysis/sim/registries.py` (`AUX_KIND_COLUMNS`) and [ADR-003](../../docs/architecture/ADR-003-registries-and-csv-schemas.md) §4.4.

## Per-scenario overrides

A scenario can replace one of these files for itself (and its descendants) by
adding `scenarios/<name>/series/<same filename>.csv`, or add a file the shared
folder doesn't have at all (a rate for a symbol only that scenario holds). The
loader uses the most specific file that exists — the scenario's own, else its
parent's, else this shared default — whole file, not merged row by row
([ADR-003](../../docs/architecture/ADR-003-registries-and-csv-schemas.md) §4.1c). This is the natural place to model a different assumed
return, or a different future security price path, on a per-scenario basis.

## The files in this folder

| File | What it is |
| --- | --- |
| `state_brackets__CA.csv` | California state ordinary-income brackets. Same columns as `aux/federal_brackets.csv`. |
| `rate__example.csv` | Template for a per-account growth/interest-rate series. Copy and rename to e.g. `rate__Asset_Brokerage.csv`, matching an `accounts.csv` `series` value. |
| `security_price__EXAMPLE.csv` | Template for a symbol's price history. Copy and rename to `security_price__<SYMBOL>.csv`, matching an `account_values.csv` lot's `symbol`. |

## Security prices

`security_price__<SYMBOL>.csv` holds one security's price history (`effective_date,price`,
price in decimal dollars with up to 6 decimal places). The loader uses the latest price on or
before the scenario start to turn a lot's `quantity` into an `opening_balance` when that
balance is left blank in `account_values.csv`; it is not used month to month. `sim prices
fetch` can populate this file for you. To give a security its own growth rate, add
`rate__<SYMBOL>.csv` (same series name).

## See also

- [`docs/architecture/ADR-003-registries-and-csv-schemas.md`](../../docs/architecture/ADR-003-registries-and-csv-schemas.md) — authoritative column lists,
  the kind/series naming rules, and the per-scenario override mechanism (§4.1c).
- `aux/README.md` — the tax-law facts that stay shared by default.
- `templates/README.md` — the data repo as a whole.
