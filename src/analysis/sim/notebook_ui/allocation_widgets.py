"""Portfolio allocation widget: the whole portfolio by asset class, optionally split
by account, owner, subkind or tax treatment, with a month for a run (ADR-003 §4.1b)."""

from __future__ import annotations

import pandas as pd
from IPython.display import display
from ipywidgets import widgets

from analysis.sim.allocation import (
    _NO_SPLIT, Grouping, UNCLASSIFIED, asset_classes_present, lot_classes, portfolio_allocation,
)
from analysis.sim.allocation_charts import allocation_figure
from analysis.sim.connection import Connection
from analysis.sim.notebook_ui.holdings_widgets import _label, _month_dropdown
from analysis.sim.notebook_ui.pickers import AssetClassPicker, GroupingPicker
from analysis.sim.notebook_ui.styled import StyledMarkdown, show_figure

__all__ = ["allocation_view", "portfolio_allocation_widget"]



def allocation_view(
    source: Connection,
    by: Grouping | None = None, month: str | None = None,
    exclude: tuple[str, ...] = (),
) -> tuple[str, pd.DataFrame]:
    """Heading and table (asset classes by row, with a Total row) the widget shows."""
    as_of = lot_classes(source, month)["as_of"]
    heading = f"**Asset allocation** as of {as_of.iloc[0] if len(as_of) else 'n/a'} ({_label(source)})"
    if exclude:
        heading += f", excluding {', '.join(exclude)}"
    if UNCLASSIFIED in portfolio_allocation(source, month, exclude=exclude).index:
        heading += "  \n*unclassified: lots whose symbol has no rows in allocations.csv*"
    return heading, portfolio_allocation(source, month, by, exclude)


def _styled(table: pd.DataFrame):
    money = [c for c in table.columns if c != "percent"]
    return table.style.format({**dict.fromkeys(money, "${:,.2f}"), "percent": "{:.1f}%"})


def portfolio_allocation_widget(
    source: Connection,
    by: Grouping | None = None,
    exclude: tuple[str, ...] = (), **_,
) -> widgets.VBox:
    """The portfolio by asset class, with a split dropdown, a checkbox per asset class
    (unchecked classes are left out, and the total covers the rest) and, for a run, a
    month. ``exclude`` names the classes that start unchecked."""
    split_picker = GroupingPicker(value=by)
    month_picker = _month_dropdown(source)
    include = AssetClassPicker(asset_classes_present(source), exclude=exclude)
    # }
    out = widgets.Output()
    pie = widgets.Output()

    def _redraw(*_):
        split = None if split_picker.value == _NO_SPLIT else split_picker.value
        month = month_picker.value if month_picker is not None else None
        left_out = tuple(name for name, value in include.value.items() if not value)
        heading, table = allocation_view(source, split, month, left_out)
        out.clear_output(wait=True)
        with out:
            display(StyledMarkdown(heading))
            display(_styled(table))
        pie.clear_output(wait=True)
        with pie:
            # A pie of the classes; split, a bar per class by the split (#327).
            show_figure(allocation_figure(table))

    for control in (split_picker, month_picker, include):
        if control is not None:
            control.observe(_redraw, names="value")
    _redraw()
    box = widgets.VBox([
        widgets.HBox([p for p in (split_picker, month_picker) if p is not None]),
        include,
        out,
        pie,
    ])
    return box
