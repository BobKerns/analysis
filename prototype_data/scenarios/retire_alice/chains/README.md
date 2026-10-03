# Chains for the `retire_alice` scenario

A chain is a named, ordered list of places money can come from (or go to):
try this step first, then the next. Every account and lot already has its own
one-step chain, so a file here is only needed for more than one step. Each
file is named `<kind>__<name>.csv`, where `<kind>` is `asset` or `income`, and
the chain is referred to by `<name>`.

The column layout and how a draw moves through the steps are documented in:

- [ADR-003 §4.7](../../../../docs/architecture/ADR-003-registries-and-csv-schemas.md#47-per-scenario-data_reposcenariosnamechainskind__namecsv)
  (columns `account_name`, `lot`, `maximum`, `percent`, `series`, `cadence`)
- [User guide §4.6](../../../../docs/user-guide.md#46-chains-as-default-policies)
  (chains as default policies)

Refilling is not a chain column. A lot's `minimum_balance` and `refill_chain`
are set on its row in `account_values.csv`; before any payment that would take
the lot below its minimum, the simulator first draws from the refill chain.

## Chains defined in this scenario

- `asset__asset_checking_liquidity.csv` (`asset_checking_liquidity`):
  the `refill_chain` for `Asset_Checking`, which has a `minimum_balance` of
  $2,000. It draws from `Asset_Savings` first, then from the `default` lot of
  `Asset_TraditionalIRA_Alice`.
