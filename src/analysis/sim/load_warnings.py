"""A load's non-fatal warnings, kept with the intent (#432).

The loader's warnings (a fallback taken, a partial allocation) and its notes
on things defined but not read yet (``LoadResult.unused``, #204) used to
exist only on the terminal of the ``sim check|load|run`` that raised them.
The loader now writes both into the intent's ``load_warning`` table, and a
run file's snapshot carries it, so ``sim explain`` and the notebooks read
them back after the fact without reloading.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

WARNING = "warning"
UNUSED = "unused"


@dataclass(frozen=True)
class LoadWarnings:
    """What a load warned about: ``warnings`` every load prints, ``unused``
    only ``sim check`` does (often deliberate: work in progress, or meant
    for child scenarios)."""

    warnings: tuple[str, ...] = ()
    unused: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.warnings or self.unused)


def write(con: sqlite3.Connection, warnings: Iterable[str], unused: Iterable[str]) -> None:
    """Record a load's warnings, then its unused notes, in the order raised."""
    con.executemany(
        "INSERT INTO load_warning (kind, message) VALUES (?, ?)",
        [*((WARNING, w) for w in warnings), *((UNUSED, u) for u in unused)],
    )


def read(con: sqlite3.Connection) -> LoadWarnings:
    """The warnings recorded in an intent or run file."""
    rows = con.execute("SELECT kind, message FROM load_warning ORDER BY id").fetchall()
    return LoadWarnings(
        warnings=tuple(message for kind, message in rows if kind == WARNING),
        unused=tuple(message for kind, message in rows if kind == UNUSED),
    )


def read_file(path: Path | str) -> LoadWarnings:
    """``read`` of the intent or run file at ``path``, read-only."""
    con = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)
    try:
        return read(con)
    finally:
        con.close()


def count_phrase(found: LoadWarnings) -> str:
    """``1 load warning``, ``3 load warnings``."""
    n = len(found.warnings)
    return f"{n} load warning{'' if n == 1 else 's'}"
