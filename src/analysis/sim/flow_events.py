"""A run's income and expense events, grouped as the reports group them,
and its spending against the plan by month (#187, #387, #408).

Kept apart from ``reports`` because an ensemble's series store (#298)
derives each member's spending from these: the store hashes this module's
source, not the whole of ``reports``, so editing an unrelated report
doesn't rebuild every batch's store.
"""

from __future__ import annotations

from typing import Literal

import pandas as pd

from analysis.sim import registries
from analysis.sim.connection import (
    EXPENSE_CATEGORIES,
    EXPENSE_SUBKIND_TO_CATEGORY,
    INCOME_SUBKIND_TO_CATEGORY,
    NON_CASH_EXPENSE_ACCOUNTS,
    RunConnection,
    event_month,
)
from analysis.sim.drift import has_table

__all__ = [
    "FlowSide", "INCOME_REPORT_CATEGORIES", "SPENDING_BASELINE", "UNTAGGED", "event_groups",
    "flow_events", "group_order", "is_cash", "shortfall_events", "spending_by_month", "uses_spending_strategies",
]

SPENDING_BASELINE = "spending_baseline"
"""The run table of what each strategy-owned posting would have been (#387)."""

type FlowSide = Literal["income", "expenses"]

INCOME_REPORT_CATEGORIES = ["salary", "soc security", "investment", "business", "other"]
"""Income categories in report order. Unlike ``income_by_category``, which keeps
only the first three, the report keeps ``other`` (pensions, rentals,
inheritances): a total that leaves them out doesn't add up to the money that
came in."""

UNTAGGED = "(untagged)"
"""The group of an event with no tag in the ``--by tag:ROLE`` role."""

_RULE_REASONS = ("rule", "ytd_rule")
"""``transfer_event.reason_kind`` values whose ``reason_id`` is a scheduled rule."""


def _tag_names(tags: str, role: str) -> str | None:
    """The names of ``tags``' tokens in ``role``, joined with ``+``; None if none."""
    names = sorted(token.partition(":")[2] for token in tags.split()
                   if token.partition(":")[0] == role)
    return "+".join(names) if names else None


def _side(events: pd.DataFrame, prefix: str) -> pd.DataFrame:
    """``events`` with their ``prefix`` side's account name and subkind as
    ``account`` and ``subkind``."""
    return events.rename(columns={f"{prefix}_name": "account", f"{prefix}_subkind": "subkind"})


def is_cash(events: pd.DataFrame, prefix: str) -> pd.Series:
    """Whether each event's ``prefix`` side is a cash account (checking,
    savings, a business account)."""
    return ((events[f"{prefix}_kind"] == "asset")
            & events[f"{prefix}_subkind"].isin(registries.CASH_SUBKINDS))


def flow_events(rc: RunConnection, side: FlowSide) -> pd.DataFrame:
    """The run's income (or expense) transfer events with the account each one
    counts against: the income account it is drawn from, or the expense
    account it is paid into. Money paid back into an income account (a
    market drop's negative growth) counts against it, negative, and so does
    a vehicle's depreciation, as an investment loss on the ``depreciation``
    account. Depreciation is a loss of value, not spending: it is never an
    expense here. Income events also say
    whether the money landed in (or came back from) a ``cash`` account.

    ``month`` is the month end of the event's own date, not of the step that
    posted it: a year-to-date catch-up posts in the first step but is dated
    before the scenario starts, a prior year's one at that year's Dec 31.
    """
    columns = ["date", "amount", "reason_kind", "reason_id", "account", "subkind"]
    if side == "income":
        received = rc.items("transfer_event", from_kind="income")
        received = _side(received, "from").assign(cash=is_cash(received, "to"))
        received.loc[received["tax_character"] == registries.BUSINESS_INCOME,
                     "subkind"] = registries.BUSINESS_SUBKIND
        # A negative growth rate posts the other way, asset into its growth
        # account (#248): a market drop takes back that year's gains.
        # An income-to-income leg (a year-to-date record) is counted once, above.
        returned = rc.items("transfer_event", to_kind="income")
        returned = returned[returned["from_kind"] != "income"]
        returned = _side(returned, "to").assign(cash=is_cash(returned, "from"))
        returned["amount"] = -returned["amount"]
        # A vehicle's depreciation is the same loss of value as a market
        # drop (#249): an investment loss, counted against its own account.
        lost = rc.items("transfer_event", to_kind="expense")
        lost = lost[lost["to_name"].isin(NON_CASH_EXPENSE_ACCOUNTS)]
        lost = _side(lost, "to").assign(subkind="capital_gain", cash=False)
        lost["amount"] = -lost["amount"]
        # A business expense is a cost of earning the business's income, not
        # consumption (#249): it comes off business income, leaving the net.
        costs = rc.items("transfer_event", to_kind="expense",
                         to_subkind=registries.BUSINESS_SUBKIND)
        costs = _side(costs, "to").assign(cash=False)
        costs["amount"] = -costs["amount"]
        events = pd.concat([frame[[*columns, "cash"]]
                            for frame in (received, returned, lost, costs)],
                           ignore_index=True)
    else:
        events = rc.items("transfer_event", to_kind="expense")
        events = _side(events[~events["to_name"].isin(NON_CASH_EXPENSE_ACCOUNTS)], "to")[columns]
    events = events.copy()
    events["month"] = event_month(events)
    return events.drop(columns="date")


