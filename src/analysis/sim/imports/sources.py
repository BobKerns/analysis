"""The sources an import can read, and which one a file is (issue #527).

A source is one outside layout, or one program's family of them, under one
name: the name of its mapping folder, ``imports/<name>/``. It reads holdings,
flows, or both, each with a test of whether a file is in its layout so
``--source`` can be left off.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import itertools
from collections import Counter
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import pandas as pd

from analysis.sim.imports import ofx, profiles, quicken
from analysis.sim.imports.forms import CORRECTS, ORIGIN, REFERENCE, REMOVES, Budget, Flows, Snapshot

__all__ = [
    "FORMS", "Form", "Reader", "Source", "SourceError", "broken", "detect", "get", "names",
    "read_budget", "read_flows", "read_holdings", "sources", "table", "unnamed",
]

Form = Literal["holdings", "transactions", "budget"]
FORMS: tuple[Form, ...] = ("holdings", "transactions", "budget")


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
    unnamed: Callable[[Path], bool] | None = None
    """Whether a file doesn't say which outside account it is, where some
    of the source's don't: such a file is read with ``read_named``."""
    read_named: Callable[[Path, str], T] | None = None
    """Reads a file as the outside account given."""


@dataclass(frozen=True)
class Source:
    name: str
    description: str
    holdings: Reader[Snapshot] | None = None
    transactions: Reader[Flows] | None = None
    budget: Reader[Budget] | None = None
    profile: profiles.Profile | None = None
    """What describes it, when that is a profile and not code."""
    payees: bool = False
    """Whether rows with no category are read by the source's
    ``payees.csv``, and asked about, even before it has one: every
    profile's, and a coded source whose transactions never come with
    categories."""

    def reader(self, form: Form) -> Reader[Snapshot] | Reader[Flows] | Reader[Budget] | None:
        return {"holdings": self.holdings, "transactions": self.transactions, "budget": self.budget}[form]

    @property
    def where(self) -> str:
        """Where the source is defined, for ``sim import sources``."""
        if self.profile is None or self.profile.built_in:
            return "built in"
        return self.profile.where


def _quicken_holdings(path: Path, account: str | None = None) -> Snapshot:
    """A Quicken holdings report: the account tree, which names its
    accounts, or one account's portfolio report, which doesn't."""
    if quicken.is_portfolio(path):
        if account is None:
            raise SourceError(
                f"{path} doesn't say which account it is (a portfolio report names none); name it with "
                "--outside-account, or import it alone at a terminal to be asked")
        return quicken.read_portfolio(path, account)
    if account is not None:
        raise SourceError(f"{path} names its accounts itself, so --outside-account doesn't apply")
    return quicken.read_account_tree(path)


_CODED: dict[str, Source] = {
    quicken.NAME: Source(
        name=quicken.NAME,
        description="Quicken report exports (CSV)",
        holdings=Reader(
            lambda path: quicken.is_account_tree(path) or quicken.is_portfolio(path), _quicken_holdings,
            'My Reports: "Net Worth Export" or "Year End Export"; or, for what one investment account '
            'holds, the "Portfolio Value & Cost Basis" report of that account alone',
            unnamed=quicken.is_portfolio, read_named=_quicken_holdings),
        transactions=Reader(quicken.is_transaction_export, quicken.read_transactions,
                            'My Reports: "Transaction Export", or the "All Transactions" report'),
        budget=Reader(quicken.is_budget, quicken.read_budget,
                      'Budget: Export, "Full" (a column for each month) or "Summary"'),
    ),
    ofx.NAME: Source(
        name=ofx.NAME,
        description="OFX and QFX downloads (a bank's, a card's, a brokerage's)",
        holdings=Reader(ofx.is_ofx, ofx.read_holdings,
                        'a download for "Quicken" or "Money" (.qfx, .ofx): balances and positions'),
        transactions=Reader(ofx.is_ofx, ofx.read_transactions,
                            'a download for "Quicken" or "Money" (.qfx, .ofx): a bank account\'s or a card\'s'),
        payees=True,
    ),
}


def _from_profile(profile: profiles.Profile) -> Source:
    held, moved = profile.holdings, profile.transactions
    return Source(
        name=profile.name, description=profile.description, profile=profile, payees=True,
        holdings=None if held is None else Reader(
            held.matches, held.read, held.layout,
            unnamed=lambda path: not held.names_account(path), read_named=held.read),
        transactions=None if moved is None else Reader(moved.matches, moved.read, moved.layout),
    )


@dataclass(frozen=True)
class Table:
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


def table(imports: Path | None = None) -> Table:
    """The sources, read now: for a command that asks about them more than
    once, since each reading parses every profile."""
    found = dict(_CODED)
    described = profiles.built_in()
    broken: dict[str, profiles.ProfileError] = {}
    if imports is not None:
        mine, broken = profiles.user(imports)
        # Whether it can be used or not: one that can't mustn't stand in the
        # coded source's way where one that can is refused.
        if taken := sorted((set(mine) | set(broken)) & set(_CODED)):
            raise SourceError(
                f"{imports / taken[0] / profiles.PROFILE}: {taken[0]!r} is a source that reads more than a "
                "table, so a profile can't replace it; give the profile's folder another name")
        # A broken profile doesn't fall back to the built-in one of its name.
        described = {name: profile for name, profile in (described | mine).items() if name not in broken}
    return Table(found | {name: _from_profile(profile) for name, profile in described.items()}, broken)


def sources(imports: Path | None = None) -> list[Source]:
    return list(table(imports).found.values())


def broken(imports: Path | None = None) -> dict[str, str]:
    """The household's profiles that can't be used, and why."""
    return {name: str(error) for name, error in table(imports).broken.items()}


def names(form: Form | None = None, imports: Path | None = None) -> list[str]:
    return table(imports).names(form)


def get(name: str, form: Form, imports: Path | None = None) -> Source:
    """Source ``name``, which must read ``form``."""
    return table(imports).get(name, form)


def detect(
    path: Path, form: Form | None = None, imports: Path | None = None,
) -> list[tuple[Source, Form]]:
    """Every (source, form) whose layout ``path`` is in; one, normally."""
    return table(imports).detect(path, form)


def _resolve(
    paths: Sequence[Path], name: str | Source | None, form: Form, imports: Path | None,
    known: Table | None = None,
) -> Source:
    """The source to read ``paths`` with: ``name`` (or the source itself,
    already found), or the one source whose layout every file is in."""
    if isinstance(name, Source):
        return name
    known = known or table(imports)
    if name is not None:
        return known.get(name, form)
    found: dict[str, Source] = {}
    for path in paths:
        matched = [source for source, _ in known.detect(path, form)]
        if len(matched) != 1:
            other = [f"{source.name} {each}" for source, each in known.detect(path)]
            hint = f" (it looks like {', '.join(other)})" if other and not matched else ""
            problem = "no source recognizes" if not matched else \
                f"more than one source ({', '.join(s.name for s in matched)}) recognizes"
            # The file may be one a profile that can't be read was written for.
            unread = "".join(f"\n  not used: {error}" for error in known.broken.values()) \
                if not matched else ""
            raise SourceError(
                f"{problem} {path} as {form}{hint}; name one with --source "
                f"({', '.join(known.names(form))}), or see 'sim import sources'{unread}")
        found[matched[0].name] = matched[0]
    if len(found) != 1:
        raise SourceError(
            f"the files come from different sources ({', '.join(found)}); import each source's "
            "files in a run of their own")
    return next(iter(found.values()))


@contextlib.contextmanager
def _reading(path: Path) -> Generator[None]:
    """Reading ``path`` as a named source: a file that isn't text is said to
    be that, and not as the codec's complaint about a byte. It is most
    often the outside program's own data file (Quicken's ``.qxf``), given
    where a report it exports was meant."""
    try:
        yield
    except UnicodeDecodeError:
        raise SourceError(
            f"{path} isn't a text file, so it isn't a report exported as CSV. A program's own data or "
            "transfer file (such as Quicken's .qxf or .qdf) can't be read: export a report from the program "
            "instead ('sim import sources' lists what each source reads)") from None


def unnamed(
    paths: Sequence[Path], name: str | None = None, imports: Path | None = None,
) -> tuple[Source, list[Path]]:
    """The source to read holdings in ``paths`` with, and the files among
    them that don't say which outside account they are: one must be given
    for each (``read_holdings``'s ``account``)."""
    source = _resolve(paths, name, "holdings", imports)
    held = source.holdings
    return source, [] if held is None or held.unnamed is None else [path for path in paths if held.unnamed(path)]


def read_holdings(
    paths: Sequence[Path], name: str | Source | None = None, imports: Path | None = None,
    *, account: str | None = None,
) -> Snapshot:
    """One snapshot from every file in ``paths``, read with source ``name``
    (or the one their layout says). The files must agree on the date.
    ``account`` names the outside account of a file that doesn't say."""
    source = _resolve(paths, name, "holdings", imports)
    assert source.holdings is not None
    if account is None:
        snapshots = []
        for path in paths:
            with _reading(path):
                snapshots.append(source.holdings.read(path))
    elif source.holdings.read_named is None:
        raise SourceError(
            f"a {source.name} file names its accounts itself, so --outside-account doesn't apply")
    elif len(paths) > 1:
        raise SourceError("--outside-account names one file's account; import the files one at a time")
    else:
        with _reading(paths[0]):
            snapshots = [source.holdings.read_named(paths[0], account)]
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
        notes=tuple(note for snapshot in snapshots for note in snapshot.notes),
        complete=frozenset(account for snapshot in snapshots for account in snapshot.complete),
        activity={account: activity for snapshot in snapshots for account, activity in snapshot.activity.items()},
        unaccounted={account: why for snapshot in snapshots for account, why in snapshot.unaccounted.items()},
        # Only a file read alone keeps its other dates: two files' agree on one.
        dated=snapshots[0].dated if len(snapshots) == 1 else {},
    )


