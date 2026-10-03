"""Scenario-compare widgets for lots (ADR-005).

Two views, called like the widgets in :mod:`compare_plots`
(``CONNECTIONS.show(widget, ...)``):

* :func:`account_definitions_widget` -- how accounts, their lots and the
  year-to-date rows are *defined* differently across scenarios.
* :func:`show_lot_balance_diffs` -- how lot balances, cost basis and
  quantities *evolve* differently, for accounts with more than one lot.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

import ipywidgets as widgets
import pandas as pd
from IPython.display import clear_output, display

from analysis.sim.connection import RunConnection
from analysis.sim.notebook_ui.compare_plots import _observe_dollar_mode
from analysis.sim.notebook_ui.pickers import (
    DollarModePicker,
    MultiScenarioConnections,
    ValuePlotModePicker,
    display_warning,
    to_markdown,
)
from analysis.sim.types import DollarMode, ValuePlotMode
from analysis.sim.notebook_ui.styled import StyledMarkdown

__all__ = [
    "account_definition_differences",
    "account_definitions_widget",
    "differing_lots",
    "lot_frame",
    "show_lot_balance_diffs",
    "year_to_date_differences",
]

ABSENT = "—"
LOT_MEASURE_LABELS = {"balance": "Balance", "cost_basis": "Cost basis", "quantity": "Quantity"}

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
    for row in accounts.itertuples():
        if row.name in excluded:
            continue
        put(row.name, "", _EXISTS, "yes")
        for field in _ACCOUNT_FIELDS:
            put(row.name, "", field, _text(getattr(row, field)))
    for row in connection.items("account_model_member").itertuples():
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
        for row in rows.itertuples():
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


# ---------- lot balances ----------


def _lots_by_account(connection: RunConnection) -> dict[str, set[str]]:
    """Asset account -> its lot labels (``Account/lot``), from the run."""
    members = connection.items("account_model_member")
    members = members[members["kind"] == "asset"]
    grouped: dict[str, set[str]] = {}
    for account, lot in zip(members["account"], members["lot"], strict=True):
        grouped.setdefault(account, set()).add(f"{account}/{lot}")
    return grouped


def _eligible_labels(connections: Mapping[str, RunConnection]) -> set[str]:
    """Lots of every asset account that has more than one lot in some scenario."""
    per_scenario = [_lots_by_account(connection) for connection in connections.values()]
    multi = {a for lots in per_scenario for a, labels in lots.items() if len(labels) > 1}
    return {label for lots in per_scenario for a in multi for label in lots.get(a, ())}


def _measure_pivot(connection: RunConnection, measure: str, cpi: bool) -> pd.DataFrame:
    return (
        connection.cpi_lot_balance_pivot(measure) if cpi
        else connection.lot_balance_pivot(measure)
    )


def differing_lots(
    baseline_rc: RunConnection, variant_rcs: Mapping[str, RunConnection],
) -> tuple[list[str], dict[str, list[str]], dict[str, list[str]]]:
    """``(labels, added, deleted)``: the ``Account/lot`` labels to compare, and per
    variant the lots it adds and deletes. A lot is worth comparing if its balance,
    basis or quantity differs in any variant, or it exists in only some scenarios.
    Only accounts with more than one lot in some scenario are considered."""
    runs = {"baseline": baseline_rc, **variant_rcs}
    eligible = _eligible_labels(runs)
    base_labels = {
        label for lots in _lots_by_account(baseline_rc).values() for label in lots
    } & eligible
    differing: set[str] = set()
    added: dict[str, list[str]] = {}
    deleted: dict[str, list[str]] = {}
    for name, variant in variant_rcs.items():
        labels = {
            label for lots in _lots_by_account(variant).values() for label in lots
        } & eligible
        added[name] = sorted(labels - base_labels)
        deleted[name] = sorted(base_labels - labels)
        differing |= labels ^ base_labels
        for measure in RunConnection.LOT_MEASURES:
            base_pivot = baseline_rc.lot_balance_pivot(measure)
            variant_pivot = variant.lot_balance_pivot(measure)
            for label in labels & base_labels:
                index = base_pivot.index.union(variant_pivot.index)
                if not base_pivot[label].reindex(index).equals(variant_pivot[label].reindex(index)):
                    differing.add(label)
    return sorted(differing), added, deleted


def lot_frame(
    baseline_rc: RunConnection,
    variant_rcs: Mapping[str, RunConnection],
    label: str,
    measure: str,
    *,
    cpi: bool = False,
    monthly: bool = False,
    baseline_name: str = "baseline",
) -> pd.DataFrame:
    """Months x (baseline, each variant, ``Δvariant``) for one ``Account/lot`` and
    measure, showing only the months where some variant differs. A lot a scenario
    does not have counts as zero; an untracked basis or quantity stays missing."""
    runs = {baseline_name: baseline_rc, **variant_rcs}
    pivots = {name: _measure_pivot(rc, measure, cpi) for name, rc in runs.items()}
    months = pd.DatetimeIndex(sorted(set().union(*(p.index for p in pivots.values()))))
    frame = pd.DataFrame({
        name: pivot[label].reindex(months) if label in pivot else pd.Series(0.0, index=months)
        for name, pivot in pivots.items()
    })
    for name in variant_rcs:
        frame[f"Δ{name}"] = frame[name] - frame[baseline_name]
    if monthly:
        frame = frame.diff()
    deltas = [f"Δ{name}" for name in variant_rcs]
    return frame.loc[frame[deltas].fillna(0).ne(0).any(axis=1)]


def _format(measure: str):
    if measure == "quantity":
        return lambda v: "-" if pd.isna(v) else (f"{v:,.6f}".rstrip("0").rstrip(".") or "0")
    return lambda v: "-" if pd.isna(v) else f"${v:,.2f}"


def show_lot_balance_diffs(
    _,
    baseline: str,
    baseline_rc: RunConnection,
    variant_rcs: Mapping[str, RunConnection],
    dollar_mode: DollarModePicker | None = None,
    value_mode: ValuePlotModePicker | None = None,
    scenario_picker: widgets.Widget | None = None,
    **__,
) -> widgets.VBox:
    """Balance, cost basis and quantity of each lot, baseline against variants,
    for accounts with more than one lot. Pick one ``Account/lot`` and a measure."""
    dollar_mode = dollar_mode or DollarModePicker()
    value_mode = value_mode or ValuePlotModePicker()
    heading = to_markdown(f"## Lot Diffs for Baseline `{baseline}`")
    picker = (scenario_picker,) if scenario_picker is not None else ()
    labels, added, deleted = differing_lots(baseline_rc, variant_rcs)
    if not labels:
        return widgets.VBox([
            *picker, widgets.HBox([dollar_mode, value_mode]), heading,
            to_markdown("_No lot differences._"),
        ])

    lot_picker = widgets.Dropdown(options=labels, value=labels[0], description="Lot:",
                                  layout=widgets.Layout(width="max-content"))
    measure_picker = widgets.ToggleButtons(
        options=[(text, key) for key, text in LOT_MEASURE_LABELS.items()],
        value="balance", description="Measure:",
    )
    notes = "\n".join(
        f"-- {kind} lots in `{variant}`: {', '.join(names)}"
        for kind, by_variant in (("Added", added), ("Deleted", deleted))
        for variant, names in by_variant.items() if names
    )
    output = widgets.Output()

    def _redraw(*_):
        frame = lot_frame(
            baseline_rc, variant_rcs, lot_picker.value, measure_picker.value,
            cpi=dollar_mode.value == DollarMode.CPI_CONSTANT,
            monthly=value_mode.value == ValuePlotMode.MONTHLY,
            baseline_name="baseline",
        )
        with output:
            clear_output(wait=True)
            display(StyledMarkdown(
                f"### {lot_picker.value} - {LOT_MEASURE_LABELS[measure_picker.value]}"
            ))
            if frame.empty:
                display(StyledMarkdown("_No difference in this measure._"))
            else:
                display(frame.style.format(_format(measure_picker.value)))

    lot_picker.observe(_redraw, names="value")
    measure_picker.observe(_redraw, names="value")
    value_mode.observe(_redraw, names="value")
    _observe_dollar_mode(dollar_mode, "lot_diffs", _redraw)
    _redraw()

    return widgets.VBox([
        *picker, heading, widgets.HBox([lot_picker, measure_picker]),
        widgets.HBox([dollar_mode, value_mode]),
        *((to_markdown(notes),) if notes else ()), output,
    ])
