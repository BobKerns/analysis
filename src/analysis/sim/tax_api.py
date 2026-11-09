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

The API is the built-in calculators' only, for now: until a data repo can
bring its own (#510) it can change without ``API_VERSION`` moving.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, ClassVar

__all__ = [
    "API_VERSION", "Account", "CapitalLoss", "Extra", "IncomeItem", "KINDS", "Person",
    "Prepared", "Return", "TaxCalculator", "TaxError", "TaxYear", "calculator", "register",
]

API_VERSION = 1

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


@dataclass(frozen=True)
class IncomeItem:
    """The year's transfers of one ``character`` from one account to
    another, summed. ``year`` is the tax year they count in (a transfer's
    ``tax_year``, else its date's). Transfers of character ``none`` are
    never income, so no item has it."""
    year: int
    character: str
    payer: Account
    payee: Account
    amount: int


@dataclass(frozen=True)
class CapitalLoss:
    """A realized loss recorded apart from its sale (``capital_loss``, #199),
    summed by tax year, character and the lot's owner."""
    year: int
    character: str
    owner: str | None
    amount: int


@dataclass(frozen=True)
class Person:
    """One of the scenario's people."""
    id: str
    birthdate: str | None
    role: str


@dataclass(frozen=True)
class TaxYear:
    """One tax year, as a calculator sees it.

    ``items`` and ``losses`` cover every year up to ``year``: a capital loss
    carries forward, so this year's netting reads the years before it.
    Tables are read through the methods, as of the year's end."""
    year: int
    jurisdiction: str
    params: Mapping[str, Any]
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

    @property
    def filing_status(self) -> str:
        """The year's filing status (``Single`` the year after a spouse's
        death)."""
        return self._filing_status_in(self.year)

    def table(self, kind: str, series: str | None = None) -> list[Row]:
        """The rows of table ``kind`` (``series`` for one keyed by state) in
        force at the year's end; [] when there are none."""
        return self._table(kind, series, self.year)

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

    @cached_property
    def _this_year(self) -> list[IncomeItem]:
        return [i for i in self.items if i.year == self.year]

    def year_items(self, *characters: str) -> list[IncomeItem]:
        """This year's items of the ``characters`` (all of them when none)."""
        return [i for i in self._this_year if not characters or i.character in characters]

    def total(self, character: str) -> int:
        """This year's total of one tax character."""
        return sum(i.amount for i in self._this_year if i.character == character)


@dataclass(frozen=True)
class Extra:
    """Income added on top of the year's, for a what-if: ``ordinary`` more
    ordinary income (a Roth conversion) and ``ltcg`` more long-term gain."""
    ordinary: int = 0
    ltcg: int = 0


NO_EXTRA = Extra()


@dataclass(frozen=True)
class Return:
    """A computed return: named lines in cents, each written to
    ``tax_year_summary`` as ``<jurisdiction>_<line>``, and ``memo`` lines
    kept for the engine and not written.

    The engine reads two memo lines, each 0 when left out: ``drawn_ordinary``,
    the year's ordinary income drawn from traditional money (an RMD), and
    ``drawn_gain``, the long-term gain it realized. The after-tax drawdown's
    share of a later year replaces them, so it isn't taxed twice. Any other
    memo line is the calculator's own.

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


class TaxCalculator:
    """A tax calculator: a ``kind`` a jurisdiction file names, and how it
    turns a :class:`TaxYear` into returns."""
    kind: ClassVar[str]
    jurisdiction: ClassVar[str]
    """The jurisdiction it computes; until calculators are pluggable (#510)
    each kind computes one, whose file has that name."""

    def prepare(self, year: TaxYear) -> Prepared:
        raise NotImplementedError


KINDS: dict[str, type[TaxCalculator]] = {}
"""The registered calculators, by kind."""


def register[C: type[TaxCalculator]](cls: C) -> C:
    """Register a calculator under its ``kind``."""
    if cls.kind in KINDS:
        raise ValueError(f"tax calculator kind {cls.kind!r} is registered twice")
    KINDS[cls.kind] = cls
    return cls


def calculator(kind: str) -> TaxCalculator:
    """The calculator a jurisdiction's ``kind`` names."""
    import analysis.sim.taxes  # noqa: F401, PLC0415  (registers the built-ins)
    try:
        return KINDS[kind]()
    except KeyError:
        raise TaxError(f"unknown tax calculator kind {kind!r}") from None
