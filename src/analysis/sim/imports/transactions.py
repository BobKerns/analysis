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

from analysis.sim.imports.forms import ORIGIN
from analysis.sim.imports.mapping import NO_RULE, TRANSFER, PayeeMap, payee_key

__all__ = ["CategoryMaps", "Unmatched", "categorize", "combine", "uncategorized"]


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
    """Whether ``source``'s rows come with no category at all: a bank's
    file, whose categories are found by payee."""
    return not frame.loc[frame[ORIGIN].eq(source), "category"].astype(bool).any()


@dataclass(frozen=True)
class Unmatched:
    """Payments to one payee that no row of the source's ``payees.csv`` covers."""

    source: str
    key: str
    """The payee as a pattern would be matched against it (``payee_key``)."""
    example: str
    """The payee as the file has it, once."""
    rows: int
    cents: int
    """What was paid in all, as a positive number."""


def categorize(
    frame: pd.DataFrame, payees: Mapping[str, PayeeMap], *, rest: str | None = None,
) -> tuple[pd.DataFrame, list[Unmatched]]:
    """``frame`` with a category on the rows of each source in ``payees``
    (those sources' rows have none), and the payees it found none for, the
    most paid first.

    A ``transfer`` is marked as one, so it isn't spending; a ``-`` row is
    dropped. Money in needs no category: only spending makes rules. A
    payment that nothing covers keeps its blank category, to be asked about,
    unless ``rest`` names the category for all of them. One whose payee has
    no words to match (a bare check number) takes ``rest`` too, or ``Other``:
    no row of the map could ever name it.
    """
    found = frame.copy()
    keep = pd.Series(True, index=found.index)
    groups: dict[tuple[str, str], list[int]] = {}
    examples: dict[tuple[str, str], str] = {}
    for index, origin, payee, cents in zip(
            found.index, found[ORIGIN], found["payee"], found["amount_cents"], strict=True):
        source = str(origin)
        if source not in payees:
            continue
        category = payees[source].category(str(payee))
        if category == NO_RULE:
            keep[index] = False
        elif category == TRANSFER:
            found.at[index, "transfer"] = TRANSFER
        elif category is not None:
            found.at[index, "category"] = category
        elif cents < 0:
            key = payee_key(str(payee))
            if not key:
                found.at[index, "category"] = rest or "Other"
                continue
            groups.setdefault((source, key), []).append(int(cents))
            examples.setdefault((source, key), str(payee))
            if rest is not None:
                found.at[index, "category"] = rest
    unmatched = [
        Unmatched(source, key, examples[source, key], len(paid), -sum(paid))
        for (source, key), paid in groups.items()
    ]
    unmatched.sort(key=lambda each: (-each.cents, each.source, each.key))
    return found.loc[keep].reset_index(drop=True), unmatched
