# Chains for the `retire_alice` scenario

A chain is a named, ordered list of places money can come from (or go to):
try this step first, then the next. Every account and lot already has its own
one-step chain, so a file here is only needed for more than one step. A chain
drawn from (a rule's `from_account`, a lot's `refill_chain`) goes in
`source/`; a chain paid into (a rule's `to_account`, a lot's `overflow_chain`)
goes in `destination/`. Each file is named `<name>.csv`, and the chain is
referred to by `<name>`.

The column layout and how a draw moves through the steps are documented in:

- [ADR-003 §4.7](../../../../docs/architecture/ADR-003-registries-and-csv-schemas.md#47-per-scenario-data_reposcenariosnamechainsrolenamecsv)
  (columns `account_name`, `lot`, `maximum`, `percent`, `series`, `cadence`)
- [User guide §4.6](../../../../docs/user-guide.md#46-chains-as-default-policies)
  (chains as default policies)

Refilling is not a chain column. A lot's `minimum_balance` and `refill_chain`
are set on its row in `account_lots.csv`; before any payment that would take
the lot below its minimum, the simulator first draws from the refill chain.

## Chains defined in this scenario

- `source/asset_checking_liquidity.csv` (`asset_checking_liquidity`):
  the `refill_chain` for `Asset_Checking`, which has a `minimum_balance` of
  $2,000. It draws from `Asset_Savings` first, then from the `default` lot of
  `Asset_TraditionalIRA_Alice`.