def read_flows(
    paths: Sequence[Path], name: str | None = None, imports: Path | None = None,
    *, account: str | Sequence[str] | None = None,
    ask_account: Callable[[Path, str], str] | None = None,
) -> Flows:
    """Every file's transactions, in order, each read with source ``name``
    (or the one its own layout says: the files of one run may come from
    several, a bank's and a card's, and make one set of rules).

    Two files may not cover the same days of one outside account: their
    transactions would be counted twice. Files whose every row of an account
    has the outside program's own reference are the exception: a row that an
    earlier file had is left out, so such downloads may overlap each other.

    ``account`` names the outside account of each file that doesn't say, in
    the files' order. Without it ``ask_account(path, source name)`` is asked
    for each, when given; otherwise one such file is the account of its own
    name, and several are refused, since the names of a bank's downloads
    don't tell its accounts apart."""
    given = [account] if isinstance(account, str) else list(account or ())
    known = table(imports)
    found = [known.get(name, "transactions") if name is not None
             else _resolve([path], None, "transactions", imports, known) for path in paths]
    unnamed = [
        number for number, (path, source) in enumerate(zip(paths, found, strict=True))
        if source.profile is not None and (described := source.profile.transactions) is not None
        and not described.names_account(path)]
    if given and not unnamed:
        raise SourceError(
            ("the files name" if len(paths) > 1 else f"a {found[0].name} file names")
            + " their accounts themselves, so --outside-account doesn't apply")
    if given and len(given) != len(unnamed):
        raise SourceError(
            f"{len(unnamed)} file(s) don't say which account they are ("
            + ", ".join(str(paths[number]) for number in unnamed)
            + f"), and --outside-account was given {len(given)} time(s); give it once for each, "
            "in the files' order")
    if not given and ask_account is None and len(unnamed) > 1:
        raise SourceError(
            f"{len(unnamed)} files don't say which account they are ("
            + ", ".join(str(paths[number]) for number in unnamed)
            + "), and their names may not either; run at a terminal to be asked, or give "
            "--outside-account once for each, in the files' order")
    named = dict(zip(unnamed, given, strict=True)) if given else {}
    frames: list[pd.DataFrame] = []
    met: dict[str, None] = {}
    references = _References()
    # Accounts that one file has together, so are known to differ.
    apart: set[tuple[str, str, str]] = set()
    duplicates = 0
    notes: list[str] = []
    # Each outside account's days in each file, and whether every one of its
    # rows there has a reference.
    spans: dict[tuple[str, str], list[tuple[dt.date, dt.date, bool, int]]] = {}
    for number, (path, source) in enumerate(zip(paths, found, strict=True)):
        assert source.transactions is not None
        described = None if source.profile is None else source.profile.transactions
        if number in unnamed and described is not None:
            said = named.get(number) or (ask_account(path, source.name) if ask_account else None)
            with _reading(path):
                read = described.read(path, said)
        else:
            with _reading(path):
                read = source.transactions.read(path)
        frame = read.transactions
        notes += read.notes
        frame = frame.assign(**{ORIGIN: source.name})
        if REFERENCE not in frame.columns:
            frame[REFERENCE] = ""
        days: dict[tuple[str, str], list[dt.date]] = {}
        bare: set[tuple[str, str]] = set()
        for each, stamp, reference in zip(frame["account"], frame["date"], frame[REFERENCE], strict=True):
            key = (source.name, str(each))
            days.setdefault(key, []).append(stamp.date())
            if not reference:
                bare.add(key)
        for key, dates in days.items():
            first, last = min(dates), max(dates)
            for start, end, referenced, other in spans.setdefault(key, []):
                # Where both files give every row its reference, a row in
                # both is found below and counted once. A file without them
                # (a card's short download) can't be matched row to row.
                if first <= end and start <= last and not (referenced and key not in bare):
                    raise SourceError(
                        f"{paths[other]} and {paths[number]} both have transactions of "
                        f"{key[1]!r} between {max(first, start)} and {min(last, end)}; "
                        "import each period of an outside account from one file")
            spans[key].append((first, last, key not in bare, number))
        apart.update((source.name, *pair) for pair in itertools.combinations(sorted(key[1] for key in days), 2))
        fresh, said = references.sift(source.name, path, frame)
        notes += said
        duplicates += fresh.count(False)
        frames.append(frame.loc[fresh].reset_index(drop=True))
        met[source.name] = None
    transactions = _corrected(frames[0] if len(frames) == 1 else pd.concat(frames, ignore_index=True))
    notes += _under_two_names(transactions, apart)
    return Flows(source="+".join(met), duplicates=duplicates, origins=tuple(met),
                 transactions=transactions, notes=tuple(dict.fromkeys(notes)))


