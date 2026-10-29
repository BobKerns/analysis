"""Allocation drift over a run (#326 stage 1b, #359): each month's dollars
per asset class, and each class's share less its target.

A lot's dollars split across classes by ``asset_classes.class_weights``, the
same function the strategies' context and ``sim allocation`` use, so a chart
and a strategy's note never disagree. The class dollars are computed for
every asset lot and for the asset lots of each account tag (a strategy's
portfolio is a tag), all from one read of the run's lot balances; the
ensemble series store keeps them per member, and drift against a target set
is worked out at read time, as guard rails are.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from analysis.sim import registries
from analysis.sim.asset_classes import UNCLASSIFIED, WeightSets, class_weights, weight_sets, weights_at

__all__ = [
    "ALL_LOTS", "MAX_ABS", "Watch", "choose", "class_dollars", "class_order", "drift", "drift_by_month",
    "ensemble_drift_table", "has_table", "pick", "target_sets", "watches", "year_end_rows",
]

ALL_LOTS = ""
"""The portfolio of every asset lot, as ``class_dollars`` keys it."""

MAX_ABS = "max_abs"
"""``drift``'s column of each month's largest drift, in either direction."""


def _sets(con: sqlite3.Connection, table: str, key: str) -> WeightSets:
    return weight_sets(
        (name, as_of, asset_class, weight / 1_000_000) for name, as_of, asset_class, weight in con.execute(
            f"SELECT {key}, as_of, asset_class, weight_micros FROM {table}"  # noqa: S608 -- fixed names
        )
    )


def target_sets(con: sqlite3.Connection) -> WeightSets:
    """The run's ``allocation_target`` sets (fractions), oldest ``as_of`` first."""
    return _sets(con, "allocation_target", "target_set")


def class_order(classes: Sequence[str] | set[str]) -> list[str]:
    """``classes`` in display order: the registry's, ``unclassified`` last."""
    known = [c for c in (*registries.ASSET_CLASSES, UNCLASSIFIED) if c in classes]
    return known + sorted(set(classes) - set(known))


def class_dollars(
    con: sqlite3.Connection, *, portfolios: Sequence[str] | None = None,
) -> dict[str, pd.DataFrame]:
    """A run's month-end nominal dollars per asset class, for every asset lot
    (key ``ALL_LOTS``) and for each tag's (``role:name``) asset lots: index
    month (ISO date), one column per class any of those lots holds. Tags
    default to every tag an asset account carries."""
    months = [m for (m,) in con.execute("SELECT month FROM simulation_step ORDER BY step_index")]
    lots = con.execute(
        "SELECT m.id, a.id, a.name, m.lot, m.symbol, a.subkind FROM account_lot m "
        "JOIN account a ON a.id = m.account_id WHERE a.kind = 'asset' ORDER BY m.id"
    ).fetchall()
    tags: dict[int, set[str]] = {}
    for account_id, tag in con.execute(
        "SELECT at.account_id, t.role || ':' || t.name FROM account_tag at JOIN tag t ON t.id = at.tag_id"
    ):
        tags.setdefault(account_id, set()).add(tag)
    wanted = [ALL_LOTS, *(
        sorted({t for _id, account_id, *_ in lots for t in tags.get(account_id, ())})
        if portfolios is None else portfolios
    )]
    if not months or not lots:
        return {p: pd.DataFrame(index=pd.Index(months, name="month")) for p in wanted}

    row_of = {lot_id: i for i, (lot_id, *_) in enumerate(lots)}
    step_of = {m: i for i, m in enumerate(months)}
    balances = np.zeros((len(lots), len(months)))
    for lot_id, month, balance in con.execute(
        "SELECT ls.lot_id, s.month, ls.balance FROM lot_state ls JOIN simulation_step s ON s.id = ls.step_id"
    ):
        if lot_id in row_of:
            balances[row_of[lot_id], step_of[month]] = balance / 100

    # Weights change only at an allocations.csv as_of: work them out once per
    # stretch of months between two, not once per month.
    symbols = _sets(con, "security_allocation", "symbol")
    bounds = sorted({as_of for sets in symbols.values() for as_of, _w in sets if as_of})
    stretch = np.searchsorted(np.array(bounds, dtype=str), np.array(months, dtype=str), side="right")
    classes: set[str] = set()
    weights_by_stretch: dict[int, list[dict[str, float]]] = {}
    for s in sorted(set(stretch.tolist())):
        month = months[int(np.argmax(stretch == s))]
        weights_by_stretch[s] = [
            class_weights(symbol=symbol or None, account=account, lot=lot, subkind=subkind, sets=symbols, date=month)
            for _lot_id, _account_id, account, lot, symbol, subkind in lots
        ]
        for weights in weights_by_stretch[s]:
            classes.update(weights)
    order = class_order(classes)
    column = {c: j for j, c in enumerate(order)}

    # lots x months x classes, then summed over each portfolio's lots.
    share = np.zeros((len(lots), len(months), len(order)))
    for s, per_lot in weights_by_stretch.items():
        in_stretch = stretch == s
        for i, weights in enumerate(per_lot):
            for asset_class, w in weights.items():
                share[i, in_stretch, column[asset_class]] = w
    dollars = balances[:, :, None] * share
    out: dict[str, pd.DataFrame] = {}
    index = pd.Index(months, name="month")
    for portfolio in wanted:
        rows = [i for i, (_lot_id, account_id, *_) in enumerate(lots)
                if portfolio == ALL_LOTS or portfolio in tags.get(account_id, ())]
        total = dollars[rows].sum(axis=0) if rows else np.zeros((len(months), len(order)))
        frame = pd.DataFrame(total, index=index, columns=order)
        out[portfolio] = frame.loc[:, (frame != 0).any(axis=0)]
    return out


