"""A state income tax with a renter deduction, a test calculator (since
#511 a row of ``state_adjustments`` can say the deduction itself): the
built-in ``state_tables`` state, which starts from federal AGI, less part
of the rent the household paid, up to a cap, and, if asked, the pension a
public employer paid."""

from dataclasses import dataclass

from analysis.sim.tax_api import Dollars, Percent, Tag, percent, register
from analysis.sim.taxes.state_base import Adjustment
from analysis.sim.taxes.state_tables import StateTables, StateTablesYear

API_VERSION = 1


class RentersYear(StateTablesYear):
    """One year of the state: the built-in state's, with two more
    subtractions."""

    def subtractions(self, income, basis):
        params = self.ty.params
        # The year's spending carrying the rent tag: a rule tagged tax:rent,
        # or any payment into an expense account tagged so.
        rent = self.ty.spent(params.rent)
        deduction = min(rent * params.rent_share // 1_000_000, params.rent_cap)
        lines = [*super().subtractions(income, basis),
                 Adjustment("renter_deduction", ordinary=min(deduction, max(0, income.ordinary)))]
        if params.public_pension is not None:
            # Income out of an account (or by a rule) tagged as a public pension.
            pension = sum(item.amount for item in self.ty.tagged(params.public_pension))
            lines.append(Adjustment("public_pension", ordinary=min(pension, max(0, income.ordinary))))
        return lines


@register
class Renters(StateTables):
    """A state like ``state_tables``, less a renter deduction: ``rent_share``
    of the year's rent (spending tagged ``rent``), up to ``rent_cap``; and,
    with ``public_pension``, the pension income tagged so."""
    kind = "state_renters"
    jurisdiction = "state"
    year_class = RentersYear

    @dataclass(frozen=True)
    class Params(StateTables.Params):
        rent: Tag = "tax:rent"
        """The tag on the rent the deduction counts."""
        rent_share: Percent = percent("50%")
        """How much of the rent comes off."""
        rent_cap: Dollars = 4000_00
        """The most the deduction takes off."""
        public_pension: Tag | None = None
        """The tag on a public pension's income, which the state doesn't tax."""
