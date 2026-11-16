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
are investment income and not spending.
"""

from __future__ import annotations

import codecs
import datetime as dt
import io
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from xml.etree.ElementTree import Element
from xml.sax.saxutils import unescape

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
# What the library leaves in a value's text: its typed models, not used
# here, are where it turns these back into the characters.
_ENTITIES = {"&nbsp;": " ", "&apos;": "'", "&quot;": '"'}


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
    """``path`` as an element tree. The library reads a file as its header
    says to; one it refuses is read again from its ``<OFX>`` tag on, for
    what institutions get wrong about the wrapping: no header at all, or a
    payee in the bank's own code page under a header that says UTF-8 or
    ASCII."""
    from ofxtools.Parser import OFXTree, TreeBuilder

    try:
        # A byte-order mark is no part of the header's first line.
        raw = Path(path).read_bytes().removeprefix(codecs.BOM_UTF8)
    except OSError as error:
        raise ValueError(f"{path}: not an OFX file that can be read: {error}") from None
    root: Element | None
    try:
        parser = OFXTree()
        parser.parse(io.BytesIO(raw))
        root = parser.getroot()
    except Exception as error:  # noqa: BLE001  the library raises several kinds of its own
        start = raw.upper().find(b"<OFX>")
        refused = ValueError(f"{path}: not an OFX file that can be read: {error}")
        if start < 0:
            raise refused from None
        try:
            body = raw[start:].decode("utf-8")
        except UnicodeDecodeError:
            # Every byte is some character in Latin-1, which cp1252 is but for a few.
            body = raw[start:].decode("cp1252", errors="replace")
        try:
            builder = TreeBuilder()
            builder.feed(body)
            root = builder.close()
        except Exception:  # noqa: BLE001  as above; the first refusal says more
            raise refused from None
    if root is None or not any(True for _ in _statements(root, *_SPENDING, _INVESTMENT)):
        raise ValueError(f"{path}: not an OFX file that can be read: it has no account's statement")
    return root


def _text(element: Element | None, tag: str) -> str:
    """The text of ``element``'s first ``tag`` at any depth; blank without.
    ``&amp;`` and the like are the characters they stand for, so a payee
    is the same text whichever institution's file names it."""
    found = None if element is None else element.find(f".//{tag}")
    return unescape((found.text or "").strip(), _ENTITIES).strip() if found is not None else ""


def _number(text: str, *, money: bool) -> Decimal:
    """An OFX number. The specification allows a decimal comma and no
    thousands separators, and files have both, so of two kinds of separator
    the last is the decimal point and the other is dropped, and one that
    repeats separates thousands.

    A lone comma before exactly three digits could be either: ``5,000`` is
    five thousand or five. For ``money`` it separates thousands, as the
    other sources read it, since money has two decimals and not three. A
    count of shares often has three, so there it is refused and not
    guessed: the two readings differ a thousand times."""
    plain = text.strip().replace(" ", "")
    comma, point = plain.rfind(","), plain.rfind(".")
    decimal = "," if comma > point else "."
    if plain.count(decimal) > 1:
        decimal = ""
    elif decimal == "," and point < 0 and len(plain) - comma == 4:
        if not money:
            raise _Ambiguous(text)
        decimal = ""
    whole = "".join(c for c in plain if c not in ",." or c == decimal)
    number = Decimal(whole.replace(",", "."))
    if not number.is_finite():
        raise InvalidOperation(text)
    return number


class _Ambiguous(InvalidOperation):
    """A number that reads two ways."""


def _cents(path: Path, text: str, what: str, *, exact: bool = True) -> int:
    """``text`` as cents. A fraction of a cent is refused, as Quicken's
    reader refuses one, except where ``exact`` is off: a position's value
    is shares times a price, which some brokerages don't round."""
    try:
        cents = _number(text, money=True) * 100
    except InvalidOperation:
        raise ValueError(f"{path}: {what} {text!r} is not an amount") from None
    if exact and cents != cents.to_integral_value():
        raise ValueError(f"{path}: {what} {text!r} has a fraction of a cent")
    return int(cents.to_integral_value(ROUND_HALF_UP))


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


