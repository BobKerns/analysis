"""Locked vocabularies from ADR-003."""

from __future__ import annotations

import re
from typing import Literal, NamedTuple

from analysis.sim.types import INVESTMENT_ASSET_SUBKINDS

ACCOUNT_SUBKINDS: dict[str, set[str]] = {
    "asset": {
        "cash", "brokerage_taxable", "retirement", "hsa", "real_estate", "vehicle",
        "other_asset", "business",
    },
    "liability": {"mortgage", "heloc", "credit_card", "other_liability"},
    "income": {
        "salary", "ss_benefit", "pension", "interest", "dividend", "capital_gain",
        "rental", "other_income",
    },
    "expense": {
        "living", "healthcare", "housing", "interest_expense", "taxes_federal",
        "taxes_state", "taxes_property", "business", "other_expense",
    },
}

AMOUNT_RULE_KINDS: set[str] = {
    "fixed_amount", "percent_of_balance", "rmd",
    "ss_benefit", "tax_accumulator", "mortgage_payment", "on_death", "irmaa",
}

# Canonical scheduled.csv column order (ADR-003 §4.6). `series` and `amount` are
# each interpreted per amount_rule_kind; see SERIES_RULE_KINDS below.
SCHEDULED_CSV_HEADER: tuple[str, ...] = (
    "name", "from_account", "from_lot", "to_account", "to_lot", "cadence",
    "month_of_year", "amount_rule_kind", "interest_account", "person", "series",
    "rate", "claim_age", "amount", "base_date", "inflation_cadence",
    "tax_character", "tax_year", "start", "end", "priority", "ytd_amount", "ytd_preroll",
    "description", "meta_json", "params_json", "tags",
)

# Rules that read the `series` column, and what it names for each.
SERIES_RULE_KINDS: dict[str, str] = {
    "fixed_amount": "inflation series",
    "percent_of_balance": "rate series",
    "ss_benefit": "COLA series",
}

# tax_treatment -> withdrawal tax_character, for chain legs (ADR-003 §4.7): the
# character of a leg drawn from an account depends on which concrete account
# actually supplied the money, so this is looked up per-leg at runtime rather
# than authored on the scheduled_transaction row. 'taxable' is deliberately
# absent: `cash` withdrawals return already-taxed principal
# ('none'), but a taxable brokerage withdrawal splits into `return_of_capital`
# / `ltcg` legs based on the account's running cost basis (see
# `engine._post_withdrawal`) -- returning None here is how this function
# signals "not a flat lookup, go compute the split" rather than an outright
# unsupported case. Callers must special-case 'taxable' by subkind (see
# `withdrawal_tax_character`) rather than treat it as a flat lookup.
TAX_TREATMENT_TO_WITHDRAWAL_CHARACTER: dict[str, str] = {
    "tax_deferred": "tax_deferred_out",
    "tax_free": "tax_free",
    "none": "none",
}

# A sole proprietor's business bank account (#231): cash, but the place a
# Schedule C's receipts arrive and its expenses are paid from.
BUSINESS_SUBKIND = "business"

# Cash accounts: they earn a yield, are liquid, and hold cash-class money.
CASH_SUBKINDS: frozenset[str] = frozenset({"cash", BUSINESS_SUBKIND})

# Subkinds whose 'taxable' tax_treatment still withdraws tax-free (principal,
# already taxed when earned) -- i.e. cash-like accounts, not brokerage.
TAXABLE_SUBKINDS_WITH_NO_WITHDRAWAL_TAX: frozenset[str] = CASH_SUBKINDS


