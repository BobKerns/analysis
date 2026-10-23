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
    "CASH_FLOW", "CASH_INCOME", "DEFERRED_TAX_CHANGE", "ECONOMIC_INCOME", "FlowSide",
    "INCOME_REPORT_CATEGORIES", "NET_WORTH_CHANGE", "NET_WORTH_COLUMNS", "UNTAGGED",
    "account_history", "flows_by_year", "flows_for_year", "net_worth_change_by_year",
    "gains_by_year", "tax_by_year", "tax_reconciliation",
    "render_report_table",
]

type FlowSide = Literal["income", "expenses"]

INCOME_REPORT_CATEGORIES = ["salary", "soc security", "investment", "business", "other"]
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
        received = _side(received, "from").assign(cash=_is_cash(received, "to"))
        received.loc[received["tax_character"] == registries.BUSINESS_INCOME,
                     "subkind"] = registries.BUSINESS_SUBKIND
        # A negative growth rate posts the other way, asset into its growth
        # account (#248): a market drop takes back that year's gains.
        # An income-to-income leg (a year-to-date record) is counted once, above.
        returned = rc.items("transfer_event", to_kind="income")
        returned = returned[returned["from_kind"] != "income"]
        returned = _side(returned, "to").assign(cash=_is_cash(returned, "from"))
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


def _groups(rc: RunConnection, events: pd.DataFrame, side: FlowSide, by: str) -> pd.Series:
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
    mode: DollarMode = DollarMode.NOMINAL, untagged: bool = False, after_tax: bool = True,
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

    By tag, flows with no tag in ROLE are left out, of ``total`` and
    ``cash_income`` too, since they don't belong to anything the tag
    measures; ``cash_flow`` is dropped, as its withdrawals carry no tag.
    ``untagged=True`` keeps them as an ``(untagged)`` group, counted in every
    total.

    Income that tells the whole story (not just the tagged part) names its
    total ``economic_income`` and follows it, before the cash columns, with
    :func:`net_worth_change_by_year`'s columns down to ``Δnet_worth``,
    after tax unless ``after_tax=False``.
    """
    events = _flow_events(rc, side)
    events["group"] = _groups(rc, events, side, by)
    tagged_only = by.startswith("tag:") and not untagged
    months = pd.Index(events["month"].unique()).sort_values()
    if tagged_only:
        events = events[events["group"] != UNTAGGED]
    # Keep every year with flows, so a year with none tagged reads as zero.
    monthly = (events.groupby(["month", "group"])["amount"].sum()
               .unstack("group", fill_value=0) / 100)
    monthly = monthly.reindex(monthly.index.union(months), fill_value=0)
    cash_columns = []
    if side == "income":
        cash_columns.append(CASH_INCOME)
        cash = events[events["cash"]].groupby("month")["amount"].sum() / 100
        monthly = monthly.reindex(monthly.index.union(cash.index), fill_value=0)
        monthly[CASH_INCOME] = cash.reindex(monthly.index, fill_value=0)
        if not tagged_only:
            cash_columns.append(CASH_FLOW)
            withdrawn = _withdrawals(rc)
            monthly = monthly.reindex(monthly.index.union(withdrawn.index), fill_value=0)
            monthly[CASH_FLOW] = (monthly[CASH_INCOME]
                                  + withdrawn.reindex(monthly.index, fill_value=0))
    monthly = _cpi(rc, monthly, mode)
    yearly = monthly.groupby(index_years(monthly)).sum()
    yearly.index.name = "year"
    cash_totals = yearly[cash_columns]
    yearly = yearly.drop(columns=cash_columns)
    yearly = yearly.reindex(columns=_group_order(side, by, yearly.sum()), fill_value=0)
    yearly.columns.name = None
    yearly["total"] = yearly.sum(axis=1)
    if side == "income" and not tagged_only:
        # The whole story (#249): the total is economic income, carried on
        # down to the change in net worth.
        yearly = yearly.rename(columns={"total": ECONOMIC_INCOME})
        yearly = yearly.join(net_worth_change_by_year(
            rc, yearly[ECONOMIC_INCOME], mode=mode, after_tax=after_tax))
    return yearly.join(cash_totals)


def _withdrawals(rc: RunConnection) -> pd.Series:
    """Monthly dollars moved into cash accounts out of other assets: sales and
    withdrawals from brokerage, retirement and other holdings. Not moves
    between cash accounts, loan proceeds, or money put back."""
    events = rc.items("transfer_event", from_kind="asset", to_kind="asset")
    events = events[_is_cash(events, "to") & ~_is_cash(events, "from")]
    return events.groupby(event_month(events))["amount"].sum() / 100


ECONOMIC_INCOME = "economic_income"
"""The income report's total when it tells the whole story (#249): income plus
paper gains, net of losses, i.e. Haig-Simons income before tax."""

DEFERRED_TAX_CHANGE = "Δdeferred_tax"
"""The income report's change in tax still owed on what has grown (#249)."""

