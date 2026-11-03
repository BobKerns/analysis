"""Year-end actions (#421): ``harvest`` and ``gift``.

Both act each December, after every rule has posted, on the year's income as
the April settlement will see it (``ctx.tax``). Neither owns a chain or
rules: each acts on the lots of its ``portfolio`` tag.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date

from analysis.sim.strategy_api import (
    Account, Context, Decision, Dollars, LotView, Percent, Realize, ScenarioView, Strategy,
    StrategyError, Tag, Transfer, is_long_term, percent, register, sale_gain,
)

TAX_STEP = 10_000
"""How far a marginal-rate check looks ahead: the tax on the next $100."""


def _dollars(cents: float) -> str:
    return f"${cents / 100:,.0f}"


def _rate(micros: int) -> str:
    return f"{micros / 10_000:g}%"


def _federal_rule(scenario: ScenarioView, what: str) -> list[str]:
    """The load problem when ``ctx.tax`` would have no single federal rule to read."""
    count = scenario.tax_rules.get("federal", 0)
    if count == 1:
        return []
    found = "there is none" if count == 0 else f"there are {count}"
    return [f"{what} measures with the federal tax_accumulator rule, and {found}"]


def _largest_within(fits: Callable[[int], bool], cap: int) -> int:
    """The largest amount from 0 to ``cap`` that ``fits`` (true up to some
    point, false after it), by bisection."""
    if fits(cap):
        return cap
    low, high = 0, cap
    while high - low > 1:
        middle = (low + high) // 2
        if fits(middle):
            low = middle
        else:
            high = middle
    return low


def _share(view: LotView) -> float:
    """The part of a lot's balance that is gain."""
    return (view.gain or 0) / view.balance if view.balance > 0 else 0.0


def _sale_for_gain(view: LotView, gain: int) -> int:
    """The largest sale out of ``view`` whose gain is at most ``gain``."""
    assert view.cost_basis is not None
    held = view.gain or 0
    if gain >= held:
        return view.balance
    amount = gain * view.balance // held
    while amount > 0 and sale_gain(view.balance, view.cost_basis, amount) > gain:
        amount -= 1
    while amount < view.balance and sale_gain(view.balance, view.cost_basis, amount + 1) <= gain:
        amount += 1
    return amount


def _cpi_since(ctx: Context, since: str) -> float:
    """Inflation from ``since`` (a month end) to now, as a factor; 1 when the
    scenario has no CPI series."""
    try:
        return ctx.cpi(date.fromisoformat(since)) / 1_000_000
    except StrategyError:
        return 1.0


