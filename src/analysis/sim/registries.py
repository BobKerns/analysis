"""Locked vocabularies from ADR-003."""

from __future__ import annotations

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
    "fixed_amount", "percent_of_balance", "rmd", "waterfall_fill",
    "ss_benefit", "tax_accumulator", "mortgage_payment",
}

CALENDAR_EVENT_KINDS: set[str] = {
    "set_rate", "set_amount", "add_scheduled", "retire_scheduled", "swap_aux_table",
    "one_off_transfer", "sell_asset", "buy_asset", "life_transition",
}

CADENCES: set[str] = {"monthly", "biweekly", "quarterly", "annual", "one_off"}

TAX_CHARACTERS: set[str] = {
    "ordinary", "ltcg", "ss_benefit", "tax_free",
    "tax_deferred_in", "tax_deferred_out", "return_of_capital", "property_tax", "none",
}

TAX_TREATMENTS: set[str] = {"taxable", "tax_deferred", "tax_free", "none"}

DEFAULT_PRIORITIES: dict[str, int] = {
    "percent_of_balance": 100,
    "ss_benefit": 200,
    "fixed_amount": 200,
    "mortgage_payment": 300,
    "tax_accumulator": 400,
    "rmd": 500,
    "waterfall_fill": 900,
}

# Aux file kind -> required columns (ADR-003 §4.4).
AUX_KIND_COLUMNS: dict[str, tuple[str, ...]] = {
    "federal_brackets":
        ("effective_date", "filing_status", "bracket_index", "floor_cents", "marginal_rate_micros"),
    "state_brackets":
        ("effective_date", "filing_status", "bracket_index", "floor_cents", "marginal_rate_micros"),
    "standard_deduction": ("effective_date", "filing_status", "amount_cents"),
    "ss_taxation_thresholds":
        ("effective_date", "filing_status", "tier1_floor_cents", "tier2_floor_cents"),
    "rmd_factors": ("effective_date", "age", "factor_micros"),
    "rate": ("effective_date", "annual_rate_micros"),
    "inflation": ("effective_date", "annual_rate_micros"),
    "filing_status": ("effective_date", "filing_status"),
    "rmd_start_age": ("effective_date", "start_age"),
}
