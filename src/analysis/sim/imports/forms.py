"""The two common forms every source is read into (issue #527)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

import pandas as pd

__all__ = ["FLOW_COLUMNS", "Flows", "Holding", "Snapshot"]


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
class Snapshot:
    """Holdings as of one date."""

    source: str
    holdings: tuple[Holding, ...]
    as_of: dt.date | None = None
    """None when the file doesn't say."""


FLOW_COLUMNS = (
    "source_row", "account", "date", "payee", "category", "transfer", "memo", "amount_cents",
)
"""What every source's transactions frame has: ``date`` a timestamp,
``amount_cents`` signed with money out negative, ``transfer`` non-blank for
a move between the household's own accounts, ``source_row`` the 1-based line
of the file. A source may add columns of its own."""


@dataclass(frozen=True)
class Flows:
    """Transactions over a period, one row each (``FLOW_COLUMNS``)."""

    source: str
    transactions: pd.DataFrame

    def __post_init__(self) -> None:
        missing = [c for c in FLOW_COLUMNS if c not in self.transactions.columns]
        if missing:
            raise ValueError(f"{self.source}: transactions lack column(s) {missing}")
