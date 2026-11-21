"""The strategies that ship with the simulator (#326, ADR-010 §6).

Each module defines kinds with :func:`zabob.retirement.sim.strategy_api.register`.
A strategy proven in a data repo's ``strategies/`` folder is promoted here
with its household specifics turned into parameters and persona-only tests.

The kinds' classes are part of the strategy API (#411, ADR-010 §3): a
household's kind may subclass one (``class WholeTrips(Guardrail)``, giving
its own ``kind``) or delegate to one (:meth:`Strategy.delegate`), importing
it from here. Their hooks, ``Params`` and memory keys change only with an
``API_VERSION`` bump; the modules' other names (``_Spending`` and the
helpers) can change without notice.
"""

from zabob.retirement.sim.strategies.markets import Buckets, DownMarket
from zabob.retirement.sim.strategies.rebalance import Rebalance
from zabob.retirement.sim.strategies.spending import Amortize, CeilingFloor, CutOnBreach, Guardrail, Threshold
from zabob.retirement.sim.strategies.year_end import Gift, Harvest

__all__ = [
    "Amortize", "Buckets", "CeilingFloor", "CutOnBreach", "DownMarket", "Gift", "Guardrail", "Harvest",
    "Rebalance", "Threshold",
]
