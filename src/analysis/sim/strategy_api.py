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
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import (
    TYPE_CHECKING, Any, ClassVar, NamedTuple,
)

from analysis.sim import param_units as param_units_module
from analysis.sim.param_units import Account, Dollars, Percent, Tag, TargetSet, Unit, percent
from analysis.sim import registries, tax_api

if TYPE_CHECKING:
    import numpy as np

    from analysis.sim.measures import RailYears

__all__ = [
    "API_VERSION", "Account", "Allocation", "BreachView", "ChainView", "Context", "Decision", "Dollars",
    "History", "LotRef", "LotView", "Mortality", "Percent", "PersonView", "Put", "Realize", "RuleView", "ScenarioView",
    "Spend", "StepView", "Strategy", "StrategyError", "Tag", "Take", "TargetSet", "TaxEstimate",
    "TaxView", "Transfer", "Unit", "is_long_term", "is_registered", "param_units", "percent",
    "register", "sale_gain",
]

API_VERSION = 10
"""Bumped when a strategy written against the previous version could break,
or one written for this version needs what the previous lacks. A module may
set ``API_VERSION`` to the version it was written for; one newer than this
is refused at load. 2 (#387) added ``year_start``, ``scale`` and
:class:`Spend`, and the context's ``end``, ``cpi``, ``people``, ``budget``
and ``annual_return``. 3 (#421) added ``year_end``, :class:`Transfer`,
:class:`Realize`, ``ctx.tax``, :data:`Account`, optional parameters, and
``LotView``'s ``acquired``, ``taxable_sale`` and ``gain``. 4 (#441) added
``month_start``, ``on_breach`` and :class:`BreachView`, ``ctx.history``
and ``ctx.rail``. 5 (#459) added ``ctx.survival_years``,
``ctx.life_expectancy`` and ``ctx.rmd_divisor``, and :class:`ScenarioView`'s
``people``, ``aux`` and ``portfolio_classes``. 6 (#463) added
``ctx.trailing_return`` and :class:`ScenarioView`'s ``portfolio_lots``. 7
(#507) replaced :class:`ScenarioView`'s ``tax_rules`` (counts by scope) with
``taxes``, the set of the scenario's tax jurisdictions. 8 (#507) renamed
the tax summary's lines. 9 (#508) made the local tax its own jurisdiction:
``ctx.tax`` takes ``scope="local"``, the state's estimate no longer includes
it, and ``last_year``'s ``state_local_tax`` is ``local_tax``."""


class StrategyError(Exception):
    """A strategy, or its use, broke the API's rules (raised by the engine
    and loader with the strategy's name, month and hook)."""


# ---------- parameter units (shared with tax calculators, #510) ----------

def param_units(params: type) -> dict[str, Unit | None]:
    """Each ``Params`` field's :class:`Unit`, or None for a plain JSON value
    (:func:`analysis.sim.param_units.param_units`)."""
    try:
        return param_units_module.param_units(params)
    except TypeError as e:
        raise StrategyError(str(e)) from None


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
    """Adjusted gross income."""
    magi: int
    """IRMAA's MAGI (#253): AGI plus tax-exempt interest (#265)."""


_RENAMED_LINES = {
    "adjusted_gross_income": "agi", "tax_liability": "tax", "deduction_used": "deduction",
    "ss_income": "ss_benefits", "taxable_ss": "ss_taxable", "ltcg_income": "ltcg_taxed",
    "capital_loss_deduction": "capital_loss_used", "schedule_c_net": "business_net",
    "hobby_expense_disallowed": "hobby_disallowed",
}
"""The ``tax_year_summary`` lines API 8 renamed (#507), old to new."""

_RETIRED_BUCKETS = {"state_local_tax": ("local_tax", 9)}
"""Whole buckets a later API retired: the local tax is its own jurisdiction
since 9 (#508)."""


