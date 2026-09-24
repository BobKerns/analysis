# Scenario descriptions for the notebook UI

from collections.abc import Collection

import pandas as pd
from IPython.display import display
from ipywidgets import widgets

from analysis.sim.notebook_ui.scenario_view import calendar_view, chains_view


def truncate(width):
    """A .style.format formatter that renders any value's first `width` characters."""
    return lambda value: str(value)[:width]


def _no_wrap(styled, skip: Collection[str] = ()):
    """Keep every cell on one line (except columns in ``skip``), so a wide table
    scrolls sideways instead of wrapping into tall rows."""
    columns = [c for c in styled.data.columns if c not in skip]
    return styled.set_properties(subset=columns, **{"white-space": "nowrap"})


def scrollable_output() -> widgets.Output:
    """An output widget that scrolls sideways when its content is wider than the page."""
    return widgets.Output(layout=widgets.Layout(overflow="auto", max_width="100%"))


def scrollable(styled) -> widgets.Output:
    """``styled`` in a container that scrolls sideways rather than overflowing."""
    out = scrollable_output()
    with out:
        display(styled)
    return out


def show_chains(sc):
    """
    Show only the interior chains (the ones not auto-created for a single account)
    from the scenario connection `sc`.

    Formats the "maximum", "amount", and "percent" columns for display.
    """
    chains = chains_view(sc)
    interior_chains = chains[chains["step"].notna()].copy()
    # threshold=0 means "no minimum" -- not meaningfully different from n/a.
    interior_chains["maximum"] = interior_chains["maximum"].mask(interior_chains["maximum"] == 0)
    styled = interior_chains.style.format({"maximum": "${:,.2f}",
                                  "amount": "${:,.2f}",
                                  "percent": "{:,.2f}%"},
                                 na_rep="-").hide(axis="index")
    return scrollable(_no_wrap(styled))


SCHEDULED_OPTIONAL_COLUMNS = {"id": False, "name": True, "priority": False, "description": True}
"""Scheduled-view columns a checkbox can hide, with whether each starts shown."""


def scheduled_styler(view: pd.DataFrame, hidden: Collection[str] = ()):
    """The scheduled view as a styled table, without the ``hidden`` columns and
    without the row numbers. Cells stay on one line, except the description,
    which may wrap."""
    shown = view.drop(columns=list(hidden))
    styled = shown.style.format(
        {"amount": "${:,.2f}", "start": truncate(7), "end": truncate(7)}, na_rep="-",
    ).hide(axis="index")
    styled = _no_wrap(styled, skip=["description"])
    styled = styled.set_properties(subset=["start", "end"], **{"width": "7ch"})
    if "description" in shown.columns:
        styled = styled.set_properties(
            subset=["description"], **{"white-space": "normal", "min-width": "30ch"},
        )
    return styled


def show_scheduled_view(sc):
    """
    Show the scheduled items from the scenario connection `sc`, with a checkbox
    for each optional column (id, name, priority, description) so the table can
    be narrowed to fit a small screen. The table scrolls sideways if it is still
    too wide.
    """
    from analysis.sim.notebook_ui.scenario_view import scheduled_view

    view = scheduled_view(sc)
    boxes = {
        column: widgets.Checkbox(
            value=default, description=column, indent=False,
            layout=widgets.Layout(width="auto"),
        )
        for column, default in SCHEDULED_OPTIONAL_COLUMNS.items()
    }
    out = scrollable_output()

    def _redraw(*_):
        hidden = [column for column, box in boxes.items() if not box.value]
        out.clear_output(wait=True)
        with out:
            display(scheduled_styler(view, hidden))

    for box in boxes.values():
        box.observe(_redraw, names="value")
    _redraw()
    return widgets.VBox([widgets.HBox(list(boxes.values())), out])


def show_calendar_view(sc):
    """
    Show the calendar items from the scenario connection `sc`.
    """
    styled = calendar_view(sc).style.format(
        {"amount": "${:,.2f}", "date": truncate(7)}, na_rep="-"
    ).hide(axis="index")
    styled = _no_wrap(styled, skip=["description"])
    return scrollable(styled.set_properties(subset=["date"], **{"width": "7ch"}))
