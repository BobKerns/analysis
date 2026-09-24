# ADR-005 implementation plan: lots and per-lot state

Companion to [ADR-005](ADR-005-lots-and-per-lot-state.md). Phases are ordered so each ends with a green test suite and a shippable state. Phase 1 is a pure refactor and must not change any output.

Sizes of the files involved (lines): `engine.py` 2367, `loader.py` 1556, `connection.py` 1001, `cli.py` 2219, `accounts.py` 209, `quicken.py` 123. Test files touching `account_state` / `account_model_member`: `test_engine.py`, `test_sim_loader.py`, `test_sim_cli.py`, `test_runs.py`.

## Phase 0 — Baseline and audit (no behavior change)

1. Pin current behavior for `retire_alice` in a new test: net worth by month (`net_worth` view), per-account year-end balances, the brokerage `return_of_capital`/`ltcg` split, and the existing RMD values (681,542 / 558,190 / 454,889 in `test_engine.py`). These become the Phase 1 equivalence check.
2. Audit every engine site that keys on account id. Use `ripwire --uses` on `balances`, `cost_bases`, `account_kinds`, `account_tax_treatment`, `account_subkind`, `opening_balances`, `prior_year_end_balances`, and `ChainGraph.leaf_account`. Record which rule kinds assume one balance per account: `percent_of_balance`, `rmd`, `ss_benefit`, `tax_accumulator`, `mortgage_payment`, `on_death` (`_apply_on_death`), `_apply_home_sale`, `_chain_sweep`, `_settle_accounts`, refill/overflow. The audit list goes into this file before Phase 1 starts.
3. List every loader path that resolves an account name to an id: `scheduled.csv`, chain steps, `calendar.csv` payloads, `year_to_date.csv` (`account_ids[...]` near `loader.py:1518`), `sell_asset`, growth expansion.

Exit: baseline test passes; audit recorded.

## Phase 1 — Lot-keyed engine, every account has exactly one lot (behavior-preserving)

1. `schema.py`: add `id` and `lot` to `account_model_member` (`UNIQUE (model_id, account_id, lot)`); add `lot_state`; make `account_state` a view; add `from_lot_id`/`to_lot_id` to `transfer_event` (drop the holding columns); add `lot_id` to `chain`; remove `holding`, `lot`, `RESERVED_DDL`; update `SNAPSHOT_TABLES` and `schema_overview`; `SCHEMA_VERSION` 17 → 18. Check FK ordering against `defer_foreign_keys` in `runs.open_run_file`.
2. `loader.py`: `account_values.csv` reads `lot` (optional in this phase); write one `account_model_member` row per account with lot `'1'`; create each account's leaf chain as `Account:1` with `lot_id`. Add the `Account:lot` reference parser in one place and route every resolution site from Phase 0 step 3 through it; bare names resolve to lot `'1'` for now. Reject `:` in account and chain names.
3. `engine.py`: key `balances`, `cost_bases`, and the per-account attribute dicts on lot id; `ChainGraph.leaf_account` → `leaf_lot`; `_post_transfer` writes lot ids; the step snapshot writes `lot_state`; `_apply_rmd` resolves the subject lot's account for the `account_state` query.
4. `connection.py`, `accounts.py`, `cli.py:432` (`account_states` count): confirm they work through the `account_state` view. `accounts.py` `members.set_index("account")` needs a per-account aggregation once there can be several rows; do that here so Phase 2 doesn't break it.
5. Update tests that reference the removed columns/tables.

Exit: Phase 0 baseline test passes with identical numbers; full suite green.

## Phase 2 — Multiple lots