NET_WORTH_CHANGE = "Δnet_worth"
"""The income report's change in net worth over the year (#249)."""

NET_WORTH_COLUMNS = [
    "tax", "tax_paid", DEFERRED_TAX_CHANGE, "after_tax_income",
    "spending", "inflation", NET_WORTH_CHANGE,
]
"""The income report's columns from economic income down to the change in
net worth (#249), in order. A report shows only those its mode has: ``tax``
and ``Δdeferred_tax`` after tax, ``tax_paid`` before it, ``inflation`` in
CPI-constant dollars."""


def net_worth_change_by_year(
    rc: RunConnection, economic_income: pd.Series, *,
    mode: DollarMode = DollarMode.NOMINAL, after_tax: bool = True,
) -> pd.DataFrame:
    """Year by year, how ``economic_income`` (the income report's total, in
    ``mode`` dollars) becomes the change in net worth (#249)::

        economic_income - tax - Δdeferred_tax = after_tax_income
        after_tax_income - spending - inflation = Δnet_worth

    This is Haig-Simons accounting: income is what the year added before
    spending, gains counted as they accrue and tax as it is owed. After tax
    (the default, like ``sim networth``), ``tax`` is the year's federal and
    state liability, whenever it is paid (in a year no tax rule computes, what
    was paid), and ``Δdeferred_tax`` the
    change in the tax still owed on traditional balances and unrealized
    gains; net worth is after-tax net worth (#251). Before tax, the tax line
    is ``tax_paid`` (what was paid in the year, mostly last year's balance
    and this year's withholding), there is no deferred line, and net worth is
    the sum of the balances.

    ``spending`` is every expense but income tax, depreciation and business
    expenses: a vehicle's lost value is a loss in economic income, like a
    market drop, and a business's costs come off its income there.
    ``inflation``, in
    CPI-constant dollars only, is what inflation took from the real value of
    what was held: each month's flows are deflated as they happen, while net
    worth loses value just by sitting still.

    ``Δnet_worth`` is measured from net worth itself, so each row adds
    up only because every change in net worth is a flow on it. The first
    year runs from its Jan 1, worked back from the opening balances and the
    year-to-date rows dated before the scenario starts. Years before that
    (a prior year's carried-forward record) have no row.
    """
    expenses = rc.items("transfer_event", to_kind="expense").copy()
    expenses["month"] = event_month(expenses)
    is_depreciation = expenses["to_name"].isin(NON_CASH_EXPENSE_ACCOUNTS)
    category = expenses["to_subkind"].map(EXPENSE_SUBKIND_TO_CATEGORY)
    expenses["line"] = "spending"
    expenses.loc[category == "taxes", "line"] = "tax_paid"
    # Both are already in economic income, as losses (see _flow_events).
    expenses.loc[category == "business", "line"] = "economic_income"
    expenses.loc[is_depreciation, "line"] = "economic_income"
    monthly = (expenses.groupby(["month", "line"])["amount"].sum()
               .unstack("line", fill_value=0)
               .reindex(columns=["tax_paid", "spending"], fill_value=0) / 100)
    worth = rc.after_tax_net_worth_frame() if after_tax else rc.net_worth_frame()
    nw = worth["nominal"].astype(float)
    if nw.empty:
        # An empty scenario (no accounts) has no net worth to follow.
        return pd.DataFrame(index=economic_income.index, columns=[NET_WORTH_CHANGE],
                            dtype=float)
    first_month = nw.index.min()
    first_year = first_month.year

    # Net worth before the first step: its month end less what that step
    # posted, back to Jan 1 of the first year (a year-to-date row dated
    # before the start posts in the first step). A prior year's record
    # (income to income, dated an earlier Dec 31) moves no money.
    first_posted = rc.items("transfer_event")
    first_posted = first_posted[pd.to_datetime(first_posted["month"]) == first_month]
    in_year = pd.to_datetime(first_posted["date"]).dt.year == first_year
    posted_ids = set(first_posted.loc[in_year, "id"])
    income_rows = rc.items("transfer_event", from_kind="income")
    income_first = income_rows[income_rows["id"].isin(posted_ids)]["amount"].sum()
    returned = rc.items("transfer_event", to_kind="income")
    returned = returned[(returned["from_kind"] != "income") & returned["id"].isin(posted_ids)]
    spent_first = expenses[expenses["id"].isin(posted_ids)]["amount"].sum()
    opening = nw.iloc[0] - (income_first - returned["amount"].sum() - spent_first) / 100
    year_end = year_end_values(nw)
    if mode == DollarMode.CPI_CONSTANT:
        monthly = rc.to_cpi_constant(monthly)
        real = rc.to_cpi_constant(nw)
        opening = opening * float(rc.to_cpi_constant(pd.Series([1.0], index=[first_month])).iloc[0])
        year_end = year_end_values(real)
    years = year_end.index
    frame = monthly.groupby(index_years(monthly)).sum().reindex(years, fill_value=0)
    frame.index.name = "year"
    starts = year_end.shift(1)
    starts.iloc[0] = opening
    change = year_end - starts

    income = economic_income.reindex(years).fillna(0)
    columns: dict[str, pd.Series] = {}
    if after_tax:
        # A year no tax rule computes (a scenario without one) owes what it
        # paid: there is no liability to accrue instead.
        liability = _cpi(rc, rc.tax_liability_by_year().reindex(years), mode, annual=True)
        liability = liability.fillna(frame["tax_paid"])
        deferred = worth["deferred_tax"].astype(float)
        if mode == DollarMode.CPI_CONSTANT:
            deferred = rc.to_cpi_constant(deferred)
        deferred_end = year_end_values(deferred)
        deferred_start = deferred_end.shift(1)
        deferred_start.iloc[0] = deferred.iloc[0]
        columns["tax"] = liability
        columns[DEFERRED_TAX_CHANGE] = deferred_end - deferred_start
        columns["after_tax_income"] = income - columns["tax"] - columns[DEFERRED_TAX_CHANGE]
        kept = columns["after_tax_income"]
    else:
        columns["tax_paid"] = frame["tax_paid"]
        kept = income - frame["tax_paid"]
    columns["spending"] = frame["spending"]
    if mode == DollarMode.CPI_CONSTANT:
        columns["inflation"] = kept - frame["spending"] - change
    columns[NET_WORTH_CHANGE] = change
    return pd.DataFrame(columns, index=years).astype(float)


