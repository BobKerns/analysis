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
| `housing` | utilities, insurance, maintenance (not mortgage interest, which is its own leg) |
| `taxes_federal` | federal income tax paid |
| `taxes_state` | state income tax paid |
| `taxes_property` | real estate tax |
| `other_expense` | catch-all |

The `_other` catch-alls are deliberate escape hatches; the `--check` mode (ADR-001 §CLI) flags any account using one so the author can decide whether to extend the vocabulary.

### 2. `amount_rule_kind` registry

Seven rule kinds in v1. Each rule's `compute(state, aux, date) → list[TransferEventSpec]` is a pure function. The columns `from_account`, `to_account`, `base_amount`, `base_date`, `inflation_series` on `scheduled_transaction` carry the common parameters; rule-specific parameters live in `amount_rule_params_json`.

**Invariant (rule-as-sole-origin):** every `transfer_event` traces to **exactly one rule** via `(reason_kind='rule', reason_id = scheduled_transaction.id)`. A once-only movement is a rule whose `cadence='one_off'`. Calendar events never write to `transfer_event` directly; money-moving calendar kinds *synthesize* one or more `cadence='one_off'` scheduled_transactions at expansion time (see §3). The only exception is the synthetic per-account `opening_balance` rule materialized at run start from the account model, which uses `reason_kind='initial'` and is its own degenerate rule. (ADR-002 follow-up: tighten `transfer_event.reason_kind` CHECK to `('rule','initial')` and add `scheduled_transaction.created_by_event_id INTEGER REFERENCES calendar_event(id)` so synthesized rules trace back to their authoring calendar event.)

**Multi-leg rules** (in v1: only `mortgage_payment`) emit more than one transfer event per fire; all legs share the same `(reason_kind, reason_id)` and are linked by `parent_event_id` for grouping. The `from_account`/`to_account` columns on the scheduled_transaction row are interpreted as the **primary leg**; additional legs are fully described by `amount_rule_params_json`. This invariant — "each event traces to one rule, but a rule may emit multiple events per fire" — is the *direction* that matters: it makes audit, `explain`, and `compare` straightforward.

#### 2.1 `fixed_amount` (priority bands: 200, 300, 500)

Constant amount, optionally inflated.

- **Params:** none in JSON. Uses `base_amount` (required, cents), `base_date` (optional), `inflation_series` (optional aux series).
- **Compute:** `amount = base_amount` if `inflation_series` is NULL else `base_amount × cumulative_inflation(base_date → date, series)`.
- **Emits:** one event, `from_account → to_account`, amount as above, tax_character from the row.
- **Use:** salary, SS post-FRA (without COLA series), pensions, recurring fixed expenses, lump-sum one-offs.

#### 2.2 `percent_of_balance` (priority band: 100)

Accrue interest, dividends, appreciation against a source account's balance.

- **Params:** `{ "source_account": "<name>", "rate_basis": "balance" | "cost_basis", "rate": <int_micros> }` OR `{ ..., "rate_series": "<aux_table series name>" }` (one of the two).
- **Compute:** `amount = round(source_balance × monthly_rate)`. Annual rates are converted by `(1 + r)^(1/12) - 1`, computed in fixed-point micros.
- **Emits:** one event. Typical: `from_account = <income account>` (e.g. `Income_Interest`), `to_account = <source_account>` (interest credits to the asset). For depreciation, swap signs by routing through an expense account.
- **Use:** money-market interest, bond coupon approximations, portfolio appreciation per asset, real-estate appreciation.

#### 2.3 `rmd` (priority band: 200 or 500)

IRS Required Minimum Distribution.

- **Params:** `{ "subject_account": "<traditional_ira / traditional_401k name>", "person": "<person_id>", "factor_table": "rmd_factors" }`.
- **Compute:** Fires once per year (cadence = `annual`, `month_of_year` typically 12). `amount = round(dec31_prev_year_balance / uniform_lifetime_factor(person_age_year_end))`. `dec31_prev_year_balance` comes from end-of-prior-year `account_state`; for the partial first year it comes from `prior_account_fact(fact_kind='dec31_balance')`.
- **Emits:** one event, `from_account = subject_account`, `to_account = <taxable landing>` (typically `Asset_Checking`); `tax_character = 'tax_deferred_out'` (the retrospective tax scan reclassifies as ordinary income).
- **Use:** post-age-73 required distributions for either spouse.

#### 2.4 `waterfall_fill` (priority band: 900 — must run last)

Cover any remaining cash shortfall in a target account by drawing from an ordered list of sources.

