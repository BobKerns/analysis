"""Applying a holdings snapshot to a scenario's ``account_lots.csv`` (issue #527).

``plan`` works out the change without writing anything: the CLI shows it as a
before/after diff, asks about whatever the mapping left open, and writes the
planned rows through a staged edit.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from analysis.sim.imports.forms import Holding, Snapshot
from analysis.sim.imports.mapping import AccountMap, AccountTarget
from analysis.sim.registries import DEFAULT_LOT
from analysis.sim.types import INVESTMENT_ASSET_SUBKINDS

__all__ = ["Change", "HoldingsPlan", "format_dollars", "is_investment_asset", "plan"]


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
    """One cell of ``account_lots.csv`` the import sets."""

    account: str
    lot: str
    column: str
    before: str
    after: str
    note: str = ""


@dataclass
class HoldingsPlan:
    rows: list[dict[str, str]]
    """``account_lots.csv`` as the import would leave it."""
    changes: list[Change] = field(default_factory=list)
    lots_changed: int = 0
    """Lots whose balance differs (a seeded prior-year balance alone doesn't count)."""
    lots_unchanged: int = 0
    unmapped: list[Holding] = field(default_factory=list)
    """Holdings the map has no row for."""
    unknown: list[tuple[Holding, AccountTarget]] = field(default_factory=list)
    """Holdings mapped to an account or lot the scenario doesn't have."""
    skipped: list[str] = field(default_factory=list)
    ineligible: list[str] = field(default_factory=list)
    """Accounts ``--year-end`` doesn't apply to."""
    warnings: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """Whether every holding found a place (or was skipped on purpose)."""
        return not self.unmapped and not self.unknown


def _target_label(target: AccountTarget) -> str:
    return f"{target.account}:{target.lot}" if target.lot else target.account


def plan(
    snapshot: Snapshot, account_map: AccountMap, accounts: Mapping[str, tuple[str, str]],
    lots: Sequence[Mapping[str, str]], *, year_end: bool = False,
) -> HoldingsPlan:
    """What importing ``snapshot`` would change.

    ``accounts`` is each scenario account's (kind, subkind); ``lots`` the
    rows of the scenario's own ``account_lots.csv``.

    Without ``year_end``, sets ``opening_balance`` (and seeds
    ``prior_year_end_balance`` only when it's still blank, and only for
    investment asset accounts). With it, the snapshot is the prior year's
    end: ``prior_year_end_balance`` is always overwritten, for investment
    asset accounts only, and ``opening_balance`` is left alone.

    A liability is stored as a non-negative debt, so its signed outside value
    is negated. Two holdings mapped to one lot are added together.
    """
    result = HoldingsPlan(rows=[dict(row) for row in lots])
    totals: dict[int, int] = {}
    for holding in snapshot.holdings:
        target = account_map.lookup(holding.account, holding.asset)
        if target is None:
            result.unmapped.append(holding)
            continue
        if target.action == "skip":
            result.skipped.append(holding.account)
            continue
        info = accounts.get(target.account)
        if info is None:
            result.unknown.append((holding, target))
            continue
        kind, subkind = info
        if year_end and not is_investment_asset(kind, subkind):
            result.ineligible.append(target.account)
            continue
        indexes = [i for i, row in enumerate(result.rows) if row.get("account_name") == target.account]
        if target.lot:
            # The default lot is a blank cell, whichever way the map spells it.
            wanted = {target.lot, "" if target.lot == DEFAULT_LOT else target.lot}
            indexes = [i for i in indexes if result.rows[i].get("lot", "") in wanted]
            if not indexes:
                result.unknown.append((holding, target))
                continue
        elif not indexes:
            raise ValueError(f"mapped account not found in account_lots.csv: {target.account!r}")
        elif len(indexes) > 1:
            result.warnings.append(
                f"{target.account!r} has {len(indexes)} lots and the mapping of "
                f"{holding.account!r} names none, so it was skipped; set its 'lot' in "
                f"{account_map.path}"
            )
            continue
        cents = holding.value_cents
        if not year_end and kind == "liability":
            cents = -cents
            if cents < 0:
                result.warnings.append(
                    f"{_target_label(target)!r} has an unusual credit balance in "
                    f"{snapshot.source}; storing magnitude only"
                )
                cents = abs(cents)
        totals[indexes[0]] = totals.get(indexes[0], 0) + cents

    for index, cents in totals.items():
        row = result.rows[index]
        name, lot = row["account_name"], row.get("lot", "")
        dollars = format_dollars(cents)
        column = "prior_year_end_balance" if year_end else "opening_balance"
        before = row.get(column, "")
        if before != dollars:
            result.changes.append(Change(name, lot, column, before, dollars))
            result.lots_changed += 1
        else:
            result.lots_unchanged += 1
        row[column] = dollars
        if not year_end and is_investment_asset(*accounts[name]) \
                and not row.get("prior_year_end_balance", ""):
            row["prior_year_end_balance"] = dollars
            result.changes.append(
                Change(name, lot, "prior_year_end_balance", "", dollars, "seeded from opening"))
    return result
