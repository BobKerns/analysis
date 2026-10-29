"""Accounts: tabular view over a scenario's intent data.

Pure helpers used by the CLI (``analysis sim account list``) and notebooks.
Reads go through :class:`~analysis.sim.connection.ScenarioConnection` --
account setup (opening balances, growth rates) is intent-side data, not
simulation-run output, so it's available even before a scenario has ever
been run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING
from collections.abc import Collection

import pandas as pd
from rich.console import Console

from analysis.sim import registries
from analysis.sim.types import AccountKind

if TYPE_CHECKING:
    from analysis.sim.connection import RunConnection, ScenarioConnection


def _or_empty(value) -> str:
    """pandas represents a SQL NULL text column as NaN here, not None -- and
    NaN is truthy, so a plain ``value or ""`` doesn't normalize it."""
    return "" if pd.isna(value) else value


@dataclass(frozen=True)
class AccountRow:
    name: str
    kind: str
    subkind: str
    owner: str
    tax_treatment: str
    opened: str
    closed: str
    opening_balance: int | None
    cost_basis: int | None
    rate_series: str | None
    annual_rate_micros: int | None
    rate_role: str | None  # "growth" | "yield" | None


def _rate_role(subkind: str) -> str:
    return "yield" if subkind in registries.CASH_SUBKINDS else "growth"


def _latest_rate_micros(
    sc: ScenarioConnection, series: str, as_of: str | None
) -> int | None:
    """Most recent ``aux_table`` rate row for ``series`` with
    effective_date ≤ as_of (default: today)."""
    cutoff = as_of or date.today().isoformat()
    aux = sc.items("aux_table", kind="rate", series=series)
    if aux.empty:
        return None
    eligible = aux[aux["effective_date"] <= cutoff]
    row = (
        eligible.sort_values("effective_date").iloc[-1] if not eligible.empty
        # Fall back to earliest if none qualify (matches prior CSV behavior).
        else aux.sort_values("effective_date").iloc[0]
    )
    payload = json.loads(row["payload_json"])
    v = payload.get("annual_rate_micros")
    return int(v) if v is not None else None


def _members_by_account(sc: ScenarioConnection) -> pd.DataFrame:
    """Opening balance and cost basis summed over each account's lots. Basis is
    missing if any lot's basis is."""
    members = sc.items("account_lot")
    grouped = members.groupby("account", sort=False)
    return pd.DataFrame({
        "opening_balance": grouped["opening_balance"].sum(),
        "opening_cost_basis": grouped["opening_cost_basis"].sum(min_count=1).where(
            grouped["opening_cost_basis"].count() == grouped["opening_balance"].size()
        ),
    })


def load_accounts_view(
    sc: ScenarioConnection, *, as_of: str | None = None,
) -> list[AccountRow]:
    """Return accounts joined with model opening balances and percent-of-balance rate rules."""
    accounts = sc.items("account")
    members = _members_by_account(sc)
    opening = members["opening_balance"] if not members.empty else None
    basis = members["opening_cost_basis"]


    # Rate series come from percent_of_balance scheduled transactions
    # keyed by to_account (the balance-bearing account, per ADR-003).
    account_name_by_id = accounts.set_index("id")["name"]
    scheduled = sc.items("scheduled")
    rate_rows = scheduled[
        (scheduled["amount_rule_kind"] == "percent_of_balance")
        & scheduled["series"].notna()
    ]
    rate_by_name: dict[str, tuple[str, int | None]] = {}
    for _, row in rate_rows.iterrows():
        account_name = account_name_by_id.get(row["to_account"])
        if account_name is None:
            continue
        rate_by_name[account_name] = (
            row["series"], _latest_rate_micros(sc, row["series"], as_of),
        )

    out: list[AccountRow] = []
    for _, a in accounts.iterrows():
        name = a["name"]
        series, micros = rate_by_name.get(name, (None, None))
        cost_basis = basis.get(name) if basis is not None else None
        out.append(
            AccountRow(
                name=name,
                kind=a["kind"],
                subkind=a["subkind"],
                owner=_or_empty(a["owner"]),
                tax_treatment=_or_empty(a["tax_treatment"]),
                opened=_or_empty(a["opened"]),
                closed=_or_empty(a["closed"]),
                cost_basis=cost_basis / 100.0 if cost_basis is not None else None,
                opening_balance=(
                    None if opening is None else opening.get(name) / 100 if opening.get(name) is not None else None
                ),
                rate_series=series,
                annual_rate_micros=micros,
                rate_role=_rate_role(a["subkind"]) if series else None,
            )
        )
    return out


