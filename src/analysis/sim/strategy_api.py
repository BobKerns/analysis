"""The API a responsive strategy is written against (#326, ADR-010).

This module is the only part of the simulator a strategy imports, whether it
is a built-in (``analysis.sim.strategies``) or a module in the data repo's
``strategies/`` folder. Engine internals change behind it; this surface
changes only with :data:`API_VERSION`.

A strategy is a :class:`Strategy` subclass marked with :func:`register`. It
reads a read-only :class:`Context` that ends at the current month and acts
only by returning a :class:`Decision`, whose moves the engine posts through
its own code (so lots, basis, tax character, refill and the ledger stay
right). Returning ``None`` from a hook means "no opinion": the chain's own
steps decide, exactly as they do without a strategy.

Money is integer cents and rates are integer millionths, as everywhere past
the loader. Shares and class weights are floats: they inform decisions, and
never become amounts without the strategy rounding them to whole cents.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from types import UnionType
from typing import (
    Annotated, Any, ClassVar, NamedTuple, Union, get_args, get_origin, get_type_hints,
)

__all__ = [
    "API_VERSION", "Account", "Allocation", "ChainView", "Context", "Decision", "Dollars",
    "LotRef", "LotView", "Percent", "PersonView", "Put", "Realize", "RuleView", "Spend",
    "StepView", "Strategy", "StrategyError", "Tag", "Take", "TargetSet", "TaxEstimate",
    "TaxView", "Transfer", "Unit", "is_long_term", "is_registered", "param_units", "percent",
    "register", "sale_gain",
]

API_VERSION = 3
"""Bumped when a strategy written against the previous version could break,
or one written for this version needs what the previous lacks. A module may
set ``API_VERSION`` to the version it was written for; one newer than this
is refused at load. 2 (#387) added ``year_start``, ``scale`` and
:class:`Spend`, and the context's ``end``, ``cpi``, ``people``, ``budget``
and ``annual_return``. 3 (#421) added ``year_end``, :class:`Transfer`,
:class:`Realize`, ``ctx.tax``, :data:`Account`, optional parameters, and
``LotView``'s ``acquired``, ``taxable_sale`` and ``gain``."""


class StrategyError(Exception):
    """A strategy, or its use, broke the API's rules (raised by the engine
    and loader with the strategy's name, month and hook)."""


# ---------- parameter units ----------

@dataclass(frozen=True)
class Unit:
    """How a ``Params`` field is written in a strategy file's ``params_json``
    and what the strategy receives: ``percent`` ("5%" -> 50000 millionths),
    ``dollars`` ("1200.00" -> 120000 cents), ``tag`` (an account tag
    ``role:name``), ``target_set`` (a set in ``targets.csv``), ``account``
    (an account's name)."""

    name: str


Percent = Annotated[int, Unit("percent")]
"""A share, written ``"5%"`` and received as millionths (50000)."""
Dollars = Annotated[int, Unit("dollars")]
"""Money, written ``"1200.00"`` and received as cents (120000)."""
Tag = Annotated[str, Unit("tag")]
"""An account tag ``role:name``; the loader checks some account carries it."""
TargetSet = Annotated[str, Unit("target_set")]
"""A target set's name in ``targets.csv``; the loader checks it exists."""
Account = Annotated[str, Unit("account")]
"""An account's name; the loader checks the scenario defines it."""


def percent(text: str) -> int:
    """``percent("5%")`` -> 50000: a ``Percent`` field's default in the units it
    is received in."""
    from analysis.sim import units

    return units.percent_to_micros(text)


def param_units(params: type) -> dict[str, Unit | None]:
    """Each ``Params`` field's :class:`Unit`, or None for a plain JSON value.
    An optional field (``Dollars | None = None``) has its value's unit, and
    takes ``null`` for "none"."""
    if not dataclasses.is_dataclass(params):
        raise StrategyError(f"Params must be a dataclass, not {params!r}")
    hints = get_type_hints(params, include_extras=True)
    out: dict[str, Unit | None] = {}
    for f in dataclasses.fields(params):
        hint = hints.get(f.name)
        unit = None
        if get_origin(hint) in (Union, UnionType):
            hint = next((a for a in get_args(hint) if get_origin(a) is Annotated), hint)
        if get_origin(hint) is Annotated:
            unit = next((a for a in get_args(hint)[1:] if isinstance(a, Unit)), None)
        out[f.name] = unit
    return out


# ---------- what a strategy sees ----------

class LotRef(NamedTuple):
    """A lot, by account and lot name. ``str()`` is ``account:lot``."""

    account: str
    lot: str

    def __str__(self) -> str:
        return f"{self.account}:{self.lot}"


@dataclass(frozen=True)
class LotView:
    """One lot at the current month. ``available`` is what a draw can take
    without dropping below the lot's minimum balance."""

    ref: LotRef
    kind: str
    subkind: str
    lot_subkind: str | None
    tax_treatment: str | None
    owner: str | None
    tags: frozenset[str]
    symbol: str | None
    balance: int
    cost_basis: int | None
    minimum: int
    roth_qualified: bool | None = None
    acquired: date | None = None
    """When a lot that tracks basis was bought, for its holding period; None
    is held long enough to be long-term (:func:`is_long_term`)."""
    taxable_sale: bool = False
    """A sale out of this lot realizes a taxable gain or loss: a taxable,
    non-cash lot with a cost basis, the lots :class:`Realize` takes."""

    @property
    def available(self) -> int:
        return max(0, self.balance - self.minimum)

    @property
    def gain(self) -> int | None:
        """Unrealized gain (negative: a loss), for a lot that tracks basis."""
        return None if self.cost_basis is None else self.balance - self.cost_basis


def is_long_term(acquired: date | None, sale: date) -> bool:
    """Held more than a year (IRC §1222): ``sale`` falls after the first
    anniversary of ``acquired``. A lot with no acquisition date is long-term."""
    if acquired is None:
        return True
    try:
        anniversary = acquired.replace(year=acquired.year + 1)
    except ValueError:  # Feb 29
        anniversary = date(acquired.year + 1, 2, 28)
    return sale > anniversary


def sale_gain(balance: int, basis: int, amount: int) -> int:
    """The gain (negative: a loss) the engine realizes selling ``amount``
    cents of a lot holding ``balance`` with ``basis``: the basis leaves pro
    rata, rounded down, so a strategy can size a sale to the cent."""
    basis_out = 0 if balance <= 0 else min(basis, basis * amount // balance)
    return amount - basis_out


@dataclass(frozen=True)
class StepView:
    """One step of a chain: what it draws from (a lot, or another chain by
    name), and its caps. The caps bind the static steps, not a strategy's own
    moves; a strategy that wants to honor them reads them here."""

    source: LotRef | str
    maximum: int | None
    percent: int


@dataclass(frozen=True)
class ChainView:
    """The chain a strategy owns: its steps, and its leaves (the lots its
    steps reach, recursively) in step order -- the household's own
    preference, and a natural tie-breaker."""

    name: str
    role: str
    steps: tuple[StepView, ...]
    leaves: tuple[LotRef, ...]


@dataclass(frozen=True)
class RuleView:
    """A scheduled rule the strategy owns, at one posting (``scale``)."""

    name: str
    kind: str
    tags: frozenset[str]
    """The rule's own tags, ``role:name``."""
    source: str
    """The chain or account it draws from."""
    destination: str
    """The chain or account it pays into."""
    base_amount: int
    """Its ``amount`` as written (cents, before inflation)."""
    series: str | None
    """The inflation series indexing it, or None for a fixed amount."""


@dataclass(frozen=True)
class PersonView:
    """A person in ``persons.csv`` as of the current month."""

    id: str
    role: str | None
    birthdate: date | None
    age: int | None
    """Whole years at the current month, or None without a birthdate."""
    alive: bool
    """False after the month their ``lifespan.csv`` row ends."""
    final_month: date | None
    """The last month they are alive, when a lifespan says (a plan, as
    the scenario's end is)."""


@dataclass(frozen=True)
class Allocation:
    """A set of lots split by asset class at the current month."""

    dollars: Mapping[str, int]
    """Cents per asset class (each lot's balance times its class weight)."""
    weights: Mapping[LotRef, Mapping[str, float]]
    """Each lot's split across classes, summing to 1."""

    @property
    def total(self) -> int:
        return sum(self.dollars.values())

    @property
    def shares(self) -> dict[str, float]:
        total = self.total
        return {c: (d / total if total else 0.0) for c, d in self.dollars.items()}

    def after(self, moves: Mapping[LotRef, int]) -> Allocation:
        """The allocation after adding ``moves`` (cents, negative to take out)
        to lots in the set; a lot outside it changes nothing."""
        dollars = dict(self.dollars)
        for ref, amount in moves.items():
            for asset_class, weight in self.weights.get(ref, {}).items():
                dollars[asset_class] = dollars.get(asset_class, 0) + round(amount * weight)
        return Allocation(dollars, self.weights)


@dataclass(frozen=True)
class TaxEstimate:
    """A tax year's income tax as the settlement would figure it, in cents."""

    tax: int
    taxable_income: int
    ordinary_taxable: int
    """The part of ``taxable_income`` that isn't long-term gain."""
    agi: int
    """Adjusted gross income, which is also IRMAA's MAGI (#253)."""


class TaxView:
    """``ctx.tax``: this year's income tax, from the transfers posted so far
    and the same estimator the April settlement uses, so a strategy and the
    settlement can't disagree. Each method asks about the year as it stands
    when the hook is called, with more ordinary income or long-term gain on
    top. Amounts are cents, rates millionths (``percent("22%")``)."""

    def __init__(
        self, *, year: int,
        estimator: Callable[[str], Any],
        ytd: Callable[[], Mapping[str, int]],
        last_year: Callable[[], Mapping[str, int] | None],
        irmaa_floors: Callable[[str], tuple[int, ...]],
    ) -> None:
        self._year = year
        self._estimator = estimator
        self._ytd = ytd
        self._last_year = last_year
        self._irmaa_floors = irmaa_floors
        self._estimates: dict[str, Any] = {}

    def _est(self, scope: str) -> Any:
        if scope not in ("federal", "state"):
            raise StrategyError(f"scope must be 'federal' or 'state', not {scope!r}")
        if scope not in self._estimates:
            self._estimates[scope] = self._estimator(scope)
        return self._estimates[scope]

    @property
    def year(self) -> int:
        return self._year

    @property
    def filing_status(self) -> str:
        """This year's federal filing status (it changes after a death)."""
        return str(self._est("federal").filing_status)

    @property
    def ytd(self) -> Mapping[str, int]:
        """This year's income so far by tax character, in cents."""
        return self._ytd()

    @property
    def last_year(self) -> Mapping[str, int] | None:
        """Last year's ``tax_year_summary`` buckets, or None before it is settled."""
        return self._last_year()

    def estimate(self, ordinary: int = 0, ltcg: int = 0, scope: str = "federal") -> TaxEstimate:
        """The year's tax with ``ordinary`` more ordinary income and ``ltcg``
        more long-term gain."""
        est = self._est(scope)
        taxable, ordinary_taxable = est.taxable_with(ordinary, ltcg)
        return TaxEstimate(
            tax=est.tax_with(ordinary, ltcg), taxable_income=taxable,
            ordinary_taxable=ordinary_taxable, agi=est.agi_with(ordinary, ltcg),
        )

    def marginal(
        self, ordinary: int = 0, ltcg: int = 0, *, next: str = "ordinary",
        scope: str = "federal", step: int = 10_000,
    ) -> int:
        """The tax on the next ``step`` cents as a rate (millionths), beyond
        ``ordinary`` and ``ltcg`` more; ``next`` says whether those cents are
        ``"ordinary"`` income or ``"ltcg"``. It counts what the bracket
        tables alone don't show: gains pushed into a higher band, Social
        Security made taxable."""
        if next not in ("ordinary", "ltcg"):
            raise StrategyError(f"next must be 'ordinary' or 'ltcg', not {next!r}")
        est = self._est(scope)
        more = (ordinary + step, ltcg) if next == "ordinary" else (ordinary, ltcg + step)
        return (est.tax_with(*more) - est.tax_with(ordinary, ltcg)) * 1_000_000 // step

    def bracket_top(self, rate: int, scope: str = "federal") -> int | None:
        """The taxable ordinary income where this year's bracket taxed at
        ``rate`` ends; None when no bracket has that rate, or it is the top."""
        return _band_top(self._est(scope).brackets, rate)

    def ltcg_band_top(self, rate: int) -> int | None:
        """The taxable income (ordinary and gains together) where this year's
        long-term gains band at ``rate`` ends: ``percent("0%")``'s is the most
        taxable income a gain can be realized under tax-free."""
        return _band_top(self._est("federal").ltcg_brackets, rate)

    def irmaa_floors(self) -> tuple[int, ...]:
        """This year's IRMAA tier floors (MAGI, cents) for the filing status,
        lowest first: this year's MAGI sets the premiums two years out.
        Empty without ``irmaa_tiers``."""
        return self._irmaa_floors(self.filing_status)


def _band_top(brackets: Sequence[tuple[int, int]], rate: int) -> int | None:
    floors = [floor for floor, r in brackets if r == rate]
    above = [floor for floor, _ in brackets if floors and floor > floors[0]]
    return min(above) if above else None


class Context:
    """What a strategy can read: the run's state as of the current month and
    nothing after it. The engine builds one per call; strategies never build
    their own (tests use :mod:`analysis.sim.strategy_testing`)."""

    def __init__(
        self, *, month: date, lots: Mapping[LotRef, LotView],
        weights: Callable[[LotRef], Mapping[str, float]],
        targets: Callable[[str], Mapping[str, float]],
        memory: dict[str, Any],
        end: date | None = None,
        cpi: Callable[[date], int] | None = None,
        people: Mapping[str, PersonView] | None = None,
        budget: Callable[[], int] | None = None,
        annual_return: Callable[[tuple[LotRef, ...], int], float | None] | None = None,
        tax: TaxView | None = None,
    ) -> None:
        self._month = month
        self._lots = lots
        self._weights = weights
        self._targets = targets
        self._end = end
        self._cpi = cpi
        self._people = people if people is not None else {}
        self._budget = budget
        self._annual_return = annual_return
        self._tax = tax
        self.memory = memory
        """The strategy's own state, kept across calls. JSON values only."""

    @property
    def month(self) -> date:
        return self._month

    @property
    def year(self) -> int:
        return self._month.year

    @property
    def lots(self) -> Mapping[LotRef, LotView]:
        return self._lots

    def tagged(self, tag: str) -> tuple[LotRef, ...]:
        """The asset lots of every account carrying ``tag`` (``role:name``)."""
        return tuple(
            ref for ref, view in self._lots.items()
            if view.kind == "asset" and tag in view.tags
        )

    def allocation(self, lots: Iterable[LotRef]) -> Allocation:
        """``lots`` split by asset class as of this month (the same rules as
        ``sim allocation``)."""
        dollars: dict[str, int] = {}
        weights: dict[LotRef, Mapping[str, float]] = {}
        for ref in lots:
            w = self._weights(ref)
            weights[ref] = w
            balance = self._lots[ref].balance
            for asset_class, share in w.items():
                dollars[asset_class] = dollars.get(asset_class, 0) + round(balance * share)
        return Allocation(dollars, weights)

    def targets(self, target_set: str) -> dict[str, float]:
        """``target_set``'s shares in force this month (classes it leaves
        out are 0)."""
        return dict(self._targets(target_set))

    @property
    def end(self) -> date:
        """The scenario's last month: the plan's horizon, not future data."""
        if self._end is None:
            raise StrategyError("this context has no end month")
        return self._end

    @property
    def people(self) -> Mapping[str, PersonView]:
        """Each person by id, as of this month."""
        return self._people

    def cpi(self, since: date) -> int:
        """The CPI factor from the month ending ``since`` (no later than
        this month) to this month, in millionths: 1050000 is 5% inflation.
        Multiply cents by it and divide by 1000000 for today's dollars."""
        if since > self._month:
            raise StrategyError(f"cpi({since.isoformat()}) is after {self._month.isoformat()}")
        if self._cpi is None:
            raise StrategyError("this context has no CPI series")
        return self._cpi(since)

    def budget(self) -> int:
        """The strategy's own rules' spending over the twelve months from
        this one at this month's prices, in cents: each rule's amount,
        indexed to now, times the times it posts in them, stopping at the
        run's end. Today's plan, not a forecast of inflation. Zero for a
        strategy that owns no rules."""
        return self._budget() if self._budget is not None else 0

    def annual_return(self, lots: Iterable[LotRef], year: int) -> float | None:
        """A finished year's investment return on ``lots``: the growth
        posted to them that year over their balance at its start (the
        run's opening balance in its first year). None when they held
        nothing then, or the year is before the run."""
        if year >= self.year:
            raise StrategyError(f"annual_return({year}) needs a year before {self.year}")
        if self._annual_return is None:
            return None
        return self._annual_return(tuple(lots), year)

    @property
    def tax(self) -> TaxView:
        """This year's income tax so far, and what more income would cost."""
        if self._tax is None:
            raise StrategyError("this context has no tax view")
        return self._tax


# ---------- what a strategy returns ----------

@dataclass(frozen=True)
class Take:
    """Draw ``amount`` cents from ``lot`` (a leaf of the owned source chain)."""

    lot: LotRef
    amount: int


@dataclass(frozen=True)
class Put:
    """Place ``amount`` cents into ``lot`` (a leaf of the owned destination chain)."""

    lot: LotRef
    amount: int


@dataclass(frozen=True)
class Transfer:
    """Move ``amount`` cents out of the asset lot ``source`` into ``to``
    (``year_end``): another asset lot, or an expense account by name (a
    gift). It posts as a ``one_off_transfer`` with a blank tax character
    does: out of a taxable lot it is a sale, from a traditional lot into a
    Roth lot a conversion, out of a Roth lot a distribution. ``in_kind``
    (into an expense only, from a lot that tracks basis) gives the shares
    themselves: their basis leaves with them and no gain is realized."""

    source: LotRef
    to: LotRef | str
    amount: int
    in_kind: bool = False


@dataclass(frozen=True)
class Realize:
    """Sell and rebuy ``amount`` cents of a taxable lot (``year_end``): its
    gain or loss (:func:`sale_gain`) is realized this year and its basis
    moves to the sale value; the balance and holding period stay."""

    lot: LotRef
    amount: int


@dataclass(frozen=True)
class Spend:
    """Post ``amount`` cents of an owned rule's posting (``scale``): 0 skips
    it, and more than it asked raises it."""

    amount: int


@dataclass(frozen=True)
class Decision:
    """A hook's answer: its moves, a one-line ``note`` a person reads in
    ``sim strategy log``, and ``detail`` (JSON values) with the numbers
    behind it."""

    moves: Sequence[Take | Put | Spend | Transfer | Realize]
    note: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)

    def detail_json(self) -> str:
        return json.dumps(dict(self.detail), sort_keys=True, separators=(",", ":"))


# ---------- the strategy ----------

@dataclass(frozen=True)
class _NoParams:
    """A kind that takes no parameters."""


class Strategy:
    """Base class. A subclass sets ``kind`` (its name in a strategy file's ``kind``),
    a frozen dataclass ``Params`` (fields typed with :data:`Percent`,
    :data:`Dollars`, :data:`Tag`, :data:`TargetSet`, or plain JSON types),
    a docstring (shown by ``sim strategy kinds``), and overrides the hooks it
    uses. Every hook defaults to "no opinion".

    A strategy must be a pure function of its context, parameters and
    ``ctx.memory``: no clock, files, network or unseeded random numbers, so a
    run is the same every time (ADR-004). ``sim strategy check`` runs a
    scenario twice and compares the decisions."""

    kind: ClassVar[str]

    Params: type[Any] = _NoParams
    """The kind's parameters: a frozen dataclass nested in the subclass."""

    hooks: ClassVar[frozenset[str]] = frozenset()
    """The hooks a kind implements, filled in by :func:`register`."""

    def __init__(self, name: str, params: Any) -> None:
        self.name = name
        self.params = params

    def draw(self, ctx: Context, chain: ChainView, need: int) -> Decision | None:
        """Choose which leaves of the owned source ``chain`` pay ``need``
        cents. Whatever the moves leave unpaid falls through to the chain's
        own steps."""
        return None

    def place(self, ctx: Context, chain: ChainView, amount: int) -> Decision | None:
        """Choose which leaves of the owned destination ``chain`` receive
        ``amount`` cents. What the moves leave falls through to its steps."""
        return None

    def year_start(self, ctx: Context) -> Decision | None:
        """Asked in the strategy's first month and each January after, before
        any rule posts: set the year's course in ``ctx.memory``. It moves
        nothing; a returned decision (no moves) is recorded with its note."""
        return None

    def scale(self, ctx: Context, rule: RuleView, amount: int) -> Decision | None:
        """Asked before each posting of a rule the strategy owns (by tag), with
        the ``amount`` it would post: the budgeted amount. Answer with one
        :class:`Spend` to post that instead; None posts the budget."""
        return None

    def year_end(self, ctx: Context) -> Decision | None:
        """Asked each December, after every rule has posted and before the
        month-end settlement: answer with :class:`Transfer` and
        :class:`Realize` moves, posted in order (a later one sees the
        balances an earlier one left). ``ctx.tax`` sees the year's income."""
        return None


_HOOKS = ("draw", "place", "year_start", "scale", "year_end")
_REGISTERED = "__strategy_registered__"


def register[S: type[Strategy]](cls: S) -> S:
    """Class decorator: make ``cls`` a strategy kind."""
    if not (isinstance(cls, type) and issubclass(cls, Strategy)):
        raise StrategyError(f"@register needs a Strategy subclass, not {cls!r}")
    kind = cls.__dict__.get("kind")
    if not isinstance(kind, str) or not kind.isidentifier():
        raise StrategyError(f"{cls.__name__}: kind must be set to an identifier, got {kind!r}")
    if not dataclasses.is_dataclass(cls.Params):
        raise StrategyError(f"{cls.__name__}: Params must be a dataclass")
    cls.hooks = frozenset(h for h in _HOOKS if getattr(cls, h) is not getattr(Strategy, h))
    if not cls.hooks:
        raise StrategyError(f"{cls.__name__}: implements no hook ({', '.join(_HOOKS)})")
    setattr(cls, _REGISTERED, True)
    return cls


def is_registered(obj: object) -> bool:
    """Whether ``obj`` is a class marked by :func:`register` itself (not one
    inheriting the mark from a registered base)."""
    return isinstance(obj, type) and _REGISTERED in obj.__dict__
