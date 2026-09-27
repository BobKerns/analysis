"""Portfolio allocation widget: the whole portfolio by asset class, optionally split
by account, owner, subkind or tax treatment, with a month for a run (ADR-003 §4.1b)."""

from __future__ import annotations

import matplotlib.pyplot as plt
import pandas as pd
from IPython.display import Markdown, display
from ipywidgets import widgets

from analysis.sim.allocation import (
    GROUPINGS, UNCLASSIFIED, asset_classes_present, lot_classes, portfolio_allocation,
)
from analysis.sim.connection import RunConnection, ScenarioConnection
from analysis.sim.notebook_ui.holdings_widgets import _label, _month_dropdown

__all__ = ["allocation_view", "portfolio_allocation_widget"]

_NO_SPLIT = "(none)"


def allocation_view(
    source: RunConnection | ScenarioConnection, by: str | None = None, month: str | None = None,
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


def _pie(table: pd.DataFrame) -> None:
    body = table.drop(index="Total")
    body = body[body["percent"] > 0]
    if body.empty:
        return
    fig, ax = plt.subplots(figsize=(4.5, 4.5))
    ax.pie(body["percent"], labels=body.index, autopct="%1.0f%%", startangle=90, counterclock=False)
    ax.axis("equal")
    plt.show()


def portfolio_allocation_widget(
    source: RunConnection | ScenarioConnection, by: str | None = None,
    exclude: tuple[str, ...] = (),
) -> widgets.VBox:
    """The portfolio by asset class, with a split dropdown, a checkbox per asset class
    (unchecked classes are left out, and the total covers the rest) and, for a run, a
    month. ``exclude`` names the classes that start unchecked."""
    split_picker = widgets.Dropdown(
        options=[_NO_SPLIT, *GROUPINGS], value=by or _NO_SPLIT, description="Split by:",
    )
    month_picker = _month_dropdown(source)
    include = {
        name: widgets.Checkbox(value=name not in exclude, description=name, indent=False,
                               layout=widgets.Layout(width="max-content"))
        for name in asset_classes_present(source)
    }
    out = widgets.Output()

    def _redraw(*_):
        split = None if split_picker.value == _NO_SPLIT else split_picker.value
        month = month_picker.value if month_picker is not None else None
        left_out = tuple(name for name, box in include.items() if not box.value)
        heading, table = allocation_view(source, split, month, left_out)
        out.clear_output(wait=True)
        with out:
            display(Markdown(heading))
            display(_styled(table))
            _pie(table)

    for control in (split_picker, month_picker, *include.values()):
        if control is not None:
            control.observe(_redraw, names="value")
    _redraw()
    box = widgets.VBox([
        widgets.HBox([p for p in (split_picker, month_picker) if p is not None]),
        widgets.HBox([widgets.Label("Include:"), *include.values()],
                     layout=widgets.Layout(flex_flow="row wrap")),
        out,
    ])
    return box
