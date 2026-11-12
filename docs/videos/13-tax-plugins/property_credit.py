"""DC's income tax with its property tax credit (Schedule H), the worked
example in docs/guides/tax-calculators.md and video 13: the built-in
``state_tables``, less a refundable credit for property tax above a share
of federal AGI."""

from dataclasses import dataclass

from analysis.sim.tax_api import Dollars, Tag, percent, register
from analysis.sim.taxes.state_tables import StateTables, StateTablesYear

API_VERSION = 4

# Schedule H's worksheet for 2025: up to each federal AGI, the share of it
# the property tax must exceed. Past the last row, no credit.
UNDER_70 = [(24_999_00, percent("3%")),
            (51_999_00, percent("4%")),
            (66_000_00, percent("5%"))]
AGE_70 = [(90_000_00, percent("3%"))]


class PropertyCreditYear(StateTablesYear):
    """One year of DC's tax, with the credit."""

    def setup(self):
        # Once a year: what a what-if's extra income doesn't change.
        super().setup()
        ty, params = self.ty, self.ty.params
        bands = AGE_70 if self.oldest() >= 70 else UNDER_70
        self.bands = [(ty.grown(top, params.series, 2025), share)
                      for top, share in bands]
        self.most = ty.grown(params.most, params.series, 2025)
        self.paid = (0 if params.property_tax is None
                     else ty.spent(params.property_tax))
        if params.rent is not None:
            self.paid += ty.spent(params.rent) // 5  # 20% of rent counts

    def refundable_credits(self, tax, income, basis):
        # Each return, with the federal AGI it builds on.
        agi = basis.federal.lines["agi"]
        share = next((s for top, s in self.bands if agi <= top), None)
        if share is None:
            return {}
        credit = self.paid - agi * share // 1_000_000
        return {"property_credit": max(0, min(credit, self.most))}

    def oldest(self):
        """The older filer's age on December 31: the primary and secondary
        people, less one who died before the year."""
        year = self.ty.year
        return max((year - int(p.birthdate[:4]) for p in self.ty.persons
                    if p.birthdate and p.role in ("primary", "secondary")
                    and (p.final_month is None
                         or int(p.final_month[:4]) >= year)), default=0)


@register
class PropertyCredit(StateTables):
    """``state_tables`` with DC's property tax credit: the year's property
    tax, or 20% of its rent, above a share of federal AGI, up to ``most``,
    paid even when no tax is due. Its 2025 figures grow with ``series``."""
    kind = "property_credit"
    jurisdiction = "state"
    year_class = PropertyCreditYear

    @dataclass(frozen=True)
    class Params(StateTables.Params):
        property_tax: Tag | None = "tax:property"
        """The tag on the property tax paid; none for a renter."""
        rent: Tag | None = None
        """The tag on rent paid, for a renter."""
        most: Dollars = 1425_00
        """The largest credit, in 2025."""
        series: str = "cpi"
        """The inflation series the 2025 figures grow by."""
