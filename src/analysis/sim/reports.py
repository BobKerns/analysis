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
from analysis.sim import registries
from analysis.sim.accounts import CASHFLOW_KINDS
from analysis.sim.connection import (
    EXPENSE_CATEGORIES,
    EXPENSE_SUBKIND_TO_CATEGORY,
    INCOME_SUBKIND_TO_CATEGORY,
    NON_CASH_EXPENSE_ACCOUNTS,
    RunConnection,
    event_month,
    year_end_values,
)
from analysis.sim.types import DollarMode

__all__ = [
    "CASH_FLOW", "CASH_INCOME", "FlowSide", "INCOME_REPORT_CATEGORIES", "UNTAGGED",
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

CASH_INCOME = "cash_income"
"""The income report's column of income received into cash accounts (#248)."""

CASH_FLOW = "cash_flow"
"""The income report's column of cash income plus withdrawals into cash (#248)."""

UNTAGGED = "(untagged)"
"""The group of an event with no tag in the ``--by tag:ROLE`` role."""

_RULE_REASONS = ("rule", "ytd_rule")
"""``transfer_event.reason_kind`` values whose ``reason_id`` is a scheduled rule."""


def _cpi(rc: RunConnection, frame, mode: DollarMode, *, annual: bool = False):
    """``frame`` in ``mode`` dollars: month-indexed, or year-indexed with
    ``annual`` (each year's year-end CPI)."""
    if mode != DollarMode.CPI_CONSTANT:
        return frame
    return rc.to_cpi_constant_annual(frame) if annual else rc.to_cpi_constant(frame)


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


def _side(events: pd.DataFrame, prefix: str) -> pd.DataFrame:
    """``events`` with their ``prefix`` side's account name and subkind as
    ``account`` and ``subkind``."""
    return events.rename(columns={f"{prefix}_name": "account", f"{prefix}_subkind": "subkind"})


def _is_cash(events: pd.DataFrame, prefix: str) -> pd.Series:
    """Whether each event's ``prefix`` side is a cash account (checking,
    savings, a business account)."""
    return ((events[f"{prefix}_kind"] == "asset")
            & events[f"{prefix}_subkind"].isin(registries.CASH_SUBKINDS))


def _flow_events(rc: RunConnection, side: FlowSide) -> pd.DataFrame:
    """The run's income (or expense) transfer events with the account each one
    counts against: the income account it is drawn from, or the expense
    account it is paid into. Money paid back into an income account (a
    market drop's negative growth) counts against it, negative.
    Depreciation is a book entry, not spending. Income events also say
    whether the money landed in (or came back from) a ``cash`` account.

    ``month`` is the month end of the event's own date, not of the step that
    posted it: a year-to-date catch-up posts in the first step but is dated
    before the scenario starts, a prior year's one at that year's Dec 31.
    """
    columns = ["date", "amount", "reason_kind", "reason_id", "account", "subkind"]
    if side == "income":
        received = rc.items("transfer_event", from_kind="income")
        received = _side(received, "from").assign(cash=_is_cash(received, "to"))
        # A negative growth rate posts the other way, asset into its growth
        # account (#248): a market drop takes back that year's gains.
        # An income-to-income leg (a year-to-date record) is counted once, above.
        returned = rc.items("transfer_event", to_kind="income")
        returned = returned[returned["from_kind"] != "income"]
        returned = _side(returned, "to").assign(cash=_is_cash(returned, "from"))
        returned["amount"] = -returned["amount"]
        events = pd.concat([received[[*columns, "cash"]], returned[[*columns, "cash"]]],
                           ignore_index=True)
    else:
        events = rc.items("transfer_event", to_kind="expense")
        events = _side(events[~events["to_name"].isin(NON_CASH_EXPENSE_ACCOUNTS)], "to")[columns]
    events = events.copy()
    events["month"] = event_month(events)
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
    """Year x group income (or expense) totals in dollars, plus a ``total`` column
    and, for income, ``cash_income``: the part of the total that was paid
    into a cash account (paychecks, Social Security, interest into checking),
    leaving out paper growth and money deferred into retirement accounts;
    and ``cash_flow``, that plus what was withdrawn into cash from other
    assets (sales, IRA and 401k withdrawals). Neither is part of ``total``.

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
    cash_columns = [CASH_INCOME, CASH_FLOW] if side == "income" else []
    if side == "income":
        cash = events[events["cash"]].groupby("month")["amount"].sum() / 100
        withdrawn = _withdrawals(rc)
        monthly = monthly.reindex(monthly.index.union(withdrawn.index), fill_value=0)
        monthly[CASH_INCOME] = cash.reindex(monthly.index, fill_value=0)
        monthly[CASH_FLOW] = monthly[CASH_INCOME] + withdrawn.reindex(monthly.index, fill_value=0)
    monthly = _cpi(rc, monthly, mode)
    yearly = monthly.groupby(index_years(monthly)).sum()
    yearly.index.name = "year"
    cash_totals = yearly[cash_columns]
    yearly = yearly.drop(columns=cash_columns)
    yearly = yearly.reindex(columns=_group_order(side, by, yearly.sum()), fill_value=0)
    yearly.columns.name = None
    yearly["total"] = yearly.sum(axis=1)
    return yearly.join(cash_totals)


def _withdrawals(rc: RunConnection) -> pd.Series:
    """Monthly dollars moved into cash accounts out of other assets: sales and
    withdrawals from brokerage, retirement and other holdings. Not moves
    between cash accounts, loan proceeds, or money put back."""
    events = rc.items("transfer_event", from_kind="asset", to_kind="asset")
    events = events[_is_cash(events, "to") & ~_is_cash(events, "from")]
    return events.groupby(event_month(events))["amount"].sum() / 100


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
    amounts = yearly.loc[year].drop([CASH_INCOME, CASH_FLOW], errors="ignore")
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
    """Federal and state tax by year, with the income each was figured on.

    For each of federal and state: ``agi``, the larger of the standard and
    itemized ``deduction``, the ``taxable_income`` its brackets taxed, and the
    ``tax``. Federal AGI is Form 1040 line 11, except that a carried loss
    comes off taxable income instead and there are no adjustments such as half
    of self-employment tax; state AGI leaves out Social Security. The state
    columns appear only when the run computes state tax. ``effective_rate``
    is total tax over federal AGI (``fed_agi``). Liability belongs to the tax year the
    income was earned in, whatever year it is paid or refunded. CPI-constant
    dollars use each year's year-end CPI, as the notebook does; the rate is
    the same in either.
    """
    frame = _tax_buckets(rc, list(_TAX_COLUMNS)).rename(columns=_TAX_COLUMNS)
    if frame["state_tax"].isna().all():
        frame = frame.drop(columns=[c for c in frame.columns if c.startswith("state_")])
    # A scenario may compute federal tax and not state tax: a blank is "not
    # computed", so the total is blank only when neither is.
    taxes = [c for c in ("fed_tax", "state_tax") if c in frame.columns]
    frame["total_tax"] = frame[taxes].sum(axis=1, min_count=1)
    # A business (#231) adds its federal Schedule C result, the expenses a
    # hobby couldn't deduct, and the loss carried forward: each only when
    # some year has one, since most scenarios have none of them.
    business = _tax_buckets(rc, list(_BUSINESS_COLUMNS)).rename(columns=_BUSINESS_COLUMNS)
    business = business.reindex(frame.index)
    frame = frame.join(business.loc[:, business.fillna(0).ne(0).any()])
    frame.index.name = "year"
    frame = _cpi(rc, frame, mode, annual=True)
    agi = frame["fed_agi"].where(frame["fed_agi"] > 0)
    frame.insert(frame.columns.get_loc("total_tax") + 1, "effective_rate",
                 frame["total_tax"] / agi)
    return frame


_SCOPE_PREFIX = {"federal": "fed", "state": "state"}
"""Column prefixes: ``fed`` keeps the wide tax tables narrower."""

_TAX_COLUMNS = {
    f"{scope}_{bucket}": f"{_SCOPE_PREFIX[scope]}_{column}"
    for scope in ("federal", "state")
    for bucket, column in (
        ("adjusted_gross_income", "agi"),
        ("deduction_used", "deduction"),
        ("taxable_income", "taxable_income"),
        ("tax_liability", "tax"),
    )
}


_BUSINESS_COLUMNS = {
    "federal_schedule_c_net": "business_net",
    "federal_hobby_expense_disallowed": "hobby_disallowed",
    "federal_nol_carryover": "nol_carryover",
}


_RECONCILIATION_COLUMNS = {
    "federal_tax_liability": "fed_liability",
    "federal_tax_paid": "fed_paid",
    "federal_tax_due": "fed_due",
    "federal_tax_overpaid": "fed_refund",
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


_PERCENT_COLUMNS = frozenset({"share", "effective_rate"})


def _cell(value: float, column: str) -> str:
    if pd.isna(value):
        return ""
    if column in _PERCENT_COLUMNS:
        return f"{value:.1%}"
    if column == "quantity":
        return f"{value:,.6f}".rstrip("0").rstrip(".") or "0"
    return f"-${-value:,.0f}" if round(value) < 0 else f"${value:,.0f}"


def render_report_table(frame: pd.DataFrame, title: str | None = None, *,
                        total_row: bool = False):
    """A ``rich`` table of a report frame: its index first, then its columns,
    money in whole dollars, ``share`` and ``effective_rate`` as percents and ``quantity`` in shares.
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
