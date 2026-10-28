"""Portfolio asset allocation: each asset lot's balance split by the asset-class
weights of its symbol (allocations.csv), grouped as asked.

Works on a :class:`RunConnection` (month-end balances) or a
:class:`ScenarioConnection` (opening values). Weights are the symbol's latest set
on or before the date of the balances, else its earliest; a lot with no
allocation is classed by its account subkind (cash, real estate, ...) or shown as
``unclassified`` so a gap is visible rather than silently dropped.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

import pandas as pd

from analysis.sim import registries
from analysis.sim.asset_classes import UNCLASSIFIED, class_weights, is_lot_key, lot_key, weight_sets
from analysis.sim.connection import Connection
from analysis.frames import named_rows
from analysis.sim.holdings import lots_frame

__all__ = [
    "GROUPINGS", "UNCLASSIFIED", "asset_classes_present", "class_weights", "is_lot_key", "lot_classes",
    "lot_key", "portfolio_allocation", "render_allocation_table",
]

type Grouping = Literal["account", "owner", "subkind", "tax_treatment", "security"]
GROUPINGS: tuple[Grouping, ...] = Grouping.__value__.__args__
_NO_SYMBOL = "(no symbol)"
_JOINT = "joint"

_NO_SPLIT = "(none)"
GroupingOrNone = Literal["account", "owner", "subkind", "tax_treatment", "security", "(none)"]


def _weight_sets(source: Connection) -> dict[str, list[tuple[str, dict[str, float]]]]:
    """Symbol to its weight sets, oldest first: ``(as_of, {asset_class: weight})``."""
    return weight_sets(
        (row.symbol, row.as_of, row.asset_class, row.weight_micros / 1_000_000)
        for row in named_rows(source.items("security_allocation"))
    )


def _reference_date(source: Connection, as_of: str) -> str:
    if as_of != "opening":
        return as_of
    return str(source.items("scenario")["start"].iloc[0])


def lot_classes(source: Connection, month: str | None = None) -> pd.DataFrame:
    """One row per asset lot and asset class: account, lot, symbol, security (the symbol,
    or "(no symbol)"), owner, subkind,
    tax_treatment, asset_class, weight and balance (dollars, the lot's balance times
    the weight), and ``as_of``. Liabilities are left out."""
    lots = lots_frame(source, month)
    lots = lots[lots["kind"] == "asset"]
    owners = source.items("account").set_index("name")["owner"]
    lots = lots.assign(owner=lots["account"].map(owners).fillna(_JOINT))
    sets = _weight_sets(source)
    dates = {as_of: _reference_date(source, as_of) for as_of in set(lots["as_of"])}
    rows = []
    for lot in named_rows(lots):
        symbol = lot.symbol if isinstance(lot.symbol, str) and lot.symbol else None
        weights = class_weights(
            symbol=symbol, account=lot.account, lot=lot.lot, subkind=lot.subkind,
            sets=sets, date=dates[lot.as_of],
        )
        for asset_class, weight in weights.items():
            rows.append({
                "account": lot.account, "lot": lot.lot, "symbol": symbol,
                "security": symbol or _NO_SYMBOL, "owner": lot.owner,
                "subkind": lot.subkind, "tax_treatment": lot.tax_treatment,
                "asset_class": asset_class, "weight": weight, "balance": lot.balance * weight,
                "as_of": lot.as_of,
            })
    columns = ["account", "lot", "symbol", "security", "owner", "subkind", "tax_treatment",
               "asset_class", "weight", "balance", "as_of"]
    return pd.DataFrame(rows, columns=columns)


def asset_classes_present(source: Connection,
                          month: str | None = None,
                          ) -> list[str]:
    """The asset classes some lot has a balance in, in display order (``unclassified`` last)."""
    present = set(lot_classes(source, month)["asset_class"])
    return [c for c in [*registries.ASSET_CLASSES, UNCLASSIFIED] if c in present]


def portfolio_allocation(
    source: Connection,
    month: str | None = None,
    by: str | None = None,
    exclude: Iterable[str] = (),
) -> pd.DataFrame:
    """Balance per asset class (rows, in registry order, ``unclassified`` last, then a
    ``Total`` row). Without ``by`` the columns are ``balance`` and ``percent``; with it
    (one of :data:`GROUPINGS`; ``security`` splits by symbol) there is a balance column per group plus ``Total``, and
    a ``percent`` column of the overall total. Classes in ``exclude`` are left out, and
    the total and percentages cover what remains (exclude ``real_estate`` and
    ``unclassified`` to see what has been classified among the liquid holdings)."""
    if by is not None and by not in GROUPINGS:
        raise ValueError(f"cannot group by {by!r}; choose from {', '.join(GROUPINGS)}")
    known = [*registries.ASSET_CLASSES, UNCLASSIFIED]
    unknown = sorted(set(exclude) - set(known))
    if unknown:
        raise ValueError(f"unknown asset class {unknown[0]!r}; choose from {', '.join(known)}")
    frame = lot_classes(source, month)
    frame = frame[~frame["asset_class"].isin(set(exclude))]
    order = [*registries.ASSET_CLASSES, UNCLASSIFIED]
    if by is None:
        table = frame.groupby("asset_class")[["balance"]].sum()
    else:
        table = frame.pivot_table(index="asset_class", columns=by, values="balance",
                                  aggfunc="sum", fill_value=0.0)
        table.columns.name = None
        table["Total"] = table.sum(axis=1)
    table = table.reindex([c for c in order if c in table.index])
    total = table["Total" if by else "balance"].sum()
    table["percent"] = table["Total" if by else "balance"] / total * 100 if total else 0.0
    table.loc["Total"] = table.sum()
    table.index.name = "asset_class"
    return table


def render_allocation_table(table: pd.DataFrame, title: str | None = None):
    """A ``rich`` table of :func:`portfolio_allocation`."""
    from rich.table import Table

    out = Table(title=title, header_style="bold", show_lines=False)
    out.add_column("Asset class", no_wrap=True)
    for column in table.columns:
        out.add_column("%" if column == "percent" else str(column), justify="right", no_wrap=True)
    for name, row in table.iterrows():
        cells = [f"{v:,.1f}%" if column == "percent" else f"${v:,.2f}"
                 for column, v in row.items()]
        out.add_row(str(name), *cells, style="bold" if name == "Total" else None)
    return out
