# ADR-005: Lots and per-lot state

- **Status:** Proposed
- **Date:** 2026-09-23
- **Supersedes:** the reserved `holding` / `lot` tables and `transfer_event.from_holding_id` / `to_holding_id` (ADR-002 §Reserved schema); the "account-level running cost basis" limitation noted in ADR-003 §Later amendments #8.
- **Related:** ADR-001 (domain objects, `AccountState`), ADR-002 (storage schema), ADR-003 (registries, CSV layouts, chains §4.7, `rmd` §2.3, `sell_asset` §3.7).

## Context

Every account is one balance with one running cost basis. A brokerage or IRA that holds several securities bought at different times cannot be modelled below the account: a withdrawal blends the whole account's basis pro-rata (`engine._post_withdrawal`), a `sell_asset` liquidates the entire account, and growth is one rate per account. We want to:

1. Track individual positions (lots) inside investment accounts, including IRAs, and say which lot a sale comes from.
2. Model growth per security rather than per account.
3. Derive opening balances from quantities and prices when we have them.
4. Change as little of the engine, reporting, and notebook code as possible.

ADR-002 reserved `holding` / `lot` tables and `transfer_event.from_holding_id` / `to_holding_id` for this, but nothing reads or writes them. They are replaced here, because a separate holding table adds a second file to author and a second identity to keep in step, for no information a `GROUP BY account, symbol` over lots doesn't give.

## Decision

### 1. A lot is a row of `account_model_member`

`account_model_member` gains a surrogate `id` and becomes one row per **(model, account, lot)**.

| column | type | notes |
| --- | --- | --- |
| `id` | int PK | the **lot id**; the engine keys balances, basis and quantity on it |
| `model_id`, `account_id` | int | as today |
| `lot` | text NOT NULL | free-form label, unique within the account; `'default'` for accounts that are not split. Sequential numbers (`1`, `2`) are a fine convention, and so are acquisition dates or any other name without `:` |
| `symbol` | text | optional security identifier |
| `acquired` | text | optional ISO date; enables short/long-term treatment later |
| `quantity_micros` | int | optional; shares × 1e6 |
| `series` | text | optional growth-rate series override (see §5) |
| `opening_balance`, `opening_cost_basis`, `prior_year_end_balance` | int | as today, now per lot |

`UNIQUE (model_id, account_id, lot)`. Every account has at least one lot. The `holding` and `lot` tables and `RESERVED_DDL` are removed. A "holding" is `GROUP BY account_id, symbol`. The same symbol in two accounts is two independent lots by construction, since a lot belongs to exactly one account.

### 2. `lot_state` is the ledger's state; `account_state` becomes a view

```sql
CREATE TABLE lot_state (
    step_id INTEGER NOT NULL REFERENCES simulation_step(id),
    lot_id  INTEGER NOT NULL REFERENCES account_model_member(id),
    balance INTEGER NOT NULL,
    cost_basis INTEGER,
    quantity_micros INTEGER,
    PRIMARY KEY (step_id, lot_id)
);
CREATE VIEW account_state AS
SELECT ls.step_id, m.account_id, SUM(ls.balance) AS balance,
       CASE WHEN COUNT(ls.cost_basis) < COUNT(*) THEN NULL
            ELSE SUM(ls.cost_basis) END AS cost_basis
FROM lot_state ls JOIN account_model_member m ON m.id = ls.lot_id
GROUP BY ls.step_id, m.account_id;
```

`account_state` keeps its current columns, so `net_worth`, `balance_by_account`, `balance_by_owner`, `connection.py`, and the notebooks read it unchanged. Basis is NULL if any lot's basis is unknown, rather than under-reporting. The engine reads and writes `lot_state` throughout.

Quantity cannot be derived from balance once growth has occurred, so `lot_state` carries it. A sale of `amount` from a lot removes `quantity × amount / balance` shares (integer rounding, remainder to the last leg).

