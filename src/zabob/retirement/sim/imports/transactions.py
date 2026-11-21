"""What a transactions import does before the rules are worked out (issue #617):
gives a category to rows that come with none, by their payee, and brings the
category maps of several sources together.

A bank's download has a date, an amount and the bank's text for who was
paid; the expense baseline works from categories. ``imports/<source>/payees.csv``
(``mapping.PayeeMap``) is what joins them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import pandas as pd

from zabob.retirement.sim.imports.forms import ORIGIN
from zabob.retirement.sim.imports.mapping import NO_RULE, TRANSFER, PayeeMap, payee_key, payee_matches

__all__ = ["Categorizer", "CategoryMaps", "Unmatched", "categorize", "combine", "uncategorized"]


@dataclass
class CategoryMaps:
    """One source's ``categories.csv``, ``splits.csv`` and ``excluded_payees.csv``."""

    merges: dict[str, str] = field(default_factory=dict)
    exclusions: set[str] = field(default_factory=set)
    splits: dict[str, dict[str, str]] = field(default_factory=dict)
    excluded_payees: tuple[str, ...] = ()


def combine(maps: Mapping[str, CategoryMaps]) -> CategoryMaps:
    """The maps of every source in a run, as one: the rules are worked out
    from all the transactions together. Two sources may not say different
    things of one outside category, since nothing later knows which source a
    category came from."""
    found = CategoryMaps()
    said: dict[str, tuple[str, str]] = {}
    for source, each in maps.items():
        targets = {**each.merges, **dict.fromkeys(each.exclusions, NO_RULE)}
        for category, target in targets.items():
            other, before = said.setdefault(category, (source, target))
            if before != target:
                raise ValueError(
                    f"category {category!r} goes to {before!r} for {other} and to {target!r} for "
                    f"{source}; make their categories.csv agree")
        found.merges |= each.merges
        found.exclusions |= each.exclusions
        for category, by_account in each.splits.items():
            into = found.splits.setdefault(category, {})
            for account, target in by_account.items():
                if into.setdefault(account, target) != target:
                    raise ValueError(
                        f"category {category!r} paid from {account or 'any account'!r} is split two "
                        f"ways, to {into[account]!r} and to {target!r} ({source}); make the "
                        "splits.csv files agree")
        found.excluded_payees = tuple(dict.fromkeys((*found.excluded_payees, *each.excluded_payees)))
    return found


def uncategorized(frame: pd.DataFrame, source: str) -> bool:
    """Whether ``source``'s rows come with no category at all (a bank's
    file), so that it needs no ``categories.csv``."""
    return not frame.loc[frame[ORIGIN].eq(source), "category"].astype(bool).any()


@dataclass(frozen=True)
class Unmatched:
    """Payments to one payee that have no category, and that no row of the
    source's ``payees.csv`` covers."""

    source: str
    key: str
    """The payee as a pattern would be matched against it (``payee_key``)."""
    example: str
    """The payee as the file has it, once."""
    rows: int
    cents: int
    """What was paid in all, as a positive number."""


class Categorizer:
    """Gives the rows of the sources in ``payees`` their categories by
    payee, and keeps track of the payees still without one while they are
    asked about.

    A row of a source's map that is found in a payee says what its
    transactions are, whatever category their file gave them: a card's own
    may not be the household's. A ``transfer`` is marked as one, so it isn't
    spending, and a ``-`` row is dropped. A payment with a category of its
    file's keeps it otherwise; one with none is *unmatched*, to be asked
    about. Money in needs no category: only spending makes rules.

    Each row's payee is read once, and rows are kept by payee, so an answer
    is checked against the payees still unmatched and nothing else.
    """

    def __init__(self, frame: pd.DataFrame, payees: Mapping[str, PayeeMap]) -> None:
        self.frame = frame
        self.payees = payees
        self._rows: dict[tuple[str, str], list[int]] = {}
        self._examples: dict[tuple[str, str], str] = {}
        for at, (origin, payee) in enumerate(zip(frame[ORIGIN], frame["payee"], strict=True)):
            source = str(origin)
            if source in payees:
                group = (source, payee_key(str(payee)))
                self._rows.setdefault(group, []).append(at)
                self._examples.setdefault(group, str(payee))
        self._found = {group: payees[group[0]].category_of(group[1]) for group in self._rows}
        cents = frame["amount_cents"].tolist()
        blank = (~frame["category"].astype(bool)).tolist()
        # What each payee's uncategorized payments come to: only those are asked about.
        self._owed = {
            group: paid for group, rows in self._rows.items()
            if (paid := [int(cents[at]) for at in rows if blank[at] and cents[at] < 0])}

    def unmatched(self) -> list[Unmatched]:
        """The payees with uncategorized payments that no row covers, the
        most paid first. One with no words to match (a bare check number) is
        not among them: no row could ever name it."""
        found = [
            Unmatched(group[0], group[1], self._examples[group], len(paid), -sum(paid))
            for group, paid in self._owed.items() if group[1] and self._found[group] is None]
        found.sort(key=lambda each: (-each.cents, each.source, each.key))
        return found

    def add(self, source: str, pattern: str, category: str) -> None:
        """A new last row of ``source``'s map, applied to the payees no
        earlier row covers."""
        self.payees[source].add(pattern, category)
        key = payee_key(pattern)
        for group, found in self._found.items():
            if found is None and group[0] == source and payee_matches(key, group[1]):
                self._found[group] = category

    def result(self, rest: str | None = None) -> pd.DataFrame:
        """The frame with its categories. An uncategorized payment that
        nothing covers keeps its blank category unless ``rest`` names one
        for all of them; one whose payee has no words takes ``rest``, or
        ``Other``."""
        found = self.frame.copy()
        keep = [True] * len(found)
        category_at = found.columns.get_loc("category")
        transfer_at = found.columns.get_loc("transfer")
        cents = found["amount_cents"].tolist()
        blank = (~found["category"].astype(bool)).tolist()
        for group, rows in self._rows.items():
            category = self._found[group]
            for at in rows:
                if category == NO_RULE:
                    keep[at] = False
                elif category == TRANSFER:
                    found.iat[at, transfer_at] = TRANSFER
                elif category is not None:
                    found.iat[at, category_at] = category
                elif blank[at] and cents[at] < 0:
                    if not group[1]:
                        found.iat[at, category_at] = rest or "Other"
                    elif rest is not None:
                        found.iat[at, category_at] = rest
        return found.loc[keep].reset_index(drop=True)


def categorize(
    frame: pd.DataFrame, payees: Mapping[str, PayeeMap], *, rest: str | None = None,
) -> tuple[pd.DataFrame, list[Unmatched]]:
    """``frame`` with categories by payee on the rows of each source in
    ``payees``, and the payees it found none for, the most paid first
    (``Categorizer``, with nothing asked)."""
    sorter = Categorizer(frame, payees)
    return sorter.result(rest), sorter.unmatched()
