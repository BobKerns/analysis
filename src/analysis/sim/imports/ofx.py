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
import html
import io
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from xml.etree.ElementTree import Element

import pandas as pd

from analysis.sim.imports.forms import CORRECTS, FLOW_COLUMNS, REFERENCE, REMOVES, Flows, Holding, Snapshot

__all__ = ["NAME", "is_ofx", "read_holdings", "read_transactions"]

NAME = "ofx"

# An OFX 1 file's first header line, an OFX 2 file's processing
# instruction, or the body's own tag where a file has no header at all.
_SIGNS = (b"OFXHEADER", b"<?OFX", b"<OFX>")
# Statements that have transactions of spending: a bank account's, a card's.
_SPENDING = ("STMTRS", "CCSTMTRS")
_INVESTMENT = "INVSTMTRS"
# The currency every amount is taken to be in: nothing is converted.
_DOLLARS = "USD"


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
    if root is None or not _statements(root, *_SPENDING, _INVESTMENT):
        raise ValueError(f"{path}: not an OFX file that can be read: it has no account's statement")
    return root


def _find(element: Element | None, tag: str) -> Element | None:
    """``element``'s first ``tag`` below it, but not one inside another
    element of ``element``'s own kind.

    Not its child only, and not any descendant: the library reads a value
    left empty (``<MEMO>`` with nothing after it, or ``<MEMO/>``) as the
    start of a group, so what follows it lands a level down, the next
    transaction inside this one. Looking at any depth would then read the
    next transaction's payee or correction as this one's."""
    if element is None:
        return None
    waiting = list(reversed(element))
    while waiting:
        each = waiting.pop()
        if each.tag == tag:
            return each
        if each.tag != element.tag:
            waiting.extend(reversed(each))
    return None


def _text(element: Element | None, tag: str) -> str:
    """The text of ``element``'s ``tag`` (as ``_find`` finds it); blank
    without. A character reference, named or numbered (``&amp;``,
    ``&#38;``), is the character it stands for, which the library leaves
    to its typed models: a payee is then one text whichever institution's
    file names it, and however it escapes it."""
    found = _find(element, tag)
    if found is None:
        return ""
    return html.unescape(found.text or "").replace("\N{NO-BREAK SPACE}", " ").strip()


# A whole number in groups of three, and what may follow its last group.
_GROUPED = r"[1-9]\d{{0,2}}(?:{0}\d{{3}})+"
_COMMAS = re.compile(rf"({_GROUPED.format(',')})(?:\.(\d*))?")
_POINTS = re.compile(rf"({_GROUPED.format(r'\.')})(?:,(\d*))?")
_PLAIN = re.compile(r"(\d*)(?:[.,](\d*))?")


def _number(text: str, *, money: bool) -> Decimal:
    """An OFX number. The specification allows a decimal comma and no
    thousands separators, and files have both. A separator is read as
    separating thousands only where every group after the first has three
    digits and the first has no leading zero (``1,234,567``, ``1.234,56``);
    a lone separator is otherwise the decimal point; anything else
    (``1,000,00``) is refused.

    One group after a lone comma could be either: ``5,000`` is five
    thousand or five. For ``money`` it separates thousands, as the other
    sources read it, since money has two decimals and not three. A count
    of shares often has three, so there it is refused and not guessed: the
    two readings differ a thousand times. A lone point is the decimal
    point (``1.234``), as everywhere."""
    plain = text.strip().replace(" ", "")
    sign = -1 if plain.startswith("-") else 1
    digits = plain[1:] if plain[:1] in "+-" else plain
    if found := _COMMAS.fullmatch(digits):
        whole, fraction = found.group(1).replace(",", ""), found.group(2)
        if fraction is None and found.group(1).count(",") == 1 and not money:
            raise _Ambiguous(text)
    elif (found := _POINTS.fullmatch(digits)) and (found.group(2) is not None or found.group(1).count(".") > 1):
        whole, fraction = found.group(1).replace(".", ""), found.group(2)
    elif (found := _PLAIN.fullmatch(digits)) and (found.group(1) or found.group(2)):
        whole, fraction = found.group(1), found.group(2)
    else:
        raise InvalidOperation(text)
    return sign * Decimal(f"{whole or '0'}.{fraction or '0'}")


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