def withdrawal_tax_character(
    tax_treatment: str | None, subkind: str, to_tax_treatment: str | None = None,
) -> str | None:
    """The tax_character of a leg drawn from an account with these attributes
    into one whose tax treatment is ``to_tax_treatment``.

    Traditional retirement money moved into another tax-deferred lot is a
    rollover, not a distribution, so it is untaxed (#217); into a Roth lot it stays
    ``tax_deferred_out``, which is how a conversion is taxed.

    Returns ``None`` when the character can't be determined by a flat lookup
    (today: a `taxable` account that isn't cash-like, e.g. `brokerage_taxable`)
    -- the caller must instead split the leg by cost basis, see
    ``engine._post_withdrawal``.
    """
    if tax_treatment == "taxable":
        return "none" if subkind in TAXABLE_SUBKINDS_WITH_NO_WITHDRAWAL_TAX else None
    if tax_treatment == "tax_deferred" and to_tax_treatment == "tax_deferred" \
            and subkind == RETIREMENT_SUBKIND:
        return "none"
    return TAX_TREATMENT_TO_WITHDRAWAL_CHARACTER.get(tax_treatment or "none", "none")

CALENDAR_EVENT_KINDS: set[str] = {
    "set_rate", "set_amount", "retire_scheduled", "swap_aux_table",
    "one_off_transfer", "transfer_balance", "latent_factor",
}

# ADR-008 §3: a shared influence (an "inflation regime") that rows correlate
# with but that isn't itself a modeled value. Inert in the engine; only the
# ensemble's correlated draws read it.
LATENT_FACTOR_KIND = "latent_factor"

# ADR-006 §4a (#115): the year-by-year processes a varying set_rate row may
# carry around its drawn level. Blank is the held draw.
VARY_PROCESSES: set[str] = {"ar1"}

PROCESS_SHOCK_CLAMP = 5.0
"""A process path's log departure from its level is clamped to this, either
side (ADR-006 §4a): overflow protection for a mistyped shock size, never
reached by realistic inputs (a growth factor between 0.007 and 148)."""

MIN_PROCESS_TAIL_DEPENDENCE = 2.0
"""A t-copula tree holding a process row needs nu above this (ADR-006 §4a):
its yearly shocks are t with nu degrees of freedom, whose SD is finite only
for nu > 2."""

MIN_TAIL_DEPENDENCE = 1.0
"""The smallest ``vary_tail_dependence`` (nu) accepted (#68). nu = 1 is the
Cauchy copula, already far more tail dependence than any data supports;
below it the chi-square draw behind the shared scale underflows to 0 for
ordinary draws (its median is about 1e-6 at nu = 0.1, 0 at nu = 0.01)."""

CADENCES: set[str] = {"monthly", "biweekly", "quarterly", "annual", "one_off"}

# A sole proprietor's receipts into, and deductible spending out of, a
# `business` account (#231): netted per year into Schedule C.
BUSINESS_INCOME, BUSINESS_EXPENSE = "business_income", "business_expense"
BUSINESS_TAX_CHARACTERS: frozenset[str] = frozenset({BUSINESS_INCOME, BUSINESS_EXPENSE})

TAX_CHARACTERS: set[str] = {
    "ordinary", "ltcg", "stcg", "gains", "deferred", "ss_benefit", "tax_free",
    "tax_deferred_in", "tax_deferred_out", "return_of_capital", "property_tax", "none",
    # IRC §121 excluded portion of a home-sale gain: real gain, but not
    # taxed. Distinct from 'return_of_capital' (not gain at all) and from
    # 'ltcg' (gain that *is* taxed).
    "excluded_gain",
    # A chain step's basis characters (#199, ADR-003 §4.7): the sale of a
    # personal residence recovers its basis like return_of_capital, but basis
    # it can't recover is a personal loss, not a deductible capital loss.
    "home_sale",
    # Money spent on an asset (a remodel) that adds to its cost basis.
    "capital_improvement",
    # Earnings out of a Roth lot before the owner's 5-year clock has run (or
    # before 59½): ordinary income (#217). Its basis comes out tax_free.
    "roth_nonqualified",
    *BUSINESS_TAX_CHARACTERS,
}

# The characters of a chain step that recovers a sold lot's basis (#199): the
# step is sized by the lot's basis and uses it up. What it can't recover is a
# capital loss for 'return_of_capital' and nothing for 'home_sale'.
BASIS_TAX_CHARACTERS: frozenset[str] = frozenset({"return_of_capital", "home_sale"})

TAX_TREATMENTS: set[str] = {"taxable", "tax_deferred", "tax_free", "none"}

RETIREMENT_SUBKIND = "retirement"