`transfer_event` keeps `from_account` / `to_account` (indexed, used by reporting) and replaces `from_holding_id` / `to_holding_id` with NOT NULL `from_lot_id` / `to_lot_id`. The zero-sum invariant is checked per step at lot level and at account level.

### 3. References: a `lot` column beside every account column

Leaf chains are per lot (ADR-003 §4.7 already gives every account a leaf chain in the one chain namespace), and `chain` gains `lot_id` (its `account_id` stays, derived). `ChainGraph.leaf_account` becomes `leaf_lot`. The leaf chain's name is internal: the bare account name for the default lot of a non-investment account, `account:lot` otherwise. **No CSV ever spells a lot that way.** Every place a CSV names an account has a `lot` column next to it:

| file | account column | lot column |
| --- | --- | --- |
| `account_values.csv` | `account_name` | `lot` |
| `chains/*.csv` | `account_name` | `lot` |
| `scheduled.csv` | `from_account`, `to_account` | `from_lot`, `to_lot` |
| `year_to_date.csv` | `from_account`, `to_account` | `from_lot`, `to_lot` |
| `calendar.csv` | `account_1`, `account_2` | `lot_1`, `lot_2` |

The loader resolves each `(account, lot)` pair to that lot's leaf chain and reports a bad pair with the column names.

- A chain named in an account column takes no lot; the lot column must be blank.
- For **investment subkinds** (`types.INVESTMENT_ASSET_SUBKINDS`: `brokerage_taxable`, `traditional_ira`, `roth_ira`, `traditional_401k`, `roth_401k`, `hsa`) a blank lot is a **load error** that lists the account's lots (`'Asset_Brokerage' is an investment account and needs a to_lot (its lots: default)`). There is no defaulting and no implicit chain over an account's lots.
- For all other subkinds a blank lot means `default`.
- `:` is reserved: account names, chain names and lot labels may not contain it.
- `accounts.csv` `refill_chain`/`overflow_chain` name a chain or a non-investment account; an investment account's lots are reached through a chain file.

The rule depends only on subkind, never on the current lot count, so adding a lot never changes what an existing row means. Splitting lot `default` into, say, `2024-03` and `2025-06` leaves rows saying `lot=default` valid and still meaning that one lot only; the author is responsible for revisiting them.

### 4. CSV layout

`account_values.csv` (ADR-003 §4.3) gains optional columns `lot`, `symbol`, `acquired`, `quantity`, `series`, with several rows per account allowed. In a derived scenario, rows overlay the parent's by **(`account_name`, `lot`)**.

- `opening_balance` may be blank if `quantity`, `symbol`, and a price (§6) are all present; it is then `quantity × price` at `scenario.start`, rounded to cents. If both balance and quantity are given, the balance is authoritative and no price is consulted.
- `prior_year_end_balance` is summed across an account's lots for RMD. Either every lot of an account has it or none does.
- `opening_cost_basis` requirements are unchanged, now per lot.
- Cash-management attributes on `accounts.csv` (`minimum_balance`, `refill_*`, `maximum_balance`, `overflow`) are permitted only on accounts with a single lot. Load error otherwise.

### 5. Growth is per lot

`_expand_account_growth` already turns an account's `series` into a `percent_of_balance` rule. It now emits one rule per lot, named `growth_<account>_<lot>`, into that lot's leaf chain. Rate series for a lot resolve in order: the lot's `series`, then a rate series named for the lot's `symbol`, then the account's `series`. The income counterparty and `tax_character` remain account-level. Accrual is an ordinary transfer from an income account into the lot, so growth moves the balance and not the basis, and the ledger stays event-sourced and zero-sum. The engine never marks positions to market.

### 6. Prices

Prices live in `aux_table` (`kind='security_price'`, `series=<symbol>`, `payload_json={"price_micros": N}`, dated by `effective_date`). They are snapshotted from intent into run files like any aux data, so a run records the prices it used. A fetcher, if we add one, only writes aux rows at load time. Prices are used to derive opening balances (§4); they are not a monthly engine input.

