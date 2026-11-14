"""Applying a holdings snapshot to a scenario's lots (issue #527).

``plan`` works out the change without writing anything: the CLI shows it as a
before/after diff, asks about whatever the mapping left open, and writes the
changed lots through a staged edit.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from analysis.sim.imports.forms import Holding, Snapshot
from analysis.sim.imports.mapping import AccountMap, AccountTarget
from analysis.sim.registries import DEFAULT_LOT, RETIREMENT_LOT_SUBKINDS
from analysis.sim.types import INVESTMENT_ASSET_SUBKINDS, InvestmentAssetSubkind
from analysis.sim.units import dollars_to_cents

__all__ = [
    "Change", "HoldingsPlan", "format_dollars", "is_investment_asset", "plan",
    "roth_basis_note",
]

def is_investment_asset(kind: str, subkind: str) -> bool:
    """Whether an account's ``prior_year_end_balance`` is meaningful to set."""
    return kind == "asset" and subkind in INVESTMENT_ASSET_SUBKINDS


def format_dollars(cents: int) -> str:
    """Format integer cents as the dollars-and-cents string account_lots.csv expects."""
    sign = "-" if cents < 0 else ""
    dollars, rem = divmod(abs(cents), 100)
    return f"{sign}{dollars}.{rem:02d}"


@dataclass(frozen=True)
class Change:
    """One cell of a lot the import sets."""

    account: str
    lot: str
    column: str
    before: str
    after: str
    note: str = ""


@dataclass
class HoldingsPlan:
    rows: list[dict[str, str]]
    """The scenario's lots as the import would leave them, in the order given."""
    cells: dict[int, dict[str, str]] = field(default_factory=dict)
    """The cells to write, by index into ``rows``: only what the import sets."""
    changes: list[Change] = field(default_factory=list)
    lots_changed: int = 0
    """Lots whose balance differs (a seeded prior-year balance alone doesn't count)."""
    lots_unchanged: int = 0
    unmapped: list[Holding] = field(default_factory=list)
    """Holdings the map has no row for."""
    unknown: list[tuple[Holding, AccountTarget]] = field(default_factory=list)
    """Holdings mapped to an account the scenario doesn't have."""
    no_lot: list[tuple[Holding, AccountTarget]] = field(default_factory=list)
    """Holdings mapped to an account the scenario has, but not to a lot of it."""
    undecided: list[tuple[Holding, AccountTarget]] = field(default_factory=list)
    """Holdings with a symbol, mapped to an account whose one lot has none:
    add them together into that lot, or keep each as a lot of its own?"""
    new: list[tuple[Holding, AccountTarget]] = field(default_factory=list)
    """Holdings with a symbol none of the account's lots has."""
    skipped: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    """Lots emptied because their account's file no longer has them."""
    ineligible: list[str] = field(default_factory=list)
    """Accounts ``--year-end`` doesn't apply to."""
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    """What the import couldn't set, and what to do about it."""

    @property
    def complete(self) -> bool:
        """Whether every holding found a place (or was skipped on purpose)."""
        return not (self.unmapped or self.unknown or self.no_lot or self.undecided or self.new)


@dataclass
class _Landed:
    """What the holdings that land in one lot add up to."""

    cents: int = 0
    quantity: Decimal | None = Decimal(0)
    """None once any holding lacks one."""
    basis_cents: int | None = 0
    by_symbol: bool = False


def _lot_name(row: Mapping[str, str]) -> str:
    return row.get("lot", "") or DEFAULT_LOT


