# Type definitions for the analysis.sim module.

from enum import StrEnum
from typing import Literal


type AccountKind = Literal["asset", "liability", "income", "expense"]
"""Account kind type."""

type AccountKindAny = AccountKind | Literal["any"]
"""Account kind type including 'any'."""

type AccountName = str
"""Account name type."""

type ScenarioName = str
"""Scenario name type."""


class DollarMode(StrEnum):
    """Dollar mode type."""
    NOMINAL = "nominal"
    CPI_CONSTANT = "cpi_constant"


class ValuePlotMode(StrEnum):
    """Value plot mode type."""
    TOTAL = "total"
    MONTHLY = "monthly"


class YAxisPlotMode(StrEnum):
    """Y-axis plot mode type."""
    ZERO_ANCHORED = "zero_anchored"
    AUTO_SCALE = "auto_scale"

class LogLinearMode(StrEnum):
    """Log-linear mode type."""
    LOG = "log"
    LINEAR = "linear"

class DeltaScale(StrEnum):
    """Sensitivity delta scale (ADR-007 §4): dollars, or % of the base."""
    ABSOLUTE = "absolute"
    RELATIVE = "relative"

class HeatmapValue(StrEnum):
    """What a sensitivity heatmap cell shows (ADR-007 §9)."""
    SHARE = "share"
    SWING = "swing"

class AttributionAxis(StrEnum):
    """How a sensitivity delta is decomposed (ADR-007 §6.1)."""
    CATEGORY = "category"
    FLOW_ACCOUNT = "flow_account"
    HOLDING_ACCOUNT = "holding_account"

class InvestmentAssetSubkind(StrEnum):
    """
    Investment asset subkind type.

    These subkinds represent different types of investment accounts.
    The categories represent the tax treatment of the investment accounts.
    """
    BROKERAGE_TAXABLE = "brokerage_taxable"
    TRADITIONAL_IRA = "traditional_ira"
    ROTH_IRA = "roth_ira"
    TRADITIONAL_401K = "traditional_401k"
    ROTH_401K = "roth_401k"
    HSA = "hsa"
    
INVESTMENT_ASSET_SUBKINDS = frozenset(item.value for item in InvestmentAssetSubkind)
"""
Subkinds eligible for prior_year_end_balance (the RMD-relevant investment
holdings); cash/vehicle/real_estate/other_asset are excluded.
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
    "lot_balance",
    "account_lot",
    "aux_table",
    "scenario",
    "tax_year_summary",
    "capital_loss",
    "year_to_date",
    "person",
    "security",
    "security_allocation",
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