"""Event explorer: every dollar movement in a run (``transfer_event``),
filterable by reason kind, tax character, account and date range. The
filtering is ``sim.events``, shared with ``sim items``."""

from __future__ import annotations


import pandas as pd
from IPython.display import display
from ipywidgets import widgets

from analysis.sim import registries
from analysis.sim.connection import RunConnection
from analysis.sim.events import events_view, run_date_bounds
from analysis.sim.notebook_ui.pickers import AccountPicker, DateRangeSlider
from analysis.sim.notebook_ui.styled import StyledMarkdown

__all__ = ["events_view", "events_widget", "run_date_bounds"]


def _styled(table: pd.DataFrame):
    dashed_dollars = [c for c in ("requested", "remainder", "balance") if c in table.columns]
    return (table.style
            .format({"amount": "${:,.2f}"}, na_rep="")
            .format("${:,.2f}", subset=dashed_dollars, na_rep="-")
            .format(subset=["caused_by"], na_rep="-"))


def events_widget(rc: RunConnection, **_) -> widgets.VBox:
    """Filter and browse a run's transfer events: reason kind, tax character,
    account, and date range."""
    everything = rc.items("transfer_event")
    accounts = sorted(set(everything["from_name"]) | set(everything["to_name"]))
    # Every kind in one list, nothing picked (no filter) to start; a single
    # account with several lots can be narrowed to some of them (#170).
    account_picker = AccountPicker(
        kind="any", allow_any=True, show_kind=False, accounts_by_kind={"any": accounts},
        initial_count=0, lots_by_account=rc.lots_by_account(),
    )
    if isinstance(account_picker.account_picker, widgets.SelectMultiple):  # not max_accounts=1
        account_picker.account_picker.rows = min(10, max(4, len(accounts)))
    account_picker.account_column.layout.width = "100%"
    account_picker.layout = widgets.Layout(width="45%")
    reason_picker = widgets.SelectMultiple(
        description="Reason:",
        options=[r for r in registries.REASON_KINDS if r in set(everything["reason_kind"])],
        rows=6, layout=widgets.Layout(width="25%"),
    )
    tax_picker = widgets.SelectMultiple(
        description="Tax:", options=sorted(set(everything["tax_character"])),
        rows=6, layout=widgets.Layout(width="25%"),
    )
    start, end = run_date_bounds(rc)
    date_picker = DateRangeSlider(start=start, end=end, description="Date range:")
    out = widgets.Output()

    def _redraw(*_):
        heading, table = events_view(
            rc, accounts=account_picker.selection[0], lots=account_picker.selection[1],
            reason_kinds=reason_picker.value,
            tax_characters=tax_picker.value, start=date_picker.value[0], end=date_picker.value[1],
        )
        out.clear_output(wait=True)
        with out:
            display(StyledMarkdown(heading))
            display(_styled(table))

    account_picker.observe(_redraw, names="selection")
    for control in (reason_picker, tax_picker, date_picker):
        control.observe(_redraw, names="value")
    _redraw()
    box = widgets.VBox([
        widgets.HBox([account_picker, reason_picker, tax_picker]),
        date_picker,
        out,
    ])
    return box