def plan(
    snapshot: Snapshot, account_map: AccountMap, accounts: Mapping[str, tuple[str, str]],
    lots: Sequence[Mapping[str, str]], *, year_end: bool = False,
    split: Collection[str] = (), fresh: Collection[int] = (), prune: bool = False,
) -> HoldingsPlan:
    """What importing ``snapshot`` would change.

    ``accounts`` is each scenario account's (kind, subkind); ``lots`` every
    lot the scenario has, its own and inherited, one whole row each.

    Without ``year_end``, sets ``opening_balance`` (and seeds
    ``prior_year_end_balance`` only when it's still blank, and only for
    investment asset accounts). With it, the snapshot is the prior year's
    end: ``prior_year_end_balance`` is always overwritten, for investment
    asset accounts only, and ``opening_balance`` is left alone. The loader
    wants that balance on every lot of an account (Roth lots aside) or on
    none, so a lot is seeded, or set with ``year_end``, only when that leaves
    the account whole.

    **Which lot.** A mapping that names a lot takes everything it covers:
    two holdings mapped to one lot are added together, as the source signs
    them. One that names only the account sends a holding with a symbol to
    the account's lot of that symbol. With no such lot, the holding is
    ``new``, unless the account has a single lot with no symbol: then it is
    ``undecided``, since the holdings may be meant to add up into that lot;
    ``split`` names the accounts where that was decided against, so theirs
    are ``new``. A holding with no symbol goes to the account's only lot.

    **What is set.** A lot kept in shares (a quantity and a symbol, and no
    balance) has its ``quantity`` set; any other lot its balance, and its
    quantity too if it has one. The quantity is the file's only when all
    that lands in the lot is the lot's own symbol. ``opening_cost_basis`` is
    set on a taxable brokerage lot when the file has a basis for all of it. A retirement or
    HSA lot's basis is never set: a broker's cost basis is what the
    holdings cost, and a Roth lot's basis means what was contributed.

    ``fresh`` is the indexes of lots made for this import, which have only
    their names and symbol yet: each takes its holding's quantity and
    balance both, so it needs no price to load.

    A file that lists an account's holdings one by one says all the account
    holds, so a lot of that account it doesn't mention may have been sold.
    Such a lot keeps what it holds, with a warning, unless ``prune``: then
    its balance, shares and basis are set to zero. The one lot of a ``split``
    account is emptied either way, since the lots made from it hold its total. Its prior-year balance
    stays, since it held that on December 31 whatever became of it since.

    A liability is stored as a non-negative debt, so its total is negated.
    """
    result = HoldingsPlan(rows=[dict(row) for row in lots])
    landed: dict[int, _Landed] = {}
    # The lots a holding was matched to by symbol, landed or not: the file has them.
    in_file: set[int] = set()
    for holding in snapshot.holdings:
        target = account_map.lookup(holding.account, holding.asset)
        if target is None:
            result.unmapped.append(holding)
            continue
        if target.action == "skip":
            result.skipped.append(holding.label)
            continue
        info = accounts.get(target.account)
        if info is None:
            result.unknown.append((holding, target))
            continue
        if year_end and not is_investment_asset(*info):
            result.ineligible.append(target.account)
            continue
        of_account = [i for i, row in enumerate(result.rows) if row.get("account_name") == target.account]
        by_symbol = False
        if target.lot:
            # The default lot is a blank cell, whichever way the map spells it.
            wanted = {target.lot, "" if target.lot == DEFAULT_LOT else target.lot}
            indexes = [i for i in of_account if result.rows[i].get("lot", "") in wanted]
            if not indexes:
                result.no_lot.append((holding, target))
                continue
        elif holding.asset:
            indexes = [i for i in of_account
                       if result.rows[i].get("symbol", "").strip().upper() == holding.asset.upper()]
            by_symbol = True
            in_file.update(indexes)
            if not indexes:
                alone = len(of_account) == 1 and not result.rows[of_account[0]].get("symbol", "").strip()
                undecided = alone and target.account not in split
                (result.undecided if undecided else result.new).append((holding, target))
                continue
        else:
            indexes = of_account
            if not indexes:
                result.no_lot.append((holding, target))
                continue
        if len(indexes) > 1:
            what = f"lots of {holding.asset}" if by_symbol else "lots"
            result.warnings.append(
                f"{target.account!r} has {len(indexes)} {what} and the mapping of "
                f"{holding.label!r} names none, so it was skipped; set its 'lot' in "
                f"{account_map.path}"
            )
            continue
        into = landed.setdefault(indexes[0], _Landed())
        into.cents += holding.value_cents
        into.by_symbol |= by_symbol
        # Shares of two funds don't add up to a number of shares.
        same = holding.asset.upper() == result.rows[indexes[0]].get("symbol", "").strip().upper()
        into.quantity = None if into.quantity is None or holding.quantity is None or not same \
            else into.quantity + holding.quantity
        into.basis_cents = None if into.basis_cents is None or holding.cost_basis_cents is None \
            else into.basis_cents + holding.cost_basis_cents

    for index, into in landed.items():
        row = result.rows[index]
        name, lot = row["account_name"], row.get("lot", "")
        kind, subkind = accounts[name]
        cents = into.cents
        if not year_end and kind == "liability":
            cents = -cents
            if cents < 0:
                result.warnings.append(
                    f"{name!r} has an unusual credit balance in {snapshot.source}; "
                    "storing magnitude only"
                )
                cents = abs(cents)
        dollars = format_dollars(cents)
        # The account's other lots that must agree with this one on having a
        # prior-year balance: those this run doesn't set too.
        others = [
            other for i, other in enumerate(result.rows)
            if i not in landed and other.get("account_name") == name
            and RETIREMENT_LOT_SUBKINDS.get(other.get("subkind", "")) != "tax_free"
        ]
        whole = all(other.get("prior_year_end_balance", "") for other in others)
        if year_end and not whole:
            result.warnings.append(
                f"{name!r} has other lots with no prior_year_end_balance, which must be on "
                f"every lot of an account or on none, so lot {lot or DEFAULT_LOT!r} was "
                "skipped; import every lot of the account in one run")
            continue
        cells: dict[str, str] = {}
        changed = False

        def put(column: str, after: str, *, same: bool, note: str = "",
                row: dict[str, str] = row, cells: dict[str, str] = cells,
                name: str = name, lot: str = lot) -> bool:
            if same:
                return False
            result.changes.append(Change(name, lot, column, row.get(column, ""), after, note))
            cells[column] = after
            return True

        # Compared as money: '20000' and '20000.00' are the same balance, and
        # rewriting one as the other would override an inherited lot for nothing.
        if year_end:
            changed |= put("prior_year_end_balance", dollars,
                           same=_cents(row.get("prior_year_end_balance", "")) == cents)
        else:
            had_quantity = _number(row.get("quantity", ""))
            in_shares = had_quantity is not None and not row.get("opening_balance", "").strip() \
                and bool(row.get("symbol", "").strip())
            if into.quantity is not None and (had_quantity is not None or index in fresh):
                changed |= put("quantity", _plain(into.quantity), same=had_quantity == into.quantity)
            elif had_quantity is not None and into.by_symbol:
                # The file lists this holding and gives no share count (a
                # money market's row): a new balance beside the old count
                # would have the lot say two things. A source with one
                # number for the account says nothing of shares either way.
                changed |= put("quantity", "", same=False, note="the file has no share count")
            if not (in_shares and into.quantity is not None):
                changed |= put("opening_balance", dollars,
                               same=_cents(row.get("opening_balance", "")) == cents,
                               note="new lot" if index in fresh else "")
            # The one subkind whose lots keep a cost basis an import can set.
            if into.basis_cents is not None and subkind == InvestmentAssetSubkind.BROKERAGE_TAXABLE:
                changed |= put("opening_cost_basis", format_dollars(into.basis_cents),
                               same=_cents(row.get("opening_cost_basis", "")) == into.basis_cents)
            if RETIREMENT_LOT_SUBKINDS.get(row.get("subkind", "")) == "tax_free" \
                    and not row.get("opening_cost_basis", "").strip():
                result.notes.append(roth_basis_note(name, lot))
        result.lots_changed += changed
        result.lots_unchanged += not changed
        if not year_end and is_investment_asset(kind, subkind) and whole \
                and not row.get("prior_year_end_balance", ""):
            put("prior_year_end_balance", dollars, same=False, note="seeded from opening")
        if cells:
            row |= cells
            result.cells[index] = cells

    # A file that lists an account's holdings one by one says what the
    # account holds: a lot it doesn't mention may have been sold.
    listed = {result.rows[index]["account_name"] for index, into in landed.items() if into.by_symbol}
    listed |= {target.account for _, target in result.new}
    for index, row in enumerate(result.rows):
        if year_end or row.get("account_name") not in listed or index in landed or index in in_file \
                or not (_cents(row.get("opening_balance", "")) or _number(row.get("quantity", ""))):
            continue
        # A lot the holdings were split out of is emptied whatever was asked:
        # its total is in the new lots now, and would be counted twice.
        split_out = row["account_name"] in split and not row.get("symbol", "").strip()
        if prune or split_out:
            cells = {"opening_balance": "0.00"} if row.get("opening_balance", "").strip() else {}
            cells |= {column: zero for column, zero in (("quantity", "0"), ("opening_cost_basis", "0.00"))
                      if row.get(column, "").strip()}
            cells = {column: zero for column, zero in cells.items()
                     if _number(row.get(column, "")) != 0}
            result.changes.extend(
                Change(row["account_name"], row.get("lot", ""), column, row.get(column, ""), zero,
                       "split into lots" if split_out else "not in the file")
                for column, zero in cells.items())
            row |= cells
            result.cells[index] = cells
            result.lots_changed += 1
            result.pruned.append(f"{row['account_name']}:{_lot_name(row)}")
            continue
        result.warnings.append(
            f"lot {_lot_name(row)!r} of {row['account_name']} isn't in the "
            f"{snapshot.source} file and keeps what it holds, though it may have been sold; "
            "--prune empties such lots, and "
            f"'sim account lot delete {row['account_name']} {_lot_name(row)}' removes this one")
    return result


def roth_basis_note(account: str, lot: str) -> str:
    """What to tell the user about a Roth lot an import filled: its basis
    is the contributions, which no brokerage file has."""
    return (
        f"lot {lot or DEFAULT_LOT!r} of {account} is a Roth lot with no basis, so the plan "
        "takes its whole balance as contributions, which come out untaxed at any age. An "
        "outside file has at most what the holdings cost, which isn't that. Set what was "
        f"contributed and converted with 'sim account lot edit {account} {lot or DEFAULT_LOT} "
        "--opening-cost-basis AMOUNT' (the custodian's Form 5498s add up to it)")


def _number(text: str) -> Decimal | None:
    """A quantity cell as a number; None for a blank or malformed one."""
    try:
        return Decimal(text.strip()) if text.strip() else None
    except InvalidOperation:
        return None


def _plain(number: Decimal) -> str:
    """``number`` without an exponent or trailing zeros: 808.576, 1000."""
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _cents(text: str) -> int | None:
    """A balance cell as cents; None for a blank or malformed one."""
    try:
        return dollars_to_cents(text)
    except ValueError:
        return None
