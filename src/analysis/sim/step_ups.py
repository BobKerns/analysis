"""A run's basis step-ups at a death, read back (#255, #434).

The engine records each one in the run's ``basis_step_up`` table (ADR-003
§4.9): the lot, whose death, its balance, its basis before and after, and the
rule that decided how much of it stepped up. ``sim explain`` and the
notebooks both read them through this module, so the two say the same thing
about the same run.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from analysis.sim.shortfalls import account_with_lot

# The rules, as ``basis_step_up.rule`` holds them, and how a table says them.
RULES: dict[str, str] = {
    "owner": "owner alone",
    "inherited": "inherited from spouse",
    "joint_half": "joint, decedent's half",
    "community_property": "community property, both halves",
    "last_survivor": "joint, last survivor",
}


@dataclass(frozen=True)
class BasisStepUp:
    """One lot's basis step-up at a death. ``account`` names the lot too
    when it's worth naming (``shortfalls.account_with_lot``); ``person`` is
    the decedent's id and ``display`` their display name; ``rule`` is a key
    of ``RULES``."""

    month: str
    account: str
    person: str
    display: str
    balance_cents: int
    basis_before_cents: int
    basis_after_cents: int
    rule: str

    @property
    def rule_label(self) -> str:
        return RULES.get(self.rule, self.rule)


def basis_step_ups(con: sqlite3.Connection) -> list[BasisStepUp]:
    """The run's step-ups in the order they happened; empty for a run with
    no deaths, or none that stepped up.
    A lot that was empty at the death (no balance, no basis) is left out
    (#377): the engine records it, but it stepped nothing up."""
    rows = con.execute(
        "SELECT b.date, a.name, a.subkind, m.lot, b.person, COALESCE(p.display, b.person), "
        "       b.balance, b.basis_before, b.basis_after, b.rule "
        "FROM basis_step_up b JOIN account_lot m ON m.id = b.lot_id "
        "JOIN account a ON a.id = m.account_id "
        "LEFT JOIN person p ON p.id = b.person "
        "WHERE NOT (b.balance = 0 AND b.basis_before = 0 AND b.basis_after = 0) ORDER BY b.id"
    ).fetchall()
    return [
        BasisStepUp(str(month)[:10], account_with_lot(name, subkind, lot), str(person),
                    str(display), int(balance), int(before), int(after), rule_key)
        for month, name, subkind, lot, person, display, balance, before, after, rule_key in rows
    ]


def run_basis_step_ups(run_path: Path) -> list[BasisStepUp]:
    """``basis_step_ups`` of the run file at ``run_path``, read only."""
    con = sqlite3.connect(f"file:{run_path}?mode=ro", uri=True)
    try:
        return basis_step_ups(con)
    finally:
        con.close()
