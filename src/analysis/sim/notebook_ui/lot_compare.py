"""Scenario-compare widget for lots (ADR-005), called like the widgets in
:mod:`compare_plots` (``CONNECTIONS.show(widget, ...)``).

:func:`account_definitions_widget` shows how accounts, their lots and the
year-to-date rows are *defined* differently across scenarios. How they
*evolve* differently (balance, cost basis, quantity) is
:func:`compare_plots.show_account_balance_diffs`, which drills into lots.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

import pandas as pd
from IPython.display import display

from analysis.sim.connection import RunConnection
from analysis.frames import named_rows
from analysis.sim.notebook_ui.pickers import (
    MultiScenarioConnections,
    display_warning,
)
from analysis.sim.notebook_ui.styled import StyledMarkdown

__all__ = [
    "account_definition_differences",
    "account_definitions_widget",
    "year_to_date_differences",
]

ABSENT = "—"

_ACCOUNT_TEXT_FIELDS = ("kind", "subkind", "owner", "tax_treatment", "opened", "closed")
_ACCOUNT_FIELDS = (*_ACCOUNT_TEXT_FIELDS, "tags")
_LOT_MONEY_FIELDS = ("minimum_balance", "maximum_balance", "refill_target", "refill_amount")
_LOT_CHAIN_FIELDS = ("refill_chain", "refill_lot", "overflow_chain", "overflow_lot")
_LOT_FIELDS = (
    "symbol", "acquired", "quantity", "series", "opening_balance",
    "opening_cost_basis", "prior_year_end_balance",
    *_LOT_MONEY_FIELDS, *_LOT_CHAIN_FIELDS,
)
_YEAR_TO_DATE_FIELDS = ("from", "to", "amount", "tax_character", "year")
_EXISTS = "(exists)"


# ---------- formatting ----------


def _dollars(cents) -> str | None:
    return None if pd.isna(cents) else f"${cents / 100:,.2f}"


def _shares(value) -> str | None:
    if pd.isna(value):
        return None
    return f"{value:,.6f}".rstrip("0").rstrip(".") or "0"


def _text(value) -> str | None:
    return None if pd.isna(value) or value == "" else str(value)


def _account_with_lot(account: str, lot: str) -> str:
    return account if lot == "default" else f"{account}/{lot}"


# ---------- account definitions ----------


def _excluded_accounts(connections: Mapping[str, RunConnection]) -> set[str]:
    """Built-in and ``diff: transient`` accounts, which are never worth comparing."""
    excluded: set[str] = set()
    for connection in connections.values():
        accounts = connection.items("account")
        for name, meta in zip(accounts["name"], accounts["meta_json"], strict=True):
            info = json.loads(meta or "{}")
            if info.get("builtin") or info.get("diff") == "transient":
                excluded.add(name)
    return excluded


def _definition_values(connection: RunConnection, excluded: set[str]) -> pd.Series:
    """One scenario's account and lot definitions as strings, indexed by
    (account, lot, field); the account's own fields have a blank lot."""
    values: dict[tuple[str, str, str], str] = {}

    def put(account: str, lot: str, field: str, value: str | None) -> None:
        if value is not None:
            values[(account, lot, field)] = value

    accounts = connection.items("account")
    for row in named_rows(accounts):
        if row.name in excluded:
            continue
        put(row.name, "", _EXISTS, "yes")
        for field in _ACCOUNT_FIELDS:
            put(row.name, "", field, _text(getattr(row, field)))
    for row in named_rows(connection.items("account_lot")):
        if row.account in excluded:
            continue
        put(row.account, row.lot, _EXISTS, "yes")
        put(row.account, row.lot, "symbol", _text(row.symbol))
        put(row.account, row.lot, "acquired", _text(row.acquired))
        put(row.account, row.lot, "quantity",
            _shares(None if pd.isna(row.quantity_micros) else row.quantity_micros / 1_000_000))
        put(row.account, row.lot, "series", _text(row.series))
        put(row.account, row.lot, "opening_balance", _dollars(row.opening_balance))
        put(row.account, row.lot, "opening_cost_basis", _dollars(row.opening_cost_basis))
        put(row.account, row.lot, "prior_year_end_balance", _dollars(row.prior_year_end_balance))
        for field in _LOT_MONEY_FIELDS:
            cents = getattr(row, f"{field}_cents")
            # a minimum of zero is the default, so it is not worth showing
            put(row.account, row.lot, field,
                None if field == "minimum_balance" and cents == 0 else _dollars(cents))
        for field in _LOT_CHAIN_FIELDS:
            put(row.account, row.lot, field, _text(getattr(row, field)))
    index = pd.MultiIndex.from_arrays(
        [[k[i] for k in values] for i in range(3)], names=["account", "lot", "field"],
    )
    return pd.Series(list(values.values()), index=index, dtype=object)


