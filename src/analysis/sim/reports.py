"""Income / expense / net-worth aggregation used by the net-worth notebook.

Pure DataFrame transforms over a :class:`~analysis.sim.connection.RunConnection`
(or an already-loaded ``transfer_event`` / ``net_worth`` frame). No plotting,
no I/O beyond what ``RunConnection`` already provides.
"""

from __future__ import annotations

import pandas as pd

from analysis.sim.connection import RunConnection

_INCOME_SUBKIND_TO_CAT: dict[str, str] = {
    "salary": "salary",
    "interest": "investment",
    "dividend": "investment",
    "capital_gain": "investment",
    "ss_benefit": "social security",
}
INCOME_CATEGORIES = ["salary", "social security", "investment"]

_EXPENSE_SUBKIND_TO_CAT: dict[str, str] = {
    "housing": "housing",
    "interest_expense": "housing",
    "taxes_property": "housing",
    "healthcare": "healthcare",
    "taxes_federal": "taxes",
    "taxes_state": "taxes",
    "living": "living",
    "food": "living",
}
EXPENSE_CATEGORIES = ["housing", "healthcare", "taxes", "living", "other"]


def net_worth_series(rc: RunConnection) -> pd.DataFrame:
    """Month-indexed nominal and CPI-constant net worth, in dollars."""
    nw = rc.items("net_worth").rename(columns={"net_worth": "nominal_cents"})
    cpi = rc.items("net_worth_constant_cpi")[["month", "constant_cents"]]
    nw = nw.merge(cpi, on="month", how="left")

    nw["month"] = pd.to_datetime(nw["month"])
    nw["nominal"] = nw["nominal_cents"] / 100
    nw["cpi_constant"] = nw["constant_cents"].astype("Float64") / 100
    return nw.set_index("month")[["nominal", "cpi_constant"]]


def year_end_net_worth(nw: pd.DataFrame) -> pd.DataFrame:
    """Last observation of each year from a month-indexed net-worth frame."""
    return (
        nw.groupby(nw.index.year)
        .tail(1)
        .assign(year=lambda d: d.index.year)
        .set_index("year")[["nominal", "cpi_constant"]]
    )


def income_by_category(rc: RunConnection) -> pd.DataFrame:
    """Monthly income totals by category, in dollars, plus a ``total`` column."""
    xfer = rc.items("transfer_event", from_kind="income").copy()
    xfer["month"] = pd.to_datetime(xfer["month"])
    xfer["category"] = xfer["from_subkind"].map(_INCOME_SUBKIND_TO_CAT).fillna("other")

    income = (
        xfer.groupby(["month", "category"])["amount"].sum()
        .unstack("category", fill_value=0)
        .reindex(columns=INCOME_CATEGORIES, fill_value=0)
        / 100
    )
    income["total"] = income.sum(axis=1)
    return income


def expenses_by_category(rc: RunConnection) -> pd.DataFrame:
    """Monthly expense totals by category, in dollars, plus a ``total`` column."""
    xfer = rc.items("transfer_event", to_kind="expense").copy()
    xfer["month"] = pd.to_datetime(xfer["month"])
    xfer["category"] = xfer["to_subkind"].map(_EXPENSE_SUBKIND_TO_CAT).fillna("other")

    expenses = (
        xfer.groupby(["month", "category"])["amount"].sum()
        .unstack("category", fill_value=0)
        .reindex(columns=EXPENSE_CATEGORIES, fill_value=0)
        / 100
    )
    expenses["total"] = expenses.sum(axis=1)
    return expenses


def yearly_totals(monthly: pd.DataFrame) -> pd.DataFrame:
    """Sum a month-indexed category frame (from ``income_by_category`` /
    ``expenses_by_category``) by calendar year."""
    yearly = monthly.groupby(monthly.index.year).sum()
    yearly.index.name = "year"
    return yearly


def net_income(income_yearly: pd.DataFrame, expenses_yearly: pd.DataFrame) -> pd.DataFrame:
    """Yearly income, expenses, and their difference."""
    net = pd.DataFrame({
        "income": income_yearly["total"],
        "expenses": expenses_yearly["total"],
    })
    net["net"] = net["income"].fillna(0) - net["expenses"].fillna(0)
    return net


def account_balances(rc: RunConnection) -> tuple[pd.DataFrame, pd.Series]:
    """Month x account pivot of account balances (dollars) for a run.

    Returns ``(balance_pivot, account_kinds)`` where ``account_kinds`` maps
    account name to its ``kind`` (e.g. ``asset`` / ``liability``).
    """
    balances = rc.items("account_balance")
    balances["month"] = pd.to_datetime(balances["month"])
    balances["balance"] = balances["balance"] / 100

    balance_pivot = balances.pivot_table(
        index="month", columns="account", values="balance", aggfunc="last"
    )
    account_kinds = balances.drop_duplicates("account").set_index("account")["kind"]
    return balance_pivot, account_kinds