def flows_for_year(
    rc: RunConnection, side: FlowSide, year: int, *, by: str = "category",
    mode: DollarMode = DollarMode.NOMINAL, untagged: bool = False,
) -> pd.DataFrame:
    """One year of ``flows_by_year`` turned on its side: a row per group with
    its ``amount`` and ``share`` of the year's total (0-1), and a total row.
    Accounts and tags are ordered by that year's amounts, and those with none
    that year are left out; categories keep their fixed rows. ``untagged`` is
    as for ``flows_by_year``.

    Raises ``KeyError`` for a year the run doesn't cover.
    """
    yearly = flows_by_year(rc, side, by=by, mode=mode, untagged=untagged)
    yearly = yearly.rename(columns={ECONOMIC_INCOME: "total"})
    if year not in yearly.index:
        years = f"{yearly.index.min()}-{yearly.index.max()}" if len(yearly) else "none"
        raise KeyError(f"the run has no {side} in {year} (years: {years})")
    amounts = yearly.loc[year].drop([CASH_INCOME, CASH_FLOW, *NET_WORTH_COLUMNS], errors="ignore")
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

    For each of federal (``fed_``) and state (``state_``): ``agi``, the
    larger of the standard and itemized ``deduction``, ``income`` (the taxable
    income its brackets taxed), and ``tax``. Federal AGI is Form 1040 line
    11, except that a carried loss comes off taxable income instead and there
    are no adjustments such as half of self-employment tax; state AGI leaves
    out Social Security. The state columns appear only when the run computes
    state tax. A run that charges IRMAA (#253) adds ``fed_magi`` and
    ``irmaa_tier``, the tier that year's MAGI set for the premiums two years
    later (0 below the first), blank for a year whose premiums the run ends
    before charging. ``effective_rate`` is total tax over federal AGI. Liability
    belongs to the tax year the income was earned in, whatever year it is
    paid or refunded. CPI-constant dollars use each year's year-end CPI, as
    the notebook does; the rate is the same in either.
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
    irmaa = _tax_buckets(rc, ["federal_magi", "federal_irmaa_tier"]).reindex(frame.index)
    charged = irmaa["federal_irmaa_tier"].notna().any()
    if charged:
        frame.insert(frame.columns.get_loc("fed_agi") + 1, "fed_magi", irmaa["federal_magi"])
    frame.index.name = "year"
    frame = _cpi(rc, frame, mode, annual=True)
    if charged:
        # A tier number, not money: _tax_buckets' /100 undone, and after the
        # CPI conversion so it isn't deflated.
        frame.insert(frame.columns.get_loc("fed_magi") + 1, "irmaa_tier",
                     irmaa["federal_irmaa_tier"] * 100)
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
        ("taxable_income", "income"),
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
    if column == "irmaa_tier":
        return f"{value:.0f}"
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
