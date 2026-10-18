# `prices/` — security price histories

`security_price__<SYMBOL>.csv` holds one security's price history
(`effective_date,price`, price in decimal dollars with up to 6 decimal
places). Unlike growth-rate series (`series/`, per-scenario), a security's
price history is a shared fact: every scenario that holds the same symbol
uses the same prices by default. This folder sits at the data repo root,
like `aux/`.

The loader uses the latest price on or before the scenario start to turn a
lot's `quantity` into an `opening_balance`, when that balance is left blank
in `account_lots.csv` (ADR-005 §6). It is not used month to month.

## See also

- `docs/architecture/ADR-003-registries-and-csv-schemas.md` — column schema
  and the per-scenario override mechanism (§4.1d).
- `aux/README.md` — the shared tax-law facts.
