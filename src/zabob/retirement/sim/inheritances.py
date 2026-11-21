"""What each death did with the household's accounts, read back from a run (#274).

The engine records it in the run's ``inheritance`` table (ADR-003 §4.9): an
account a household member took over, no money moving, or a lot swept into
its beneficiary chain, which may place some of it outside the household as
a bequest. ``sim explain``, the allocation by owner and the notebooks read it
through this module, so they say the same thing about the same run.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from zabob.retirement.sim.shortfalls import account_with_lot

# The rules, as ``inheritance.rule`` holds them, and how a listing says them.
RULES: dict[str, str] = {
    "designated": "its beneficiary",
    "spouse": "surviving spouse",
    "estate": "the estate",
}


@dataclass(frozen=True)
class Inheritance:
    """One account or lot at one death. ``to`` is who took the account over,
    or the chain (or account) a lot was swept into; ``amount_cents`` is the
    account's balance then, or what the sweep placed; ``bequest_cents`` how
    much of that left the household."""

    month: str
    decedent: str
    account: str
    to: str
    amount_cents: int
    bequest_cents: int
    rule: str

    @property
    def rule_label(self) -> str:
        return RULES.get(self.rule, self.rule)


def inheritances(con: sqlite3.Connection) -> list[Inheritance]:
    """The run's inheritances in the order they happened; empty for a run
    with none. A lot that was empty is left out."""
    rows = con.execute(
        "SELECT i.date, COALESCE(p.display, i.decedent), a.name, a.subkind, m.lot, "
        "       COALESCE(i.owner, c.name), CASE WHEN i.owner IS NULL THEN i.placed ELSE i.balance END, "
        "       (SELECT COALESCE(SUM(te.amount), 0) FROM transfer_event te "
        "        WHERE te.reason_kind = 'inheritance' AND te.reason_id = i.id "
        "        AND te.tax_character = 'bequest'), i.rule "
        "FROM inheritance i JOIN account a ON a.id = i.account_id "
        "LEFT JOIN account_lot m ON m.id = i.lot_id LEFT JOIN chain c ON c.id = i.beneficiary_chain_id "
        "LEFT JOIN person p ON p.id = i.decedent "
        "WHERE i.owner IS NOT NULL OR i.balance > 0 ORDER BY i.id"
    ).fetchall()
    return [
        Inheritance(str(month)[:10], str(decedent),
                    account if lot is None else account_with_lot(account, subkind, lot),
                    str(to), int(amount), int(bequest), str(rule))
        for month, decedent, account, subkind, lot, to, amount, bequest, rule in rows
    ]


def run_inheritances(run_path: Path) -> list[Inheritance]:
    """``inheritances`` of the run file at ``run_path``, read only."""
    con = sqlite3.connect(f"file:{run_path}?mode=ro", uri=True)
    try:
        return inheritances(con)
    finally:
        con.close()


def owner_changes(con: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """Each account a death passed to a new owner, no money moving:
    ``(account name, final month, new owner)``, in order. The new owner
    holds it from the month after."""
    return [(str(a), str(d), str(o)) for a, d, o in con.execute(
        "SELECT a.name, i.date, i.owner FROM inheritance i JOIN account a ON a.id = i.account_id "
        "WHERE i.owner IS NOT NULL ORDER BY i.date, i.id")]


def run_owner_changes(run_path: Path) -> list[tuple[str, str, str]]:
    """``owner_changes`` of the run file at ``run_path``, read only."""
    con = sqlite3.connect(f"file:{run_path}?mode=ro", uri=True)
    try:
        return owner_changes(con)
    finally:
        con.close()


def owner_in(changes: list[tuple[str, str, str]], account: str, owner: str, month: str) -> str:
    """``account``'s owner in ``month``, given its ``owner`` column and the
    run's ``owner_changes``."""
    for name, final_month, new_owner in changes:
        if name == account and final_month < month:
            owner = new_owner
    return owner
