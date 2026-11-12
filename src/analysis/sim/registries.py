"""Locked vocabularies from ADR-003."""

from __future__ import annotations

import re
from typing import Literal, NamedTuple

from analysis.sim import states
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
        "taxes_state", "taxes_property", "business", "gift", "other_expense",
    },
}

AMOUNT_RULE_KINDS: set[str] = {
    "fixed_amount", "percent_of_balance", "rmd",
    "ss_benefit", "mortgage_payment", "on_death", "fill_bracket", "irmaa",
}

# The rule kind income tax had before jurisdiction files (#507): a scheduled
# row of it is refused at load, naming the migration script.
RETIRED_TAX_RULE_KIND = "tax_accumulator"


# Rule kinds whose catch-up for the months before scenario.start can be
# replayed from the rule itself (`ytd_preroll`, ADR-003 §4.10); any kind
# takes an observed `ytd_amount`.
YTD_PREROLL_KINDS: frozenset[str] = frozenset({"fixed_amount", "mortgage_payment"})

# Canonical scheduled.csv column order (ADR-003 §4.6). `series` and `amount` are
# each interpreted per amount_rule_kind; see SERIES_RULE_KINDS below.
SCHEDULED_CSV_HEADER: tuple[str, ...] = (
    "name", "from_account", "from_lot", "to_account", "to_lot", "cadence",
    "month_of_year", "amount_rule_kind", "interest_account", "person", "series",
    "rate", "claim_age", "amount", "base_date", "inflation_cadence",
    "tax_character", "tax_year", "start", "end", "priority", "ytd_amount", "ytd_preroll",
    "description", "meta_json", "params_json", "tags",
)

# A scenario's income tax jurisdictions (#506, #507, ADR-003 §4.15): one file
# each, taxes/<jurisdiction>.csv, of one row. The file name is the
# jurisdiction's, which prefixes its tax_year_summary buckets. Each says which
# calculator computes its tax, the jurisdictions it builds on, and how it is
# paid: from `source_chain` into the `destination` expense account, on its
# `schedule`.
TAXES_DIR = "taxes"
TAX_CSV_HEADER: tuple[str, ...] = (
    "kind", "upstream", "source_chain", "destination", "schedule", "start", "end",
    "params_json", "description",
)
TAX_DEFAULT_KINDS: dict[str, str] = {
    "federal": "federal", "state": "state_tables", "local": "local_bands"}
"""The built-in calculator each jurisdiction's file starts with (`sim tax
add`); a household's own kind (``taxes/*.py``, #510) says which
jurisdiction it computes."""
TAX_JURISDICTIONS: tuple[str, ...] = ("federal", "state", "local")
"""The jurisdictions, in the order they settle: a state's return builds on
the federal one, a local one on the state's (#508)."""
TAX_PAID_WITH: dict[str, str] = {"local": "state"}
"""A jurisdiction paid with another's (#508): a blank ``source_chain`` or
``destination`` in its file takes that one's."""
RESIDENCE_FILE = "residence.csv"
RESIDENCE_CSV_HEADER: tuple[str, ...] = ("effective_date", "state", "county", "description")
"""Where the household lives (#509, ADR-003 §4.16): from each row's month
on, a state (a postal code) and optionally a county, the series of its
``state_local_tax`` table. A tax year is filed where the household lives on
December 31. Rows merge by date over the lineage."""
TAX_SCHEDULES: tuple[str, ...] = ("april",)
"""``april``: each April, the tax still due for the year before."""
MONTH_PHASES: tuple[str, ...] = ("rules", "tax_settlement", "fill_bracket")
"""The order a month posts in, after its strategy hooks, each phase whole
before the next (#507). The scheduled rules come first, in priority order
among themselves. The tax settlement (each April, the year before's tax)
follows every rule, so no rule's order is tied to it. ``fill_bracket``'s
conversions come last: an RMD can't be converted, so it must come out first,
and the conversion's target needs the year's whole income."""

# Canonical year_to_date.csv column order (ADR-003 §4.10).
YEAR_TO_DATE_CSV_HEADER: tuple[str, ...] = (
    "name", "from_account", "from_lot", "to_account", "to_lot", "amount",
    "tax_character", "year", "description",
)

# Canonical guard_rails.csv column order (#305, ADR-003 §4.11), and the
# values its enumerated columns take. A rail is read only by reports.
GUARD_RAILS_CSV_HEADER: tuple[str, ...] = (
    "name", "measure", "select", "portfolio", "side", "threshold", "basis",
    "sense", "start", "end", "description",
)
GUARD_RAIL_MEASURES: tuple[str, ...] = ("spending", "withdrawals")
GUARD_RAIL_SIDES: tuple[str, ...] = ("above", "below")
GUARD_RAIL_BASES: tuple[str, ...] = ("nominal", "cpi_constant", "portfolio", "initial_portfolio")
GUARD_RAIL_SENSES: tuple[str, ...] = ("negative", "positive")

# Rules that read the `series` column, and what it names for each.
SERIES_RULE_KINDS: dict[str, str] = {
    "fixed_amount": "inflation series",
    "percent_of_balance": "rate series",
    "ss_benefit": "COLA series",
    "fill_bracket": "inflation series",
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
    "one_off_transfer", "transfer_balance", "latent_factor", "date_marker",
}

# ADR-008 §3: a shared influence (an "inflation regime") that rows correlate
# with but that isn't itself a modeled value. Inert in the engine; only the
# ensemble's correlated draws read it.
LATENT_FACTOR_KIND = "latent_factor"

