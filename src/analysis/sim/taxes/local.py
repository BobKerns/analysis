"""The ``local_bands`` tax calculator (#487, #508; ADR-003 §2.6): a local
income tax charged on the state's taxable income at the
``state_local_tax__<XX>`` bands, ``XX`` being the state's
``bracket_series`` unless the local file's params name another."""

from __future__ import annotations

from types import MappingProxyType

from analysis.sim import registries
from analysis.sim.tax_api import (
    NO_EXTRA, Extra, Prepared, Return, TaxCalculator, TaxYear, register,
)
from analysis.sim.taxes import common


@register
class LocalBands(TaxCalculator):
    """A local income tax: bands on the state's taxable income."""
    kind = "local_bands"
    jurisdiction = "local"
    upstream = ("state",)

    def prepare(self, year: TaxYear) -> Prepared:
        return LocalBandsYear(year)


class LocalBandsYear(Prepared):
    """One local tax year, ready to compute: the state's year with the
    same extra income, and the bands."""

    def __init__(self, ty: TaxYear) -> None:
        self.state = ty.upstream("state")
        self.filing_status = self.state.filing_status
        series = ty.params.get("bracket_series", self.state.bracket_series)
        self.bracket_series = None if series is None else str(series)
        self.brackets = (common.rate_table(ty, registries.STATE_LOCAL_TAX, self.bracket_series)
                         if self.bracket_series is not None else [])

    def compute(self, extra: Extra = NO_EXTRA) -> Return:
        state = self.state.compute(extra)
        taxable = state.taxable_income
        lines = {
            "agi": state.agi,
            "taxable_income": taxable,
            "ordinary_taxable": taxable,
            "tax": common.progressive_tax(taxable, self.brackets),
            # A local tax carries nothing: the state's own carryovers are its.
            "nol_carryover": 0,
            "capital_loss_carryover": 0,
        }
        memo = state.memo
        return Return(lines, MappingProxyType({
            "drawn_ordinary": memo.get("drawn_ordinary", 0),
            "drawn_gain": memo.get("drawn_gain", 0)}))
