"""Comparing plain scenario runs (#188): the data behind
``scenario_compare.ipynb`` and ``sim compare``, with no widgets, so the
notebook and the terminal can't drift apart.

Every function takes run connections by scenario name, the baseline first
among ``scenarios``. A scenario with no accounts is a valid compare point
(#174): it counts as zero wherever the others have a value.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from typing import Any

import pandas as pd

from analysis.frames import index_years, named_rows
from analysis.sim.connection import RunConnection, year_end_values
from analysis.sim.dates import month_before
from analysis.sim.types import Death, DollarMode

__all__ = [
    "ABSENT", "META_COLUMNS", "RULE_COMPARE_COLUMNS",
    "account_definition_differences", "all_nan_columns", "balance_by_month", "breakeven_month",
    "breakeven_summary", "comparability", "drift_by_scenario", "drift_targets", "event_differences",
    "net_worth_by_month", "rule_differences", "ss_by_year", "ss_differs", "ss_gross_by_month",
    "ss_summary", "strategy_activity", "strategy_differences", "target_differences", "tax_by_scenario",
    "tax_comparison", "tax_differs", "tax_summary", "uses_strategies", "variant_delta",
    "year_end_comparison", "year_to_date_differences",
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

def _dollars(cents: Any) -> str:
    return f"${int(cents) / 100:,.2f}"


def _percent(micros: Any) -> str:
    return f"{int(micros) / 10_000:g}%"


def _whole(value: Any) -> str:
    return str(int(value))


RULE_COMPARE_COLUMNS: dict[str, tuple[str, Callable[[Any], str] | None]] = {
    # column: (the field's name in the table, how a value shows)
    "amount_rule_kind": ("kind", None),
    "from_name": ("from", None),
    "to_name": ("to", None),
    "amount_cents": ("amount", _dollars),
    "rate_micros": ("rate", _percent),
    "claim_age": ("claim_age", _whole),
    "person": ("person", None),
    "series": ("series", None),
    "base_date": ("base_date", None),
    "inflation_cadence": ("inflation_cadence", None),
    "cadence": ("cadence", None),
    "month_of_year": ("month_of_year", _whole),
    "start": ("start", None),
    "end": ("end", None),
    "tax_character": ("tax_character", None),
    "tax_year": ("tax_year", _whole),
    "priority": ("priority", _whole),
    "ytd_amount": ("ytd_amount", _dollars),
    "ytd_preroll": ("ytd_preroll", _whole),
    "params_json": ("params", None),
    "tags": ("tags", None),
}
"""Every scheduled-rule field a run's behavior follows, in the order the
differences list them (#376: a changed claim age has to show)."""


def _blank(value: Any) -> bool:
    return value is None or (not isinstance(value, str) and pd.isna(value)) or value == ""


START = "(start)"
"""How a date in a scenario's first month shows in the differences (#502):
growth rules and latent factors begin with the scenario, so two scenarios
that start in different months (last year's plan and this year's) would
otherwise list every one of them."""


def _start_month(rc: RunConnection) -> str | None:
    """The run's scenario start as ``YYYY-MM``, or ``None`` without one."""
    start = rc.items("scenario")["start"]
    return None if start.empty or _blank(start.iloc[0]) else str(start.iloc[0])[:7]


def _same(a: Any, b: Any) -> bool:
    """Whether two cells hold the same value, blanks alike."""
    return (_blank(a) and _blank(b)) or (not _blank(a) and not _blank(b) and str(a) == str(b))


def _at_start(value: Any, start: str | None, *, or_before: bool = False) -> Any:
    """``value``, or ``START`` when it is a date in the ``start`` month (or,
    ``or_before``, any earlier one: a rule that began before the scenario
    starts runs from its start)."""
    if start is None or not isinstance(value, str):
        return value
    return START if value[:7] == start or (or_before and value[:7] < start) else value


def rule_differences(
    connections: Mapping[str, RunConnection], scenarios: Sequence[str], baseline: str,
) -> pd.DataFrame:
    """Scheduled rules as they stand at the end of each run, rows (rule,
    field), one column per scenario, only where a variant differs from the
    baseline, each value as it reads (dollars, a percent); a rule a scenario
    lacks, or a field it leaves blank, is missing there. A calendar event
    that edits a rule mid-run shows here as its end state."""
    columns = list(RULE_COMPARE_COLUMNS)
    rules = {
        name: connections[name].items("scheduled")
            .set_index("name").reindex(columns=columns)
        for name in scenarios
    }
    index = sorted(set().union(*(frame.index for frame in rules.values())))
    rules = {name: frame.reindex(index) for name, frame in rules.items()}
    starts = {name: _start_month(connections[name]) for name in scenarios}

    def shown(name: str, rule: str, column: str) -> Any:
        value = rules[name].at[rule, column]
        if _blank(value):
            return None
        if column == "start":
            return _at_start(value, starts[name], or_before=True)
        formatter = RULE_COMPARE_COLUMNS[column][1]
        return formatter(value) if formatter else str(value)

    rows = []
    for rule in index:
        for column in columns:
            raw = {name: rules[name].at[rule, column] for name in scenarios}
            if column == "start" and all(
                    _same(raw[name], raw[baseline]) for name in scenarios):
                # The same authored date: not a difference, though one
                # scenario's start makes it ``START`` there (#502).
                continue
            values = {name: shown(name, rule, column) for name in scenarios}
            if any(values[name] != values[baseline] for name in scenarios):
                rows.append({"rule": rule, "field": RULE_COMPARE_COLUMNS[column][0], **values})
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
    none). An event in its scenario's first month is dated ``START``."""
    frames = []
    for name in scenarios:
        frame = connections[name].items("calendar_event").assign(scenario=name)
        start = _start_month(connections[name])
        frames.append(frame.assign(
            dated=frame["date"].map(lambda d, s=start: _at_start(d, s) != START)))
    events = pd.concat(frames, ignore_index=True)
    if events.empty:
        return pd.DataFrame(columns=["scenario", *_EVENT_COLUMNS])
    events["amount"] = (events["amount"].astype("Float64") / 100).map(
        lambda v: "" if pd.isna(v) else f"${v:,.2f}"
    )
    # An event every scenario has as authored is no difference, though it
    # falls in one scenario's first month (#502); of the rest, one each
    # has in its first month (a growth rule's row) is START in each.
    every = len(scenarios)
    as_authored = events[_EVENT_COLUMNS].apply(tuple, axis=1)
    events = events.loc[events.groupby(as_authored)["scenario"].transform("nunique") < every]
    if events.empty:
        return pd.DataFrame(columns=["scenario", *_EVENT_COLUMNS])
    events = events.assign(date=events["date"].where(events["dated"], START))
    signature = events[_EVENT_COLUMNS].apply(tuple, axis=1)
    in_scenarios = events.groupby(signature)["scenario"].transform("nunique")
    return events.loc[in_scenarios < every, ["scenario", *_EVENT_COLUMNS]]


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


