"""Finding strategy kinds: the built-ins and the data repo's ``strategies/``
modules (#326, ADR-010 §4), imported as :mod:`analysis.sim.plugin_code`
imports any data-repo code: from the source text the loader snapshots into
the intent (``strategy_source``), a fresh copy for each run.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from analysis.sim import plugin_code, strategy_api
from analysis.sim.strategy_api import Strategy, StrategyError

__all__ = [
    "STRATEGIES_DIR", "StrategyKinds", "builtin_kinds", "package_name", "read_sources",
    "strategy_kinds",
]

STRATEGIES_DIR = "strategies"
"""The data repo folder of strategy modules (ADR-003 §4.0)."""

FOLDER = plugin_code.Folder(
    name=STRATEGIES_DIR, builtin_package="analysis.sim.strategies", api="strategy API",
    api_version=lambda: strategy_api.API_VERSION, is_kind=strategy_api.is_registered,
)


def read_sources(data_dir: Path) -> dict[str, str]:
    """``strategies/*.py`` in the data repo: module name to source, sorted.
    No folder means no data-repo strategies."""
    return plugin_code.read_sources(data_dir, STRATEGIES_DIR)


def package_name(sources: Mapping[str, str]) -> str:
    """The synthetic package for ``sources``: one name per exact set of texts."""
    return plugin_code.package_name(sources, STRATEGIES_DIR)


def builtin_kinds() -> dict[str, type[Strategy]]:
    """The strategies that ship with the simulator."""
    return plugin_code.builtin_kinds(FOLDER)


class StrategyKinds(plugin_code.PluginKinds[Strategy]):
    """Every strategy kind a scenario can name, and where each came from."""

    folder = FOLDER

    def get(self, kind: str) -> type[Strategy]:
        try:
            return self.kinds[kind]
        except KeyError:
            raise StrategyError(f"unknown strategy kind {kind!r}") from None


def strategy_kinds(sources: Mapping[str, str]) -> StrategyKinds:
    """The built-in kinds plus those ``sources`` define; import problems are
    in ``.problems`` rather than raised, so a loader can report them all."""
    return StrategyKinds(sources)
