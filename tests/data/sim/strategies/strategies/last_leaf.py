"""A persona strategy that stands in for a household's own (#326 tests):
draw everything from the chain's last leaf."""

from analysis.sim.strategy_api import ChainView, Context, Decision, Strategy, Take, register

from ._helpers import last

API_VERSION = 1


@register
class LastLeaf(Strategy):
    """Draw the whole need from the chain's last leaf."""

    kind = "last_leaf"

    def draw(self, ctx: Context, chain: ChainView, need: int) -> Decision | None:
        ref = last(chain.leaves)
        ctx.memory["draws"] = ctx.memory.get("draws", 0) + 1
        return Decision([Take(ref, need)], note=f"all from {ref}")
