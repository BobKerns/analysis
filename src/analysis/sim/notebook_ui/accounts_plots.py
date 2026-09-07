"""Accounts notebook widget: wraps the low-level balance-history renderer.

Supplies its pivots from a :class:`~analysis.sim.connection.RunConnection`
instead of pre-built pandas structures.
"""

from __future__ import annotations
from typing import Protocol

from ipywidgets import widgets

from analysis.sim.accounts_plot import account_balance_history_widget
from analysis.sim.connection import RunConnection

def account_history_widget(
    rc: RunConnection, kind: str, title: str, initial_count: int = 1,
    plot_mode: widgets.ToggleButtons | None = None,
    value_mode: widgets.ToggleButtons | None = None,
    y_axis_mode: widgets.ToggleButtons | None = None,
    caveat: str | None = None,
):
    """Balance-history widget for accounts of one ``kind`` (asset/liability/income/expense)."""
    title = f"{title} - {rc.label}"
    return account_balance_history_widget(
        rc.balance_pivot(kind), rc.account_kinds(), title, kind,
        initial_count=initial_count, cpi_balance_pivot=rc.cpi_balance_pivot(kind),
        plot_mode=plot_mode,
        value_mode=value_mode,
        y_axis_mode=y_axis_mode,
        caveat=caveat,
    )
