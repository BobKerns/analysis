"""Accounts notebook widget: wraps the low-level balance-history renderer.

Supplies its pivots from a :class:`~analysis.sim.connection.RunConnection`
instead of pre-built pandas structures.
"""

from __future__ import annotations

from ipywidgets import widgets

from analysis.sim.accounts_plot import account_balance_history_widget
from analysis.sim.connection import RunConnection


def account_history_widget(
    rc: RunConnection, kind: str, title: str, initial_count: int = 1,
    plot_mode: widgets.ToggleButtons | None = None,
    value_mode: widgets.ToggleButtons | None = None,
    y_axis_mode: widgets.ToggleButtons | None = None,
):
    """Balance-history widget covering every account kind.

    ``kind`` only sets the Kind dropdown's initial selection
    (asset/liability/income/expense); switching it repopulates the Accounts
    picker instead of needing a separate widget per kind.
    """
    title = f"{title} - {rc.label}"
    return account_balance_history_widget(
        rc.balance_pivot, rc.account_kinds(), title, kind,
        initial_count=initial_count, cpi_balance_pivot_by_kind=rc.cpi_balance_pivot,
        plot_mode=plot_mode,
        value_mode=value_mode,
        y_axis_mode=y_axis_mode,
    )
