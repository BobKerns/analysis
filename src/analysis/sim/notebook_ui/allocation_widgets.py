"""Portfolio allocation widget: the whole portfolio by asset class, optionally split
by account, owner, subkind or tax treatment, with a month for a run (ADR-003 §4.1b)."""

from __future__ import annotations

import pandas as pd
from IPython.display import clear_output, display
from ipywidgets import widgets

from analysis.sim.allocation import (
    _NO_SPLIT, Grouping, UNCLASSIFIED, asset_classes_present, lot_classes, portfolio_allocation,
)
from analysis.sim.allocation_charts import allocation_figure
from analysis.sim.connection import Connection, RunConnection
from analysis.sim.notebook_ui.holdings_widgets import _label, _month_dropdown
from analysis.sim.notebook_ui.pickers import (
    AssetClassPicker, GroupingPicker, TimeRangePicker, controls_row, observe_while_shown, run_years,
)
from analysis.sim.notebook_ui.styled import StyledMarkdown, show_figure

__all__ = ["allocation_drift_widget", "allocation_view", "drift_choices", "portfolio_allocation_widget"]



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


EVERY_ASSET_LOT = "(every asset lot)"


def drift_choices(con) -> tuple[list[str], list[str]]:
    """A run's target sets, and the portfolios a drift view offers: every
    asset lot, then each tag an asset account carries."""
    from analysis.sim import drift

    sets = sorted(drift.target_sets(con)) if drift.has_table(con, "allocation_target") else []
    tags = [t for (t,) in con.execute(
        "SELECT DISTINCT t.role || ':' || t.name FROM account_tag at JOIN tag t ON t.id = at.tag_id "
        "JOIN account a ON a.id = at.account_id WHERE a.kind = 'asset' ORDER BY 1")]
    return sets, [EVERY_ASSET_LOT, *tags]


def allocation_drift_widget(
    rc: RunConnection, time_range: TimeRangePicker | None = None, **_,
) -> widgets.VBox:
    """How far the run's portfolio strays from a target set, month by month
    (#359), as ``sim allocation --drift`` tabulates it by year: a line per
    asset class (share less target, percentage points; above zero is
    overweight), the band of a strategy steering to that set shaded, and a
    tick for each month a strategy decided. Defaults to a strategy's
    targets and portfolio."""
    from analysis.sim import drift
    from analysis.sim.drift_charts import drift_figure
    from analysis.sim.schema import connect_sim_file

    time_range = time_range or TimeRangePicker()
    time_range.cover(run_years(rc))
    # Kept open while the widget is shown, as a RunConnection is.
    con = connect_sim_file(f"file:{rc.run_path}?mode=ro", uri=True)
    sets, portfolios = drift_choices(con)
    if not sets:
        out = widgets.Output()
        with out:
            print(f"No target sets in {rc.name}; `sim target set` adds one.")
        con.close()
        return widgets.VBox([out])
    try:
        target_set, portfolio, _band = drift.choose(con)
    except ValueError:  # several sets and no strategy to pick one
        target_set, portfolio = sets[0], None
    watches = drift.watches(con)
    decided: dict[str, list[str]] = {}
    if drift.has_table(con, "strategy_decision"):
        for name, month in con.execute(
            "SELECT DISTINCT st.name, s.month FROM strategy_decision d JOIN simulation_step s "
            "ON s.id = d.step_id JOIN strategy st ON st.id = d.strategy_id ORDER BY 1, 2"
        ):
            decided.setdefault(name, []).append(month)
    frames: dict[tuple[str, str | None], pd.DataFrame] = {}
    sets_picker = widgets.Dropdown(options=sets, value=target_set, description="Targets")
    portfolio_picker = widgets.Dropdown(options=portfolios, value=portfolio or EVERY_ASSET_LOT,
                                        description="Portfolio")
    chart = widgets.Output()
    table = widgets.Output()

    def _redraw(*_):
        chosen = None if portfolio_picker.value == EVERY_ASSET_LOT else portfolio_picker.value
        key = (sets_picker.value, chosen)
        if key not in frames:
            frames[key] = drift.drift_by_month(con, sets_picker.value, portfolio=chosen)
        frame = frames[key]
        mine = [w for w in watches if w.target_set == sets_picker.value and w.portfolio == chosen]
        band = next((w.band for w in mine if w.band is not None), None)
        months = sorted({m for w in mine for m in decided.get(w.strategy, [])})
        whose = f"accounts tagged {chosen}" if chosen else "every asset lot"
        low, high = time_range.value
        with chart:
            clear_output(wait=True)
            fig = drift_figure(frame, f"Drift from {sets_picker.value} ({whose}; {rc.label})", band=band,
                               decisions=months, years=(low, high), deaths=rc.deaths())
            show_figure(fig)
        with table:
            clear_output(wait=True)
            rows = drift.year_end_rows(frame)
            rows = rows[[low <= int(str(m)[:4]) <= high for m in rows.index]]
            display(rows.style.format("{:+.1f}"))
            if mine:
                display(StyledMarkdown(
                    "Share less target, percentage points; positive is overweight. Strategies on these "
                    f"targets: {', '.join(w.strategy for w in mine)}."))

    sets_picker.observe(_redraw, names="value")
    portfolio_picker.observe(_redraw, names="value")
    observe_while_shown(time_range, _redraw)
    _redraw()
    return widgets.VBox([controls_row(sets_picker, portfolio_picker, time_range), chart, table])
