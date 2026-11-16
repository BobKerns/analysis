"""Where money comes from in a bad market (#463, ADR-010 §6.5).

``down_market`` is Guyton's withdrawal rule: don't sell what just fell.
``buckets`` is the cash-flow reserve and three-bucket plan (Evensky, Benz):
spend from cash, refill it from stocks when they gained, from bonds when
they didn't. Both read "how the market did" at a ``cadence``: ``monthly``
(the default) looks at the twelve months just finished, every month, so
nothing changes in a jump at New Year; ``annual`` looks at the last
calendar year, and ``buckets`` refills only in January, the once-a-year
review most households make.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from analysis.sim.strategies.spending import _cpi_since, _dollars
from analysis.sim.strategy_api import (
    ChainView, Context, Decision, Dollars, LotRef, Percent, ScenarioView, Strategy, Tag, Take,
    Transfer, register,
)

CADENCES = ("monthly", "annual")

# The bucket each asset class belongs to: 1 is spent first, 3 last.
BUCKET_OF = {"cash": 1, "cd": 1, "bond": 2, "stock": 3, "stock_us": 3, "stock_intl": 3}
BUCKET_NAMES = {1: "cash", 2: "bonds", 3: "stocks"}


def _check_cadence(params: Any) -> list[str]:
    problems = []
    if params.cadence not in CADENCES:
        problems.append(f"cadence {params.cadence!r}: monthly or annual")
    if not isinstance(params.lookback, int) or isinstance(params.lookback, bool) or params.lookback < 1:
        problems.append(f"lookback {params.lookback!r}: a whole number of years, 1 or more")
    return problems


def recent_return(ctx: Context, lots: Iterable[LotRef], cadence: str, lookback: int) -> float | None:
    """How ``lots`` did lately: over the ``12 × lookback`` months just
    finished (``monthly``), or the last ``lookback`` calendar years
    compounded (``annual``; a year before the run is left out). None when
    nothing is known yet."""
    lots = tuple(lots)
    if cadence == "monthly":
        return ctx.trailing_return(lots, 12 * lookback)
    growth, known = 1.0, False
    for year in range(ctx.year - lookback, ctx.year):
        r = ctx.annual_return(lots, year)
        if r is not None:
            growth *= 1 + r
            known = True
    return growth - 1 if known else None


def _period(ctx: Context, cadence: str, lookback: int) -> str:
    if cadence == "monthly":
        return f"over {12 * lookback} months"
    last = ctx.year - 1
    return f"in {last}" if lookback == 1 else f"in {last - lookback + 1}-{last}"


def _bucket(weights: dict[str, float] | Any) -> int | None:
    """The bucket holding most of a lot (ties go to the one spent first);
    None for a lot that is mostly real estate or other."""
    share = {1: 0.0, 2: 0.0, 3: 0.0}
    rest = 0.0
    for asset_class, weight in weights.items():
        if asset_class in BUCKET_OF:
            share[BUCKET_OF[asset_class]] += weight
        else:
            rest += weight
    best = max(share, key=lambda b: (share[b], -b))
    return best if share[best] > rest else None


def _wrapper(ctx: Context, ref: LotRef) -> str:
    """Which side of a retirement account a lot is on: one account can hold
    traditional and Roth lots, and money between them is a conversion."""
    subkind = ctx.lots[ref].lot_subkind or ""
    return "roth" if subkind.startswith("roth") else "traditional" if subkind.startswith("traditional") else ""


def _take_in_order(ctx: Context, order: Sequence[LotRef], need: int) -> dict[LotRef, int]:
    taken: dict[LotRef, int] = {}
    remaining = need
    for ref in order:
        if remaining <= 0:
            break
        amount = min(ctx.lots[ref].available, remaining)
        if amount > 0:
            taken[ref] = amount
            remaining -= amount
    return taken


@register
class DownMarket(Strategy):
    """Guyton's withdrawal rule: don't sell what just fell. Each draw takes
    from the owned chain's leaves in the chain's own order, skipping a leaf
    whose recent return is below ``threshold``. What the others can't cover
    falls through to the chain's steps, so a leaf that fell is sold last,
    not never. When no leaf fell the chain decides as it would anyway."""

    kind = "down_market"

    @dataclass(frozen=True)
    class Params:
        threshold: Percent = 0
        """A leaf whose recent return is below this is skipped."""
        lookback: int = 1
        """Years of returns that count as recent: 2 also spares a leaf that fell, rose a little, and is
        still under water."""
        cadence: str = "monthly"
        """monthly: the 12 x lookback months just finished, every draw; annual: the last lookback
        calendar years."""

    @classmethod
    def check(cls, params: Any, scenario: ScenarioView) -> Sequence[str]:
        return _check_cadence(params)

    def draw(self, ctx: Context, chain: ChainView, need: int) -> Decision | None:
        p = self.params
        if need <= 0:
            return None
        leaves = [ref for ref in chain.leaves if ref in ctx.lots]
        returns = {ref: recent_return(ctx, [ref], p.cadence, p.lookback) for ref in leaves}
        threshold = p.threshold / 1_000_000
        fallen = [ref for ref in leaves if (r := returns[ref]) is not None and r < threshold]
        if not fallen:
            return None
        taken = _take_in_order(ctx, [ref for ref in leaves if ref not in fallen], need)
        period = _period(ctx, p.cadence, p.lookback)
        skipped = ", ".join(f"{ref} ({returns[ref]:+.1%})" for ref in fallen)
        moved = ", ".join(f"{_dollars(a)} from {ref}" for ref, a in taken.items()) or "nothing"
        short = need - sum(taken.values())
        rest = f"; {_dollars(short)} left to the chain" if short > 0 else ""
        return Decision(
            [Take(ref, a) for ref, a in taken.items()],
            note=f"skipped {skipped} {period}: took {moved}{rest}",
            detail={
                "fallen": [str(ref) for ref in fallen],
                "returns": {str(ref): r for ref, r in returns.items() if r is not None},
                "need": need, "taken": sum(taken.values()),
            },
        )


@register
class Buckets(Strategy):
    """A cash reserve, refilled when markets allow (Evensky's cash-flow
    reserve, Benz's three buckets). The portfolio's lots fall into buckets
    by asset class: cash and CDs (1), bonds (2), stocks (3). Draws take
    cash first, then bonds, then stocks, stocks that fell coming last. The
    refill tops cash up to ``cash_years`` of ``need``, selling stocks when
    their recent return is at least ``threshold`` and bonds when it isn't,
    and tops bonds up to ``bond_years`` of ``need`` from stocks only when
    stocks gained. A refill stays inside each account: a stock lot sells
    into a cash lot of the same account, so it never crosses a tax wrapper.
    ``monthly`` refills each month; ``annual`` each January."""

    kind = "buckets"

    @dataclass(frozen=True)
    class Params:
        portfolio: Tag
        """Account tag of the lots in the buckets."""
        need: Dollars
        """Yearly spending from the portfolio, in dollars of the strategy's first month, raised with CPI."""
        cash_years: float = 2
        """Bucket 1's size, in years of need."""
        bond_years: float = 5
        """Bucket 2's size, in years of need; 0 for a cash-and-stocks plan."""
        threshold: Percent = 0
        """Stocks gained when their recent return is at least this."""
        lookback: int = 1
        """Years of returns that count as recent."""
        cadence: str = "monthly"
        """monthly: refill each month on the 12 x lookback months just finished; annual: refill each
        January on the last lookback calendar years."""

    @classmethod
    def check(cls, params: Any, scenario: ScenarioView) -> Sequence[str]:
        problems = _check_cadence(params)
        if params.need <= 0:
            problems.append("need must be more than $0")
        for name in ("cash_years", "bond_years"):
            value = getattr(params, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0):
                problems.append(f"{name} {value!r}: a number of years, 0 or more")
        # Each lot in the one bucket it is drawn and refilled as, as at run time.
        held = {_bucket(weights) for weights in scenario.portfolio_lots.get(params.portfolio, ())}
        wanted = {1, 3} | ({2} if params.bond_years else set())
        missing = sorted(wanted - held)
        if missing:
            names = {1: "cash or cd", 2: "bond", 3: "stock"}
            problems.append(
                f"portfolio {params.portfolio} has no lot mostly {' and none mostly '.join(names[b] for b in missing)}"
                ": buckets spends from and refills a lot in each bucket"
                + (" (a plan without bonds sets bond_years to 0)" if 2 in missing else ""))
        return problems

    # ----- draws -----

    def draw(self, ctx: Context, chain: ChainView, need: int) -> Decision | None:
        p = self.params
        if need <= 0:
            return None
        leaves = [ref for ref in chain.leaves if ref in ctx.lots]
        weights = ctx.allocation(leaves).weights
        bucket = {ref: _bucket(weights[ref]) for ref in leaves}
        threshold = p.threshold / 1_000_000
        stocks = [ref for ref in leaves if bucket[ref] == 3]
        returns = {ref: recent_return(ctx, [ref], p.cadence, p.lookback) for ref in stocks}
        fallen = {ref for ref, r in returns.items() if r is not None and r < threshold}

        def rank(ref: LotRef) -> int:
            b = bucket[ref]
            return 5 if b is None else 4 if ref in fallen else b

        order = sorted((ref for ref in leaves if bucket[ref] is not None), key=rank)  # stable: chain order
        taken = _take_in_order(ctx, order, need)
        if not taken:
            return None
        moved = ", ".join(
            f"{_dollars(a)} from {ref} ({BUCKET_NAMES[bucket[ref] or 3]}"
            f"{', fell ' + format(returns[ref], '+.1%') if ref in fallen else ''})"
            for ref, a in taken.items()
        )
        return Decision(
            [Take(ref, a) for ref, a in taken.items()],
            note=f"took {moved}",
            detail={"need": need, "taken": sum(taken.values()),
                    "fallen": sorted(str(ref) for ref in fallen)},
        )

    # ----- refills -----

    def month_start(self, ctx: Context) -> Decision | None:
        p = self.params
        memory = ctx.memory
        memory.setdefault("since", ctx.month.isoformat())
        if p.cadence == "annual" and ctx.month.month != 1:
            return None
        lots = ctx.tagged(p.portfolio)
        weights = ctx.allocation(lots).weights
        bucket = {ref: _bucket(weights[ref]) for ref in lots}
        held = {b: sum(max(0, ctx.lots[ref].balance) for ref in lots if bucket[ref] == b) for b in (1, 2, 3)}
        need = p.need * _cpi_since(ctx, memory["since"])
        target = {1: round(p.cash_years * need), 2: round(p.bond_years * need)}
        stocks = [ref for ref in lots if bucket[ref] == 3]
        stock_return = recent_return(ctx, stocks, p.cadence, p.lookback)
        up = stock_return is None or stock_return >= p.threshold / 1_000_000
        returns = {ref: recent_return(ctx, [ref], p.cadence, p.lookback) for ref in lots}
        spent: dict[LotRef, int] = {}
        moves: list[Transfer] = []
        parts: list[str] = []
        stuck: set[tuple[str, int]] = set()

        def refill(into: int, source: int) -> int:
            """Top bucket ``into`` up from bucket ``source``, account by account."""
            short = target[into] - held[into]
            if short <= 0:
                return 0
            sellers = sorted(
                (ref for ref in lots if bucket[ref] == source),
                key=lambda ref: -(returns[ref] or 0.0),  # most gained first
            )
            moved = 0
            for ref in sellers:
                if moved >= short:
                    break
                to = next((r for r in lots if r.account == ref.account and bucket[r] == into
                           and _wrapper(ctx, r) == _wrapper(ctx, ref)), None)
                if to is None:
                    if ctx.lots[ref].available - spent.get(ref, 0) > 0:
                        stuck.add((ref.account, into))
                    continue
                amount = min(ctx.lots[ref].available - spent.get(ref, 0), short - moved)
                if amount <= 0:
                    continue
                moves.append(Transfer(ref, to, amount))
                spent[ref] = spent.get(ref, 0) + amount
                moved += amount
            held[into] += moved
            held[source] -= moved
            return moved

        period = _period(ctx, p.cadence, p.lookback)
        how = "" if stock_return is None else f" ({stock_return:+.1%} {period})"
        cash = refill(1, 3 if up else 2)
        if cash:
            parts.append(f"cash to {_dollars(held[1])} ({p.cash_years:g} years): {_dollars(cash)} from "
                         f"{'stocks' + how if up else 'bonds'}")
        bonds = refill(2, 3) if up and p.bond_years else 0
        if bonds:
            parts.append(f"bonds to {_dollars(held[2])} ({p.bond_years:g} years): {_dollars(bonds)} from stocks")
        missing = [f"{BUCKET_NAMES[b]} lot in {account}" for account, b in sorted(stuck)]
        if not moves:
            # Nothing could move: say why, once while the reason holds (a
            # monthly note repeating it would bury the log).
            if not missing or memory.get("stuck") == missing:
                return None
            memory["stuck"] = missing
            return Decision([], note="could not refill: no " + ", no ".join(missing), detail={
                "need": round(need), "targets": target, "held": held, "stocks_up": up,
                "stock_return": stock_return, "cannot_refill": missing,
            })
        memory["stuck"] = missing
        prefix = "" if up else f"stocks {stock_return:+.1%} {period}, bonds not refilled: "
        note = prefix + "refilled " + "; ".join(parts)
        if missing:
            note += "; no " + ", no ".join(missing)
        return Decision(moves, note=note, detail={
            "need": round(need), "targets": target, "held": held, "stocks_up": up,
            "stock_return": stock_return,
            "cannot_refill": missing,
        })