# ---------- one dollar for every run (#502) ----------


def align_cpi(connections: Mapping[str, RunConnection], baseline: str) -> dict[str, float]:
    """Put every run's CPI-constant dollars in the baseline's (#502).

    Each run deflates to its own start, so a variant that starts a year
    later (last year's plan against this year's) would be in that year's
    dollars. Each variant whose index starts in another month than the
    baseline's is rebased so that, in the first month both runs cover, its
    index equals the baseline's. Runs with the same start are left alone.
    Returns the scale given each rebased variant; a variant with no month in
    common with the baseline, or no CPI index, is left as it is.
    """
    def recorded(rc: RunConnection) -> pd.Series:
        # The run's own index, whatever an earlier alignment (a notebook
        # picking another baseline) scaled its deflator by.
        return rc.net_worth_frame()["cumulative_index_micros"].dropna()

    for rc in connections.values():
        if rc.cpi_scale != 1.0:
            rc.rebase_cpi(1.0)
    base = recorded(connections[baseline])
    scales: dict[str, float] = {}
    if base.empty:
        return scales
    for name, rc in connections.items():
        index = recorded(rc)
        if name == baseline or index.empty or index.index[0] == base.index[0]:
            continue
        month = max(index.index[0], base.index[0])
        if month not in base.index or month not in index.index:
            continue
        scales[name] = float(base[month]) / float(index[month])
        rc.rebase_cpi(scales[name])
    return scales


