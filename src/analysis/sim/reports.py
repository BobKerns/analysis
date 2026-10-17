"""Result reports for the terminal (#187): an account's balance history, and
income, expenses, gains and tax by year, read from one run.

The same numbers as ``accounts.ipynb`` and ``net_worth.ipynb``: every frame is
built from :class:`~analysis.sim.connection.RunConnection`'s derived data, in
nominal or CPI-constant dollars. The ``render_*`` functions turn a frame into a
``rich`` table; the ``sim account history`` and ``sim report`` commands print
them.
"""

from __future__ import annotations

from typing import Literal

import pandas as pd

from analysis.frames import index_years
from analysis.sim.accounts import CASHFLOW_KINDS
from analysis.sim.connection import (
    EXPENSE_CATEGORIES,
    EXPENSE_SUBKIND_TO_CATEGORY,
    INCOME_SUBKIND_TO_CATEGORY,
    NON_CASH_EXPENSE_ACCOUNTS,
    RunConnection,
    year_end_values,
)
from analysis.sim.types import DollarMode

__all__ = [
    "FlowSide", "INCOME_REPORT_CATEGORIES", "UNTAGGED",
    "account_history", "flows_by_year", "flows_for_year",
    "gains_by_year", "tax_by_year", "tax_reconciliation",
    "render_report_table",
]

type FlowSide = Literal["income", "expenses"]

INCOME_REPORT_CATEGORIES = ["salary", "social security", "investment", "other"]
"""Income categories in report order. Unlike ``income_by_category``, which keeps
only the first three, the report keeps ``other`` (pensions, rentals,
inheritances): a total that leaves them out doesn't add up to the money that
came in."""

UNTAGGED = "(untagged)"
"""The group of an event with no tag in the ``--by tag:ROLE`` role."""

_RULE_REASONS = ("rule", "ytd_rule")
"""``transfer_event.reason_kind`` values whose ``reason_id`` is a scheduled rule."""


def _cpi(rc: RunConnection, frame, mode: DollarMode, *, annual: bool = False):
    """``frame`` in ``mode`` dollars: month-indexed, or year-indexed with
    ``annual`` (each year's year-end CPI). A month or year before the run's
    first, such as a year-to-date row's, takes the first one's CPI: the run
    has no index for earlier ones."""
    if mode != DollarMode.CPI_CONSTANT:
        return frame
    cpi = rc.cpi_index().dropna()
    if annual:
        cpi = cpi.groupby(index_years(cpi)).last()
    lookup = pd.Series(frame.index).clip(cpi.index.min(), cpi.index.max())
    return frame.mul(1_000_000 / cpi.reindex(lookup).to_numpy(), axis=0)


def account_history(
    rc: RunConnection, account: str, *, lot: str | None = None,
    start: str | None = None, end: str | None = None, yearly: bool = False,
    mode: DollarMode = DollarMode.NOMINAL,
) -> pd.DataFrame:
    """One account's (or one lot's) month-end balance, and its change since the
    month (or year) before, indexed by month end (or by year with ``yearly``).

    A lot of an asset account adds its cost basis, unrealized gain and
    quantity where the run tracks them; an account-wide basis or quantity
    would mix unlike lots, so the account itself shows only its balance.
    Income and expense balances are cumulative totals, so their change is
    the period's flow. ``start``/``end`` are month-end dates, inclusive.

    Raises ``KeyError`` for an account or lot the run doesn't have.
    """
    kinds = rc.account_kinds()
    if account not in kinds.index:
        raise KeyError(f"unknown account in run: {account!r}")
    kind = kinds[account]
    if lot is None:
        balance = rc.account_balance(account, mode)
    else:
        lots = rc.lots_by_account(single=True).get(account, [])
        if lot not in lots:
            known = f" (lots: {', '.join(lots)})" if lots else ""
            raise KeyError(f"unknown lot {lot!r} of account {account!r}{known}")
        balance = rc.lot_balance(account, lot, mode)
    frame = pd.DataFrame({"balance": balance})
    if kind in CASHFLOW_KINDS:
        frame["change"] = balance.diff().fillna(balance)
    else:
        frame["change"] = balance.diff()
    if lot is not None and kind == "asset":
        label = f"{account}/{lot}"
        for measure in ("cost_basis", "quantity"):
            pivot = (rc.cpi_lot_balance_pivot(measure) if mode == DollarMode.CPI_CONSTANT
                     else rc.lot_balance_pivot(measure))
            if label in pivot.columns and pivot[label].notna().any():
                frame[measure] = pivot[label].reindex(frame.index)
        if "cost_basis" in frame.columns:
            frame.insert(frame.columns.get_loc("cost_basis") + 1, "unrealized",
                         frame["balance"] - frame["cost_basis"])
    if start is not None:
        frame = frame[frame.index >= pd.Timestamp(start)]
    if end is not None:
        frame = frame[frame.index <= pd.Timestamp(end)]
    if yearly:
        changes = frame["change"].groupby(index_years(frame)).sum(min_count=1)
        frame = year_end_values(frame)
        frame["change"] = changes
        frame.index.name = "year"
    else:
        frame.index = pd.DatetimeIndex(frame.index).strftime("%Y-%m")
        frame.index.name = "month"
    return frame