- **Params:** `{ "target_account": "<name>", "target_balance": <cents> }` (the minimum acceptable end-of-month balance; usually 0 or a small cushion).
- **Sources:** rows in `scheduled_transaction_source` ordered by `position`.
- **Compute:** at fire time, `shortfall = max(0, target_balance - target_balance_after_all_other_rules)`. If `shortfall > 0`, draw `min(shortfall, source_balance)` from sources in order. If still short after all sources, **halt the run** (per ADR-001 principle 7); record `failure_reason = "WaterfallExhausted: <month> short $<amount>"` and persist partial results.
- **Emits:** zero or more events (one per source actually drawn from). Each event uses `tax_character` from the source's account semantics (taxable brokerage → `ltcg`, IRA → `tax_deferred_out`, etc.) — the `tax_character` column on the scheduled_transaction row is overridden per leg from `account.tax_treatment`.
- **Use:** the single "fill checking from savings, then brokerage, then traditional IRA" rule that every scenario carries.

#### 2.5 `ss_benefit` (priority band: 200)

Social Security retirement benefit with COLA.

- **Params:** `{ "person": "<person_id>", "claim_age": 67, "base_pia_at_fra": <cents>, "cola_series": "ss_cola" }`.
- **Compute:** Fires monthly. Adjusts `base_pia_at_fra` by the claim-age multiplier (a lookup based on months early/late vs. FRA, hard-coded) and applies cumulative COLA from claim date to `date`. Zero before the person reaches `claim_age`.
- **Emits:** one event, `from_account = Income_SS_<person>`, `to_account = Asset_Checking`, `tax_character = 'ss_benefit'`.
- **Use:** each spouse's SS rule. Different scenarios swap `claim_age` to test claim-timing decisions.

#### 2.6 `tax_accumulator` (priority band: 400)

A placeholder rule that marks "compute taxes for the prior tax year" at a specific calendar point. Fires at year-end (December) for accrual and in April for reconciliation.

- **Params:** `{ "year": "previous", "scope": "federal" | "state", "filing_status_aux": "filing_status", "bracket_aux": "federal_brackets" }`.
- **Compute:** delegates to `tax.py`. Scans `transfer_event` rows for the relevant year by `tax_character`, applies brackets and deductions from aux tables, and computes net tax owed.
- **Emits:** one event in April: `from_account = Asset_Checking`, `to_account = Expense_Taxes_Federal` (or `_State`), amount = computed tax minus withholding already paid via `fixed_amount` rules through the year. The December firing typically emits zero (it only writes `tax_year_summary` rows for visibility) but may emit a true-up if estimated payments are part of the scenario.
- **Use:** one per (year, scope) pair, authored as `cadence = annual`.

#### 2.7 `mortgage_payment` (priority band: 300; multi-leg)

Monthly P&I against a mortgage liability account.

