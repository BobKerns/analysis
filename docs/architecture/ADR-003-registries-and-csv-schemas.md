# ADR-003: Rule registry, event-kind registry, subkind vocabulary, and CSV schemas

- **Status:** Accepted
- **Date:** 2026-05-17
- **Supersedes:** —
- **Related:** ADR-001 (overall architecture), ADR-002 (storage schema), ADR-004 (reproducibility).

## Context

ADR-002 left four pieces of vocabulary deliberately open: the list of `account.subkind` values, the set of `amount_rule_kind` values for `scheduled_transaction`, the set of `kind` values for `calendar_event`, and the CSV column schemas the loader expects under the per-scenario folder. This ADR closes them.

Each registry is **closed in v1**: the loader rejects unknown values. New kinds enter only through this ADR (or a successor). This keeps the simulator legible — a reader can enumerate every way a rule can compute an amount and every way a calendar can patch reality.

## Decision

### 1. `account.subkind` vocabulary

CHECK-constrained at load time (the loader validates; the DDL keeps `subkind` as free TEXT so future ADRs can extend without a destructive migration).

**Asset:**

| subkind | meaning |
| --- | --- |
| `checking` | demand deposit, primary cash |
| `savings` | demand deposit, separate from checking |
| `brokerage_taxable` | taxable investment account |
| `traditional_ira` | pre-tax IRA |
| `roth_ira` | post-tax IRA |
| `traditional_401k` | pre-tax employer plan (also 403b, 457b) |
| `roth_401k` | post-tax employer plan |
| `hsa` | health savings account |
| `real_estate` | primary residence or other real property |
| `vehicle` | car, boat, etc. (depreciating asset) |
| `other_asset` | catch-all; populates `meta_json` for explanation |

**Liability:**

| subkind | meaning |
| --- | --- |
| `mortgage` | secured by `real_estate` account |
| `heloc` | home equity line of credit |
| `credit_card` | revolving consumer debt |
| `other_liability` | catch-all |

**Income** (flow accounts; balance accumulates over the run):

| subkind | meaning |
| --- | --- |
| `salary` | wage or salary income, ordinary |
| `ss_benefit` | Social Security retirement benefit |
| `pension` | defined-benefit pension |
| `interest` | interest income |
| `dividend` | dividend income |
| `capital_gain` | realized capital gains (long or short; bucket on `transfer_event.tax_character`) |
| `rental` | rental income |
| `other_income` | catch-all |

**Expense** (flow accounts):

| subkind | meaning |
| --- | --- |
| `living` | groceries, entertainment, day-to-day |
| `healthcare` | premiums, out-of-pocket medical |
| `housing` | utilities, insurance, maintenance (a broad category; the v2 ADR will likely split this) |
| `interest_expense` | mortgage interest, HELOC interest, any deductible/non-deductible interest leg |
| `taxes_federal` | federal income tax paid |
| `taxes_state` | state income tax paid |
| `taxes_property` | real estate tax |
| `other_expense` | catch-all |

The expense vocabulary intentionally mixes categories (`housing`) with mechanisms (`interest_expense`); a future ADR is expected to refactor `housing` into finer-grained subkinds without disturbing the others.

The `_other` catch-alls are deliberate escape hatches; the `--check` mode (ADR-001 §CLI) flags any account using one so the author can decide whether to extend the vocabulary.

### 2. `amount_rule_kind` registry

Eight rule kinds in v1. Each rule's `compute(state, aux, date) → list[TransferEventSpec]` is a pure function. The columns `from_account`, `to_account`, `base_amount`, `base_date`, `inflation_series` on `scheduled_transaction` carry the common parameters; rule-specific parameters live in the named columns `interest_account`, `person`, `series`, `rate_micros`, `claim_age`, `amount` (see §4.6). Rare or future extension parameters may go in the nullable `params_json` column.

**Invariant (rule-as-sole-origin):** every `transfer_event` traces to **exactly one rule** via `(reason_kind='rule', reason_id = scheduled_transaction.id)`. A once-only movement is a rule whose `cadence='one_off'`. Calendar events never write to `transfer_event` directly; money-moving calendar kinds *synthesize* one or more `cadence='one_off'` scheduled_transactions at expansion time (see §3). The only exception is the synthetic per-account `opening_balance` rule materialized at run start from the account model, which uses `reason_kind='initial'` and is its own degenerate rule. (ADR-002 follow-up: tighten `transfer_event.reason_kind` CHECK to `('rule','initial')` and add `scheduled_transaction.created_by_event_id INTEGER REFERENCES calendar_event(id)` so synthesized rules trace back to their authoring calendar event.)

**Multi-leg rules** (in v1: only `mortgage_payment`) emit more than one transfer event per fire; all legs share the same `(reason_kind, reason_id)` and are linked by `parent_event_id` for grouping. The `from_account`/`to_account` columns on the scheduled_transaction row are interpreted as the **primary leg** (cash → liability principal); the secondary leg (cash → interest expense) targets the account named by `interest_account`. This invariant — "each event traces to one rule, but a rule may emit multiple events per fire" — is the *direction* that matters: it makes audit, `explain`, and `compare` straightforward.

#### 2.1 `fixed_amount` (priority bands: 200, 300, 500)

Constant amount, optionally inflated.

