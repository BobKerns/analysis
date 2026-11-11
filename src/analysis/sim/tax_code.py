"""Finding tax calculator kinds (#510, ADR-011): the built-ins in
:mod:`analysis.sim.taxes` and the data repo's ``taxes/*.py`` modules,
imported as :mod:`analysis.sim.plugin_code` imports any data-repo code: from
the source text the loader snapshots into the intent (``tax_source``), a
fresh copy for each run.
"""

from __future__ import annotations

import functools
import hashlib
import sqlite3
from collections.abc import Collection, Mapping
from pathlib import Path

from analysis.sim import plugin_code, registries, tax_api
from analysis.sim.tax_api import TaxCalculator, TaxError

__all__ = ["TaxKinds", "builtin_kinds", "read_sources", "snapshot", "write_sources"]

FOLDER = plugin_code.Folder(
    name=registries.TAXES_DIR, builtin_package="analysis.sim.taxes", api="tax calculator API",
    api_version=lambda: tax_api.API_VERSION, is_kind=tax_api.is_registered,
)


def read_sources(data_dir: Path) -> dict[str, str]:
    """``taxes/*.py`` in the data repo (the root folder, not a scenario's
    ``taxes/*.csv``): module name to source, sorted."""
    return plugin_code.read_sources(data_dir, registries.TAXES_DIR)


@functools.cache
def builtin_kinds() -> dict[str, type[TaxCalculator]]:
    """The calculators that ship with the simulator."""
    return plugin_code.builtin_kinds(FOLDER)


class TaxKinds(plugin_code.PluginKinds[TaxCalculator]):
    """Every calculator kind a scenario can name, and where each came from."""

    folder = FOLDER

    def get(self, kind: str) -> type[TaxCalculator]:
        try:
            return self.kinds[kind]
        except KeyError:
            raise TaxError(f"unknown tax calculator kind {kind!r}") from None


def snapshot(kinds: TaxKinds, used: Collection[str]) -> dict[str, str]:
    """The sources a scenario's intent keeps: the whole folder when any of
    the kinds it ``used`` comes from the data repo (a module may import
    another), else none."""
    return dict(kinds.sources) if kinds.from_data_repo(list(used)) else {}


def write_sources(con: sqlite3.Connection, sources: Mapping[str, str]) -> int:
    """Insert ``tax_source`` rows; how many."""
    for module, text in sorted(sources.items()):
        con.execute(
            "INSERT INTO tax_source(module, sha256, source) VALUES (?,?,?)",
            (module, hashlib.sha256(text.encode()).hexdigest(), text),
        )
    return len(sources)


def read_snapshot(con: sqlite3.Connection) -> dict[str, str]:
    """The ``tax_source`` snapshot of an intent or run file; {} for one that
    has none."""
    return dict(con.execute("SELECT module, source FROM tax_source").fetchall())
