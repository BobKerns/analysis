# Scenario descriptions for the notebook UI

import functools
import html
from collections.abc import Collection

import anywidget
import pandas as pd
import traitlets
from IPython.display import Markdown, display
from ipywidgets import widgets

from analysis.sim import chain_map
from analysis.sim.chain_graph import MERMAID_SCRIPT
from analysis.sim.notebook_ui.scenario_view import UNUSED, calendar_view, chain_table


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
    The named chains from the scenario connection ``sc``, one row per step,
    each chain a block under a rule, with its role and what uses it on its
    first row (``sim chain list`` in table form).

    A step's ``series`` is the inflation series (such as ``cpi``) that grows
    its ``maximum`` from the scenario's start, compounding at its ``cadence``.
    """
    table = chain_table(sc)
    first = table["chain"] != ""

    def separators(frame: pd.DataFrame) -> pd.DataFrame:
        css = pd.DataFrame("", index=frame.index, columns=frame.columns)
        css.loc[first & (frame.index > 0)] = "border-top: 2px solid #888"
        return css

    def used_by(text: str) -> str:
        # Names come from the data, so escape them before adding markup.
        return "<br>".join(_breakable(html.escape(line)) for line in text.split("\n"))

    styled = (table.style
              .apply(separators, axis=None)
              .map(lambda v: "color: red" if v == UNUSED else "", subset=["used_by"])
              .hide(axis="index")
              # A chain's name and uses sit level with its first step, not
              # centered on a tall row, and its rule runs unbroken across.
              .set_table_styles([{"selector": "td", "props": [("vertical-align", "top")]},
                                 {"selector": "", "props": [("border-collapse", "collapse")]}])
              .format({"maximum": "${:,.2f}", "percent": "{:,.2f}%",
                       "chain": _breakable, "draw_from": _breakable,
                       "lot": _breakable, "series": _breakable, "used_by": used_by},
                      na_rep="-"))
    return scrollable(_fit_to_width(styled, nowrap=["step", "maximum", "percent", "cadence"]))


@functools.cache
def _mermaid_gz() -> bytes:
    return MERMAID_SCRIPT.read_bytes()


class MermaidWidget(anywidget.AnyWidget):
    """Draws the Mermaid source ``chart``, again whenever it changes. A widget
    carries its own JavaScript, so it draws in VS Code, which runs no script
    an Output widget's HTML holds, as well as in JupyterLab.

    It draws with the Mermaid the CLI uses, shipped with the package, so it
    needs no network (issue #176). The gzipped script (about 1 MB) goes to
    the front end with each widget, as a binary buffer; the first widget on
    the page unpacks and runs it, and the rest reuse that copy."""

    # VS Code's widget renderer loads RequireJS, so a global ``define`` is
    # there, and the UMD-wrapped libraries Mermaid bundles call it instead
    # of filling in their exports: 11.17.2 fails as it loads (issue #153).
    # Hiding ``define`` while the script runs sends them down their CommonJS
    # path, as in JupyterLab. Each widget runs this module afresh, so the
    # load is shared through the page: a second widget loading while the
    # first waits would otherwise save the hidden ``undefined`` and restore it.
    # The script is npm's classic one, which leaves Mermaid on
    # ``globalThis.mermaid``; run as a module, its top-level names would stay
    # in the module and it fails. So it runs from a <script> tag, with a
    # blob: URL, which a page's content security policy treats like the
    # blob: URL anywidget imports this module from.
    _esm = """
    const loading = Symbol.for("analysis.MermaidWidget.mermaid");
    function load(model) {
        globalThis[loading] ??= (async () => {
            const gzipped = new Blob([model.get("_mermaid_gz")]);
            const script = await new Response(
                gzipped.stream().pipeThrough(new DecompressionStream("gzip"))).text();
            const url = URL.createObjectURL(new Blob([script], {type: "text/javascript"}));
            const tag = document.createElement("script");
            tag.src = url;
            const define = globalThis.define;
            globalThis.define = undefined;
            try {
                await new Promise((resolve, reject) => {
                    tag.onload = resolve;
                    tag.onerror = () => reject(new Error("couldn't run the Mermaid script"));
                    document.head.append(tag);
                });
            } finally {
                globalThis.define = define;
                tag.remove();
                URL.revokeObjectURL(url);
            }
            // A script that throws still fires onload, and leaves no Mermaid.
            const mermaid = globalThis.mermaid;
            if (!mermaid) throw new Error("the Mermaid script didn't load");
            mermaid.initialize({startOnLoad: false});
            return mermaid;
        })();
        return globalThis[loading];
    }
    let drawn = 0;
    function render({model, el}) {
        async function draw() {
            const chart = model.get("chart");
            try {
                const mermaid = await load(model);
                const {svg} = await mermaid.render(`chain-flow-${++drawn}`, chart);
                el.innerHTML = svg;
            } catch (error) {
                // Show the source, so a chart Mermaid can't draw is still readable.
                el.textContent = `${error}\n\n${chart}`;
                el.style.whiteSpace = "pre";
            }
        }
        model.on("change:chart", draw);
        draw();
    }
    export default {render};
    """
    chart = traitlets.Unicode("").tag(sync=True)
    _mermaid_gz = traitlets.Bytes().tag(sync=True)

    @traitlets.default("_mermaid_gz")
    def _default_mermaid_gz(self) -> bytes:
        return _mermaid_gz()


def show_chain_flow(sc, chain: str | None = None, **_):
    """One chain's flowchart, picked from a dropdown: every chain at once is too
    tangled to read. ``chain`` picks the one shown first. The same chart as
    ``sim chain show CHAIN --graph`` draws in the terminal (``--mermaid``
    prints its Mermaid source): arrows the way money moves through its
    steps and its lots' refill or overflow, with the picked chain
    highlighted. A checkbox adds the rules that use it, as ``--rules`` does,
    and a dropdown limits how far it reaches, as ``--depth`` does."""
    cmap = chain_map.chain_map(sc)
    out = scrollable_output()
    if not cmap.steps:
        with out:
            display(Markdown("No chains: every lot is still a one-step chain of its own."))
        return out
    names = sorted(cmap.steps)
    picker = widgets.Dropdown(options=names, value=chain if chain in names else names[0],
                              description="Chain:")
    depth = widgets.Dropdown(options=[("1 step", "1"), ("to lots", "lots"), ("all", "all")],
                             value="all", description="Depth:", layout=widgets.Layout(width="max-content"))
    rules = widgets.Checkbox(value=False, description="Rules", indent=False)
    chart = MermaidWidget(chart=chain_map.mermaid(cmap, [picker.value]),
                          layout=widgets.Layout(overflow="auto", max_width="100%"))

    def redraw(_change=None) -> None:
        chart.chart = chain_map.mermaid(cmap, [picker.value], rules=rules.value, depth=depth.value)

    for control in (picker, depth, rules):
        control.observe(redraw, names="value")
    return widgets.VBox([widgets.HBox([picker, depth, rules]), chart])


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
