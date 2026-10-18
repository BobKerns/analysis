# Destination chains

A chain in this folder is **paid into**: money goes into its steps, the first
step first. Name a destination chain as a rule's `to_account`, a calendar
transfer's `account_2`, or a lot's `overflow_chain` in `account_lots.csv`.
A step that names another chain must name another destination chain.

Each file is `<name>.csv`, and the chain is referred to by `<name>`.
Columns: [ADR-003 §4.7](../../../../../docs/architecture/ADR-003-registries-and-csv-schemas.md#47-per-scenario-data_reposcenariosnamechainsrolenamecsv).
