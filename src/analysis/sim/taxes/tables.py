"""The table-driven income tax both built-in calculators compute (#507):
brackets, a standard or itemized deduction, Social Security taxation,
capital-loss netting, gain tables, a net operating loss, and for a state its
gain exclusion, retirement exclusions, HSA, surcharge and local tax
(ADR-003 §2.6).

``federal`` and ``state_tables`` share this code and differ by what
:attr:`TablesCalculator.state` turns on. A state that starts from the
federal return is #508's work."""

from __future__ import annotations

from collections.abc import Sequence
from types import MappingProxyType
from typing import Any, ClassVar

from analysis.sim import registries
from analysis.sim.tax_api import (
    NO_EXTRA, Extra, Prepared, Return, TaxCalculator, TaxError, TaxYear,
)
from analysis.sim.taxes import common


class TablesCalculator(TaxCalculator):
    """A tax computed from the scenario's aux tables."""
    state: ClassVar[bool] = False
    """A state's tax: its tables are keyed by its ``bracket_series``, and it
    has the state-only steps (dividends as ordinary income, the gain
    exclusion, retirement exclusions, an HSA it taxes, Social Security only
    by its own table, a surcharge and a local tax)."""
    default_nol_limit_percent: ClassVar[int] = 80

    def prepare(self, year: TaxYear) -> Prepared:
        return _TablesYear(self, year)