# persons.csv roles of an actual person (ADR-003 §4.1), who needs a birthdate
# and may own a retirement account (#217). A blank role is a pseudo-person
# such as `joint`, or someone who only appears (an ancestor, an heir).
PERSON_ROLES: frozenset[str] = frozenset({"primary", "secondary"})

# A `retirement` account's lot subkinds (#217) and the tax treatment each
# gives its lot. One IRA is either Roth or traditional, but a 401k can hold
# both, so the distinction lives on the lot; the account's own tax_treatment
# stays blank.
RETIREMENT_LOT_SUBKINDS: dict[str, str] = {
    "traditional_ira": "tax_deferred",
    "roth_ira": "tax_free",
    "traditional_401k": "tax_deferred",
    "roth_401k": "tax_free",
}

# Lot subkinds an RMD may be drawn from: Roth IRAs never had RMDs, and Roth
# 401ks have none from 2024 (SECURE 2.0 §325).
RMD_LOT_SUBKINDS: frozenset[str] = frozenset({"traditional_ira", "traditional_401k"})

# transfer_event.reason_kind (schema.py CHECK), in a sensible display order:
# a rule firing, the run's opening balances, partial-first-year catch-up
# facts, a refill draw, or a month-end overflow sweep.
REASON_KINDS: tuple[str, ...] = ("rule", "initial", "ytd", "ytd_rule", "refill", "overflow")

type AssetClass = Literal[
    "stock_us",
    "stock_intl",
    "stock",
    "bond",
    "cd",
    "cash",
    "real_estate",
    "other",
]
"""Asset classes of allocations.csv (ADR-003 §4.1b), in display order."""

ASSET_CLASSES: tuple[AssetClass, ...] = AssetClass.__value__.__args__
"""Asset classes of allocations.csv (ADR-003 §4.1b), in display order. Closed like the
other registries; a new class is a one-line addition. `stock` is stock whose split``
between US and international is not known (a placeholder to refine, but still distinct
from bonds).
"""

ALLOCATIONS_CSV_HEADER: tuple[str, ...] = ("symbol", "as_of", "asset_class", "weight")
CASH_SYMBOL = "CASH"
# Symbols that mean an asset class without any allocations.csv row: `CASH` on cash lots,
# `CD` on certificates of deposit (no ticker; a CD is reinvested at maturity, so it
# stays in its lot and is only a classification here).
SYMBOL_ASSET_CLASS: dict[str, AssetClass] = {"CASH": "cash", "CD": "cd"}

# The class of a lot that no allocations.csv row describes, by account subkind
# (a `CASH` or `CD` symbol is that class whatever the account). Investment subkinds are absent:
# what they hold has to be said, and an unclassified lot shows up as such.
SUBKIND_ASSET_CLASS: dict[str, AssetClass] = {
    "cash": "cash", BUSINESS_SUBKIND: "cash", "real_estate": "real_estate", "vehicle": "other",
    "other_asset": "other",
}

# Canonical accounts.csv column order (ADR-003 §4.2).
ACCOUNTS_CSV_HEADER: tuple[str, ...] = (
    "name", "kind", "subkind", "owner", "tax_treatment", "series",
    "tax_character", "opened", "closed", "meta_json", "tags",
)

# A named chain's role, which is the subdirectory of chains/ its file lives in
# (ADR-003 §4.7): a source chain is drawn from (a rule's from_account, a lot's
# refill_chain), a destination chain is paid into (a rule's to_account, a lot's
# overflow_chain). Leaf chains, one lot each, play either part.
CHAIN_ROLES: tuple[str, ...] = ("source", "destination")


class GrowthCounterparty(NamedTuple):
    """A reserved account on the far side of accounts.csv-declared growth."""

    name: str
    kind: str
    subkind: str
    tax_treatment: str
    tax_character: str
    description: str