def _tag_names(tags: str, role: str) -> str | None:
    """The names of ``tags``' tokens in ``role``, joined with ``+``; None if none."""
    names = sorted(token.partition(":")[2] for token in tags.split()
                   if token.partition(":")[0] == role)
    return "+".join(names) if names else None


def _flow_events(rc: RunConnection, side: FlowSide) -> pd.DataFrame:
    """The run's income (or expense) transfer events with the account each one
    counts against: the income account it is drawn from, or the expense
    account it is paid into. Depreciation is a book entry, not spending.

    ``month`` is the month end of the event's own date, not of the step that
    posted it: a year-to-date catch-up posts in the first step but is dated
    before the scenario starts, a prior year's one at that year's Dec 31.
    """
    if side == "income":
        events = rc.items("transfer_event", from_kind="income")
        prefix = "from"
    else:
        events = rc.items("transfer_event", to_kind="expense")
        events = events[~events["to_name"].isin(NON_CASH_EXPENSE_ACCOUNTS)]
        prefix = "to"
    events = events.rename(columns={f"{prefix}_name": "account", f"{prefix}_subkind": "subkind"})
    events = events[["date", "amount", "reason_kind", "reason_id", "account", "subkind"]].copy()
    events["month"] = pd.to_datetime(events["date"]) + pd.offsets.MonthEnd(0)
    return events.drop(columns="date")


def _groups(rc: RunConnection, events: pd.DataFrame, side: FlowSide, by: str) -> pd.Series:
    """Each event's group for ``--by``: ``category``, ``account`` or ``tag:ROLE``.

    A tag is looked up on the scheduled rule that posted the event, then on
    its income or expense account, so a rule can refine its account's tag.
    """
    if by == "category":
        mapping = INCOME_SUBKIND_TO_CATEGORY if side == "income" else EXPENSE_SUBKIND_TO_CATEGORY
        return events["subkind"].map(mapping).fillna("other")
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


def _group_order(side: FlowSide, by: str, totals: pd.Series) -> list[str]:
    """Categories in their fixed order; accounts and tags largest first, with
    the untagged last."""
    if by == "category":
        return INCOME_REPORT_CATEGORIES if side == "income" else EXPENSE_CATEGORIES
    tagged = totals.drop(UNTAGGED, errors="ignore")
    order = sorted(tagged.index, key=lambda g: (-totals[g], g))
    return [*order, *([UNTAGGED] if UNTAGGED in totals.index else [])]


def flows_by_year(
    rc: RunConnection, side: FlowSide, *, by: str = "category",
    mode: DollarMode = DollarMode.NOMINAL,
) -> pd.DataFrame:
    """Year x group income (or expense) totals in dollars, plus a ``total`` column.

    ``by`` is ``category`` (the notebooks' buckets, keeping ``other``),
    ``account``, or ``tag:ROLE`` (each event's tag in ROLE, from its rule or
    else its account). Each event counts in the year of its own date. CPI-constant
    dollars convert each month's flow before summing the year, as the
    notebooks do.
    """
    events = _flow_events(rc, side)
    events["group"] = _groups(rc, events, side, by)
    monthly = (events.groupby(["month", "group"])["amount"].sum()
               .unstack("group", fill_value=0) / 100)
    monthly = _cpi(rc, monthly, mode)
    yearly = monthly.groupby(index_years(monthly)).sum()
    yearly.index.name = "year"
    yearly = yearly.reindex(columns=_group_order(side, by, yearly.sum()), fill_value=0)
    yearly.columns.name = None
    yearly["total"] = yearly.sum(axis=1)
    return yearly


def flows_for_year(
    rc: RunConnection, side: FlowSide, year: int, *, by: str = "category",
    mode: DollarMode = DollarMode.NOMINAL,
) -> pd.DataFrame:
    """One year of ``flows_by_year`` turned on its side: a row per group with
    its ``amount`` and ``share`` of the year's total (0-1), and a total row.
    Accounts and tags are ordered by that year's amounts, and those with none
    that year are left out; categories keep their fixed rows.

    Raises ``KeyError`` for a year the run doesn't cover.
    """
    yearly = flows_by_year(rc, side, by=by, mode=mode)
    if year not in yearly.index:
        years = f"{yearly.index.min()}-{yearly.index.max()}" if len(yearly) else "none"
        raise KeyError(f"the run has no {side} in {year} (years: {years})")
    amounts = yearly.loc[year]
    total = amounts.pop("total")
    if by != "category":
        # This year's own order, and only the groups with money in it.
        amounts = amounts[amounts != 0]
        amounts = amounts.loc[_group_order(side, by, amounts)]
    amounts["total"] = total
    frame = pd.DataFrame({"amount": amounts})
    frame["share"] = amounts / total if total else float("nan")
    frame.index.name = by.removeprefix("tag:")
    return frame


