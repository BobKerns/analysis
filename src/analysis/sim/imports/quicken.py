"""Quicken's own report layouts: the first source (issue #527).

The "Net Worth Export" and "Year End Export" reports (My Reports) share one
indented-category CSV shape, read as holdings. The "Transaction Export" is
grouped under account header rows, read as flows. A budget's export (issue
#617), in its "Summary" form or its "Full" one with a column for each month,
is an indented tree of categories, read as a budget. None is a flat table,
so a column profile can't describe them.
"""

from __future__ import annotations

import csv
import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd

from analysis.sim.imports.forms import Budget, BudgetLine, Flows, Holding, Snapshot, read_rows

__all__ = [
    "NAME", "is_account_tree", "is_budget", "is_transaction_export", "read_account_tree",
    "read_budget", "read_transactions",
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


# ---------- a budget's export ----------

_BUDGETED = "budgeted"
_MARK = chr(0xFEFF)
_SECTIONS = {"income": True, "expenses": False}
_PERIOD = re.compile(r"(\d{4})(?:\s*-\s*\d{4})?\s*:\s*([A-Za-z]+)\s+to\s+([A-Za-z]+)")
_MONTHS = {dt.date(2000, month, 1).strftime("%B").casefold(): month for month in range(1, 13)}
_LEVEL = re.compile(r"\s*-\s+")


@dataclass
class _BudgetRow:
    """A category's row as the export has it: everything under it counted in."""

    income: bool
    path: tuple[str, ...]
    amounts: list[int]
    children: list[_BudgetRow] = field(default_factory=list)


def _budget_rows(path: Path, limit: int | None = None) -> list[list[str]]:
    """The file's rows without the byte-order mark that opens each line of
    a budget's export, not just the first."""
    return [[cell.replace(_MARK, "") for cell in row] for row in read_rows(path, limit)]


def _budgeted_columns(rows: list[list[str]]) -> tuple[int, list[int]] | None:
    """The index of the header row (``Actual, Budgeted, Difference``, once
    or for each month) and the columns that are budgeted amounts."""
    for index, row in enumerate(rows):
        found = [at for at, cell in enumerate(row) if cell.strip().casefold() == _BUDGETED]
        if found:
            return index, found
    return None


def is_budget(path: Path) -> bool:
    """Whether ``path`` looks like a budget's export: a header row with
    ``Budgeted`` near the top, and an ``INCOME`` or ``EXPENSES`` section."""
    try:
        rows = _budget_rows(path, 60)
    except (OSError, UnicodeDecodeError, csv.Error):
        return False
    return _budgeted_columns(rows[:20]) is not None and any(
        row and row[0].strip().casefold() in _SECTIONS for row in rows)


def _budget_months(rows: list[list[str]], header: int, columns: list[int]) -> tuple[dt.date, ...]:
    """The month of each budgeted column of a Full export: the rows above
    the header name each month over its three columns, and the year over
    the first month it has. A Summary export's one column has none."""
    if len(columns) == 1 or header < 2:
        return ()
    names, years = rows[header - 1], rows[header - 2]
    months: list[dt.date] = []
    year: int | None = None
    for column in columns:
        # A month's name, and a year, sit over the first of their columns.
        over = max((at for at in range(column + 1) if at < len(names) and names[at].strip()), default=None)
        said = max((at for at in range(column + 1) if at < len(years) and years[at].strip()), default=None)
        month = None if over is None else _MONTHS.get(names[over].strip().casefold())
        if month is None or said is None or not years[said].strip().isdigit():
            raise ValueError("the rows over the Budgeted columns don't name each one's month and year")
        year = int(years[said].strip())
        months.append(dt.date(year, month, 1))
    return tuple(months)


def _period_months(rows: list[list[str]]) -> tuple[dt.date, ...]:
    """A Summary export's months, from its period line (``2026 - 2027:  JUNE
    to MAY``): the first year is the first month's."""
    for row in rows[:12]:
        found = _PERIOD.search(row[0]) if row else None
        first = None if found is None else _MONTHS.get(found.group(2).casefold())
        last = None if found is None else _MONTHS.get(found.group(3).casefold())
        if found is None or first is None or last is None:
            continue
        count = (last - first) % 12 + 1
        start = int(found.group(1)) * 12 + first - 1
        return tuple(dt.date((start + n) // 12, (start + n) % 12 + 1, 1) for n in range(count))
    return ()


def read_budget(path: Path) -> Budget:
    """The categories of a budget's export, each with its own budgeted
    amount for the period, and month by month from the Full form.

    A category's row counts everything under it too, so its own amount is
    the row less the rows one level down. The export is in whole dollars,
    each row rounded by itself: a remainder no bigger than that rounding
    explains is nothing. A category with no amount of its own in any month
    is left out, as are the summary rows.
    """
    rows = _budget_rows(path)
    found = _budgeted_columns(rows[:20])
    if found is None:
        raise ValueError(f"{path}: no header row with a Budgeted column")
    header, columns = found
    try:
        months = _budget_months(rows, header, columns) or _period_months(rows)
    except ValueError as error:
        raise ValueError(f"{path}: {error}") from None

    nodes: list[_BudgetRow] = []
    open_: list[_BudgetRow] = []
    income: bool | None = None
    for number, row in enumerate(rows[header + 1:], start=header + 2):
        section = row[0].strip().casefold() if row else ""
        if section:
            income = _SECTIONS.get(section)
            open_ = []
            continue
        if income is None or len(row) < 2 or not row[1].strip():
            continue
        label = row[1]
        level = 0
        while (step := _LEVEL.match(label)) is not None:
            label = label[step.end():]
            level += 1
        name = label.strip()
        if not name or level > len(open_):
            raise ValueError(f"{path} row {number}: {row[1]!r} is indented under no category")
        try:
            amounts = [
                _dollars_to_cents(row[column]) if column < len(row) and row[column].strip() else 0
                for column in columns]
        except InvalidOperation:
            raise ValueError(f"{path} row {number}: {name!r} has a budgeted amount that isn't money") from None
        del open_[level:]
        node = _BudgetRow(income, (*(open_[-1].path if open_ else ()), name), amounts)
        if open_:
            open_[-1].children.append(node)
        open_.append(node)
        nodes.append(node)

    lines: list[BudgetLine] = []
    for node in nodes:
        # Each row is rounded to the dollar by itself, so a row and its
        # parts can differ by half a dollar apiece.
        slack = 50 * (len(node.children) + 1)
        own = [amount - sum(child.amounts[at] for child in node.children)
               for at, amount in enumerate(node.amounts)]
        if node.children:
            own = [0 if abs(each) <= slack else each for each in own]
        if not any(own):
            continue
        # Income is budgeted as a positive number, and so is spending.
        lines.append(BudgetLine(
            income=node.income, category=":".join(node.path), total_cents=sum(own),
            monthly_cents=tuple(own) if len(columns) > 1 else ()))
    return Budget(source=NAME, lines=tuple(lines), months=months)