# #281: a named date other rows' dates can be written relative to
# (`NAME+2y`), drawn like any date row. Inert in the engine.
DATE_MARKER_KIND = "date_marker"

# The calendar_event kinds the engine skips: they hold something the ensemble
# step or the loader reads, not an event.
INERT_CALENDAR_KINDS: frozenset[str] = frozenset({LATENT_FACTOR_KIND, DATE_MARKER_KIND})

# #281: the calendar.csv columns a date may be written relative to a
# date_marker in, each with where a bare month lands there (None: its end).
RELATIVE_DATE_COLUMNS: dict[str, Literal["low", "high", "point"] | None] = {
    "date": None, "vary_low": "low", "vary_high": "high",
    "vary_median": "point", "vary_mode": "point",
    # #291: the date before which a return row finds what to resume.
    "vary_resume": None,
}

# ADR-006 §4a (#115): the year-by-year processes a varying set_rate row may
# carry around its drawn level. Blank is the held draw.
VARY_PROCESSES: set[str] = {"ar1"}

# #280: where a process path starts. Blank is a stationary draw around the
# level; `current` starts at the rate in effect just before the row's date
# and reverts to the level at the row's persistence.
VARY_PROCESS_STARTS: set[str] = {"current"}

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
    # A stock fund's dividends, paid and reinvested in a taxable lot (#468):
    # taxed each year, at the long-term gains rates (IRC §1(h)(11)).
    "qualified_dividend",
    # Municipal bond interest (#265): out of AGI, in provisional income and MAGI.
    "tax_exempt",
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
# #256: scenario meta_json "roth_survivor", how a surviving spouse takes the
# decedent's Roth IRAs: as their own (the earlier of the two five-year clocks,
# the survivor's 59½) or as a beneficiary (the decedent's clock, no age test).
ROTH_SURVIVOR_ELECTIONS: tuple[str, ...] = ("own", "beneficiary")
RMD_LOT_SUBKINDS: frozenset[str] = frozenset({"traditional_ira", "traditional_401k"})

# transfer_event.reason_kind (schema.py CHECK), in a sensible display order:
# a rule firing, the run's opening balances, partial-first-year catch-up
# facts, a refill draw, a month-end overflow sweep, or a strategy's year-end
# move (#421; reason_id is its strategy_decision row).
REASON_KINDS: tuple[str, ...] = (
    "rule", "initial", "ytd", "ytd_rule", "refill", "overflow", "strategy", "tax",
)

type AssetClass = Literal[
    "stock_us",
    "stock_intl",
    "stock",
    "bond",
    "muni",
    "cd",
    "cash",
    "real_estate",
    "other",
]
"""Asset classes of allocations.csv (ADR-003 §4.1b), in display order."""

BASE_ASSET_CLASSES: tuple[AssetClass, ...] = AssetClass.__value__.__args__
"""The asset classes other than one state's municipal bonds, in display order."""

# Municipal bonds (#265): `muni` is a fund of many states' bonds, `muni_<ST>`
# one state's (a postal code, `muni_CA`). Their interest is federally exempt;
# a state taxes every state's but its own.
MUNI_CLASS = "muni"
MUNI_STATE_CLASSES: tuple[str, ...] = tuple(f"{MUNI_CLASS}_{s.code}" for s in states.STATES)

ASSET_CLASSES: tuple[str, ...] = tuple(
    c for base in BASE_ASSET_CLASSES
    for c in ((base, *MUNI_STATE_CLASSES) if base == MUNI_CLASS else (base,)))
"""Asset classes of allocations.csv (ADR-003 §4.1b), in display order. Closed like the
other registries; a new class is a one-line addition. `stock` is stock whose split``
between US and international is not known (a placeholder to refine, but still distinct
from bonds). Each state's municipal bonds follow `muni`.
"""


def muni_state(asset_class: str) -> str | None:
    """The state whose bonds ``asset_class`` holds (#265): ``""`` for
    ``muni``, a fund of many states', and None for a class that isn't
    municipal bonds."""
    if asset_class == MUNI_CLASS:
        return ""
    return asset_class.removeprefix(f"{MUNI_CLASS}_") if asset_class in MUNI_STATE_CLASSES else None


def asset_class_list() -> str:
    """The asset classes for a message or help text, the per-state
    municipal bond classes as one pattern."""
    return ", ".join(f"{MUNI_CLASS}_<ST>" if c == MUNI_STATE_CLASSES[0] else c
                     for c in ASSET_CLASSES if c not in MUNI_STATE_CLASSES[1:])

ALLOCATIONS_CSV_HEADER: tuple[str, ...] = ("symbol", "as_of", "asset_class", "weight")

# Per-scenario strategies and targets.csv (#326, ADR-003 §4.12, §4.13). A
# strategy is one file, strategies/<name>.csv, of one row (#408): the file
# name is the strategy's name.
STRATEGIES_DIR = "strategies"
STRATEGY_CSV_HEADER: tuple[str, ...] = (
    "kind", "chain", "rules", "trigger", "start", "end", "enabled", "params_json", "description",
)
# #441: a strategy's `trigger` names guard rails, each `<rail>:<margin kind>`.
TRIGGER_MARGIN_KINDS: tuple[str, ...] = ("annual", "cumulative")
TARGETS_CSV_HEADER: tuple[str, ...] = ("target_set", "as_of", "asset_class", "weight")
# #408: strategy_vary/<strategy>.csv, one file per strategy, one row per
# parameter an ensemble varies (ADR-003 §4.14). Bounds are in the parameter's
# own unit, as params_json writes it. Its node in correlation trees,
# sensitivity sweeps and ensemble_draw is `strategy:<strategy>.<param>`, a
# name no calendar row may take; the draw's target is the parameter's name.
STRATEGY_VARY_DIR = "strategy_vary"
STRATEGY_VARY_CSV_HEADER: tuple[str, ...] = (
    "param", "vary_distribution", "vary_low", "vary_high", "vary_mode", "vary_median", "vary_sd",
    "vary_skew", "vary_tailweight", "vary_outcomes", "vary_correlate_with", "vary_correlation",
)
STRATEGY_NODE_PREFIX = "strategy:"
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

