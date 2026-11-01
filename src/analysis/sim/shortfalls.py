"""A run's negative-balance warnings, read back (#186, #431).

A shortfall doesn't halt a run: the debit posts, the balance goes negative,
and the engine records the event under
``simulation_run.run_args_json.$.negative_balance_warnings`` (ADR-003 §4.7).
``sim explain`` and the notebooks both read it through this module, so the
two say the same thing about the same run.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from analysis.sim.engine import describe_negative_balance_event
from analysis.sim.registries import DEFAULT_LOT
from analysis.sim.types import INVESTMENT_ASSET_SUBKINDS


def account_with_lot(name: str | None, subkind: str | None, lot: str | None) -> str:
    """``name``, plus ``(lot)`` where the lot is worth showing."""
    if not name:
        return ""
    if lot and (subkind in INVESTMENT_ASSET_SUBKINDS or lot != DEFAULT_LOT):
        return f"{name} ({lot})"
    return name


def compact_cents(cents: int | None) -> str:
    """``$8,000.00``, ``-$12.50``; ``-`` for None."""
    if cents is None:
        return "-"
    sign = "-" if cents < 0 else ""
    dollars, rem = divmod(abs(int(cents)), 100)
    return f"{sign}${dollars:,}.{rem:02d}"


@dataclass(frozen=True)
class NegativeBalance:
    """One recorded negative-balance warning: an asset account's move below
    zero, and how low it went before its next warning (or the run's end).

    ``balance_cents`` is the balance just after the debit that took it
    below zero; ``lowest_cents`` is the lower of that and its month-end
    balances since, at ``lowest_month``. ``lot`` names the account's lowest
    lot then, when the account has more than one worth naming."""

    month: str
    account: str
    lot: str | None
    balance_cents: int
    lowest_cents: int
    lowest_month: str
    refill: str

    @property
    def label(self) -> str:
        """The account, with the lot when it's worth showing."""
        return f"{self.account} ({self.lot})" if self.lot else self.account


def recorded_events(con: sqlite3.Connection) -> list[dict]:
    """The run's ``negative_balance_warnings`` entries as the engine wrote
    them; none for a run without any, or from before they were recorded."""
    row = con.execute(
        "SELECT json_extract(run_args_json, '$.negative_balance_warnings') "
        "FROM simulation_run WHERE id = 1"
    ).fetchone()
    return json.loads(row[0]) if row and row[0] else []


def negative_balances(con: sqlite3.Connection) -> list[NegativeBalance]:
    """Each of the run's negative-balance warnings, in the order recorded,
    with the lowest balance its account reached before it next went negative."""
    events = recorded_events(con)
    if not events:
        return []
    ids = {name: (account_id, subkind) for account_id, name, subkind in con.execute(
        "SELECT id, name, subkind FROM account")}
    # Each warning's stretch ends where its account's next one begins.
    following: dict[int, str | None] = {}
    last_by_account: dict[str, int] = {}
    for index, event in enumerate(events):
        account = str(event.get("account", ""))
        if account in last_by_account:
            following[last_by_account[account]] = str(event.get("month", ""))
        last_by_account[account] = index
    return [
        _with_lowest(con, event, ids.get(str(event.get("account", ""))), following.get(index))
        for index, event in enumerate(events)
    ]


def _with_lowest(
    con: sqlite3.Connection, event: dict, account: tuple[int, str | None] | None,
    until: str | None,
) -> NegativeBalance:
    month = str(event.get("month", "-"))
    name = str(event.get("account", "-"))
    balance = int(event.get("balance_cents") or 0)
    lowest, lowest_month, lot = balance, month, None
    if account is not None:
        account_id, subkind = account
        row = con.execute(
            "SELECT s.id, s.month, SUM(ls.balance) AS balance "
            "FROM lot_state ls JOIN account_lot m ON m.id = ls.lot_id "
            "JOIN simulation_step s ON s.id = ls.step_id "
            "WHERE m.account_id = ? AND s.month >= ? AND (? IS NULL OR s.month < ?) "
            "GROUP BY s.id ORDER BY balance, s.month LIMIT 1",
            (account_id, month, until, until),
        ).fetchone()
        step_id = None
        if row is not None and row[2] < lowest:
            step_id, lowest_month, lowest = row
        lot = _lowest_lot(con, account_id, subkind, step_id, month)
    return NegativeBalance(
        month=month, account=name, lot=lot, balance_cents=balance,
        lowest_cents=lowest, lowest_month=lowest_month,
        refill=describe_negative_balance_event(event),
    )


def _lowest_lot(
    con: sqlite3.Connection, account_id: int, subkind: str | None,
    step_id: int | None, month: str,
) -> str | None:
    """The account's lowest lot at ``step_id`` (else the step of ``month``),
    when it is below zero and worth naming."""
    if step_id is None:
        found = con.execute("SELECT id FROM simulation_step WHERE month = ?", (month,)).fetchone()
        if found is None:
            return None
        step_id = found[0]
    row = con.execute(
        "SELECT m.lot, ls.balance FROM lot_state ls JOIN account_lot m ON m.id = ls.lot_id "
        "WHERE m.account_id = ? AND ls.step_id = ? ORDER BY ls.balance LIMIT 1",
        (account_id, step_id),
    ).fetchone()
    if row is None or row[1] >= 0:
        return None
    lot = row[0]
    return lot if account_with_lot("x", subkind, lot) != "x" else None


def warning_line(warning: NegativeBalance, width: int = 0) -> str:
    """One warning as ``sim explain`` and the notebooks list it: the month,
    the account (and lot), the balance then, the lowest it reached, and
    what its refill chain did."""
    lowest = (
        "" if warning.lowest_cents == warning.balance_cents
        and warning.lowest_month == warning.month
        else f"  lowest {compact_cents(warning.lowest_cents)} at {warning.lowest_month}"
    )
    return (f"{warning.month}  {warning.label:<{width}}  {compact_cents(warning.balance_cents)}"
            f"{lowest}  {warning.refill}")


def warning_lines(warnings: Sequence[NegativeBalance]) -> list[str]:
    """``warning_line`` for each, with the account column aligned."""
    width = max((len(w.label) for w in warnings), default=0)
    return [warning_line(w, width) for w in warnings]


def run_negative_balances(run_path: Path | str) -> list[NegativeBalance]:
    """``negative_balances`` of the run file at ``run_path``, read-only."""
    con = sqlite3.connect(f"file:{Path(run_path)}?mode=ro", uri=True)
    try:
        return negative_balances(con)
    finally:
        con.close()


@dataclass(frozen=True)
class MemberShortfalls:
    """An ensemble batch's members' negative-balance warnings: how many
    members had any, of how many read, and the lowest balance any reached."""

    members: int
    with_warnings: int
    worst_member: str | None = None
    worst: NegativeBalance | None = None


def member_shortfalls(run_paths: Iterable[Path]) -> MemberShortfalls:
    """``MemberShortfalls`` over the member run files ``run_paths``. Each is
    asked only for its recorded warnings, and only a member with some is
    searched for its lowest balance, so a batch without shortfalls is quick."""
    members = with_warnings = 0
    worst_member: str | None = None
    worst: NegativeBalance | None = None
    for path in run_paths:
        members += 1
        con = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)
        try:
            if not recorded_events(con):
                continue
            found = negative_balances(con)
        finally:
            con.close()
        with_warnings += 1
        lowest = min(found, key=lambda w: w.lowest_cents)
        if worst is None or lowest.lowest_cents < worst.lowest_cents:
            worst_member, worst = Path(path).stem, lowest
    return MemberShortfalls(members, with_warnings, worst_member, worst)
