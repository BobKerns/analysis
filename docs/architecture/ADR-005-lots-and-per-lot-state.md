# ADR-005: Lots and per-lot state

- **Status:** Accepted
- **Date:** 2026-09-23
- **Supersedes:** the reserved `holding` / `lot` tables and `transfer_event.from_holding_id` / `to_holding_id` ([ADR-002](ADR-002-storage-schema.md) §Reserved schema); the account-level running cost basis that [ADR-003](ADR-003-registries-and-csv-schemas.md) §4.7 (withdrawing from a taxable lot) now describes per lot.
- **Amended:** see [Amendments](#amendments) at the end.
- **Related:** [ADR-001](ADR-001-overall-architecture.md) (domain objects, `AccountState`), [ADR-002](ADR-002-storage-schema.md) (storage schema), [ADR-003](ADR-003-registries-and-csv-schemas.md) (registries, CSV layouts, chains §4.7, `rmd` §2.3).

## Context

Every account is one balance with one running cost basis. A brokerage or IRA that holds several securities bought at different times cannot be modelled below the account: a withdrawal blends the whole account's basis pro-rata (`engine._post_withdrawal`), and growth is one rate per account. We want to:

1. Track individual positions (lots) inside investment accounts, including IRAs, and say which lot a sale comes from.
2. Model growth per security rather than per account.
3. Derive opening balances from quantities and prices when we have them.
4. Change as little of the engine, reporting, and notebook code as possible.

[ADR-002](ADR-002-storage-schema.md) reserved `holding` / `lot` tables and `transfer_event.from_holding_id` / `to_holding_id` for this, but nothing reads or writes them. They are replaced here, because a separate holding table adds a second file to author and a second identity to keep in step, for no information a `GROUP BY account, symbol` over lots doesn't give.

## Decision

### 1. A lot is a row of `account_lot`

`account_lot` is one row per **(account, lot)**, with a surrogate `id`.

| column | type | notes |
| --- | --- | --- |
| `id` | int PK | the **lot id**; the engine keys balances, basis and quantity on it |
| `account_id` | int | the account the lot belongs to |
| `lot` | text NOT NULL | free-form label, unique within the account; `'default'` for accounts that are not split. Sequential numbers (`1`, `2`) are a fine convention, and so are acquisition dates or any other name without `:` |
| `subkind` | text | a `retirement` account's lot only, and required there: `traditional_ira`, `roth_ira`, `traditional_401k` or `roth_401k` ([ADR-003](ADR-003-registries-and-csv-schemas.md) §1a) |
| `tax_treatment` | text | the lot's effective tax treatment, resolved at load: its subkind's (Roth `tax_free`, traditional `tax_deferred`) on a retirement lot, else its account's. The engine reads a lot's treatment from here |
| `symbol` | text | optional security identifier |
| `acquired` | text | optional ISO date; enables short/long-term treatment later |
| `quantity_micros` | int | optional; shares × 1e6 |
| `series` | text | optional growth-rate series override (see §5) |
| `opening_balance`, `opening_cost_basis`, `prior_year_end_balance` | int | as today, now per lot |

`UNIQUE (account_id, lot)`. Every account has at least one lot. The `holding` and `lot` tables and `RESERVED_DDL` are removed. A "holding" is `GROUP BY account_id, symbol`. The same symbol in two accounts is two independent lots by construction, since a lot belongs to exactly one account.

### 2. `lot_state` is the ledger's state; `account_state` becomes a view

```sql
CREATE TABLE lot_state (
    step_id INTEGER NOT NULL REFERENCES simulation_step(id),
    lot_id  INTEGER NOT NULL REFERENCES account_lot(id),
    balance INTEGER NOT NULL,
    cost_basis INTEGER,
    quantity_micros INTEGER,
    PRIMARY KEY (step_id, lot_id)
);
CREATE VIEW account_state AS
SELECT ls.step_id, m.account_id, SUM(ls.balance) AS balance,
       CASE WHEN COUNT(ls.cost_basis) < COUNT(*) THEN NULL
            ELSE SUM(ls.cost_basis) END AS cost_basis
FROM lot_state ls JOIN account_lot m ON m.id = ls.lot_id
GROUP BY ls.step_id, m.account_id;
```

`account_state` keeps its current columns, so `net_worth`, `balance_by_account`, `balance_by_owner`, `connection.py`, and the notebooks read it unchanged. Basis is NULL if any lot's basis is unknown, rather than under-reporting. The engine reads and writes `lot_state` throughout.

Quantity cannot be derived from balance once growth has occurred, so `lot_state` carries it. A sale of `amount` from a lot removes `quantity × amount / balance` shares (integer rounding, remainder to the last leg).

`transfer_event` keeps `from_account` / `to_account` (indexed, used by reporting) and replaces `from_holding_id` / `to_holding_id` with NOT NULL `from_lot_id` / `to_lot_id`. The zero-sum invariant is checked per step at lot level and at account level.

### 3. References: a `lot` column beside every account column

Leaf chains are per lot ([ADR-003](ADR-003-registries-and-csv-schemas.md) §4.7 already gives every account a leaf chain in the one chain namespace), and `chain` gains `lot_id` (its `account_id` stays, derived). `ChainGraph.leaf_account` becomes `leaf_lot`. The leaf chain's name is internal: the bare account name for the default lot of a non-investment account, `account:lot` otherwise. **No CSV ever spells a lot that way.** Every place a CSV names an account has a `lot` column next to it:

| file | account column | lot column |
| --- | --- | --- |
| `account_lots.csv` | `account_name` | `lot` |
| `chains/<role>/*.csv` | `account_name` | `lot` |
| `scheduled.csv` | `from_account`, `to_account` | `from_lot`, `to_lot` |
| `year_to_date.csv` | `from_account`, `to_account` | `from_lot`, `to_lot` |
| `calendar.csv` | `account_1`, `account_2` | `lot_1`, `lot_2` |

The loader resolves each `(account, lot)` pair to that lot's leaf chain and reports a bad pair with the column names.

- A chain named in an account column takes no lot; the lot column must be blank.
- For **investment subkinds** (`types.INVESTMENT_ASSET_SUBKINDS`: `brokerage_taxable`, `retirement`, `hsa`) a blank lot is a **load error** that lists the account's lots (`'Asset_Brokerage' is an investment account and needs a to_lot (its lots: default)`). There is no defaulting and no implicit chain over an account's lots.
- For all other subkinds a blank lot means `default`.
- `:` is reserved: account names, chain names and lot labels may not contain it.
- `refill_chain`/`overflow_chain` (on the lot, see §4) name a chain or an account, completed by `refill_lot`/`overflow_lot` under the same rule as every other reference.

The rule depends only on subkind, never on the current lot count, so adding a lot never changes what an existing row means. Splitting lot `default` into, say, `2024-03` and `2025-06` leaves rows saying `lot=default` valid and still meaning that one lot only; the author is responsible for revisiting them.

### 4. CSV layout

`account_lots.csv` ([ADR-003](ADR-003-registries-and-csv-schemas.md) §4.3) gains optional columns `lot`, `symbol`, `acquired`, `quantity`, `series`, with several rows per account allowed. In a derived scenario, rows overlay the parent's by **(`account_name`, `lot`)**.

- `opening_balance` may be blank if `quantity`, `symbol`, and a price (§6) are all present; it is then `quantity × price` at `scenario.start`, rounded to cents. If both balance and quantity are given, the balance is authoritative and no price is consulted.
- `prior_year_end_balance` is summed across an account's traditional lots for RMD. Either every one of an account's lots has it or none does, Roth lots aside: they owe no RMD, so they needn't carry it.
- `opening_cost_basis` requirements are unchanged, now per lot.
- Cash-management attributes (`minimum_balance`, `refill_*`, `maximum_balance`, `overflow_*`) are per lot: they moved from `accounts.csv` to `account_lots.csv` (plan Phase 7), and the engine, which already keyed them by lot id, is unchanged. A lot may refill from a sibling lot of its own account.

### 5. Growth is per lot

`_expand_account_growth` already turns an account's `series` into a `percent_of_balance` rule. It now emits one rule per lot, named `growth_` plus the account and lot with each word trimmed to four letters (`growth_bill_roth_roll_sett`), widening the trimming only if that name is already taken (`loader.growth_rule_name`), into that lot's leaf chain. Rate series for a lot resolve in order: the lot's `series`, then a rate series named for the lot's `symbol`, then the blend of its symbol's allocation (below), then the account's `series`.

**A fund grows at the blend of its allocation** ([#78](https://github.com/BobKerns/analysis/issues/78)). A fund holding stocks and bonds depends on more than one rate, but a row in a correlation tree has one parent ([ADR-008](ADR-008-correlated-parameters.md) §2). Splitting the fund by allocation lets each slice follow one rate instead. When a lot's symbol has `allocations.csv` rows ([ADR-003](ADR-003-registries-and-csv-schemas.md) §4.1b), each asset class it holds reads the rate series `<symbol>.<class>` (`rate__BAL.stock_us.csv`), else the class's own series (`rate__stock_us.csv`, `rate__bond.csv`). The lot then grows each month at

```text
annual rate = Σ weight(class) × rate(slice series)
```

with the weights of the allocation set in force that month (its latest `as_of` on or before the month, else its earliest), so a target-date fund's glide path moves its growth. Fixed weights with blended returns is a fund rebalancing to its target. The blend lives in the growth rule's `params_json` (`{"blend": [[as_of, [[series, weight_micros], ...]], ...]}`, the rule's `series` blank), and the engine reads each slice's rate monthly, never a precomputed series, since a `set_rate` event (varied or not) can move any slice mid-run. A slice series is an ordinary rate series, so it varies and correlates through ordinary `set_rate` rows.

The blend applies only when every class the symbol holds resolves to a series. When none does, the lot grows at its account's series as before. When only some do, the load warns and also falls back: a series that happens to share a class's name (a cash account's `cash` rate) mustn't stop a scenario that never meant to blend from loading. The income counterparty follows the account's `tax_character`, except in a `retirement` account, where each lot's subkind picks it: `tax_free` for a Roth lot, `deferred` for a traditional one ([ADR-003](ADR-003-registries-and-csv-schemas.md) §2.2). Accrual is an ordinary transfer from an income account into the lot, so growth moves the balance and not the basis, and the ledger stays event-sourced and zero-sum. The engine never marks positions to market.

### 6. Prices

Prices live in `aux_table` (`kind='security_price'`, `series=<symbol>`, `payload_json={"price_micros": N}`, dated by `effective_date`). They are snapshotted from intent into run files like any aux data, so a run records the prices it used. A fetcher, if we add one, only writes aux rows at load time. Prices are used to derive opening balances (§4); they are not a monthly engine input.

Symbols get names from `securities.csv` (`symbol,name`), at the top of the data repo beside `persons.csv` and inside the provenance scope. It is loaded into a `security` table (`symbol` primary key, `name`) that is snapshotted into run files; the file is optional and a symbol with no row is unnamed. Reports show the name where they show the symbol.

### 7. Sales and withdrawals

A Roth lot's distribution splits by its own rules instead: basis first (pro-rata in a Roth 401k), earnings `roth_nonqualified` until the owner's five-year clock has run ([ADR-003](ADR-003-registries-and-csv-schemas.md) §1a). A death changes the lot's clock or age test from a date on, which the engine works out when the run starts.

A withdrawal from a lot applies the existing pro-rata split (`return_of_capital` + `ltcg`, [ADR-003](ADR-003-registries-and-csv-schemas.md) #8) to that lot's balance and basis, and reduces the lot's quantity proportionally. There is no cross-lot allocation policy in v1; drawing from several lots means a chain listing them explicitly. A `transfer_balance` calendar event (ADR-003 §3.7) names a lot and empties it; emptying an account is one such event per lot.

A sale can instead go into a destination chain with a basis step for the lot (ADR-003 §4.7, [#199](https://github.com/BobKerns/analysis/issues/199)): the withdrawal is not split, and the step recovers the same pro-rata share of the lot's basis, which leaves the lot whether or not the price covered it. Basis it couldn't recover is a `capital_loss` row for an investment (`return_of_capital`) and nothing for a home (`home_sale`). A chain step with `capital_improvement` adds what it places to a lot's basis. At a death, the decedent's lots reset their basis to their balance, all of it or the decedent's half ([ADR-003](ADR-003-registries-and-csv-schemas.md) §4.9, [#255](https://github.com/BobKerns/analysis/issues/255)).

Money moved *into* a lot that tracks cost basis is a purchase: its basis rises by the amount and, if shares are tracked, they rise at the lot's implied price (balance ÷ quantity). This covers purchases from cash, inheritances, and proceeds moved from another taxable lot. Growth generated from `accounts.csv` series (from the built-in counterparty accounts) is appreciation and changes neither. A taxable asset lot with a zero opening balance and no stated basis starts at basis 0, so a lot can be declared up front for a purchase the scenario makes later (with its `acquired` date), and taxable growth is total return: reinvested dividends are not split out, so tax on distributions is deferred to the sale.

The gain leg of a sale from a lot is `ltcg` if the lot has been held more than a year at the sale date (its `acquired` date; no date means long-term) and `stcg` otherwise. `stcg` is taxed as ordinary income. A lot's `acquired` date does not move when money is added to it, so a purchase that should start its own holding period belongs in its own lot. For federal tax the accumulator can tax long-term gains at preferential rates: an optional `ltcg_brackets` aux table (same shape as `federal_brackets`, with a band's floor as the taxable income, gain included, at which its rate starts) stacks the gain on top of ordinary taxable income. Without that table long-term gains are taxed as ordinary income, and state tax never uses it. Short- and long-term results net against each other first; a net capital loss then reduces ordinary income by at most the `capital_loss_limit` aux amount for the year's filing status, and the rest carries forward with its character (ADR-003 §2.6).

### 8. RMD

An `rmd` rule's amount comes from its **subject account**: the total of its traditional lots' balances (a Roth lot owes no RMD and counts toward none, and an `rmd` may not draw from one) on the previous Dec 31 (or the lots' `prior_year_end_balance` values in the first year), over the age factor. The money comes from `from_account`:

- a single lot, when the subject account has one lot (`subject_account` is then optional, and the rule behaves as before); or
- an **explicit chain**, with `{"subject_account": "<account>"}` in `params_json`, drawn in the chain's own order, so which lots fund the RMD is written down rather than inferred.

On an account with several lots a single-lot source is a load error, as is a chain that draws from anything but the subject account's lots. There are no implicit chains. Aggregation across several of an owner's IRAs is deliberately not modelled: a brokerage distributes per account, without regard to distributions taken elsewhere, so each RMD is computed from its own account's lots.

### Amendments applied on acceptance

Applied to [ADR-001](ADR-001-overall-architecture.md), [ADR-002](ADR-002-storage-schema.md) and [ADR-003](ADR-003-registries-and-csv-schemas.md) (Phase 6).

- [ADR-002](ADR-002-storage-schema.md): replace the `holding`/`lot` reserved schema and `schema_overview` rows with the §1–§2 DDL; `account_model_member` and `transfer_event` changes; `account_state` view; `SCHEMA_VERSION` 17 → 18.
- [ADR-003](ADR-003-registries-and-csv-schemas.md): §4.3 new columns and overlay key; §4.7 leaf chains per lot and the lot columns; §2.3 interim `rmd` guard; §4.7 taxable-lot withdrawals made per lot.
- [ADR-001](ADR-001-overall-architecture.md): `AccountState` row in the domain-object table becomes a view over per-lot state.
- [ADR-003](ADR-003-registries-and-csv-schemas.md): the `tax_character` registry gains `stcg`; §4.4 gains the `ltcg_brackets` aux kind; the `tax_accumulator` rule (§2.6) taxes long-term gains at those rates when present.

## Consequences

- Positive: lot-level sales and per-security growth with no price path in the engine; reporting is untouched through the view; one file (`account_lots.csv`) carries opening state; the ledger stays zero-sum and event-sourced.
- Negative: every engine site keyed on account id changes to lot id; existing data needs a `lot` column on `account_lots.csv` and chain files, and `from_lot`/`to_lot`/`lot_1`/`lot_2` columns filled in for investment accounts; row counts in `lot_state` scale with lots × months.
- Runtime lot creation is not supported: money moved into an account goes to a named existing lot.

## Open questions deferred

- Runtime-created lots, and cost-basis lot selection (FIFO / HIFO / specific) as a policy on multi-lot withdrawals.
- `on_death` sweeps and the other account-level rule kinds (`ss_benefit`, `tax_accumulator`, `mortgage_payment`) all need auditing for one-balance-per-account assumptions; the plan's Phase 0 does this.
- Namespace collision between account names and symbols in `aux_table` rate series (lookup order in §5 covers it, but an account and a ticker with the same name would be ambiguous).
- Quicken import is per account; multi-lot accounts need a per-security import.

## Amendments

Each entry changed this ADR's design. The sections it names were rewritten in place to describe the current design; git history has the earlier text.

- [#78](https://github.com/BobKerns/analysis/issues/78): a fund lot can grow at the blend of its allocation slices' rate series (§5).
- [#160](https://github.com/BobKerns/analysis/issues/160): the table is named `account_lot` (was `account_model_member`) and loses `model_id` with the single-row `account_model` table (§1).
- [#197](https://github.com/BobKerns/analysis/issues/197): the `sell_asset` and `buy_asset` calendar kinds are dropped, so nothing here refers to them (Context, Consequences).
- [#199](https://github.com/BobKerns/analysis/issues/199): a sale through a destination chain's basis step recovers the lot's basis, and a `capital_improvement` step adds to it (§7).
- [#217](https://github.com/BobKerns/analysis/issues/217): a `retirement` account's lots each carry a subkind (Roth or traditional, IRA or 401k) that sets the lot's tax treatment, and its RMD counts only traditional lots (§1, §3, §8).
- [#217](https://github.com/BobKerns/analysis/issues/217): a Roth lot tracks basis and splits a distribution by the five-year rule (§7).
- [#224](https://github.com/BobKerns/analysis/issues/224): a `retirement` lot's growth counterparty follows its subkind, not the account's `tax_character` (§5).
- [#256](https://github.com/BobKerns/analysis/issues/256): a Roth lot's five-year rules change at a death (§7).
- [#255](https://github.com/BobKerns/analysis/issues/255): a lot's basis steps up at a death (§7).
