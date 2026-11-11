"""Bases for a state income tax (#508): :class:`StateFromFederal`, which
starts from the federal return as most states do (California's Schedule CA),
and :class:`StateFromIncome`, for a state that builds its own income
(Pennsylvania's classes).

Both compute a year in the same steps, each a method a subclass can
override on its :class:`StateYear`:

1. :meth:`~StateYear.start`: the income it starts from, by kind;
2. :meth:`~StateYear.additions` and :meth:`~StateYear.subtractions`: named
   lines of the return, each changing income of one or more kinds;
3. state AGI, the start plus the additions less the subtractions;
4. :meth:`~StateYear.deductions`, then :meth:`~StateYear.other_deductions`
   (lines that depend on the year's income), then a carried-in NOL, then
   taxable income;
5. :meth:`~StateYear.tax`, less :meth:`~StateYear.credits` and
   :meth:`~StateYear.credit_lines`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import ClassVar

from analysis.sim.tax_api import (
    NO_EXTRA, Extra, Prepared, Return, TaxCalculator, TaxError, TaxYear,
)
from analysis.sim.taxes import common


@dataclass(frozen=True)
class Income:
    """A state's income by kind, as the steps change it."""
    ordinary: int = 0
    """Ordinary income, the net short-term gain in it included."""
    stcg: int = 0
    """The net short-term gain within ``ordinary``, which a state may tax apart."""
    ltcg: int = 0
    """Income taxed with long-term gain."""
    ss: int = 0
    """Taxable Social Security."""

    @property
    def agi(self) -> int:
        return self.ordinary + self.ltcg + self.ss

    def changed(self, adjustment: Adjustment, sign: int) -> Income:
        a = adjustment
        return Income(
            self.ordinary + sign * (a.ordinary + a.stcg), self.stcg + sign * a.stcg,
            self.ltcg + sign * a.ltcg, self.ss + sign * a.ss)


@dataclass(frozen=True)
class Adjustment:
    """A named line of a state return, written as ``state_<line>``: an
    addition to or subtraction from income of each kind. ``stcg`` is
    short-term gain, which is ordinary income too. The line shows the kinds'
    sum unless ``amount`` says otherwise (income moved from one kind to
    another, which leaves AGI alone)."""
    line: str
    ordinary: int = 0
    stcg: int = 0
    ltcg: int = 0
    ss: int = 0
    amount: int | None = None

    @property
    def value(self) -> int:
        if self.amount is not None:
            return self.amount
        return self.ordinary + self.stcg + self.ltcg + self.ss


@dataclass(frozen=True)
class Basis:
    """What a step works from: the what-if's ``extra`` income and, for a
    state that starts from it, the federal return with that income too."""
    extra: Extra
    _federal: Return | None = None

    @property
    def federal(self) -> Return:
        if self._federal is None:
            raise TaxError("this state builds its own income: it has no federal return")
        return self._federal