@register
class Harvest(Strategy):
    """Gain and loss harvesting each December. With ``gains``, long-term
    gains in the portfolio's taxable lots are realized (sold and rebought, so
    the holdings stay) until taxable income reaches the top of the long-term
    gains ``band`` (0%: tax-free gains), largest gain share first, which
    raises the most basis per dollar sold. It stops sooner where the next
    $100 of gain would cost more than ``max_marginal_rate``: a gain can make
    Social Security taxable, so a 0% band isn't always 0%. With ``losses``,
    each taxable lot down by at least ``loss_minimum`` is sold and rebought
    at its loss instead, modeling a switch to a similar fund (no wash sale);
    a year that harvests losses harvests no gains, which would use them up."""

    kind = "harvest"

    @dataclass(frozen=True)
    class Params:
        portfolio: Tag
        """Account tag whose taxable asset lots are harvested."""
        gains: bool = True
        """Harvest long-term gains up to the band's top."""
        band: Percent = percent("0%")
        """The long-term gains band to fill: 0% for tax-free gains."""
        max_marginal_rate: Percent | None = None
        """The most the next $100 of gain may cost; blank: the band's own rate."""
        losses: bool = False
        """Harvest losses instead, in a year a lot is down by loss_minimum."""
        loss_minimum: Dollars = 100_000
        """The smallest loss on a lot worth harvesting."""

    @classmethod
    def check(cls, params: Harvest.Params, scenario: ScenarioView) -> list[str]:
        return _federal_rule(scenario, "harvesting gains") if params.gains else []

    def year_end(self, ctx: Context) -> Decision | None:
        p = self.params
        lots = [ctx.lots[ref] for ref in ctx.tagged(p.portfolio)]
        sellable = [v for v in lots if v.taxable_sale and v.balance > 0 and v.cost_basis is not None]
        if p.losses:
            losers = sorted(
                (v for v in sellable if (v.gain or 0) <= -p.loss_minimum),
                key=lambda v: (v.gain or 0, str(v.ref)),
            )
            if losers:
                total = sum(v.gain or 0 for v in losers)
                return Decision(
                    [Realize(v.ref, v.balance) for v in losers],
                    note=f"harvested {_dollars(-total)} of losses in {len(losers)} lot(s)",
                    detail={"losses": {str(v.ref): v.gain for v in losers}},
                )
        if p.gains:
            return self._gains(ctx, sellable)
        return None

    def _gains(self, ctx: Context, sellable: Sequence[LotView]) -> Decision | None:
        p = self.params
        winners = sorted(
            (v for v in sellable if (v.gain or 0) > 0 and is_long_term(v.acquired, ctx.month)),
            key=lambda v: (-_share(v), str(v.ref)),
        )
        if not winners:
            return None
        top = ctx.tax.ltcg_band_top(p.band)
        if top is None:
            raise StrategyError(f"no long-term gains band taxed at {_rate(p.band)} below the top one")
        before = ctx.tax.estimate().taxable_income
        if before >= top:
            return None
        held = sum(v.gain or 0 for v in winners)
        room = _largest_within(lambda g: ctx.tax.estimate(ltcg=g).taxable_income <= top, held)
        limit = p.band if p.max_marginal_rate is None else p.max_marginal_rate
        gain = 0
        while gain < room:
            step = min(TAX_STEP, room - gain)
            if ctx.tax.marginal(ltcg=gain, next="ltcg", step=step) > limit:
                break
            gain += step
        if gain == 0:
            cost = ctx.tax.marginal(next="ltcg", step=min(TAX_STEP, room))
            return Decision([], note=(
                f"no harvest: the first $100 of gain would cost {_rate(cost)} "
                f"(limit {_rate(limit)}), e.g. by making Social Security taxable"),
                detail={"band_top": top, "taxable_before": before, "room": room, "limit": limit,
                        "marginal": cost})
        moves: list[Realize] = []
        realized: dict[str, int] = {}
        left = gain
        for view in winners:
            if left <= 0:
                break
            assert view.cost_basis is not None
            amount = _sale_for_gain(view, left)
            if amount <= 0:
                continue
            got = sale_gain(view.balance, view.cost_basis, amount)
            moves.append(Realize(view.ref, amount))
            realized[str(view.ref)] = got
            left -= got
        if not moves:
            return None
        total = gain - left
        stopped = "" if gain == room else f", stopped at a {_rate(limit)} marginal rate"
        return Decision(
            moves,
            note=(f"realized {_dollars(total)} of long-term gain into the {_rate(p.band)} band "
                  f"(room {_dollars(room)}{stopped})"),
            detail={"band_top": top, "taxable_before": before, "room": room,
                    "limit": limit, "realized": realized},
        )