_Row = tuple[str, str, str, str]
_What = tuple[dt.date, int]


@dataclass
class _References:
    """Which rows of a run's files are ones an earlier file had, by their
    references.

    A reference is the institution's word that two rows are one
    transaction, and is taken at it: a row whose reference an earlier file
    had is left out, whatever its day and amount (a charge may post on
    another day, or with its tip). Within one file a reference repeated is
    the file's own business (some make it from the day, amount and payee,
    so two like purchases share one), so only as many are left out as the
    files before kept: those with an earlier copy's day and amount first,
    wherever they are in the file, and only then others.

    A correction may reuse the reference of what it corrects, so it is a
    row of its own: only the same correction, in an earlier file, is its
    duplicate. A row that corrects nothing is the duplicate of any earlier
    row with its reference, a correction too: a later download lists a
    corrected transaction as it stands, with nothing to say it was one.

    What a reference can't be trusted for is noted: rows left out whose
    day or amount is not the earlier file's, and rows kept that have the
    day and amount of an earlier file's row under another reference."""

    # The referenced rows kept so far, by their source, account, reference
    # and what they correct: their days and amounts.
    seen: dict[_Row, Counter[_What]] = field(default_factory=dict)
    # The same, whatever they correct.
    referenced: dict[tuple[str, str, str], Counter[_What]] = field(default_factory=dict)
    # Each account's referenced rows kept so far, by day and amount.
    had: dict[tuple[str, str], Counter[_What]] = field(default_factory=dict)

    def sift(self, source: str, path: Path, frame: pd.DataFrame) -> tuple[list[bool], list[str]]:
        """Whether each row of ``frame``, the next file's (``path``), is
        new to the run, and the notes of what its references left unsure."""
        corrects = frame[CORRECTS].fillna("").astype(str) if CORRECTS in frame.columns else [""] * len(frame)
        fresh = [True] * len(frame)
        copies: dict[_Row, list[tuple[int, _What]]] = {}
        for place, (each, reference, correcting, stamp, cents) in enumerate(zip(
                frame["account"], frame[REFERENCE], corrects, frame["date"], frame["amount_cents"], strict=True)):
            if reference:
                key = (source, str(each), str(reference), str(correcting))
                copies.setdefault(key, []).append((place, (stamp.date(), int(cents))))
        new: list[tuple[_Row, _What]] = []
        # The earlier files' rows that this file's duplicates are, by day and amount.
        again: dict[tuple[str, str], Counter[_What]] = {}
        differing: Counter[str] = Counter()
        unmet: list[tuple[tuple[str, str], _What]] = []
        for key, rows in copies.items():
            account, correcting = key[:2], key[3]
            earlier = Counter(self.seen.get(key, ()) if correcting else self.referenced.get(key[:3], ()))
            known = bool(earlier)
            unmatched: list[tuple[int, _What]] = []
            for place, what in rows:
                if earlier[what] > 0:
                    earlier[what] -= 1
                    fresh[place] = False
                    again.setdefault(account, Counter())[what] += 1
                else:
                    unmatched.append((place, what))
            left = sorted((+earlier).elements())
            for (place, _), was in zip(unmatched, left, strict=False):
                fresh[place] = False
                again.setdefault(account, Counter())[was] += 1
                differing[account[1]] += 1
            for _, what in unmatched[len(left):]:
                new.append((key, what))
                if not known and not correcting:
                    unmet.append((account, what))
        # A row no earlier file's reference matched, though one of them has
        # a row of its day and amount that this file doesn't have as well.
        alike: Counter[str] = Counter()
        left_over = {account: self.had[account] - again.get(account, Counter())
                     for account in {account for account, _ in unmet} & set(self.had)}
        for account, what in unmet:
            if left_over.get(account, {}).get(what, 0) > 0:
                left_over[account][what] -= 1
                alike[account[1]] += 1
        for key, what in new:
            self.seen.setdefault(key, Counter())[what] += 1
            self.referenced.setdefault(key[:3], Counter())[what] += 1
            self.had.setdefault(key[:2], Counter())[what] += 1
        return fresh, [
            f"{count} transaction(s) of {account!r} in {path} have a reference that an earlier file gave a "
            "transaction of another day or amount; they are left out as that transaction, at the earlier "
            "file's day and amount. If this institution numbers each download's transactions afresh, they "
            "are other transactions and are missing: import one download that has the whole period"
            for account, count in differing.items()] + [
            f"{count} transaction(s) of {account!r} in {path} have the day and amount of ones in an earlier "
            "file, under other references; if this institution gives a transaction a new reference in each "
            "download, they are counted twice: import downloads that don't overlap"
            for account, count in alike.items()]


