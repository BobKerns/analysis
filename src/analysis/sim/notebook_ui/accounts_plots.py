"""Accounts notebook widget: wraps the low-level balance-history renderer.

Supplies its pivots from a :class:`~analysis.sim.connection.RunConnection`
instead of pre-built pandas structures.
"""

from __future__ import annotations

from analysis.sim.accounts_plot import account_balance_history_widget
from analysis.sim.connection import RunConnection


def account_history_widget(
    rc: RunConnection, kind: str, title: str, initial_count: int = 1,
):
    """Balance-history widget for accounts of one ``kind`` (asset/liability/income/expense)."""
    return account_balance_history_widget(
        rc.balance_pivot(kind), rc.account_kinds(), title, kind,
        initial_count=initial_count, cpi_balance_pivot=rc.cpi_balance_pivot(kind),
    )
