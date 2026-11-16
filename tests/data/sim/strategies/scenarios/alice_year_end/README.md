# alice_year_end

Alice and Bill's year-end actions (#421): a `harvest` strategy fills the 0%
long-term gains band from the joint brokerage each December, and federal
income tax is settled each April so the harvest can be checked against it.
`Expense_Family` takes gifts when a test adds a `gift` strategy.

Builds on: alice_rebalance

## Purpose

Tests for the `year_end` hook, `ctx.tax`, and the `harvest` and `gift` built-ins.
