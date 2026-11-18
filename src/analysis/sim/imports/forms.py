"""The two common forms every source is read into (issue #527)."""

from __future__ import annotations

import csv
import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pandas as pd

__all__ = [
    "Activity",
    "CORRECTS", "FLOW_COLUMNS", "ORIGIN", "REFERENCE", "REMOVES", "Budget", "BudgetLine", "Flows", "Holding",
    "Snapshot", "read_rows",
]


# What a holding is named (its ``asset``) when it is an account's cash, or
# what it has borrowed, beside its holdings that have symbols. No symbol is
# written so; its lot is the account's cash lot (the symbol ``CASH``), or one
# with no symbol. With a name of its own it can have a mapping row of its
# own, apart from the account's other holdings.
CASH = "(cash)"
MARGIN = "(margin)"
UNLISTED = frozenset({CASH, MARGIN})


@dataclass(frozen=True)
class Holding:
    """One thing an outside account holds. Only the account and a value are
    required; a source fills in what its file has."""

    account: str
    """The account's name as the source spells it."""
    value_cents: int
    """Signed as the source has it: a debt is usually negative."""
    asset: str = ""
    """A symbol, or a name ("House"); blank when the file gives one number
    for the whole account."""
    quantity: Decimal | None = None
    cost_basis_cents: int | None = None
    acquired: dt.date | None = None
    hints: Mapping[str, str] = field(default_factory=dict)
    """Whatever else the file says about the account (type, owner, tax
    treatment), for pre-filling a new account."""

    @property
    def label(self) -> str:
        """``account``, or ``account / asset``: how a message names it."""
        return f"{self.account} / {self.asset}" if self.asset else self.account


@dataclass(frozen=True)
class Activity:
    """What a file says was posted to an account, beside its balance: enough
    to work the balance back to an earlier day (``holdings.at``)."""

    start: dt.date
    """The first day the file lists the account's transactions of."""
    end: dt.date
    """The last such day."""
    posted: tuple[tuple[dt.date, int], ...]
    """Each transaction's day and signed cents, money out negative."""


@dataclass(frozen=True)
class Snapshot:
    """Holdings as of one date."""

    source: str
    holdings: tuple[Holding, ...]
    as_of: dt.date | None = None
    """None when the file doesn't say."""
    notes: tuple[str, ...] = ()
    """What the reader guessed, or read without being sure of, for the
    user to check."""
    complete: frozenset[str] = frozenset()
    """The accounts (as ``Holding.account``) whose holdings the file says
    it lists every one of, where it says so apart from the holdings: an
    account listed in full that holds only cash, or nothing, has sold
    what an earlier file had."""
    activity: Mapping[str, Activity] = field(default_factory=dict)
    """By account (as ``Holding.account``), what was posted to it up to the
    file's date, where the file has that and the account's one holding is
    its balance. A bank's download is as of the day it was made and never
    of an earlier one; with this its balance on an earlier day is known."""
    dated: Mapping[dt.date, tuple[Holding, ...]] = field(default_factory=dict)
    """The holdings as of each date the file has them for, ``as_of`` among
    them, where it has more than one: a report over a period, with a set
    of columns for each of its dates (``holdings.nearest`` picks one)."""
    dated_notes: Mapping[dt.date, tuple[str, ...]] = field(default_factory=dict)
    """With ``dated``, what is to be said of one date's holdings only: the
    notes of the date picked join ``notes``."""
    unaccounted: Mapping[str, str] = field(default_factory=dict)
    """By account, why a file that could have such activity has none for
    it, as the end of a sentence: said where the balance was wanted as of
    an earlier day, so that it is never silently set as of the file's."""


FLOW_COLUMNS = (
    "source_row", "account", "date", "payee", "category", "transfer", "memo", "amount_cents",
)
"""What every source's transactions frame has: ``date`` a timestamp,
``amount_cents`` signed with money out negative, ``transfer`` non-blank for
a move between the household's own accounts, ``source_row`` the 1-based line
of the file (the row's place among them, where a file has no lines to
count by). A source may add columns of its own."""


ORIGIN = "origin"
"""The column naming each row's source, in transactions read from several."""
REFERENCE = "reference"
"""A column some sources add: the outside program's own id for a transaction,
the same in every file that has it."""
CORRECTS = "corrects"
"""A column some sources add, beside ``REFERENCE``: on a row that corrects
an earlier transaction of its account, that transaction's reference; blank
otherwise. ``sources.read_flows`` leaves out what a row corrects, and the
column."""
REMOVES = "removes"
"""With ``CORRECTS``: whether the row only removes what it corrects, and is
no transaction itself."""


@dataclass(frozen=True)
class Flows:
    """Transactions over a period, one row each (``FLOW_COLUMNS``)."""

    source: str
    """The source they were read from; of several, a label naming them all
    (joined by '+'), for a rule's provenance. Not to be taken apart: a
    source's name may have a '+' in it."""
    transactions: pd.DataFrame
    duplicates: int = 0
    """Rows left out because an earlier file of the run had them too, by
    their reference."""
    origins: tuple[str, ...] = ()
    """Each source the rows came from, in the order first met, when there
    are several."""
    notes: tuple[str, ...] = ()
    """What the reader guessed, or read without being sure of, for the
    user to check."""

    @property
    def sources(self) -> tuple[str, ...]:
        """Each source the rows came from, in the order first met."""
        return self.origins or (self.source,)

    def __post_init__(self) -> None:
        missing = [c for c in FLOW_COLUMNS if c not in self.transactions.columns]
        if missing:
            raise ValueError(f"{self.source}: transactions lack column(s) {missing}")


@dataclass(frozen=True)
class BudgetLine:
    """What a budget plans for one category: its own amount, apart from
    any category under it."""

    income: bool
    """Money in; otherwise spending."""
    category: str
    """The category's whole path, its levels joined by ``:`` as the outside
    program's transactions name it (``Auto:Fuel``)."""
    total_cents: int
    """The period's amount. Spending is positive; a budgeted refund negative."""
    monthly_cents: tuple[int, ...] = ()
    """Each month's amount, in the order of ``Budget.months``; empty when
    the file has only the period's total."""


@dataclass(frozen=True)
class Budget:
    """A plan of income and spending by category over a period."""

    source: str
    lines: tuple[BudgetLine, ...]
    months: tuple[dt.date, ...] = ()
    """The first day of each month of the period, in order; empty when the
    file doesn't say which months it covers."""


def format_dollars(cents: int) -> str:
    """Format integer cents as the dollars-and-cents string account_lots.csv expects."""
    sign = "-" if cents < 0 else ""
    dollars, rem = divmod(abs(cents), 100)
    return f"{sign}{dollars}.{rem:02d}"


def read_rows(path: Path, limit: int | None = None) -> list[list[str]]:
    """The rows of an outside CSV file, or its first ``limit``: how every
    source reads one, so they agree on the encoding (a byte-order mark is
    not part of the first cell)."""
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        rows = []
        for row in csv.reader(fh):
            rows.append(row)
            if limit is not None and len(rows) >= limit:
                break
        return rows
