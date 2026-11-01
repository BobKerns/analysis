"""Helpers for reading pandas frames under a type checker."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import pandas as pd


def named_rows(frame: pd.DataFrame, *, index: bool = True) -> Iterable[Any]:
    """``frame.itertuples(index=index)``, typed so a row's fields read by name.

    pandas types each row as a bare ``tuple``, so a type checker rejects
    ``row.symbol`` although the namedtuple has it."""
    return frame.itertuples(index=index)


def datetime_index(data: pd.DataFrame | pd.Series) -> pd.DatetimeIndex:
    """``data.index``, checked to be a ``DatetimeIndex``.

    pandas types ``.index`` as a plain ``Index``, which has no ``.year``; a
    frame indexed by anything else is a bug, so it fails here."""
    index = data.index
    if not isinstance(index, pd.DatetimeIndex):
        raise TypeError(f"expected a DatetimeIndex, got {type(index).__name__}")
    return index


# pandas attaches DatetimeIndex's date fields (year, month, ...) at import
# time, so a type checker can't see them; these read them in one place.


def index_years(data: pd.DataFrame | pd.Series) -> pd.Index:
    """The year of each entry of ``data``'s ``DatetimeIndex``."""
    return datetime_index(data).year  # ty: ignore[unresolved-attribute]


def index_months(data: pd.DataFrame | pd.Series) -> pd.Index:
    """The month (1-12) of each entry of ``data``'s ``DatetimeIndex``."""
    return datetime_index(data).month  # ty: ignore[unresolved-attribute]


def clip_years[T: pd.DataFrame | pd.Series](data: T, years: tuple[int, int] | None) -> T:
    """The rows of ``data`` in ``years`` (first, last; None keeps every row).

    ``data`` is indexed by month (a ``DatetimeIndex`` or ``PeriodIndex``, or
    ISO month strings) or by year (integers, or their strings). The
    notebooks' time-range picker and the widget-free charts clip with this,
    so they agree on which rows a range keeps."""
    if years is None or data.empty:
        return data
    year = row_years(data)
    return data[(year >= years[0]) & (year <= years[1])]


def row_years(data: pd.DataFrame | pd.Series) -> pd.Index:
    """The calendar year of each row of ``data``, indexed as ``clip_years``
    takes it: by month (a ``DatetimeIndex`` or ``PeriodIndex``, or ISO month
    strings) or by year (integers, or their strings)."""
    index = data.index
    if isinstance(index, (pd.DatetimeIndex, pd.PeriodIndex)):
        return pd.Index([moment.year for moment in index])
    return pd.Index([int(str(value)[:4]) for value in index])


def by_year_and_label(tables: Mapping[str, pd.DataFrame], label: str = "recipe") -> pd.DataFrame:
    """Year-indexed ``tables``, one per ``label`` (a recipe, say), as one
    table indexed by (year, label), the labels in the order given within
    each year."""
    combined = pd.concat(dict(tables), names=[label, "year"])
    return combined.swaplevel().sort_index(level="year", sort_remaining=False, kind="stable")