def _own(statement: Element) -> Element | None:
    """The group naming the statement's own account."""
    for tag in ("BANKACCTFROM", "CCACCTFROM", "INVACCTFROM"):
        if (found := _find(statement, tag)) is not None:
            return found
    return None


def _kind(statement: Element) -> str:
    """The account's type as the file says it (``CHECKING``, ``SAVINGS``),
    ``CREDITCARD`` for a card's statement, blank for an investment's. Only
    the statement's own account is asked: a transfer's other side
    (``BANKACCTTO``) has a type too."""
    return _text(_find(statement, "BANKACCTFROM"), "ACCTTYPE").upper() \
        or ("CREDITCARD" if statement.tag == "CCSTMTRS" else "")


def _account(root: Element, statement: Element) -> str:
    """How a statement's account is named: the institution, a bank
    account's type, and the last four digits of the account's number. The
    whole number stays out of the mapping files.

    The mappings and the dropping of duplicates go by the name, so it
    should be the same in every download of the account. It is made from
    the file alone, which can't promise that: the institution is the
    file's sign-on block's name or id, and only without one the
    statement's routing number or broker, so a bank whose two kinds of
    download differ there, or a card reissued with a new number, is two
    names. ``sources.read_flows`` notes transactions that two names share."""
    own = _own(statement)
    number = _text(own, "ACCTID")
    signed_on = root.find(".//SONRS/FI")
    institution = _text(signed_on, "ORG") or _text(signed_on, "FID") or _text(own, "BANKID") \
        or _text(own, "BROKERID")
    kind = "" if statement.tag == "CCSTMTRS" else _kind(statement).lower()
    return " ".join(part for part in (institution, kind, f"...{number[-4:] if number else '?'}") if part)


def _accounts(path: Path, root: Element, statements: list[Element]) -> list[str]:
    """Each statement's account name. Refused: two accounts of one file
    that would get one name (one institution, one type, the same last four
    digits), since read as one each would take the other's mapping and
    lose transactions as the other's duplicates; and one account with two
    statements in the file, which would be counted twice."""
    names = [_account(root, each) for each in statements]
    numbers: dict[str, str] = {}
    for name, statement in zip(names, statements, strict=True):
        number = _text(_own(statement), "ACCTID")
        if name not in numbers:
            numbers[name] = number
        elif numbers[name] == number:
            raise ValueError(
                f"{path}: it has more than one statement of {name!r}, which would be counted twice")
        else:
            raise ValueError(
                f"{path}: two of its accounts would both be {name!r}, which can't be told apart "
                "by the institution, the type and the last four digits; they can't be read from one file")
    return names


def _money(cents: int) -> str:
    return f"${Decimal(cents) / 100:,.2f}"


def _foreign(account: str, statement: Element, items: str) -> list[str]:
    """Notes of what in ``statement`` is not in dollars: nothing is
    converted, and an amount in another currency is read as it is."""
    notes = []
    stated = _text(statement, "CURDEF").upper()
    if stated and stated != _DOLLARS:
        notes.append(f"{account} is in {stated}; its amounts are read as they are, not converted")
    symbols = {_text(_find(each, "CURRENCY"), "CURSYM").upper() for each in statement.iter(items)}
    others = sorted(symbols - {"", stated or _DOLLARS})
    if others:
        notes.append(f"{account} has amounts in {', '.join(others)}, read as they are and not converted")
    return notes


def _statements(root: Element, *tags: str) -> list[Element]:
    """The statements of ``tags``, in the file's order."""
    return [each for each in root.iter() if each.tag in tags]


