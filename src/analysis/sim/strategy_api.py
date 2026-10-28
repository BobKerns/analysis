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
from typing import Annotated, Any, ClassVar, NamedTuple, get_args, get_origin, get_type_hints

__all__ = [
    "API_VERSION", "Allocation", "ChainView", "Context", "Decision", "Dollars", "LotRef",
    "LotView", "Percent", "Put", "StepView", "Strategy", "StrategyError", "Tag", "Take",
    "TargetSet", "Unit", "is_registered", "param_units", "percent", "register",
]

API_VERSION = 1
"""Bumped when a strategy written against the previous version could break.
A module may set ``API_VERSION`` to the version it was written for; one newer
than this is refused at load."""


class StrategyError(Exception):
    """A strategy, or its use, broke the API's rules (raised by the engine
    and loader with the strategy's name, month and hook)."""


# ---------- parameter units ----------

@dataclass(frozen=True)
class Unit:
    """How a ``Params`` field is written in ``strategies.csv``'s ``params_json``
    and what the strategy receives: ``percent`` ("5%" -> 50000 millionths),
    ``dollars`` ("1200.00" -> 120000 cents), ``tag`` (an account tag
    ``role:name``), ``target_set`` (a set in ``targets.csv``)."""

    name: str


Percent = Annotated[int, Unit("percent")]
"""A share, written ``"5%"`` and received as millionths (50000)."""
Dollars = Annotated[int, Unit("dollars")]
"""Money, written ``"1200.00"`` and received as cents (120000)."""
Tag = Annotated[str, Unit("tag")]
"""An account tag ``role:name``; the loader checks some account carries it."""
TargetSet = Annotated[str, Unit("target_set")]
"""A target set's name in ``targets.csv``; the loader checks it exists."""


def percent(text: str) -> int:
    """``percent("5%")`` -> 50000: a ``Percent`` field's default in the units it
    is received in."""
    from analysis.sim import units

    return units.percent_to_micros(text)


def param_units(params: type) -> dict[str, Unit | None]:
    """Each ``Params`` field's :class:`Unit`, or None for a plain JSON value."""
    if not dataclasses.is_dataclass(params):
        raise StrategyError(f"Params must be a dataclass, not {params!r}")
    hints = get_type_hints(params, include_extras=True)
    out: dict[str, Unit | None] = {}
    for f in dataclasses.fields(params):
        hint = hints.get(f.name)
        unit = None
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

    @property
    def available(self) -> int:
        return max(0, self.balance - self.minimum)


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


class Context:
    """What a strategy can read: the run's state as of the current month and
    nothing after it. The engine builds one per call; strategies never build
    their own (tests use :mod:`analysis.sim.strategy_testing`)."""

    def __init__(
        self, *, month: date, lots: Mapping[LotRef, LotView],
        weights: Callable[[LotRef], Mapping[str, float]],
        targets: Callable[[str], Mapping[str, float]],
        memory: dict[str, Any],
    ) -> None:
        self._month = month
        self._lots = lots
        self._weights = weights
        self._targets = targets
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
class Decision:
    """A hook's answer: its moves, a one-line ``note`` a person reads in
    ``sim strategy log``, and ``detail`` (JSON values) with the numbers
    behind it."""

    moves: Sequence[Take | Put]
    note: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)

    def detail_json(self) -> str:
        return json.dumps(dict(self.detail), sort_keys=True, separators=(",", ":"))


# ---------- the strategy ----------

@dataclass(frozen=True)
class _NoParams:
    """A kind that takes no parameters."""


class Strategy:
    """Base class. A subclass sets ``kind`` (its name in ``strategies.csv``),
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


_HOOKS = ("draw", "place")
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