@register
class Gift(Strategy):
    """A yearly gift each December from a surplus: ``amount`` (raised with
    CPI from the strategy's first month) into the expense account ``to``,
    only from what the portfolio holds above ``minimum`` (raised alike), out
    of its taxable lots. ``in_kind`` gives shares, most-appreciated first:
    the recipient takes over their basis and the household owes no tax on
    the gain. Otherwise it gives cash, selling the least-appreciated first,
    and gives less when the sale would add more than ``max_tax`` to the
    year's federal tax. Each recipient gets a strategy of its own."""

    kind = "gift"

    @dataclass(frozen=True)
    class Params:
        to: Account
        """The expense account the gift goes to (subkind gift)."""
        amount: Dollars
        """The yearly gift, in dollars of the strategy's first month, raised with CPI."""
        portfolio: Tag
        """Account tag whose taxable asset lots the gift comes from."""
        minimum: Dollars = 0
        """Give only what the portfolio holds above this, raised with CPI."""
        in_kind: bool = True
        """Give shares (no tax on their gain) rather than sell for cash."""
        max_tax: Dollars | None = None
        """The most a cash gift's sale may add to the year's federal tax; blank: no limit."""

    @classmethod
    def check(cls, params: Gift.Params, scenario: ScenarioView) -> list[str]:
        problems = []
        kind, subkind = scenario.accounts.get(params.to, ("", ""))
        if (kind, subkind) != ("expense", "gift"):
            problems.append(
                f"to '{params.to}' is {kind or '?'}/{subkind or '?'}; "
                "a gift goes to an expense account of subkind gift")
        if params.max_tax is not None and not params.in_kind:
            problems.extend(_federal_rule(scenario, "max_tax"))
        return problems

    def year_start(self, ctx: Context) -> Decision | None:
        # The amounts are in dollars of the strategy's first month, not its
        # first December.
        ctx.memory.setdefault("since", ctx.month.isoformat())
        return None

    def year_end(self, ctx: Context) -> Decision | None:
        p = self.params
        since = ctx.memory.setdefault("since", ctx.month.isoformat())
        factor = _cpi_since(ctx, since)
        amount, minimum = round(p.amount * factor), round(p.minimum * factor)
        lots = [ctx.lots[ref] for ref in ctx.tagged(p.portfolio)]
        held = sum(v.balance for v in lots)
        give = min(amount, held - minimum)
        if give <= 0:
            return Decision(
                [], note=f"no gift: portfolio {_dollars(held)} not above {_dollars(minimum)}",
                detail={"portfolio": held, "minimum": minimum},
            )
        sources = [v for v in lots if v.tax_treatment == "taxable" and v.available > 0]
        if p.in_kind:
            # Shares with the most gain first: that gain is never taxed.
            order = sorted(sources, key=lambda v: (not v.taxable_sale, -_share(v), str(v.ref)))
        else:
            order = sorted(sources, key=lambda v: (v.taxable_sale, _share(v), str(v.ref)))
        plan = _plan(order, give)
        capped = ""
        if not p.in_kind and p.max_tax is not None:
            base = ctx.tax.estimate().tax

            def cost(cents: int) -> int:
                short, long = _gains(_plan(order, cents), ctx.month)
                return ctx.tax.estimate(ordinary=short, ltcg=long).tax - base

            if cost(give) > p.max_tax:
                give = _largest_within(lambda cents: cost(cents) <= p.max_tax, give)
                plan = _plan(order, give)
                capped = f", held to {_dollars(p.max_tax)} of tax"
        if not plan:
            return Decision([], note=f"no gift: nothing to give from {p.portfolio}")
        given = sum(cents for _view, cents in plan)
        how = "in kind" if p.in_kind else "in cash"
        return Decision(
            [Transfer(view.ref, p.to, cents, in_kind=p.in_kind and view.taxable_sale)
             for view, cents in plan],
            note=f"gave {_dollars(given)} {how} to {p.to}{capped}",
            detail={"amount": amount, "minimum": minimum, "portfolio": held,
                    "from": {str(view.ref): cents for view, cents in plan}},
        )


def _plan(order: Sequence[LotView], give: int) -> list[tuple[LotView, int]]:
    """``give`` cents taken from ``order``'s lots in turn, each up to what it can spare."""
    out: list[tuple[LotView, int]] = []
    left = give
    for view in order:
        if left <= 0:
            break
        take = min(left, view.available)
        if take > 0:
            out.append((view, take))
            left -= take
    return out


def _gains(plan: Sequence[tuple[LotView, int]], month: date) -> tuple[int, int]:
    """The short- and long-term gains selling ``plan`` realizes. A lot sold
    at a loss counts as none: ``ctx.tax.estimate`` doesn't net losses or
    apply the $3,000 limit, so counting one could understate the tax and
    let a cash gift exceed ``max_tax``."""
    short = long = 0
    for view, cents in plan:
        if view.taxable_sale and view.cost_basis is not None:
            gain = max(0, sale_gain(view.balance, view.cost_basis, cents))
            if is_long_term(view.acquired, month):
                long += gain
            else:
                short += gain
    return short, long