class _TablesYear(Prepared):
    """One tax year of a :class:`TablesCalculator`, ready to compute."""

    def __init__(self, calc: TablesCalculator, ty: TaxYear) -> None:
        params, state, name = ty.params, calc.state, ty.jurisdiction
        where = f"taxes/{name}.csv"
        standard_deduction_aux = str(params.get(
            "standard_deduction_aux",
            registries.STATE_STANDARD_DEDUCTION if state else "standard_deduction",
        ))
        ss_threshold_aux = str(params.get("ss_threshold_aux", "ss_taxation_thresholds"))
        ltcg_bracket_aux = str(params.get(
            "ltcg_bracket_aux", registries.STATE_LTCG_BRACKETS if state else "ltcg_brackets"))
        stcg_bracket_aux = params.get(
            "stcg_bracket_aux", registries.STATE_STCG_BRACKETS if state else None)
        ltcg_exclusion_aux = params.get(
            "ltcg_exclusion_aux", registries.STATE_LTCG_EXCLUSION if state else None)
        bracket_aux = str(params.get(
            "bracket_aux", "state_brackets" if state else "federal_brackets"))
        bracket_series = params.get("bracket_series")
        filing_status = ty.filing_status
        self.filing_status = filing_status

        if bracket_aux == "state_brackets" and bracket_series is None:
            # If exactly one state bracket series exists, default to it.
            state_series = ty.series_of("state_brackets")
            if len(state_series) == 1:
                bracket_series = state_series[0]
            elif len(state_series) > 1:
                raise TaxError(f"{where}: bracket_series required for state_brackets")

        def loss_limit(year: int) -> int:
            # A carryover from a year before the filing_status table starts (a
            # year-to-date loss) is limited as if filed with this year's status.
            status = filing_status if year == ty.year or not ty.table_as_of(
                str(params.get("filing_status_aux", "filing_status")), None, year,
            ) else ty.filing_status_in(year)
            row = ty.keyed("capital_loss_limit", "filing_status", status, year)
            if row is None:
                raise TaxError(
                    f"{where}: {year} has a net capital loss, but no "
                    f"capital_loss_limit row for filing_status={status!r} effective on "
                    f"or before {year}-12-31 (aux/capital_loss_limit.csv)"
                )
            return int(row["amount"])

        other_ordinary, ss_income, qualified_dividends = common.income_buckets(ty)
        business = common.business_result(ty)
        hsa_growth: dict[int, int] = {}
        hsa_withdrawn = 0
        hsa_taxed = False
        if state and bracket_series is not None:
            # A state that doesn't recognize an HSA (#490) taxes it like a
            # brokerage account: no deduction going in, its growth each year,
            # nothing coming out.
            taxed_years = [
                year for year in common.hsa_flow_years(ty)
                if common.state_taxes_hsa(ty, bracket_series, year)
            ]
            hsa_growth = common.hsa_growth_by_year(ty, taxed_years)
            hsa_taxed = ty.year in taxed_years
            if hsa_taxed:
                contributed, hsa_withdrawn = common.hsa_deferred_flows(ty)
                other_ordinary += contributed - hsa_withdrawn
        capital = common.capital_gains(ty, loss_limit=loss_limit, extra_ltcg=hsa_growth)
        # A Schedule C loss offsets the year's other income (#231).
        ordinary_income = other_ordinary + business.net + capital.stcg - capital.loss_deduction
        # A state's gain tables and exclusion (#485) are keyed by state, like its
        # standard deduction, so they follow bracket_series.
        gain_series = bracket_series if state else None
        exclusion_rows = ty.table(str(ltcg_exclusion_aux), gain_series) if ltcg_exclusion_aux else []

        def net_ltcg(gain: int) -> int:
            """The long-term gain ``gain`` less what the state excludes of it."""
            if not exclusion_rows or gain <= 0:
                return gain
            row = exclusion_rows[0]
            excluded = gain * int(row["exclusion_micros"]) // 1_000_000
            if row.get("max_exclusion") is not None:
                excluded = min(excluded, int(row["max_exclusion"]))
            excluded = max(excluded, int(row.get("min_exclusion") or 0))
            return gain - min(excluded, gain)

        # Qualified dividends are taxed with long-term gains (IRC §1(h)(11)),
        # but a capital loss doesn't offset them: they join after the
        # netting. A state has no such preference: its gain rules (#485) are
        # for gains, so its dividends are ordinary income.
        if state:
            gain_dividends = 0
            ordinary_income += qualified_dividends
        else:
            gain_dividends = qualified_dividends
        self._net_ltcg = net_ltcg
        self._gain_dividends = gain_dividends
        self._capital = capital
        self._gross_ordinary = ordinary_income
        # A state's retirement income exclusions (#486) come off before AGI,
        # as the states' own subtractions from income do, after its gain
        # exclusion. Keyed by state, like the standard deduction below.
        exclusion_aux = str(params.get(
            "retirement_exclusion_aux", registries.STATE_RETIREMENT_EXCLUSION))
        self._exclusion = (
            common.retirement_exclusion(
                ty, exclusion_aux, bracket_series,
                net_gain=capital.stcg + net_ltcg(capital.ltcg), hsa_taxed=hsa_taxed)
            if state and bracket_series is not None
            else common.RetirementExclusion()
        )

        # A state taxes Social Security only by its state_social_security
        # table (#484), keyed by state like its brackets.
        state_ss: dict[str, Any] | None = None
        if state and bracket_series is not None and ty.has_table(
                registries.STATE_SOCIAL_SECURITY, bracket_series):
            rows = [
                r for r in ty.indexed(registries.STATE_SOCIAL_SECURITY, bracket_series)
                if r.get("filing_status") == filing_status
            ]
            if not rows:
                raise TaxError(
                    f"{where}: no {registries.STATE_SOCIAL_SECURITY}__"
                    f"{bracket_series} row for filing_status={filing_status!r} in {ty.year}"
                )
            state_ss = rows[0]
        self._state_ss = state_ss
        self._ss_income = ss_income
        self._ss_tiers: tuple[int, int] | None = None
        if (not state or state_ss is not None) and ss_income > 0:
            thresholds = [r for r in ty.table(ss_threshold_aux)
                          if r.get("filing_status") == filing_status]
            if not thresholds:
                raise TaxError(f"{where}: missing ss thresholds for filing_status={filing_status!r}")
            self._ss_tiers = (int(thresholds[0]["tier1_floor"]), int(thresholds[0]["tier2_floor"]))

        # A state's standard deduction is keyed by state, like its brackets
        # (state_standard_deduction__CA.csv), so it follows bracket_series.
        deduction_series = (
            bracket_series if standard_deduction_aux == registries.STATE_STANDARD_DEDUCTION
            else None
        )
        if deduction_series is not None and not ty.has_table(standard_deduction_aux, deduction_series):
            # The loader warned: the federal figure stands in for the state's.
            standard_deduction_aux, deduction_series = "standard_deduction", None
        deduction_rows = [r for r in ty.indexed(standard_deduction_aux, deduction_series)
                          if r.get("filing_status") == filing_status]
        if not deduction_rows:
            raise TaxError(f"{where}: missing standard deduction for filing_status={filing_status!r}")
        standard_deduction = int(deduction_rows[0]["amount"])

        # Itemized deductions (optional): a dated total, no filing_status
        # split. state_deductions falls back to federal_deductions when the
        # scenario has no state_deductions rows at all -- most states don't
        # need their own file.
        itemized_deduction_aux = str(params.get(
            "itemized_deduction_aux", "state_deductions" if state else "federal_deductions"))
        if state and not ty.has_table(itemized_deduction_aux):
            itemized_deduction_aux = "federal_deductions"
        # A row is one tax year's itemized total, so it counts for the year
        # it's dated in only, never carried into the years after (#370).
        itemized_rows = [row for effective, row in ty.dated(itemized_deduction_aux)
                         if effective[:4] == str(ty.year)]
        itemized_deduction = int(itemized_rows[-1]["amount"]) if itemized_rows else 0
        self._deduction = deduction = max(standard_deduction, itemized_deduction)

        bracket_rows = [r for r in ty.indexed(bracket_aux, bracket_series)
                        if r.get("filing_status") == filing_status]
        if not bracket_rows:
            raise TaxError(
                f"{where}: missing brackets kind={bracket_aux!r} "
                f"series={bracket_series!r} filing_status={filing_status!r}"
            )
        self.brackets = sorted(
            (int(r["floor"]), int(r["marginal_rate_micros"])) for r in bracket_rows)

        def gain_brackets(kind: str | None) -> list[tuple[int, int]]:
            if kind is None:
                return []
            return sorted(
                (int(r["floor"]), int(r["marginal_rate_micros"]))
                for r in ty.indexed(str(kind), gain_series)
                if r.get("filing_status") == filing_status
            )

        self.ltcg_brackets = gain_brackets(ltcg_bracket_aux)
        self._stcg_brackets = gain_brackets(stcg_bracket_aux)

        def state_bands(kind: str) -> list[tuple[int, int]]:
            """A state's bands of ``kind`` keyed by its brackets' series,
            whose rates add to the brackets'; none for the federal tax."""
            if not state or bracket_series is None:
                return []
            return sorted(
                (int(r["floor"]), int(r["marginal_rate_micros"]))
                for r in ty.indexed(kind, bracket_series)
                if r.get("filing_status") == filing_status
            )

        # A state's surcharge on high incomes (#490) adds its rates to the
        # brackets'. Its own bands, rather than more bracket rows, because
        # its floor needn't be indexed or fall between the same brackets for
        # every filing status (California's $1 million). A local income tax
        # (#487) is bands on the state's taxable income too, reported apart
        # from it.
        self._surcharge = state_bands(registries.STATE_SURCHARGE)
        self._local = state_bands(registries.STATE_LOCAL_TAX)

        # A net operating loss carried in offsets part of this year's
        # taxable income (#231). It doesn't lower AGI first, as the NOL
        # deduction really does, so taxable Social Security can come out a
        # little high in a year that uses one.
        nol_limit_percent = int(params.get(
            "nol_limit_percent", 100 if state else calc.default_nol_limit_percent))
        if not 0 <= nol_limit_percent <= 100:
            raise TaxError(f"{where}: nol_limit_percent must be 0 to 100, got {nol_limit_percent}")
        self._nol_limit_percent = nol_limit_percent
        self._nol_in = ty.prior("nol_carryover")

        ordinary, ltcg, _stcg = self._net_income(0, 0)
        excluded = self._gross_ordinary + self._taxed_ltcg(0) - ordinary - ltcg
        taxable_ss = self._taxable_ss_for(0, 0)
        # The loss that carries: the business loss less the income it
        # absorbed. The deduction never adds to it (a nonbusiness deduction
        # offsets only nonbusiness income, §172(d)(4)), nor does the
        # capital-loss deduction.
        nonbusiness_income = (other_ordinary + capital.stcg + self._taxed_ltcg(0)
                              + qualified_dividends - gain_dividends + taxable_ss - excluded)
        self._nol_new = max(0, -business.net - max(0, nonbusiness_income - deduction))
        self._fixed = {
            "business_net": business.net,
            "hobby_disallowed": business.disallowed,
            "ltcg_exclusion": capital.ltcg - net_ltcg(capital.ltcg),
            "capital_loss_used": capital.loss_deduction,
            "capital_loss_carryover": capital.carryover_st + capital.carryover_lt,
            "ss_benefits": ss_income,
            "standard_deduction": standard_deduction,
            "itemized_deduction": itemized_deduction,
            "deduction": deduction,
        }
        self._state = state
        growth = hsa_growth.get(ty.year, 0)
        # A Return shares the memo, so it must not be writable.
        self._memo = MappingProxyType({
            "hsa_withdrawn": hsa_withdrawn,
            "hsa_growth": growth,
            # What the year drew from traditional money (an RMD), and the
            # gain it realized: a state that taxes an HSA counts its growth,
            # or fall, as gain (#490), but nothing was sold.
            "drawn_ordinary": ty.total("tax_deferred_out") - hsa_withdrawn,
            "drawn_gain": max(0, capital.ltcg - growth),
        })

    def _taxed_ltcg(self, extra_gain: int) -> int:
        """The income taxed with long-term gain, with ``extra_gain`` more gain."""
        return self._net_ltcg(self._capital.ltcg + extra_gain) + self._gain_dividends

    def _net_income(self, extra_ordinary: int, extra_gain: int) -> tuple[int, int, int]:
        """Ordinary income, the income taxed with long-term gain, and the
        net short-term gain within the ordinary income, with more ordinary
        income and long-term gain on top, less the retirement exclusions."""
        capital = self._capital
        ordinary, ltcg = self._gross_ordinary + extra_ordinary, self._taxed_ltcg(extra_gain)
        # Gain taken away (the drawdown estimate replaces posted gain) can't
        # be left out; gain added isn't in anyone's income by kind.
        net_gain = min(self._exclusion.net_gain,
                       capital.stcg + self._net_ltcg(capital.ltcg + extra_gain))
        off_ordinary, off_ltcg, off_short = self._exclusion.split(ordinary, ltcg, net_gain)
        return ordinary - off_ordinary, ltcg - off_ltcg, capital.stcg - off_short

    def _taxable_ss_for(self, extra_ordinary: int, extra_gain: int) -> int:
        """Taxable Social Security with more ordinary income and long-term
        gain on top. It starts from the federally taxable amount, so the
        income it reads (and a state's phase-out by federal AGI) comes
        before the retirement exclusions (#486), which only a state
        subtracts."""
        if self._ss_tiers is None:
            return 0
        ordinary, ltcg = self._gross_ordinary + extra_ordinary, self._taxed_ltcg(extra_gain)
        taxable = common.taxable_social_security(
            ordinary_income=ordinary, ltcg_income=ltcg, ss_income=self._ss_income,
            tier1=self._ss_tiers[0], tier2=self._ss_tiers[1],
        )
        if self._state_ss is None:
            return taxable
        return taxable - common.state_ss_exclusion(self._state_ss, taxable, ordinary + ltcg + taxable)

    def _tax_on(self, taxable: int, ltcg: int, stcg: int, local: int) -> int:
        """The brackets' tax on ``taxable`` income, ``ltcg`` of it long-term
        gain and up to ``stcg`` more of it short-term gain, plus the
        ``local`` tax on it."""
        brackets, ltcg_brackets, stcg_brackets = self.brackets, self.ltcg_brackets, self._stcg_brackets
        extra = common.progressive_tax(taxable, self._surcharge) + local
        # Long-term gains sit on top of ordinary taxable income and are
        # taxed at the preferential rates for the band each part lands in; a
        # state's short-term gain rates (#485) take the slice just below
        # them. A gain without a table of its own keeps its place, at the
        # ordinary rates.
        if not (ltcg_brackets or stcg_brackets):
            return common.progressive_tax(taxable, brackets) + extra
        gain_taxable = min(max(0, ltcg), taxable)
        below_gain = taxable - gain_taxable
        short_taxable = min(stcg, below_gain) if stcg_brackets else 0
        ordinary_taxable = below_gain - short_taxable
        tax = common.progressive_tax(ordinary_taxable, brackets)
        if short_taxable > 0:
            tax += _band_tax(ordinary_taxable, below_gain, stcg_brackets)
        if gain_taxable > 0:
            tax += _band_tax(below_gain, taxable, ltcg_brackets or brackets)
        return tax + extra

    def compute(self, extra: Extra = NO_EXTRA) -> Return:
        ordinary, ltcg, stcg = self._net_income(extra.ordinary, extra.ltcg)
        taxable_ss = self._taxable_ss_for(extra.ordinary, extra.ltcg)
        before_nol = ordinary + ltcg + taxable_ss - self._deduction
        # More income can use more of a carried-in NOL under its limit, so
        # that is worked out again.
        nol_used = min(self._nol_in, max(0, before_nol) * self._nol_limit_percent // 100)
        taxable = max(0, before_nol - nol_used)
        below_gain = taxable - min(ltcg, taxable)
        # A state's separately taxed short-term gain (#485) isn't ordinary.
        short = min(stcg, below_gain) if self._stcg_brackets else 0
        agi = ordinary + ltcg + taxable_ss
        local = common.progressive_tax(taxable, self._local)
        lines = {
            "ordinary_income": ordinary,
            "ltcg_taxed": ltcg,
            "nol_used": nol_used,
            "nol_carryover": self._nol_in - nol_used + self._nol_new,
            "ss_taxable": taxable_ss,
            "agi": agi,
            **self._fixed,
            "taxable_income": taxable,
            "ordinary_taxable": below_gain - short,
            "tax": self._tax_on(taxable, ltcg, stcg, local),
        }
        if self._state:
            lines["retirement_exclusion"] = (
                self._gross_ordinary + extra.ordinary + self._taxed_ltcg(extra.ltcg) - ordinary - ltcg)
            lines["local_tax"] = local
        else:
            # IRMAA's MAGI (#253): AGI plus tax-exempt interest, which the
            # model has no character for yet (#265), so it is AGI.
            lines["magi"] = agi
        return Return(lines, self._memo)


def _band_tax(low: int, high: int, table: Sequence[tuple[int, int]]) -> int:
    return common.progressive_tax(high, table) - common.progressive_tax(low, table)