def drift(dollars: pd.DataFrame, targets: Sequence[tuple[str, Mapping[str, float]]]) -> pd.DataFrame:
    """Each month's share in each asset class less its target, in percentage
    points, and ``max_abs``, the largest either way. Classes are those the
    targets name or the dollars hold; a month with no dollars is all zero.
    ``targets`` is one set's dated weights; ``dollars`` is indexed by ISO month."""
    if dollars.empty and not len(dollars.index):
        return pd.DataFrame(columns=[MAX_ABS])
    targets_by_month = pd.DataFrame(
        [dict(weights_at(targets, str(m))) for m in dollars.index], index=dollars.index,
    ).fillna(0.0)
    order = class_order(set(dollars.columns) | set(targets_by_month.columns))
    held = dollars.reindex(columns=order, fill_value=0.0)
    total = held.sum(axis=1)
    shares = held.div(total.where(total != 0), axis=0).fillna(0.0)
    out = ((shares - targets_by_month.reindex(columns=order, fill_value=0.0)) * 100).round(1)
    # Nothing held is no drift: there is no mix to be off target.
    out[total == 0] = 0.0
    out[MAX_ABS] = out.abs().max(axis=1)
    return out


def year_end_rows(frame: pd.DataFrame) -> pd.DataFrame:
    """``frame``'s December rows and its last month (index: ISO month)."""
    months = [str(m) for m in frame.index]
    chosen = [m for m in months if m[5:7] == "12"]
    if months and (not chosen or chosen[-1] != months[-1]):
        chosen.append(months[-1])
    return frame.loc[chosen]


