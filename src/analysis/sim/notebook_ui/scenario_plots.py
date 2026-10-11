# Scenario descriptions for the notebook UI

from collections.abc import Collection

import anywidget
import pandas as pd
import traitlets
from IPython.display import Markdown, display
from ipywidgets import widgets

from analysis.sim import chain_map
from analysis.sim.notebook_ui.scenario_view import calendar_view, chains_view


def truncate(width):
    """A .style.format formatter that renders any value's first `width` characters."""
    return lambda value: str(value)[:width]


def _breakable(value) -> str:
    """Text whose underscores are line-break opportunities, so a long name wraps
    instead of widening the table past the page (a renderer that cannot scroll
    would clip it)."""
    return str(value).replace("_", "_<wbr>")


def _fit_to_width(styled, nowrap: Collection[str]):
    """Let every column wrap except ``nowrap`` (numbers and dates), and tighten the
    cell padding, so the table shrinks to fit a narrow window."""
    present = [c for c in styled.data.columns if c in nowrap]
    if present:
        styled = styled.set_properties(subset=present, **{"white-space": "nowrap"})
    return styled.set_table_styles(
        [{"selector": "th, td", "props": [("padding", "2px 6px")]}], overwrite=False,
    )


def scrollable_output() -> widgets.Output:
    """An output widget that scrolls sideways when its content is wider than the page."""
    return widgets.Output(layout=widgets.Layout(overflow="auto", max_width="100%"))


def scrollable(styled) -> widgets.Output:
    """``styled`` in a container that scrolls sideways rather than overflowing."""
    out = scrollable_output()
    with out:
        display(styled)
    return out


def show_chains(sc, **_):
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
                                  "percent": "{:,.2f}%",
                                  "chain": _breakable, "draw_from": _breakable,
                                  "lot": _breakable, "series": _breakable},
                                 na_rep="-").hide(axis="index")
    return scrollable(_fit_to_width(styled, nowrap=["step", "maximum", "percent", "cadence"]))


MERMAID_URL = "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.esm.min.mjs"
"""Where the flowchart widget loads Mermaid from."""


class MermaidWidget(anywidget.AnyWidget):
    """Draws the Mermaid source ``chart``, again whenever it changes. A widget
    carries its own JavaScript, so it draws in VS Code, which runs no script
    an Output widget's HTML holds, as well as in JupyterLab."""

    _esm = f"""
    import mermaid from "{MERMAID_URL}";
    mermaid.initialize({{startOnLoad: false}});
    let drawn = 0;
    function render({{model, el}}) {{
        async function draw() {{
            const chart = model.get("chart");
            try {{
                const {{svg}} = await mermaid.render(`chain-flow-${{++drawn}}`, chart);
                el.innerHTML = svg;
            }} catch (error) {{
                // Show the source, so a chart Mermaid can't draw is still readable.
                el.textContent = `${{error}}\n\n${{chart}}`;
                el.style.whiteSpace = "pre";
            }}
        }}
        model.on("change:chart", draw);
        draw();
    }}
    export default {{render}};
    """
    chart = traitlets.Unicode("").tag(sync=True)


def show_chain_flow(sc, chain: str | None = None, **_):
    """One chain's flowchart, picked from a dropdown: every chain at once is too
    tangled to read. ``chain`` picks the one shown first. The same chart as
    ``sim chain show CHAIN --mermaid``: its steps as numbered lines, the refill
    and overflow of its lots as arrows, and the rules that use it."""
    cmap = chain_map.chain_map(sc)
    out = scrollable_output()
    if not cmap.steps:
        with out:
            display(Markdown("No chains: every lot is still a one-step chain of its own."))
        return out
    names = sorted(cmap.steps)
    picker = widgets.Dropdown(options=names, value=chain if chain in names else names[0],
                              description="Chain:")
    chart = MermaidWidget(chart=chain_map.mermaid(cmap, [picker.value]),
                          layout=widgets.Layout(overflow="auto", max_width="100%"))
    picker.observe(lambda change: setattr(chart, "chart", chain_map.mermaid(cmap, [change.new])),
                   names="value")
    return widgets.VBox([picker, chart])


SCHEDULED_OPTIONAL_COLUMNS = {"id": False, "name": True, "priority": False, "description": True}
"""Scheduled-view columns a checkbox can hide, with whether each starts shown."""


_SCHEDULED_TEXT_COLUMNS = [
    "name", "from_account", "from_lot", "to_account", "to_lot", "cadence",
    "amount_rule_kind", "tax_character", "description",
]
_SCHEDULED_NOWRAP_COLUMNS = ["id", "priority", "amount", "start", "end"]


def scheduled_styler(view: pd.DataFrame, hidden: Collection[str] = ()):
    """The scheduled view as a styled table, without the ``hidden`` columns and
    without the row numbers. Text wraps (at underscores and spaces) and the
    padding is tight, so the table fits a narrow window; numbers and dates stay
    on one line."""
    shown = view.drop(columns=list(hidden))
    formats = {
        "amount": "${:,.2f}", "start": truncate(7), "end": truncate(7),
        **{column: _breakable for column in _SCHEDULED_TEXT_COLUMNS if column in shown.columns},
    }
    styled = shown.style.hide(axis="index").format(formats, na_rep="-")
    styled = _fit_to_width(styled, nowrap=_SCHEDULED_NOWRAP_COLUMNS)
    dates = [c for c in ("start", "end") if c in shown.columns]
    return styled.set_properties(subset=dates, **{"width": "7ch"}) if dates else styled


def show_scheduled_view(sc, **_):
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


def show_calendar_view(sc, **_):
    """
    Show the calendar items from the scenario connection `sc`.
    """
    view = calendar_view(sc)
    formats = {
        "date": truncate(7),
        **{c: _breakable for c in ("kind", "target", "account_1", "lot_1",
                                   "account_2", "lot_2", "description")},
    }
    styled = view.style.hide(axis="index").format(formats, na_rep="-")
    styled = _fit_to_width(styled, nowrap=["date", "amount"])
    styled = styled.set_properties(subset=["date"], **{"width": "7ch"})
    # Amounts are display text now (a rate or dollars, by kind): keep them
    # right-aligned like the numbers they are.
    return scrollable(styled.set_properties(subset=["amount"], **{"text-align": "right"}))
