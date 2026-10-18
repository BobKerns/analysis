# `prices/` — security price histories

`security_price__<SYMBOL>.csv` holds one security's price history
(`effective_date,price`, price in decimal dollars with up to 6 decimal
places). Unlike growth-rate and inflation series (`series/`, which are
per-scenario projections), a security's price history is a shared fact:
every scenario that holds the same symbol uses the same prices by default.
This folder sits at the data repo root, like `aux/`.

## What it's used for

The loader uses the latest price on or before the scenario start to turn a
lot's `quantity` into an `opening_balance`, when that balance is left blank
in `account_lots.csv` (ADR-005 §6). It is not used month to month — after
that a lot grows by its own rate series, from `series/`, not by price.

## Fetching prices

`sim prices fetch` populates this folder for you: it takes every symbol held
with a `quantity` (across all scenarios), fetches month-end and latest closes
from a public price source, and writes them here. See `templates/README.md`
("Fetching prices") for the full behavior and options.

## Per-scenario overrides

Like `aux/`, a scenario can replace one file for itself (and its
descendants) by adding `scenarios/<name>/prices/<same filename>.csv` — the
loader uses that in place of the shared one here. This is rarely needed
(prices are the same regardless of scenario), but it's available for, say, a
what-if scenario that assumes a different price path for one holding.

## See also

- `docs/architecture/ADR-003-registries-and-csv-schemas.md` — column schema
  and the per-scenario override mechanism (§4.1d).
- `aux/README.md` — the shared tax-law facts.
- `templates/README.md` — the data repo as a whole.
