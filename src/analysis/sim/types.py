# Type definitions for the analysis.sim module.

from enum import StrEnum
from typing import Literal


type AccountKind = Literal["asset", "liability", "income", "expense"]
"""Account kind type."""

type AccountName = str
"""Account name type."""

type ScenarioName = str
"""Scenario name type."""


class InvestmentAssetSubkind(StrEnum):
    BROKERAGE_TAXABLE = "brokerage_taxable"
    TRADITIONAL_IRA = "traditional_ira"
    ROTH_IRA = "roth_ira"
    TRADITIONAL_401K = "traditional_401k"
    ROTH_401K = "roth_401k"
    HSA = "hsa"
    
INVESTMENT_ASSET_SUBKINDS = frozenset(item.value for item in InvestmentAssetSubkind)
"""
Subkinds eligible for prior_year_end_balance (the RMD-relevant investment
holdings); checking/savings/vehicle/real_estate/other_asset are excluded.
"""


type ItemType = Literal[
    "account",
    "scheduled",
    "calendar_event",
    "transfer_event",
    "simulation_run",
    "net_worth",
    "net_worth_constant_cpi",
    "account_balance",
    "account_model_member",
    "aux_table",
    "scenario",
    "tax_year_summary",
    "year_to_date",
    "person",
    "person_lifespan",
    "chain",
    "chain_steps",
    "intent_manifest",
]
"""Item type literals."""


type IncomeCategory = Literal[
    "salary",
    "social security",
    "investment",
]
"""Income category type."""

INCOME_CATEGORIES: frozenset[IncomeCategory] = frozenset(IncomeCategory.__value__.__args__)
"""Income category literals."""