_ACCOUNTS_COLUMNS = (
    "kind", "subkind", "owner", "tax_treatment",
    "opening_balance", "rate_role", "rate_series", "annual_rate",
    "opened", "closed", "cost_basis",
)
"""``accounts_dataframe``'s columns, in display order (its index is ``name``)."""


def accounts_dataframe(sc: ScenarioConnection, *, as_of: str | None = None):
    """Return ``load_accounts_view`` as a pandas DataFrame for notebook display."""
    rows = load_accounts_view(sc, as_of=as_of)
    if not rows:
        # No accounts (an empty scenario, or an as_of before any opened):
        # the same columns with no rows, so callers can still pick columns.
        return pd.DataFrame(columns=["name", *_ACCOUNTS_COLUMNS]).set_index("name")
    df = pd.DataFrame([r.__dict__ for r in rows]).set_index("name")
    values = _members_by_account(sc)["opening_cost_basis"]
    df["annual_rate"] = df["annual_rate_micros"].apply(
        lambda m: None if m is None else round(m / 10_000, 4)
    )
    df['cost_basis'] = values.reindex(df.index) / 100.0
    return df[list(_ACCOUNTS_COLUMNS)]


def render_accounts_table(rows: list[AccountRow], title: str | None = None):
    """Build a ``rich.table.Table`` of accounts with balances and rates."""
    from rich.table import Table
    table = Table(title=title, header_style="bold", show_lines=False)
    for col, justify in (
        ("Name", "left"), ("Kind", "left"), ("Subkind", "left"),
        ("Owner", "left"), ("Tax", "left"),
        ("Opening", "right"), ("Rate", "right"), ("Series", "left"), ("Role", "left"),
    ):
        table.add_column(col, justify=justify, overflow="fold")
    for r in rows:
        ob = (
            "" if r.opening_balance is None
            else f"${r.opening_balance:,.2f}"
        )
        rate = (
            "" if r.annual_rate_micros is None
            else f"{r.annual_rate_micros/10_000:.2f}%"
        )
        table.add_row(
            r.name, r.kind, r.subkind, r.owner, r.tax_treatment,
            ob, rate, r.rate_series or "", r.rate_role or "",
        )
    return table

def account_summary_by_kind(df: pd.DataFrame) -> pd.DataFrame:
    """Return a summary of opening balances grouped by account kind."""
    return (
            df.dropna(subset=["opening_balance"])
            .groupby("kind", as_index=False)["opening_balance"]
            .sum()
            .sort_values("opening_balance", ascending=False)
        )

def show_account_rows_text(rc: RunConnection, as_of: str | None = None):
    rows = load_accounts_view(rc.scenario, as_of=as_of)
    Console().print(render_accounts_table(rows, title=f"accounts ({rc.data_dir.name})"))


KIND_LABELS: dict[AccountKind, str] = {
    "asset": "Asset", "liability": "Liability",
    "income": "Income", "expense": "Expense",
}
"""Account kinds a balance-history widget can switch between, in dropdown order."""
BALANCE_KINDS: Collection[AccountKind] = ("asset", "liability")
"""Account kinds representing balance sheet kinds."""
CASHFLOW_KINDS: Collection[AccountKind] = ("income", "expense")
"""Account kinds representing cash flow kinds."""
POSITIVE_KINDS: Collection[AccountKind] = ("asset", "income")
"""Account kinds that are considered positive."""
NEGATIVE_KINDS: Collection[AccountKind] = ("liability", "expense")
"""Account kinds that are considered negative."""


def plottable_accounts(
    balance_pivot: pd.DataFrame, account_kinds: pd.Series, kind: str
) -> list[str]:
    """Account names of one exact kind, sorted alphabetically."""
    names = [
        name for name in balance_pivot.columns
        if account_kinds.get(name) == kind
    ]
    names.sort(key=str.casefold)
    return names