def _corrected(frame: pd.DataFrame) -> pd.DataFrame:
    """``frame`` with its corrections applied (``forms.CORRECTS``), and
    without their columns. Here and not in a source's reader, since what a
    row corrects may be in an earlier file of the run, where it survives
    the dropping of duplicates.

    A row is left out when another row of its account names its reference:
    a correction that a later one corrects goes too, and one that names
    its own reference (an institution reusing the id) stays. A row that
    only removes is never a transaction."""
    if CORRECTS not in frame.columns:
        return frame
    corrects = frame[CORRECTS].fillna("").astype(str)
    removes = frame[REMOVES].fillna(value=False).astype(bool)
    if not corrects.any() and not removes.any():
        return frame.drop(columns=[CORRECTS, REMOVES])
    naming: dict[tuple[str, str, str], set[int]] = {}
    for place, (origin, account, reference) in enumerate(zip(frame[ORIGIN], frame["account"], corrects, strict=True)):
        if reference:
            naming.setdefault((origin, str(account), reference), set()).add(place)
    keep = [
        not gone and not (naming.get((origin, str(account), str(reference)), set()) - {place})
        for place, (origin, account, reference, gone)
        in enumerate(zip(frame[ORIGIN], frame["account"], frame[REFERENCE], removes, strict=True))]
    return frame.loc[keep].drop(columns=[CORRECTS, REMOVES]).reset_index(drop=True)