# accounts.csv `tax_character` -> the built-in account on the other side of the
# growth flow (ADR-003 §2.2). Reserved names; created on demand at load time so
# authors never wire up (or mis-wire) that side by hand. A negative rate series
# makes the flow run the other way (asset -> expense), which is what the
# `depreciation` entry is for.
GROWTH_COUNTERPARTY_ACCOUNTS: dict[str, GrowthCounterparty] = {
    "ordinary": GrowthCounterparty(
        "interest_income", "income", "interest", "taxable", "ordinary",
        "Built-in: taxable interest and dividends",
    ),
    "gains": GrowthCounterparty(
        "capital_gains", "income", "capital_gain", "taxable", "gains",
        "Built-in: unrealized gains in taxable accounts",
    ),
    "deferred": GrowthCounterparty(
        "deferred_gains", "income", "capital_gain", "tax_deferred", "deferred",
        "Built-in: unrealized gains in tax-deferred accounts",
    ),
    "tax_free": GrowthCounterparty(
        "tax_free_gains", "income", "capital_gain", "tax_free", "tax_free",
        "Built-in: unrealized gains in tax-free accounts (Roth, HSA)",
    ),
    "depreciation": GrowthCounterparty(
        "depreciation", "expense", "other_expense", "none", "none",
        "Built-in: depreciation of vehicles and other wasting assets",
    ),
}

DEFAULT_PRIORITIES: dict[str, int] = {
    "percent_of_balance": 100,
    "ss_benefit": 200,
    "fixed_amount": 200,
    "mortgage_payment": 300,
    "tax_accumulator": 400,
    "rmd": 500,
    "on_death": 900,
    # Reads a finished year's MAGI (two years back), so its order in a
    # month doesn't matter; the band of the premium legs it sits beside.
    "irmaa": 200,
}

# Aux file kind -> required columns (ADR-003 §4.4).
# CSV money columns are written in decimal dollars (e.g. ``1234.56``); the loader
# converts them to integer cents stored under the same column name in the
# payload JSON.
STATE_STANDARD_DEDUCTION = "state_standard_deduction"
"""The aux kind of a state's own standard deduction (#248)."""

IRMAA_TIERS = "irmaa_tiers"
"""The aux kind of the Medicare income-related surcharge tiers (#253)."""

IRMAA_SERIES_COLUMNS: tuple[str, ...] = ("floor_series", "part_b_series", "part_d_series")
"""The ``irmaa_tiers`` columns naming an optional inflation series that grows
a row's floor, Part B add-on and Part D add-on (#253)."""

