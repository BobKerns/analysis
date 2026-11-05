"""Spending strategies (#387): ``threshold``, ``guardrail``, ``ceiling_floor``,
amortization, ``amortize`` (#459), and an answer to a guard rail,
``cut_on_breach`` (#377).

Each owns the scheduled rules carrying its ``rules`` tag (the household's
plan for that spending) and moves their level by one yearly factor, set in
``year_start`` (``on_breach`` for ``cut_on_breach``) and applied to every
posting in ``scale``. The plan's mix across the rules is kept; only its level moves, so the spending report can
set what was spent against what was budgeted.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from collections.abc import Sequence
from datetime import date
from typing import Any

from analysis.sim.strategy_api import (
    BreachView, Context, Decision, Dollars, Percent, RuleView, ScenarioView, Spend, Strategy, StrategyError, Tag,
    percent, register,
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
class CutOnBreach(_Spending):
    """Answer a guard rail (#377): while a rail the ``trigger`` names is
    past its line, spend ``cut`` less on the owned rules; while a positive
    rail is past (room to spend), spend ``increase`` more. Each January's
    breach (#441) says how last year ended, so the level holds for the year
    after one past the line and returns to plan after a year back inside. A
    rail to stay inside wins over a positive one when both are past. The
    level is flat while past, not compounded year after year as the
    ``guardrail`` kind's cuts are."""

    kind = "cut_on_breach"

    @dataclass(frozen=True)
    class Params:
        cut: Percent = percent("10%")
        """How much less to spend while a rail to stay inside is past."""
        increase: Percent = percent("0%")
        """How much more to spend while a positive rail is past; 0% ignores positive rails."""

    def on_breach(self, ctx: Context, breach: BreachView) -> Decision | None:
        p = self.params
        past: dict[str, str] = ctx.memory.setdefault("past", {})
        key = f"{breach.rail}:{breach.margin_kind}"
        if breach.state == "left":
            past.pop(key, None)
        else:
            past[key] = breach.sense
        start = ctx.memory.get("factor", 1.0)
        if "negative" in past.values():
            factor = 1 - p.cut / 1_000_000
        elif "positive" in past.values():
            factor = 1 + p.increase / 1_000_000
        else:
            factor = 1.0
        ctx.memory["factor"] = factor
        answer = "" if factor == start else (
            "back to plan" if factor == 1.0 else f"now {factor:.0%} of plan")
        return Decision([], note=f"{breach.rail} {breach.margin_kind} {breach.state}"
                        + (f": {answer}" if answer else ""),
                        detail={"margin": breach.margin, "years_past": breach.years_past,
                                "factor": factor})


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


def annuity_rate(rate: float, years: int) -> float:
    """The share of a portfolio that pays the same real amount at the start
    of each of ``years`` years and spends it all, at real return ``rate``:
    an annuity-due's payment per dollar. ``1/years`` at a zero return."""
    if years < 1:
        raise StrategyError(f"annuity_rate needs at least one year, not {years}")
    if rate <= -1:
        raise StrategyError(f"annuity_rate needs a real return above -100%, not {rate:.2%}")
    if abs(rate) < 1e-12:
        return 1 / years
    # In logs, so a return near zero keeps its precision and a return near
    # -100% over many years doesn't overflow: for a negative return the
    # growth factor is written with positive powers, which only shrink.
    growth = math.log1p(rate)
    if rate > 0:
        return rate / -math.expm1(-years * growth) / (1 + rate)
    return rate * math.exp((years - 1) * growth) / math.expm1(years * growth)


# The asset classes each class return parameter covers (VPW's two figures,
# and cash). real_estate and other have no published figure.
_RETURN_OF = {
    "stock": "stock_return", "stock_us": "stock_return", "stock_intl": "stock_return",
    "bond": "bond_return", "cd": "bond_return", "cash": "cash_return",
}


def parse_horizon(text: str) -> tuple[str, float] | str:
    """``age:N`` -> ("age", N), ``survival:P%`` -> ("survival", P/100),
    ``rmd`` -> ("rmd", 0); otherwise the problem, as a phrase."""
    kind, _, arg = text.strip().partition(":")
    if kind == "rmd" and not arg:
        return ("rmd", 0.0)
    if kind == "age":
        try:
            age = int(arg)
        except ValueError:
            return f"horizon {text!r}: age:N needs a whole age, such as age:100"
        return ("age", float(age)) if 0 < age <= 130 else f"horizon {text!r}: an age from 1 to 130"
    if kind == "survival":
        try:
            p = percent(arg) / 1_000_000
        except ValueError:
            return f"horizon {text!r}: survival:P needs a percent, such as survival:10%"
        return ("survival", p) if 0 < p < 1 else f"horizon {text!r}: a percent above 0% and below 100%"
    return f"horizon {text!r}: age:N, survival:P% or rmd"


@register
class Amortize(_Spending):
    """Amortization spending: VPW (Bogleheads variable percentage
    withdrawal) and the RMD method. Each January the portfolio is priced as
    an annuity: what it could pay each year, the same in real terms, if it
    were spent down over the ``horizon`` at a real return. That payment
    rises and falls with the portfolio. The first year starts on plan (the
    owned rules post their budget); after that the owned rules take the
    change in the payment since then, in today's dollars, dollar for dollar
    and never below zero, so the spending the strategy doesn't own is paid
    as planned and the optional spending is what moves. ``ceiling`` and
    ``floor`` limit the owned spending's real change from one year to the
    next. The horizon reads ages and life tables, never a lifespan an
    ensemble member drew."""

    kind = "amortize"

    @dataclass(frozen=True)
    class Params:
        portfolio: Tag
        """Account tag whose asset lots are amortized."""
        horizon: str = "age:100"
        """age:N (until the youngest living primary person is N; VPW uses 100), survival:P% (until
        the chance anyone in the household is alive falls to P), or rmd (the IRS divisor)."""
        real_return: Percent | None = None
        """One real return for the portfolio; blank blends the class returns by its allocation."""
        stock_return: Percent = percent("5%")
        """Real return for stock classes (VPW's figure)."""
        bond_return: Percent = percent("1.9%")
        """Real return for bonds and CDs (VPW's figure)."""
        cash_return: Percent = 0
        """Real return for cash."""
        ceiling: Percent | None = None
        """The most the owned spending may rise in a year, after inflation; blank for no limit."""
        floor: Percent | None = None
        """The most the owned spending may fall in a year, after inflation; blank for no limit."""

    @classmethod
    def check(cls, params: Any, scenario: ScenarioView) -> Sequence[str]:
        problems: list[str] = []
        horizon = parse_horizon(params.horizon)
        if isinstance(horizon, str):
            return [horizon]
        kind = horizon[0]
        if kind == "rmd":
            if params.real_return is not None:
                problems.append("the rmd horizon takes no real_return (the IRS divisor is the whole rule)")
            if "rmd_factors" not in scenario.aux:
                problems.append("the rmd horizon needs aux rmd_factors.csv")
        if kind in ("rmd", "survival"):
            household = {pid: person for pid, person in scenario.people.items()
                         if person[0] in ("primary", "secondary")}
            missing = sorted(pid for pid, (_role, sex, _table) in household.items() if not sex)
            if missing:
                problems.append(f"the {kind} horizon needs a sex in persons.csv for {', '.join(missing)}")
            tables = sorted({f"life_table__{table}" if table else "life_table"
                             for _role, _sex, table in household.values()} - scenario.aux)
            if tables:
                problems.append(f"the {kind} horizon needs aux {', '.join(t + '.csv' for t in tables)}")
        for name in ("real_return", "stock_return", "bond_return", "cash_return"):
            value = getattr(params, name)
            if value is not None and value <= -1_000_000:
                problems.append(f"{name} must be above -100%")
        if params.real_return is None and kind != "rmd":
            odd = sorted(scenario.portfolio_classes.get(params.portfolio, frozenset()) - set(_RETURN_OF))
            if odd:
                problems.append(f"portfolio {params.portfolio} holds {', '.join(odd)}, which has no "
                                f"class return: give real_return")
        return problems

    def _return(self, ctx: Context) -> float:
        p = self.params
        if p.real_return is not None:
            return p.real_return / 1_000_000
        allocation = ctx.allocation(ctx.tagged(p.portfolio))
        # Blend what is held: a lot overdrawn below zero (a shortfall the run
        # carries on past) isn't a holding with a return.
        held = {c: cents for c, cents in allocation.dollars.items() if cents > 0}
        total = sum(held.values())
        if total <= 0:
            return 0.0
        blended = 0.0
        for asset_class, cents in held.items():
            field_name = _RETURN_OF.get(asset_class)
            if field_name is None:
                raise StrategyError(
                    f"portfolio {p.portfolio} holds {asset_class}, which has no class return: "
                    f"give real_return")
            blended += cents / total * getattr(p, field_name) / 1_000_000
        return blended

    def _rate(self, ctx: Context) -> tuple[float, str, dict[str, Any]] | None:
        """This year's payment rate, a phrase saying how, and the detail."""
        horizon = parse_horizon(self.params.horizon)
        if isinstance(horizon, str):
            raise StrategyError(horizon)
        kind, arg = horizon
        living = [v for v in ctx.people.values()
                  if v.alive and v.role in ("primary", "secondary") and v.age is not None]
        if not living:
            return None
        if kind == "rmd":
            oldest = min(living, key=lambda v: (v.birthdate or date.max, v.id))
            divisor = ctx.rmd_divisor(oldest.id)
            source = "rmd_factors"
            if divisor is None:
                divisor, source = ctx.life_expectancy(oldest.id), "life expectancy"
            divisor = max(divisor, 1.0)
            return 1 / divisor, f"{source} {divisor:.1f} at {oldest.id}'s {oldest.age}", {"divisor": divisor}
        r = self._return(ctx)
        if kind == "age":
            youngest = min(v.age for v in living if v.age is not None)
            years = max(1, int(arg) - youngest)
        else:
            years = ctx.survival_years(arg)
        how = f"{self.params.horizon}, {years} years at {r:.2%}"
        return annuity_rate(r, years), how, {"years": years, "real_return": r}

    def year_start(self, ctx: Context) -> Decision | None:
        p = self.params
        memory = ctx.memory
        balance = self._portfolio(ctx, p.portfolio)
        budget = ctx.budget()
        found = self._rate(ctx)
        if found is None:
            return None  # nobody left to plan for
        rate, how, detail = found
        # An empty portfolio pays nothing; that is a payment of zero once
        # the strategy has started, so the owned spending falls with it.
        payment = round(rate * balance) if balance > 0 else 0
        first = "first_payment" not in memory
        if first:
            if budget <= 0 or balance <= 0:
                return None
            spend = budget
            memory.update(first_payment=payment, first_since=ctx.month.isoformat())
            held = ""
            note_tail = "on plan"
        else:
            then = memory["first_payment"] * _cpi_since(ctx, memory["first_since"])
            target = max(0.0, budget + payment - then)
            last = memory["spend"] * _cpi_since(ctx, memory["since"])
            high = last * (1 + p.ceiling / 1_000_000) if p.ceiling is not None else None
            low = last * (1 - p.floor / 1_000_000) if p.floor is not None else None
            spend_f = target
            held = ""
            if high is not None and spend_f > high:
                spend_f, held = high, " (held to the ceiling)"
            elif low is not None and spend_f < low:
                spend_f, held = low, " (held to the floor)"
            spend = round(spend_f)
            change = payment - then
            note_tail = (f"payment {'up' if change >= 0 else 'down'} {_dollars(abs(change))} "
                         f"since the first year, in today's dollars")
        factor = spend / budget if budget > 0 else memory.get("factor", 1.0)
        memory.update(spend=spend, since=ctx.month.isoformat(), factor=factor)
        note = (f"pay {_dollars(payment)}: {rate:.2%} of {_dollars(balance)} ({how}); "
                f"spend {_dollars(spend)}{held}, {note_tail} ({factor:.0%} of plan)")
        return Decision([], note=note, detail={
            **detail, "portfolio": balance, "rate": rate, "payment": payment,
            "first_payment": memory["first_payment"], "budget": budget, "spend": spend, "factor": factor,
        })