# A taxable lot's payout (#468, ADR-005 §5): the part of its total return a
# fund pays out each year (dividends, interest), taxed that year and
# reinvested, so it adds to the lot's basis. Each asset class pays from the
# rate series `<class>_yield`; cash and CDs, which have no price to move, pay
# all of their rate. Stocks' payouts are qualified dividends, the rest
# ordinary income. The built-in income accounts the payouts come from are
# not growth counterparties: money from them is a purchase.
YIELD_SUFFIX = "_yield"
QUALIFIED_DIVIDEND = "qualified_dividend"
NONQUALIFIED_DIVIDEND = "nonqualified_dividend"
QUALIFIED_DIVIDEND_CLASSES: frozenset[str] = frozenset({"stock", "stock_us", "stock_intl"})
ALL_PAYOUT_CLASSES: frozenset[str] = frozenset({"cash", "cd"})
PAYOUT_ACCOUNTS: dict[str, GrowthCounterparty] = {
    QUALIFIED_DIVIDEND: GrowthCounterparty(
        "dividends", "income", "dividend", "taxable", QUALIFIED_DIVIDEND,
        "Built-in: qualified dividends paid and reinvested in taxable lots",
    ),
    "ordinary": GrowthCounterparty(
        "investment_interest", "income", "interest", "taxable", "ordinary",
        "Built-in: interest and other dividends paid and reinvested in taxable lots",
    ),
    # Short-term gain distributions (#547): ordinary income, but a dividend,
    # not interest, for a state that tells them apart.
    NONQUALIFIED_DIVIDEND: GrowthCounterparty(
        "ordinary_dividends", "income", "dividend", "taxable", "ordinary",
        "Built-in: non-qualified dividends (short-term gain distributions) paid and reinvested in taxable lots",
    ),
    "ltcg": GrowthCounterparty(
        "gain_distributions", "income", "dividend", "taxable", "ltcg",
        "Built-in: long-term capital-gain distributions paid and reinvested in taxable lots",
    ),
}

# A fund's capital-gain distributions (#547), beside its dividends: the rate
# series `<series>_cg_yield` pays long-term gains, taxed as such and netted
# with the year's other gains, and `<series>_stcg_yield` pays short-term
# gains, which a 1099-DIV counts as ordinary (non-qualified) dividends. Unlike
# `_yield`, no class pays all of its rate as gains.
CG_YIELD_SUFFIX = "_cg_yield"
STCG_YIELD_SUFFIX = "_stcg_yield"
PAYOUT_SUFFIXES: tuple[str, ...] = (YIELD_SUFFIX, CG_YIELD_SUFFIX, STCG_YIELD_SUFFIX)
CG_PAYOUT_CHARACTERS: dict[str, str] = {CG_YIELD_SUFFIX: "ltcg", STCG_YIELD_SUFFIX: "ordinary"}

# An untaxed lot's payouts (#547), when it tracks shares: reinvested in the
# lot and untaxed, they buy shares as a statement shows, but like growth they
# add no basis (a Roth's basis stays its contributions) and count as
# unrealized growth. Keyed by the lot's growth character.
SHELTERED_PAYOUT_ACCOUNTS: dict[str, GrowthCounterparty] = {
    "deferred": GrowthCounterparty(
        "deferred_payouts", "income", "capital_gain", "tax_deferred", "deferred",
        "Built-in: dividends and distributions reinvested in tax-deferred lots",
    ),
    "tax_free": GrowthCounterparty(
        "tax_free_payouts", "income", "capital_gain", "tax_free", "tax_free",
        "Built-in: dividends and distributions reinvested in tax-free lots (Roth, HSA)",
    ),
}


# An HSA lot's payouts (#497): untaxed federally, as a sheltered lot's are
# (the leg keeps the lot's growth character), but a state that doesn't
# recognize HSAs (California, `state_hsa`) taxes them the year they're paid,
# as it would a brokerage account's. So unlike a sheltered payout they buy
# into the lot, adding to the basis that state taxes a withdrawal over, and
# an HSA lot gets them whether or not it tracks shares. Dividends, interest
# and short-term distributions are ordinary income to the state; long-term
# distributions are gain. Keyed by that.
HSA_PAYOUT_ACCOUNTS: dict[str, GrowthCounterparty] = {
    "ordinary": GrowthCounterparty(
        "hsa_dividends", "income", "dividend", "tax_free", "tax_free",
        "Built-in: dividends and interest reinvested in HSA lots",
    ),
    "ltcg": GrowthCounterparty(
        "hsa_gain_distributions", "income", "dividend", "tax_free", "tax_free",
        "Built-in: long-term gain distributions reinvested in HSA lots",
    ),
}


