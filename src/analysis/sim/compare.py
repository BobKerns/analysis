"""Comparing plain scenario runs (#188): the data behind
``scenario_compare.ipynb`` and ``sim compare``, with no widgets, so the
notebook and the terminal can't drift apart.

Every function takes run connections by scenario name, the baseline first
among ``scenarios``. A scenario with no accounts is a valid compare point
(#174): it counts as zero wherever the others have a value.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime

import pandas as pd

from analysis.frames import named_rows
from analysis.sim.connection import RunConnection, year_end_values
from analysis.sim.types import Death, DollarMode

__all__ = [
    "ABSENT", "META_COLUMNS", "RULE_COMPARE_COLUMNS",
    "account_definition_differences", "all_nan_columns", "balance_by_month", "breakeven_month",
    "breakeven_summary", "comparability", "event_differences", "net_worth_by_month",
    "rule_differences", "variant_delta", "year_end_comparison", "year_to_date_differences",
]

# ---------- are the runs comparable? ----------

META_COLUMNS = [
    "scenario_name", "status", "finished_at",
    "code_version", "code_dirty", "data_version", "data_dirty",
]


def comparability(
    connections: Mapping[str, RunConnection], scenarios: Sequence[str],
) -> tuple[pd.DataFrame, list[str]]:
    """Each run's status, finish time and code/data versions (12 characters),
    one row per scenario, and why the comparison may mislead: different code
    or data versions, uncommitted code or data, or a run that didn't succeed.
    A difference could then be an unrelated edit, not the scenario change."""
    meta = pd.concat(
        [connections[name].items("simulation_run")[META_COLUMNS] for name in scenarios],
        ignore_index=True,
    )
    for column in ("code_version", "data_version"):
        meta[column] = meta[column].str[:12]
    warnings = []
    if meta["data_version"].nunique() > 1:
        warnings.append("different data versions: the data repo changed between runs")
    if meta["code_version"].nunique() > 1:
        warnings.append("different code versions: the engine changed between runs")
    if meta[["code_dirty", "data_dirty"]].to_numpy().any():
        warnings.append("at least one run was made from uncommitted code or data")
    if (meta["status"] != "succeeded").any():
        warnings.append("at least one run did not succeed")
    return meta, warnings


# ---------- what differs: rules and calendar events ----------

RULE_COMPARE_COLUMNS = ["amount_cents", "start", "end", "series", "cadence", "tax_character"]


def rule_differences(
    connections: Mapping[str, RunConnection], scenarios: Sequence[str], baseline: str,
) -> pd.DataFrame:
    """Scheduled rules as they stand at the end of each run, rows (rule,
    field), one column per scenario, only where a variant differs from the
    baseline; a rule a scenario lacks is missing there. A calendar event
    that edits a rule mid-run shows here as its end state."""
    rules = {
        name: connections[name].items("scheduled")
            .set_index("name").reindex(columns=RULE_COMPARE_COLUMNS)
        for name in scenarios
    }
    index = sorted(set().union(*(frame.index for frame in rules.values())))
    rules = {name: frame.reindex(index) for name, frame in rules.items()}
    base_rules = rules[baseline]
    differs = pd.DataFrame(False, index=index, columns=RULE_COMPARE_COLUMNS)
    for name in scenarios:
        if name != baseline:
            other = rules[name]
            differs |= (base_rules != other) & ~(base_rules.isna() & other.isna())
    rows = [
        {"rule": rule, "field": field, **{name: rules[name].at[rule, field] for name in scenarios}}
        for rule in index if differs.loc[rule].any()
        for field in RULE_COMPARE_COLUMNS if differs.at[rule, field]
    ]
    if not rows:
        return pd.DataFrame(
            columns=list(scenarios),
            index=pd.MultiIndex.from_tuples([], names=["rule", "field"]),
        )
    return pd.DataFrame(rows).set_index(["rule", "field"])


_EVENT_COLUMNS = ["date", "kind", "scheduled_name", "amount", "description"]


def event_differences(
    connections: Mapping[str, RunConnection], scenarios: Sequence[str],
) -> pd.DataFrame:
    """Calendar events not identical in every scenario, by (date, kind,
    rule, amount, description), with the scenario each belongs to and the
    amount in dollars; empty when every scenario has the same events (or
    none)."""
    events = pd.concat(
        [connections[name].items("calendar_event").assign(scenario=name) for name in scenarios],
        ignore_index=True,
    )
    if events.empty:
        return pd.DataFrame(columns=["scenario", *_EVENT_COLUMNS])
    events["amount"] = (events["amount"].astype("Float64") / 100).map(
        lambda v: "" if pd.isna(v) else f"${v:,.2f}"
    )
    signature = events[_EVENT_COLUMNS].apply(tuple, axis=1)
    in_scenarios = events.groupby(signature)["scenario"].transform("nunique")
    return events.loc[in_scenarios < len(scenarios), ["scenario", *_EVENT_COLUMNS]]


# ---------- what differs: accounts, lots and year-to-date rows ----------

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


# ---------- net worth and break-even ----------


def net_worth_by_month(
    connections: Mapping[str, RunConnection], scenarios: Sequence[str],
    mode: DollarMode = DollarMode.NOMINAL, *, after_tax: bool = False,
) -> pd.DataFrame:
    """Net worth (USD) by month, one column per scenario, after tax with
    ``after_tax`` (#251).

    A run with no accounts has no net-worth rows; it counts as zero net worth
    over the others' months (#174), not as missing data.
    """
    columns = {name: connections[name].net_worth_series(mode, after_tax=after_tax)
               for name in scenarios}
    nw = pd.DataFrame(columns)
    for name, column in columns.items():
        if column.empty:
            nw[name] = 0.0
    return nw


def all_nan_columns(frame: pd.DataFrame) -> list[str]:
    """The columns of a frame with rows whose every value is NaN: a
    CPI-constant series from a run with no CPI index. NaN in only some months
    is runs with different horizons, not missing data."""
    return [] if frame.empty else [str(name) for name in frame.columns[frame.isna().all()]]


def variant_delta(nw: pd.DataFrame, baseline: str, variants: Sequence[str]) -> pd.DataFrame:
    """Each variant's column minus the baseline's, by month."""
    return nw[list(variants)].sub(nw[baseline], axis=0)


def breakeven_month(series: pd.Series) -> datetime | None:
    """First month after which ``series`` never dips back to/below zero
    again; ``None`` when it never does, or has no months (two empty
    scenarios)."""
    if series.empty:
        return None
    ahead = series > 0
    if ahead.all():
        return series.index[0]
    if not ahead.any():
        return None
    after_last_deficit = series.index[series.index > series[~ahead].index.max()]
    return after_last_deficit.min() if len(after_last_deficit) else None


def breakeven_summary(
    nw: pd.DataFrame, delta: pd.DataFrame, baseline: str, variants: Sequence[str],
) -> pd.DataFrame:
    """Per-variant break-even month, deepest deficit, and end-of-run comparison."""
    rows = {}
    for name in variants:
        # Runs with different horizons leave NaN outside the months both
        # cover; break-even and the end values come from the shared months.
        series = delta[name].dropna()
        month = breakeven_month(series)
        if series.empty:  # no months to compare: nothing to report but that
            rows[name] = {"break-even": "never", "deepest deficit": None, "when": None,
                          "at scenario end": None, "final net worth": None}
            continue
        rows[name] = {
            "break-even": f"{month:%Y-%m}" if month is not None else "never",
            "deepest deficit": series.min(),
            "when": f"{series.idxmin():%Y-%m}",
            "at scenario end": series.iloc[-1],
            "final net worth": nw[name].dropna().iloc[-1],
        }
    summary = pd.DataFrame(rows).T
    summary.index.name = f"vs {baseline}"
    return summary


def year_end_comparison(nw: pd.DataFrame, baseline: str, variants: Sequence[str]) -> pd.DataFrame:
    """Each scenario's value at each year's last month, then a ``Δ name``
    column per variant: its value minus the baseline's."""
    year_end = year_end_values(nw)
    year_end.index.name = "year"
    for name in variants:
        year_end[f"Δ {name}"] = year_end[name] - year_end[baseline]
    return year_end


# ---------- one account's or lot's balance ----------


def balance_by_month(
    connections: Mapping[str, RunConnection], scenarios: Sequence[str],
    account: str, lot: str | None = None, mode: DollarMode = DollarMode.NOMINAL,
) -> pd.DataFrame:
    """One account's balance by month (or one ``lot`` of it), one column per
    scenario, over every scenario's months: zero in a scenario without it
    (#174). Signed as ``RunConnection.account_balance`` shows it.

    A balance that is all NaN (a CPI-constant balance from a run with no CPI
    index) stays NaN rather than reading as zero, so callers can say why.
    """
    columns = {
        name: (connections[name].lot_balance(account, lot, mode) if lot is not None
               else connections[name].account_balance(account, mode))
        for name in scenarios
    }
    frame = pd.DataFrame(columns)
    for name, column in columns.items():
        if column.empty or column.notna().any():
            frame[name] = frame[name].fillna(0.0)
    return frame


def scenario_deaths(connections: Mapping[str, RunConnection], scenarios: Sequence[str]) -> list[Death]:
    """The deaths a comparison chart marks (#255): every death in any of the
    scenarios, once each, so a variant that moves a death shows both."""
    return sorted(
        {death for name in scenarios for death in connections[name].deaths()},
        key=lambda d: (d.month, d.person),
    )