def _differing(frame: pd.DataFrame, baseline: str) -> pd.DataFrame:
    """Rows of ``frame`` (columns are scenarios) where any scenario differs from
    the baseline, treating two missing values as equal."""
    base = frame[baseline]
    differs = pd.Series(False, index=frame.index)
    for name in frame.columns:
        if name != baseline:
            differs |= (frame[name] != base) & ~(frame[name].isna() & base.isna())
    return frame[differs]


def account_definition_differences(
    connections: Mapping[str, RunConnection], scenarios: Sequence[str], baseline: str,
) -> pd.DataFrame:
    """How accounts and lots are defined differently: rows (account, lot, field),
    one column per scenario, only where a scenario differs from the baseline.
    Missing values are ``None``. Account-level fields have a blank lot and sort
    ahead of the account's lots; a lot (or account) missing from a scenario shows
    ``(exists)`` as missing there."""
    excluded = _excluded_accounts({name: connections[name] for name in scenarios})
    frame = pd.concat(
        {name: _definition_values(connections[name], excluded) for name in scenarios},
        axis=1,
    )
    frame = _differing(frame, baseline)
    fields = [_EXISTS, *_ACCOUNT_FIELDS, *_LOT_FIELDS]
    order = pd.Series(range(len(fields)), index=fields)
    keys = pd.DataFrame(frame.index.to_list(), columns=["account", "lot", "field"])
    keys["rank"] = keys["field"].map(order)
    keys["position"] = range(len(keys))
    positions = keys.sort_values(["account", "lot", "rank", "position"])["position"]
    return frame.iloc[positions.to_numpy()]


def year_to_date_differences(
    connections: Mapping[str, RunConnection], scenarios: Sequence[str], baseline: str,
) -> pd.DataFrame:
    """How ``year_to_date.csv`` rows differ: rows (row name, field), one column
    per scenario, only where a scenario differs from the baseline."""
    series = {}
    for name in scenarios:
        rows = connections[name].items("year_to_date")
        values: dict[tuple[str, str], str | None] = {}
        for row in named_rows(rows):
            values[(row.name, "from")] = _account_with_lot(row.from_name, row.from_lot)
            values[(row.name, "to")] = _account_with_lot(row.to_name, row.to_lot)
            values[(row.name, "amount")] = _dollars(row.amount)
            values[(row.name, "tax_character")] = _text(row.tax_character)
            values[(row.name, "year")] = None if pd.isna(row.year) else str(int(row.year))
        index = pd.MultiIndex.from_arrays(
            [[k[i] for k in values] for i in range(2)], names=["row", "field"],
        )
        series[name] = pd.Series(list(values.values()), index=index, dtype=object)
    frame = _differing(pd.concat(series, axis=1), baseline)
    rank = {field: i for i, field in enumerate(_YEAR_TO_DATE_FIELDS)}
    order = sorted(range(len(frame)), key=lambda i: (frame.index[i][0], rank[frame.index[i][1]]))
    return frame.iloc[order]


def account_definitions_widget(
    container: MultiScenarioConnections,
    connections: Mapping[str, RunConnection],
    scenarios: Sequence[str],
    variants: Sequence[str],
    baseline: str = "current",
    **_,
) -> None:
    """Accounts, lots and year-to-date rows that are defined differently."""
    if not container.ok:
        return
    with pd.option_context("display.max_rows", None, "display.max_columns", None):
        accounts = account_definition_differences(connections, scenarios, baseline)
        if accounts.empty:
            display_warning("**All scenarios define accounts and lots identically.**")
        else:
            display(StyledMarkdown("### Accounts and lots"))
            display(accounts.fillna(ABSENT))
        year_to_date = year_to_date_differences(connections, scenarios, baseline)
        if year_to_date.empty:
            display_warning("**All scenarios have identical year-to-date rows.**")
        else:
            display(StyledMarkdown("### Year-to-date rows"))
            display(year_to_date.fillna(ABSENT))