# Municipal bond interest (#265): out of federal AGI, but counted in
# provisional income (Social Security) and in IRMAA's MAGI. A state adds it
# back unless the bonds are its own, so each state's bonds have their own
# payer. Keyed by asset class.
TAX_EXEMPT = "tax_exempt"
MUNI_INTEREST = "muni_interest"
MUNI_PAYOUT_ACCOUNTS: dict[str, GrowthCounterparty] = {
    MUNI_CLASS: GrowthCounterparty(
        MUNI_INTEREST, "income", "interest", "tax_free", TAX_EXEMPT,
        "Built-in: tax-exempt interest from many states' municipal bonds, reinvested",
    ),
    **{c: GrowthCounterparty(
        f"{MUNI_INTEREST}__{c.removeprefix(MUNI_CLASS + '_')}", "income", "interest", "tax_free", TAX_EXEMPT,
        f"Built-in: tax-exempt interest from {c.removeprefix(MUNI_CLASS + '_')} municipal bonds, reinvested",
    ) for c in MUNI_STATE_CLASSES},
}
MUNI_PAYER_STATE: dict[str, str] = {a.name: muni_state(c) or "" for c, a in MUNI_PAYOUT_ACCOUNTS.items()}
"""Each municipal bond interest account's issuing state, ``""`` for many."""


def payout_account(payer: str) -> GrowthCounterparty:
    """A taxable lot's payout account: a ``PAYOUT_ACCOUNTS`` key, or a
    municipal bond class (#265)."""
    return MUNI_PAYOUT_ACCOUNTS.get(payer) or PAYOUT_ACCOUNTS[payer]


def hsa_payout_account(payer: str) -> GrowthCounterparty:
    """The HSA payout account for a payout a taxable lot would get from
    ``PAYOUT_ACCOUNTS[payer]`` (#497)."""
    return HSA_PAYOUT_ACCOUNTS["ltcg" if payer == "ltcg" else "ordinary"]


def yield_series(series: str, suffix: str = YIELD_SUFFIX) -> str:
    """The rate series a lot growing at ``series`` pays out from (#468):
    dividends and interest, or with ``suffix`` gain distributions (#547)."""
    return series + suffix


def builtin_payout_accounts() -> tuple[GrowthCounterparty, ...]:
    """Every built-in account a payout comes from, taxed or not."""
    return (*PAYOUT_ACCOUNTS.values(), *MUNI_PAYOUT_ACCOUNTS.values(),
            *SHELTERED_PAYOUT_ACCOUNTS.values(), *HSA_PAYOUT_ACCOUNTS.values())


def payout_character(asset_class: str) -> str:
    """How an asset class's payout is taxed (#468): for municipal bonds
    (#265), the class itself, since each state's has its own payer."""
    if muni_state(asset_class) is not None:
        return asset_class
    return QUALIFIED_DIVIDEND if asset_class in QUALIFIED_DIVIDEND_CLASSES else "ordinary"


DEFAULT_PRIORITIES: dict[str, int] = {
    "percent_of_balance": 100,
    "ss_benefit": 200,
    "fixed_amount": 200,
    "mortgage_payment": 300,
    "rmd": 500,
    "on_death": 900,
    # Reads a finished year's MAGI (two years back), so its order in a
    # month doesn't matter; the band of the premium legs it sits beside.
    "irmaa": 200,
}
"""Kinds with no entry here have no priority: a ``fill_bracket`` always runs
after every other rule of its month (#252)."""

# Aux file kind -> required columns (ADR-003 §4.4).
# CSV money columns are written in decimal dollars (e.g. ``1234.56``); the loader
# converts them to integer cents stored under the same column name in the
# payload JSON.
STATE_STANDARD_DEDUCTION = "state_standard_deduction"
"""The aux kind of a state's own standard deduction (#248)."""

STATE_SURCHARGE = "state_surcharge"
"""The aux kind of a state's surcharge on high taxable income (#490), in the
shape of its brackets, whose rates add to the brackets' (California's 1%
over $1 million)."""

STATE_LOCAL_TAX = "state_local_tax"
"""The aux kind of a local income tax (#487): a county's or city's bands on
the state's taxable income, in the shape of its brackets (Maryland's county
rate, New York City's brackets). Keyed by state like the brackets, since a
household lives in one locality. The local jurisdiction reads it
(``taxes/local.csv``, ``local_bands``, #508), paid with the state's tax,
as most are filed on the state return, and reported apart from it."""

STATE_HSA = "state_hsa"
"""The aux kind of how a state treats a health savings account (#490)."""

STATE_GAINS_TAX = "state_gains_tax"
"""The aux kind of a state's tax on long-term capital gains apart from its
income tax (#489), in the shape of its brackets: Washington's 7%, and 9.9%
over $1 million. Its base is the federal net long-term gain (Schedule D
line 15) less a home's, after ``state_gains_deduction``."""

STATE_GAINS_DEDUCTION = "state_gains_deduction"
"""The aux kind of the standard deduction of a state's gains tax (#489),
one figure per filing status like ``state_standard_deduction``."""

STATE_HSA_TREATMENTS: tuple[str, ...] = ("federal", "taxable")
"""A ``state_hsa`` row's ``treatment``: ``federal`` follows the federal
rules (the default, without a table); ``taxable`` is California's, which
doesn't recognize an HSA: contributions aren't deducted, its growth is
taxed each year, so what comes out was taxed already."""

STATE_RETIREMENT_EXCLUSION = "state_retirement_exclusion"
"""The aux kind of a state's retirement income exclusions (#486), keyed by
state like its brackets."""