def _kind(statement: Element) -> str:
    """The account's type as the file says it (``CHECKING``, ``SAVINGS``),
    ``CREDITCARD`` for a card's statement, blank for an investment's. Only
    the statement's own account is asked: a transfer's other side
    (``BANKACCTTO``) has a type too."""
    return _text(statement.find(".//BANKACCTFROM"), "ACCTTYPE").upper() \
        or ("CREDITCARD" if statement.tag == "CCSTMTRS" else "")


def _account(root: Element, statement: Element) -> str:
    """How a statement's account is named: the institution, a bank
    account's type, and the last four digits of the account's number. The
    whole number stays out of the mapping files.

    The name has to be the same in every download of the account, since
    the mappings and the dropping of duplicates go by it, so it is made
    from the statement alone. A file that doesn't name its institution
    (many banks') gives its id or the bank's routing number instead, so two
    banks' accounts ending alike stay two."""
    own = statement.find(".//BANKACCTFROM")
    number = _number_of(statement)
    signed_on = root.find(".//SONRS/FI")
    institution = _text(signed_on, "ORG") or _text(signed_on, "FID") or _text(own, "BANKID") \
        or _text(statement.find(".//INVACCTFROM"), "BROKERID")
    kind = _text(own, "ACCTTYPE").lower()
    return " ".join(part for part in (institution, kind, f"...{number[-4:] if number else '?'}") if part)


def _number_of(statement: Element) -> str:
    return _text(statement.find(".//BANKACCTFROM"), "ACCTID") \
        or _text(statement.find(".//CCACCTFROM"), "ACCTID") \
        or _text(statement.find(".//INVACCTFROM"), "ACCTID")


def _accounts(path: Path, root: Element, statements: list[Element]) -> list[str]:
    """Each statement's account name. Two accounts of one file that would
    get one name (one institution, one type, the same last four digits)
    are refused: read as one, each would take the other's mapping and
    lose transactions as the other's duplicates."""
    names = [_account(root, each) for each in statements]
    numbers: dict[str, str] = {}
    for name, statement in zip(names, statements, strict=True):
        if numbers.setdefault(name, _number_of(statement)) != _number_of(statement):
            raise ValueError(
                f"{path}: two of its accounts would both be {name!r}, which can't be told apart "
                "by the institution, the type and the last four digits; they can't be read from one file")
    return names


def _statements(root: Element, *tags: str) -> list[Element]:
    """The statements of ``tags``, in the file's order."""
    return [each for each in root.iter() if each.tag in tags]