AUX_KIND_COLUMNS: dict[str, tuple[str, ...]] = {
    "federal_brackets":
        ("effective_date", "filing_status", "bracket_index", "floor", "marginal_rate"),
    "state_brackets":
        ("effective_date", "filing_status", "bracket_index", "floor", "marginal_rate"),
    # Federal long-term capital gain rates: the same shape as federal_brackets,
    # but a band's floor is the taxable income (ordinary income plus the gain,
    # stacked on top) at which its rate starts. Optional: without it long-term
    # gains are taxed as ordinary income.
    "ltcg_brackets":
        ("effective_date", "filing_status", "bracket_index", "floor", "marginal_rate"),
    "standard_deduction": ("effective_date", "filing_status", "amount"),
    # The state's own standard deduction (#248), which is often far smaller
    # than the federal one. A state tax rule falls back to standard_deduction
    # when there is no state_standard_deduction__<state> for its brackets'
    # state, and the loader warns that it did.
    STATE_STANDARD_DEDUCTION: ("effective_date", "filing_status", "amount"),
    # Itemized deductions: a dated total, no filing_status split (unlike
    # standard_deduction). Optional; when present, the tax_accumulator rule uses
    # whichever is greater, itemized or standard. `amount` may be a literal
    # figure or an itemizations/<name>.csv rollup (`@name`), same as any other
    # aux money column. `state_deductions` defaults to `federal_deductions`'s
    # value when no `state_deductions` rows exist at all.
    "federal_deductions": ("effective_date", "amount"),
    "state_deductions": ("effective_date", "amount"),
    "ss_taxation_thresholds":
        ("effective_date", "filing_status", "tier1_floor", "tier2_floor"),
    "rmd_factors": ("effective_date", "age", "factor"),
    "rate": ("effective_date", "annual_rate"),
    "inflation": ("effective_date", "annual_rate"),
    "filing_status": ("effective_date", "filing_status"),
    "rmd_start_age": ("effective_date", "start_age"),
    # A security's price in decimal dollars (up to 6 places), one series per
    # symbol; stored in the payload as `price_micros`. Used only to derive a
    # lot's opening_balance from its quantity (ADR-005 §6), never monthly.
    "security_price": ("effective_date", "price"),
    # One row per (category, effective_date): a federal/state credit or
    # rebate schedule that appears and disappears on its own timeline (25C,
    # 25D, 30C all had different 2025/2026 sunset dates under OBBBA).
    # `category` free-text, e.g. "heat_pump", "ev_charger". `rate` is the
    # credit as a percent (`30%`); `cap` is the per-project dollar
    # cap, 0/blank for uncapped. A category/date combo with no matching row
    # means "no credit available" -- callers must not assume a fallback.
    "energy_credits": ("effective_date", "category", "rate", "cap"),
    # IRC §1211(b): how much of a year's net capital loss offsets ordinary
    # income, by filing status ($3,000; $1,500 married filing separately).
    # The rest carries forward (§1212(b)).
    "capital_loss_limit": ("effective_date", "filing_status", "amount"),
    # A period life table (#222): q, the chance of dying before the next
    # birthday, at each whole age from 0, by sex. Read only at load and when
    # an ensemble member draws a `life_table` lifespan, never monthly.
    "life_table": ("effective_date", "age", "q_female", "q_male"),
    # Medicare IRMAA (#253): for each filing status, the tiers of MAGI (more
    # than magi_floor) and each tier's monthly Part B and Part D add-ons.
    # Each figure grows past effective_date with its own optional inflation
    # series (IRMAA_SERIES_COLUMNS); blank holds it flat.
    IRMAA_TIERS: ("effective_date", "filing_status", "tier", "magi_floor",
                  "part_b", "part_d", *IRMAA_SERIES_COLUMNS),
}

# Aux kinds that are no longer read, with what replaced them: a file of one
# fails to load with this message rather than as an unknown kind.
RETIRED_AUX_KINDS: dict[str, str] = {
    "home_sale_exclusion": (
        "the home-sale exclusion is now the maximum of a chain step whose "
        "tax_character is excluded_gain (#199); delete the file"
    ),
}

# The aux kinds a swap_aux_table event may replace (ADR-003 §3.5). Security
# prices are read only at load, to value lots at the scenario start, so a
# swap of one during the run would change nothing.
SWAPPABLE_AUX_KINDS: frozenset[str] = (
    frozenset(AUX_KIND_COLUMNS) - {"security_price", "life_table"}
)

# Columns authored in a natural unit but stored as integer micros, by kind:
# {kind: {csv column: (payload key, unit)}} (#130). The unit is `price`
# (decimal dollars, positive, up to six places: sub-cent precision),
# `percent` (a rate, with its sign: `4.5%`) or `number` (a plain decimal).
AUX_MICROS_COLUMNS: dict[str, dict[str, tuple[str, str]]] = {
    "security_price": {"price": ("price_micros", "price")},
    "federal_brackets": {"marginal_rate": ("marginal_rate_micros", "percent")},
    "state_brackets": {"marginal_rate": ("marginal_rate_micros", "percent")},
    "ltcg_brackets": {"marginal_rate": ("marginal_rate_micros", "percent")},
    "rmd_factors": {"factor": ("factor_micros", "number")},
    "rate": {"annual_rate": ("annual_rate_micros", "percent")},
    "inflation": {"annual_rate": ("annual_rate_micros", "percent")},
    "energy_credits": {"rate": ("rate_micros", "percent")},
    "life_table": {"q_female": ("q_female_micros", "number"),
                   "q_male": ("q_male_micros", "number")},
}

# Column names the CSVs used before #130, when rates and factors were
# written in micros. A file still carrying one fails to load, naming the
# conversion script, rather than being read 10,000 times too large.
LEGACY_MICROS_COLUMNS: frozenset[str] = frozenset({
    "annual_rate_micros", "marginal_rate_micros", "rate_micros", "factor_micros",
})
UNITS_CONVERSION_HINT = (
    "rates and factors are now percents and plain numbers (#130); "
    "convert the data repo with scripts/convert_units_130.py"
)