STATE_EXCLUSION_INCOME: tuple[str, ...] = (
    "pension", "retirement_account", "interest", "dividend", "capital_gain", "other",
)
"""The kinds of income a ``state_retirement_exclusion`` row may cover (#486),
in the order a row that covers several uses them up: ordinary income from a
``pension``, ``interest`` or ``dividend`` income account (and, for
``dividend``, ``qualified_dividend`` legs); a
``retirement_account`` distribution (``tax_deferred_out``, ``roth_nonqualified``);
a ``capital_gain`` (``stcg``/``ltcg``); and ``other``, the rest of the state's
taxable income. ``all`` in a row stands for every one of them."""

STATE_ADJUSTMENTS = "state_adjustments"
"""The aux kind of a state's own deductions and credits (#511, #488), keyed
by state like its brackets: each row a named line of the return, computed
from tagged spending or income, the filers, or the federal return."""

STATE_ADJUSTMENT_STEPS: tuple[str, ...] = ("subtraction", "deduction", "credit")
"""Where a ``state_adjustments`` row's line comes off: ``subtraction`` from
income before state AGI, ``deduction`` after it (beside the standard or
itemized deduction), ``credit`` against the tax (never more than the tax)."""

STATE_ADJUSTMENT_BASES: tuple[str, ...] = (
    "spending", "income", "filers", "federal_tax", "federal_deduction", "gains_tax",
)
"""What a ``state_adjustments`` row's line is figured from: the year's
``spending`` carrying its tag (rent, premiums), its ordinary ``income``
carrying the tag (a public pension), the ``filers`` (an exemption of
``amount`` each), or the federal return's ``tax`` (a deduction for federal
tax paid) or ``deduction`` (Utah's taxpayer credit), or the state's own
``gains_tax`` (#489; Washington credits it against its income tax)."""

STATE_LTCG_BRACKETS = "state_ltcg_brackets"
"""A state's own rates on long-term gain (#485), stacked on top of its
taxable income as ``ltcg_brackets`` is federally: a lower rate (Montana), a
rate cap (Hawaii) or a rate of its own (Massachusetts)."""

STATE_STCG_BRACKETS = "state_stcg_brackets"
"""A state's own rates on net short-term gain (#485), stacked between its
other ordinary income and its long-term gain (Massachusetts' 8.5%)."""

STATE_LTCG_EXCLUSION = "state_ltcg_exclusion"
"""The part of a net long-term gain a state leaves out of its income (#485):
a percent, at least ``min_exclusion`` and at most ``max_exclusion``."""

STATE_SOCIAL_SECURITY = "state_social_security"
"""The aux kind of how a state taxes Social Security (#484). Without one a
state taxes none of it."""

ITEMIZED_LIMITS = "itemized_limits"
"""The aux kind of the limits on itemized deductions figured from the
year's spending (#542): the SALT cap and its phase-down, the mortgage debt
limit, and the AGI floors and cap on charity and medical expenses. Without
one, itemized deductions are only the hand-typed ``federal_deductions``."""

STATE_ITEMIZED_LIMITS = "state_itemized_limits"
"""A state's own ``itemized_limits`` (#542), keyed by state like its
brackets (California's: no SALT cap, a $1 million mortgage limit). A state
without one uses the federal table."""

ITEMIZED_TAGS: dict[str, str] = {
    "property_tax": "tax:property",
    "mortgage_interest": "tax:mortgage_interest",
    "charity": "tax:charity",
    "medical": "tax:medical",
}
"""The spending tags an itemized line is figured from (#542), by line.
Property tax is also spending of the ``property_tax`` character; a
``mortgage_payment`` rule's interest carries ``tax:mortgage_interest``, and
an ``irmaa`` rule's surcharges ``tax:medical``, unless the rule has a
``tax:`` tag of its own."""

STATE_KEYED_AUX_KINDS: frozenset[str] = frozenset({
    "state_brackets", STATE_ITEMIZED_LIMITS, STATE_STANDARD_DEDUCTION, STATE_LTCG_BRACKETS, STATE_STCG_BRACKETS,
    STATE_LTCG_EXCLUSION, STATE_SOCIAL_SECURITY, STATE_SURCHARGE, STATE_HSA,
    STATE_RETIREMENT_EXCLUSION, STATE_LOCAL_TAX, STATE_ADJUSTMENTS,
    STATE_GAINS_TAX, STATE_GAINS_DEDUCTION,
})
"""The aux kinds keyed by state (or, for ``state_local_tax``, by county):
a tax year reads the series where the household lives that year
(``residence.csv``, #509), so none of them is swapped by a move."""

OPTIONAL_STATE_AUX_KINDS: frozenset[str] = frozenset({
    STATE_ITEMIZED_LIMITS, STATE_SURCHARGE, STATE_HSA, STATE_LOCAL_TAX, STATE_ADJUSTMENTS,
    STATE_GAINS_TAX, STATE_GAINS_DEDUCTION,
})
"""The state tables a state may lack (#490, #487, #489, #542): then it has
no surcharge, local tax or gains tax, its HSA follows the federal rules, and
its itemized deductions the federal limits."""

STATE_BAND_KINDS: frozenset[str] = frozenset({STATE_SURCHARGE, STATE_LOCAL_TAX, STATE_GAINS_TAX})
"""The state tables of bands (#490, #487, #489): a surcharge's and a local
tax's rates add to the brackets', and a gains tax's tax its own base. A
filing status the brackets have and the table lacks would tax nothing."""

IRMAA_TIERS = "irmaa_tiers"
"""The aux kind of the Medicare income-related surcharge tiers (#253)."""