Symbols get names from `securities.csv` (`symbol,name`), at the top of the data repo beside `persons.csv` and inside the provenance scope. It is loaded into a `security` table (`symbol` primary key, `name`) that is snapshotted into run files; the file is optional and a symbol with no row is unnamed. Reports show the name where they show the symbol.

### 7. Sales and withdrawals

A withdrawal from a lot applies the existing pro-rata split (`return_of_capital` + `ltcg`, ADR-003 #8) to that lot's balance and basis, and reduces the lot's quantity proportionally. There is no cross-lot allocation policy in v1; drawing from several lots means a chain listing them explicitly. `sell_asset` on an investment account names a lot and liquidates that lot. Liquidating an account is one `sell_asset` per lot.

Money moved *into* a lot that tracks cost basis is a purchase: its basis rises by the amount and, if shares are tracked, they rise at the lot's implied price (balance ÷ quantity). This covers purchases from cash, inheritances, and proceeds moved from another taxable lot. Growth generated from `accounts.csv` series (from the built-in counterparty accounts) is appreciation and changes neither. A taxable asset lot with a zero opening balance and no stated basis starts at basis 0, so a lot can be declared up front for a purchase the scenario makes later (with its `acquired` date), and taxable growth is total return: reinvested dividends are not split out, so tax on distributions is deferred to the sale.

### 8. RMD (deferred)

Until the follow-up, the loader rejects an `rmd` rule whose `from_account` has more than one lot, so it cannot silently draw from one lot only. The follow-up requires an **explicit chain** as the draw source plus a `subject_account` param. The amount comes from that account's aggregate `account_state` balance, and the loader rejects any chain leaf that is not a lot of the subject account. Aggregation across several of an owner's IRAs is out of scope.

### Amendments to apply on acceptance

These change what ADR-002 and ADR-003 describe, so they are applied to those documents when the code lands, not before.

- ADR-002: replace the `holding`/`lot` reserved schema and `schema_overview` rows with the §1–§2 DDL; `account_model_member` and `transfer_event` changes; `account_state` view; `SCHEMA_VERSION` 17 → 18.
- ADR-003: §4.3 new columns and overlay key; §4.7 leaf chains per lot and the lot columns; §3.7 `sell_asset` requires a lot; §2.3 interim `rmd` guard; §Later amendments #8 revised.
- ADR-001: `AccountState` row in the domain-object table becomes a view over per-lot state.

## Consequences

- Positive: lot-level sales and per-security growth with no price path in the engine; reporting is untouched through the view; one file (`account_values.csv`) carries opening state; the ledger stays zero-sum and event-sourced.
- Negative: every engine site keyed on account id changes to lot id; existing data needs a `lot` column on `account_values.csv` and chain files, and `from_lot`/`to_lot`/`lot_1`/`lot_2` columns filled in for investment accounts; row counts in `lot_state` scale with lots × months.
- Runtime lot creation (a `buy_asset` into a brokerage opening a new lot) is not supported; buys add to a named existing lot.

## Open questions deferred

- Runtime-created lots, and cost-basis lot selection (FIFO / HIFO / specific) as a policy on multi-lot withdrawals.
- Cash-management attributes (`minimum_balance`, refill, overflow) on multi-lot accounts. Motivating case: an HSA whose money-market position is refilled from its mutual funds, currently modelled as two accounts. Revisit once lots exist; the open design question is whether these attach to a lot or to the account.
- Short-term vs long-term gain treatment using `acquired`. It only matters once the tax accumulator has preferential long-term rates: today it applies one bracket schedule to ordinary income plus long-term gains, so `ltcg` is already taxed at ordinary rates and a `stcg` character would change nothing in the computed tax. Do the two together.
- `on_death` sweeps and the other account-level rule kinds (`ss_benefit`, `tax_accumulator`, `mortgage_payment`) all need auditing for one-balance-per-account assumptions; the plan's Phase 0 does this.
- Namespace collision between account names and symbols in `aux_table` rate series (lookup order in §5 covers it, but an account and a ticker with the same name would be ambiguous).
- Quicken import is per account; multi-lot accounts need a per-security import.