def read_transactions(path: Path) -> Flows:
    """The transactions of ``path``'s bank and card statements. They have
    no categories (a payee map gives them), and each has its ``reference``,
    the institution's id for it.

    A correction is a row too, with the reference it corrects in
    ``CORRECTS`` and, where it only removes that transaction, ``REMOVES``
    set: what it corrects may be in an earlier download, so
    ``sources.read_flows`` applies corrections, over every file of a run."""
    root = _tree(path)
    rows: list[dict[str, object]] = []
    notes: list[str] = []
    statements = _statements(root, *_SPENDING)
    for statement, account in zip(statements, _accounts(path, root, statements), strict=True):
        notes += _foreign(account, statement, "STMTTRN")
        for each in statement.iter("STMTTRN"):
            # A payee is NAME, or a PAYEE aggregate with its own NAME.
            payee = _text(each, "NAME")
            check = _text(each, "CHECKNUM")
            rows.append({
                # The file has no lines to count by: the transaction's place in it.
                "source_row": len(rows) + 1,
                "account": account,
                "date": pd.Timestamp(_day(path, _text(each, "DTPOSTED"), "a transaction's DTPOSTED")),
                "payee": payee or (f"CHECK {check}" if check else ""),
                "category": "",
                "transfer": "",
                "memo": _text(each, "MEMO"),
                "amount_cents": _cents(path, _text(each, "TRNAMT"), "a transaction's TRNAMT"),
                REFERENCE: _text(each, "FITID"),
                CORRECTS: _text(each, "CORRECTFITID"),
                REMOVES: _text(each, "CORRECTACTION").upper() == "DELETE",
            })
    frame = pd.DataFrame(rows, columns=[*FLOW_COLUMNS, REFERENCE, CORRECTS, REMOVES])
    # As Quicken's reader: an empty frame's columns must still have their types.
    frame["date"] = pd.to_datetime(frame["date"])
    frame["amount_cents"] = frame["amount_cents"].astype("int64")
    frame["source_row"] = frame["source_row"].astype("int64")
    frame[REMOVES] = frame[REMOVES].astype("bool")
    return Flows(source=NAME, transactions=frame, notes=tuple(notes))


def _tickers(root: Element) -> dict[str, tuple[str, str]]:
    """Each security's symbol and name, by the id positions name it by."""
    found: dict[str, tuple[str, str]] = {}
    for info in root.iter("SECINFO"):
        if unique := _text(info, "UNIQUEID"):  # in the security's SECID
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
    notes: list[str] = []
    days: set[dt.date] = set()
    spending = _statements(root, *_SPENDING)
    investing = _statements(root, _INVESTMENT)
    names = _accounts(path, root, [*spending, *investing])
    for statement, account in zip(spending, names[:len(spending)], strict=True):
        notes += _foreign(account, statement, "STMTTRN")
        ledger = _find(statement, "LEDGERBAL")
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
        notes += _foreign(account, statement, "INVPOS")
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
        balances = _find(statement, "INVBAL")
        available = _text(balances, "AVAILCASH")
        cash = _cents(path, available, "an investment statement's AVAILCASH", exact=False) if available else 0
        if cash > swept:
            holdings.append(Holding(account, cash - swept, hints={"name": "cash", "type": "investment"}))
        # Which positions are the cash is a guess, so it is said wherever
        # it decided something: a money market fund bought as an investment
        # is priced at a dollar too, and is no part of AVAILCASH.
        if swept and cash > swept:
            notes.append(
                f"{account}: {_money(cash - swept)} of its cash of {_money(cash)} is added, taking the rest to "
                f"be its position(s) priced at a dollar a share ({_money(swept)}); check the account's total")
        elif 0 < cash < swept:
            notes.append(
                f"{account}: its cash of {_money(cash)} is not added, taken to be in its position(s) priced "
                f"at a dollar a share ({_money(swept)}); if it is apart from them, the account's total is short by it")
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
    return Snapshot(source=NAME, holdings=tuple(holdings), as_of=next(iter(days), None), notes=tuple(notes))
