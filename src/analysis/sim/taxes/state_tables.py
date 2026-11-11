"""The ``state_tables`` tax calculator (ADR-003 §2.6): a state that starts
from federal AGI (#508) and adjusts it by tables keyed by the state
the household lives in that year (``residence.csv``, #509): qualified dividends as ordinary income, an HSA it
taxes, its own capital-gain netting and exclusion, retirement exclusions and
a Social Security exclusion; then its deduction, brackets, gain tables
and surcharge; and its own deductions and credits (``state_adjustments``,
#511). A local tax on it is :mod:`analysis.sim.taxes.local`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
        bracket_aux = params.bracket_aux
        # Every table is the state's where the household lives on the year's
        # December 31 (#509): its brackets, gain tables and exclusion (#485).
        series = self.series = common.state_series(ty)
        self.brackets = common.brackets(ty, bracket_aux, series)
        self.ltcg_brackets = common.rate_table(ty, params.ltcg_bracket_aux, series)
        self.stcg_brackets = common.rate_table(ty, params.stcg_bracket_aux, series)
        self._net_ltcg = common.ltcg_exclusion(ty, params.ltcg_exclusion_aux, series)

        # A state that doesn't recognize an HSA (#490) taxes it like a
        # brokerage account: no deduction going in, its growth each year,
        # nothing coming out. Each year by the state it was filed in.
        taxed_years = [year for year in common.hsa_flow_years(ty)
                       if common.state_taxes_hsa(ty, common.state_series(ty, year), year)]
        hsa_growth = common.hsa_growth_by_owner(ty, taxed_years)
        self._hsa_taxed = ty.year in taxed_years
        self._hsa_growth = sum(hsa_growth.get(ty.year, {}).values())
        # The state nets its own gains: an HSA's growth is gain to it.
        capital = self._capital = common.capital_gains(
            ty, loss_limit=common.loss_limit(ty), hsa_growth=hsa_growth)

        # A state's retirement income exclusions (#486) come off before AGI,
        # as the states' own subtractions from income do, after its gain
        # exclusion.
        exclusion_aux = params.retirement_exclusion_aux
        self._exclusion = common.retirement_exclusion(
            ty, exclusion_aux, series,
            net_gain=capital.stcg + self._net_ltcg(capital.ltcg), hsa_taxed=self._hsa_taxed)

        # A state taxes Social Security only by its state_social_security
        # table (#484).
        state_ss: dict[str, Any] | None = None
        if ty.has_table(registries.STATE_SOCIAL_SECURITY, series):
            rows = [r for r in ty.indexed(registries.STATE_SOCIAL_SECURITY, series)
                    if r.get("filing_status") == ty.filing_status]
            if not rows:
                raise TaxError(
                    f"{where}: no {registries.STATE_SOCIAL_SECURITY}__"
                    f"{series} row for filing_status={ty.filing_status!r} in {ty.year}")
            state_ss = rows[0]
        self._state_ss = state_ss

        # A state's surcharge on high incomes (#490) adds its rates to the
        # brackets'. Its own bands, rather than more bracket rows, because
        # its floor needn't be indexed or fall between the same brackets for
        # every filing status (California's $1 million). A local income tax
        # is its own jurisdiction (local_bands, #508).
        self._surcharge = common.rate_table(ty, registries.STATE_SURCHARGE, series)

        # The state's own deductions and credits (#511, #488): a renter's
        # deduction, an exemption, a deduction for federal tax paid. Each is
        # a named line at its step.
        self._adjustments = common.state_adjustments(ty, params.adjustments_aux, series)

    def deductions(self) -> Mapping[str, int]:
        ty, params = self.ty, self.ty.params
        standard_aux = params.standard_deduction_aux
        # A state's standard deduction is keyed by state, like its brackets
        # (state_standard_deduction__CA.csv).
        series = self.series if standard_aux == registries.STATE_STANDARD_DEDUCTION else None
        if series is not None and not ty.has_table(standard_aux, series):
            # The loader warned: the federal figure stands in for the state's.
            standard_aux, series = "standard_deduction", None
        standard = common.standard_deduction(ty, standard_aux, series)
        # state_deductions falls back to federal_deductions when the
        # scenario has no state_deductions rows at all: most states don't
        # need their own file.
        itemized_aux = params.itemized_deduction_aux
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
        ss = Adjustment("ss_difference", ss=income.ss - self._taxable_ss(basis))
        # The table's subtractions come off ordinary income, tested against
        # the income before them, and never take it below zero.
        left = income.ordinary - exclusion.ordinary - retirement.ordinary - retirement.stcg
        test = income.agi - exclusion.value - retirement.value - ss.value
        own = []
        for line, amount in self._adjusted("subtraction", test, basis).items():
            amount = min(amount, max(0, left))
            left -= amount
            own.append(Adjustment(line, ordinary=amount))
        return (exclusion, retirement, ss, *own)

    def _adjusted(self, step: str, income: int, basis: Basis) -> dict[str, int]:
        """The table's lines at ``step``, after the household income test on
        ``income``."""
        out = {}
        for a in self._adjustments:
            if a.step != step:
                continue
            figure = a.figure
            if a.base == "federal_tax":
                figure = a.from_federal(basis.federal["tax"])
            elif a.base == "federal_deduction":
                figure = a.from_federal(basis.federal["deduction"])
            out[a.name] = a.after_income_test(figure, income)
        return out

    def other_deductions(self, income: Income, basis: Basis) -> Mapping[str, int]:
        return self._adjusted("deduction", income.agi, basis)

    def credit_lines(self, tax: int, income: Income, basis: Basis) -> Mapping[str, int]:
        return self._adjusted("credit", income.agi, basis)

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
        # The state nets its own gains, so its capital-loss lines replace
        # the federal ones as a group.
        return {
            **{line: amount for line, amount in super().fixed_lines(basis).items()
               if line not in common.CAPITAL_LINES},
            **common.capital_lines(self._capital),
        }

    def tax(self, taxable: int, income: Income) -> int:
        return super().tax(taxable, income) + common.progressive_tax(taxable, self._surcharge)

    def memo(self, basis: Basis) -> Mapping[str, int]:
        memo = basis.federal.memo
        withdrawn = memo.get("hsa_withdrawn", 0) if self._hsa_taxed else 0
        return {
            "hsa_withdrawn": withdrawn,
            "hsa_growth": self._hsa_growth,
            # What the year drew from traditional money (an RMD), and the
            # gain it realized: a state that taxes an HSA counts its growth,
            # or fall, as gain (#490), but nothing was sold, and nor is a
            # reinvested gain distribution (#547).
            "drawn_ordinary": memo.get("drawn_ordinary", 0) - withdrawn,
            "drawn_gain": max(0, self._capital.ltcg - self._hsa_growth - common.gain_distributions(self.ty)),
        }


@register
class StateTables(StateFromFederal):
    """A state income tax from tables keyed by the state the household lives
    in (``residence.csv``), starting from federal AGI."""
    kind = "state_tables"
    jurisdiction = "state"
    year_class = StateTablesYear

    @dataclass(frozen=True)
    class Params:
        """Each ``_aux`` param names the aux table read in place of the
        usual one, keyed by the state the household lives in like it. A
        calculator built on this one extends these (``class
        Params(StateTables.Params)``)."""
        filing_status_aux: str = "filing_status"
        bracket_aux: str = "state_brackets"
        standard_deduction_aux: str = registries.STATE_STANDARD_DEDUCTION
        itemized_deduction_aux: str = "state_deductions"
        retirement_exclusion_aux: str = registries.STATE_RETIREMENT_EXCLUSION
        ltcg_bracket_aux: str = registries.STATE_LTCG_BRACKETS
        stcg_bracket_aux: str = registries.STATE_STCG_BRACKETS
        ltcg_exclusion_aux: str = registries.STATE_LTCG_EXCLUSION
        adjustments_aux: str = registries.STATE_ADJUSTMENTS
        nol_limit_percent: int | None = None
        """How much of taxable income a carried-in NOL may offset; 100 by default."""