# ---------- where a later start begins (#502) ----------


def later_starts(connections: Mapping[str, RunConnection], baseline: str,
                 variants: Sequence[str]) -> list[str]:
    """The variants whose scenario starts after the baseline's, while the
    baseline's run still covers the month before: this year's plan against
    last year's."""
    base_start = _start_month(connections[baseline])
    months = connections[baseline].lot_balance_pivot().index
    later = []
    for name in variants:
        start = _start_month(connections[name])
        if base_start is None or start is None or start <= base_start:
            continue
        if pd.Timestamp(month_before(start)) in months:
            later.append(name)
    return later


def start_comparison(
    connections: Mapping[str, RunConnection], baseline: str, variant: str,
) -> tuple[date, pd.DataFrame]:
    """What the baseline's run projected each asset lot to hold at the end
    of the month before ``variant`` starts, against what the variant opens
    with, in nominal dollars (both are that month's dollars): last year's
    plan against this year's statements. Rows (account, lot) for every lot
    either has, then a ``total``; columns ``baseline``, ``variant`` and ``Δ``
    (the variant's opening less the projection). A lot one of them lacks is
    zero there."""
    start = _start_month(connections[variant])
    assert start is not None
    month = month_before(start)
    projected = connections[baseline].lot_balance_pivot().loc[pd.Timestamp(month)].dropna()
    projected.index = pd.MultiIndex.from_tuples(
        [tuple(label.split("/", 1)) for label in projected.index], names=["account", "lot"])
    lots = connections[variant].items("account_lot")
    lots = lots[lots["kind"] == "asset"]
    opening = pd.Series(
        lots["opening_balance"].astype("Float64").fillna(0).to_numpy() / 100,
        index=pd.MultiIndex.from_frame(lots[["account", "lot"]]), dtype=float)
    frame = pd.DataFrame({baseline: projected, variant: opening}).fillna(0.0).sort_index()
    frame["Δ"] = frame[variant] - frame[baseline]
    frame.loc[("total", ""), :] = frame.sum()
    return month, frame


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


# ---------- Social Security and tax (#335, #336) ----------


def ss_gross_by_month(
    connections: Mapping[str, RunConnection], scenarios: Sequence[str],
    mode: DollarMode = DollarMode.NOMINAL,
) -> pd.DataFrame:
    """Gross Social Security received by month (before Medicare is withheld),
    one column per scenario, over every run's months: zero in a month or a
    scenario with none. In CPI-constant dollars each month's benefit takes
    that month's CPI; a run with no CPI index gives NaN."""
    columns: dict[str, pd.Series] = {}
    months = pd.DatetimeIndex([], name="month")
    for name in scenarios:
        rc = connections[name]
        months = months.union(pd.DatetimeIndex(rc.cpi_index().index))
        ss = rc.ss_gross_by_month()
        columns[name] = rc.to_cpi_constant(ss) if mode == DollarMode.CPI_CONSTANT else ss
    frame = pd.DataFrame(columns, columns=list(scenarios))
    frame = frame.reindex(months.union(pd.DatetimeIndex(frame.index)))
    for name, ss in columns.items():
        if ss.empty or ss.notna().any():
            frame[name] = frame[name].fillna(0.0)
    frame.index.name = "month"
    return frame


def ss_by_year(ss: pd.DataFrame, baseline: str, variants: Sequence[str]) -> pd.DataFrame:
    """``ss_gross_by_month`` summed by calendar year, then a ``Δ name``
    column per variant: its benefit less the baseline's."""
    yearly = ss.groupby(index_years(ss)).sum(min_count=1)
    yearly.index.name = "year"
    for name in variants:
        yearly[f"Δ {name}"] = yearly[name] - yearly[baseline]
    return yearly


def ss_summary(ss: pd.DataFrame, baseline: str) -> pd.DataFrame:
    """Per scenario over the whole run: lifetime gross Social Security
    (``gross_ss``), and ``Δ gross_ss``, its difference from the baseline's."""
    lifetime = ss.sum(min_count=1)
    frame = pd.DataFrame({"gross_ss": lifetime, "Δ gross_ss": lifetime - lifetime[baseline]})
    frame.index.name = "scenario"
    return frame


