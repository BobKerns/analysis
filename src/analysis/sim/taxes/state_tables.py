"""The ``state_tables`` tax calculator (ADR-003 §2.6)."""

from __future__ import annotations

from analysis.sim.tax_api import register
from analysis.sim.taxes.tables import TablesCalculator


@register
class StateTables(TablesCalculator):
    """A state income tax from tables keyed by the state (``bracket_series``):
    its brackets, deduction, gain tables and exclusion, Social Security
    exclusion, retirement exclusions, HSA treatment, surcharge and local
    tax."""
    kind = "state_tables"
    jurisdiction = "state"
    state = True
