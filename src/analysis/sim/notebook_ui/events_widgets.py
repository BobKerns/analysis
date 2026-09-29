"""Event explorer: every dollar movement in a run (``transfer_event``),
filterable by reason kind, tax character, account and date range."""

from __future__ import annotations

from datetime import date

import pandas as pd
from IPython.display import Markdown, display
from ipywidgets import widgets

from analysis.sim import registries
from analysis.sim.connection import RunConnection
from analysis.sim.notebook_ui.pickers import DateRangeSlider

__all__ = ["events_view", "events_widget", "run_date_bounds"]

_DISPLAY_COLUMNS = [
    "date", "reason_kind", "rule_name", "from_name", "from_lot", "to_name", "to_lot",
    "amount", "tax_character",
]
_RENAME = {
    "reason_kind": "reason", "rule_name": "rule", "from_name": "from", "to_name": "to",
}


def run_date_bounds(rc: RunConnection) -> tuple[date, date]:
    """The run's first and last event date, for sizing a date-range picker."""
    dates = rc.items("transfer_event")["date"]
    return date.fromisoformat(dates.min()), date.fromisoformat(dates.max())


def events_view(
    rc: RunConnection,
    accounts: tuple[str, ...] = (),
    reason_kinds: tuple[str, ...] = (),
    tax_characters: tuple[str, ...] = (),
    start: date | None = None,
    end: date | None = None,
) -> tuple[str, pd.DataFrame]:
    """Heading and table of ``transfer_event`` rows matching the filters.

    An empty ``accounts``/``reason_kinds``/``tax_characters`` means no filter
    on that axis. ``accounts`` matches a row where the account is *either*
    end of the transfer (its lot is shown alongside it when the account has
    more than one)."""
    filters: dict[str, list[str]] = {}
    if reason_kinds:
        filters["reason_kind"] = list(reason_kinds)
    if tax_characters:
        filters["tax_character"] = list(tax_characters)
    frame = rc.items("transfer_event", **filters)
    if accounts:
        frame = frame[frame["from_name"].isin(accounts) | frame["to_name"].isin(accounts)]
    if start is not None:
        frame = frame[frame["date"] >= start.isoformat()]
    if end is not None:
        frame = frame[frame["date"] <= end.isoformat()]
    frame = frame.sort_values(["date", "id"]).reset_index(drop=True)

    total = frame["amount"].sum() / 100
    heading = f"**{len(frame):,} event(s)**, ${total:,.2f} total ({rc.label})"
    shown = frame[[c for c in _DISPLAY_COLUMNS if c in frame.columns]].rename(columns=_RENAME)
    return heading, shown


def _styled(table: pd.DataFrame):
    return table.style.format({"amount": "${:,.2f}"}, na_rep="")


def events_widget(rc: RunConnection) -> widgets.VBox:
    """Filter and browse a run's transfer events: reason kind, tax character,
    account, and date range."""
    everything = rc.items("transfer_event")
    accounts = sorted(set(everything["from_name"]) | set(everything["to_name"]))
    account_picker = widgets.SelectMultiple(
        description="Accounts:", options=accounts,
        rows=min(10, max(4, len(accounts))), layout=widgets.Layout(width="45%"),
    )
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
            rc, accounts=account_picker.value, reason_kinds=reason_picker.value,
            tax_characters=tax_picker.value, start=date_picker.value[0], end=date_picker.value[1],
        )
        out.clear_output(wait=True)
        with out:
            display(Markdown(heading))
            display(_styled(table))

    for control in (account_picker, reason_picker, tax_picker, date_picker):
        control.observe(_redraw, names="value")
    _redraw()
    box = widgets.VBox([
        widgets.HBox([account_picker, reason_picker, tax_picker]),
        date_picker,
        out,
    ])
    return box