def ss_differs(ss: pd.DataFrame) -> bool:
    """Whether any scenario's monthly benefit differs from another's by a
    dollar or more."""
    return bool((ss.max(axis=1) - ss.min(axis=1)).ge(1).any())


def tax_by_scenario(
    connections: Mapping[str, RunConnection], scenarios: Sequence[str],
    mode: DollarMode = DollarMode.NOMINAL,
) -> dict[str, pd.DataFrame]:
    """Each run's ``reports.tax_by_year`` (the table ``sim report tax``
    prints), by scenario, in ``mode`` dollars."""
    from analysis.sim.reports import tax_by_year

    return {name: tax_by_year(connections[name], mode) for name in scenarios}


def _total_tax(tax: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """Each scenario's ``total_tax`` by tax year, zero in a year (or a run)
    with none: a scenario without tax rules owes nothing."""
    frame = pd.DataFrame({name: table["total_tax"] for name, table in tax.items()},
                         columns=list(tax)).fillna(0.0)
    frame.index.name = "year"
    return frame


def tax_comparison(
    tax: Mapping[str, pd.DataFrame], baseline: str, variants: Sequence[str],
) -> pd.DataFrame:
    """Total tax (federal and state liability) by tax year: a column per
    scenario, then ``Δ name``, each variant's less the baseline's (negative
    owes less)."""
    frame = _total_tax(tax)
    for name in variants:
        frame[f"Δ {name}"] = frame[name] - frame[baseline]
    return frame


def tax_differs(comparison: pd.DataFrame) -> pd.Series:
    """The years of ``tax_comparison`` where some variant's tax differs from
    the baseline's by a dollar or more."""
    deltas = comparison[[c for c in comparison.columns if str(c).startswith("Δ ")]]
    return deltas.abs().ge(1).any(axis=1)


def tax_summary(
    tax: Mapping[str, pd.DataFrame], ss: pd.DataFrame, nw: pd.DataFrame, baseline: str,
) -> pd.DataFrame:
    """Per scenario over the whole run, what a plan costs in tax and what it
    buys: lifetime ``fed_tax``, ``state_tax`` and ``local_tax`` (state and
    local only when a run computes them, #487), ``total_tax`` and ``Δ tax`` (less the baseline's), lifetime
    ``gross_ss`` and ``Δ ss``, then ``Δ ss net of tax`` (Δ ss less Δ tax),
    and ``final_net_worth``, ``nw``'s last month each run has."""
    def lifetime(column: str) -> pd.Series:
        return pd.Series({name: t[column].sum() if column in t else 0.0 for name, t in tax.items()})

    total = _total_tax(tax).sum()
    gross_ss = ss.sum(min_count=1)
    columns: dict[str, pd.Series] = {"fed_tax": lifetime("fed_tax")}
    for column in ("state_tax", "local_tax"):
        if any(column in t for t in tax.values()):
            columns[column] = lifetime(column)
    columns |= {
        "total_tax": total,
        "Δ tax": total - total[baseline],
        "gross_ss": gross_ss,
        "Δ ss": gross_ss - gross_ss[baseline],
        "Δ ss net of tax": (gross_ss - gross_ss[baseline]) - (total - total[baseline]),
        "final_net_worth": pd.Series({
            name: nw[name].dropna().iloc[-1] if name in nw and nw[name].notna().any()
            else float("nan") for name in tax}),
    }
    frame = pd.DataFrame(columns, index=list(tax))
    frame.index.name = "scenario"
    return frame


# ---------- strategies (#359) ----------

STRATEGY_COMPARE_COLUMNS = ["kind", "chain", "rules", "start", "end", "params_json"]


def _keyed_differences(
    frames: Mapping[str, pd.DataFrame], scenarios: Sequence[str], baseline: str,
    fields: Sequence[str], key: Sequence[str],
) -> pd.DataFrame:
    """Rows (key..., field), one column per scenario, where a variant's value
    differs from the baseline's; a row a scenario lacks is missing there."""
    index = sorted(set().union(*(frame.index for frame in frames.values())))
    frames = {name: frame.reindex(index) for name, frame in frames.items()}
    base = frames[baseline]
    rows = []
    for row_key in index:
        for field in fields:
            b = base.at[row_key, field]
            if any(not (pd.isna(b) and pd.isna(v)) and b != v
                   for name in scenarios if name != baseline
                   for v in [frames[name].at[row_key, field]]):
                keys = row_key if isinstance(row_key, tuple) else (row_key,)
                rows.append({**dict(zip(key, keys, strict=True)), "field": field,
                             **{name: frames[name].at[row_key, field] for name in scenarios}})
    names = [*key, "field"]
    if not rows:
        return pd.DataFrame(columns=list(scenarios), index=pd.MultiIndex.from_tuples([], names=names))
    return pd.DataFrame(rows).set_index(names)


def uses_strategies(connections: Mapping[str, RunConnection]) -> bool:
    """Whether any run has a strategy or a target set: the strategy
    comparisons leave no trace otherwise."""
    return any(
        rc._con.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()  # noqa: S608
        for rc in connections.values() for table in ("strategy", "allocation_target"))


def strategy_differences(
    connections: Mapping[str, RunConnection], scenarios: Sequence[str], baseline: str,
) -> pd.DataFrame:
    """Strategies as each run had them (only enabled ones run), rows
    (strategy, field), where a variant differs from the baseline; a strategy
    a scenario doesn't run is missing there, as one switched off is."""
    frames = {}
    for name in scenarios:
        # A spending strategy owns rules and no chain (#387).
        frame = connections[name]._read_sql(
            'SELECT s.name, s.kind, c.name AS chain, s.rule_tag AS rules, s.start, '
            's."end", s.params_json FROM strategy s LEFT JOIN chain c ON c.id = s.chain_id')
        frames[name] = (frame.set_index("name") if not frame.empty
                        else pd.DataFrame(columns=STRATEGY_COMPARE_COLUMNS))
    return _keyed_differences(frames, scenarios, baseline, STRATEGY_COMPARE_COLUMNS, ["strategy"])


def target_differences(
    connections: Mapping[str, RunConnection], scenarios: Sequence[str], baseline: str,
) -> pd.DataFrame:
    """Target set weights (percent) that differ, rows (set, as_of, class)."""
    frames = {}
    for name in scenarios:
        frame = connections[name]._read_sql("SELECT target_set, as_of, asset_class, weight_micros "
                                            "FROM allocation_target")
        if frame.empty:
            frames[name] = pd.DataFrame(columns=["weight"])
            continue
        frame["weight"] = frame.pop("weight_micros") / 10_000
        frames[name] = frame.set_index(["target_set", "as_of", "asset_class"])
    out = _keyed_differences(frames, scenarios, baseline, ["weight"], ["target_set", "as_of", "asset_class"])
    return out.droplevel("field")


def strategy_activity(
    connections: Mapping[str, RunConnection], scenarios: Sequence[str],
) -> pd.DataFrame:
    """Each scenario's strategies and what they did over the run: rows
    (scenario, strategy); kind, chain, decisions, and dollars asked, moved
    by the strategy and left to the chain's steps."""
    rows = []
    from analysis.sim.strategy_guide import owns_sql

    for name in scenarios:
        rc = connections[name]
        frame = rc._read_sql(
            f"SELECT s.name, s.kind, {owns_sql()} AS chain, COUNT(d.id) AS decisions, "
            "COALESCE(SUM(d.asked_cents), 0) AS asked, COALESCE(SUM(d.chosen_cents), 0) AS moved, "
            "COALESCE(SUM(d.fallback_cents), 0) AS steps FROM strategy s "
            "LEFT JOIN chain c ON c.id = s.chain_id "
            "LEFT JOIN strategy_decision d ON d.strategy_id = s.id GROUP BY s.id ORDER BY s.name")
        for row in named_rows(frame):
            rows.append({"scenario": name, "strategy": row.name, "kind": row.kind, "chain": row.chain,
                         "decisions": int(row.decisions), "asked": row.asked / 100,
                         "moved": row.moved / 100, "steps": row.steps / 100})
    columns = ["scenario", "strategy", "kind", "chain", "decisions", "asked", "moved", "steps"]
    return pd.DataFrame(rows, columns=columns).set_index(["scenario", "strategy"])


def drift_targets(
    connections: Mapping[str, RunConnection], scenarios: Sequence[str],
    target_set: str | None = None, portfolio: str | None = None,
) -> tuple[str, str | None, float | None]:
    """The target set, portfolio and band to compare the runs' drift on: a
    strategy's in any of them (the baseline's first), else the one set
    they all have. ``ValueError`` when nothing settles it."""
    from analysis.sim import drift

    watched, known = [], set()
    for name in scenarios:
        con = connections[name]._con
        watched += drift.watches(con)
        known |= set(drift.target_sets(con))
    return drift.pick(watched, sorted(known), target_set, portfolio)


def drift_by_scenario(
    connections: Mapping[str, RunConnection], scenarios: Sequence[str], target_set: str,
    portfolio: str | None = None,
) -> dict[str, pd.DataFrame]:
    """Each run's ``drift.drift_by_month`` against ``target_set``; a run
    without that set is left out."""
    from analysis.sim import drift

    out = {}
    for name in scenarios:
        con = connections[name]._con
        if target_set in drift.target_sets(con):
            out[name] = drift.drift_by_month(con, target_set, portfolio=portfolio)
    return out


# ---------- spending against the plan (#408) ----------


def uses_spending_strategies(connections: Mapping[str, RunConnection]) -> bool:
    """Whether any run has a strategy owning scheduled rules: without one,
    every run's spending is its plan and the section leaves no trace."""
    from analysis.sim.flow_events import uses_spending_strategies as uses

    return any(uses(rc) for rc in connections.values())


type SpendingReport = tuple[pd.DataFrame, dict[str, float], list[int]]
"""A run's ``reports.spending_report`` frame and summary, and its whole years."""


def spending_by_scenario(
    connections: Mapping[str, RunConnection], scenarios: Sequence[str], mode: DollarMode,
) -> dict[str, SpendingReport]:
    """Each run's ``reports.spending_report`` and the years it covers whole,
    by scenario, in ``mode`` dollars; a run that spent nothing is left out."""
    from analysis.sim.reports import spending_by_year, spending_report, whole_years

    out = {}
    for name in scenarios:
        rc = connections[name]
        delivered, budgeted = spending_by_year(rc, mode=mode)
        if not delivered.empty:
            out[name] = (*spending_report(delivered, budgeted), whole_years(pd.DatetimeIndex(rc.cpi_index().index)))
    return out


def spending_comparison(
    reports: Mapping[str, SpendingReport], baseline: str, variants: Sequence[str],
) -> pd.DataFrame:
    """Delivered spending by year: a column per scenario, then ``Δ name``,
    each variant's less the baseline's (0 where either spent nothing)."""
    frame = pd.DataFrame({name: report["delivered"] for name, (report, _summary, _years) in reports.items()})
    frame.index.name = "year"
    for name in variants:
        if name in frame.columns and baseline in frame.columns:
            frame[f"Δ {name}"] = frame[name].fillna(0) - frame[baseline].fillna(0)
    return frame


def spending_summaries(reports: Mapping[str, SpendingReport]) -> pd.DataFrame:
    """Per scenario, over the whole run: delivered and budgeted in all,
    ``of_plan``, the RMS of the yearly differences, the whole year spending
    was lowest and how low (a part year would always be lowest), and the
    share redirected on average."""
    rows = {}
    for name, (report, summary, years) in reports.items():
        whole = report.loc[report.index.isin(years)]
        lowest = whole if not whole.empty else report
        rows[name] = {
            "delivered": float(report["delivered"].sum()),
            "budgeted": float(report["budgeted"].sum()),
            "of_plan": summary["of_plan"],
            "rms": summary["rms"],
            "lowest_year": int(lowest["delivered"].idxmin()),
            "lowest": float(lowest["delivered"].min()),
            "redirected": summary["redirected_mean"],
        }
    frame = pd.DataFrame.from_dict(rows, orient="index")
    frame.index.name = "scenario"
    return frame
