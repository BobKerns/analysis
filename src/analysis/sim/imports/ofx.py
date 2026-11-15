"""OFX and QFX files (issue #618): what a bank, a card or a brokerage
offers as "Quicken" or "Money" downloads.

One file can feed both imports. Its bank and card statements give
transactions, each with the institution's own id, so overlapping downloads
drop their duplicates exactly; every statement gives a balance, which a
bank's CSV never has; an investment statement gives its positions by
symbol.

OFX 1 is SGML with unclosed tags and OFX 2 is XML. ``ofxtools`` reads
both into one element tree, and this module reads that tree by tag and not
through the library's typed models: those refuse a file over any field out
of the specification, and institutions' files have such fields. Nothing
else here knows the library, so it can be replaced.

Not read: investment transactions (buys, dividends, reinvestments), which
are investment income and not spending, and an investment account's cash
balance, which a brokerage usually also lists as a position in its sweep
fund.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree.ElementTree import Element

import pandas as pd

from analysis.sim.imports.forms import FLOW_COLUMNS, REFERENCE, Flows, Holding, Snapshot

__all__ = ["NAME", "is_ofx", "read_holdings", "read_transactions"]

NAME = "ofx"

# An OFX 1 file's first header line, an OFX 2 file's processing
# instruction, or the body's own tag where a file has no header at all.
_SIGNS = (b"OFXHEADER", b"<?OFX", b"<OFX>")
# Statements that have transactions of spending: a bank account's, a card's.
_SPENDING = ("STMTRS", "CCSTMTRS")
_INVESTMENT = "INVSTMTRS"


def is_ofx(path: Path) -> bool:
    """Whether ``path`` is an OFX file, by its content: a download may be
    named .ofx, .qfx or .qbo, or anything."""
    try:
        with Path(path).open("rb") as fh:
            head = fh.read(2048).upper()
    except OSError:
        return False
    return any(sign in head for sign in _SIGNS)


def _tree(path: Path) -> Element:
    from ofxtools.Parser import OFXTree

    parser = OFXTree()
    try:
        parser.parse(str(path))
    except Exception as error:  # noqa: BLE001  the library raises several kinds of its own
        raise ValueError(f"{path}: not an OFX file that can be read: {error}") from None
    root = parser.getroot()
    if root is None:
        raise ValueError(f"{path}: not an OFX file that can be read")
    return root


def _text(element: Element | None, tag: str) -> str:
    """The text of ``element``'s first ``tag`` at any depth; blank without."""
    found = None if element is None else element.find(f".//{tag}")
    return (found.text or "").strip() if found is not None else ""


def _cents(path: Path, text: str, what: str) -> int:
    # Some institutions write a decimal comma, which the specification allows.
    try:
        return int((Decimal(text.replace(",", ".")) * 100).to_integral_value())
    except InvalidOperation:
        raise ValueError(f"{path}: {what} {text!r} is not an amount") from None


def _day(path: Path, text: str, what: str) -> dt.date:
    """The day of an OFX date-time (``YYYYMMDD``, then an optional time and
    zone): the day as the institution wrote it, in its own zone, and not
    the day that instant is elsewhere."""
    if not re.match(r"\d{8}", text):
        raise ValueError(f"{path}: {what} {text!r} is not a date")
    try:
        return dt.date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        raise ValueError(f"{path}: {what} {text!r} is not a date") from None


def _account(root: Element, statement: Element) -> str:
    """How a statement's account is named: the institution, where the file
    says, and the last four digits of the account's number. The whole
    number stays out of the mapping files."""
    number = _text(statement.find(".//BANKACCTFROM"), "ACCTID") \
        or _text(statement.find(".//CCACCTFROM"), "ACCTID") \
        or _text(statement.find(".//INVACCTFROM"), "ACCTID")
    institution = _text(root.find(".//SONRS/FI"), "ORG") or _text(statement.find(".//INVACCTFROM"), "BROKERID")
    last = number[-4:] if number else "?"
    return f"{institution} ...{last}" if institution else f"...{last}"


