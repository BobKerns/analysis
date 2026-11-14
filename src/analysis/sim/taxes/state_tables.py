"""The ``state_tables`` tax calculator (ADR-003 §2.6): a state that starts
from federal AGI (#508) and adjusts it by tables keyed by the state
the household lives in that year (``residence.csv``, #509): qualified dividends as ordinary income, an HSA it
taxes, its own capital-gain netting and exclusion, retirement exclusions and
a Social Security exclusion; then its deduction, brackets, gain tables
and surcharge; its own deductions and credits (``state_adjustments``,
#511); and a tax on long-term gains apart from its income tax
(``state_gains_tax``, #489). A local tax on it is
:mod:`analysis.sim.taxes.local`."""

from __future__ import annotations

import sys
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from functools import cached_property
from typing import Any

from analysis.sim import registries
from analysis.sim.tax_api import NO_EXTRA, Extra, TaxError, register
from analysis.sim.taxes import common
from analysis.sim.taxes.state_base import (
    Adjustment, Basis, FederalStateYear, Income, Joint, Separate, StateFromFederal,
)


ITEMIZED_LINES_API = 5
"""The API version that added :meth:`StateTablesYear.itemized_lines` (#542)."""

SEPARATE_API = 6
"""The API version that added :meth:`StateTablesYear.separate` (#580)."""

SEPARATE = "MFS"
"""The filing status of each spouse filing separately on one return (#580)."""


@dataclass(frozen=True)
class _Exclusion(Adjustment):
    """The retirement exclusion's line, with whose it is (#580): each
    person's grants (total, and how much of it is gain), which reach a
    separate return on :class:`Joint` with the subtractions."""
    grants: Mapping[str, tuple[int, int]] = field(default_factory=dict)


