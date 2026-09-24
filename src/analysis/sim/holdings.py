"""Holdings: the lots inside accounts, and which accounts hold a symbol (ADR-005).

Works on either kind of connection. Given a :class:`RunConnection` the values
are the run's month-end balances (the final month unless one is chosen); given
a :class:`ScenarioConnection` they are the opening values.
"""

from __future__ import annotations

import pandas as pd

from analysis.sim.connection import RunConnection, ScenarioConnection
from analysis.sim.types import INVESTMENT_ASSET_SUBKINDS

__all__ = [
    "account_holdings", "holding_accounts", "holding_months", "holding_symbols",
    "holdings_totals", "lots_frame", "render_holders_table", "render_holdings_table",
    "security_names", "symbol_holders",
]

_BALANCE_SHEET_KINDS = ("asset", "liability")
_OPENING = "opening"


def holding_months(source: RunConnection | ScenarioConnection) -> list[str]:
    """Month-end dates a run has lot balances for (empty for a scenario)."""
    if not isinstance(source, RunConnection):
        return []
    return sorted(source.items("lot_balance")["month"].unique())


def _pick_month(months: list[str], month: str | None) -> str:
    if not months:
        raise ValueError("the run has no lot balances")
    if month is None:
        return months[-1]
    matches = [m for m in months if m.startswith(month)]
    if len(matches) != 1:
        raise ValueError(
            f"no month {month!r} in the run (it covers {months[0]} to {months[-1]})"
        )
    return matches[0]


def lots_frame(
    source: RunConnection | ScenarioConnection, month: str | None = None,
) -> pd.DataFrame:
    """One row per balance-sheet lot: account, lot, symbol, name (of the
    security, if securities.csv lists it), acquired, kind, subkind, tax_treatment,
    quantity (shares), price (implied by balance and quantity), balance,
    cost_basis and unrealized_gain (dollars), and ``as_of``.
    """
    members = source.items("account_model_member")
    members = members[members["kind"].isin(_BALANCE_SHEET_KINDS)]
    if isinstance(source, RunConnection):
        balances = source.items("lot_balance")
        chosen = _pick_month(holding_months(source), month)
        at = balances.loc[
            balances["month"] == chosen,
            ["account", "lot", "balance", "cost_basis", "quantity_micros"],
        ]
        frame = members[
            ["account", "lot", "symbol", "acquired", "kind", "subkind", "tax_treatment"]
        ].merge(at, on=["account", "lot"])
        frame["as_of"] = chosen
    else:
        if month is not None:
            raise ValueError("a month needs a run; a scenario only has opening values")
        frame = members.rename(columns={
            "opening_balance": "balance", "opening_cost_basis": "cost_basis",
        })[["account", "lot", "symbol", "acquired", "kind", "subkind", "tax_treatment",
            "balance", "cost_basis", "quantity_micros"]].copy()
        frame["as_of"] = _OPENING
    for column in ("balance", "cost_basis", "quantity_micros"):
        frame[column] = pd.to_numeric(frame[column])
    frame["quantity"] = frame.pop("quantity_micros") / 1_000_000
    frame["balance"] = frame["balance"] / 100
    frame["cost_basis"] = frame["cost_basis"] / 100
    frame["price"] = (frame["balance"] / frame["quantity"]).where(frame["quantity"] > 0)
    frame["unrealized_gain"] = frame["balance"] - frame["cost_basis"]
    frame["name"] = frame["symbol"].map(security_names(source))
    return frame[[
        "account", "lot", "symbol", "name", "acquired", "kind", "subkind", "tax_treatment",
        "quantity", "price", "balance", "cost_basis", "unrealized_gain", "as_of",
    ]].reset_index(drop=True)


def security_names(source: RunConnection | ScenarioConnection) -> dict[str, str]:
    """Symbol to security name, from the data repo's securities.csv."""
    securities = source.items("security")
    return dict(zip(securities["symbol"], securities["name"]))


def account_holdings(
    source: RunConnection | ScenarioConnection, account: str, month: str | None = None,
) -> pd.DataFrame:
    """The lots of one account. Raises ``KeyError`` for an unknown account."""
    frame = lots_frame(source, month)
    held = frame[frame["account"] == account].reset_index(drop=True)
    if held.empty:
        raise KeyError(
            f"no asset or liability account {account!r} "
            f"(accounts with lots: {', '.join(sorted(set(frame['account'])))})"
        )
    return held