# Per-aux-kind set of column names that hold dollar amounts in the CSV and must
# be converted to integer cents during load.
AUX_MONEY_COLUMNS: dict[str, frozenset[str]] = {
    "federal_brackets": frozenset({"floor"}),
    "ltcg_brackets": frozenset({"floor"}),
    "state_brackets": frozenset({"floor"}),
    "standard_deduction": frozenset({"amount"}),
    STATE_STANDARD_DEDUCTION: frozenset({"amount"}),
    "federal_deductions": frozenset({"amount"}),
    "state_deductions": frozenset({"amount"}),
    "ss_taxation_thresholds": frozenset({"tier1_floor", "tier2_floor"}),
    "energy_credits": frozenset({"cap"}),
    "capital_loss_limit": frozenset({"amount"}),
    IRMAA_TIERS: frozenset({"magi_floor", "part_b", "part_d"}),
}


SCENARIO_HEADER: tuple[str, ...] = (
    "name", "description", "start", "end", "seed",
    "parent_scenario", "created_by", "meta_json",
)

SCENARIO_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


CALENDAR_HEADER: tuple[str, ...] = (
    "date", "kind", "name", "scheduled_name", "account_1", "lot_1", "account_2", "lot_2",
    "series", "amount", "param_date", "tax_character", "params_json", "description",
    "tags",
    # Ensemble variation (ADR-006): a row with vary_distribution set has its own
    # amount/date (per vary_target) drawn per member instead of used literally.
    # vary_low/vary_high are `uniform`'s and `triangular`'s own bounds, and
    # also double as `truncated_normal`'s optional clip bounds -- each of
    # vary_median/vary_sd/vary_low/vary_high may be inferred from the others
    # when omitted (see distributions.resolve_truncated_normal_bounds), so
    # truncated_normal is never actually unbounded. vary_skew/vary_tailweight
    # shape a truncated_normal; vary_mode is a triangular's peak (#31).
    "vary_target", "vary_distribution", "vary_low", "vary_high", "vary_median",
    "vary_sd", "vary_skew", "vary_tailweight", "vary_mode", "vary_ramp_months",
    # #165: a `discrete` row's weighted outcomes, '30%: 0; 25%: 1000..6000'.
    "vary_outcomes",
    # ADR-006 §4a (#115): a set_rate row's year-by-year path around its drawn
    # level -- the process (`ar1`), its persistence phi, and the yearly shock
    # size in micros.
    "vary_process", "vary_persistence", "vary_innovation_sd",
    # ADR-008: this row's (or latent factor's) parent in a correlation tree,
    # and the correlation rho between their normal scores; on a tree's root,
    # the Student-t copula's degrees of freedom for the whole tree (#68).
    "vary_correlate_with", "vary_correlation", "vary_tail_dependence",
)

# The columns that identify a calendar.csv row when a scenario's rows are
# merged over its parent's: rows alike in all of them are one event. `name`
# is deliberately not among them (ADR-006); a latent factor (ADR-008) is
# keyed by its name alone.
CALENDAR_KEY_COLUMNS: tuple[str, ...] = (
    "date", "kind", "scheduled_name", "account_1", "lot_1", "account_2", "lot_2",
    "series", "amount",
)

# ADR-006: which of a varying calendar_event row's own columns is drawn.
VARY_TARGETS: set[str] = {"amount", "date"}

# ADR-006: closed like every other registry; a new family (lognormal, ...)
# is a one-line addition when a concrete case needs one. `triangular` (#31)
# is the three-point estimate -- "most likely X, no earlier/lower than A,
# possibly as late/high as B" -- and a skewed choice for a date. `discrete`
# (#165) is a few weighted outcomes, each a value or a range: a bet whose
# outcomes are separate cases, an exact zero among them.
ENSEMBLE_DISTRIBUTIONS: set[str] = {"uniform", "truncated_normal", "triangular", "discrete"}