- **Params:** none in JSON. Uses `base_amount` (required, cents), `base_date` (optional), `inflation_series` (optional aux series).
- **Compute:** `amount = base_amount` if `inflation_series` is NULL else `base_amount × cumulative_inflation(base_date → date, series)`.
- **Emits:** one event, `from_account → to_account`, amount as above, tax_character from the row.
- **Use:** salary, SS post-FRA (without COLA series), pensions, recurring fixed expenses, lump-sum one-offs.

#### 2.2 `percent_of_balance` (priority band: 100)

Accrue interest, dividends, appreciation against a source account's balance.

- **Params:** the row's `to_account` is the balance-bearing account; `series` names the aux rate-series. `rate_basis` defaults to `"balance"`.
- **Compute:** `amount = round(to_account_balance × monthly_rate)`. Annual rates are converted by `(1 + r)^(1/12) - 1`, computed in fixed-point micros.
- **Emits:** one event. Typical: `from_account = <income account>` (e.g. `Income_Interest`), `to_account = <balance-bearing account>` (interest credits to the asset). For depreciation, swap signs by routing through an expense account.
- **Use:** money-market interest, bond coupon approximations, portfolio appreciation per asset, real-estate appreciation.

#### 2.3 `rmd` (priority band: 200 or 500)

IRS Required Minimum Distribution.

