"""Holdings notebook widgets: the lots in one account, and the accounts holding
one symbol. Both take a run (month-end balances) or a scenario (opening values),
and only offer investment accounts.
"""

from __future__ import annotations

import pandas as pd
from IPython.display import Markdown, display
from ipywidgets import widgets

from analysis.sim.connection import RunConnection, ScenarioConnection
from analysis.sim.holdings import (
    account_holdings,
    holding_accounts,
    holding_months,
    holding_symbols,
    holdings_totals,
    lots_frame,
    security_names,
)
from analysis.sim.types import INVESTMENT_ASSET_SUBKINDS

__all__ = [
    "account_holdings_view", "account_holdings_widget",
    "symbol_holders_view", "symbol_holders_widget",
]


def _shares(value: float) -> str:
    return "-" if pd.isna(value) else (f"{value:,.6f}".rstrip("0").rstrip(".") or "0")


_FORMATS = {
    "quantity": _shares,
    "price": "${:,.4f}",
    "balance": "${:,.2f}",
    "cost_basis": "${:,.2f}",
    "unrealized_gain": "${:,.2f}",
}


def _styled(frame: pd.DataFrame):
    return frame.style.format(_FORMATS, na_rep="-")


def _with_total(frame: pd.DataFrame, label_column: str, *, quantity: bool) -> pd.DataFrame:
    total = {
        column: float("nan") if pd.api.types.is_numeric_dtype(frame[column]) else ""
        for column in frame.columns
    }
    total.update(holdings_totals(frame, quantity=quantity))
    total[label_column] = "Total"
    return pd.concat([frame, pd.DataFrame([total])], ignore_index=True)


def _month_dropdown(source: RunConnection | ScenarioConnection) -> widgets.Dropdown | None:
    months = holding_months(source)
    if not months:
        return None
    return widgets.Dropdown(options=months, value=months[-1], description="Month:")


def _label(source: RunConnection | ScenarioConnection) -> str:
    return source.label if isinstance(source, RunConnection) else f"{source.name} (opening values)"


def account_holdings_view(
    source: RunConnection | ScenarioConnection, account: str, month: str | None = None,
) -> tuple[str, pd.DataFrame]:
    """Heading and table (lots by row, with a Total row) the account widget shows."""
    frame = account_holdings(source, account, month=month)
    shown = _with_total(
        frame.drop(columns=["account", "kind", "subkind", "tax_treatment", "as_of"]),
        "lot", quantity=False,
    )
    heading = f"**{account}** as of {frame['as_of'].iloc[0]} ({_label(source)})"
    return heading, shown.set_index("lot")


def symbol_holders_view(
    source: RunConnection | ScenarioConnection, symbol: str, month: str | None = None,
) -> tuple[str, pd.DataFrame]:
    """Heading and table (lots by account, with a Total row) the symbol widget shows."""
    everything = lots_frame(source, month)
    frame = everything[
        (everything["symbol"] == symbol)
        & everything["subkind"].isin(INVESTMENT_ASSET_SUBKINDS | {"cash"})
    ]
    shown = _with_total(
        frame[["account", "lot", "tax_treatment", "quantity", "price", "balance",
               "cost_basis", "unrealized_gain"]],
        "account", quantity=True,
    )
    name = security_names(source).get(symbol)
    described = f"**{symbol}** ({name})" if name else f"**{symbol}**"
    heading = f"{described} as of {everything['as_of'].iloc[0]} ({_label(source)})"
    return heading, shown.set_index(["account", "lot"])


def account_holdings_widget(
    source: RunConnection | ScenarioConnection, account: str | None = None,
) -> widgets.VBox:
    """The lots in one account (lot, symbol, acquired, quantity, price, balance,
    basis, unrealized gain), with an account dropdown and, for a run, a month."""
    accounts = holding_accounts(source)
    if not accounts:
        return _empty("no account has any lots")
    account_picker = widgets.Dropdown(
        options=accounts, value=account if account in accounts else accounts[0],
        description="Account:",
    )
    month_picker = _month_dropdown(source)
    out = widgets.Output()

    def _redraw(*_):
        month = month_picker.value if month_picker is not None else None
        heading, shown = account_holdings_view(source, account_picker.value, month)
        out.clear_output(wait=True)
        with out:
            display(Markdown(heading))
            display(_styled(shown))

    for picker in (account_picker, month_picker):
        if picker is not None:
            picker.observe(_redraw, names="value")
    _redraw()
    box = widgets.VBox([
        widgets.HBox([p for p in (account_picker, month_picker) if p is not None]), out,
    ])
    display(box, clear=True)
    return box


def symbol_holders_widget(
    source: RunConnection | ScenarioConnection, symbol: str | None = None,
) -> widgets.VBox:
    """Every lot holding one symbol, across accounts, with a symbol dropdown and,
    for a run, a month."""
    symbols = holding_symbols(source)
    if not symbols:
        return _empty("no lot has a symbol")
    names = security_names(source)
    symbol_picker = widgets.Dropdown(
        options=[(f"{s} - {names[s]}" if s in names else s, s) for s in symbols],
        value=symbol if symbol in symbols else symbols[0],
        description="Symbol:",
        layout=widgets.Layout(width="max-content"),
    )
    month_picker = _month_dropdown(source)
    out = widgets.Output()

    def _redraw(*_):
        month = month_picker.value if month_picker is not None else None
        heading, shown = symbol_holders_view(source, symbol_picker.value, month)
        out.clear_output(wait=True)
        with out:
            display(Markdown(heading))
            display(_styled(shown))

    for picker in (symbol_picker, month_picker):
        if picker is not None:
            picker.observe(_redraw, names="value")
    _redraw()
    box = widgets.VBox([
        widgets.HBox([p for p in (symbol_picker, month_picker) if p is not None]), out,
    ])
    display(box, clear=True)
    return box


def _empty(message: str) -> widgets.VBox:
    out = widgets.Output()
    with out:
        display(Markdown(f"_{message}_"))
    return widgets.VBox([out])