class StateYear(Prepared):
    """One tax year of a state, ready to compute. A subclass sets
    ``brackets`` (and ``ltcg_brackets``, ``stcg_brackets``) in
    :meth:`setup`, and overrides the steps it needs."""

    stcg_brackets: Sequence[tuple[int, int]] = ()
    nol_limit_percent_default: ClassVar[int] = 100

    def __init__(self, calc: TaxCalculator, ty: TaxYear) -> None:
        self.calc, self.ty = calc, ty
        self.filing_status = ty.filing_status
        self.setup()
        self._deductions = dict(self.deductions())
        self._nol_limit = common.nol_limit_percent(ty, self.nol_limit_percent_default)
        self._nol_in = ty.prior("nol_carryover")
        self._nol_lapse = common.nol_lapse_share(ty)
        basis = self.basis(NO_EXTRA)
        fixed = self.fixed_lines(basis)
        self._nol_new = common.nol_new(
            agi=self._income(basis)[0].agi, business_net=fixed.get("business_net", 0),
            capital_loss_used=fixed.get("capital_loss_used", 0),
            deduction=self._deductions["deduction"])

    # -- The steps ------------------------------------------------------------

    def setup(self) -> None:
        """Read the year's tables: at least set ``brackets``."""
        raise NotImplementedError

    def basis(self, extra: Extra) -> Basis:
        return Basis(extra)

    def start(self, basis: Basis) -> Income:
        """The income the state starts from."""
        raise NotImplementedError

    def additions(self, income: Income, basis: Basis) -> Sequence[Adjustment]:
        return ()

    def subtractions(self, income: Income, basis: Basis) -> Sequence[Adjustment]:
        """What comes off ``income``, the start plus the additions."""
        return ()

    def fixed_lines(self, basis: Basis) -> Mapping[str, int]:
        """Lines that report the year without changing its income; a
        ``business_net`` and ``capital_loss_used`` here count toward a new NOL."""
        return {}

    def deductions(self) -> Mapping[str, int]:
        """The deduction lines; ``deduction`` is the one taken."""
        raise NotImplementedError

    def other_deductions(self, income: Income, basis: Basis) -> Mapping[str, int]:
        """Named deductions taken after AGI besides :meth:`deductions`'
        ``deduction``, which may depend on the year's income (a renter's
        deduction with an income limit, #511); none by default."""
        return {}

    def tax(self, taxable: int, income: Income) -> int:
        """The tax on ``taxable`` income, with long- and short-term gain
        stacked as the brackets say."""
        return common.stacked_tax(
            taxable, income.ltcg, income.stcg, self.brackets, self.ltcg_brackets, self.stcg_brackets)

    def credits(self, tax: int, income: Income, basis: Basis) -> int:
        """Credits against ``tax``, a total; none by default."""
        return 0

    def credit_lines(self, tax: int, income: Income, basis: Basis) -> Mapping[str, int]:
        """Named credits against ``tax`` (#511), each written to the return
        as what it took: in order, after :meth:`credits`, until the tax is
        used up. None by default."""
        return {}

    def memo(self, basis: Basis) -> Mapping[str, int]:
        """The return's memo (``drawn_ordinary``, ``drawn_gain``)."""
        return {}

    # -- The pipeline ---------------------------------------------------------

    def _income(self, basis: Basis) -> tuple[Income, dict[str, int]]:
        income = self.start(basis)
        lines: dict[str, int] = {}
        for adjustment in self.additions(income, basis):
            income = income.changed(adjustment, 1)
            lines[adjustment.line] = adjustment.value
        for adjustment in self.subtractions(income, basis):
            income = income.changed(adjustment, -1)
            lines[adjustment.line] = adjustment.value
        return income, lines

    def compute(self, extra: Extra = NO_EXTRA) -> Return:
        basis = self.basis(extra)
        income, adjustments = self._income(basis)
        others = dict(self.other_deductions(income, basis))
        # A NOL is figured without these (#511): a nonbusiness deduction
        # offsets only nonbusiness income, as the federal rule says.
        before_nol = income.agi - self._deductions["deduction"] - sum(others.values())
        nol_used = common.nol_used(self._nol_in, before_nol, self._nol_limit)
        taxable = max(0, before_nol - nol_used)
        tax = self.tax(taxable, income)
        # Credits are never refundable: a named line shows what it took,
        # in order, so the lines add up to the credits taken.
        credits = min(tax, self.credits(tax, income, basis))
        credit_lines = {}
        for line, amount in self.credit_lines(tax, income, basis).items():
            credit_lines[line] = max(0, min(amount, tax - credits))
            credits += credit_lines[line]
        lines = {
            "ordinary_income": income.ordinary,
            "ltcg_taxed": income.ltcg,
            "nol_used": nol_used,
            **common.nol_carried(self._nol_in - nol_used + self._nol_new, self._nol_lapse),
            "ss_taxable": income.ss,
            "agi": income.agi,
            **self.fixed_lines(basis),
            **adjustments,
            **self._deductions,
            **others,
            **credit_lines,
            "taxable_income": taxable,
            "ordinary_taxable": common.ordinary_taxable(
                taxable, income.ltcg, income.stcg, bool(self.stcg_brackets)),
            "credits": credits,
            "tax": tax - credits,
        }
        return Return(lines, MappingProxyType(dict(self.memo(basis))))


class StateFromFederal(TaxCalculator):
    """A state that starts from the federal return: federal AGI, split into
    ordinary income, long-term gain and taxable Social Security, with its
    own additions and subtractions."""
    upstream = ("federal",)
    year_class: ClassVar[type[FederalStateYear]]

    def prepare(self, year: TaxYear) -> Prepared:
        return self.year_class(self, year)


class FederalStateYear(StateYear):
    """A :class:`StateFromFederal` year: its basis carries the federal return
    with the same extra income, so a what-if moves both."""

    def __init__(self, calc: TaxCalculator, ty: TaxYear) -> None:
        self.federal = ty.upstream("federal")
        super().__init__(calc, ty)

    def basis(self, extra: Extra) -> Basis:
        return Basis(extra, self.federal.compute(extra))

    def start(self, basis: Basis) -> Income:
        fed = basis.federal
        return Income(
            ordinary=fed["ordinary_income"], stcg=fed.memo.get("net_stcg", 0),
            ltcg=fed["ltcg_taxed"], ss=fed["ss_taxable"])

    def fixed_lines(self, basis: Basis) -> Mapping[str, int]:
        fed = basis.federal
        return {line: fed.lines[line] for line in (
            "business_net", "hobby_disallowed", "capital_loss_used",
            "capital_loss_carryover", "capital_loss_lapsed", "ss_benefits") if line in fed.lines}

    def memo(self, basis: Basis) -> Mapping[str, int]:
        memo = basis.federal.memo
        return {"drawn_ordinary": memo.get("drawn_ordinary", 0),
                "drawn_gain": memo.get("drawn_gain", 0)}


StateFromFederal.year_class = FederalStateYear


class StateFromIncome(TaxCalculator):
    """A state that builds its own income from the year's items, not from
    the federal return. Its year class says how."""
    year_class: ClassVar[type[StateYear]]

    def prepare(self, year: TaxYear) -> Prepared:
        return self.year_class(self, year)
