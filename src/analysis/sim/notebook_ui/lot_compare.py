"""Scenario-compare widget for lots (ADR-005), called like the widgets in
:mod:`compare_plots` (``CONNECTIONS.show(widget, ...)``).

:func:`account_definitions_widget` shows how accounts, their lots and the
year-to-date rows are *defined* differently across scenarios. How they
*evolve* differently (balance, cost basis, quantity) is
:func:`compare_plots.show_account_balance_diffs`, which drills into lots.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pandas as pd
from IPython.display import display

from analysis.sim.compare import (
    ABSENT,
    account_definition_differences,
    year_to_date_differences,
)
from analysis.sim.connection import RunConnection
from analysis.sim.notebook_ui.pickers import (
    MultiScenarioConnections,
    display_warning,
)
from analysis.sim.notebook_ui.styled import StyledMarkdown

__all__ = [
    "account_definition_differences",
    "account_definitions_widget",
    "year_to_date_differences",
]

def account_definitions_widget(
    container: MultiScenarioConnections,
    connections: Mapping[str, RunConnection],
    scenarios: Sequence[str],
    variants: Sequence[str],
    baseline: str = "current",
    **_,
) -> None:
    """Accounts, lots and year-to-date rows that are defined differently."""
    if not container.ok:
        return
    with pd.option_context("display.max_rows", None, "display.max_columns", None):
        accounts = account_definition_differences(connections, scenarios, baseline)
        if accounts.empty:
            display_warning("**All scenarios define accounts and lots identically.**")
        else:
            display(StyledMarkdown("### Accounts and lots"))
            display(accounts.fillna(ABSENT))
        year_to_date = year_to_date_differences(connections, scenarios, baseline)
        if year_to_date.empty:
            display_warning("**All scenarios have identical year-to-date rows.**")
        else:
            display(StyledMarkdown("### Year-to-date rows"))
            display(year_to_date.fillna(ABSENT))
