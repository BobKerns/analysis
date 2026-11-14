"""The sources an import can read, and which one a file is (issue #527).

A source is one outside layout, or one program's family of them, under one
name: the name of its mapping folder, ``imports/<name>/``. It reads holdings,
flows, or both, each with a test of whether a file is in its layout so
``--source`` can be left off.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pandas as pd

from analysis.sim.imports import profiles, quicken
from analysis.sim.imports.forms import Flows, Snapshot

__all__ = [
    "FORMS", "Form", "Reader", "Source", "SourceError", "broken", "detect", "get", "names",
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
    profile: profiles.Profile | None = None
    """What describes it, when that is a profile and not code."""

    def reader(self, form: Form) -> Reader[Snapshot] | Reader[Flows] | None:
        return self.holdings if form == "holdings" else self.transactions

    @property
    def where(self) -> str:
        """Where the source is defined, for ``sim import sources``."""
        if self.profile is None or self.profile.built_in:
            return "built in"
        return self.profile.where


_CODED: dict[str, Source] = {
    quicken.NAME: Source(
        name=quicken.NAME,
        description="Quicken report exports (CSV)",
        holdings=Reader(quicken.is_account_tree, quicken.read_account_tree,
                        'My Reports: "Net Worth Export" or "Year End Export"'),
        transactions=Reader(quicken.is_transaction_export, quicken.read_transactions,
                            'My Reports: "Transaction Export"'),
    ),
}


def _from_profile(profile: profiles.Profile) -> Source:
    held = profile.holdings
    return Source(
        name=profile.name, description=profile.description, profile=profile,
        holdings=None if held is None else Reader(held.matches, held.read, held.layout),
    )


@dataclass(frozen=True)
class _Table:
    """Every source, read once for a command: the coded ones, the built-in
    profiles, and the household's own in the data dir's imports/ folder."""

    found: dict[str, Source]
    broken: dict[str, profiles.ProfileError]
    """Household profiles that can't be used, by source name. One stops
    only an import that names it: a half-written profile mustn't stop the
    import of another source's file."""

    def names(self, form: Form | None = None) -> list[str]:
        return [s.name for s in self.found.values() if form is None or s.reader(form) is not None]

    def get(self, name: str, form: Form) -> Source:
        if name in self.broken:
            raise self.broken[name]
        source = self.found.get(name)
        if source is None:
            raise SourceError(f"unknown source {name!r}; sources: {', '.join(self.names(form))}")
        if source.reader(form) is None:
            raise SourceError(
                f"source {name!r} has no {form}; sources that do: {', '.join(self.names(form))}")
        return source

    def detect(self, path: Path, form: Form | None = None) -> list[tuple[Source, Form]]:
        return [
            (source, each) for source in self.found.values() for each in FORMS
            if (form is None or each == form)
            and (reader := source.reader(each)) is not None and reader.matches(path)
        ]


def _table(imports: Path | None) -> _Table:
    found = dict(_CODED)
    described = profiles.built_in()
    broken: dict[str, profiles.ProfileError] = {}
    if imports is not None:
        mine, broken = profiles.user(imports)
        if taken := sorted(set(mine) & set(_CODED)):
            raise SourceError(
                f"{mine[taken[0]].where}: {taken[0]!r} is a source that reads more than a "
                "table, so a profile can't replace it; give the profile's folder another name")
        # A broken profile doesn't fall back to the built-in one of its name.
        described = {name: profile for name, profile in (described | mine).items() if name not in broken}
    return _Table(found | {name: _from_profile(profile) for name, profile in described.items()}, broken)


def sources(imports: Path | None = None) -> list[Source]:
    return list(_table(imports).found.values())


def broken(imports: Path | None = None) -> dict[str, str]:
    """The household's profiles that can't be used, and why."""
    return {name: str(error) for name, error in _table(imports).broken.items()}


def names(form: Form | None = None, imports: Path | None = None) -> list[str]:
    return _table(imports).names(form)


def get(name: str, form: Form, imports: Path | None = None) -> Source:
    """Source ``name``, which must read ``form``."""
    return _table(imports).get(name, form)


def detect(
    path: Path, form: Form | None = None, imports: Path | None = None,
) -> list[tuple[Source, Form]]:
    """Every (source, form) whose layout ``path`` is in; one, normally."""
    return _table(imports).detect(path, form)


