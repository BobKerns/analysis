"""Which asset classes a lot holds (allocations.csv, ADR-003 §4.1b).

Kept free of pandas so the engine can use it per month (#326): a strategy's
view of the allocation and ``sim allocation``'s table come from the same
function, so the two can't disagree.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from analysis.sim import registries

__all__ = ["UNCLASSIFIED", "WeightSets", "class_weights", "is_lot_key", "lot_key", "weight_sets", "weights_at"]

UNCLASSIFIED = "unclassified"

type WeightSets = dict[str, list[tuple[str, dict[str, float]]]]
"""A key (a symbol, ``<account>:<lot>`` or target set) to its dated weight
sets, oldest first: ``(as_of, {asset_class: weight})``."""


def weight_sets[V](rows: Iterable[tuple[str, str, str, V]]) -> dict[str, list[tuple[str, dict[str, V]]]]:
    """``(key, as_of, asset_class, weight)`` rows grouped as :data:`WeightSets`
    (weights as fractions there; micros or text for the loader and editors):
    every reader of allocations and targets builds them here."""
    by_key: dict[str, dict[str, dict[str, V]]] = {}
    for key, as_of, asset_class, weight in rows:
        by_key.setdefault(key, {}).setdefault(as_of, {})[asset_class] = weight
    return {key: sorted(by_date.items()) for key, by_date in by_key.items()}


def weights_at(sets: Sequence[tuple[str, Mapping[str, float]]], date: str) -> Mapping[str, float]:
    """The latest set on or before ``date`` (``as_of`` '' always applies), else the earliest."""
    chosen = sets[0][1]
    for as_of, weights in sets:
        if as_of <= date:
            chosen = weights
    return chosen


def lot_key(account: str, lot: str) -> str:
    """The ``allocations.csv`` key that classes a lot with no symbol (#326):
    ``<account>:<lot>``. A ticker never holds a colon, so the two can't collide."""
    return f"{account}:{lot}"


def is_lot_key(symbol: str) -> bool:
    """Whether an ``allocations.csv`` symbol is a ``<account>:<lot>`` key."""
    return ":" in symbol


def class_weights(
    *, symbol: str | None, account: str, lot: str, subkind: str,
    sets: Mapping[str, Sequence[tuple[str, Mapping[str, float]]]], date: str,
) -> dict[str, float]:
    """One asset lot's split across asset classes on ``date``: its symbol's
    allocations.csv set, else its ``<account>:<lot>`` set, else the class its
    symbol (``CASH``, ``CD``) or account subkind (cash, real estate, ...)
    means, else ``unclassified``. Shared by ``sim allocation`` and the
    strategies' context (#326), so the two can't disagree."""
    described = (sets.get(symbol) if symbol else None) or sets.get(lot_key(account, lot))
    if described:
        return dict(weights_at(described, date))
    if symbol and symbol in registries.SYMBOL_ASSET_CLASS:
        return {registries.SYMBOL_ASSET_CLASS[symbol]: 1.0}
    return {registries.SUBKIND_ASSET_CLASS.get(subkind, UNCLASSIFIED): 1.0}
