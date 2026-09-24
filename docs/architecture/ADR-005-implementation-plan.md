# ADR-005 implementation plan: lots and per-lot state

Companion to [ADR-005](ADR-005-lots-and-per-lot-state.md). Phases are ordered so each ends with a green test suite and a shippable state. Phase 1 is a pure refactor and must not change any output.

Sizes of the files involved (lines): `engine.py` 2367, `loader.py` 1556, `connection.py` 1001, `cli.py` 2219, `accounts.py` 209, `quicken.py` 123. Test files touching `account_state` / `account_model_member`: `test_engine.py`, `test_sim_loader.py`, `test_sim_cli.py`, `test_runs.py`.

## Status

- **Phase 0: done.** `tests/test_retire_alice_baseline.py` pins net worth, per-account state and the full transfer ledger against `tests/data/sim/retire_alice_baseline/` (regenerate with `UPDATE_BASELINE=1` only for an intentional behavior change). Basis splitting is not exercised by `retire_alice`; it is covered by the `_post_withdrawal` unit tests.
- **Phase 1: done (f81296d).** The baseline is byte-identical and the suite is green. Where the implementation differs from the text below:
  - The engine keeps its dict and parameter names (`balances`, `account_kinds`, `from_id`, ...) but their keys are now lot ids. Only `ChainGraph.leaf_account` was renamed (`leaf_lot`), because tests construct it directly.
  - `transfer_event.from_account`/`to_account` are filled by a sub-select on `account_model_member` inside `_post_transfer`/`_post_ytd_transfer`, so no call site changed signature. The engine unit tests now use a `ledger_con` fixture (run schema plus identity lots 1..99) instead of hand-written DDL.
  - Leaf chain names come from `registries.leaf_chain_name` (bare account name for the default lot, `account:lot` otherwise). Investment-subkind enforcement of explicit lots is Phase 2.
  - `year_to_date` gained `from_lot_id`/`to_lot_id`; the tax accumulator's paid-tax query now filters on `to_lot_id`.
  - Every account now gets a default lot row, including income and expense accounts that had no `account_values.csv` row. `accounts.py` sums per account (`_members_by_account`).
  - Duplicate `(account_name, lot)` rows are flagged at read time. The old duplicate-`account_name` check in `_validate_account_model_money` could never fire, because the overlay merge collapses same-key rows first.
- **Phase 2: done.** Suite green; multi-lot behavior is covered by `tests/test_lots.py`. Differences from the text below:
  - The engine needed no per-rule changes: nodes are already lots, so multi-lot support is in the loader (`_LeafIndex`, `_ref_problem`, per-lot `_expand_account_growth`, the account_values validations) plus per-lot quantity, tracked in `_post_transfer` through `RefillContext.quantities`.
  - `registries.leaf_chain_name(account, lot, subkind)` names the internal leaf chain (`account:lot` for investment accounts). No CSV uses that spelling: every account column has a lot column beside it (see ADR-005 §3), and the loader resolves the pair in `_resolve_ref`. The engine takes lot names for messages from the `chain` table.
  - Only `calendar.csv` `account_1`/`account_2` of `one_off_transfer` events are validated for lot-qualified names. `sell_asset`/`buy_asset` are not implemented in the engine (they are skipped), so their payload references are unchecked until they are.
  - Deposits into a lot originally changed neither its cost basis nor its quantity. That is fixed (see ADR-005 §7): a purchase, inheritance or proceeds moved into a basis-tracked lot raises its basis and shares; growth does not. The pinned baseline changed for exactly that reason: `Asset_Brokerage_Joint`'s `cost_basis` is $50,000 higher from the 2029 inheritance on, and nothing else moved.
  - CLI tooling was adapted here rather than in Phase 4 because renames, deletes and completions would otherwise break: `--from-lot`/`--to-lot` options (prompted for investment accounts) on `transfer add`/`scheduled add`, a `--from-lot` on `import-baseline`, lot shown in `transfer list`, `account add`/`sync` write the lot column, `_append_csv_row` tolerates extra optional columns, `import-quicken` skips multi-lot accounts with a warning.
  - `prototype_data` and `templates` were migrated to the lot columns. Fixed on the way: `sim scheduled list` and `accounts.py` looked chains up by account id, which only worked while the two id sequences coincided.
