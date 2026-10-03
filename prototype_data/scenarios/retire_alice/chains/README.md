# Flow chain definitions

Flow chains are policies for how money is either obtained or distributed.

## Policies

A chain is a set of accounts that act as a unit.

* Every account is a chain with one element, with the same name
  * This allows accounts and chains to be used interchangeably
* Chains can have other chains as elements
* You can use a chain wherever an account name is expected. (The chain must be of the same type as the type of account expected, i.e. asset or income)
* Asset names follow the pattern \<asset|income>`__`\<name>`.csv`
* Assets can have an _refill_ chain. A refill chain is triggered when the asset balance drops below a threshold.

### Asset chains

An asset chain represents a set of accounts from which funds can be withdrawn. If one account lacks sufficient funds, it either transfers funds from the rest of the chain, or skips on to the rest of the chain.

#### Asset Chain Columns

* `account_name`—name of account or chain to supply funds
* `threshold`—if non-zero, a minimum of this amount will be maintained (if possible) by transferring funds after withdrawing the required amount
* `amount`—if non-zero and funds are insufficient, this amount is transferred from the rest of the chain first
* `percent`—if unspecified or 100, all of the remaining funds will be supplied by this account/chain. If $< 100$, that proportion of the outstanding amount to be transferred will be taken from this account, and move on to the next. (The last entry should always be 100).

### Income chains

An income chain distributes incoming funds to one or more target assets. An example is to split Social Security income between what pays Medicare Part B/D and what is deposited to a bank account.

#### Income Chain Columns

* `account_name`—name of account or chain to transfer funds to.
* `amount`—Fixed amount to transfer
* `percent`—Fraction of the remaining amount to transfer.

## Chains defined in this scenario

* `asset__asset_checking_liquidity.csv` (`asset_checking_liquidity`):
  the `refill_chain` for `Asset_Checking`, which has a `minimum_balance` of
  $2,000. It draws from `Asset_Savings` first, then from the `default` lot of
  `Asset_TraditionalIRA_Alice`.