# #31: `vary_mean` was renamed to `vary_median` (the truncated distribution's
# median, so the base case is exactly the authored value). Its meaning
# changed for asymmetric bounds, so the old name is rejected, not aliased.
RENAMED_CALENDAR_COLUMNS: dict[str, str] = {"vary_mean": "vary_median"}

# ADR-006 §9: `sim ensemble create`'s default scenario.meta_json.ensemble_size
# when --size is omitted, so a recipe always has a member count `sim run`/
# `sim all` can use without requiring --ensemble-size on every invocation.
# See docs/guides/ensembles.md for what a good size actually depends on
# (mainly how many dimensions -- varying rows -- the recipe has).
DEFAULT_ENSEMBLE_SIZE = 30

# ADR-007 §5: ensemble_manifest.mode / batch_manifest.json "mode" -- a random
# Monte Carlo batch (ADR-006) or a deterministic one-at-a-time sensitivity
# batch.
ENSEMBLE_MODES: tuple[str, ...] = ("monte_carlo", "sensitivity")

# ADR-007 §3: `sim run --sensitivity`'s default perturbation quantiles --
# "plausibly low / plausibly high", not the authored extremes.
DEFAULT_SENSITIVITY_QUANTILES: tuple[float, ...] = (0.1, 0.9)

# ADR-006: calendar_event kinds whose `amount` column has a real meaning to
# vary (a rate in `set_rate`, a new base amount in `set_amount`, a transfer
# amount in `one_off_transfer`). The other kinds have no amount column a
# draw could affect.
AMOUNT_VARYING_KINDS: set[str] = {"set_rate", "set_amount", "one_off_transfer"}

# What a calendar_event row's `amount` means, by kind: an annual rate in
# micros (millionths, so 30000 is 3%) for `set_rate` -- inflation series
# included -- and cents for the kinds that move or set money.
RATE_MICROS_CALENDAR_KINDS: frozenset[str] = frozenset({"set_rate"})


def format_calendar_amount(
    kind: str, amount: float | None, *, cents: bool = True, rate_places: int | None = None,
) -> str:
    """A calendar_event ``amount`` in its own unit (issue #24): a ``set_rate``
    as a percent, anything else as dollars; ``-`` when absent.

    A rate is exact by default (``3%``, ``4.5%``, ``3.125%``: the authored
    value), or fixed to ``rate_places`` decimals for a column of computed
    values. Dollars show cents unless ``cents=False``."""
    if amount is None or amount != amount:  # None or NaN
        return "-"
    if kind in RATE_MICROS_CALENDAR_KINDS:
        if rate_places is not None:
            return f"{amount / 10_000:.{rate_places}f}%"
        percent = f"{amount / 10_000:.4f}".rstrip("0").rstrip(".")
        return f"{percent}%"
    sign = "-" if amount < 0 else ""
    dollars = abs(amount) / 100
    return f"{sign}${dollars:,.2f}" if cents else f"{sign}${dollars:,.0f}"

# ADR-006 / #31: the vary_* parameter columns each distribution actually
# reads; any other one left non-blank alongside it is a parameter the
# eventual draw silently ignores, so it's rejected. `truncated_normal`'s
# median/sd/low/high may each be inferred from the others when blank, so
# none is individually "required" here -- see DISTRIBUTION_REQUIRED_PARAMS
# and distributions.resolve_truncated_normal_bounds.
DISTRIBUTION_PARAMS: dict[str, frozenset[str]] = {
    "uniform": frozenset({"vary_low", "vary_high"}),
    "truncated_normal": frozenset({
        "vary_median", "vary_sd", "vary_low", "vary_high", "vary_skew", "vary_tailweight",
    }),
    "triangular": frozenset({"vary_low", "vary_high", "vary_mode"}),
    "discrete": frozenset({"vary_outcomes"}),
}

# ADR-006: the subset of DISTRIBUTION_PARAMS[dist] that must actually be
# given a value, checked by a plain "is this column blank" scan.
# `truncated_normal` isn't here: its four params can satisfy each other in
# more than one combination (mean+sd; low+high; mean+one bound; ...), which
# needs distributions.resolve_truncated_normal_bounds, not a fixed column list.
DISTRIBUTION_REQUIRED_PARAMS: dict[str, tuple[str, ...]] = {
    "uniform": ("vary_low", "vary_high"),
    "triangular": ("vary_low", "vary_high"),
    "discrete": ("vary_outcomes",),
}


