"""The ensemble comparison's year-end tables (#298, #328): a baseline
recipe's against any number of others, by (year, recipe). The notebook's
``ensemble_table_widget`` shows them and ``sim ensemble compare`` prints
them, so they depend on neither ipywidgets nor click."""

from __future__ import annotations

from collections.abc import Callable, Mapping

import pandas as pd

from analysis.frames import by_year_and_label
from analysis.sim.connection import year_end_values
from analysis.sim.ensemble import EnsembleError, comparison_deltas, risk_summary
from analysis.sim.series_db import EnsembleSeries, pair_members
from analysis.sim.types import DollarMode

__all__ = ["RecipeTables"]


def _whole_run(frame: pd.DataFrame) -> pd.DataFrame:
    return frame


class RecipeTables:
    """Recipes' stores opened for a comparison, the first the baseline:
    each one's frame in ``mode`` dollars and, given a ``reserve``,
    CPI-constant, which the reserve is measured in. A recipe without
    ``account`` (or its ``lot``) has no frame, so no row in the tables."""

    def __init__(self, stores: Mapping[str, EnsembleSeries], account: str | None,
                 lot: str | None, mode: DollarMode, reserve: float | None, *,
                 after_tax: bool = False):
        self.stores = dict(stores)
        self.account, self.lot, self.after_tax = account, lot, after_tax
        self.mode, self.reserve = DollarMode(mode), reserve
        self.frames: dict[str, pd.DataFrame] = {}
        for name, store in self.stores.items():
            try:
                self.frames[name] = store.frame(account, self.mode, lot, after_tax=after_tax)
            except EnsembleError:
                pass
        self.cpi_frames: dict[str, pd.DataFrame] = {
            name: (
                frame if self.mode == DollarMode.CPI_CONSTANT
                else self.stores[name].frame(
                    account, DollarMode.CPI_CONSTANT, lot, after_tax=after_tax)
            )
            for name, frame in self.frames.items()
        } if reserve is not None else {}

    def missing(self) -> list[str]:
        """The recipes without the account compared."""
        return [name for name in self.stores if name not in self.frames]

    def pairs(self, baseline: str, variant: str) -> tuple[list[tuple[str, str]], str | None]:
        """The members of the two that drew the same future, or why none did."""
        return pair_members(self.stores[baseline].member_ids(),
                            self.stores[variant].member_ids())

    def cpi_pair(self, baseline: str, variant: str) -> tuple[pd.DataFrame, pd.DataFrame] | None:
        return (self.cpi_frames[baseline], self.cpi_frames[variant]) if self.cpi_frames else None

    def deltas(self, baseline: str, variant: str,
               clip: Callable[[pd.DataFrame], pd.DataFrame] = _whole_run) -> pd.DataFrame:
        """``variant`` against ``baseline`` by year end, in the years
        ``clip`` keeps (the deltas are taken over the whole run first)."""
        pairs, _unpaired = self.pairs(baseline, variant)
        table = year_end_values(clip(comparison_deltas(
            self.frames[baseline], self.frames[variant], pairs, self.reserve,
            self.cpi_pair(baseline, variant),
        )))
        table.index.name = "year"
        return table

    def deltas_table(self, baseline: str, variants: list[str],
                     clip: Callable[[pd.DataFrame], pd.DataFrame] = _whole_run) -> pd.DataFrame:
        """Each of ``variants`` that has a frame against ``baseline``, by
        (year, recipe). A column only some have (``P(ahead)``, for the
        recipes that pair) is blank in the others."""
        return by_year_and_label({
            name: self.deltas(baseline, name, clip) for name in variants if name in self.frames})

    def tail(self, name: str,
             clip: Callable[[pd.DataFrame], pd.DataFrame] = _whole_run) -> pd.DataFrame:
        """``name``'s tail means and reserve shares, by year end."""
        return year_end_values(clip(risk_summary(
            self.frames[name], self.reserve, self.cpi_frames.get(name))))

    def tails(self, clip: Callable[[pd.DataFrame], pd.DataFrame] = _whole_run) -> pd.DataFrame:
        """Every recipe's tail means and reserve shares, by (year, recipe)."""
        return by_year_and_label({name: self.tail(name, clip) for name in self.frames})