IRMAA_SERIES_COLUMNS: tuple[str, ...] = ("floor_series", "part_b_series", "part_d_series")
"""The ``irmaa_tiers`` columns naming an optional inflation series that grows
a row's floor, Part B add-on and Part D add-on (#253)."""

TAX_GROWTH_SERIES_COLUMNS: dict[str, dict[str, str]] = {
    "federal_brackets": {"floor_series": "floor"},
    "state_brackets": {"floor_series": "floor"},
    STATE_SURCHARGE: {"floor_series": "floor"},
    STATE_LOCAL_TAX: {"floor_series": "floor"},
    STATE_GAINS_TAX: {"floor_series": "floor"},
    STATE_GAINS_DEDUCTION: {"amount_series": "amount"},
    "ltcg_brackets": {"floor_series": "floor"},
    STATE_LTCG_BRACKETS: {"floor_series": "floor"},
    STATE_STCG_BRACKETS: {"floor_series": "floor"},
    "standard_deduction": {"amount_series": "amount"},
    STATE_STANDARD_DEDUCTION: {"amount_series": "amount"},
    STATE_SOCIAL_SECURITY: {
        "phaseout_start_series": "phaseout_start", "phaseout_end_series": "phaseout_end",
    },
    STATE_RETIREMENT_EXCLUSION: {"amount_series": "amount"},
    STATE_ADJUSTMENTS: {"amount_series": "amount", "income_limit_series": "income_limit"},
}
"""Optional columns of the tax tables the law indexes for inflation (#467),
by kind: {series column: the money column it grows}. Each names an
inflation series that grows the row's figure from the table's effective
date to January of the tax year, rounded down to $50 (or the kind's
``TAX_INDEX_ROUNDING``); blank or missing
holds it flat. Social Security's thresholds and the capital loss limit are
fixed in law, so they have none."""

TAX_INDEX_ROUNDING: dict[str, int] = {STATE_ADJUSTMENTS: 100}
"""Kinds whose indexed figures round down to other than $50, in cents: a
state's own credits and limits round to the dollar (California's $153
exemption credit, #488), where $50 would swallow most of their growth."""

AUX_SERIES_REFERENCE_COLUMNS: dict[str, tuple[str, ...]] = {
    IRMAA_TIERS: IRMAA_SERIES_COLUMNS,
    **{kind: tuple(columns) for kind, columns in TAX_GROWTH_SERIES_COLUMNS.items()},
}
"""The aux table columns, by kind, that name an inflation series, which a
series rename rewrites (#467)."""

ITEMIZED_LIMITS_COLUMNS: tuple[str, ...] = (
    "effective_date", "filing_status", "salt_cap", "salt_floor", "salt_phaseout_start",
    "salt_phaseout_rate", "mortgage_limit", "charity_floor", "charity_limit", "medical_floor",
)