# ADR-003 §4.1 and §4.9.
PERSONS_CSV_HEADER: tuple[str, ...] = (
    "id", "display", "birthdate", "sex", "role", "meta_json",
)
# #222: persons.csv `sex`, which picks a life table's column. Optional (an
# older persons.csv has no such column), but required of a person with a
# `life_table` lifespan.
PERSON_SEXES: tuple[str, ...] = ("f", "m")
LIFE_TABLE_Q_COLUMNS: dict[str, str] = {"f": "q_female", "m": "q_male"}
LIFESPAN_CSV_HEADER: tuple[str, ...] = (
    "person", "age_at_death", "vary_distribution", "vary_low", "vary_high", "vary_mode",
    "vary_correlate_with", "vary_correlation",
)

# #221: a lifespan varies by age, uniform or triangular; #222 adds
# `life_table`, drawn from aux life_table.csv around a median of
# age_at_death, with no vary_low/high/mode. Its node in correlation trees, sensitivity sweeps and
# ensemble_draw is `lifespan:<person>`, a name no calendar row may take, and
# its draws are recorded with target `age_at_death`.
LIFESPAN_DISTRIBUTIONS: tuple[str, ...] = ("uniform", "triangular", "life_table")
LIFE_TABLE_DISTRIBUTION = "life_table"
LIFESPAN_NODE_PREFIX = "lifespan:"
LIFESPAN_TARGET = "age_at_death"

# #221: an age_at_death (or bound) beyond this is a typo, not a plan.
MAX_AGE_AT_DEATH = 130.0

ACCOUNT_LOTS_CSV_HEADER: tuple[str, ...] = (
    "account_name", "lot", "subkind", "symbol", "acquired", "quantity", "series",
    "opening_balance", "opening_cost_basis", "prior_year_end_balance",
    "minimum_balance", "maximum_balance", "refill_chain", "refill_lot",
    "refill_target", "refill_amount", "overflow_chain", "overflow_lot",
)

# The per-lot balance-management columns of account_lots.csv (ADR-003 §4.3).
CASH_MANAGEMENT_COLUMNS: tuple[str, ...] = (
    "minimum_balance", "maximum_balance", "refill_chain", "refill_lot",
    "refill_target", "refill_amount", "overflow_chain", "overflow_lot",
)



# Tag tokens are `<role>:<name>` with lowercase [a-z0-9_] on each side.
# Role is intentionally an OPEN registry to support ad-hoc exploration.
TAG_TOKEN_RE = re.compile(r"^[a-z0-9_]+:[a-z0-9_]+$")


def parse_tags(raw: str) -> list[tuple[str, str]]:
    """Split a whitespace-separated ``tags`` cell into ``(role, name)`` pairs.

    Raises ``ValueError`` on any malformed token. Order is preserved; callers
    de-dupe as needed.
    """
    out: list[tuple[str, str]] = []
    for tok in (raw or "").split():
        if not TAG_TOKEN_RE.match(tok):
            raise ValueError(f"invalid tag {tok!r}; expected '<role>:<name>'")
        role, name = tok.split(":", 1)
        out.append((role, name))
    return out


def format_tags(tags: list[tuple[str, str]]) -> str:
    """Inverse of ``parse_tags``: render a sorted, de-duplicated tag string."""
    return " ".join(f"{r}:{n}" for r, n in sorted(set(tags)))


DEFAULT_LOT = "default"


def leaf_chain_name(account: str, lot: str, subkind: str = "") -> str:
    """Name of the leaf chain for one lot (ADR-005 §3): the bare account name
    for the default lot of a non-investment account, ``account:lot`` otherwise.
    Investment accounts are always spelled ``account:lot``.
    """
    if lot == DEFAULT_LOT and subkind not in INVESTMENT_ASSET_SUBKINDS:
        return account
    return f"{account}:{lot}"