def gains_by_year(rc: RunConnection, mode: DollarMode = DollarMode.NOMINAL) -> pd.DataFrame:
    """``RunConnection.gains_by_category``: realized (taxable and non-taxable)
    vs. unrealized gains by year, with that year's expenses and how far
    realized gains cover them."""
    return _cpi(rc, rc.gains_by_category(), mode, annual=True)


def _tax_buckets(rc: RunConnection, buckets: list[str]) -> pd.DataFrame:
    summary = rc.items("tax_year_summary")
    summary = summary[summary["bucket"].isin(buckets)]
    return (summary.pivot_table(index="year", columns="bucket", values="amount", aggfunc="sum")
            .reindex(columns=buckets) / 100)


def tax_by_year(rc: RunConnection, mode: DollarMode = DollarMode.NOMINAL) -> pd.DataFrame:
    """Federal and state tax liability by year, and the after-tax income they leave.

    ``gross_income`` is the income report's total; ``non_tax_expenses`` leaves
    out the tax categories, so tax isn't counted twice in
    ``after_tax_surplus``. Liability belongs to the tax year the income was
    earned in, whatever year it is paid or refunded. A year with no computed
    tax (a run with no tax rules) leaves the tax columns blank. CPI-constant
    dollars use each year's year-end CPI, as the notebook does.
    """
    tax = _tax_buckets(rc, ["federal_tax_liability", "state_tax_liability"]).rename(columns={
        "federal_tax_liability": "federal_tax", "state_tax_liability": "state_tax",
    })
    income = flows_by_year(rc, "income")["total"]
    expenses = flows_by_year(rc, "expenses")
    frame = pd.DataFrame({
        "federal_tax": tax["federal_tax"],
        "state_tax": tax["state_tax"],
    }).reindex(income.index.union(tax.index).union(expenses.index))
    # A scenario may compute federal tax and not state tax: a blank is "not
    # computed", so the total is blank only when neither is.
    frame["total_tax"] = frame[["federal_tax", "state_tax"]].sum(axis=1, min_count=1)
    frame["gross_income"] = income.reindex(frame.index).fillna(0)
    frame["after_tax_income"] = frame["gross_income"] - frame["total_tax"]
    frame["non_tax_expenses"] = (expenses["total"] - expenses["taxes"]).reindex(frame.index).fillna(0)
    frame["after_tax_surplus"] = frame["after_tax_income"] - frame["non_tax_expenses"]
    frame.index.name = "year"
    return _cpi(rc, frame, mode, annual=True)


_RECONCILIATION_COLUMNS = {
    "federal_tax_liability": "federal_liability",
    "federal_tax_paid": "federal_paid",
    "federal_tax_due": "federal_due",
    "federal_tax_overpaid": "federal_refund",
    "state_tax_liability": "state_liability",
    "state_tax_paid": "state_paid",
    "state_tax_due": "state_due",
    "state_tax_overpaid": "state_refund",
}


def tax_reconciliation(rc: RunConnection, mode: DollarMode = DollarMode.NOMINAL) -> pd.DataFrame:
    """Each tax year's liability, what was withheld or paid during it, and the
    balance due or refunded at the following April's reconciliation."""
    frame = _tax_buckets(rc, list(_RECONCILIATION_COLUMNS)).rename(columns=_RECONCILIATION_COLUMNS)
    return _cpi(rc, frame.fillna(0), mode, annual=True)


def _cell(value: float, column: str) -> str:
    if pd.isna(value):
        return ""
    if column == "share":
        return f"{value:.1%}"
    if column == "quantity":
        return f"{value:,.6f}".rstrip("0").rstrip(".") or "0"
    return f"-${-value:,.0f}" if round(value) < 0 else f"${value:,.0f}"


def render_report_table(frame: pd.DataFrame, title: str | None = None, *,
                        total_row: bool = False):
    """A ``rich`` table of a report frame: its index first, then its columns,
    money in whole dollars, ``share`` as a percent and ``quantity`` in shares.
    ``total_row`` bolds the last row (a column sum the caller appended)."""
    from rich.table import Table

    table = Table(title=title, header_style="bold")
    table.add_column(str(frame.index.name or ""), justify="left", no_wrap=True)
    for column in frame.columns:
        table.add_column(str(column), justify="right", no_wrap=True)
    last = len(frame) - 1
    for position, (label, row) in enumerate(frame.iterrows()):
        table.add_row(str(label), *(_cell(float(row[c]), str(c)) for c in frame.columns),
                      style="bold" if total_row and position == last else None)
    return table
