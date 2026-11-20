"""Quicken's own report layouts: the first source (issue #527).

The "Net Worth Export" and "Year End Export" reports (My Reports) share one
indented-category CSV shape, read as holdings. The "Transaction Export" is
grouped under account header rows, read as flows; a transaction report
exported as it stands ("All Transactions") is one row for each, under a few
lines about the report, and is read the same. A budget's export (issue
#617), in its "Summary" form or its "Full" one with a column for each month,
is an indented tree of categories, read as a budget. The "Portfolio Value &
Cost Basis" report, which is all Quicken exports of what an investment
account holds, has a set of columns for each of its dates and no account.
None is a flat table, so a column profile can't describe them.
"""

from __future__ import annotations

import csv
import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd

from zabob.retirement.sim.imports.forms import (
    CASH, Budget, BudgetLine, Flows, Holding, Snapshot, format_dollars, read_rows,
)

__all__ = [
    "NAME", "is_account_tree", "is_budget", "is_portfolio", "is_transaction_export", "read_account_tree",
    "read_budget", "read_portfolio", "read_transactions",
]

NAME = "quicken"
_DATE_FORMAT = "%Y-%m-%d"
_FLAT_DATE_FORMAT = "%m/%d/%Y"
_TRANSFER = "transfer:"
_TRANSACTION_COLUMNS = {"Date", "Payee", "Category", "Amount"}


def _is_transaction_header(row: list[str]) -> bool:
    return bool(row) and row[0].strip().casefold() == "account" \
        and _TRANSACTION_COLUMNS <= {cell.strip() for cell in row}


def _is_flat_header(row: list[str]) -> bool:
    """The header of a transaction report exported as it stands (not the
    saved Transaction Export): one row for each transaction, with its
    account in a column of its own, under a few lines saying what the
    report is. Its first column has no name (it holds only the totals'
    labels), which is what tells it from a bank's table with these
    columns, a household profile's to read."""
    cells = {cell.strip() for cell in row}
    return bool(row) and not row[0].strip() \
        and {"Date", "Amount", "Account", "Category"} <= cells and bool(cells & {"Payee", "Payee/Security"})


def is_transaction_export(path: Path) -> bool:
    """Whether ``path`` looks like a transaction report: an ``Account``
    header row with the transaction columns, or a flat report's header,
    near the top."""
    try:
        return any(_is_transaction_header(row) or _is_flat_header(row) for row in read_rows(path, 30))
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
    if any(_is_transaction_header(row) or _is_flat_header(row) or _portfolio_columns(row) for row in rows):
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


# ---------- the Portfolio Value & Cost Basis report ----------

_DATED_COLUMN = re.compile(r"(\d{1,2}/\d{1,2}/\d{2,4})\s+(.+)")
_VALUE, _BASIS = "market value", "cost basis"
_NONE = {"", "--"}
_ACCOUNTS_SAID = re.compile(r"(all|\d+) accounts?\b", re.IGNORECASE)
_PORTFOLIO_TITLE = "Portfolio Value & Cost Basis"


def _portfolio_columns(row: list[str]) -> dict[dt.date, dict[str, int]]:
    """By date, the column of each number a portfolio report's header row
    names (``12/31/25 Market Value``); empty for any other row."""
    cells = [cell.strip() for cell in row]
    if len(cells) < 4 or cells[0].casefold() != "security" or "Symbol" not in cells:
        return {}
    found: dict[dt.date, dict[str, int]] = {}
    for at, cell in enumerate(cells):
        if (dated := _DATED_COLUMN.fullmatch(cell)) is None:
            continue
        day, month_first = dated.group(1), dated.group(1).rsplit("/", 1)[1]
        try:
            date = dt.datetime.strptime(day, "%m/%d/%Y" if len(month_first) == 4 else "%m/%d/%y").date()
        except ValueError:
            continue
        found.setdefault(date, {})[dated.group(2).strip().casefold()] = at
    return {date: columns for date, columns in found.items() if _VALUE in columns}


def is_portfolio(path: Path) -> bool:
    """Whether ``path`` looks like a "Portfolio Value & Cost Basis" report:
    a ``Security`` header row with a dated ``Market Value`` column, near
    the top."""
    try:
        return any(_portfolio_columns(row) for row in read_rows(path, 30))
    except (OSError, UnicodeDecodeError, csv.Error):
        return False


def _lot_day(row: list[str]) -> dt.date | None:
    """The acquisition date of a tax lot's row, under its security's: a row
    with no name and a date in the next cell. A security's lots are in the
    export only where its line was opened in the report."""
    if len(row) < 2 or row[0].strip():
        return None
    try:
        return dt.datetime.strptime(row[1].strip(), "%m/%d/%Y").date()
    except ValueError:
        return None