1. `loader.py`: read `lot`, `symbol`, `acquired`, `quantity`, `series`; several rows per account; overlay key `(account_name, lot)` (`loader.py:396`, which currently overlays on `("account_name",)`); validations from ADR-005 §3–§4 (explicit lot for investment subkinds with an actionable message, cash-management attributes only on single-lot accounts, `prior_year_end_balance` all-or-none per account, duplicate `(account, lot)`).
2. `_expand_account_growth` (`loader.py:725`): one rule per lot, `growth_<account>_<lot>`, series precedence per ADR-005 §5.
3. `engine.py`: `_post_withdrawal` uses the lot's balance and basis and reduces `quantity_micros` proportionally; `sell_asset` requires `Account:lot`; add the loader guard rejecting `rmd` on multi-lot accounts; resolve the `on_death` and other audit findings (fail loudly at load if a rule kind cannot handle multi-lot accounts rather than guessing).
4. Tests: a two-lot brokerage where a sale from lot 1 leaves lot 2 untouched (balance, basis, quantity, and the account-level view all check out); zero-sum at lot and account level; NULL-basis propagation through the view; per-lot growth with different series; every load error above.
5. Data: add the `lot` column to `prototype_data` `account_values.csv` and `:1` to references of the four investment accounts (`Asset_Brokerage_Joint`, `Asset_TraditionalIRA_Alice`, `Asset_Roth_Alice`, `Asset_Roth_Bob`) in `scheduled.csv`, chains, `calendar.csv`, `year_to_date.csv`. Add a small multi-lot fixture scenario for the new tests rather than splitting `retire_alice`. Follow the fixture pattern in `tests/conftest.py`.

Exit: Phase 0 baseline still identical; new multi-lot tests pass.

## Phase 3 — Prices and derived opening balances

1. Register `security_price` in `registries.py` aux kinds (`kind`, `series`=symbol, `price_micros`); loader reads `aux/security_price__<SYMBOL>.csv`.
2. Loader derives blank `opening_balance` from `quantity × price at scenario.start`; balance-given-and-quantity-given keeps the balance; blank with no derivable value is an error.
3. Symbol rate series: `aux/rate__<SYMBOL>.csv` resolves through the §5 precedence; add a loader check for an account/ticker name collision.
4. Optional, separate: a price fetcher that writes the aux CSVs.

Exit: tests for derived balance, rounding, missing price, and precedence.

## Phase 4 — Tooling and reporting

1. `cli.py`: `add_account` (writes the `lot` column, prompts for it on investment subkinds, around `cli.py:1160-1269`); the account_values sync command (around `cli.py:1783-1830`) creates one row per lot; the Quicken import (around `cli.py:1850-1987`) updates single-lot accounts and skips multi-lot accounts with a warning until a per-security import exists. `sim explain` gains a per-lot listing.
2. `connection.py`: `account_model_member` item type exposes the new columns; add a `lot_state` item type and a per-lot balance view (`balance_by_lot`).
3. `accounts.py` / notebook UI: account view stays aggregated; add a lot breakdown widget (account → lots, balance, basis, unrealized gain).

Exit: CLI tests for each command; notebook UI tests using the existing fixtures.

## Phase 5 — RMD by explicit chain

1. Add `subject_account` to the `rmd` params; `_apply_rmd` takes the amount from the subject account's aggregate `account_state` balance (prior-year-end summed over lots) and draws through the explicit chain source.
2. Loader: require an explicit chain on multi-lot subject accounts, reject chain leaves outside the subject account, remove the Phase 2 guard.
3. Tests: the existing three RMD values with a single-lot IRA reached via a chain, plus a two-lot IRA drawn in the chain's declared order.

## Phase 6 — Documentation

Apply the ADR-005 "Amendments to apply" list to ADR-001, ADR-002, and ADR-003 (the DDL in ADR-002 must match `schema.py`); mark ADR-005 Accepted; update the `schema_overview` rows and `prototype_data` README text for the new columns.

## Risks

- **Engine surface (highest).** `run_scenario` has cx 54 and much of it is keyed on account ids. Phase 0's audit sizes this; if it is larger than expected, split Phase 1 by first introducing a lot-id alias for account id (one lot per account, identical values) and converting sites one rule kind at a time.
- **Silent behavior change in Phase 1.** Mitigated by the Phase 0 pins. Do not merge Phase 1 with any feature.
- **Rounding in per-lot quantity and pro-rata basis.** Basis-out uses integer division per leg; verify that lot basis never goes negative and that a full-balance withdrawal zeroes both basis and quantity exactly.
- **FK ordering at run start.** `chain.lot_id` and `account_model_member` reference each other through `account`/`chain` snapshots; confirm the snapshot order and deferred-FK setting still satisfy SQLite.
- **Existing user data repos.** The one-time edit (add `lot` column, `:1` on investment references) fails loudly with a message that names the exact fix; nothing runs on a half-migrated repo.
