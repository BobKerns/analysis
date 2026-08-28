"""Locked vocabularies from ADR-003."""

from __future__ import annotations

import re
from typing import NamedTuple

ACCOUNT_SUBKINDS: dict[str, set[str]] = {
    "asset": {
        "checking", "savings", "brokerage_taxable", "traditional_ira", "roth_ira",
        "traditional_401k", "roth_401k", "hsa", "real_estate", "vehicle", "other_asset",
    },
    "liability": {"mortgage", "heloc", "credit_card", "other_liability"},
    "income": {
        "salary", "ss_benefit", "pension", "interest", "dividend", "capital_gain",
        "rental", "other_income",
    },
    "expense": {
        "living", "healthcare", "housing", "interest_expense", "taxes_federal",
        "taxes_state", "taxes_property", "other_expense",
    },
}

AMOUNT_RULE_KINDS: set[str] = {
    "fixed_amount", "percent_of_balance", "rmd",
    "ss_benefit", "tax_accumulator", "mortgage_payment",
}

# Canonical scheduled.csv column order (ADR-003 §4.6). `series` and `amount` are
# each interpreted per amount_rule_kind; see SERIES_RULE_KINDS below.
SCHEDULED_CSV_HEADER: tuple[str, ...] = (
    "name", "from_account", "to_account", "cadence", "month_of_year",
    "amount_rule_kind", "interest_account", "person", "series",
    "rate_micros", "claim_age", "amount", "base_date", "inflation_cadence",
    "tax_character", "tax_year", "start", "end", "priority", "ytd_amount", "ytd_preroll",
    "description", "meta_json", "params_json", "tags",
)

# Rules that read the `series` column, and what it names for each.
SERIES_RULE_KINDS: dict[str, str] = {
    "fixed_amount": "inflation series",
    "percent_of_balance": "rate series",
    "ss_benefit": "COLA series",
}

# tax_treatment -> withdrawal tax_character, for chain legs (ADR-003 §2.9): the
# character of a leg drawn from an account depends on which concrete account
# actually supplied the money, so this is looked up per-leg at runtime rather
# than authored on the scheduled_transaction row. 'taxable' is deliberately
# absent: `checking`/`savings` withdrawals return already-taxed principal
# ('none'), but a taxable brokerage withdrawal may be `ltcg` or
# `return_of_capital` depending on cost basis -- state the engine doesn't yet
# track. Callers must special-case 'taxable' by subkind (see
# `withdrawal_tax_character`) rather than treat it as a flat lookup.
TAX_TREATMENT_TO_WITHDRAWAL_CHARACTER: dict[str, str] = {
    "tax_deferred": "tax_deferred_out",
    "tax_free": "tax_free",
    "none": "none",
}

# Subkinds whose 'taxable' tax_treatment still withdraws tax-free (principal,
# already taxed when earned) -- i.e. cash-like accounts, not brokerage.
TAXABLE_SUBKINDS_WITH_NO_WITHDRAWAL_TAX: set[str] = {"checking", "savings"}


def withdrawal_tax_character(tax_treatment: str | None, subkind: str) -> str | None:
    """The tax_character of a leg drawn from an account with these attributes.

    Returns ``None`` when the account's tax_treatment/subkind combination isn't
    supported yet (today: a `taxable` account that isn't cash-like, e.g.
    `brokerage_taxable`, which needs cost-basis tracking not yet implemented) --
    callers should raise rather than guess.
    """
    if tax_treatment == "taxable":
        return "none" if subkind in TAXABLE_SUBKINDS_WITH_NO_WITHDRAWAL_TAX else None
    return TAX_TREATMENT_TO_WITHDRAWAL_CHARACTER.get(tax_treatment or "none", "none")

CALENDAR_EVENT_KINDS: set[str] = {
    "set_rate", "set_amount", "add_scheduled", "retire_scheduled", "swap_aux_table",
    "one_off_transfer", "sell_asset", "buy_asset", "life_transition",
}

CADENCES: set[str] = {"monthly", "biweekly", "quarterly", "annual", "one_off"}

TAX_CHARACTERS: set[str] = {
    "ordinary", "ltcg", "gains", "deferred", "ss_benefit", "tax_free",
    "tax_deferred_in", "tax_deferred_out", "return_of_capital", "property_tax", "none",
}

TAX_TREATMENTS: set[str] = {"taxable", "tax_deferred", "tax_free", "none"}

# Canonical accounts.csv column order (ADR-003 §4.5).
ACCOUNTS_CSV_HEADER: tuple[str, ...] = (
    "name", "kind", "subkind", "owner", "tax_treatment", "series",
    "tax_character", "minimum_balance", "refill_chain",
    "maximum_balance", "overflow_chain", "opened", "closed", "meta_json", "tags",
)

# Kinds a chains/<kind>__<name>.csv file may declare (ADR-003 §2.9); a closed
# registry like every other file-kind vocabulary, extensible without engine
# changes (a future 'expense'/'income' entry would enable deposit-only chains).
CHAIN_KINDS: set[str] = {"asset", "income"}


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
}

# Aux file kind -> required columns (ADR-003 §4.4).
# CSV money columns are written in decimal dollars (e.g. ``1234.56``); the loader
# converts them to integer cents stored under the same column name in the
# payload JSON.
AUX_KIND_COLUMNS: dict[str, tuple[str, ...]] = {
    "federal_brackets":
        ("effective_date", "filing_status", "bracket_index", "floor", "marginal_rate_micros"),
    "state_brackets":
        ("effective_date", "filing_status", "bracket_index", "floor", "marginal_rate_micros"),
    "standard_deduction": ("effective_date", "filing_status", "amount"),
    "ss_taxation_thresholds":
        ("effective_date", "filing_status", "tier1_floor", "tier2_floor"),
    "rmd_factors": ("effective_date", "age", "factor_micros"),
    "rate": ("effective_date", "annual_rate_micros"),
    "inflation": ("effective_date", "annual_rate_micros"),
    "filing_status": ("effective_date", "filing_status"),
    "rmd_start_age": ("effective_date", "start_age"),
}

# Per-aux-kind set of column names that hold dollar amounts in the CSV and must
# be converted to integer cents during load.
AUX_MONEY_COLUMNS: dict[str, frozenset[str]] = {
    "federal_brackets": frozenset({"floor"}),
    "state_brackets": frozenset({"floor"}),
    "standard_deduction": frozenset({"amount"}),
    "ss_taxation_thresholds": frozenset({"tier1_floor", "tier2_floor"}),
}

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