def has_table(con: sqlite3.Connection, table: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is not None


@dataclass(frozen=True)
class Watch:
    """A strategy that steers toward a target set: what a drift chart
    defaults to, and the band it shades (percentage points; None when the
    kind has no ``band``)."""

    strategy: str
    target_set: str
    portfolio: str | None
    band: float | None


def _default_band(kind: str) -> int | None:
    from analysis.sim.strategy_code import builtin_kinds

    cls = builtin_kinds().get(kind)
    params = getattr(cls, "Params", None)
    if params is None or not dataclasses.is_dataclass(params):
        return None
    for f in dataclasses.fields(params):
        if f.name == "band" and isinstance(f.default, int):
            return f.default
    return None


def watches(con: sqlite3.Connection) -> list[Watch]:
    """The run's strategies with a ``targets`` parameter, by name."""
    if not has_table(con, "strategy"):
        return []
    out = []
    for name, kind, raw in con.execute("SELECT name, kind, params_json FROM strategy ORDER BY name"):
        params = json.loads(raw or "{}")
        if not isinstance(params, dict) or not isinstance(params.get("targets"), str):
            continue
        band = params.get("band", _default_band(kind))
        out.append(Watch(
            name, params["targets"], params.get("portfolio") or None,
            band / 10_000 if isinstance(band, int) else None,
        ))
    return out


def choose(
    con: sqlite3.Connection, target_set: str | None = None, portfolio: str | None = None,
) -> tuple[str, str | None, float | None]:
    """The target set, portfolio and band a drift view of this run shows
    (``pick``). ``ValueError`` says what to pick from."""
    if not has_table(con, "allocation_target"):
        raise ValueError("the run is from before allocation targets; run it again")
    return pick(watches(con), sorted(target_sets(con)), target_set, portfolio)


def pick(
    watched: Sequence[Watch], known: Sequence[str], target_set: str | None = None, portfolio: str | None = None,
) -> tuple[str, str | None, float | None]:
    """The target set, portfolio and band to show: those given, else a
    strategy's (the only set strategies use, else the only set there is).
    The band is a strategy's on that set and portfolio, if any."""
    if target_set is None:
        choices = sorted({w.target_set for w in watched}) or list(known)
        if len(choices) != 1:
            raise ValueError("say which target set: " + (
                ", ".join(known) if known else "there are none (sim target set)"))
        target_set = choices[0]
    if target_set not in known:
        raise ValueError(f"no target set {target_set!r} (known: {', '.join(known) or 'none'})")
    mine = [w for w in watched if w.target_set == target_set]
    if portfolio is None:
        portfolio = next((w.portfolio for w in mine if w.portfolio), None)
    band = next((w.band for w in mine if w.portfolio == portfolio and w.band is not None), None)
    return target_set, portfolio, band


def drift_by_month(
    con: sqlite3.Connection, target_set: str, *, portfolio: str | None = None,
) -> pd.DataFrame:
    """``drift`` over every month of a run, for ``portfolio`` (an account
    tag; every asset lot without one) against ``target_set``."""
    sets = target_sets(con)
    if target_set not in sets:
        known = ", ".join(sorted(sets)) or "none"
        raise KeyError(f"no target set {target_set!r} in the run (known: {known})")
    key = portfolio or ALL_LOTS
    dollars = class_dollars(con, portfolios=[] if key == ALL_LOTS else [key])[key]
    return drift(dollars, sets[target_set])


def ensemble_drift_table(
    members: pd.DataFrame, *, band: float | None = None, quantiles: Sequence[float] = (0.5, 0.75, 0.95),
) -> pd.DataFrame:
    """An ensemble's drift by year end: ``members`` is one drift column
    (``max_abs`` usually) per member, indexed by month. Per year end, its
    quantiles across members and the largest, and with a band the share of
    members past it then and the share past it at some month so far."""
    if members.empty:
        return pd.DataFrame()
    rows = year_end_rows(members.set_axis([str(m)[:10] for m in members.index]))
    out = pd.DataFrame(index=rows.index)
    for q in quantiles:
        out[f"p{q * 100:g}"] = rows.quantile(q, axis=1)
    out["max"] = rows.max(axis=1)
    if band is not None:
        past = members.abs() > band + 1e-9
        out["past_band"] = (rows.abs() > band + 1e-9).mean(axis=1)
        ever = past.cummax().set_axis([str(m)[:10] for m in members.index])
        out["ever_past"] = ever.loc[rows.index].mean(axis=1)
    return out
