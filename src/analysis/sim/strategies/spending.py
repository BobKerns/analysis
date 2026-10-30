"""Spending strategies (#387): ``threshold``, ``guardrail``, ``ceiling_floor``.

Each owns the scheduled rules carrying its ``rules`` tag (the household's
plan for that spending) and moves their level by one yearly factor, set in
``year_start`` and applied to every posting in ``scale``. The plan's mix
across the rules is kept; only its level moves, so the spending report can
set what was spent against what was budgeted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from analysis.sim.strategy_api import (
    Context, Decision, Dollars, Percent, RuleView, Spend, Strategy, StrategyError, Tag, percent,
    register,
)


def _dollars(cents: float) -> str:
    return f"${cents / 100:,.0f}"


def _cpi_since(ctx: Context, since: str) -> float:
    """Inflation from ``since`` (a month end) to now, as a factor; 1 when the
    scenario has no CPI series."""
    try:
        return ctx.cpi(date.fromisoformat(since)) / 1_000_000
    except StrategyError:
        return 1.0


class _Spending(Strategy):
    """Scale every owned posting by ``ctx.memory['factor']``."""

    def scale(self, ctx: Context, rule: RuleView, amount: int) -> Decision | None:
        factor = ctx.memory.get("factor", 1.0)
        if factor == 1.0:
            return None
        return Decision([Spend(max(0, round(amount * factor)))], note=f"{factor:.0%} of plan")

    def _portfolio(self, ctx: Context, tag: str) -> int:
        return sum(ctx.lots[ref].balance for ref in ctx.tagged(tag))

    def _last_return(self, ctx: Context, tag: str) -> float | None:
        return ctx.annual_return(ctx.tagged(tag), ctx.year - 1)


@register
class Threshold(_Spending):
    """Floor-and-upside spending: the owned rules (tag only the optional
    ones: a trip, a gift) post in full in a year that starts with the
    portfolio at or above ``minimum``, and not at all in one that doesn't.
    With ``skip_after_loss``, a year after a negative return on the
    portfolio is skipped too. Decided each January."""

    kind = "threshold"

    @dataclass(frozen=True)
    class Params:
        portfolio: Tag
        """Account tag whose asset lots are the portfolio."""
        minimum: Dollars = 0
        """The portfolio needed, in dollars of the strategy's first month, raised with CPI."""
        skip_after_loss: bool = False
        """Also skip a year that follows a negative return on the portfolio."""

    def year_start(self, ctx: Context) -> Decision | None:
        p = self.params
        memory = ctx.memory
        memory.setdefault("since", ctx.month.isoformat())
        balance = self._portfolio(ctx, p.portfolio)
        minimum = round(p.minimum * _cpi_since(ctx, memory["since"])) if p.minimum else 0
        reasons = []
        if balance < minimum:
            reasons.append(f"portfolio {_dollars(balance)} below {_dollars(minimum)}")
        last = self._last_return(ctx, p.portfolio) if p.skip_after_loss else None
        if last is not None and last < 0:
            reasons.append(f"last year's return {last:.1%}")
        factor = 0.0 if reasons else 1.0
        changed = factor != memory.get("factor", 1.0)
        memory["factor"] = factor
        if not changed:
            return None
        note = ("skip this year: " + ", ".join(reasons)) if reasons else (
            f"spend again: portfolio {_dollars(balance)}"
            + (f" at or above {_dollars(minimum)}" if minimum else ""))
        return Decision([], note=note, detail={
            "portfolio": balance, "minimum": minimum, "last_return": last, "factor": factor,
        })