- **Phase 3: done.** `security_price` aux kind (CSV column `price` in decimal dollars, up to 6 places, stored as `price_micros`); `_derive_opening_balances` fills a blank `opening_balance` from `quantity` and the latest price on or before the scenario start (half-up to the cent), leaves a given balance alone, and reports a missing price, a missing symbol, or a symbol equal to an account name. Symbol-named `rate` series were already in Phase 2. The price fetcher (step 4) was not built.
- **Holding period and long-term rates: done.** `stcg` tax character (schema 21; taxed as ordinary), gain legs from a lot are `stcg` until a year after `acquired` and `ltcg` after, and an optional `ltcg_brackets` aux table makes the federal accumulator tax long-term gains at preferential rates (state unchanged). Together they make an explicit chain that lists long-term lots before short-term ones worth having; automatic lot selection is still not built.
- **Audit result:** `on_death` sweeps and the other rule kinds address one lot through their from/to chain, so a multi-lot account needs one rule (or an explicit chain) per lot; nothing silently uses lot `default`. `rmd` is guarded at load. `_settle_accounts`/refill only apply to single-lot accounts.

## Phase 0 — Baseline and audit (no behavior change)

1. Pin current behavior for `retire_alice` in a new test: net worth by month (`net_worth` view), per-account year-end balances, the brokerage `return_of_capital`/`ltcg` split, and the existing RMD values (681,542 / 558,190 / 454,889 in `test_engine.py`). These become the Phase 1 equivalence check.
2. Audit every engine site that keys on account id. Use `ripwire --uses` on `balances`, `cost_bases`, `account_kinds`, `account_tax_treatment`, `account_subkind`, `opening_balances`, `prior_year_end_balances`, and `ChainGraph.leaf_account`. Record which rule kinds assume one balance per account: `percent_of_balance`, `rmd`, `ss_benefit`, `tax_accumulator`, `mortgage_payment`, `on_death` (`_apply_on_death`), `_apply_home_sale`, `_chain_sweep`, `_settle_accounts`, refill/overflow. The audit list goes into this file before Phase 1 starts.
3. List every loader path that resolves an account name to an id: `scheduled.csv`, chain steps, `calendar.csv` payloads, `year_to_date.csv` (`account_ids[...]` near `loader.py:1518`), `sell_asset`, growth expansion.

Exit: baseline test passes; audit recorded.

## Phase 1 — Lot-keyed engine, every account has exactly one lot (behavior-preserving)

1. `schema.py`: add `id` and `lot` to `account_model_member` (`UNIQUE (model_id, account_id, lot)`); add `lot_state`; make `account_state` a view; add `from_lot_id`/`to_lot_id` to `transfer_event` (drop the holding columns); add `lot_id` to `chain`; remove `holding`, `lot`, `RESERVED_DDL`; update `SNAPSHOT_TABLES` and `schema_overview`; `SCHEMA_VERSION` 17 → 18. Check FK ordering against `defer_foreign_keys` in `runs.open_run_file`.
2. `loader.py`: `account_values.csv` reads `lot` (optional in this phase); write one `account_model_member` row per account with lot `'default'`; create each account's leaf chain with `lot_id`. Resolve every reference through one place (`_resolve_ref`); bare names resolve to lot `'default'` for now. Reject `:` in account and chain names.
3. `engine.py`: key `balances`, `cost_bases`, and the per-account attribute dicts on lot id; `ChainGraph.leaf_account` → `leaf_lot`; `_post_transfer` writes lot ids; the step snapshot writes `lot_state`; `_apply_rmd` resolves the subject lot's account for the `account_state` query.
4. `connection.py`, `accounts.py`, `cli.py:432` (`account_states` count): confirm they work through the `account_state` view. `accounts.py` `members.set_index("account")` needs a per-account aggregation once there can be several rows; do that here so Phase 2 doesn't break it.
5. Update tests that reference the removed columns/tables.

Exit: Phase 0 baseline test passes with identical numbers; full suite green.

## Phase 2 — Multiple lots