def _resolve(paths: Sequence[Path], name: str | None, form: Form, imports: Path | None) -> Source:
    """The source to read ``paths`` with: ``name``, or the one source whose
    layout every file is in."""
    table = _table(imports)
    if name is not None:
        return table.get(name, form)
    found: dict[str, Source] = {}
    for path in paths:
        matched = [source for source, _ in table.detect(path, form)]
        if len(matched) != 1:
            other = [f"{source.name} {each}" for source, each in table.detect(path)]
            hint = f" (it looks like {', '.join(other)})" if other and not matched else ""
            problem = "no source recognizes" if not matched else \
                f"more than one source ({', '.join(s.name for s in matched)}) recognizes"
            # The file may be one a profile that can't be read was written for.
            unread = "".join(f"\n  not used: {error}" for error in table.broken.values()) \
                if not matched else ""
            raise SourceError(
                f"{problem} {path} as {form}{hint}; name one with --source "
                f"({', '.join(table.names(form))}), or see 'sim import sources'{unread}")
        found[matched[0].name] = matched[0]
    if len(found) != 1:
        raise SourceError(
            f"the files come from different sources ({', '.join(found)}); import each source's "
            "files in a run of their own")
    return next(iter(found.values()))


def read_holdings(
    paths: Sequence[Path], name: str | None = None, imports: Path | None = None,
    *, account: str | None = None,
) -> Snapshot:
    """One snapshot from every file in ``paths``, read with source ``name``
    (or the one their layout says). The files must agree on the date.
    ``account`` names the outside account of a file that doesn't say."""
    source = _resolve(paths, name, "holdings", imports)
    assert source.holdings is not None
    if account is None:
        snapshots = [source.holdings.read(path) for path in paths]
    elif source.profile is None or source.profile.holdings is None:
        raise SourceError(
            f"a {source.name} file names its accounts itself, so --outside-account doesn't apply")
    elif len(paths) > 1:
        raise SourceError("--outside-account names one file's account; import the files one at a time")
    else:
        snapshots = [source.profile.holdings.read(paths[0], account)]
    dates = {snapshot.as_of for snapshot in snapshots if snapshot.as_of is not None}
    if len(dates) > 1:
        raise SourceError(
            "the files are as of different dates: " + ", ".join(sorted(map(str, dates))))
    # The applier adds what lands in one lot together, so the same outside
    # account read from two files would be counted twice.
    seen: dict[tuple[str, str], int] = {}
    for number, snapshot in enumerate(snapshots):
        for holding in snapshot.holdings:
            first = seen.setdefault((holding.account, holding.asset), number)
            if first != number:
                raise SourceError(
                    f"{holding.label!r} is in more than one file ({paths[first]}, "
                    f"{paths[number]}); import each outside account from one file")
    return Snapshot(
        source=source.name,
        holdings=tuple(holding for snapshot in snapshots for holding in snapshot.holdings),
        as_of=next(iter(dates), None),
    )


def read_flows(
    paths: Sequence[Path], name: str | None = None, imports: Path | None = None,
) -> Flows:
    """Every file's transactions, in order, read with source ``name`` (or the
    one their layout says). Two files may not cover the same days of one
    outside account: their transactions would be counted twice."""
    source = _resolve(paths, name, "transactions", imports)
    assert source.transactions is not None
    frames = [source.transactions.read(path).transactions for path in paths]
    spans: dict[str, list[tuple[dt.date, dt.date, int]]] = {}
    for number, frame in enumerate(frames):
        days: dict[str, list[dt.date]] = {}
        for account, stamp in zip(frame["account"], frame["date"], strict=True):
            days.setdefault(str(account), []).append(stamp.date())
        for account, dates in days.items():
            first, last = min(dates), max(dates)
            for start, end, other in spans.setdefault(account, []):
                if first <= end and start <= last:
                    raise SourceError(
                        f"{paths[other]} and {paths[number]} both have transactions of "
                        f"{account!r} between {max(first, start)} and {min(last, end)}; "
                        "import each period of an outside account from one file")
            spans[account].append((first, last, number))
    return Flows(source=source.name,
                 transactions=frames[0] if len(frames) == 1 else pd.concat(frames, ignore_index=True))
