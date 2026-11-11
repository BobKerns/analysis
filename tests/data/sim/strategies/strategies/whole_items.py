"""Persona strategies that build on a built-in (#411): the built-in
``guardrail`` decides each January how much of the optional spending the
year can afford, and these take the owned rules whole, in a stated order,
until that amount is used. One subclasses ``Guardrail``; the other
delegates to it, as the two ways a household's kind can build on a
built-in."""

from dataclasses import dataclass

from analysis.sim.strategies import Guardrail
from analysis.sim.strategy_api import Context, Decision, RuleView, Spend, Strategy, register

API_VERSION = 11


def _fit(ctx: Context, factor: float, order: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """The owned rules that fit into ``factor`` of the year's budget, taken
    whole in ``order`` (rules it leaves out come last), and those that don't."""
    allowance = factor * ctx.budget()
    rank = {name: i for i, name in enumerate(order)}
    kept: list[str] = []
    skipped: list[str] = []
    spent = 0
    for name, cost in sorted(ctx.rule_budgets().items(), key=lambda item: (rank.get(item[0], len(order)), item[0])):
        if spent + cost <= allowance:
            kept.append(name)
            spent += cost
        else:
            skipped.append(name)
    return kept, skipped


@register
class WholeItems(Guardrail):
    """Guardrail decides the year's amount; the owned rules fit into it
    whole, first in ``order`` first, and the rest are skipped for the year."""

    kind = "whole_items"

    @dataclass(frozen=True)
    class Params(Guardrail.Params):  # ty: ignore[invalid-attribute-override]  # extending Params is the pattern (ADR-010 §3)
        order: tuple[str, ...] = ()
        """The owned rules, most wanted first."""

    def year_start(self, ctx: Context) -> Decision | None:
        decision = super().year_start(ctx)
        kept, skipped = _fit(ctx, ctx.memory.get("factor", 1.0), self.params.order)
        ctx.memory["kept"] = kept
        note = decision.note if decision else ""
        if skipped:
            note += f"; skip {', '.join(skipped)}"
        return Decision([], note=note, detail={"kept": kept, "skipped": skipped})

    def scale(self, ctx: Context, rule: RuleView, amount: int) -> Decision | None:
        if rule.name in ctx.memory.get("kept", [rule.name]):
            return None
        return Decision([Spend(0)], note=f"skip {rule.name} this year")


@register
class WholeItemsByDelegation(Strategy):
    """``whole_items``, written by handing the January decision to a
    ``guardrail`` it holds rather than by subclassing it."""

    kind = "whole_items_delegated"

    Params = WholeItems.Params

    def __init__(self, name: str, params: WholeItems.Params) -> None:
        super().__init__(name, params)
        self.guardrail = self.delegate(Guardrail)

    def year_start(self, ctx: Context) -> Decision | None:
        decision = self.guardrail.year_start(ctx.with_memory("guardrail"))
        kept, skipped = _fit(ctx, ctx.memory["guardrail"].get("factor", 1.0), self.params.order)
        ctx.memory["kept"] = kept
        note = decision.note if decision else ""
        if skipped:
            note += f"; skip {', '.join(skipped)}"
        return Decision([], note=note, detail={"kept": kept, "skipped": skipped})

    def scale(self, ctx: Context, rule: RuleView, amount: int) -> Decision | None:
        if rule.name in ctx.memory.get("kept", [rule.name]):
            return None
        return Decision([Spend(0)], note=f"skip {rule.name} this year")
