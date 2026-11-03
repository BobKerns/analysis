"""A data-repo strategy answering a guard rail (#441): after a year past
its trigger's line, it cuts the optional rules it owns until a year comes
back inside."""

from dataclasses import dataclass

from analysis.sim.strategy_api import Decision, Percent, Spend, Strategy, percent, register


@register
class CutOnBreach(Strategy):
    """Cut the owned rules by ``cut`` in the years after one past the line."""

    kind = "cut_on_breach"

    @dataclass(frozen=True)
    class Params:
        cut: Percent = percent("50%")
        """How much to cut while past the line."""

    def on_breach(self, ctx, breach):
        cutting = breach.state != "left"
        ctx.memory["cutting"] = cutting
        return Decision([], note=f"{breach.rail} {breach.margin_kind} {breach.state}",
                        detail={"margin": breach.margin, "years_past": breach.years_past})

    def scale(self, ctx, rule, amount):
        if not ctx.memory.get("cutting"):
            return None
        return Decision([Spend(amount * (1_000_000 - self.params.cut) // 1_000_000)])