class StateTablesYear(FederalStateYear):
    """One year of a :class:`StateTables` state."""

    # A subclass whose setup doesn't call this one's files jointly (#580).
    _spouses: tuple[str, ...] = ()

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
        # brokerage account (#497): no deduction going in, its dividends and
        # interest the year they're paid, and its gain over basis when it's
        # withdrawn, but not the rest of what comes out, which it taxed
        # already. Each year by the state it was filed in.
        taxed_years = [year for year in common.hsa_flow_years(ty)
                       if common.state_taxes_hsa(ty, common.state_series(ty, year), year)]
        # This year's own flag doesn't need a flow: the after-tax drawdown
        # prices a quiet HSA's gain over basis too.
        self._hsa_taxed = common.state_taxes_hsa(ty, series, ty.year)
        # What the HSA paid this year, which every compute reads.
        self._hsa_dividends = sum(common.hsa_dividends(ty).values()) if self._hsa_taxed else 0
        self._distributed = common.gain_distributions(ty) + (
            common.hsa_gain_distributions(ty) if self._hsa_taxed else 0)
        # The state nets its own gains: an HSA's are gains to it.
        capital = self._capital = common.capital_gains(
            ty, loss_limit=common.loss_limit(ty), hsa_years=taxed_years)

        # A tax on long-term gains apart from the income tax (#489):
        # Washington's, on the federal net long-term gain (Schedule D line
        # 15) less real estate's, after its own deduction. Its rates are
        # bands like the surcharge's; without them there is none. Line 15
        # is the federal netting's, without a taxed HSA's gains.
        self._gains_bands = common.rate_table(ty, params.gains_tax_aux, series)
        deduction = [r for r in ty.indexed(params.gains_deduction_aux, series)
                     if r.get("filing_status") == ty.filing_status]
        self._gains_deduction = int(deduction[0]["amount"]) if deduction else 0
        self._real_estate_gain = common.real_estate_gain(ty)
        self._line_15 = capital.long_term
        if self._gains_bands and taxed_years:
            self._line_15 = common.capital_gains(ty, loss_limit=common.loss_limit(ty)).long_term
        # The excise of the last what-if, which its credit reads too.
        self._excise: tuple[Basis, tuple[Mapping[str, int], int]] | None = None

        # A state's retirement income exclusions (#486) come off before AGI,
        # as the states' own subtractions from income do, after its gain
        # exclusion.
        exclusion_aux = params.retirement_exclusion_aux
        self._exclusion = common.retirement_exclusion(
            ty, exclusion_aux, series,
            net_gain=capital.stcg + self._taxed_ltcg(capital.ltcg, NO_EXTRA),
            hsa_taxed=self._hsa_taxed)

        # A state taxes Social Security only by its state_social_security
        # table (#484). Its rows may be age tiers (#581): each person's share
        # of the taxable amount, by their benefits, takes the row of the
        # highest min_age they reach, Colorado's 55 and 65 or Rhode Island's
        # full retirement age. A table without ages is one row for everyone.
        self._ss_tiers: dict[str, Mapping[str, Any] | None] | None = None
        if ty.has_table(registries.STATE_SOCIAL_SECURITY, series):
            rows = [r for r in ty.indexed(registries.STATE_SOCIAL_SECURITY, series)
                    if r.get("filing_status") == ty.filing_status]
            if not rows:
                raise TaxError(
                    f"{where}: no {registries.STATE_SOCIAL_SECURITY}__"
                    f"{series} row for filing_status={ty.filing_status!r} in {ty.year}")
            birthdates = common.household(ty)
            # No one to test (the loader warns): the household takes the
            # rows any age reaches.
            self._ss_tiers = {
                p: common.state_ss_row(rows, born, ty.year) for p, born in sorted(birthdates.items())
            } or {"": common.state_ss_row(rows, None, ty.year)}

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

        # A couple may file separately on one return (#580): each spouse
        # through the married-filing-separately rows alone.
        self._spouses = self._separate_spouses()
        if self._spouses:
            self._setup_separate()

    def _separate_spouses(self) -> tuple[str, ...]:
        """The two spouses, when the state where the household lives lets
        them file separately on one return this year (#580); none for a
        Single year, or a household that isn't two people."""
        ty, aux = self.ty, self.ty.params.separate_return_aux
        if (aux is None or ty.filing_status != "MFJ"
                or self._written_before(("deductions", "tax"), SEPARATE_API)):
            return ()
        rows = ty.table(aux, self.series)
        if not rows or not common._yes(rows[0].get("allowed")):
            return ()
        people = tuple(sorted(common.household(ty)))
        return people if len(people) == 2 else ()

    def _setup_separate(self) -> None:
        """Read the married-filing-separately tables, and how the couple's
        income divides: by whose it is, as the retirement exclusion's
        (joint income split evenly)."""
        ty, params, series = self.ty, self.ty.params, self.series
        self._mfs_brackets = common.brackets(ty, params.bracket_aux, series, SEPARATE)
        self._mfs_ltcg = common.rate_table(ty, params.ltcg_bracket_aux, series, SEPARATE)
        self._mfs_stcg = common.rate_table(ty, params.stcg_bracket_aux, series, SEPARATE)
        self._mfs_surcharge = common.rate_table(ty, registries.STATE_SURCHARGE, series, SEPARATE)
        self._mfs_standard = common.standard_deduction_row(
            ty, *self._standard_table(), SEPARATE)
        # Unclipped, so one spouse's business loss stays theirs rather than
        # leaving the other's income to the joint total.
        pools, ss = common.person_income(
            ty, self._spouses, hsa_taxed=self._hsa_taxed, clip=False)
        self._ordinary_own = {
            p: sum(v for kind, v in pools[p].items() if kind != "capital_gain")
            for p in self._spouses}
        self._ordinary_weights = {p: max(0, v) for p, v in self._ordinary_own.items()}
        self._gain_weights = {p: max(0, pools[p]["capital_gain"]) for p in self._spouses}
        # The spouses are the household, so these are the benefits the age
        # tiers (#581) divide by: one person_income call serves both.
        self._ss_benefits = self._ss_weights = dict(ss)
        # Each spouse's tables are read through ty's filing status as well
        # as self.filing_status, so a subclass's own table read in its tax
        # step (common.rate_table(self.ty, ...)) gets the MFS rows too.
        joint_status_in = ty.filing_status_in
        self._mfs_ty = replace(
            ty, _filing_status_in=lambda year: SEPARATE if year == ty.year
            else joint_status_in(year))
        self._adjustment_named = {a.name: a for a in self._adjustments}

    def _standard_table(self) -> tuple[str, str | None]:
        """The standard deduction table the year reads, and its series: a
        state's own is keyed by state, like its brackets
        (state_standard_deduction__CA.csv); without one the federal figure
        stands in (the loader warned). Both the joint return and each
        spouse's separate one (#580) read it."""
        aux = self.ty.params.standard_deduction_aux
        series = self.series if aux == registries.STATE_STANDARD_DEDUCTION else None
        if series is not None and not self.ty.has_table(aux, series):
            return "standard_deduction", None
        return aux, series

    def deductions(self) -> Mapping[str, int]:
        ty, params = self.ty, self.ty.params
        # Some states shrink their own with income (#578): deductions_at
        # phases it out at the year's AGI (_phase_out).
        standard = common.standard_deduction(ty, *self._standard_table())
        # state_deductions falls back to federal_deductions when the
        # scenario has no state_deductions rows at all: most states don't
        # need their own file.
        itemized_aux = params.itemized_deduction_aux
        if not ty.has_table(itemized_aux):
            itemized_aux = "federal_deductions"
        itemized = common.itemized_deduction(ty, itemized_aux)
        return {"standard_deduction": standard, "itemized_deduction": itemized,
                "deduction": max(standard, itemized)}

    def deductions_at(self, income: Income, basis: Basis) -> Mapping[str, int]:
        """:meth:`deductions`, with :meth:`itemized_lines` at the year's
        income added to its ``itemized_deduction`` (#542): the deduction
        taken is the larger of the standard deduction and the itemized
        total, and on top what :meth:`deductions` takes beyond either of
        its own (a subclass's exemption, which itemizing doesn't replace).
        A :meth:`deductions` that takes less than the larger of its own two
        caps the deduction there, which the lines don't raise. A subclass
        whose limit depends on the deduction with the lines (a proration)
        applies it by overriding this step. A new mapping each call, so an
        override may change what it's given."""
        base = self._deductions
        own = max(base.get("standard_deduction") or 0, base.get("itemized_deduction", 0))
        beside = max(0, base["deduction"] - own)
        standard = self._phased(base.get("standard_deduction"), income, basis)
        itemized = self._itemized
        lines: dict[str, int]
        if itemized is None:
            if standard is None or standard == base.get("standard_deduction"):
                return dict(base)
            lines = {"standard_deduction": standard,
                     "deduction": max(standard, base.get("itemized_deduction", 0)) + beside}
        else:
            lines = itemized.deduction_lines(income.agi, standard, beside=beside)
        if base["deduction"] < own:
            lines["deduction"] = min(lines["deduction"], base["deduction"])
        return {**base, **lines}

    @cached_property
    def _phase_out(self) -> Mapping[str, Any] | None:
        """The state's own standard deduction row in force when it phases
        out with income (#578), else None (as while the federal figure
        stands in for it). Read here, not in :meth:`deductions`, so a
        subclass that writes its own deductions keeps the phase-out."""
        ty, params = self.ty, self.ty.params
        if params.standard_deduction_aux != registries.STATE_STANDARD_DEDUCTION:
            return None
        if not ty.has_table(registries.STATE_STANDARD_DEDUCTION, self.series):
            return None
        row = common.standard_deduction_row(ty, registries.STATE_STANDARD_DEDUCTION, self.series)
        return row if row.get("phaseout_start") is not None else None

    def _phased(self, standard: int | None, income: Income, basis: Basis) -> int | None:
        """The ``standard`` deduction :meth:`deductions` gave, phased out at
        the year's AGI (#578), the state's or, as its row's ``phaseout_agi``
        says, the federal. The cut comes off what it gave, so a subclass's
        own additions to it stay."""
        row = self._phase_out
        if row is None or standard is None:
            return standard
        agi = basis.federal.agi if row.get("phaseout_agi") == "federal" else income.agi
        cut = int(row["amount"]) - common.phased_standard_deduction(row, agi)
        return max(0, standard - cut) if cut > 0 else standard

    def itemized_lines(self) -> common.Itemized | None:
        """The year's itemized deductions from spending (#542), under the
        state's own limits (``state_itemized_limits__<state>``), which
        :meth:`deductions_at` adds to :meth:`deductions`'. None for a state
        without them, whose rules the federal ones may not match, and
        without the federal ``itemized_limits`` table, which turns the lines
        off everywhere. ``itemized_limits_aux`` set to ``itemized_limits``
        takes the federal limits for the state. A subclass whose
        :meth:`deductions` figures its own Schedule A overrides this to
        return None. A state never deducts an income tax: not its own, and
        few another's. An HSA's medical payments count where the state taxes
        the HSA (#490): it never excluded that money."""
        ty, params = self.ty, self.ty.params
        if not ty.has_table(registries.ITEMIZED_LIMITS) or self._deductions_before_lines():
            return None
        limits_aux = params.itemized_limits_aux
        series = self.series if limits_aux == registries.STATE_ITEMIZED_LIMITS else None
        if not ty.has_table(limits_aux, series):
            return None
        return common.itemized(
            ty, common.itemized_limits(ty, limits_aux, series),
            other=self._deductions.get("itemized_deduction", 0), income_taxes=0,
            hsa_paid_counts=self._hsa_taxed)

    @cached_property
    def _itemized(self) -> common.Itemized | None:
        return self.itemized_lines()

    def _deductions_before_lines(self) -> bool:
        """Whether a subclass's :meth:`deductions` may have been written
        before the lines (API 5, #542): it may figure its own Schedule A,
        which the lines would count again, so it keeps the deduction it
        figures."""
        return self._written_before(("deductions",), ITEMIZED_LINES_API)

    def _written_before(self, steps: Sequence[str], api: int) -> bool:
        """Whether a household module written before API ``api`` overrides
        any of ``steps``: its module says so with an ``API_VERSION`` below
        ``api``, or by saying none, since a household module written before
        the change never needed one. Each step's version is the one of the
        module that wrote it, which a later subclass elsewhere may only
        inherit."""
        for name in steps:
            owner = next(c for c in type(self).__mro__ if name in vars(c))
            if owner is StateTablesYear or owner.__module__.startswith("analysis."):
                continue
            module = sys.modules.get(owner.__module__)
            if getattr(module, "API_VERSION", 0) < api:
                return True
        return False

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
            # went in, and doesn't tax what came out (#490), but for its gain.
            Adjustment("hsa_addback", ordinary=contributed - withdrawn),
            # It taxes an HSA's dividends and interest the year they're paid (#497).
            Adjustment("hsa_dividends", ordinary=self._hsa_dividends),
            # Other states' municipal bond interest (#265), as Schedule CA
            # adds it: the state taxes other states' bonds, not its own.
            Adjustment("muni_interest", ordinary=common.taxable_muni_interest(
                memo, None if self.ty.residence is None else self.ty.residence.state)),
            # Its own netting of gains and losses, with an HSA's gains (#497),
            # less the federal result.
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
        # A state with a gains tax taxes the gain it taxes in place of its
        # net long-term gain (#489): ESSB 6346 takes long-term gains and
        # losses out of federal AGI (a long-term loss's deduction from
        # ordinary income too) and adds back the gains tax's base, so real
        # estate's gain stays out, and a short-term loss doesn't offset it.
        swap = Adjustment("ltcg_for_gains_base")
        if self._gains_bands:
            swap = Adjustment("ltcg_for_gains_base", ordinary=-capital.loss_deduction_lt,
                              ltcg=ltcg - self._gains_base(extra))
        ordinary, ltcg = ordinary - swap.ordinary, ltcg - swap.ltcg
        # Gain taken away (the drawdown estimate replaces posted gain) can't
        # be left out; gain added isn't in anyone's income by kind.
        net_gain = min(self._exclusion.net_gain, capital.stcg + self._taxed_ltcg(gain, extra))
        off_ordinary, off_ltcg, off_short, grants = self._exclusion.split_by_person(
            ordinary, ltcg, net_gain)
        retirement = _Exclusion("retirement_exclusion", ordinary=off_ordinary - off_short,
                                stcg=off_short, ltcg=off_ltcg, grants=grants)
        ss = Adjustment("ss_difference", ss=income.ss - self._taxable_ss(basis))
        # The table's subtractions come off ordinary income, tested against
        # the income before them, and never take it below zero.
        left = ordinary - retirement.ordinary - retirement.stcg
        test = income.agi - exclusion.value - swap.value - retirement.value - ss.value
        own = []
        for line, amount in self._adjusted("subtraction", test, basis).items():
            amount = min(amount, max(0, left))
            left -= amount
            own.append(Adjustment(line, ordinary=amount))
        return (exclusion, *([swap] if self._gains_bands else []), retirement, ss, *own)

    def _adjusted(self, step: str, income: int, basis: Basis, *,
                  refundable: bool = False) -> dict[str, int]:
        """The table's lines at ``step``, after the household income test on
        ``income``: of credits, the ``refundable`` ones or the others."""
        out = {}
        for a in self._adjustments:
            if a.step != step or a.refundable != refundable:
                continue
            figure = a.figure
            if a.base == "federal_tax":
                figure = a.from_base(basis.federal["tax"])
            elif a.base == "federal_deduction":
                # Less the state income tax it deducted (#542): the
                # state's own tax mustn't grow its own credit.
                figure = a.from_base(
                    basis.federal["deduction"] - basis.federal.memo.get("income_tax_deducted", 0))
            elif a.base == "gains_tax":
                figure = a.from_base(self.excise(basis)[1])
            out[a.name] = a.after_income_test(figure, income)
        return out

    def other_deductions(self, income: Income, basis: Basis) -> Mapping[str, int]:
        return self._adjusted("deduction", income.agi, basis)

    def credit_lines(self, tax: int, income: Income, basis: Basis) -> Mapping[str, int]:
        return self._adjusted("credit", income.agi, basis)

    def refundable_credits(self, tax: int, income: Income, basis: Basis) -> Mapping[str, int]:
        """The table's ``refundable`` credits (#579), paid in full whatever
        the tax: Idaho's grocery credit. A calculator's own override merges
        them, ``{**super().refundable_credits(tax, income, basis), ...}``."""
        return self._adjusted("credit", income.agi, basis, refundable=True)

    @cached_property
    def _ss_benefits(self) -> dict[str, int]:
        """Each household person's Social Security benefits this year, by
        which the federally taxable amount divides (#581, #580)."""
        people = sorted(common.household(self.ty))
        return common.person_income(self.ty, people, hsa_taxed=self._hsa_taxed)[1]

    def _taxable_ss(self, basis: Basis) -> int:
        """The state's taxable Social Security: none, or by its table the
        federally taxable amount less its exclusion, phased out by federal
        AGI (#484), each person's share by their age (#581)."""
        tiers = self._ss_tiers
        if tiers is None:
            return 0
        rows = list(tiers.values())
        row = rows[0]
        if row is not None and all(r is row for r in rows):
            # Everyone on one row: the household's amount, phased as one.
            fed = basis.federal
            taxable = fed["ss_taxable"]
            return taxable - common.state_ss_exclusion(row, taxable, fed.agi)
        return sum(self._taxable_ss_by_person(basis).values())

    def _taxable_ss_by_person(self, basis: Basis) -> dict[str, int]:
        """Each person's taxable Social Security (#581): the federally
        taxable amount divided by benefits, less the exclusion of the row
        they reach. The shares on one row are phased out together at
        federal AGI (the loader allows a rate phase-out on one row only, so
        it isn't taken twice), so a row everyone reaches gives what one
        household figure does."""
        tiers = self._ss_tiers
        assert tiers is not None
        fed = basis.federal
        weights = self._ss_benefits if set(tiers) <= set(self._ss_benefits) else dict.fromkeys(tiers, 1)
        shares = common.apportion(fed["ss_taxable"], {p: weights[p] for p in tiers})
        groups: dict[int, list[str]] = {}
        for person, row in tiers.items():
            if row is not None:
                groups.setdefault(id(row), []).append(person)
        out = dict(shares)
        for people in groups.values():
            row = tiers[people[0]]
            assert row is not None
            group = {p: shares[p] for p in people}
            excluded = common.state_ss_exclusion(row, sum(group.values()), fed.agi)
            for p, amount in common.apportion(excluded, group).items():
                out[p] -= amount
        return out

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

    def separate(self, joint: Joint, basis: Basis) -> Separate | None:
        """Each spouse's tax filing separately on one return (#580): the
        joint return's income divided by whose it is (each kind by the
        spouses' shares of that kind; the retirement exclusion and the
        table's own subtractions to the spouse they're for), each spouse's
        own married-filing-separately standard deduction (phased out at
        their own AGI) where the joint return takes one, the itemized
        deduction by share of AGI, and :meth:`tax` with the married-filing-
        separately brackets, gain tables and surcharge. The carried-in NOL
        offsets each spouse's income in turn. The table's deductions and
        credits are the joint return's, divided by whose they are (a
        ``filers`` line to those who reached its age, a ``per_person``
        income line by each one's own), any other evenly; a credit takes no
        more than its spouse's tax. Income tests stay the household's."""
        if not self._spouses:
            return None
        people = self._spouses
        income_by = self._spouse_income(joint.income, joint.subtractions, basis)
        agi = {p: income_by[p].agi for p in people}
        fed_agi = common.apportion(basis.federal.agi, agi)

        # The deduction: the joint return's standard and itemized beside
        # what it takes on top of the larger (a subclass's exemption).
        deductions = joint.deductions
        standard = deductions.get("standard_deduction") or 0
        # Whether the state has a standard deduction at all, from its
        # figure before any phase-out: a joint return phased out to none
        # still leaves each spouse their own at their own AGI.
        has_standard = bool(self._deductions.get("standard_deduction"))
        itemized = deductions.get("itemized_deduction", 0)
        beside = common.split_cents(max(0, deductions["deduction"] - max(standard, itemized)), 2)
        itemized_by = common.apportion(itemized, agi)
        others_by = dict.fromkeys(people, 0)
        for line, amount in joint.others.items():
            for p, share in self._line_shares(line, amount).items():
                others_by[p] += share
        lines: dict[str, int] = {}
        taxes: dict[str, int] = {}
        total_deduction = total_taxable = ordinary_taxable = 0
        nol_left, nol_used = self._nol_in, 0
        for i, p in enumerate(people):
            # A state without a standard deduction of its own (only
            # exemptions) gives a spouse none either.
            own = self._spouse_standard(agi[p], fed_agi[p]) if has_standard else 0
            deduction = max(own, itemized_by[p]) + beside[i]
            before_nol = agi[p] - deduction - others_by[p]
            used = common.nol_used(nol_left, before_nol, self._nol_limit)
            nol_left, nol_used = nol_left - used, nol_used + used
            taxable = max(0, before_nol - used)
            with self._filing_separately():
                taxes[p] = self.tax(taxable, income_by[p])
            lines[f"separate_agi_{p}"] = agi[p]
            lines[f"separate_deduction_{p}"] = deduction
            lines[f"separate_taxable_income_{p}"] = taxable
            total_deduction += deduction
            total_taxable += taxable
            ordinary_taxable += common.ordinary_taxable(
                taxable, income_by[p].ltcg, income_by[p].stcg, bool(self._mfs_stcg))
        tax, credits, credit_lines = self._after_credits(taxes, joint, lines)
        return Separate(lines, tax, nol_used, deduction=total_deduction, taxable=total_taxable,
                        ordinary_taxable=ordinary_taxable, credits=credits,
                        credit_lines=credit_lines)

    @contextmanager
    def _filing_separately(self) -> Generator[None]:
        """:meth:`tax` with the married-filing-separately tables (#580), so
        a subclass's own tax step applies to each spouse's return too."""
        saved = (self.ty, self.filing_status, self.brackets, self.ltcg_brackets,
                 self.stcg_brackets, self._surcharge)
        self.ty, self.filing_status = self._mfs_ty, SEPARATE
        self.brackets, self.ltcg_brackets, self.stcg_brackets = (
            self._mfs_brackets, self._mfs_ltcg, self._mfs_stcg)
        self._surcharge = self._mfs_surcharge
        try:
            yield
        finally:
            (self.ty, self.filing_status, self.brackets, self.ltcg_brackets,
             self.stcg_brackets, self._surcharge) = saved

    def _spouse_income(self, income: Income, subtractions: Sequence[Adjustment],
                       basis: Basis) -> dict[str, Income]:
        """The joint return's ``income`` divided between the spouses
        (#580): before the retirement exclusion and the table's own
        subtractions, ordinary income by their shares of non-gain income,
        gains by their shares of gain, Social Security by their benefits;
        then each spouse's own exclusion (its gain off their gain, the rest
        off their other income) and own subtractions off theirs."""
        people = self._spouses
        # Whose the subtractions are, as they say: the retirement exclusion
        # by its grants, the table's own by their rows; a subclass's own
        # line is no one's in particular, and stays in the income divided.
        retirement: Adjustment = Adjustment("retirement_exclusion")
        grants: Mapping[str, tuple[int, int]] = {}
        subtracted: dict[str, int] = {}
        for adjustment in subtractions:
            if isinstance(adjustment, _Exclusion):
                retirement, grants = adjustment, adjustment.grants
            elif (found := self._adjustment_named.get(adjustment.line)) is not None \
                    and found.step == "subtraction":
                subtracted[adjustment.line] = adjustment.ordinary
        stcg = income.stcg + retirement.stcg
        rest = income.ordinary - income.stcg + retirement.ordinary + sum(subtracted.values())
        # Each spouse's own ordinary income, a loss included, and what the
        # joint return has beyond the two (a state addition, a what-if's
        # extra income) by their shares of it.
        own = self._ordinary_own
        beyond = common.apportion(rest - sum(own.values()), self._ordinary_weights)
        rest_by = {p: own[p] + beyond[p] for p in people}
        stcg_by = common.apportion(stcg, self._gain_weights)
        ltcg_by = common.apportion(income.ltcg + retirement.ltcg, self._gain_weights)
        # With rows by age (#581) each spouse's taxable share is their own.
        ss_by = common.apportion(income.ss, self._taxable_ss_by_person(basis)
                                 if self._ss_tiers is not None else self._ss_weights)
        # The exclusion's gain goes to the spouses whose grants covered
        # gain, the rest to those whose grants covered other income.
        gain_weights = {p: grants.get(p, (0, 0))[1] for p in people}
        other_weights = {p: grants.get(p, (0, 0))[0] - grants.get(p, (0, 0))[1] for p in people}
        off_stcg = common.apportion(retirement.stcg, gain_weights)
        off_ltcg = common.apportion(retirement.ltcg, gain_weights)
        off_rest = common.apportion(retirement.ordinary, other_weights)
        for line, amount in subtracted.items():
            for p, share in self._line_shares(line, amount).items():
                off_rest[p] += share
        out = {}
        for p in people:
            short = stcg_by[p] - off_stcg[p]
            long = ltcg_by[p] - off_ltcg[p]
            ordinary = rest_by[p] - off_rest[p] + short
            # The cents the shares round apart can leave a spouse a little
            # under on a kind of gain; it comes off their other income.
            if long < 0:
                ordinary, long = ordinary + long, 0
            out[p] = Income(ordinary=ordinary, stcg=max(0, min(short, ordinary)),
                            ltcg=long, ss=ss_by[p])
        return out

    def _spouse_standard(self, agi: int, federal_agi: int) -> int:
        """A spouse's own married-filing-separately standard deduction
        (#580), phased out at their own AGI where the state's shrinks
        (#578)."""
        row = self._mfs_standard
        if row.get("phaseout_start") is None:
            return int(row["amount"])
        test = federal_agi if row.get("phaseout_agi") == "federal" else agi
        return common.phased_standard_deduction(row, test)

    def _line_shares(self, line: str, amount: int) -> dict[str, int]:
        """A table line's ``amount`` divided between spouses filing
        separately (#580) by whose it is (:attr:`StateAdjustment.shares`),
        or evenly: a subclass's own line, or one that's no one's."""
        people = self._spouses
        found = self._adjustment_named.get(line)
        weights = {p: found.shares.get(p, 0) for p in people} if found is not None else {}
        if not any(weights.values()):
            weights = dict.fromkeys(people, 0)
        return common.apportion(amount, weights)

    def _after_credits(self, taxes: Mapping[str, int], joint: Joint,
                       lines: dict[str, int]) -> tuple[int, int, dict[str, int]]:
        """The spouses' taxes less the joint return's credits (#580), as it
        figured them at its own tax, each divided by :meth:`_line_shares`
        (a subclass's :meth:`credits` evenly) and taking no more than its
        spouse's tax; each spouse's tax goes into ``lines``. Returns the tax
        left, the credits taken in all, and what each credit line took."""
        people = self._spouses
        left = dict(taxes)
        for p, share in zip(people, common.split_cents(max(0, joint.credits), len(people)),
                            strict=True):
            left[p] -= min(left[p], share)
        taken: dict[str, int] = {}
        for line, amount in joint.credit_lines.items():
            taken[line] = 0
            for p, share in self._line_shares(line, max(0, amount)).items():
                took = min(left[p], share)
                left[p] -= took
                taken[line] += took
        for p in people:
            lines[f"separate_tax_{p}"] = left[p]
        tax = sum(left.values())
        return tax, sum(taxes.values()) - tax, taken

    def _gains_base(self, extra: Extra) -> int:
        """What the gains tax taxes before its deduction (#489): the net
        long-term gain, as Schedule D line 15 has it, less real estate's,
        with a what-if's on top."""
        return max(0, self._line_15 - self._real_estate_gain
                   + extra.ltcg - extra.real_estate_ltcg)

    def _taxed_ltcg(self, gain: int, extra: Extra) -> int:
        """The long-term gain in the state's income: ``gain`` less its
        exclusion, or a gains tax's base in its place (#489)."""
        return self._gains_base(extra) if self._gains_bands else self._net_ltcg(gain)

    def excise(self, basis: Basis) -> tuple[Mapping[str, int], int]:
        if not self._gains_bands:
            return {}, 0
        # A what-if's credit reads it before compute adds it.
        if self._excise is not None and self._excise[0] is basis:
            return self._excise[1]
        base = self._gains_base(basis.extra)
        tax = common.progressive_tax(max(0, base - self._gains_deduction), self._gains_bands)
        result = {"gains_base": base, "gains_deduction": self._gains_deduction,
                  "gains_tax": tax}, tax
        self._excise = (basis, result)
        return result

    def memo(self, basis: Basis) -> Mapping[str, int]:
        memo = basis.federal.memo
        withdrawn = memo.get("hsa_withdrawn", 0) if self._hsa_taxed else 0
        return {
            "hsa_withdrawn": withdrawn,
            # The after-tax drawdown prices an HSA's gain over basis, not its
            # balance, for a state that taxes it (#497).
            "hsa_taxed": int(self._hsa_taxed),
            # What the year drew from traditional money (an RMD), and the
            # gain it realized, an HSA withdrawal's included (#497); a
            # reinvested gain distribution (#547) drew none.
            "drawn_ordinary": memo.get("drawn_ordinary", 0) - withdrawn,
            "drawn_gain": max(0, self._capital.ltcg - self._distributed),
            # Real estate's part of it, which a gains tax leaves out of the
            # drawdown that replaces it too (#489).
            "drawn_real_estate_gain": max(0, min(
                self._real_estate_gain, self._capital.ltcg - self._distributed)),
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
        itemized_limits_aux: str = registries.STATE_ITEMIZED_LIMITS
        """The state's limits on its itemized lines from spending (#542),
        keyed by state; a state without them figures none (``itemized_limits``
        takes the federal limits)."""
        retirement_exclusion_aux: str = registries.STATE_RETIREMENT_EXCLUSION
        ltcg_bracket_aux: str = registries.STATE_LTCG_BRACKETS
        stcg_bracket_aux: str = registries.STATE_STCG_BRACKETS
        ltcg_exclusion_aux: str = registries.STATE_LTCG_EXCLUSION
        adjustments_aux: str = registries.STATE_ADJUSTMENTS
        gains_tax_aux: str = registries.STATE_GAINS_TAX
        gains_deduction_aux: str = registries.STATE_GAINS_DEDUCTION
        separate_return_aux: str | None = registries.STATE_SEPARATE_RETURN
        """Whether the state lets a couple file separately on one return
        (#580), keyed by state; None never does."""
        nol_limit_percent: int | None = None
        """How much of taxable income a carried-in NOL may offset; 100 by default."""