- **Params:** `{ "liability_account": "<mortgage account name>", "annual_rate_micros": <int>, "interest_expense_account": "<name>", "payment_amount": <cents> }`. `payment_amount` is the fixed total monthly P&I (the row's `base_amount`).
- **Compute:** at fire time, `interest = round(liability_balance × annual_rate_micros / 12 / 1_000_000)`; `principal = payment_amount - interest`.
- **Emits two events:**
  1. **Interest leg:** `from_account = Asset_Checking` (the row's `from_account` — primary leg), `to_account = interest_expense_account`, amount = `interest`, tax_character = `'none'`.
  2. **Principal leg:** `from_account = Asset_Checking`, `to_account = liability_account` (reducing the liability balance), amount = `principal`, tax_character = `'none'`.
- The row's `to_account` is set to `liability_account` (the principal leg target); the interest target is in JSON.
- **Use:** one row per active mortgage. Retired by a `retire_scheduled` calendar event when the home is sold (see §3.4).

### 3. `calendar_event.kind` registry

Nine kinds in v1. Per the §2 invariant, calendar events never write to `transfer_event` directly. They fall into two classes:

- **Intent-mutating** (no money moves at expansion time; the event amends the in-memory intent — rates, schedules, aux tables): `set_rate`, `set_amount`, `add_scheduled`, `retire_scheduled`, `swap_aux_table`.
- **Rule-synthesizing** (the event inserts one or more `cadence='one_off'` scheduled_transactions with `created_by_event_id = calendar_event.id`; those rules then fire on `event.date` through the normal monthly step): `one_off_transfer`, `sell_asset`, `buy_asset`, `life_transition`. `life_transition` may also perform intent-mutating sub-actions (e.g. retiring a salary rule).

Where expansions produce multiple synthesized rules, each rule's first (and only) firing is one row in `transfer_event`; linkage across siblings is via the shared `created_by_event_id` on `scheduled_transaction` plus `applied_calendar_event`.

#### 3.1 `set_rate` (primitive)

Replace one rate aux-table entry effective from this date forward.

- **Payload:** `{ "aux_kind": "rate", "series": "<account_name or named series>", "new_value_micros": <int> }`.
- **Expansion:** inserts a new `aux_table` row with `kind = 'rate'`, the given `series`, `effective_date = event.date`, and `payload_json = {"value_micros": ...}`. The original row is **not** deleted; aux lookups use the most-recent-by-`effective_date` row.
- **Use:** "checking account starts earning 4% in 2027".

#### 3.2 `set_amount` (primitive)

Mutate a `ScheduledTransaction.base_amount` going forward.

- **Payload:** `{ "scheduled_name": "<name>", "new_base_amount": <cents>, "new_base_date": "<YYYY-MM-DD>" (optional) }`.
- **Expansion:** updates the named row's `base_amount` (and `base_date` if given) in the run's in-memory intent. Persisted as an `applied_calendar_event` row; the run's snapshot of `scheduled_transaction` reflects the post-event state.
- **Use:** "discretionary spending drops to $4,500/mo on retirement".

#### 3.3 `add_scheduled` (primitive)

Introduce a new `ScheduledTransaction` from this date forward.

- **Payload:** all required fields of `scheduled_transaction` plus `amount_rule_params_json` and a `sources` array for waterfall rules. `start` defaults to `event.date` if omitted.
- **Expansion:** inserts the row (and any `scheduled_transaction_source` rows) into the in-memory intent.
- **Use:** "Medicare premium starts at age 65"; "RMD rule activates the year you turn 73".

#### 3.4 `retire_scheduled` (primitive)

End an active `ScheduledTransaction`.

- **Payload:** `{ "scheduled_name": "<name>" }`.
- **Expansion:** sets `end = event.date` on the named row.
- **Use:** "salary stops on retirement"; "mortgage rule retires when home is sold".

#### 3.5 `one_off_transfer` (rule-synthesizing)

A single one-shot movement, expressed as a synthesized rule so the §2 invariant holds.

- **Payload:** `{ "from_account": "...", "to_account": "...", "amount": <cents>, "tax_character": "...", "description": "..." }`.
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

All files: UTF-8, comma-delimited, one header row. Empty string means NULL. Boolean columns use `0`/`1`. Date columns use `YYYY-MM` (month-grain, pinned to month-end at load) or `YYYY-MM-DD` where specified. Money columns are integer cents (no decimal point).

JSON-typed columns hold a single JSON value as a string; the loader parses and revalidates against the rule/event kind. Multi-line JSON in CSV is supported via standard CSV quoting.

#### 4.1 Shared base: `data_repo/persons.csv`

| column | type | required | notes |
| --- | --- | --- | --- |
| `id` | text | ✓ | short identifier, e.g. `rwk` |
| `display` | text | ✓ | full or display name |
| `birthdate` | YYYY-MM-DD | ✓ | for RMD and life-transition computations |
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

#### 4.4 Shared: `data_repo/aux/<kind>[__<series>].csv`

One file per (kind) or (kind, series). Filename pattern: `federal_brackets.csv`, `rate__Asset_Checking.csv`, `inflation__cpi.csv`. Columns vary by kind; the loader switches on filename:

- **`federal_brackets.csv`**, **`state_brackets__<state>.csv`**:
  `effective_date`, `filing_status`, `bracket_index`, `floor_cents`, `marginal_rate_micros`
- **`standard_deduction.csv`**:
  `effective_date`, `filing_status`, `amount_cents`
- **`ss_taxation_thresholds.csv`**:
  `effective_date`, `filing_status`, `tier1_floor_cents`, `tier2_floor_cents`
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
| `calendar` | text | ✓ | this scenario's calendar's name (folder-local; convention = scenario name) |
| `start` | YYYY-MM | ✓ | first simulated month |
| `end` | YYYY-MM | ✓ | last simulated month (inclusive) |
| `seed` | int | | reserved for ensembles |
| `parent_scenario` | text | | provenance only |
| `created_by` | text | ✓ | `$ANALYSIS_USER` at authoring time |
| `meta_json` | json | | default `{}` |

#### 4.6 Per-scenario: `data_repo/scenarios/<name>/scheduled.csv`

| column | type | required | notes |
| --- | --- | --- | --- |
| `name` | text | ✓ | unique within scenario |
| `from_account` | text | ✓ | primary leg from |
| `to_account` | text | ✓ | primary leg to |
| `cadence` | enum | ✓ | `monthly`, `quarterly`, `annual`, `one_off` |
| `month_of_year` | int 1-12 | | required for `annual`; ignored otherwise |
| `amount_rule_kind` | enum | ✓ | one of §2 |
| `amount_rule_params_json` | json | | default `{}`; required when the rule's params §2 are non-empty |
| `base_amount` | cents | | required for `fixed_amount`, `mortgage_payment` |
| `base_date` | YYYY-MM | | required when `inflation_series` is set |
| `inflation_series` | text | | aux series name, e.g. `cpi`; or `fixed` for no inflation |
| `tax_character` | enum | ✓ | per ADR-002 |
| `start` | YYYY-MM | ✓ | first month rule may fire |
| `end` | YYYY-MM | | open-ended if blank |
| `priority` | int | | defaults per §2's priority band |
| `description` | text | | |
| `meta_json` | json | | default `{}` |

#### 4.7 Per-scenario: `data_repo/scenarios/<name>/waterfall_sources.csv`

| column | type | required | notes |
| --- | --- | --- | --- |
| `scheduled_name` | text | ✓ | name from `scheduled.csv` with `amount_rule_kind = 'waterfall_fill'` |
| `position` | int | ✓ | 1-based, monotonic per `scheduled_name` |
| `account_name` | text | ✓ | source account |

#### 4.8 Per-scenario: `data_repo/scenarios/<name>/calendar.csv`

| column | type | required | notes |
| --- | --- | --- | --- |
| `date` | YYYY-MM | ✓ | month-end at load |
| `kind` | enum | ✓ | one of §3 |
| `payload_json` | json | ✓ | shape per §3 for that kind |
| `description` | text | | shows up in `explain` output |

#### 4.9 Per-scenario: `data_repo/scenarios/<name>/priors.csv`

A union table for both `prior_tax_activity` and `prior_account_fact`, distinguished by `prior_kind`.

| column | type | required | notes |
| --- | --- | --- | --- |
| `prior_kind` | enum | ✓ | `tax_activity` or `account_fact` |
| `year` | int | tax_activity | calendar year |
| `bucket` | text | tax_activity | matches `tax_year_summary.bucket` vocabulary |
| `account_name` | text | account_fact | |
| `fact_kind` | text | account_fact | `ytd_401k_contribution`, `dec31_balance`, `cost_basis_estimate`, … |
| `as_of` | YYYY-MM-DD | account_fact | |
| `amount` | cents | both | |
| `source_note` | text | | free-text provenance |

The loader splits this into the two underlying tables; the union format keeps all "seed" data in one file for the author.

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
| `waterfall_fill` | 900 |

Authors can override by setting `priority` explicitly. The check command (`analysis sim check --priorities`) flags rows whose explicit priority crosses a band boundary, since that's almost always a mistake.

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
- The priors.csv union format saves a file at the cost of two `required`-conditional columns. The error messages on misuse are explicit ("priors.csv row N: prior_kind=tax_activity requires year and bucket").

## ADR-002 follow-ups required by this ADR

These amendments to ADR-002 fall out of the §2 invariant; capture them in the next ADR-002 revision:

1. Tighten `transfer_event.reason_kind` CHECK from `('scheduled','calendar','expansion_child','initial')` to `('rule','initial')`.
2. Update `transfer_event.reason_id` comment: "always `scheduled_transaction.id` when `reason_kind='rule'`; opaque per-account id when `reason_kind='initial'`."
3. Add `created_by_event_id INTEGER REFERENCES calendar_event(id)` to `scheduled_transaction` (NULL for rows authored in `scheduled.csv`; populated for rules synthesized by calendar-event expansion).
4. Add an `Income_LoanOrigination` to the standard chart of reserved accounts (or describe it as the conventional naming pattern for `buy_asset` synthesized origination rules).
5. Add the `account.subkind` CHECK constraints per §1 of this ADR.

## Open questions deferred

- Vocabulary of `tax_year_summary.bucket` is referenced by `prior_tax_activity` and `tax_accumulator`. Locked when ADR on tax computation lands.
- Whether `add_scheduled` should reference a prototype row by name (cleaner) or inline the full payload (self-contained). Currently allows both. May restrict to "prototype by name" in v2 if the inline form proves error-prone.
- Whether `life_transition.predecease` needs separate sub-transitions for survivor-benefit edge cases (e.g. deceased spouse hadn't claimed yet). Will refine when the first realistic scenario exercises it.
