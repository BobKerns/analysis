"""The ``local_bands`` tax calculator (#487, #508; ADR-003 §2.6): a local
income tax charged on the state's taxable income at the
``state_local_tax__<XX>`` bands, ``XX`` being the county the household
lives in that year (``residence.csv``, #509), or else its state."""

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
        residence = ty.residence
        # A county's own table, or the state's (one for the whole state, or
        # where the household's county isn't said); none, no local tax.
        series = None if residence is None else residence.county or residence.state
        self.brackets = (common.rate_table(ty, registries.STATE_LOCAL_TAX, series)
                         if series is not None else [])

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
            "drawn_gain": memo.get("drawn_gain", 0),
            # The local bands tax the state's taxable income, so they price
            # an HSA as the state does (#497).
            "hsa_taxed": memo.get("hsa_taxed", 0)}))