1. `loader.py`: read `lot`, `symbol`, `acquired`, `quantity`, `series`; several rows per account; overlay key `(account_name, lot)` (`loader.py:396`, which currently overlays on `("account_name",)`); validations from ADR-005 §3–§4 (explicit lot for investment subkinds with an actionable message, cash-management attributes only on single-lot accounts, `prior_year_end_balance` all-or-none per account, duplicate `(account, lot)`).
2. `_expand_account_growth` (`loader.py:725`): one rule per lot, `growth_<account>_<lot>`, series precedence per ADR-005 §5.
3. `engine.py`: `_post_withdrawal` uses the lot's balance and basis and reduces `quantity_micros` proportionally; `sell_asset` names a lot; add the loader guard rejecting `rmd` on multi-lot accounts; resolve the `on_death` and other audit findings (fail loudly at load if a rule kind cannot handle multi-lot accounts rather than guessing).
4. Tests: a two-lot brokerage where a sale from one lot leaves the other untouched (balance, basis, quantity, and the account-level view all check out); zero-sum at lot and account level; NULL-basis propagation through the view; per-lot growth with different series; every load error above.
5. Data: add the `lot` column to `prototype_data` `account_values.csv` and chain files, and the `from_lot`/`to_lot`/`lot_1`/`lot_2` columns to `scheduled.csv`, `year_to_date.csv` and `calendar.csv`, filled in for the four investment accounts (`Asset_Brokerage_Joint`, `Asset_TraditionalIRA_Alice`, `Asset_Roth_Alice`, `Asset_Roth_Bob`). Add a small multi-lot fixture scenario for the new tests rather than splitting `retire_alice`. Follow the fixture pattern in `tests/conftest.py`.

Exit: Phase 0 baseline still identical; new multi-lot tests pass.

## Phase 3 — Prices and derived opening balances

1. Register `security_price` in `registries.py` aux kinds (`kind`, `series`=symbol, `price_micros`); loader reads `aux/security_price__<SYMBOL>.csv`.
2. Loader derives blank `opening_balance` from `quantity × price at scenario.start`; balance-given-and-quantity-given keeps the balance; blank with no derivable value is an error.
3. Symbol rate series: `aux/rate__<SYMBOL>.csv` resolves through the §5 precedence; add a loader check for an account/ticker name collision.
4. Optional, separate: a price fetcher that writes the aux CSVs.

Exit: tests for derived balance, rounding, missing price, and precedence.

## Phase 4 — Tooling and reporting

1. **CLI tooling for lots: done** (built with Phase 2 because renames, deletes and completions would otherwise break): `--from-lot`/`--to-lot` on `transfer add`/`scheduled add`, `--from-lot` on `import-baseline`, lot shown in `transfer list`, `account add`/`sync` write the lot columns, `import-quicken` skips multi-lot accounts.
2. **Data layer and per-lot listings: done.** `balance_by_lot` view (schema 19), a `lot_balance` item type (month, account, lot, symbol, balance, basis, quantity), the `account_model_member` item now carries `symbol`, `acquired`, `quantity_micros` and `series`; `sim explain` lists each lot of investment and multi-lot accounts at the final step; `sim items --account A --lot L` limits ledger events to one lot and shows the lot on each row.
3. **Holdings views: done.** `analysis.sim.holdings` (`lots_frame`, `account_holdings`, `symbol_holders`, totals, rich tables) works on a run (month-end balances, any month) or a scenario (opening values). Commands: `sim account holdings <account>` and `sim account holders <symbol>`, with `--month`, `--runfile`, `--opening`. Widgets in `notebook_ui/holdings_widgets.py`: `account_holdings_widget` and `symbol_holders_widget`, with the tables prepared by `account_holdings_view`/`symbol_holders_view`. Symbol names come from an optional `securities.csv` (`symbol,name`) loaded into a `security` table (schema 20, provenance-scoped); they appear in the holdings tables, the holders title and heading, and the symbol dropdown. The scenario notebooks do not use the widgets yet.
4. **Scenario comparison of holdings: design needed.** Decide how `notebooks/scenario_compare.ipynb` shows lot-level differences between scenarios before building it. Open questions: compare by `(account, lot)` or by symbol across accounts; how to show a lot that exists in only one scenario; which columns (quantity, balance, basis, unrealized gain) and whether as absolute values or differences; how it fits with the existing account comparison functions and the account picker (`compare_plots.py`, `accounts_plots.py`, `AccountPicker`).

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
- **Existing user data repos.** The one-time edit (add the lot columns, fill them in for investment accounts) fails loudly with a message that names the exact fix; nothing runs on a half-migrated repo.
