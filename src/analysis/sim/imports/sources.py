"""The sources an import can read, and which one a file is (issue #527).

A source is one outside layout, or one program's family of them, under one
name: the name of its mapping folder, ``imports/<name>/``. It reads holdings,
flows, or both, each with a test of whether a file is in its layout so
``--source`` can be left off.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pandas as pd

from analysis.sim.imports import quicken
from analysis.sim.imports.forms import Flows, Snapshot

__all__ = [
    "FORMS", "Form", "Reader", "Source", "SourceError", "detect", "get", "names",
    "read_flows", "read_holdings", "sources",
]

Form = Literal["holdings", "transactions"]
FORMS: tuple[Form, ...] = ("holdings", "transactions")


class SourceError(ValueError):
    """No source reads a file, or the named one can't."""


@dataclass(frozen=True)
class Reader[T]:
    """How a source reads one form: ``matches`` says whether a file is in
    its layout (cheaply, from the top of the file), ``read`` reads it."""

    matches: Callable[[Path], bool]
    read: Callable[[Path], T]
    layout: str
    """What to export, in the outside program's words."""


@dataclass(frozen=True)
class Source:
    name: str
    description: str
    holdings: Reader[Snapshot] | None = None
    transactions: Reader[Flows] | None = None

    def reader(self, form: Form) -> Reader[Snapshot] | Reader[Flows] | None:
        return self.holdings if form == "holdings" else self.transactions


_SOURCES: dict[str, Source] = {
    quicken.NAME: Source(
        name=quicken.NAME,
        description="Quicken report exports (CSV)",
        holdings=Reader(quicken.is_account_tree, quicken.read_account_tree,
                        'My Reports: "Net Worth Export" or "Year End Export"'),
        transactions=Reader(quicken.is_transaction_export, quicken.read_transactions,
                            'My Reports: "Transaction Export"'),
    ),
}


def sources() -> list[Source]:
    return list(_SOURCES.values())


def names(form: Form | None = None) -> list[str]:
    return [s.name for s in _SOURCES.values() if form is None or s.reader(form) is not None]


def get(name: str, form: Form) -> Source:
    """Source ``name``, which must read ``form``."""
    source = _SOURCES.get(name)
    if source is None:
        raise SourceError(f"unknown source {name!r}; sources: {', '.join(names(form))}")
    if source.reader(form) is None:
        raise SourceError(f"source {name!r} has no {form}; sources that do: {', '.join(names(form))}")
    return source


def detect(path: Path, form: Form | None = None) -> list[tuple[Source, Form]]:
    """Every (source, form) whose layout ``path`` is in; one, normally."""
    return [
        (source, each) for source in _SOURCES.values() for each in FORMS
        if (form is None or each == form)
        and (reader := source.reader(each)) is not None and reader.matches(path)
    ]


def _resolve(paths: Sequence[Path], name: str | None, form: Form) -> Source:
    """The source to read ``paths`` with: ``name``, or the one source whose
    layout every file is in."""
    if name is not None:
        return get(name, form)
    found: dict[str, Source] = {}
    for path in paths:
        matched = [source for source, _ in detect(path, form)]
        if len(matched) != 1:
            other = [f"{source.name} {each}" for source, each in detect(path)]
            hint = f" (it looks like {', '.join(other)})" if other and not matched else ""
            problem = "no source recognizes" if not matched else \
                f"more than one source ({', '.join(s.name for s in matched)}) recognizes"
            raise SourceError(
                f"{problem} {path} as {form}{hint}; name one with --source "
                f"({', '.join(names(form))}), or see 'sim import sources'")
        found[matched[0].name] = matched[0]
    if len(found) != 1:
        raise SourceError(
            f"the files come from different sources ({', '.join(found)}); import each source's "
            "files in a run of their own")
    return next(iter(found.values()))


def read_holdings(paths: Sequence[Path], name: str | None = None) -> Snapshot:
    """One snapshot from every file in ``paths``, read with source ``name``
    (or the one their layout says). The files must agree on the date."""
    source = _resolve(paths, name, "holdings")
    assert source.holdings is not None
    snapshots = [source.holdings.read(path) for path in paths]
    dates = {snapshot.as_of for snapshot in snapshots if snapshot.as_of is not None}
    if len(dates) > 1:
        raise SourceError(
            "the files are as of different dates: " + ", ".join(sorted(map(str, dates))))
    return Snapshot(
        source=source.name,
        holdings=tuple(holding for snapshot in snapshots for holding in snapshot.holdings),
        as_of=next(iter(dates), None),
    )


def read_flows(paths: Sequence[Path], name: str | None = None) -> Flows:
    """Every file's transactions, in order, read with source ``name`` (or the
    one their layout says)."""
    source = _resolve(paths, name, "transactions")
    assert source.transactions is not None
    frames = [source.transactions.read(path).transactions for path in paths]
    return Flows(source=source.name,
                 transactions=frames[0] if len(frames) == 1 else pd.concat(frames, ignore_index=True))