def _under_two_names(frame: pd.DataFrame, apart: set[tuple[str, str, str]]) -> list[str]:
    """Notes of transactions that two outside accounts of one source both
    have: the same reference, day and amount. An account's name is made
    from what its file says, and one account may come out under two (a
    bank's two kinds of download, a card reissued with a new number); its
    overlapping downloads are then neither refused nor counted once. Not
    two that are in ``apart`` (a source and two names, in order): one file
    has both, so they are two accounts."""
    keys = [ORIGIN, REFERENCE, "date", "amount_cents"]
    referenced = frame[frame[REFERENCE].astype(str) != ""]
    # Only rows that share all four with another can be two accounts' rows.
    referenced = referenced[referenced.duplicated(keys, keep=False)]
    shared: dict[tuple[str, str, str], int] = {}
    for _, rows in referenced.groupby(keys, sort=False):
        accounts = sorted(set(rows["account"].astype(str)))
        for first, second in itertools.combinations(accounts, 2):
            key = (str(rows[ORIGIN].iloc[0]), first, second)
            if key not in apart:
                shared[key] = shared.get(key, 0) + 1
    return [
        f"{count} transaction(s) of {first!r} and of {second!r} ({origin}) have the same reference, day and "
        "amount; if they are one account under two names, those transactions are counted twice: import "
        "its downloads so they don't overlap"
        for (origin, first, second), count in shared.items()]


def read_budget(path: Path, name: str | None = None, imports: Path | None = None) -> Budget:
    """The budget in ``path``, read with source ``name`` (or the one its
    layout says)."""
    source = _resolve([path], name, "budget", imports)
    assert source.budget is not None
    try:
        with _reading(path):
            return source.budget.read(path)
    except ValueError as error:
        raise SourceError(str(error)) from None