def symbol_holders(
    source: RunConnection | ScenarioConnection, symbol: str, month: str | None = None,
) -> pd.DataFrame:
    """Every lot holding ``symbol``, across accounts (empty if none does)."""
    frame = lots_frame(source, month)
    return frame[frame["symbol"] == symbol].reset_index(drop=True)


def _investment(frame: pd.DataFrame) -> pd.DataFrame:
    return frame[frame["subkind"].isin(INVESTMENT_ASSET_SUBKINDS)]


def holding_accounts(source: RunConnection | ScenarioConnection) -> list[str]:
    """Names of the investment accounts, sorted: what the holdings widgets offer."""
    return sorted(set(_investment(lots_frame(source))["account"]))


def holding_symbols(source: RunConnection | ScenarioConnection) -> list[str]:
    """Symbols held in investment accounts, sorted: what the holders widget offers."""
    symbols = _investment(lots_frame(source))["symbol"]
    return sorted(set(symbols[symbols.notna() & (symbols != "")]))


def holdings_totals(frame: pd.DataFrame, *, quantity: bool = False) -> dict[str, float]:
    """Column totals for a holdings frame. Basis (and so gain) is NaN if any lot's
    basis is unknown; ``quantity`` is only meaningful when every lot is one symbol."""
    balance = frame["balance"].sum()
    basis = frame["cost_basis"].sum() if frame["cost_basis"].notna().all() else float("nan")
    totals = {"balance": balance, "cost_basis": basis, "unrealized_gain": balance - basis}
    if quantity:
        totals["quantity"] = frame["quantity"].sum()
    return totals


def _money(value: float, places: int = 2) -> str:
    return "" if pd.isna(value) else f"${value:,.{places}f}"


def _shares(value: float) -> str:
    return "" if pd.isna(value) else (f"{value:,.6f}".rstrip("0").rstrip(".") or "0")


def _text(value: object) -> str:
    """A cell's text; missing values (None or NaN) are blank."""
    return value if isinstance(value, str) else ""


def _table(title: str, columns: list[tuple[str, str]]):
    from rich.table import Table

    table = Table(title=title, header_style="bold", show_lines=False)
    for name, justify in columns:
        table.add_column(name, justify=justify, overflow="ellipsis", no_wrap=True)
    return table


def render_holdings_table(frame: pd.DataFrame, title: str | None = None):
    """A ``rich`` table of one account's lots, with a totals row."""
    table = _table(title, [
        ("Lot", "left"), ("Symbol", "left"), ("Name", "left"), ("Acquired", "left"),
        ("Quantity", "right"), ("Price", "right"), ("Balance", "right"),
        ("Basis", "right"), ("Unrealized", "right"),
    ])
    for row in frame.itertuples():
        table.add_row(
            row.lot, _text(row.symbol), _text(row.name), _text(row.acquired),
            _shares(row.quantity),
            _money(row.price, 4), _money(row.balance), _money(row.cost_basis),
            _money(row.unrealized_gain),
        )
    totals = holdings_totals(frame)
    table.add_row(
        "Total", "", "", "", "", "", _money(totals["balance"]), _money(totals["cost_basis"]),
        _money(totals["unrealized_gain"]), style="bold",
    )
    return table


def render_holders_table(frame: pd.DataFrame, title: str | None = None):
    """A ``rich`` table of the lots holding one symbol, with a totals row."""
    table = _table(title, [
        ("Account", "left"), ("Lot", "left"), ("Tax", "left"), ("Quantity", "right"),
        ("Price", "right"), ("Balance", "right"), ("Basis", "right"), ("Unrealized", "right"),
    ])
    for row in frame.itertuples():
        table.add_row(
            row.account, row.lot, _text(row.tax_treatment), _shares(row.quantity),
            _money(row.price, 4), _money(row.balance), _money(row.cost_basis),
            _money(row.unrealized_gain),
        )
    totals = holdings_totals(frame, quantity=True)
    table.add_row(
        "Total", "", "", _shares(totals["quantity"]), "", _money(totals["balance"]),
        _money(totals["cost_basis"]), _money(totals["unrealized_gain"]), style="bold",
    )
    return table
