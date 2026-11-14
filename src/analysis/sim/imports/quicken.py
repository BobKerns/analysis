"""Quicken's own report layouts: the first source (issue #527).

The "Net Worth Export" and "Year End Export" reports (My Reports) share one
indented-category CSV shape, read as holdings. The "Transaction Export" is
grouped under account header rows, read as flows. Neither is a flat table,
so a column profile can't describe them.
"""

from __future__ import annotations

import csv
import datetime as dt
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd

from analysis.sim.imports.forms import Flows, Holding, Snapshot, read_rows

__all__ = [
    "NAME", "is_account_tree", "is_transaction_export", "read_account_tree",
    "read_transactions",
]

NAME = "quicken"
_DATE_FORMAT = "%Y-%m-%d"
_TRANSACTION_COLUMNS = {"Date", "Payee", "Category", "Amount"}


def _is_transaction_header(row: list[str]) -> bool:
    return bool(row) and row[0].strip().casefold() == "account" \
        and _TRANSACTION_COLUMNS <= {cell.strip() for cell in row}


def is_transaction_export(path: Path) -> bool:
    """Whether ``path`` looks like a Transaction Export: an ``Account`` header
    row with the transaction columns, near the top."""
    try:
        return any(_is_transaction_header(row) for row in read_rows(path, 30))
    except (OSError, UnicodeDecodeError, csv.Error):
        return False


def is_account_tree(path: Path) -> bool:
    """Whether ``path`` looks like an account-tree report: three columns, the
    first holding only section names, with ``Assets`` or ``Liabilities``
    among them."""
    try:
        rows = read_rows(path, 60)
    except (OSError, UnicodeDecodeError, csv.Error):
        return False
    if any(_is_transaction_header(row) for row in rows):
        return False
    sections = {row[0].strip().casefold() for row in rows if len(row) == 3 and row[0].strip()}
    return bool(sections & {"assets", "liabilities"})


def _dollars_to_cents(s: str) -> int:
    raw = s.strip().replace(",", "").replace("$", "")
    # An accountant's negative: (300.00).
    if raw.startswith("(") and raw.endswith(")"):
        raw = "-" + raw[1:-1]
    cents = Decimal(raw) * 100
    if cents != cents.to_integral_value():
        raise InvalidOperation(f"sub-cent amount: {s!r}")
    return int(cents)


def _report_date(row: list[str]) -> dt.date | None:
    """The report's as-of date: a row that is nothing but a date in the value
    column, above the tree."""
    if len(row) < 3 or row[0].strip() or row[1].strip():
        return None
    try:
        return dt.datetime.strptime(row[2].strip(), _DATE_FORMAT).date()
    except ValueError:
        return None


def read_account_tree(path: Path) -> Snapshot:
    """Leaf account balances of a "Net Worth" or "Year End" report.

    The export is an indented category tree (``,"Checking",``, ``," - Business","304.17"``,
    ``," - Total Checking","67,405.09"``, ...). Only rows for individual accounts are
    kept; category headers, "Total ..." subtotal rows, and the final "Net Worth"
    line are skipped.
    """
    holdings: list[Holding] = []
    as_of: dt.date | None = None
    for row in read_rows(path):
        if len(row) < 3:
            continue
        if as_of is None and (found := _report_date(row)) is not None:
            as_of = found
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
        holdings.append(Holding(account=name, value_cents=cents))
    return Snapshot(source=NAME, holdings=tuple(holdings), as_of=as_of)


def read_transactions(path: Path) -> Flows:
    """Transaction rows of a Transaction Export.

    Account headers, separator rows, and account totals are ignored. The
    frame keeps Quicken's other fields beside the common ones.
    """
    rows: list[dict[str, object]] = []
    current_account = ""
    header: list[str] | None = None

    for source_row, row in enumerate(read_rows(path), start=1):
        if not row:
            continue
        if row[0].strip().casefold() == "account":
            header = [column.strip() for column in row]
            continue
        if header is None:
            continue

        values = row + [""] * (len(header) - len(row))
        first = values[0].strip()
        if first:
            if first.casefold().startswith("total") or first.startswith("-"):
                continue
            current_account = first
            continue

        record = dict(zip(header, values, strict=False))
        date_raw = str(record.get("Date", "")).strip()
        if not date_raw:
            continue
        try:
            transaction_date = pd.to_datetime(date_raw, format=_DATE_FORMAT)
        except (TypeError, ValueError):
            continue

        amount_raw = str(record.get("Amount", "")).strip().replace(",", "")
        if not amount_raw:
            continue
        try:
            amount_cents = _dollars_to_cents(amount_raw)
        except InvalidOperation:
            raise ValueError(
                f"{path} row {source_row}: {amount_raw!r} is not an amount") from None
        rows.append(
            {
                "source_row": source_row,
                "account": current_account,
                "date": transaction_date,
                "entered": str(record.get("Entered", "")).strip(),
                "posted": str(record.get("Posted", "")).strip(),
                "action": str(record.get("Action", "")).strip(),
                "payee": str(record.get("Payee", "")).strip(),
                "category": str(record.get("Category", "")).strip(),
                "tags": str(record.get("Tags", "")).strip(),
                "transfer": str(record.get("Transfer", "")).strip(),
                "memo": str(record.get("Memo/Notes", "")).strip(),
                "amount_cents": amount_cents,
            }
        )

    columns = [
        "source_row", "account", "date", "entered", "posted", "action",
        "payee", "category", "tags", "transfer", "memo", "amount_cents",
    ]
    frame = pd.DataFrame(rows, columns=columns)
    # An export with no transactions must still have a date column of dates:
    # an empty frame's columns are objects, and so is anything joined to one.
    frame["date"] = pd.to_datetime(frame["date"])
    frame["amount_cents"] = frame["amount_cents"].astype("int64")
    frame["source_row"] = frame["source_row"].astype("int64")
    return Flows(source=NAME, transactions=frame)
