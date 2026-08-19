"""Parse Quicken "Net Worth" report exports and map them onto this system's accounts.

Quicken's own account names rarely match ``accounts.csv`` exactly (and we don't
rename Quicken's accounts for clarity reasons), so imports go through an
explicit, versioned mapping CSV rather than fuzzy name matching.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

__all__ = [
    "QuickenBalance", "AccountMapEntry",
    "parse_net_worth_export", "load_account_map", "format_dollars",
]


@dataclass(frozen=True)
class QuickenBalance:
    """One leaf account balance from a Quicken Net Worth export."""

    quicken_name: str
    cents: int


@dataclass(frozen=True)
class AccountMapEntry:
    """One row of ``quicken_account_map.csv``."""

    action: str  # "map" or "skip"
    account_name: str  # target account_name in account_values.csv; "" if skip
    note: str


_MAP_HEADER = ("quicken_name", "account_name", "action", "note")


def parse_net_worth_export(path: Path) -> list[QuickenBalance]:
    """Parse a Quicken "Net Worth" report CSV export into leaf account balances.

    The export is an indented category tree (``,"Checking",``, ``," - Business","304.17"``,
    ``," - Total Checking","67,405.09"``, ...). Only rows for individual accounts are
    returned; category headers, "Total ..." subtotal rows, and the final "Net Worth"
    line are skipped.
    """
    balances: list[QuickenBalance] = []
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.reader(fh):
            if len(row) < 3:
                continue
            label = row[1].strip()
            value_raw = row[2].strip()
            if not label or not value_raw:
                continue
            name = label.lstrip(" -")
            if not name or name.startswith("Total "):
                continue
            try:
                cents = _dollars_to_cents(value_raw)
            except InvalidOperation:
                continue
            balances.append(QuickenBalance(quicken_name=name, cents=cents))
    return balances


def load_account_map(path: Path) -> dict[str, AccountMapEntry]:
    """Load ``quicken_name -> AccountMapEntry`` from a mapping CSV.

    Required columns: ``quicken_name, account_name, action, note``.
    ``action`` is ``"map"`` (import into ``account_name``) or ``"skip"``
    (acknowledged, deliberately not imported).
    """
    with Path(path).open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or tuple(reader.fieldnames) != _MAP_HEADER:
            raise ValueError(
                f"{path}: expected header {list(_MAP_HEADER)}, "
                f"got {list(reader.fieldnames or [])}"
            )
        entries: dict[str, AccountMapEntry] = {}
        for i, row in enumerate(reader, start=2):
            quicken_name = row["quicken_name"].strip()
            if not quicken_name:
                continue
            action = row["action"].strip()
            if action not in {"map", "skip"}:
                raise ValueError(f"{path} row {i}: action must be 'map' or 'skip', got {action!r}")
            if quicken_name in entries:
                raise ValueError(f"{path} row {i}: duplicate quicken_name {quicken_name!r}")
            entries[quicken_name] = AccountMapEntry(
                action=action,
                account_name=row["account_name"].strip(),
                note=row["note"].strip(),
            )
    return entries


def format_dollars(cents: int) -> str:
    """Format integer cents as the dollars-and-cents string account_values.csv expects."""
    sign = "-" if cents < 0 else ""
    dollars, rem = divmod(abs(cents), 100)
    return f"{sign}{dollars}.{rem:02d}"


def _dollars_to_cents(s: str) -> int:
    raw = s.strip().replace(",", "").replace("$", "")
    d = Decimal(raw)
    cents = d * 100
    if cents != cents.to_integral_value():
        raise InvalidOperation(f"sub-cent amount: {s!r}")
    return int(cents)
