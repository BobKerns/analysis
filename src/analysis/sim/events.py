"""A run's ledger (``transfer_event``), filtered: shared by ``sim items`` and
the event explorer in ``events.ipynb`` (#189), so both pick the same rows the
same way. Widget-free; ``notebook_ui.events_widgets`` draws it."""

from __future__ import annotations

from collections.abc import Collection
from datetime import date
from typing import Any

import pandas as pd

from analysis.frames import named_rows
from analysis.sim.connection import RunConnection

__all__ = ["RULE_REASONS", "events_view", "run_date_bounds", "tag_matches"]

_DISPLAY_COLUMNS = [
    "date", "reason_kind", "rule_name", "rule_kind", "from_name", "from_lot", "to_name", "to_lot",
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
RULE_REASONS = ("rule", "ytd_rule")
"""``transfer_event.reason_kind`` values whose ``reason_id`` is a scheduled rule."""


def run_date_bounds(rc: RunConnection) -> tuple[date, date]:
    """The run's first and last event date, for sizing a date-range picker."""
    dates = rc.items("transfer_event")["date"]
    return date.fromisoformat(dates.min()), date.fromisoformat(dates.max())


def _in_role(tags: str, role: str) -> set[str]:
    """The names of ``tags``' tokens (space-separated ``role:name``) in ``role``."""
    return {name for token in tags.split() for r, _, name in [token.partition(":")] if r == role}


def tag_matches(rc: RunConnection, frame: pd.DataFrame, tag: str) -> pd.Series:
    """Which of ``frame``'s events carry ``tag`` (``role:name``, or ``role:*``
    for any tag in the role).

    As ``sim report --by tag:ROLE`` groups them: a tag in the role on the
    scheduled rule that posted the event comes first, so a rule can refine
    its accounts' tags; an event whose rule has none in the role takes the
    tags of either account it moves money between."""
    role, sep, wanted = tag.partition(":")
    if not sep or not role or not wanted:
        raise ValueError(f"tag {tag!r} must be role:name, or role:* for the whole role")
    rules = rc.items("scheduled").set_index("id")["tags"]
    accounts = rc.items("account").set_index("name")["tags"]

    def has(names: set[str]) -> bool:
        return bool(names) and (wanted == "*" or wanted in names)

    def matches(row: Any) -> bool:
        if row.reason_kind in RULE_REASONS and pd.notna(row.reason_id):
            own = _in_role(rules.get(int(row.reason_id), ""), role)
            if own:
                return has(own)
        ends = _in_role(accounts.get(row.from_name, ""), role) | _in_role(accounts.get(row.to_name, ""), role)
        return has(ends)

    if frame.empty:
        return pd.Series([], index=frame.index, dtype=bool)
    return pd.Series([matches(row) for row in named_rows(frame, index=False)], index=frame.index)


def events_view(
    rc: RunConnection,
    accounts: tuple[str, ...] = (),
    reason_kinds: tuple[str, ...] = (),
    tax_characters: tuple[str, ...] = (),
    start: date | None = None,
    end: date | None = None,
    lots: tuple[str, ...] = (),
    rules: Collection[str] = (),
    tags: Collection[str] = (),
) -> tuple[str, pd.DataFrame]:
    """Heading and table of ``transfer_event`` rows matching the filters.

    An empty ``accounts``/``reason_kinds``/``tax_characters``/``rules``/``tags``
    means no filter on that axis; within one, any value matches. ``accounts``
    matches a row where the account is *either* end of the transfer (its lot
    is shown alongside it when the account has more than one). ``lots``,
    with exactly one account, narrows that to rows where one of those lots
    of the account is an end (#170), as ``sim items --lot`` does. ``rules``
    names the scheduled rules (or tax jurisdictions) that posted the row;
    ``tags`` are matched by :func:`tag_matches`.

    The table is indexed by event id. ``rule`` and ``rule_kind`` name the
    rule (or tax jurisdiction) behind a row. ``caused_by`` is the event a leg
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
    if rules:
        frame = frame[frame["rule_name"].isin(list(rules))]
    for tag in tags:
        frame = frame[tag_matches(rc, frame, tag)]
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
