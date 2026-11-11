"""Alice and Bill's own spending strategy: whole trips."""

from dataclasses import dataclass

from analysis.sim.strategies import Guardrail
from analysis.sim.strategy_api import Context, Decision, RuleView, Spend, register


@register
class WholeTrips(Guardrail):
    """Guardrail decides the year's travel budget; the trips fit into it
    whole, most wanted first, and the rest wait a year."""

    kind = "whole_trips"

    @dataclass(frozen=True)
    class Params(Guardrail.Params):
        order: tuple[str, ...] = ()
        """The trips, most wanted first."""

    def year_start(self, ctx: Context) -> Decision | None:
        decision = super().year_start(ctx)       # guardrail's January decision
        allowance = ctx.memory.get("factor", 1.0) * ctx.budget()
        costs = ctx.rule_budgets()               # each trip, this year
        kept, skipped = [], []
        for trip in self.in_order(costs):
            if sum(costs[t] for t in kept) + costs[trip] <= allowance:
                kept.append(trip)
            else:
                skipped.append(trip)
        ctx.memory["kept"] = kept
        note = decision.note if decision else ""
        if skipped:
            note += f"; skip {', '.join(skipped)}"
        return Decision([], note=note)

    def scale(self, ctx: Context, rule: RuleView, amount: int) -> Decision | None:
        if rule.name in ctx.memory.get("kept", [rule.name]):
            return None                          # pay it as planned
        return Decision([Spend(0)], note=f"skip {rule.name} this year")

    def in_order(self, trips):
        """The trips, most wanted first; any that order leaves out come last."""
        order = list(self.params.order)
        return sorted(trips, key=lambda t: order.index(t) if t in order else len(order))
