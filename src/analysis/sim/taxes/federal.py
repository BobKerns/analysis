"""The ``federal`` tax calculator (ADR-003 §2.6): brackets, a standard or
itemized deduction, Social Security taxation, capital-loss netting, long-term
gains and qualified dividends at ``ltcg_brackets``' rates, and a net
operating loss.

Its memo carries what a state that starts from federal AGI (#508) adds back
or reclassifies: income federal AGI leaves out, and the parts AGI is made
of."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import ClassVar

from analysis.sim import registries
from analysis.sim.tax_api import (
    NO_EXTRA, Extra, Prepared, Return, TaxCalculator, TaxError, TaxYear, register,
)
from analysis.sim.taxes import common


@register
class Federal(TaxCalculator):
    """The federal income tax: ``federal_brackets``, the standard deduction
    or itemized deductions, Social Security taxation, long-term gains and
    qualified dividends at ``ltcg_brackets``' rates, and an 80% NOL limit."""
    kind = "federal"
    jurisdiction = "federal"
    default_nol_limit_percent: ClassVar[int] = 80

    @dataclass(frozen=True)
    class Params:
        """Each ``_aux`` param names the aux table read in place of the
        usual one (``federal_brackets``, ...)."""
        filing_status_aux: str = "filing_status"
        bracket_aux: str = "federal_brackets"
        bracket_series: str | None = None
        """The series of the federal tables (``federal_brackets__<series>``)."""
        standard_deduction_aux: str = "standard_deduction"
        itemized_deduction_aux: str = "federal_deductions"
        ltcg_bracket_aux: str = "ltcg_brackets"
        stcg_bracket_aux: str | None = None
        """A table of separate short-term gain rates; none federally."""
        ltcg_exclusion_aux: str | None = None
        """A table excluding part of the long-term gain; none federally."""
        ss_threshold_aux: str = "ss_taxation_thresholds"
        nol_limit_percent: int | None = None
        """How much of taxable income a carried-in NOL may offset; 80 by default."""

    def prepare(self, year: TaxYear) -> Prepared:
        return FederalYear(self, year)