def read_portfolio(path: Path, account: str) -> Snapshot:
    """What one account holds, from a "Portfolio Value & Cost Basis" report
    of that account alone: each security's value and cost basis, and the
    account's cash.

    The report names no account, and one of several adds them together, so
    ``account`` says whose it is; the name the report was saved under, which
    its first line has, is in each holding's ``report`` hint as a guess at
    it. It has a set of columns for each of its dates: the snapshot is as
    of the last, with every date's in ``dated``. A security with no value
    on a date, or none left, isn't held then.

    Where a security's line was opened in the report, its tax lots follow
    it, one row for each acquisition date: each is a holding of its own
    with that date, and the security's own row is only their total. What
    of the total the lots don't account for is a holding with no date.
    """
    rows = read_rows(path)
    header = next((at for at, row in enumerate(rows[:30]) if _portfolio_columns(row)), None)
    if header is None:
        raise ValueError(f"{path}: no header row with a dated Market Value column")
    columns = _portfolio_columns(rows[header])
    symbol_at = [cell.strip() for cell in rows[header]].index("Symbol")
    title = rows[0][0].strip() if rows and rows[0] else ""
    saved_as = title[len(_PORTFOLIO_TITLE):].strip() if title.casefold().startswith(_PORTFOLIO_TITLE.casefold()) \
        else ""

    def amounts(row: list[str], number: int, date: dt.date) -> tuple[int | None, int | None]:
        def cents(at: int | None, what: str) -> int | None:
            text = row[at].strip() if at is not None and at < len(row) else ""
            if text in _NONE:
                return None
            try:
                return _dollars_to_cents(text)
            except InvalidOperation:
                raise ValueError(f"{path} row {number}: {text!r} is not a {what}") from None

        its = columns[date]
        return cents(its[_VALUE], "market value"), cents(its.get(_BASIS), "cost basis")

    # Each security's own row, and its tax lots' rows by acquisition date
    # (two bought on one day are one lot here).
    securities: list[tuple[str, str, int, list[str], dict[dt.date, list[tuple[int, list[str]]]]]] = []
    for number, row in enumerate(rows[header + 1:], start=header + 2):
        name = row[0].strip() if row else ""
        if (acquired := _lot_day(row)) is not None and securities:
            securities[-1][4].setdefault(acquired, []).append((number, row))
        elif name and name.strip("-") and name.casefold() != "total":
            securities.append((name, row[symbol_at].strip() if symbol_at < len(row) else "", number, row, {}))

    dated: dict[dt.date, list[Holding]] = {date: [] for date in columns}
    notes: list[str] = []
    # What is said of one date's numbers: of those of the date imported.
    of_date: dict[dt.date, list[str]] = {date: [] for date in columns}
    for name, symbol, number, row, lots in securities:
        cash = name.casefold() == "cash"
        asset = CASH if cash else symbol or name
        hints = {**({} if cash else {"name": name}), **({"report": saved_as} if saved_as else {})}
        for date in columns:
            value, basis = amounts(row, number, date)
            left, left_basis = value or 0, basis
            for acquired, its_rows in lots.items():
                each = [amounts(lot_row, lot_number, date) for lot_number, lot_row in its_rows]
                held = sum(lot_value or 0 for lot_value, _ in each)
                if not held:
                    continue
                cost = None if any(lot_basis is None for lot_value, lot_basis in each if lot_value) \
                    else sum(lot_basis or 0 for lot_value, lot_basis in each if lot_value)
                dated[date].append(Holding(
                    account=account, value_cents=held, asset=asset, acquired=acquired,
                    cost_basis_cents=None if cash else cost, hints=hints))
                left -= held
                left_basis = None if left_basis is None or cost is None else left_basis - cost
            if not left:
                continue
            if lots and left != value:
                of_date[date].append(
                    f"{path.name}: the tax lots of {asset} come to {format_dollars((value or 0) - left)} of its "
                    f"{format_dollars(value or 0)} as of {date}; the rest is taken as one holding with no date")
            dated[date].append(Holding(
                account=account, value_cents=left, asset=asset,
                cost_basis_cents=None if cash else left_basis, hints=hints))
    if (said := _accounts_said(rows[:header])) is not None:
        notes.append(
            f"{path.name} says it is of {said}: its numbers are those accounts' added together, "
            f"and all go to {account}. Export the report for one account at a time")
    last = max(columns)
    several = len(dated) > 1
    if not several:
        notes += of_date[last]
    return Snapshot(
        source=NAME, holdings=tuple(dated[last]), as_of=last, notes=tuple(notes),
        complete=frozenset({account}),
        dated={date: tuple(held) for date, held in dated.items()} if several else {},
        dated_notes={date: tuple(said) for date, said in of_date.items() if said} if several else {})


def _accounts_said(above: list[list[str]]) -> str | None:
    """How a portfolio report's lines over its header describe its accounts
    when they are more than one (``All Accounts``, ``3 Accounts``)."""
    for row in above:
        found = _ACCOUNTS_SAID.match(row[0].strip()) if row else None
        if found is not None and found.group(1).casefold() not in ("1", "one"):
            return found.group(0).casefold()
    return None


def read_transactions(path: Path) -> Flows:
    """Transaction rows of a Transaction Export, or of a transaction
    report exported as it stands.

    Account headers, separator rows, and account totals are ignored, and so
    are the lines over a flat report's header. The frame keeps Quicken's
    other fields beside the common ones.
    """
    rows: list[dict[str, object]] = []
    current_account = ""
    header: list[str] | None = None
    flat = False

    for source_row, row in enumerate(read_rows(path), start=1):
        if not row:
            continue
        if header is None and _is_flat_header(row):
            header, flat = [column.strip() for column in row], True
            continue
        if not flat and row[0].strip().casefold() == "account":
            header = [column.strip() for column in row]
            continue
        if header is None:
            continue

        values = row + [""] * (len(header) - len(row))
        first = values[0].strip()
        if first and flat:
            continue                    # a total, or a line about the report
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
            transaction_date = pd.to_datetime(date_raw, format=_FLAT_DATE_FORMAT if flat else _DATE_FORMAT)
        except (TypeError, ValueError):
            continue
        category = str(record.get("Category", "")).strip()
        transfer = str(record.get("Transfer", "")).strip()
        if flat:
            current_account = str(record.get("Account", "")).strip()
            # A flat report has no Transfer column: a move between one's own
            # accounts is in the category, as Transfer:[the other account].
            if category.casefold().startswith(_TRANSFER):
                transfer = category[len(_TRANSFER):].strip().strip("[]") or category

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
                "payee": str(record.get("Payee", record.get("Payee/Security", ""))).strip(),
                "category": category,
                "tags": str(record.get("Tags", "")).strip(),
                "transfer": transfer,
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