AUX_KIND_COLUMNS: dict[str, tuple[str, ...]] = {
    "federal_brackets":
        ("effective_date", "filing_status", "bracket_index", "floor", "marginal_rate"),
    "state_brackets":
        ("effective_date", "filing_status", "bracket_index", "floor", "marginal_rate"),
    # A state's surcharge on high incomes (#490): bands in the shape of its
    # brackets whose rates add to theirs, read with the brackets' series.
    # California's is one band, 1% over $1 million, not indexed.
    STATE_SURCHARGE:
        ("effective_date", "filing_status", "bracket_index", "floor", "marginal_rate"),
    # A local income tax (#487): bands in the shape of the brackets on the
    # state's taxable income, read with the brackets' series.
    STATE_LOCAL_TAX:
        ("effective_date", "filing_status", "bracket_index", "floor", "marginal_rate"),
    # How a state treats an HSA (#490), read with the brackets' series:
    # `treatment` is one of STATE_HSA_TREATMENTS. Without a table, `federal`.
    STATE_HSA: ("effective_date", "treatment"),
    # A state's tax on long-term gains apart from its income tax (#489):
    # bands in the shape of its brackets on the gain after its own
    # deduction, read with the brackets' series. Washington's.
    STATE_GAINS_TAX:
        ("effective_date", "filing_status", "bracket_index", "floor", "marginal_rate"),
    STATE_GAINS_DEDUCTION: ("effective_date", "filing_status", "amount"),
    # Federal long-term capital gain rates: the same shape as federal_brackets,
    # but a band's floor is the taxable income (ordinary income plus the gain,
    # stacked on top) at which its rate starts. Optional: without it long-term
    # gains are taxed as ordinary income.
    "ltcg_brackets":
        ("effective_date", "filing_status", "bracket_index", "floor", "marginal_rate"),
    # A state's own gain rates (#485), the same shape as ltcg_brackets and
    # keyed by state like state_brackets. Optional: without them a state
    # taxes its gains as ordinary income.
    STATE_LTCG_BRACKETS:
        ("effective_date", "filing_status", "bracket_index", "floor", "marginal_rate"),
    STATE_STCG_BRACKETS:
        ("effective_date", "filing_status", "bracket_index", "floor", "marginal_rate"),
    # The share of a net long-term gain a state excludes (#485): `exclusion`
    # percent of it, raised to `min_exclusion` and capped at `max_exclusion`
    # (dollars, blank for none), never more than the gain. Fixed in law, so
    # not indexed.
    STATE_LTCG_EXCLUSION: ("effective_date", "exclusion", "min_exclusion", "max_exclusion"),
    "standard_deduction": ("effective_date", "filing_status", "amount"),
    # The state's own standard deduction (#248), which is often far smaller
    # than the federal one. A state tax falls back to standard_deduction
    # when there is no state_standard_deduction__<state> for its brackets'
    # state, and the loader warns that it did.
    STATE_STANDARD_DEDUCTION: ("effective_date", "filing_status", "amount"),
    # How a state taxes Social Security (#484): it starts from the federally
    # taxable amount and excludes exclusion_percent of it, an exclusion that
    # phases out above phaseout_start of federal AGI: to nothing at
    # phaseout_end (linearly; at once when they're equal), or by
    # phaseout_rate of each dollar over (a credit, as a flat-rate state gives
    # one). The phase-out columns are optional (AUX_OPTIONAL_COLUMNS). Without
    # the file a state taxes none of it.
    STATE_SOCIAL_SECURITY: ("effective_date", "filing_status", "exclusion_percent",
                            "phaseout_start", "phaseout_end", "phaseout_rate"),
    # A state's retirement income exclusions (#486), keyed by state like its
    # brackets: each row excludes up to `amount` per person (blank: all of
    # it) of the `income` kinds it lists (STATE_EXCLUSION_INCOME), from
    # `min_age`. Rows sharing a `name` are age tiers of one exclusion; a
    # blank filing_status applies to every status. `income_limit` (household
    # income) and `phaseout` take it away; `less_ss` reduces it by the
    # person's Social Security.
    STATE_RETIREMENT_EXCLUSION: (
        "effective_date", "name", "income", "min_age", "amount", "filing_status",
        "income_limit", "phaseout", "less_ss",
    ),
    # A state's own deductions and credits (#511, #488), keyed by state
    # like its brackets: each `name` a line of the return, at `step`
    # (STATE_ADJUSTMENT_STEPS), figured from `base` (STATE_ADJUSTMENT_BASES;
    # `tag` for spending and income) times `rate` (blank: all of it), at
    # most `amount` (each eligible filer's with `per_person`; for `filers`,
    # the amount each). Filers count from `min_age`. Above `income_limit`
    # it ends at once, falls to nothing over `phaseout` dollars, or loses
    # `phaseout_rate` of each dollar over. A blank filing_status applies to
    # every status. Every column after `base` may be left out.
    STATE_ADJUSTMENTS: (
        "effective_date", "name", "step", "base", "tag", "rate", "amount", "per_person",
        "min_age", "filing_status", "income_limit", "phaseout", "phaseout_rate",
    ),
    # Itemized deductions: a dated total, no filing_status split (unlike
    # standard_deduction). Optional; when present, a tax jurisdiction uses
    # whichever is greater, itemized or standard. `amount` may be a literal
    # figure or an itemizations/<name>.csv rollup (`@name`), same as any other
    # aux money column. `state_deductions` defaults to `federal_deductions`'s
    # value when no `state_deductions` rows exist at all.
    "federal_deductions": ("effective_date", "amount"),
    "state_deductions": ("effective_date", "amount"),
    # The limits on the itemized lines figured from the year's spending
    # (#542), by filing status: state and local taxes (SALT) up to
    # `salt_cap`, which falls by `salt_phaseout_rate` of MAGI over
    # `salt_phaseout_start` but not below `salt_floor`; mortgage interest on
    # up to `mortgage_limit` of debt; charity over `charity_floor` and at
    # most `charity_limit` of AGI; medical expenses over `medical_floor` of
    # AGI. A blank cell is no limit. Every column after filing_status may be
    # left out.
    ITEMIZED_LIMITS: ITEMIZED_LIMITS_COLUMNS,
    STATE_ITEMIZED_LIMITS: ITEMIZED_LIMITS_COLUMNS,
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
# swap of one during the run would change nothing. A state's tables follow
# where the household lives (residence.csv, #509), not a swap.
SWAPPABLE_AUX_KINDS: frozenset[str] = (
    frozenset(AUX_KIND_COLUMNS) - {"security_price", "life_table"} - STATE_KEYED_AUX_KINDS
)

# Columns authored in a natural unit but stored as integer micros, by kind:
# {kind: {csv column: (payload key, unit)}} (#130). The unit is `price`
# (decimal dollars, positive, up to six places: sub-cent precision),
# `percent` (a rate, with its sign: `4.5%`) or `number` (a plain decimal).
AUX_MICROS_COLUMNS: dict[str, dict[str, tuple[str, str]]] = {
    "security_price": {"price": ("price_micros", "price")},
    "federal_brackets": {"marginal_rate": ("marginal_rate_micros", "percent")},
    "state_brackets": {"marginal_rate": ("marginal_rate_micros", "percent")},
    STATE_SURCHARGE: {"marginal_rate": ("marginal_rate_micros", "percent")},
    STATE_LOCAL_TAX: {"marginal_rate": ("marginal_rate_micros", "percent")},
    STATE_GAINS_TAX: {"marginal_rate": ("marginal_rate_micros", "percent")},
    "ltcg_brackets": {"marginal_rate": ("marginal_rate_micros", "percent")},
    STATE_LTCG_BRACKETS: {"marginal_rate": ("marginal_rate_micros", "percent")},
    STATE_STCG_BRACKETS: {"marginal_rate": ("marginal_rate_micros", "percent")},
    STATE_LTCG_EXCLUSION: {"exclusion": ("exclusion_micros", "percent")},
    "rmd_factors": {"factor": ("factor_micros", "number")},
    "rate": {"annual_rate": ("annual_rate_micros", "percent")},
    "inflation": {"annual_rate": ("annual_rate_micros", "percent")},
    "energy_credits": {"rate": ("rate_micros", "percent")},
    STATE_SOCIAL_SECURITY: {
        "exclusion_percent": ("exclusion_micros", "percent"),
        "phaseout_rate": ("phaseout_rate_micros", "percent"),
    },
    STATE_ADJUSTMENTS: {
        "rate": ("rate_micros", "percent"),
        "phaseout_rate": ("phaseout_rate_micros", "percent"),
    },
    **{kind: {
        "salt_phaseout_rate": ("salt_phaseout_rate_micros", "percent"),
        "charity_floor": ("charity_floor_micros", "percent"),
        "charity_limit": ("charity_limit_micros", "percent"),
        "medical_floor": ("medical_floor_micros", "percent"),
    } for kind in (ITEMIZED_LIMITS, STATE_ITEMIZED_LIMITS)},
    "life_table": {"q_female": ("q_female_micros", "number"),
                   "q_male": ("q_male_micros", "number")},
}

# Column names the CSVs used before #130, when rates and factors were
# written in micros. A file still carrying one fails to load, naming the
# conversion script, rather than being read 10,000 times too large.
LEGACY_MICROS_COLUMNS: frozenset[str] = frozenset({
    "annual_rate_micros", "marginal_rate_micros", "rate_micros", "factor_micros",
})
UNITS_CONVERSION_HINT = "rates and factors are now percents and plain numbers (#130)"

# Per-aux-kind set of column names that hold dollar amounts in the CSV and must
# be converted to integer cents during load.
AUX_MONEY_COLUMNS: dict[str, frozenset[str]] = {
    "federal_brackets": frozenset({"floor"}),
    "ltcg_brackets": frozenset({"floor"}),
    "state_brackets": frozenset({"floor"}),
    STATE_LTCG_BRACKETS: frozenset({"floor"}),
    STATE_STCG_BRACKETS: frozenset({"floor"}),
    STATE_LTCG_EXCLUSION: frozenset({"min_exclusion", "max_exclusion"}),
    STATE_SURCHARGE: frozenset({"floor"}),
    STATE_LOCAL_TAX: frozenset({"floor"}),
    STATE_GAINS_TAX: frozenset({"floor"}),
    STATE_GAINS_DEDUCTION: frozenset({"amount"}),
    "standard_deduction": frozenset({"amount"}),
    STATE_STANDARD_DEDUCTION: frozenset({"amount"}),
    STATE_RETIREMENT_EXCLUSION: frozenset({"amount", "income_limit", "phaseout"}),
    STATE_ADJUSTMENTS: frozenset({"amount", "income_limit", "phaseout"}),
    "federal_deductions": frozenset({"amount"}),
    "state_deductions": frozenset({"amount"}),
    **{kind: frozenset({"salt_cap", "salt_floor", "salt_phaseout_start", "mortgage_limit"})
       for kind in (ITEMIZED_LIMITS, STATE_ITEMIZED_LIMITS)},
    "ss_taxation_thresholds": frozenset({"tier1_floor", "tier2_floor"}),
    "energy_credits": frozenset({"cap"}),
    "capital_loss_limit": frozenset({"amount"}),
    IRMAA_TIERS: frozenset({"magi_floor", "part_b", "part_d"}),
    STATE_SOCIAL_SECURITY: frozenset({"phaseout_start", "phaseout_end"}),
}

AUX_OPTIONAL_COLUMNS: dict[str, frozenset[str]] = {
    **{kind: frozenset(ITEMIZED_LIMITS_COLUMNS[2:]) for kind in (ITEMIZED_LIMITS, STATE_ITEMIZED_LIMITS)},
    STATE_SOCIAL_SECURITY: frozenset({"phaseout_start", "phaseout_end", "phaseout_rate"}),
    STATE_ADJUSTMENTS: frozenset({
        "tag", "rate", "amount", "per_person", "min_age", "filing_status", "income_limit",
        "phaseout", "phaseout_rate",
    }),
}
"""Columns of ``AUX_KIND_COLUMNS`` a file may leave out, by kind: a missing
column or a blank cell is stored as None."""


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
    # size in micros; #280: where the path starts (blank, or `current`).
    "vary_process", "vary_persistence", "vary_shock_sd", "vary_process_start",
    # #291: on a set_rate row with no rate of its own, a date: the row puts
    # back whatever governed its series the day before it (a process resumes,
    # a fixed rate returns).
    "vary_resume",
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

# Renamed calendar.csv columns, each with why: the old name is rejected with
# its new one, not aliased, so a file is converted once (a clean break).
# #31: `vary_mean` became `vary_median`, the truncated distribution's median,
# and its meaning changed for asymmetric bounds. #284: `vary_innovation_sd`
# became `vary_shock_sd`, the name `analysis historical` gives it.
RENAMED_CALENDAR_COLUMNS: dict[str, tuple[str, str]] = {
    "vary_mean": ("vary_median", "now the median of the truncated distribution, so the "
                                 "base case is exactly the authored value"),
    "vary_innovation_sd": ("vary_shock_sd", "the same yearly shock size; `analysis historical` "
                                            "calls it Shock SD"),
}

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


# The IRS filing-status codes the aux tables are keyed by (templates/aux/
# README.md): Head of Household isn't modeled. `sim init` offers these.
FILING_STATUSES: tuple[str, ...] = ("MFJ", "Single", "MFS")


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
    "vary_correlate_with", "vary_correlation", "life_table",
)
# #315: lifespan.csv `life_table` names the aux life_table__<name>.csv the
# person's age is read against in this scenario; blank (or no such column)
# is life_table.csv. Per scenario, so scenarios can compare tables.

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
