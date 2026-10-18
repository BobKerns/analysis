# Source chains

A chain in this folder is **drawn from**: money comes out of its steps, the
first step first. Name a source chain as a rule's `from_account`, a calendar
transfer's `account_1`, or a lot's `refill_chain` in `account_lots.csv`.
A step that names another chain must name another source chain.

Each file is `<name>.csv`, and the chain is referred to by `<name>`.
Columns: [ADR-003 §4.7](../../../../../docs/architecture/ADR-003-registries-and-csv-schemas.md#47-per-scenario-data_reposcenariosnamechainsrolenamecsv).