class _LastYear(Mapping[str, int]):
    """Last year's summary, refusing a bucket name API 8 or 9 retired."""

    def __init__(self, summary: Mapping[str, int]) -> None:
        self._summary = summary

    def _check(self, key: object) -> None:
        if isinstance(key, str) and key not in self._summary:
            if key in _RETIRED_BUCKETS:
                new, version = _RETIRED_BUCKETS[key]
                raise StrategyError(
                    f"ctx.tax.last_year has no {key!r} since strategy API {version}: it is {new}")
            scope, _, line = key.partition("_")
            if line in _RENAMED_LINES:
                raise StrategyError(
                    f"ctx.tax.last_year has no {key!r} since strategy API 8: "
                    f"it is {scope}_{_RENAMED_LINES[line]!s}")

    def __getitem__(self, key: str) -> int:
        self._check(key)
        return self._summary[key]

    def __contains__(self, key: object) -> bool:
        self._check(key)
        return key in self._summary

    def __iter__(self) -> Iterator[str]:
        return iter(self._summary)

    def __len__(self) -> int:
        return len(self._summary)


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
        if scope not in registries.TAX_JURISDICTIONS:
            raise StrategyError(
                f"scope must be one of {', '.join(map(repr, registries.TAX_JURISDICTIONS))}, "
                f"not {scope!r}")
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
        """Last year's ``tax_year_summary`` buckets, or None before it is
        settled. A bucket's name from before API 8 (or ``state_local_tax``,
        from before 9) is refused, naming the new one, so an older strategy
        fails rather than reading 0."""
        summary = self._last_year()
        return None if summary is None else _LastYear(summary)

    def estimate(self, ordinary: int = 0, ltcg: int = 0, scope: str = "federal") -> TaxEstimate:
        """The year's tax with ``ordinary`` more ordinary income and ``ltcg``
        more long-term gain."""
        ret = self._est(scope).compute(tax_api.Extra(ordinary, ltcg))
        return TaxEstimate(
            tax=ret.tax, taxable_income=ret.taxable_income,
            ordinary_taxable=ret.ordinary_taxable, agi=ret.agi, magi=ret.magi,
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
        return (est.compute(tax_api.Extra(*more)).tax
                - est.compute(tax_api.Extra(ordinary, ltcg)).tax) * 1_000_000 // step

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


@dataclass(frozen=True)
class BreachView:
    """A guard rail a strategy's ``trigger`` names, past its line or back
    from it (#441), as ``on_breach`` is told each January about the year
    just ended. Money is in CPI-constant cents, as ``sim report
    guard-rails`` shows it: dollars of the run's first month.

    ``margin`` is the distance from the line for ``margin_kind``: the year's
    own (``annual``) or the sum since the rail began (``cumulative``).
    Negative is past it, whichever way the rail faces."""

    rail: str
    description: str
    margin_kind: str
    """``annual`` or ``cumulative``."""
    year: int
    measure: int
    """The year's spending or withdrawals."""
    threshold: int
    """The year's line."""
    margin: int
    sense: str
    """``negative`` (a line to stay inside) or ``positive`` (room to use)."""
    state: str
    """``entered`` (past it, not the year before), ``continued`` or ``left``
    (back inside after being past)."""
    years_past: int
    """Consecutive years past it, this one included; 0 when ``left``."""


class History:
    """Month-end balances from the run's first month to last month (#441):
    nothing from this month or later."""

    def __init__(
        self, months: Callable[[], tuple[date, ...]],
        balances: Callable[[tuple[LotRef, ...]], np.ndarray],
    ) -> None:
        self._months = months
        self._balances = balances

    @property
    def months(self) -> tuple[date, ...]:
        """The month ends, oldest first."""
        return self._months()

    def balances(self, lots: Iterable[LotRef]) -> np.ndarray:
        """``lots``' total balance at each of :attr:`months`, in cents
        (an int64 array of the same length)."""
        return self._balances(tuple(lots))


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
        trailing_return: Callable[[tuple[LotRef, ...], int], float | None] | None = None,
        tax: TaxView | None = None,
        history: History | None = None,
        rail: Callable[[str], RailYears] | None = None,
        mortality: Mortality | None = None,
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
        self._trailing_return = trailing_return
        self._tax = tax
        self._history = history
        self._rail = rail
        self._mortality = mortality
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

    def trailing_return(self, lots: Iterable[LotRef], months: int) -> float | None:
        """The investment return on ``lots`` over the ``months`` finished
        months before this one (#463): the growth posted to them in those
        months over their balance at the window's start. A window reaching
        back before the run starts with the run (its opening balance), so it
        is shorter. None when no month has finished yet, or they held
        nothing at the window's start."""
        if not isinstance(months, int) or isinstance(months, bool) or months < 1:
            raise StrategyError(f"trailing_return needs a whole number of months >= 1, not {months!r}")
        if self._trailing_return is None:
            return None
        return self._trailing_return(tuple(lots), months)

    @property
    def tax(self) -> TaxView:
        """This year's income tax so far, and what more income would cost."""
        if self._tax is None:
            raise StrategyError("this context has no tax view")
        return self._tax

    @property
    def history(self) -> History:
        """Month-end balances before this month."""
        if self._history is None:
            raise StrategyError("this context has no history")
        return self._history

    def rail(self, name: str) -> RailYears:
        """A triggered rail's years so far (``measures.RailYears``, one row,
        CPI-constant dollars), as last evaluated in January: through the
        year before the current one. Only rails some strategy's ``trigger``
        names are evaluated during a run."""
        if self._rail is None:
            raise StrategyError(f"no rail {name!r} is triggered in this run")
        return self._rail(name)

    def _household(self, people: Iterable[str] | None) -> tuple[str, ...]:
        if people is not None:
            return tuple(people)
        return tuple(pid for pid, p in self._people.items() if p.alive and p.role in _PERSON_ROLES)

    def _need_mortality(self, what: str) -> Mortality:
        if self._mortality is None:
            raise StrategyError(f"{what}: this context has no life or RMD tables")
        return self._mortality

    def survival_years(self, probability: float, people: Iterable[str] | None = None) -> int:
        """Whole years from now until the chance that at least one of
        ``people`` (default: the living primary and secondary persons) is
        still alive falls below ``probability`` (0.1 for 10%), from each one's
        life table at their age this month (#459). The table is the one their
        ``lifespan.csv`` row names, read with its set-forward, else
        ``life_table.csv``: the household's stated mortality, never the
        lifespan an ensemble member drew. At least 1."""
        if not 0 < probability < 1:
            raise StrategyError(f"survival_years({probability}): a probability between 0 and 1")
        who = self._household(people)
        if not who:
            raise StrategyError("survival_years: nobody in the household is alive")
        return self._need_mortality("survival_years").survival_years(probability, who, self._month)

    def life_expectancy(self, person: str) -> float:
        """``person``'s remaining life expectancy in years at this month, from
        the same table as :meth:`survival_years` (#459)."""
        return self._need_mortality("life_expectancy").life_expectancy(person, self._month)

    def rmd_divisor(self, person: str) -> float | None:
        """This year's ``rmd_factors`` divisor (the IRS Uniform Lifetime
        Table) at ``person``'s age at the end of the year, or None where the
        table has no row for that age (below its first) (#459)."""
        return self._need_mortality("rmd_divisor").rmd_divisor(person, self._month)


_PERSON_ROLES = frozenset({"primary", "secondary"})


class Mortality:
    """What ``ctx.survival_years``, ``life_expectancy`` and ``rmd_divisor``
    read: the engine's life and RMD tables. Strategies don't build one."""

    def survival_years(self, probability: float, people: tuple[str, ...], month: date) -> int:
        raise NotImplementedError

    def life_expectancy(self, person: str, month: date) -> float:
        raise NotImplementedError

    def rmd_divisor(self, person: str, month: date) -> float | None:
        raise NotImplementedError


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
    (``year_end``, ``month_start``, ``on_breach``): another asset lot, or an expense account by name (a
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
    """Sell and rebuy ``amount`` cents of a taxable lot (``year_end``,
    ``month_start``, ``on_breach``): its
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

    def month_start(self, ctx: Context) -> Decision | None:
        """Asked each month, after its calendar events, inflation and (in
        January) ``year_start``, before any rule posts (#441): answer with
        :class:`Transfer` and :class:`Realize` moves, as ``year_end`` does.
        Where a balance condition lives: ``ctx.lots`` is the month's start."""
        return None

    def on_breach(self, ctx: Context, breach: BreachView) -> Decision | None:
        """Asked each January about a rail the strategy's ``trigger`` names
        (#441): when the year just ended was past it, and once more the year
        it comes back. Before ``year_start``, so memory set here steers the
        year; it may move money as ``month_start`` does."""
        return None


    @classmethod
    def check(cls, params: Any, scenario: ScenarioView) -> Sequence[str]:
        """Problems with ``params`` (a ``Params``) in ``scenario``, found at
        load, each a short phrase; none by default. For what a parameter's
        unit can't say, such as a tax rule the kind needs (#421)."""
        return ()


@dataclass(frozen=True)
class ScenarioView:
    """What :meth:`Strategy.check` sees of the scenario at load (#421)."""

    accounts: Mapping[str, tuple[str, str]]
    """Each account's ``(kind, subkind)``."""
    taxes: frozenset[str]
    """The scenario's tax jurisdictions (``federal``, ``state``): a
    ``taxes/<jurisdiction>.csv`` file each (#507)."""
    people: Mapping[str, tuple[str | None, str | None, str | None]] = field(default_factory=dict)
    """Each person's ``(role, sex, life table)`` from persons.csv and
    lifespan.csv, blank as None: the table is the name a lifespan row gives
    (``life_table__<name>.csv``), None for ``life_table.csv`` (#459)."""
    aux: frozenset[str] = frozenset()
    """The aux files the scenario has, by file name without ``.csv``
    (``rmd_factors``, ``life_table``, ``life_table__jp``, ...)."""
    portfolio_classes: Mapping[str, frozenset[str]] = field(default_factory=dict)
    """Each account tag's asset classes: those its asset lots hold in the
    scenario's first month (#459)."""
    portfolio_lots: Mapping[str, tuple[Mapping[str, float], ...]] = field(default_factory=dict)
    """Each account tag's asset lots, as each lot's class weights in the
    scenario's first month (#463)."""


_HOOKS = ("draw", "place", "year_start", "scale", "year_end", "month_start", "on_breach")
HOOKS = _HOOKS  # the vocabulary, for commands that filter by hook
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