@register
class Guardrail(_Spending):
    """Guyton-Klinger decision rules. The first year sets the initial
    withdrawal rate: the owned rules' budget over the portfolio. Each
    January after, the rate is this year's spending over the portfolio.
    Above the initial rate by ``upper``, spending is cut by ``adjust``
    (capital preservation, not in the last ``final_years`` of the
    scenario); below it by ``lower``, it is raised by ``adjust``
    (prosperity). After a year with a negative return, while the rate is
    above the initial one, the year's inflation raise is skipped: the raise
    the owned rules' budget got since last January, at most inflation, so a
    rule with a fixed amount loses nothing."""

    kind = "guardrail"

    @dataclass(frozen=True)
    class Params:
        portfolio: Tag
        """Account tag whose asset lots are the portfolio spending is drawn from."""
        upper: Percent = percent("20%")
        """How far the rate may rise above the initial rate before a cut."""
        lower: Percent = percent("20%")
        """How far it may fall below the initial rate before a raise."""
        adjust: Percent = percent("10%")
        """The size of a cut or raise."""
        skip_inflation_after_loss: bool = True
        """Skip the inflation raise after a down year while the rate is above the initial."""
        final_years: int = 15
        """No cuts in this many last years of the scenario."""

    def year_start(self, ctx: Context) -> Decision | None:
        p = self.params
        memory = ctx.memory
        balance = self._portfolio(ctx, p.portfolio)
        budget = ctx.budget()
        if "initial_rate" not in memory:
            if balance <= 0 or budget <= 0:
                return None  # nothing to measure against yet
            memory.update(initial_rate=budget / balance, factor=1.0, since=ctx.month.isoformat(),
                          budget=budget)
            return Decision([], note=(
                f"initial withdrawal rate {budget / balance:.2%}: "
                f"budget {_dollars(budget)} of {_dollars(balance)}"
            ), detail={"portfolio": balance, "budget": budget})
        initial = memory["initial_rate"]
        factor = start = memory["factor"]
        # The raise the budget got since last January: inflation for indexed
        # rules, none for fixed ones (so skipping it never cuts a flat budget),
        # and never more than inflation (a rule the plan adds isn't a raise).
        raised = min(_cpi_since(ctx, memory["since"]), budget / memory["budget"]) if memory["budget"] else 1.0
        memory.update(since=ctx.month.isoformat(), budget=budget)

        def rate() -> float:
            return factor * budget / balance if balance > 0 else float("inf")

        notes = []
        last = self._last_return(ctx, p.portfolio)
        if (p.skip_inflation_after_loss and last is not None and last < 0 and rate() > initial
                and raised > 1):
            factor /= raised
            notes.append(f"no {raised - 1:.1%} inflation raise after a {last:.1%} year")
        years_left = ctx.end.year - ctx.year + 1
        checked = rate()  # the rate the guardrails test, after any skipped raise
        if checked > initial * (1 + p.upper / 1_000_000) and years_left > p.final_years:
            notes.append(f"cut {p.adjust / 10_000:g}%: rate {checked:.2%} above {initial:.2%} "
                         f"+ {p.upper / 10_000:g}%")
            factor *= 1 - p.adjust / 1_000_000
        elif checked < initial * (1 - p.lower / 1_000_000):
            notes.append(f"raise {p.adjust / 10_000:g}%: rate {checked:.2%} below {initial:.2%} "
                         f"- {p.lower / 10_000:g}%")
            factor *= 1 + p.adjust / 1_000_000
        memory["factor"] = factor
        if factor == start:
            return None
        return Decision([], note=f"{'; '.join(notes)} (now {factor:.0%} of plan)", detail={
            "portfolio": balance, "budget": budget, "rate": checked, "initial_rate": initial,
            "last_return": last, "factor": factor,
        })


@register
class CeilingFloor(_Spending):
    """Dynamic spending: each year aims to spend ``rate`` of the portfolio
    on the owned rules, but its change from last year's spending, after
    inflation, is held between ``-floor`` and ``+ceiling``. A ``rate`` of 0%
    (the default) takes the first year's budget over the portfolio, so it
    starts on plan. An explicit ``rate`` is the share for the owned rules
    alone, and sets their first year's spending outright: owning only the
    optional rules, leave it at 0%."""

    kind = "ceiling_floor"

    @dataclass(frozen=True)
    class Params:
        portfolio: Tag
        """Account tag whose asset lots are the portfolio spending is drawn from."""
        rate: Percent = 0
        """The share of the portfolio to spend on the owned rules each year; 0% starts from the plan."""
        ceiling: Percent = percent("5%")
        """The most real spending may rise in a year."""
        floor: Percent = percent("2.5%")
        """The most real spending may fall in a year."""

    def year_start(self, ctx: Context) -> Decision | None:
        p = self.params
        memory = ctx.memory
        balance = self._portfolio(ctx, p.portfolio)
        budget = ctx.budget()
        if budget <= 0:
            return None
        if "rate" not in memory:
            if balance <= 0:
                return None
            rate = p.rate / 1_000_000 or budget / balance
            spend = round(rate * balance) if p.rate else budget
            memory.update(rate=rate)
            note = f"spend {_dollars(spend)}: {rate:.2%} of {_dollars(balance)}"
        else:
            rate = memory["rate"]
            last = memory["spend"] * _cpi_since(ctx, memory["since"])
            target = rate * balance
            high = last * (1 + p.ceiling / 1_000_000)
            low = last * (1 - p.floor / 1_000_000)
            spend = round(min(max(target, low), high))
            held = " (held to the ceiling)" if target > high else " (held to the floor)" if target < low else ""
            note = f"spend {_dollars(spend)}: target {_dollars(target)}, {rate:.2%} of {_dollars(balance)}{held}"
        factor = spend / budget
        memory.update(spend=spend, since=ctx.month.isoformat(), factor=factor)
        return Decision([], note=f"{note} ({factor:.0%} of plan)", detail={
            "portfolio": balance, "budget": budget, "spend": spend, "factor": factor,
        })