def read_transactions(path: Path) -> Flows:
    """The transactions of ``path``'s bank and card statements. They have
    no categories (a payee map gives them), and each has its ``reference``,
    the institution's id for it.

    A correction (``CORRECTFITID``) takes the place of the transaction it
    names, or with ``CORRECTACTION`` of ``DELETE`` only removes it, where
    that transaction is in this file; a deletion is never a transaction
    itself."""
    root = _tree(path)
    rows: list[dict[str, object]] = []
    # The transactions corrections name, and each correction's own place:
    # one that names itself is not its own original.
    corrected: set[tuple[str, str]] = set()
    corrections: set[int] = set()
    statements = _statements(root, *_SPENDING)
    # The file has no lines to count by: a transaction's place in it.
    place = 0
    for statement, account in zip(statements, _accounts(path, root, statements), strict=True):
        for each in statement.iter("STMTTRN"):
            place += 1
            if corrects := _text(each, "CORRECTFITID"):
                corrected.add((account, corrects))
                corrections.add(place)
                if _text(each, "CORRECTACTION").upper() == "DELETE":
                    continue
            # A payee is NAME, or a PAYEE aggregate with its own NAME.
            payee = _text(each, "NAME")
            check = _text(each, "CHECKNUM")
            rows.append({
                "source_row": place,
                "account": account,
                "date": pd.Timestamp(_day(path, _text(each, "DTPOSTED"), "a transaction's DTPOSTED")),
                "payee": payee or (f"CHECK {check}" if check else ""),
                "category": "",
                "transfer": "",
                "memo": _text(each, "MEMO"),
                "amount_cents": _cents(path, _text(each, "TRNAMT"), "a transaction's TRNAMT"),
                REFERENCE: _text(each, "FITID"),
            })
    rows = [row for row in rows
            if row["source_row"] in corrections or (row["account"], row[REFERENCE]) not in corrected]
    frame = pd.DataFrame(rows, columns=[*FLOW_COLUMNS, REFERENCE])
    # As Quicken's reader: an empty frame's columns must still have their types.
    frame["date"] = pd.to_datetime(frame["date"])
    frame["amount_cents"] = frame["amount_cents"].astype("int64")
    frame["source_row"] = frame["source_row"].astype("int64")
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
    or a card's ledger balance (what a card owes is negative, though some
    issuers write it positive; the import stores a liability's size either
    way and says so of a positive one), and each position of an investment
    account, by symbol, with its cash that no position holds."""
    root = _tree(path)
    holdings: list[Holding] = []
    days: set[dt.date] = set()
    spending = _statements(root, *_SPENDING)
    investing = _statements(root, _INVESTMENT)
    names = _accounts(path, root, [*spending, *investing])
    for statement, account in zip(spending, names[:len(spending)], strict=True):
        ledger = statement.find(".//LEDGERBAL")
        if ledger is None:
            continue
        if stamp := _text(ledger, "DTASOF"):
            days.add(_day(path, stamp, "a balance's DTASOF"))
        kind = _kind(statement)
        holdings.append(Holding(
            account, _cents(path, _text(ledger, "BALAMT"), "a balance's BALAMT"),
            hints={"type": kind.lower()} if kind else {}))
    tickers = _tickers(root)
    for statement, account in zip(investing, names[len(spending):], strict=True):
        # What the positions priced at a dollar a share hold: a sweep or
        # money market fund's. Not those whose value equals their units: a
        # bond's or a CD's units are its face value, priced in percent.
        swept = 0
        if stamp := _text(statement, "DTASOF"):
            days.add(_day(path, stamp, "an investment statement's DTASOF"))
        for position in statement.iter("INVPOS"):
            unique = _text(position, "UNIQUEID")
            symbol, name = tickers.get(unique, ("", ""))
            units = _text(position, "UNITS")
            try:
                quantity = _number(units, money=False) if units else None
            except _Ambiguous:
                raise ValueError(
                    f"{path}: a position's UNITS {units!r} could be thousands or a decimal; "
                    "it is not guessed") from None
            except InvalidOperation:
                raise ValueError(f"{path}: a position's UNITS {units!r} is not a number") from None
            value = _cents(path, _text(position, "MKTVAL"), "a position's MKTVAL", exact=False)
            try:
                dollar = _number(_text(position, "UNITPRICE"), money=False) == 1
            except InvalidOperation:
                dollar = False
            if dollar:
                swept += value
            holdings.append(Holding(
                account, value,
                # A security the file gives no symbol is named by its id (a CUSIP).
                asset=symbol or unique, quantity=quantity,
                hints={hint: said for hint, said in (("name", name), ("type", "investment")) if said}))
        # AVAILCASH is all the account's cash, a sweep fund's included where
        # the brokerage lists one as a position; a core cash balance has no
        # position, and left out would understate the account. The cash
        # beyond what such positions hold is a holding with no symbol. It
        # may be a product of shares and a price, as a position's value is.
        balances = statement.find(".//INVBAL")
        available = _text(balances, "AVAILCASH")
        cash = _cents(path, available, "an investment statement's AVAILCASH", exact=False) if available else 0
        if cash > swept:
            holdings.append(Holding(account, cash - swept, hints={"name": "cash", "type": "investment"}))
        # Money borrowed on margin is less than no cash, or a margin
        # balance below zero where the cash doesn't show it. Left out, the
        # account would be worth what was bought with the loan as well.
        borrowed = _text(balances, "MARGINBALANCE")
        margin = _cents(path, borrowed, "an investment statement's MARGINBALANCE", exact=False) if borrowed else 0
        if min(cash, margin) < 0:
            holdings.append(Holding(account, min(cash, margin), hints={"name": "margin", "type": "investment"}))
    if len(days) > 1:
        raise ValueError(
            f"{path}: its balances are as of different days ({', '.join(sorted(map(str, days)))}); "
            "download each account by itself")
    return Snapshot(source=NAME, holdings=tuple(holdings), as_of=next(iter(days), None))