def _statements(root: Element, *tags: str) -> list[Element]:
    return [each for tag in tags for each in root.iter(tag)]


def read_transactions(path: Path) -> Flows:
    """The transactions of ``path``'s bank and card statements. They have
    no categories (a payee map gives them), and each has its ``reference``,
    the institution's id for it."""
    root = _tree(path)
    rows: list[dict[str, object]] = []
    for statement in _statements(root, *_SPENDING):
        account = _account(root, statement)
        for number, each in enumerate(statement.iter("STMTTRN"), start=1):
            # A payee is NAME, or a PAYEE aggregate with its own NAME.
            payee = _text(each, "NAME")
            check = _text(each, "CHECKNUM")
            rows.append({
                "source_row": number,
                "account": account,
                "date": pd.Timestamp(_day(path, _text(each, "DTPOSTED"), "a transaction's DTPOSTED")),
                "payee": payee or (f"CHECK {check}" if check else ""),
                "category": "",
                "transfer": "",
                "memo": _text(each, "MEMO"),
                "amount_cents": _cents(path, _text(each, "TRNAMT"), "a transaction's TRNAMT"),
                REFERENCE: _text(each, "FITID"),
            })
    frame = pd.DataFrame(rows, columns=[*FLOW_COLUMNS, REFERENCE])
    # As Quicken's reader: an empty frame's columns must still have their types.
    frame["date"] = pd.to_datetime(frame["date"])
    frame["amount_cents"] = frame["amount_cents"].astype("int64")
    return Flows(source=NAME, transactions=frame)


def _tickers(root: Element) -> dict[str, tuple[str, str]]:
    """Each security's symbol and name, by the id positions name it by."""
    found: dict[str, tuple[str, str]] = {}
    for info in root.iter("SECINFO"):
        if unique := _text(info, "UNIQUEID"):
            found[unique] = (_text(info, "TICKER"), _text(info, "SECNAME"))
    return found


def read_holdings(path: Path) -> Snapshot:
    """What ``path``'s statements say each account holds: a bank account's
    or a card's ledger balance (a card's is negative, a debt), and each
    position of an investment account, by symbol."""
    root = _tree(path)
    holdings: list[Holding] = []
    days: set[dt.date] = set()
    for statement in _statements(root, *_SPENDING):
        ledger = statement.find(".//LEDGERBAL")
        if ledger is None:
            continue
        if stamp := _text(ledger, "DTASOF"):
            days.add(_day(path, stamp, "a balance's DTASOF"))
        kind = _text(statement, "ACCTTYPE") or ("CREDITCARD" if statement.tag == "CCSTMTRS" else "")
        holdings.append(Holding(
            _account(root, statement), _cents(path, _text(ledger, "BALAMT"), "a balance's BALAMT"),
            hints={"type": kind.lower()} if kind else {}))
    tickers = _tickers(root)
    for statement in _statements(root, _INVESTMENT):
        account = _account(root, statement)
        if stamp := _text(statement, "DTASOF"):
            days.add(_day(path, stamp, "an investment statement's DTASOF"))
        for position in statement.iter("INVPOS"):
            unique = _text(position, "UNIQUEID")
            symbol, name = tickers.get(unique, ("", ""))
            units = _text(position, "UNITS")
            try:
                quantity = Decimal(units.replace(",", ".")) if units else None
            except InvalidOperation:
                raise ValueError(f"{path}: a position's UNITS {units!r} is not a number") from None
            holdings.append(Holding(
                account, _cents(path, _text(position, "MKTVAL"), "a position's MKTVAL"),
                # A security the file gives no symbol is named by its id (a CUSIP).
                asset=symbol or unique, quantity=quantity,
                hints={key: value for key, value in (("name", name), ("type", "investment")) if value}))
    if len(days) > 1:
        raise ValueError(
            f"{path}: its balances are as of different days ({', '.join(sorted(map(str, days)))}); "
            "download each account by itself")
    return Snapshot(source=NAME, holdings=tuple(holdings), as_of=next(iter(days), None))
