"""Event explorer: every dollar movement in a run (``transfer_event``),
filterable by reason kind, tax character, account and date range."""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
from IPython.display import display
from ipywidgets import widgets

from analysis.sim import registries
from analysis.sim.connection import RunConnection
from analysis.sim.notebook_ui.pickers import AccountPicker, DateRangeSlider
from analysis.sim.notebook_ui.styled import StyledMarkdown

__all__ = ["events_view", "events_widget", "run_date_bounds"]

_DISPLAY_COLUMNS = [
    "date", "reason_kind", "rule_name", "from_name", "from_lot", "to_name", "to_lot",
    "amount", "requested_cents", "remainder_cents", "tax_character", "parent_event_id",
    "balance",
]
_RENAME = {
    "reason_kind": "reason", "rule_name": "rule", "from_name": "from", "to_name": "to",
    "requested_cents": "requested", "remainder_cents": "remainder",
    "parent_event_id": "caused_by",
}
_DOLLAR_COLUMNS = ["amount", "requested", "remainder", "balance"]
# Catch-up entries for the months before the scenario starts: reported, but
# they don't move balances (ADR-003 §4.10).
_BALANCE_NEUTRAL = {"ytd", "ytd_rule"}


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
    lots: tuple[str, ...] = (),
) -> tuple[str, pd.DataFrame]:
    """Heading and table of ``transfer_event`` rows matching the filters.

    An empty ``accounts``/``reason_kinds``/``tax_characters`` means no filter
    on that axis. ``accounts`` matches a row where the account is *either*
    end of the transfer (its lot is shown alongside it when the account has
    more than one). ``lots``, with exactly one account, narrows that to rows
    where one of those lots of the account is an end (#170), as
    ``sim items --lot`` does.

    The table is indexed by event id. ``caused_by`` is the event a leg
    derives from, such as the debit that set off a refill. ``requested`` and
    ``remainder`` are filled in only where a request was split across legs
    or fell short; the table shows them as '-' elsewhere. With exactly one
    asset or liability account picked, a ``balance`` column gives that
    account's balance after each row, and rows are in posting order; not
    with ``lots``, since that balance is the whole account's."""
    if lots and len(accounts) != 1:
        raise ValueError("lots narrow a single account")
    filters: dict[str, Any] = {}
    if reason_kinds:
        filters["reason_kind"] = list(reason_kinds)
    if tax_characters:
        filters["tax_character"] = list(tax_characters)
    frame = rc.items("transfer_event", **filters)
    if accounts:
        frame = frame[frame["from_name"].isin(accounts) | frame["to_name"].isin(accounts)]
    if lots:
        (account,) = accounts
        frame = frame[((frame["from_name"] == account) & frame["from_lot"].isin(lots))
                      | ((frame["to_name"] == account) & frame["to_lot"].isin(lots))]
    if start is not None:
        frame = frame[frame["date"] >= start.isoformat()]
    if end is not None:
        frame = frame[frame["date"] <= end.isoformat()]
    balance = _running_balance(rc, accounts[0]) if len(accounts) == 1 and not lots else None
    if balance is not None:
        # The balance accumulates in posting order, so the table follows it;
        # a debit posted after a later-dated one would otherwise read as a jump.
        frame = frame.assign(balance=balance.reindex(frame["id"]).to_numpy())
        frame = frame.sort_values("id")
    else:
        frame = frame.sort_values(["date", "id"])
    frame = frame.set_index("id").rename_axis("event")

    total = frame["amount"].sum() / 100
    heading = f"**{len(frame):,} event(s)**, ${total:,.2f} total ({rc.label})"
    if lots:
        heading += f"; lot(s) {', '.join(lots)} of {accounts[0]}"
    if balance is not None:
        heading += f"; running balance of {accounts[0]}, in posting order"
    shown = frame[[c for c in _DISPLAY_COLUMNS if c in frame.columns]].rename(columns=_RENAME)
    # requested/remainder only say something when a request was split across
    # legs or fell short; otherwise they repeat amount and 0, so leave them out.
    plain = (shown["requested"] == shown["amount"]) & (shown["remainder"] == 0)
    shown[["requested", "remainder"]] = shown[["requested", "remainder"]].mask(plain)
    shown["caused_by"] = shown["caused_by"].astype("Int64")
    # transfer_event money is integer cents; the table, like the heading, shows dollars.
    for column in _DOLLAR_COLUMNS:
        if column in shown.columns:
            shown[column] = shown[column] / 100
    return heading, shown


def _running_balance(rc: RunConnection, account: str) -> pd.Series | None:
    """``account``'s balance in cents after each of its transfer events, keyed
    by event id; None for an account without a balance (income, expense).

    Balance-neutral year-to-date entries get NaN. The opening balance is
    recovered from the first month-end balance, so the running total agrees
    with ``account_balance`` at every month end."""
    month_ends = rc.items("account_balance", account=account)
    if month_ends.empty or month_ends["kind"].iloc[0] not in ("asset", "liability"):
        return None
    events = rc.items("transfer_event")
    events = events[(events["from_name"] == account) | (events["to_name"] == account)]
    events = events.sort_values("id")
    moves = events[~events["reason_kind"].isin(_BALANCE_NEUTRAL)]
    # A transfer between two lots of the account nets to zero here.
    delta = (moves["amount"].where(moves["to_name"] == account, 0)
             - moves["amount"].where(moves["from_name"] == account, 0))
    if month_ends["kind"].iloc[0] == "liability":
        delta = -delta  # liabilities store the debt's magnitude
    running = delta.cumsum()
    first_month = month_ends["month"].min()
    first_balance = month_ends.loc[month_ends["month"] == first_month, "balance"].iloc[0]
    through_first = running[moves["month"] <= first_month]
    opening = first_balance - (through_first.iloc[-1] if len(through_first) else 0)
    balance = (running + opening).set_axis(moves["id"])
    return balance.reindex(events["id"])


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
