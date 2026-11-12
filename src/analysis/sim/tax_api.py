"""What a tax calculator sees (#507): a tax year's income item by item, the
tables in force, and the return it fills.

A calculator is a :class:`TaxCalculator` subclass marked with
:func:`register`. Each tax jurisdiction (``taxes/<name>.csv``, ADR-003
§4.15) names one by its ``kind``. For each tax year the engine builds a
:class:`TaxYear` and asks the calculator to :meth:`TaxCalculator.prepare`
it, once; the :class:`Prepared` year then answers
:meth:`Prepared.compute` as often as asked, with more income on top
(:class:`Extra`) for the what-ifs (``fill_bracket``, the after-tax
drawdown, IRMAA's MAGI, ``ctx.tax``).

A calculator is a pure function of its :class:`TaxYear`: it reads no
database and keeps nothing between years, which is what makes a run
reproducible and a calculator testable on a year built by hand.

A data repo brings its own calculators as ``taxes/*.py`` modules (#510,
ADR-011), imported as strategies are. A module may set ``API_VERSION`` to the
version it was written for; one newer than the code provides is refused at
load. The API is this module and the calculator classes and bases in
:mod:`analysis.sim.taxes` (``federal``, ``state_base``, ``state_tables``,
``local``), which a household's calculator may subclass;
:mod:`analysis.sim.taxes.common` is their own helpers, which can change.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, ClassVar

from analysis.sim import ownership, registries
from analysis.sim.param_units import Dollars, Percent, Tag, percent

__all__ = [
    "API_VERSION", "Account", "CapitalLoss", "Dollars", "Extra", "HsaGain", "IncomeItem", "Mortgage", "Percent",
    "Person", "Prepared", "Residence", "Return", "SpendingItem", "Tag", "TaxCalculator", "TaxError", "TaxPaid",
    "TaxYear", "calculator", "is_registered", "percent", "register",
]

API_VERSION = 3
"""Bumped when a calculator written against the previous version could
break, or one written for this version needs what the previous lacks. 1
(#510) is the first a data repo can write to; 2 (#511) adds a state year's
``other_deductions`` and ``credit_lines`` steps; 3 (#489) its ``excise``
step and :attr:`Extra.real_estate_ltcg`."""

Row = dict[str, Any]
"""One aux table row's payload."""


class TaxError(Exception):
    """A tax year a calculator can't compute: a table it needs is missing, or
    a param is out of range. The engine reports it as the run's error."""


@dataclass(frozen=True)
class Account:
    """The account on one side of an income item."""
    name: str
    kind: str
    subkind: str
    owner: str | None
    hobby: bool = False
    """A ``business`` account whose ``meta_json`` says ``"hobby": true`` (#231)."""
    tags: frozenset[str] = frozenset()
    """The account's own tags, ``role:name`` (#510)."""


def _effective_tags(rule: frozenset[str], account: frozenset[str]) -> frozenset[str]:
    """``rule``'s tags, and ``account``'s in each role the rule has none of:
    a rule refines its account's tag, as in the reports' ``--by tag:ROLE``."""
    roles = {tag.partition(":")[0] for tag in rule}
    return rule | frozenset(tag for tag in account if tag.partition(":")[0] not in roles)


@dataclass(frozen=True)
class IncomeItem:
    """The year's transfers of one ``character`` from one account to
    another, summed. ``year`` is the tax year they count in (a transfer's
    ``tax_year``, else its date's). Transfers of character ``none`` are
    never income, so no item has it.

    ``rule_tags`` are the tags of the rule (or calendar row) that posted
    them; :attr:`tags` adds the income account's (#510)."""
    year: int
    character: str
    payer: Account
    payee: Account
    amount: int
    rule_tags: frozenset[str] = frozenset()

    @property
    def tags(self) -> frozenset[str]:
        """The item's tags: its rule's, and in each role the rule has none
        of, the income account's (the payer; the payee when the payer isn't
        an income account)."""
        account = self.payer if self.payer.kind == "income" or self.payee.kind != "income" else self.payee
        return _effective_tags(self.rule_tags, account.tags)


@dataclass(frozen=True)
class SpendingItem:
    """The year's payments from one account into an expense account (#510),
    summed by the rule's tags and person: rent, property tax, a premium.
    ``year`` is the tax year they count in, as for :class:`IncomeItem`, and
    ``character`` theirs (``none`` for most spending).

    ``rule_tags`` are the posting rule's (or calendar row's) tags, and
    ``person`` its ``person``, if it names one; :attr:`tags` adds the
    expense account's."""
    year: int
    character: str
    payer: Account
    payee: Account
    amount: int
    rule_tags: frozenset[str] = frozenset()
    person: str | None = None

    @property
    def tags(self) -> frozenset[str]:
        """The item's tags: its rule's, and in each role the rule has none
        of, the expense account's. A rule tagged ``tax:repair`` paying into
        an account tagged ``tax:rent`` is a repair, not rent."""
        return _effective_tags(self.rule_tags, self.payee.tags)


@dataclass(frozen=True)
class CapitalLoss:
    """A realized loss recorded apart from its sale (``capital_loss``, #199),
    summed by tax year, character, the lot's owner and its account's
    subkind (a ``real_estate`` loss, which a state's gains tax leaves out,
    #489)."""
    year: int
    character: str
    owner: str | None
    amount: int
    subkind: str | None = None


@dataclass(frozen=True)
class HsaGain:
    """An HSA withdrawal's gain over the lot's basis (``hsa_gain``, #497), a
    loss negative, summed by tax year, character and the lot's owner: what a
    state that doesn't recognize HSAs taxes, and the federal return never
    sees."""
    year: int
    character: str
    owner: str | None
    amount: int


@dataclass(frozen=True)
class Mortgage:
    """One mortgage debt's month (#542): the interest its ``mortgage_payment``
    rules paid and the balance it was figured on, which the debt limit on
    deductible interest is tested against. ``name`` is the liability
    account. ``tags`` are its interest's, as a :class:`SpendingItem`'s: a
    home's carries ``tax:mortgage_interest``, a rental's another ``tax:``
    tag."""
    year: int
    month: int
    name: str
    interest: int
    balance: int
    tags: frozenset[str] = frozenset()


@dataclass(frozen=True)
class TaxPaid:
    """What the household paid in one calendar year toward a jurisdiction's
    income tax (#542), by the payments' dates (a cash-basis taxpayer's
    deduction): the year's estimated payments and the April settlement of
    the year before."""
    year: int
    jurisdiction: str
    amount: int


@dataclass(frozen=True)
class Person:
    """One of the scenario's people, with the last month they're alive
    (``YYYY-MM-DD``, the month's end) when the scenario gives a lifespan."""
    id: str
    birthdate: str | None
    role: str
    final_month: str | None = None


@dataclass(frozen=True)
class Residence:
    """Where the household lives (``residence.csv``, #509): a state's postal
    code, and the county whose local tax applies, if it names one."""
    state: str
    county: str | None = None


@dataclass(frozen=True)
class TaxYear:
    """One tax year, as a calculator sees it.

    ``items``, ``losses`` and ``hsa_gains`` cover every year up to ``year``: a capital loss
    carries forward, so this year's netting reads the years before it.
    Tables are read through the methods, as of the year's end.
    ``params`` is the jurisdiction's ``params_json`` as the calculator's
    :attr:`TaxCalculator.Params`, in stored units (cents, millionths).
    ``spending`` is the year's payments into expense accounts, and the years
    before it."""
    year: int
    jurisdiction: str
    params: Any
    items: Sequence[IncomeItem]
    losses: Sequence[CapitalLoss]
    persons: Sequence[Person]
    _table: Callable[[str, str | None, int], list[Row]] = field(repr=False)
    _indexed: Callable[[str, str | None], list[Row]] = field(repr=False)
    _dated: Callable[[str, str | None], list[tuple[str, Row]]] = field(repr=False)
    _keyed: Callable[[str, str, str, int], Row | None] = field(repr=False)
    _filing_status_in: Callable[[int], str] = field(repr=False)
    _prior: Callable[[str], int] = field(repr=False)
    _series_of: Callable[[str], list[str]] = field(repr=False)
    _upstream: Callable[[str], Prepared] = field(repr=False, default=lambda name: _no_upstream(name))
    _residences: Sequence[tuple[str, Residence]] = field(repr=False, default=())
    """Where the household lives, from each row's effective date (a month
    end), in date order."""
    spending: Sequence[SpendingItem] = ()
    _line_in: Callable[[str, int], int] = field(repr=False, default=lambda _line, _year: 0)
    hsa_gains: Sequence[HsaGain] = ()
    """Every year's HSA withdrawals' gains over basis up to ``year`` (#497),
    for a state that taxes an HSA."""
    mortgages: Sequence[Mortgage] = ()
    """Each mortgage's interest and balance by month (#542); the engine
    gives this year's only."""
    taxes_paid: Sequence[TaxPaid] = ()
    """What was paid toward each state and local jurisdiction in a calendar
    year (#542), the SALT deduction's income taxes; the engine gives this
    year's only."""

    @property
    def filing_status(self) -> str:
        """The year's filing status (``Single`` the year after a spouse's
        death)."""
        return self._filing_status_in(self.year)

    def table(self, kind: str, series: str | None = None) -> list[Row]:
        """The rows of table ``kind`` (``series`` for one keyed by state) in
        force at the year's end; [] when there are none."""
        return self._table(kind, series, self.year)

    @property
    def residence(self) -> Residence | None:
        """Where the household lives on the year's December 31, which is
        where it files (#509); None in a scenario that says nowhere."""
        return self.residence_in(self.year)

    def residence_in(self, year: int) -> Residence | None:
        """:attr:`residence` for another year (an earlier year's HSA). A
        year before the first row is the first row's: it says where the
        household lived when the scenario starts."""
        if not self._residences:
            return None
        in_force = [where for effective, where in self._residences if effective <= f"{year}-12-31"]
        return in_force[-1] if in_force else self._residences[0][1]

    def table_as_of(self, kind: str, series: str | None, year: int) -> list[Row]:
        """:meth:`table` as of another year's end."""
        return self._table(kind, series, year)

    def indexed(self, kind: str, series: str | None = None) -> list[Row]:
        """:meth:`table`, with each figure that has a growth series grown to
        January of the year, as the law indexes them (#467)."""
        return self._indexed(kind, series)

    def dated(self, kind: str, series: str | None = None) -> list[tuple[str, Row]]:
        """Every row of table ``kind`` with its effective date, in date order."""
        return self._dated(kind, series)

    def has_table(self, kind: str, series: str | None = None) -> bool:
        """Whether the scenario has table ``kind`` at all, at any date."""
        return bool(self._dated(kind, series))

    def series_of(self, kind: str) -> list[str]:
        """The series table ``kind`` has (the states a state table is keyed
        by), sorted."""
        return self._series_of(kind)

    def keyed(self, kind: str, column: str, value: str, year: int) -> Row | None:
        """The latest row of table ``kind`` whose ``column`` is ``value``, in
        force at ``year``'s end."""
        return self._keyed(kind, column, value, year)

    def filing_status_in(self, year: int) -> str:
        """Another year's filing status (an earlier year's loss limit)."""
        return self._filing_status_in(year)

    def prior(self, line: str) -> int:
        """This jurisdiction's ``line`` of last year's return; 0 before the
        first year it computed."""
        return self._prior(line)

    def line_in(self, line: str, year: int) -> int:
        """This jurisdiction's ``line`` of an earlier year's return; 0 for a
        year it didn't compute. A carryover's history (#277)."""
        return self._line_in(line, year)

    @cached_property
    def household(self) -> ownership.Household:
        """The household and its people's final months (#277): whose an
        account's money was at a death."""
        return ownership.Household.of(
            ((p.id, p.role) for p in self.persons),
            {p.id: p.final_month for p in self.persons if p.final_month})

    @cached_property
    def _this_year(self) -> list[IncomeItem]:
        return [i for i in self.items if i.year == self.year]

    def upstream(self, name: str) -> Prepared:
        """The same year of jurisdiction ``name``, one this calculator
        builds on (:attr:`TaxCalculator.upstream`), prepared on the same
        ledger. Its :meth:`Prepared.compute` takes the same :class:`Extra`,
        so a what-if moves both returns."""
        return self._upstream(name)

    def year_items(self, *characters: str) -> list[IncomeItem]:
        """This year's items of the ``characters`` (all of them when none)."""
        return [i for i in self._this_year if not characters or i.character in characters]

    def total(self, character: str) -> int:
        """This year's total of one tax character."""
        return sum(i.amount for i in self._this_year if i.character == character)

    def tagged(self, tag: str) -> list[IncomeItem]:
        """This year's income items carrying ``tag`` (:attr:`IncomeItem.tags`)."""
        return [i for i in self._this_year if tag in i.tags]

    @cached_property
    def _spent_this_year(self) -> list[SpendingItem]:
        return [s for s in self.spending if s.year == self.year]

    def year_spending(self, *tags: str) -> list[SpendingItem]:
        """This year's spending items carrying any of ``tags`` (all of them
        when none)."""
        return [s for s in self._spent_this_year if not tags or s.tags & set(tags)]

    def spent(self, tag: str) -> int:
        """This year's spending carrying ``tag``, in cents: what a deduction
        for rent or property tax starts from."""
        return sum(s.amount for s in self._spent_this_year if tag in s.tags)

    def year_mortgages(self) -> list[Mortgage]:
        """This year's :class:`Mortgage` months."""
        return [m for m in self.mortgages if m.year == self.year]

    def tax_paid(self, *jurisdictions: str) -> int:
        """What was paid this calendar year toward the ``jurisdictions``'
        income taxes (every state and local one when none are named)."""
        return sum(t.amount for t in self.taxes_paid
                   if t.year == self.year and (not jurisdictions or t.jurisdiction in jurisdictions))


@dataclass(frozen=True)
class Extra:
    """Income added on top of the year's, for a what-if: ``ordinary`` more
    ordinary income (a Roth conversion) and ``ltcg`` more long-term gain,
    ``real_estate_ltcg`` of it a home's (the after-tax drawdown's), which a
    state's gains tax leaves out (#489)."""
    ordinary: int = 0
    ltcg: int = 0
    real_estate_ltcg: int = 0


NO_EXTRA = Extra()


@dataclass(frozen=True)
class Return:
    """A computed return: named lines in cents, each written to
    ``tax_year_summary`` as ``<jurisdiction>_<line>``, and ``memo`` lines
    kept for the engine and not written.

    The engine reads four memo lines, each 0 when left out: ``drawn_ordinary``,
    the year's ordinary income drawn from traditional money (an RMD), and
    ``drawn_gain``, the long-term gain its sales realized (not a reinvested
    gain distribution, #547), which the after-tax drawdown's share of a later
    year replaces, so it isn't taxed twice; ``drawn_real_estate_gain``, real
    estate's part of ``drawn_gain``, which it takes off the share's
    :attr:`Extra.real_estate_ltcg` likewise (#489); and ``hsa_taxed``, 1 when the
    jurisdiction taxes an HSA like a brokerage account (#497), so the
    drawdown prices an HSA's gain over basis rather than its whole balance.
    Any other memo line is the calculator's own.

    Every calculator fills ``REQUIRED``. A what-if (:class:`Extra`) works
    out again the lines more income changes (the income lines, AGI,
    taxable income, the NOL used, the tax); the rest stay the year's."""
    lines: Mapping[str, int]
    memo: Mapping[str, int] = field(default_factory=dict)

    REQUIRED: ClassVar[tuple[str, ...]] = (
        "agi", "taxable_income", "ordinary_taxable", "tax", "nol_carryover",
        "capital_loss_carryover",
    )

    def __getitem__(self, line: str) -> int:
        return self.lines[line]

    @property
    def tax(self) -> int:
        return self.lines["tax"]

    @property
    def agi(self) -> int:
        return self.lines["agi"]

    @property
    def magi(self) -> int:
        """IRMAA's MAGI (#253): AGI plus tax-exempt interest (#265), the
        ``magi`` line when the return has one, else AGI."""
        return self.lines.get("magi", self.agi)

    @property
    def taxable_income(self) -> int:
        return self.lines["taxable_income"]

    @property
    def ordinary_taxable(self) -> int:
        """The part of taxable income taxed at the ordinary brackets: less
        the long-term gain on top, and a state's separately taxed
        short-term gain (#485)."""
        return self.lines["ordinary_taxable"]


class Prepared:
    """A calculator's year, ready to compute. ``brackets`` (the ordinary
    schedule, (floor, rate micros) pairs) and ``ltcg_brackets`` are what
    ``fill_bracket`` and ``ctx.tax`` aim at."""
    filing_status: str
    brackets: Sequence[tuple[int, int]]
    ltcg_brackets: Sequence[tuple[int, int]] = ()

    def compute(self, extra: Extra = NO_EXTRA) -> Return:
        raise NotImplementedError


def _no_upstream(name: str) -> Prepared:
    raise TaxError(f"no upstream jurisdiction {name!r}: the calculator doesn't build on it")


@dataclass(frozen=True)
class _NoParams:
    """A calculator that takes no parameters."""


class TaxCalculator:
    """A tax calculator: a ``kind`` a jurisdiction file names, and how it
    turns a :class:`TaxYear` into returns.

    A subclass sets ``kind``, ``jurisdiction``, ``upstream`` if it builds on
    another return, a frozen dataclass ``Params`` (fields typed with
    :data:`Dollars`, :data:`Percent`, :data:`Tag` or plain JSON types, each
    with a default or required), a docstring (shown by ``sim tax kinds``),
    and :meth:`prepare`, and is marked with :func:`register`. A ``Tag``
    param names a tag the calculator reads (:meth:`TaxYear.spent`); the
    loader refuses a scenario where no account, rule or calendar row
    carries it, so a deduction never silently comes out zero."""
    kind: ClassVar[str]
    Params: type[Any] = _NoParams
    """The calculator's parameters: a frozen dataclass nested in the class.
    A subclass of a calculator may extend its ``Params``."""
    upstream: ClassVar[tuple[str, ...]] = ()
    """The jurisdictions whose return it builds on (:meth:`TaxYear.upstream`):
    each must have a file in the scenario, and settles before it."""
    jurisdiction: ClassVar[str]
    """The jurisdiction it computes (``federal``, ``state`` or ``local``):
    the file that names the kind is ``taxes/<jurisdiction>.csv``."""

    def prepare(self, year: TaxYear) -> Prepared:
        raise NotImplementedError


_REGISTERED = "__tax_calculator_registered__"


def register[C: type[TaxCalculator]](cls: C) -> C:
    """Class decorator: make ``cls`` a calculator kind."""
    if not (isinstance(cls, type) and issubclass(cls, TaxCalculator)):
        raise TaxError(f"@register needs a TaxCalculator subclass, not {cls!r}")
    kind = cls.__dict__.get("kind")
    if not isinstance(kind, str) or not kind.isidentifier():
        raise TaxError(f"{cls.__name__}: kind must be set to an identifier, got {kind!r}")
    if getattr(cls, "jurisdiction", None) not in registries.TAX_JURISDICTIONS:
        raise TaxError(f"{cls.__name__}: jurisdiction must be one of "
                       f"{', '.join(registries.TAX_JURISDICTIONS)}, "
                       f"got {getattr(cls, 'jurisdiction', None)!r}")
    if not dataclasses.is_dataclass(cls.Params):
        raise TaxError(f"{cls.__name__}: Params must be a dataclass")
    for up in cls.upstream:
        if up not in registries.TAX_JURISDICTIONS or up == cls.jurisdiction:
            raise TaxError(f"{cls.__name__}: upstream {up!r} must be another of "
                           f"{', '.join(registries.TAX_JURISDICTIONS)}")
    setattr(cls, _REGISTERED, True)
    return cls


def is_registered(obj: object) -> bool:
    """Whether ``obj`` is a class marked by :func:`register` itself (not one
    inheriting the mark from a registered base)."""
    return isinstance(obj, type) and _REGISTERED in obj.__dict__


def calculator(kind: str) -> TaxCalculator:
    """The built-in calculator ``kind`` names. A scenario's kinds, the data
    repo's included, are :class:`analysis.sim.tax_code.TaxKinds`."""
    from analysis.sim import tax_code  # noqa: PLC0415  (imports the built-ins)
    try:
        return tax_code.builtin_kinds()[kind]()
    except KeyError:
        raise TaxError(f"unknown tax calculator kind {kind!r}") from None