def event_groups(rc: RunConnection, events: pd.DataFrame, side: FlowSide, by: str) -> pd.Series:
    """Each event's group for ``--by``: ``category``, ``account`` or ``tag:ROLE``.

    A tag is looked up on the scheduled rule that posted the event, then on
    its income or expense account, so a rule can refine its account's tag.
    """
    if by == "category":
        if side == "expenses":
            return events["subkind"].map(EXPENSE_SUBKIND_TO_CATEGORY).fillna("other")
        # The report's narrower name for the notebooks' "social security".
        categories = (events["subkind"].map(INCOME_SUBKIND_TO_CATEGORY).fillna("other")
                      .replace("social security", "soc security"))
        return categories.mask(events["subkind"] == registries.BUSINESS_SUBKIND, "business")
    if by == "account":
        return events["account"]
    role = by.removeprefix("tag:")
    accounts = rc.items("account").set_index("name")["tags"]
    rules = rc.items("scheduled").set_index("id")["tags"]
    rule_tag = pd.Series(
        [_tag_names(rules.get(int(rid), ""), role) if kind in _RULE_REASONS and pd.notna(rid)
         else None
         for kind, rid in zip(events["reason_kind"], events["reason_id"], strict=True)],
        index=events.index, dtype=object,
    )
    account_tag = events["account"].map(lambda name: _tag_names(accounts.get(name, ""), role))
    return rule_tag.fillna(account_tag).fillna(UNTAGGED)


def group_order(side: FlowSide, by: str, totals: pd.Series) -> list[str]:
    """Categories in their fixed order; accounts and tags largest first, with
    the untagged last."""
    if by == "category":
        return INCOME_REPORT_CATEGORIES if side == "income" else EXPENSE_CATEGORIES
    tagged = totals.drop(UNTAGGED, errors="ignore")
    order = sorted(tagged.index, key=lambda g: (-totals[g], g))
    return [*order, *([UNTAGGED] if UNTAGGED in totals.index else [])]


def shortfall_events(rc: RunConnection) -> pd.DataFrame:
    """Each owned rule's posting that a strategy scaled (#387), as an expense
    event of what it left unspent (budgeted less delivered, negative for a
    raise), against the account the rule pays into: added to the delivered
    expenses it makes the budget, grouped the same way.

    Only a rule paying straight into an expense account is spending, as
    only those postings are delivered expenses: one that saves into an asset
    (a 529) isn't, and one into an interior chain can't say which expense
    its unspent budget would have reached."""
    columns = ["amount", "reason_kind", "reason_id", "account", "subkind", "month"]
    con = rc._con
    if not has_table(con, SPENDING_BASELINE):
        return pd.DataFrame(columns=columns)
    frame = pd.read_sql_query(
        "SELECT b.budgeted_cents - b.delivered_cents AS amount, 'rule' AS reason_kind, "
        "       b.scheduled_transaction_id AS reason_id, c.name AS account, "
        "       COALESCE(a.subkind, '') AS subkind, s.month AS month "
        f"FROM {SPENDING_BASELINE} b JOIN simulation_step s ON s.id = b.step_id "
        "JOIN scheduled_transaction r ON r.id = b.scheduled_transaction_id "
        "JOIN chain c ON c.id = r.to_account JOIN account a ON a.id = c.account_id "
        "WHERE b.budgeted_cents != b.delivered_cents AND a.kind = 'expense'",
        con,
    )
    frame = frame[~frame["account"].isin(NON_CASH_EXPENSE_ACCOUNTS)]
    frame["month"] = pd.to_datetime(frame["month"])
    return frame[columns]


def spending_by_month(rc: RunConnection, *, by: str = "category") -> tuple[pd.DataFrame, pd.DataFrame]:
    """Month x group spending, delivered and budgeted (#387), in nominal
    dollars: ``spending_by_year``'s figures before they're converted and
    summed by year. Both frames have the same months and groups."""
    events = flow_events(rc, "expenses")
    events["group"] = event_groups(rc, events, "expenses", by)
    short = shortfall_events(rc)
    if not short.empty:
        short["group"] = event_groups(rc, short, "expenses", by)

    def monthly(frame: pd.DataFrame) -> pd.DataFrame:
        if frame.empty:
            return pd.DataFrame()
        out = frame.groupby(["month", "group"])["amount"].sum().unstack("group", fill_value=0) / 100
        out.columns.name = None
        return out

    delivered = monthly(events)
    budgeted = delivered.add(monthly(short), fill_value=0) if not short.empty else delivered.copy()
    columns = group_order("expenses", by, budgeted.sum()) if by != "category" else [
        c for c in EXPENSE_CATEGORIES if c in budgeted.columns or c in delivered.columns]
    columns += [c for c in budgeted.columns.union(delivered.columns) if c not in columns]
    index = delivered.index.union(budgeted.index)
    return (delivered.reindex(index=index, columns=columns, fill_value=0),
            budgeted.reindex(index=index, columns=columns, fill_value=0))


def uses_spending_strategies(rc: RunConnection) -> bool:
    """Whether a strategy owned any of the run's scheduled rules: without
    one, the run's spending is its plan."""
    return (has_table(rc._con, SPENDING_BASELINE)
            and rc._con.execute(f"SELECT 1 FROM {SPENDING_BASELINE} LIMIT 1").fetchone() is not None)