class FederalYear(Prepared):
    """One federal tax year, ready to compute."""

    def __init__(self, calc: Federal, ty: TaxYear) -> None:
        params = ty.params
        self.filing_status = ty.filing_status
        bracket_aux = params.bracket_aux
        series = common.bracket_series(ty)
        self.brackets = common.brackets(ty, bracket_aux, series)
        self.ltcg_brackets = common.rate_table(ty, params.ltcg_bracket_aux)
        stcg_aux = params.stcg_bracket_aux
        self._stcg_brackets = common.rate_table(ty, None if stcg_aux is None else str(stcg_aux))
        exclusion_aux = params.ltcg_exclusion_aux
        self._net_ltcg = common.ltcg_exclusion(
            ty, None if exclusion_aux is None else str(exclusion_aux), None)

        other_ordinary, ss_income, qualified_dividends = common.income_buckets(ty)
        business = common.business_result(ty)
        capital = common.capital_gains(ty, loss_limit=common.loss_limit(ty))
        self._capital = capital
        # A Schedule C loss offsets the year's other income (#231).
        self._ordinary = other_ordinary + business.net + capital.stcg - capital.loss_deduction
        # Qualified dividends are taxed with long-term gains (IRC §1(h)(11)),
        # but a capital loss doesn't offset them: they join after the netting.
        self._dividends = qualified_dividends
        self._ss_income = ss_income
        self._ss_tiers: tuple[int, int] | None = None
        if ss_income > 0:
            status = ty.filing_status
            thresholds = [r for r in ty.table(params.ss_threshold_aux)
                          if r.get("filing_status") == status]
            if not thresholds:
                raise TaxError(f"{common.where(ty)}: missing ss thresholds for filing_status={status!r}")
            self._ss_tiers = (int(thresholds[0]["tier1_floor"]), int(thresholds[0]["tier2_floor"]))

        standard = common.standard_deduction(
            ty, params.standard_deduction_aux)
        itemized = common.itemized_deduction(
            ty, params.itemized_deduction_aux)
        self._deduction = deduction = max(standard, itemized)
        # A net operating loss carried in offsets part of this year's
        # taxable income (#231). It doesn't lower AGI first, as the NOL
        # deduction really does, so taxable Social Security can come out a
        # little high in a year that uses one.
        self._nol_limit = common.nol_limit_percent(ty, calc.default_nol_limit_percent)
        self._nol_in = ty.prior("nol_carryover")
        self._nol_lapse = common.nol_lapse_share(ty)
        self._nol_new = common.nol_new(
            agi=self._agi(NO_EXTRA), business_net=business.net,
            capital_loss_used=capital.loss_deduction, deduction=deduction)
        self._fixed = {
            "business_net": business.net,
            "hobby_disallowed": business.disallowed,
            "ltcg_exclusion": capital.ltcg - self._net_ltcg(capital.ltcg),
            **common.capital_lines(capital),
            "ss_benefits": ss_income,
            "standard_deduction": standard,
            "itemized_deduction": itemized,
            "deduction": deduction,
        }
        hsa_contributed, hsa_withdrawn = common.hsa_deferred_flows(ty)
        self._memo = {
            # What the year drew from traditional money (an RMD), and the
            # gain it realized by selling: a reinvested gain distribution
            # (#547) took nothing out of a lot's embedded gain.
            "drawn_ordinary": ty.total("tax_deferred_out"),
            "drawn_gain": max(0, capital.ltcg - common.gain_distributions(ty)),
            # The parts of AGI, for a state that taxes them its own way.
            "net_stcg": capital.stcg,
            "net_ltcg": capital.ltcg,
            "qualified_dividends": qualified_dividends,
            # Money federal AGI leaves out or deducts, for a state to add
            # back by name rather than reading the ledger (#508).
            "hsa_contributed": hsa_contributed,
            "hsa_withdrawn": hsa_withdrawn,
            "excluded_gain": ty.total("excluded_gain"),
            "roth_qualified": sum(i.amount for i in ty.year_items("tax_free")
                                  if i.payer.subkind == registries.RETIREMENT_SUBKIND),
        }

    def _taxed_ltcg(self, extra_gain: int) -> int:
        """The income taxed with long-term gain, with ``extra_gain`` more gain."""
        return self._net_ltcg(self._capital.ltcg + extra_gain) + self._dividends

    def _taxable_ss(self, ordinary: int, ltcg: int) -> int:
        if self._ss_tiers is None:
            return 0
        return common.taxable_social_security(
            ordinary_income=ordinary, ltcg_income=ltcg, ss_income=self._ss_income,
            tier1=self._ss_tiers[0], tier2=self._ss_tiers[1],
        )

    def _agi(self, extra: Extra) -> int:
        ordinary, ltcg = self._ordinary + extra.ordinary, self._taxed_ltcg(extra.ltcg)
        return ordinary + ltcg + self._taxable_ss(ordinary, ltcg)

    def compute(self, extra: Extra = NO_EXTRA) -> Return:
        ordinary, ltcg = self._ordinary + extra.ordinary, self._taxed_ltcg(extra.ltcg)
        taxable_ss = self._taxable_ss(ordinary, ltcg)
        agi = ordinary + ltcg + taxable_ss
        before_nol = agi - self._deduction
        nol_used = common.nol_used(self._nol_in, before_nol, self._nol_limit)
        taxable = max(0, before_nol - nol_used)
        stcg = self._capital.stcg
        lines = {
            "ordinary_income": ordinary,
            "ltcg_taxed": ltcg,
            "nol_used": nol_used,
            **common.nol_carried(self._nol_in - nol_used + self._nol_new, self._nol_lapse),
            "ss_taxable": taxable_ss,
            "agi": agi,
            **self._fixed,
            "taxable_income": taxable,
            "ordinary_taxable": common.ordinary_taxable(taxable, ltcg, stcg, bool(self._stcg_brackets)),
            "tax": common.stacked_tax(
                taxable, ltcg, stcg, self.brackets, self.ltcg_brackets, self._stcg_brackets),
            # IRMAA's MAGI (#253): AGI plus tax-exempt interest, which the
            # model has no character for yet (#265), so it is AGI.
            "magi": agi,
        }
        # A Return shares the memo, so it must not be writable.
        return Return(lines, MappingProxyType(
            {**self._memo, "ss_untaxed": self._ss_income - taxable_ss}))
