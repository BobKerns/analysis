"""The names a scenario defines, read straight from its CSVs for tab
completion (#240): scenarios, accounts, lots, chains, scheduled rules,
calendar rows, persons, series, tags, symbols and itemizations.

Completion runs on every TAB press, so these use only the ``csv`` module
and walk ``lineage.load_order`` the way the loader overlays rows, with no
cache. They are tolerant on purpose: a missing or malformed file gives what
it can, or nothing, since reporting a bad file is the loader's job, not a
completion's. Nothing here validates a name.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Iterator
from pathlib import Path

from analysis.sim import registries
from analysis.sim.lineage import load_order, scenario_dir, template_names

__all__ = [
    "account_names",
    "calendar_row_names",
    "chain_names",
    "itemization_names",
    "lot_names",
    "person_ids",
    "scenario_names",
    "scheduled_names",
    "series_names",
    "symbols",
    "tag_tokens",
    "ytd_names",
]


def _rows(path: Path) -> Iterator[dict[str, str]]:
    """``path``'s data rows, stripped; none if it's missing or unreadable."""
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                yield {k: (v or "").strip() for k, v in row.items() if k is not None}
    except (OSError, UnicodeDecodeError, csv.Error):
        return


def _lineage_dirs(data_dir: Path, scenario: str) -> list[Path]:
    """The scenario's folder and every one it inherits from, root first."""
    scenarios = data_dir / "scenarios"
    return [scenario_dir(scenarios, name) for name in load_order(scenarios, scenario)]


def _column(paths: Iterable[Path], column: str) -> list[str]:
    """Every non-blank ``column`` value across ``paths``, sorted, once each."""
    return sorted({row[column] for path in paths for row in _rows(path) if row.get(column)})


def _stems(folders: Iterable[Path], pattern: str = "*.csv") -> set[str]:
    return {
        path.stem for folder in folders if folder.is_dir()
        for path in folder.glob(pattern) if not path.stem.startswith("_")
    }


def scenario_names(data_dir: Path) -> list[str]:
    """Scenario folder names, as ``loader.list_scenario_names`` lists them."""
    scenarios = data_dir / "scenarios"
    if not scenarios.is_dir():
        return []
    return sorted([*(p.name for p in scenarios.iterdir() if p.is_dir()), *template_names(scenarios)])


def account_names(data_dir: Path, scenario: str) -> list[str]:
    """Every account the scenario has, its own and inherited."""
    return _column((d / "accounts.csv" for d in _lineage_dirs(data_dir, scenario)), "name")


def lot_names(data_dir: Path, scenario: str, account: str, *, implicit: bool = True) -> list[str]:
    """``account``'s lots: the ones account_lots.csv names, a blank lot
    being the default one, or (when ``implicit``) just the default lot of an
    account with no rows there. None for a name that isn't an account (a
    chain, say)."""
    dirs = _lineage_dirs(data_dir, scenario)
    lots = {
        row.get("lot") or registries.DEFAULT_LOT
        for d in dirs for row in _rows(d / "account_lots.csv")
        if row.get("account_name") == account
    }
    if implicit and not lots and account in account_names(data_dir, scenario):
        lots = {registries.DEFAULT_LOT}
    return sorted(lots)


def chain_names(data_dir: Path, scenario: str, *, accounts: bool = False) -> list[str]:
    """The scenario's chain files (``chains/<role>/<name>.csv``), and with
    ``accounts`` its accounts too, since each is a leaf chain of the same
    name (the endpoints of a transfer)."""
    dirs = _lineage_dirs(data_dir, scenario)
    names = _stems(d / "chains" / role for d in dirs for role in registries.CHAIN_ROLES)
    if accounts:
        names.update(account_names(data_dir, scenario))
    return sorted(names)


def scheduled_names(data_dir: Path, scenario: str) -> list[str]:
    """The scenario's scheduled rules, its own and inherited."""
    return _column((d / "scheduled.csv" for d in _lineage_dirs(data_dir, scenario)), "name")


def ytd_names(data_dir: Path, scenario: str) -> list[str]:
    """The names of the scenario's year_to_date.csv rows, its own and inherited."""
    return _column((d / "year_to_date.csv" for d in _lineage_dirs(data_dir, scenario)), "name")


def calendar_row_names(data_dir: Path, scenario: str) -> list[str]:
    """The names of the scenario's named calendar rows (latent factors among
    them), its own and inherited."""
    return _column((d / "calendar.csv" for d in _lineage_dirs(data_dir, scenario)), "name")


def person_ids(data_dir: Path) -> list[str]:
    """The ids in the shared persons.csv."""
    return _column([data_dir / "persons.csv"], "id")


def series_names(data_dir: Path, scenario: str, kind: str | None = None) -> list[str]:
    """The series of ``kind`` (``rate``, ``inflation``, ...), or of any kind,
    that the scenario's ``series/`` and ``aux/`` folders and the shared
    ``aux/`` have a file for: ``<kind>__<name>.csv``."""
    folders = [data_dir / "aux"]
    for d in _lineage_dirs(data_dir, scenario):
        folders += [d / "series", d / "aux"]
    names = set()
    for stem in _stems(folders, f"{kind}__*.csv" if kind else "*__*.csv"):
        names.add(stem.split("__", 1)[1])
    return sorted(names)


def tag_tokens(data_dir: Path, scenario: str) -> list[str]:
    """Every ``role:name`` tag on the scenario's accounts, rules and
    calendar rows. A malformed token is skipped."""
    tokens: set[str] = set()
    for d in _lineage_dirs(data_dir, scenario):
        for filename in ("accounts.csv", "scheduled.csv", "calendar.csv"):
            for row in _rows(d / filename):
                tokens.update(
                    t for t in row.get("tags", "").split() if registries.TAG_TOKEN_RE.match(t)
                )
    return sorted(tokens)


def symbols(data_dir: Path) -> list[str]:
    """Every symbol named anywhere, from the sources ``symbols.symbol_table``
    lists: securities.csv, allocations.csv, the shared and every scenario's
    ``prices/`` files, and every scenario's lots."""
    scenarios = data_dir / "scenarios"
    scenario_dirs = sorted(p for p in scenarios.iterdir() if p.is_dir()) if scenarios.is_dir() else []
    found = set(_column(
        [data_dir / "securities.csv", data_dir / "allocations.csv",
         *(d / "account_lots.csv" for d in scenario_dirs)],
        "symbol",
    ))
    prefix = "security_price__"
    price_dirs = [data_dir / "prices", *(d / "prices" for d in scenario_dirs)]
    found.update(s[len(prefix):] for s in _stems(price_dirs, f"{prefix}*.csv"))
    return sorted(found)


def itemization_names(data_dir: Path, scenario: str) -> list[str]:
    """The itemizations the scenario can name: ``itemizations/<name>.csv``
    in its lineage or the shared folder."""
    folders = [data_dir / "itemizations", *(d / "itemizations" for d in _lineage_dirs(data_dir, scenario))]
    return sorted(_stems(folders))