- **Params:** the row's `from_account` is the subject IRA/401k account (the one being drawn down); `person` = person id. Factor table is `rmd_factors`; start-age series is `rmd_start_age` (both fixed aux references).
- **Compute:** Fires once per year (cadence = `annual`, `month_of_year` typically 12). `amount = round(dec31_prev_year_balance / uniform_lifetime_factor(person_age_year_end))`. `dec31_prev_year_balance` comes from end-of-prior-year `account_state`; for the partial first year it comes from `account_model_member.prior_year_end_balance` (which the author must supply for any `traditional_ira` / `traditional_401k` account whose owner reaches RMD age during the scenario's first calendar year).
- **Start semantics:** the row's `start` column is a lower bound on candidate fire dates. The actual first fire is `max(row.start, year-end of the year person reaches start_age)`. Authors typically set `start = scenario.start` and let the age gate dominate.
- **Emits:** one event, `from_account = subject_account`, `to_account = <taxable landing>` (typically `Asset_Checking`); `tax_character = 'tax_deferred_out'` (the retrospective tax scan reclassifies as ordinary income).
- **Use:** post-age-73 required distributions for either spouse.

#### 2.4 `waterfall_fill` (priority band: 900 — must run last)

Cover any remaining cash shortfall in a target account by drawing from an ordered list of sources.

- **Params:** the row's `to_account` is the target account being filled; `amount` = minimum acceptable end-of-month balance in dollars (usually 0 or a small cushion).
- **Sources:** rows in `scheduled_transaction_source` ordered by `position`. The row's `from_account` column MUST equal the `position = 1` source — a redundancy the loader enforces so a human (or AI) reading `scheduled.csv` alone sees the primary draw without cross-referencing `waterfall_sources.csv`.
- **Compute:** at fire time, `shortfall = max(0, target_balance_cents - target_balance_after_all_other_rules)`. If `shortfall > 0`, draw `min(shortfall, source_balance)` from sources in order. If still short after all sources, **halt the run** (per ADR-001 principle 7); record `failure_reason = "WaterfallExhausted: <month> short $<amount>"` and persist partial results.
- **Emits:** zero or more events (one per source actually drawn from). Each event uses `tax_character` from the source's account semantics (taxable brokerage → `ltcg`, IRA → `tax_deferred_out`, etc.) — the `tax_character` column on the scheduled_transaction row is overridden per leg from `account.tax_treatment`.
- **Use:** the single "fill checking from savings, then brokerage, then traditional IRA" rule that every scenario carries.

#### 2.5 `ss_benefit` (priority band: 200)

Social Security retirement benefit with COLA.

- **Params:** `person` = person id, `claim_age` = integer claim age, `amount` = PIA at FRA in dollars, `series` = COLA aux series name (e.g. `cpi`). `tax_character` on the row should be `ss_benefit`.
- **Compute:** Fires monthly. Adjusts `base_pia_at_fra_cents` by the claim-age multiplier (a lookup based on months early/late vs. FRA, hard-coded) and applies cumulative COLA from claim date to `date`. Zero before the person reaches `claim_age`.
- **Emits:** one event, `from_account = Income_SS_<person>`, `to_account = Asset_Checking`, `tax_character = 'ss_benefit'`.
- **Start semantics:** as with `rmd`, the row's `start` column is a lower bound. The rule emits zero amount in months before the person reaches `claim_age`; that age gate ANDs with the row's `start`.
- **Use:** each spouse's SS rule. Different scenarios swap `claim_age` to test claim-timing decisions.

#### 2.6 `tax_accumulator` (priority band: 400)

A placeholder rule that marks "compute taxes for the prior tax year" at a specific calendar point. Fires at year-end (December) for accrual and in April for reconciliation.

- **Params:** `{ "year": "previous", "scope": "federal" | "state", "filing_status_aux": "filing_status", "bracket_aux": "federal_brackets" }`.
- **Compute:** delegates to `tax.py`. Scans `transfer_event` rows for the relevant year by `tax_character`, applies brackets and deductions from aux tables, and computes net tax owed.
- **Emits:** one event in April: `from_account = Asset_Checking`, `to_account = Expense_Taxes_Federal` (or `_State`), amount = computed tax minus withholding already paid via `fixed_amount` rules through the year. The December firing typically emits zero (it only writes `tax_year_summary` rows for visibility) but may emit a true-up if estimated payments are part of the scenario.
- **Use:** one per (year, scope) pair, authored as `cadence = annual`.

#### 2.7 `mortgage_payment` (priority band: 300; multi-leg)

Monthly P&I against a mortgage liability account.

- **Params:** the row's `to_account` is the mortgage liability; `interest_account` names the interest-expense account; `rate_micros` = annual rate as millionths of 1 (e.g. `60000` = 6.0%). Monthly P&I is the row's `base_amount`.
- **Compute:** at fire time, `interest = round(liability_balance × annual_rate_micros / 12 / 1_000_000)`; `principal = payment_amount - interest`.
- **Emits two events:**
  1. **Interest leg:** `from_account = Asset_Checking` (the row's `from_account`), `to_account = interest_account`, amount = `interest`, tax_character = `'none'`.
  2. **Principal leg:** `from_account = Asset_Checking`, `to_account = <liability>` (the row's `to_account`, reducing the liability balance), amount = `principal`, tax_character = `'none'`, `parent_event_id` linked to the interest leg.
- **Use:** one row per active mortgage. Retired by a `retire_scheduled` calendar event when the home is sold (see §3.4).

#### 2.8 `refill_transfer` (priority band: 800)

Conditional fixed top-up: when the `to_account` balance is below a threshold at fire time, transfer a fixed amount; otherwise do nothing. Models concrete manual transfers ("top up checking by $5,000 whenever it drops below $20,000") as a more explicit alternative to `waterfall_fill`.

- **Params:** the row's `to_account` is the account being checked and refilled; `amount` = refill size in dollars. `params_json` may set `threshold_cents` (default `0`).
- **Sources (optional):** if `scheduled_transaction_source` rows exist for the rule, sources are drained in `position` order until the refill is satisfied (same convention as `waterfall_fill`, including the rule that `from_account` must equal the `position = 1` source). If no source rows exist, the entire refill comes from `from_account`.
- **Compute:** at fire time, if `to_account_balance >= threshold_cents`, emit zero events. Otherwise transfer `amount × fire_count` from the configured source(s) to `to_account`. Unlike `waterfall_fill`, the transfer is a **fixed size** that does not depend on how far below threshold the account is, and sources may end up insufficient (the run does not halt — any unsatisfied remainder is simply not transferred).
- **Emits:** zero or more events. With no sources: one event. With sources: one event per source actually drawn from. Each event carries the row's `tax_character` (no per-leg override).
- **Use:** standalone scheduled refills (savings → checking when checking dips); a more explicit alternative to `waterfall_fill` when authors prefer fixed-size top-ups over fill-to-target.

### 3. `calendar_event.kind` registry

Nine kinds in v1. Per the §2 invariant, calendar events never write to `transfer_event` directly. They fall into two classes:

- **Intent-mutating** (no money moves at expansion time; the event amends the in-memory intent — rates, schedules, aux tables): `set_rate`, `set_amount`, `add_scheduled`, `retire_scheduled`, `swap_aux_table`.
- **Rule-synthesizing** (the event inserts one or more `cadence='one_off'` scheduled_transactions with `created_by_event_id = calendar_event.id`; those rules then fire on `event.date` through the normal monthly step): `one_off_transfer`, `sell_asset`, `buy_asset`, `life_transition`. `life_transition` may also perform intent-mutating sub-actions (e.g. retiring a salary rule).

Where expansions produce multiple synthesized rules, each rule's first (and only) firing is one row in `transfer_event`; linkage across siblings is via the shared `created_by_event_id` on `scheduled_transaction` plus `applied_calendar_event`.

#### 3.1 `set_rate` (primitive)

Replace one rate aux-table entry effective from this date forward.

- **Columns:** `series` = rate-series name, `amount` = new value in micros (e.g. `45000` = 4.5%).
- **Expansion:** inserts a new `aux_table` row with `kind = 'rate'`, the given `series`, `effective_date = event.date`, and `payload_json = {"value_micros": amount}`. The original row is **not** deleted; aux lookups use the most-recent-by-`effective_date` row.
- **Use:** "checking account starts earning 4% in 2027".

#### 3.2 `set_amount` (primitive)

Mutate a `ScheduledTransaction.base_amount` going forward.

- **Columns:** `scheduled_name` = target rule name, `amount` = new base amount in cents, `param_date` = new base date (optional).
- **Expansion:** updates the named row's `base_amount` (and `base_date` if given) in the run's in-memory intent. Persisted as an `applied_calendar_event` row; the run's snapshot of `scheduled_transaction` reflects the post-event state.
- **Use:** "discretionary spending drops to $4,500/mo on retirement".

#### 3.3 `add_scheduled` (primitive)

Introduce a new `ScheduledTransaction` from this date forward.

- **Columns:** all required fields of `scheduled_transaction` (using the same named param columns as §4.6) plus a `sources` array for waterfall rules. `start` defaults to `event.date` if omitted.
- **Expansion:** inserts the row (and any `scheduled_transaction_source` rows) into the in-memory intent.
- **Use:** "Medicare premium starts at age 65"; "RMD rule activates the year you turn 73".

#### 3.4 `retire_scheduled` (primitive)

End an active `ScheduledTransaction`.

- **Columns:** `scheduled_name` = name of rule to retire.
- **Expansion:** sets `end = event.date` on the named row.
- **Use:** "salary stops on retirement"; "mortgage rule retires when home is sold".

#### 3.5 `one_off_transfer` (rule-synthesizing)

A single one-shot movement, expressed as a synthesized rule so the §2 invariant holds.

- **Columns:** `account_1` = from account, `account_2` = to account, `amount` = cents, `tax_character` = tax character.
- **Expansion:** synthesize one `scheduled_transaction` with `amount_rule_kind='fixed_amount'`, `cadence='one_off'`, `start = end = event.date`, `base_amount = amount`, fields from payload, `created_by_event_id = calendar_event.id`. The engine fires it on `event.date`, emitting one `transfer_event` traced to that synthesized rule.
- **Use:** windfalls, gifts, one-time settlements that have no recurring structure.

#### 3.6 `swap_aux_table` (primitive)

Replace an entire aux-table series.

- **Payload:** `{ "aux_kind": "federal_brackets", "series": null, "source": "aux/federal_brackets_2027.csv" }` OR `{ ..., "inline_payload_json": {...} }`.
- **Expansion:** inserts new `aux_table` rows (or one row, depending on kind) at `effective_date = event.date`. Older rows are left in place; lookups pick by most recent.
- **Use:** new tax legislation; updated RMD divisor tables.

#### 3.7 `sell_asset` (rule-synthesizing)

Realize a sale. Each sub-step below is a separately synthesized `cadence='one_off'` rule sharing the same `created_by_event_id`.

- **Payload:** `{ "asset_account": "<real_estate or brokerage account>", "sale_price": <cents>, "cost_basis_override": <cents (optional)>, "proceeds_account": "<asset account>", "selling_costs": <cents>, "associated_liability_account": "<mortgage account (optional)>", "associated_liability_payoff": <cents (optional)>, "tax_character": "ltcg" | "ordinary" | "none" }`.
- **Expansion** (each item is a synthesized rule; firing order set by `priority`):
  1. **Basis recognition rule** — `fixed_amount`, `asset_account → proceeds_account`, `base_amount = cost_basis` (or `cost_basis_override`), `tax_character='return_of_capital'`.
  2. **Gain/loss rule** — `fixed_amount`, `Income_CapitalGain → proceeds_account` (or `Expense_CapitalLoss` reversed for a loss), `base_amount = sale_price - cost_basis - selling_costs`, `tax_character` from payload.
  3. **Selling-costs rule** — `fixed_amount`, `proceeds_account → Expense_Housing` (or appropriate expense), `base_amount = selling_costs`.
  4. **Liability-payoff rule** (only if `associated_liability_account` is set) — `fixed_amount`, `proceeds_account → associated_liability_account`, `base_amount = associated_liability_payoff`, `tax_character='none'`.
  5. **Asset zero-out** is the side-effect of step 1 (the basis transfer empties the asset) — no extra rule.
  6. If `associated_liability_account` is set: also emit an intent-mutating side-action equivalent to `retire_scheduled` against the mortgage_payment rule referencing it (recorded as a secondary `applied_calendar_event`).
- **Use:** home sale; large brokerage liquidation.

#### 3.8 `buy_asset` (rule-synthesizing)

Acquire an asset. As with §3.7, each money-moving sub-step is a synthesized `cadence='one_off'` rule.

- **Payload:** `{ "asset_account": "<name, must exist>", "purchase_price": <cents>, "from_account": "<asset account>", "fees": <cents>, "new_liability_account": "<name (optional)>", "new_liability_principal": <cents (optional)>, "starts_scheduled": ["mortgage_<name>", "property_tax_<name>", ...] }`.
- **Expansion:**
  1. **Down-payment rule** — `fixed_amount`, `from_account → asset_account`, `base_amount = purchase_price - new_liability_principal`, `tax_character='none'`. Sets `asset_account.cost_basis = purchase_price + fees` as a state side-effect at firing time.
  2. **Fees rule** — `fixed_amount`, `from_account → Expense_Housing`, `base_amount = fees`.
  3. **Loan-origination rule** (only if `new_liability_account`) — `fixed_amount`, `Income_LoanOrigination → new_liability_account`, `base_amount = new_liability_principal`, `tax_character='none'`. (`Income_LoanOrigination` is a synthetic income-class account in the standard chart; reserved name.) This preserves the bookkeeping zero-sum invariant from ADR-002 when a new liability appears mid-run.
  4. **Activate scheduled rules** — for each name in `starts_scheduled`: an intent-mutating side-action equivalent to `add_scheduled` referencing a prototype row in `scheduled.csv` (prototype rows are flagged `start = NULL` so they don't fire on their own). Not money-moving in itself.
- **Use:** downsize-to-condo scenario; investment property purchase.

#### 3.9 `life_transition` (expanding)

Author-named bundle of changes that represent a real-world transition. The `kind` is opinionated; the expansion is custom code per kind.

- **Payload:** `{ "transition": "predecease" | "divorce" | "relocate" | "retire" | "long_term_care", "subject_person": "<person_id>", "params_json": { ... transition-specific ... } }`.
- **Expansion (per transition):**
  - **`predecease`**: retire the deceased person's salary/pension scheduled rules (intent-mutating); transfer survivor benefit eligibility on SS rule (intent-mutating; params recompute base PIA per survivor rules); merge deceased's accounts into surviving spouse's via synthesized `one_off` fixed_amount rules (rule-synthesizing); switch filing-status aux to `single` (intent-mutating).
  - **`divorce`**: split asset accounts per a payload-specified percentage; sever shared scheduled rules; switch filing-status aux to `single`.
  - **`relocate`**: swap state-tax bracket aux to new state; update property-tax rates.
  - **`retire`**: retire salary rule for the subject person; start Medicare premium rules; flip discretionary-spending `set_amount`.
  - **`long_term_care`**: add a large `fixed_amount` expense rule with COLA series `cpi_medical`.
- **Use:** the scenario-as-checklist payoff (ADR-001 principle 5). The `--check life-transitions` validator flags any expected transition missing from a scenario that runs past a person's life-expectancy threshold.

### 4. CSV file layouts

All files: UTF-8, comma-delimited, one header row. Empty string means NULL. Boolean columns use `0`/`1`. Date columns use `YYYY-MM` (month-grain, pinned to month-end at load) or `YYYY-MM-DD` where specified. Money columns are decimal dollars (e.g. `1234.56`); the loader converts them to integer cents.

JSON-typed columns hold a single JSON value as a string; the loader parses and revalidates against the rule/event kind. Multi-line JSON in CSV is supported via standard CSV quoting.

#### 4.1 Shared base: `data_repo/persons.csv`

Single identity list for the household: account owners *and* operator identities (`$ANALYSIS_USER`) live in the same table. The two sets typically coincide; the `role` column distinguishes persons-who-can-act from persons-who-only-appear (e.g. deceased ancestors carried for basis, a `joint` household pseudo-row, future heirs).

| column | type | required | notes |
| --- | --- | --- | --- |
| `id` | text | ✓ | short identifier, e.g. `rwk`; matched against `$ANALYSIS_USER` |
| `display` | text | ✓ | full or display name |
| `birthdate` | YYYY-MM-DD | ✓ | for RMD and life-transition computations |
| `role` | text | | informational; conventional values `primary`, `secondary`; NULL = person-not-operator |
| `meta_json` | json | | default `{}` |

#### 4.2 Shared base: `data_repo/accounts.csv`

| column | type | required | notes |
| --- | --- | --- | --- |
| `name` | text | ✓ | unique; referenced by other CSVs |
| `kind` | enum | ✓ | `asset`, `liability`, `income`, `expense` |
| `subkind` | enum | ✓ | per §1 |
| `owner` | text | | `person.id`; NULL for jointly-held |
| `tax_treatment` | enum | | `taxable`, `tax_deferred`, `tax_free`, `none` |
| `opened` | YYYY-MM | | informational |
| `closed` | YYYY-MM | | informational |
| `meta_json` | json | | default `{}` |

#### 4.3 Shared: `data_repo/account_models/<name>.csv`

One file per named model. Filename stem becomes `account_model.name`.

| column | type | required | notes |
| --- | --- | --- | --- |
| `account_name` | text | ✓ | resolves to `account.id` |
| `opening_balance` | cents | ✓ | balance at the model's effective start |
| `opening_cost_basis` | cents | | required for accounts with `tax_treatment IN ('taxable')` holding appreciable assets |
| `prior_year_end_balance` | cents | | balance at the Dec 31 *preceding* `scenario.start`; required for `traditional_ira` / `traditional_401k` accounts whose owner reaches RMD age during the scenario's first calendar year, so the partial-first-year RMD has a Dec-31 balance to divide by. NULL for all other accounts and for scenarios that start on Jan 1 of a year in which RMDs are computed against the *current* simulated year-end. |

#### 4.4 Shared: `data_repo/aux/<kind>[__<series>].csv`

One file per (kind) or (kind, series). Filename pattern: `federal_brackets.csv`, `rate__Asset_Checking.csv`, `inflation__cpi.csv`. Columns vary by kind; the loader switches on filename. **The series name is derived from the filename stem after `__`, with every `_` replaced by a space.** So `rate__Asset_Checking.csv` has series `Asset Checking`, and `rate_series` in `amount_rule_params_json` must use the space form. This lets account names contain spaces while filenames stay underscore-safe.

- **`federal_brackets.csv`**, **`state_brackets__<state>.csv`**:
  `effective_date`, `filing_status`, `bracket_index`, `floor`, `marginal_rate_micros`
- **`standard_deduction.csv`**:
  `effective_date`, `filing_status`, `amount`
- **`ss_taxation_thresholds.csv`**:
  `effective_date`, `filing_status`, `tier1_floor`, `tier2_floor`
- **`rmd_factors.csv`**:
  `effective_date`, `age`, `factor_micros`
- **`rate__<account_name>.csv`**:
  `effective_date`, `annual_rate_micros`
- **`inflation__<series>.csv`**:
  `effective_date`, `annual_rate_micros` (deterministic projection; future ensembles substitute realizations)
- **`filing_status.csv`**:
  `effective_date`, `filing_status` (rows reflect transitions; one row per change)
- **`rmd_start_age.csv`**:
  `effective_date`, `start_age`

#### 4.5 Per-scenario: `data_repo/scenarios/<name>/scenario.csv`

Exactly one data row (the loader rejects more).

| column | type | required | notes |
| --- | --- | --- | --- |
| `name` | text | ✓ | must match folder name |
| `description` | text | | |
| `account_model` | text | ✓ | `account_model.name` |
| `start` | YYYY-MM | ✓ | first simulated month |
| `end` | YYYY-MM | ✓ | last simulated month (inclusive) |
| `seed` | int | | reserved for ensembles |
| `parent_scenario` | text | | provenance only in v1 (folder name of the scenario this one was forked from). Reuse/inheritance semantics — add/replace/delete of scheduled/calendar/aux rows from the parent — are deliberately deferred to a later ADR. |
| `created_by` | text | ✓ | `$ANALYSIS_USER` at authoring time |
| `meta_json` | json | | default `{}` |

The calendar lives at `scenarios/<name>/calendar.csv` by filename convention — there is no `calendar` column. Calendars are the least-shared file in practice; the `parent_scenario` mechanism is the intended path for reuse once its semantics are settled.

#### 4.6 Per-scenario: `data_repo/scenarios/<name>/scheduled.csv`

| column | type | required | notes |
| --- | --- | --- | --- |
| `name` | text | ✓ | unique within scenario |
| `from_account` | text | ✓ | primary leg from |
| `to_account` | text | ✓ | primary leg to |
| `cadence` | enum | ✓ | `monthly`, `quarterly`, `annual`, `one_off` |
| `month_of_year` | int 1-12 | | required for `annual`; ignored otherwise |
| `amount_rule_kind` | enum | ✓ | one of §2 |
| `interest_account` | text | | mortgage_payment only: interest-expense account for the second leg |
| `person` | text | | person id; required for `ss_benefit` and `rmd` |
| `series` | text | | aux rate series name (e.g. `Asset Checking`, `cpi`); required for `percent_of_balance`, `ss_benefit` |
| `rate_micros` | int | | annual rate as millionths of 1; required for `mortgage_payment` (e.g. `60000` = 6.0%) |
| `claim_age` | int | | required for `ss_benefit` |
| `amount` | dollars | | base amount in dollars; required for `ss_benefit` (pia), `waterfall_fill` (target balance), `refill_transfer` (refill size); decimal string e.g. `1234.56`; loader converts to integer cents |
| `base_amount` | dollars | | required for `fixed_amount`, `mortgage_payment`; decimal string e.g. `1234.56`; loader converts to integer cents at load time |
| `base_date` | YYYY-MM | | required when `inflation_series` is set |
| `inflation_series` | text | | aux series name, e.g. `cpi`; or `fixed` for no inflation |
| `tax_character` | enum | ✓ | per ADR-002 (`ordinary`, `ltcg`, `ss_benefit`, `tax_free`, `tax_deferred_in`, `tax_deferred_out`, `return_of_capital`, `property_tax`, `none`); set per-row by the author. Account `kind`/`tax_treatment` does not constrain this field — an income-account-sourced transfer can carry `tax_character='none'` (e.g. an inheritance), and an expense-leg can carry any value. `waterfall_fill` is the one exception: per §2.4, the engine overrides each leg's `tax_character` from the source account's `tax_treatment`. |
| `start` | YYYY-MM | ✓ | first month rule may fire (a *lower bound* — rules with internal age gates like `ss_benefit` and `rmd` AND this with their own gate; see §2.3 and §2.5) |
| `end` | YYYY-MM | | open-ended if blank |
| `priority` | int | | defaults per §2's priority band |
| `description` | text | | |
| `meta_json` | json | | default `{}` |
| `params_json` | json | | nullable; for rare or future extension parameters not covered by the named columns above |
| `ytd_amount` | dollars | | partial-first-year catch-up (§4.10); only valid when `amount_rule_kind='fixed_amount'` and `cadence='monthly'`; mutually exclusive with `ytd_preroll` |
| `ytd_preroll` | `0`/`1` | | if `1`, catch-up amount = `base_amount × (scenario.start.month − 1)`; same restrictions as `ytd_amount` |

#### 4.7 Per-scenario: `data_repo/scenarios/<name>/waterfall_sources.csv`

| column | type | required | notes |
| --- | --- | --- | --- |
| `scheduled_name` | text | ✓ | name from `scheduled.csv` with `amount_rule_kind` either `waterfall_fill` or `refill_transfer` |
| `position` | int | ✓ | 1-based, monotonic per `scheduled_name` |
| `account_name` | text | ✓ | source account |

#### 4.8 Per-scenario: `data_repo/scenarios/<name>/calendar.csv`

| column | type | required | notes |
| --- | --- | --- | --- |
| `date` | YYYY-MM | ✓ | month-end at load |
| `kind` | enum | ✓ | one of §3 |
| `scheduled_name` | text | | name of target scheduled transaction; required for `retire_scheduled`, `set_amount` |
| `account_1` | text | | first event-specific account (e.g. `from_account` for `one_off_transfer`) |
| `account_2` | text | | second event-specific account (e.g. `to_account` for `one_off_transfer`) |
| `series` | text | | rate series name; required for `set_rate` |
| `amount` | int | | amount in appropriate units (micros for `set_rate`, cents for `one_off_transfer`, cents for `set_amount`) |
| `param_date` | YYYY-MM | | effective date for `set_amount` new base |
| `tax_character` | enum | | tax character for `one_off_transfer` |
| `params_json` | json | | nullable; for rare or future extension parameters not covered by the named columns above |
| `description` | text | | shows up in `explain` output |

#### 4.9 Per-scenario: `data_repo/scenarios/<name>/lifespan.csv` (optional)

Lets a scenario play with a person's lifespan without touching the shared `persons.csv` (birthdate lives there; death is a per-comparison assumption). File may be omitted entirely — no rows means no lifespan overrides.

| column | type | required | notes |
| --- | --- | --- | --- |
| `person` | text | ✓ | `person.id`; at most one row per person |
| `final_month` | YYYY-MM-DD | ✓ | last month this person is alive; month-end date |

Writes directly into `person_lifespan` (PK `(scenario_id, person)`). At `date > final_month`, the engine stops firing every `scheduled_transaction` **associated with that person** — the row's explicit `person` column (`ss_benefit`, `rmd`), or a row whose `from_account`/`to_account` owner is that person (`salary`, per-person expenses, etc.). Jointly-owned accounts (`owner` blank) are unaffected, so shared expenses and joint-account interest continue for a surviving spouse. This is a lighter-weight alternative to a `life_transition: predecease` calendar event — it stops the deceased person's own transfers but does **not** merge accounts, adjust survivor SS benefits, or switch filing status; use `life_transition` when those effects matter.

#### 4.10 Partial-first-year catch-up (`ytd_amount`/`ytd_preroll` on scheduled.csv; `year_to_date.csv`)

A scenario's first simulated month is rarely January 1: the author needs the run's first (partial) calendar year to report correctly (annual totals, tax accrual) as if the elapsed months had actually been simulated. This also covers what an earlier revision called `priors.csv`/`prior_tax_activity` (tax-bucket-only YTD facts and fully-prior-year activity like loss carryforwards) — consolidated here since `year_to_date.csv`'s account-based model is strictly more general and lets these facts show up in ordinary income/expense reporting queries, not just tax computation.

Two mechanisms cover this, both posting **balance-neutral** `transfer_event` rows — visible to income/expense/tax queries exactly like any other flow, but never mutating account balances, since `account_model_member.opening_balance` already reflects the true, post-receipt balance as of `scenario.start`. Both fire once, at step 0 (`step_index=0`).

**On `scheduled.csv`** (§4.6), for a single `fixed_amount` + `cadence='monthly'` row's own catch-up, since the amount is directly derived from that row's `base_amount`:

- `ytd_amount` (dollars) — hard actual total already paid/received this calendar year before `scenario.start`. Posted with `reason_kind='ytd_rule'`, `reason_id` = the row's own `scheduled_transaction.id`.
- `ytd_preroll` (`0`/`1`) — auto-computed as `base_amount × (scenario.start.month − 1)`, i.e. one full month's amount per elapsed month, unadjusted for inflation (a same-order approximation, not a substitute for real records).
- Mutually exclusive; loader rejects both set together, or either set on a non-`fixed_amount`/non-`monthly` row.
- Dated the last day of the month before `scenario.start` (degenerates to `scenario.start` itself when it falls in January).

**`data_repo/scenarios/<name>/year_to_date.csv`** (optional) — for everything else needing a partial-first-year catch-up that isn't one `fixed_amount` monthly rule's own amount (investment growth, Social Security already received, prior withholding, fully-prior-year loss carryforwards, etc.). No `cadence`/`inflation_series`/`base_date`: each row is one flat historical fact, not a recurring rule. Posted with `reason_kind='ytd'`, `reason_id` = the row's `year_to_date.id` (a distinct id space from `scheduled_transaction.id`, hence the different `reason_kind`).

| column | type | required | notes |
| --- | --- | --- | --- |
| `name` | text | ✓ | unique within scenario; identifies the row for `reason_id` provenance |
| `from_account` | text | ✓ | mirrors `one_off_transfer`'s `account_1`. For a pure tax-bucket adjustment with no natural real-world transfer (e.g. a loss carryforward), use the same account for `from_account` and `to_account` — self-canceling, contributes only to the tax_character sum |
| `to_account` | text | ✓ | mirrors `one_off_transfer`'s `account_2` |
| `amount` | dollars | ✓ | decimal string; loader converts to integer cents. May be negative (e.g. a loss carryforward reducing that year's `ltcg` bucket) |
| `tax_character` | enum | ✓ | per ADR-002 |
| `year` | int | | defaults to `scenario.start`'s year (dated the month before start, as above). An explicit different year is dated that year's Dec 31 — for facts belonging to a fully-prior tax year (not the scenario's own partial first year) |
| `description` | text | | |

Writes directly into `year_to_date` (`UNIQUE (scenario_id, name)`).

### 5. Default priorities

When `scheduled.csv` leaves `priority` blank, the loader fills it from the rule kind's band:

| rule kind | default priority |
| --- | --- |
| `percent_of_balance` | 100 |
| `ss_benefit` | 200 |
| `fixed_amount` | 200 (income) / 300 (expense) / 500 (one-off) — disambiguated by `tax_character` and `cadence`; the loader emits a warning if it had to guess |
| `mortgage_payment` | 300 |
| `tax_accumulator` | 400 |
| `rmd` | 500 |
| `refill_transfer` | 800 |
| `waterfall_fill` | 900 |

Authors can override by setting `priority` explicitly. The check command (`analysis sim check --priorities`) flags rows whose explicit priority crosses a band boundary, since that's almost always a mistake.

### 6. Ad-hoc tags (`tags` column)

`accounts.csv`, `scheduled.csv`, and `calendar.csv` carry an optional **`tags`** column
that holds a whitespace-separated list of `<role>:<name>` tokens. Tags are an
**open** registry — `role` is not validated against a fixed vocabulary — and are
intended for low-friction ad-hoc grouping (e.g. `expense:discretionary
household:rwk`, `tax:itemized`, `notebook:net_worth`).

Token shape: `^[a-z0-9_]+:[a-z0-9_]+$`. Both halves are required; the loader
rejects malformed tokens.

The loader normalizes tags into three join tables in the intent (and snapshotted
run) DB:

- `tag(id, role, name)` with `UNIQUE(role, name)`,
- `account_tag(account_id, tag_id)`,
- `scheduled_transaction_tag(scheduled_transaction_id, tag_id)`,
- `calendar_event_tag(calendar_event_id, tag_id)`.

CLI helpers (`analysis sim tag add|rm|list|items`) read/write the underlying
CSV row's `tags` cell via pandas, preserving the existing header-validation
contract. Calendar events are tagged via CSV directly (no stable CLI key yet).
Tags are scenario-scoped today; when scenario inheritance lands they will be
inherited along the parent chain.

## Consequences

### Positive

- The full vocabulary of the simulator is in one file. A new reader can enumerate every kind of rule and every kind of event in a single pass; the loader's CHECKs guarantee the code agrees.
- **One-rule-per-event invariant** makes `explain`, `compare`, and audit trivial: a `transfer_event` always has exactly one `scheduled_transaction` as its origin; group-by `reason_id` is the canonical aggregation. Calendar events route through synthesized rules, so the calendar never short-circuits the rule audit trail.
- Multi-leg rules are an explicit, named concept (currently only `mortgage_payment`). The "rule emits a list" interface generalizes cleanly when refinancing or escrow accounts are added later.
- Subkind vocabulary is locked but extensible — adding a new subkind is one row in this ADR plus a code change in the loader's validator, no schema migration.
- The `life_transition` kinds operationalize the "scenario as checklist" principle; `--check life-transitions` becomes an actual lint.

### Negative / accepted

- Free-text `kind`/`subkind` in the DDL with loader-side validation means a hand-written SQL `INSERT` could bypass the registry. The loader is the only sanctioned writer; ad-hoc edits to `state/intent/*.sqlite3` are out of scope (and would be overwritten on next reload anyway).
- The `life_transition` expansion code is per-transition. Adding a new transition is a code change, not a data change. That's deliberate — these transitions are rare and high-impact enough to want a code review, not a CSV edit.
- `sell_asset` requires the author to supply `cost_basis_override` whenever `account_state.cost_basis` is NULL (basis was never tracked). The `--check basis` validator surfaces these cases before a run fails.
- `priors.csv` is intentionally narrow (tax activity only). Account-level state facts that used to live in a union `prior_account_fact` table now live as columns on `account_model_member` (`opening_balance`, `opening_cost_basis`, `prior_year_end_balance`). This keeps the "what's in the accounts at scenario start" story in one file (`account_models/<name>.csv`) and lets `priors.csv` stay a thin tax-activity feed. (Superseded: `priors.csv`/`prior_tax_activity` were later folded into `year_to_date.csv` — see §4.10.)

## Amendments applied to ADR-002 alongside this ADR

These amendments to ADR-002 fall out of the §2 invariant, §1 vocabulary, and the priors-vs-account-state split; all have been rolled into ADR-002:

1. Tightened `transfer_event.reason_kind` CHECK from `('scheduled','calendar','expansion_child','initial')` to `('rule','initial')`.
2. Updated `transfer_event.reason_id` comment: "scheduled_transaction.id when 'rule'; opaque per-account id when 'initial'."
3. Added `created_by_event_id INTEGER REFERENCES calendar_event(id)` to `scheduled_transaction` (NULL for rows authored in `scheduled.csv`; populated for rules synthesized by calendar-event expansion) plus its supporting index.
4. Reserved the `Income_LoanOrigination` account name in the Conventions section, used by `buy_asset` synthesized origination rules.
5. Added the `account.subkind` CHECK enforcing the §1 vocabulary as a single compound constraint keyed off `kind`.
6. Added `interest_expense` to the expense subkind vocabulary (in the §1 table and the `account` CHECK).
7. Dropped the `prior_account_fact` table entirely; added `prior_year_end_balance INTEGER` (nullable) to `account_model_member` to carry the one fact `prior_account_fact` actually drove (partial-first-year RMD's `dec31_prev_year_balance`).
8. Added `role TEXT` (nullable) to `person`, unifying the former standalone `users.toml` operator list with the persons table. `$ANALYSIS_USER` is now validated against `persons.csv`; `users.toml` is removed from the data-repo layout.

## Open questions deferred

- Vocabulary of `tax_year_summary.bucket` is referenced by `prior_tax_activity` and `tax_accumulator`. Locked when ADR on tax computation lands.
- Whether `add_scheduled` should reference a prototype row by name (cleaner) or inline the full payload (self-contained). Currently allows both. May restrict to "prototype by name" in v2 if the inline form proves error-prone.
- Whether `life_transition.predecease` needs separate sub-transitions for survivor-benefit edge cases (e.g. deceased spouse hadn't claimed yet). Will refine when the first realistic scenario exercises it.
