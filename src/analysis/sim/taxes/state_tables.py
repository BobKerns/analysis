"""The ``state_tables`` tax calculator (ADR-003 §2.6): a state that starts
from federal AGI (#508) and adjusts it by tables keyed by the state
(``bracket_series``): qualified dividends as ordinary income, an HSA it
taxes, its own capital-gain netting and exclusion, retirement exclusions and
a Social Security exclusion; then its deduction, brackets, gain tables,
surcharge and local tax."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from analysis.sim import registries
from analysis.sim.tax_api import TaxError, register
from analysis.sim.taxes import common
from analysis.sim.taxes.state_base import (
    Adjustment, Basis, FederalStateYear, Income, StateFromFederal,
)


class StateTablesYear(FederalStateYear):
    """One year of a :class:`StateTables` state."""

    def setup(self) -> None:
        ty, params = self.ty, self.ty.params
        where = common.where(ty)
        bracket_aux = str(params.get("bracket_aux", "state_brackets"))
        series = self._series = common.bracket_series(ty, bracket_aux)
        self.brackets = common.brackets(ty, bracket_aux, series)
        # A state's gain tables and exclusion (#485) are keyed by state, like
        # its standard deduction, so they follow bracket_series.
        self.ltcg_brackets = common.rate_table(ty, str(params.get(
            "ltcg_bracket_aux", registries.STATE_LTCG_BRACKETS)), series)
        self.stcg_brackets = common.rate_table(ty, str(params.get(
            "stcg_bracket_aux", registries.STATE_STCG_BRACKETS)), series)
        self._net_ltcg = common.ltcg_exclusion(ty, str(params.get(
            "ltcg_exclusion_aux", registries.STATE_LTCG_EXCLUSION)), series)

        # A state that doesn't recognize an HSA (#490) taxes it like a
        # brokerage account: no deduction going in, its growth each year,
        # nothing coming out.
        hsa_growth: dict[int, int] = {}
        self._hsa_taxed = False
        if series is not None:
            taxed_years = [year for year in common.hsa_flow_years(ty)
                           if common.state_taxes_hsa(ty, series, year)]
            hsa_growth = common.hsa_growth_by_year(ty, taxed_years)
            self._hsa_taxed = ty.year in taxed_years
        self._hsa_growth = hsa_growth.get(ty.year, 0)
        # The state nets its own gains: an HSA's growth is gain to it.
        capital = self._capital = common.capital_gains(
            ty, loss_limit=common.loss_limit(ty), extra_ltcg=hsa_growth)

        # A state's retirement income exclusions (#486) come off before AGI,
        # as the states' own subtractions from income do, after its gain
        # exclusion. Keyed by state, like the standard deduction below.
        exclusion_aux = str(params.get(
            "retirement_exclusion_aux", registries.STATE_RETIREMENT_EXCLUSION))
        self._exclusion = (
            common.retirement_exclusion(
                ty, exclusion_aux, series,
                net_gain=capital.stcg + self._net_ltcg(capital.ltcg), hsa_taxed=self._hsa_taxed)
            if series is not None else common.RetirementExclusion()
        )

        # A state taxes Social Security only by its state_social_security
        # table (#484), keyed by state like its brackets.
        state_ss: dict[str, Any] | None = None
        if series is not None and ty.has_table(registries.STATE_SOCIAL_SECURITY, series):
            rows = [r for r in ty.indexed(registries.STATE_SOCIAL_SECURITY, series)
                    if r.get("filing_status") == ty.filing_status]
            if not rows:
                raise TaxError(
                    f"{where}: no {registries.STATE_SOCIAL_SECURITY}__"
                    f"{series} row for filing_status={ty.filing_status!r} in {ty.year}")
            state_ss = rows[0]
        self._state_ss = state_ss

        def bands(kind: str) -> list[tuple[int, int]]:
            return [] if series is None else common.rate_table(ty, kind, series)

        # A state's surcharge on high incomes (#490) adds its rates to the
        # brackets'. Its own bands, rather than more bracket rows, because
        # its floor needn't be indexed or fall between the same brackets for
        # every filing status (California's $1 million). A local income tax
        # (#487) is bands on the state's taxable income too, reported apart
        # from it.
        self._surcharge = bands(registries.STATE_SURCHARGE)
        self._local = bands(registries.STATE_LOCAL_TAX)

    def deductions(self) -> Mapping[str, int]:
        ty, params = self.ty, self.ty.params
        standard_aux = str(params.get("standard_deduction_aux", registries.STATE_STANDARD_DEDUCTION))
        # A state's standard deduction is keyed by state, like its brackets
        # (state_standard_deduction__CA.csv), so it follows bracket_series.
        series = self._series if standard_aux == registries.STATE_STANDARD_DEDUCTION else None
        if series is not None and not ty.has_table(standard_aux, series):
            # The loader warned: the federal figure stands in for the state's.
            standard_aux, series = "standard_deduction", None
        standard = common.standard_deduction(ty, standard_aux, series)
        # state_deductions falls back to federal_deductions when the
        # scenario has no state_deductions rows at all: most states don't
        # need their own file.
        itemized_aux = str(params.get("itemized_deduction_aux", "state_deductions"))
        if not ty.has_table(itemized_aux):
            itemized_aux = "federal_deductions"
        itemized = common.itemized_deduction(ty, itemized_aux)
        return {"standard_deduction": standard, "itemized_deduction": itemized,
                "deduction": max(standard, itemized)}

    def additions(self, income: Income, basis: Basis) -> Sequence[Adjustment]:
        fed, capital = basis.federal, self._capital
        memo = fed.memo
        dividends = memo.get("qualified_dividends", 0)
        contributed = withdrawn = 0
        if self._hsa_taxed:
            contributed, withdrawn = memo.get("hsa_contributed", 0), memo.get("hsa_withdrawn", 0)
        return (
            # A state has no preference for qualified dividends: its gain
            # rules (#485) are for gains, so its dividends are ordinary.
            Adjustment("dividends_as_ordinary", ordinary=dividends, ltcg=-dividends, amount=dividends),
            # A state that taxes an HSA undoes the federal deduction of what
            # went in, and doesn't tax what came out (#490).
            Adjustment("hsa_addback", ordinary=contributed - withdrawn),
            # Its own netting of gains and losses, with an HSA's growth as
            # gain, less the federal result.
            Adjustment(
                "capital_gain_difference",
                ordinary=fed["capital_loss_used"] - capital.loss_deduction,
                stcg=capital.stcg - memo.get("net_stcg", 0),
                ltcg=capital.ltcg - memo.get("net_ltcg", 0)),
        )

    def subtractions(self, income: Income, basis: Basis) -> Sequence[Adjustment]:
        capital, extra = self._capital, basis.extra
        gain = capital.ltcg + extra.ltcg
        exclusion = Adjustment("ltcg_exclusion", ltcg=gain - self._net_ltcg(gain))
        ordinary, ltcg = income.ordinary, income.ltcg - exclusion.ltcg
        # Gain taken away (the drawdown estimate replaces posted gain) can't
        # be left out; gain added isn't in anyone's income by kind.
        net_gain = min(self._exclusion.net_gain, capital.stcg + self._net_ltcg(gain))
        off_ordinary, off_ltcg, off_short = self._exclusion.split(ordinary, ltcg, net_gain)
        retirement = Adjustment(
            "retirement_exclusion", ordinary=off_ordinary - off_short, stcg=off_short, ltcg=off_ltcg)
        return exclusion, retirement, Adjustment("ss_difference", ss=income.ss - self._taxable_ss(basis))

    def _taxable_ss(self, basis: Basis) -> int:
        """The state's taxable Social Security: none, or by its table the
        federally taxable amount less its exclusion, phased out by federal
        AGI (#484)."""
        if self._state_ss is None:
            return 0
        fed = basis.federal
        taxable = fed["ss_taxable"]
        return taxable - common.state_ss_exclusion(self._state_ss, taxable, fed.agi)

    def fixed_lines(self, basis: Basis) -> Mapping[str, int]:
        capital = self._capital
        return {
            **super().fixed_lines(basis),
            "capital_loss_used": capital.loss_deduction,
            "capital_loss_carryover": capital.carryover_st + capital.carryover_lt,
        }

    def tax(self, taxable: int, income: Income) -> int:
        return (super().tax(taxable, income) + common.progressive_tax(taxable, self._surcharge)
                + common.progressive_tax(taxable, self._local))

    def tax_lines(self, taxable: int, income: Income) -> Mapping[str, int]:
        return {"local_tax": common.progressive_tax(taxable, self._local)}

    def memo(self, basis: Basis) -> Mapping[str, int]:
        memo = basis.federal.memo
        withdrawn = memo.get("hsa_withdrawn", 0) if self._hsa_taxed else 0
        return {
            "hsa_withdrawn": withdrawn,
            "hsa_growth": self._hsa_growth,
            # What the year drew from traditional money (an RMD), and the
            # gain it realized: a state that taxes an HSA counts its growth,
            # or fall, as gain (#490), but nothing was sold.
            "drawn_ordinary": memo.get("drawn_ordinary", 0) - withdrawn,
            "drawn_gain": max(0, self._capital.ltcg - self._hsa_growth),
        }


@register
class StateTables(StateFromFederal):
    """A state income tax from tables keyed by the state (``bracket_series``),
    starting from federal AGI."""
    kind = "state_tables"
    jurisdiction = "state"
    year_class = StateTablesYear
