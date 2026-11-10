"""The ``federal`` tax calculator (ADR-003 §2.6)."""

from __future__ import annotations

from analysis.sim.tax_api import register
from analysis.sim.taxes.tables import TablesCalculator


@register
class Federal(TablesCalculator):
    """The federal income tax: ``federal_brackets``, the standard deduction
    or itemized deductions, Social Security taxation, long-term gains and
    qualified dividends at ``ltcg_brackets``' rates, and an 80% NOL limit."""
    kind = "federal"
    jurisdiction = "federal"
