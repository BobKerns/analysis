"""The units of a ``Params`` field (#326, #510): how a value is written in a
file's ``params_json`` and what the code receives. Shared by strategies
(:mod:`analysis.sim.strategy_api`) and tax calculators
(:mod:`analysis.sim.tax_api`), which re-export them."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from types import UnionType
from typing import Annotated, Union, get_args, get_origin, get_type_hints

__all__ = ["Account", "Dollars", "Percent", "Tag", "TargetSet", "Unit", "param_types",
           "param_units", "percent"]


@dataclass(frozen=True)
class Unit:
    """How a ``Params`` field is written in ``params_json`` and what the code
    receives: ``percent`` ("5%" -> 50000 millionths), ``dollars`` ("1200.00"
    -> 120000 cents), ``tag`` (a tag ``role:name``), ``target_set`` (a set
    in ``targets.csv``), ``account`` (an account's name)."""

    name: str


Percent = Annotated[int, Unit("percent")]
"""A share, written ``"5%"`` and received as millionths (50000)."""
Dollars = Annotated[int, Unit("dollars")]
"""Money, written ``"1200.00"`` and received as cents (120000)."""
Tag = Annotated[str, Unit("tag")]
"""A tag ``role:name``; the loader checks something in the scenario carries
it (an account, for a strategy; an account or a rule, for a tax calculator)."""
TargetSet = Annotated[str, Unit("target_set")]
"""A target set's name in ``targets.csv``; the loader checks it exists."""
Account = Annotated[str, Unit("account")]
"""An account's name; the loader checks the scenario defines it."""


def percent(text: str) -> int:
    """``percent("5%")`` -> 50000: a ``Percent`` field's default in the units it
    is received in."""
    from analysis.sim import units  # noqa: PLC0415

    return units.percent_to_micros(text)


def _hints(params: type) -> dict[str, object]:
    if not dataclasses.is_dataclass(params):
        raise TypeError(f"Params must be a dataclass, not {params!r}")
    return get_type_hints(params, include_extras=True)


def _value_hint(hint: object) -> object:
    """An optional field's (``Dollars | None``) value type."""
    if get_origin(hint) in (Union, UnionType):
        args = [a for a in get_args(hint) if a is not type(None)]
        return next((a for a in args if get_origin(a) is Annotated), args[0] if len(args) == 1 else hint)
    return hint


def param_units(params: type) -> dict[str, Unit | None]:
    """Each ``Params`` field's :class:`Unit`, or None for a plain JSON value.
    An optional field (``Dollars | None = None``) has its value's unit, and
    takes ``null`` for "none"."""
    hints = _hints(params)
    out: dict[str, Unit | None] = {}
    for f in dataclasses.fields(params):
        hint = _value_hint(hints.get(f.name))
        unit = None
        if get_origin(hint) is Annotated:
            unit = next((a for a in get_args(hint)[1:] if isinstance(a, Unit)), None)
        out[f.name] = unit
    return out


def param_types(params: type) -> dict[str, type | None]:
    """Each plain ``Params`` field's JSON type (``str``, ``int``, ``float``,
    ``bool``), or None where it isn't one of those (a unit, a list)."""
    hints = _hints(params)
    out: dict[str, type | None] = {}
    for f in dataclasses.fields(params):
        hint = _value_hint(hints.get(f.name))
        out[f.name] = hint if hint in (str, int, float, bool) else None  # type: ignore[assignment]
    return out
