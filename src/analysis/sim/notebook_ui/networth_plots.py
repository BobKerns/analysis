"""Net-worth notebook widgets: one function per section, each self-contained.

All take a :class:`~analysis.sim.connection.RunConnection` and pull their
data from its cached derived-data methods and :mod:`analysis.sim.reports`
-- no notebook-local pandas code. The charts are
:mod:`analysis.sim.run_charts`' (#327), the ones ``sim networth`` and
``sim report ... --graph`` draw.
"""

from __future__ import annotations

import ipywidgets as widgets
import pandas as pd
from IPython.display import clear_output, display

from analysis.sim import reports, run_charts
from analysis.sim.connection import RunConnection, year_end_values
from analysis.sim.notebook_ui.pickers import (
    AfterTaxPicker, DollarModePicker, TimeRangePicker, controls_row, observe_while_shown, run_years,
)
from analysis.sim.types import DollarMode
from analysis.sim.notebook_ui.styled import StyledMarkdown, show_figure


def net_worth_widget(rc: RunConnection,
                    *, member: int = 0,
                    time_range: TimeRangePicker | None = None,
                    after_tax: AfterTaxPicker | None = None,
                    **_) -> widgets.VBox:
    """Year-end table + monthly chart of net worth (nominal and CPI-constant),
    pre-tax or after tax (#251); after tax, the table also shows the
    deferred and unpaid tax taken off."""
    time_range = time_range or TimeRangePicker()
    after_tax = after_tax or AfterTaxPicker()
    time_range.cover(run_years(rc))

    table_out = widgets.Output()
    chart_out = widgets.Output()

    def _redraw(*_):
        taxed = after_tax.value
        nw = time_range.clip(
            rc.after_tax_net_worth_frame() if taxed else rc.net_worth_frame())
        columns = ["nominal", "cpi_constant", *(["deferred_tax", "unpaid_tax"] if taxed else [])]
        year_end = year_end_values(nw[columns])
        year_end.index.name = "year"
        suffix = "" if member == 0 else f"#{member}"
        with chart_out:
            clear_output(wait=True)
            show_figure(run_charts.net_worth_figure(
                nw[["nominal", "cpi_constant"]].rename(columns={"cpi_constant": "CPI-constant"}),
                f"{'After-tax net' if taxed else 'Net'} worth ({rc.label}{suffix})",
                deaths=rc.deaths()))
        with table_out:
            clear_output(wait=True)
            display(year_end.style.format("${:,.0f}"))

    observe_while_shown(time_range, _redraw)
    observe_while_shown(after_tax, _redraw)
    _redraw()

    return widgets.VBox([controls_row(after_tax, time_range), chart_out, table_out])


def _nothing_to_chart(chart: pd.DataFrame, what: str, rc: RunConnection) -> bool:
    """Say so, and return True, when ``chart`` has no rows to draw.

    An empty scenario (no accounts) or a time range holding none of the
    run's years leaves none (#348).
    """
    if not chart.empty:
        return False
    print(f"No {what} to chart in this run ({rc.label}).")
    return True


def _flows(rc: RunConnection, side: reports.FlowSide, mode: DollarMode) -> pd.DataFrame:
    """``sim report income|expenses``' groups by category and their total,
    less a category with nothing in any year, as the report leaves it out."""
    frame = reports.without_empty_groups(
        reports.flows_by_year(rc, side, mode=mode, story=False)
        .rename(columns={reports.ECONOMIC_INCOME: "total"}),
        keep={"total"})
    return frame[[*run_charts.flow_groups(frame), "total"]]


def _flow_widget(rc: RunConnection, side: reports.FlowSide, dollar_mode: DollarModePicker,
                 time_range: TimeRangePicker, caveat: str | None = None) -> widgets.VBox:
    time_range.cover(run_years(rc))
    table_out = widgets.Output()
    chart_out = widgets.Output()
    extra = []
    if caveat is not None:
        caveat_out = widgets.Output()
        with caveat_out:
            display(StyledMarkdown(caveat))
        extra.append(caveat_out)
    # reports caches nothing: build each mode's frame once, so moving the
    # year slider only clips it rather than re-reading the run's events.
    by_mode: dict[DollarMode, pd.DataFrame] = {}

    def _redraw(*_):
        mode = dollar_mode.value
        if mode not in by_mode:
            by_mode[mode] = _flows(rc, side, mode)
        yearly = time_range.clip(by_mode[mode])
        with table_out:
            clear_output(wait=True)
            display(yearly.style.format("${:,.0f}"))
        with chart_out:
            clear_output(wait=True)
            if _nothing_to_chart(yearly, side, rc):
                return
            show_figure(run_charts.flows_figure(
                yearly, f"Annual {side} by category ({DollarMode(mode).label}) ({rc.label})",
                deaths=rc.deaths()))

    observe_while_shown(dollar_mode, _redraw)
    observe_while_shown(time_range, _redraw)
    _redraw()
    return widgets.VBox([controls_row(dollar_mode, time_range), chart_out, *extra, table_out])


def income_widget(rc: RunConnection,
                  dollar_mode: DollarModePicker | None = None,
                  time_range: TimeRangePicker | None = None, **_,
                  ) -> widgets.VBox:
    """Yearly income table + a stacked bar chart by category, in nominal or
    CPI-constant dollars: ``sim report income``'s categories."""
    return _flow_widget(rc, "income", dollar_mode or DollarModePicker(),
                        time_range or TimeRangePicker())


_EXPENSES_CAVEAT = (
    "_Note that expenses in CPI-constant dollars decrease slightly, because "
    "constant-valued expenses scale down with inflation._  \n"
    "_For example, fixed-rate mortgage expenses do not increase with inflation, "
    "so after compensating for inflation, they appear smaller._  \n"
    "_Fixed expenses are a small part of the total, so the overall effect is "
    "small, but it is present._  \n"
)


def expenses_widget(rc: RunConnection,
                    dollar_mode: DollarModePicker | None = None,
                    time_range: TimeRangePicker | None = None,
                    **_) -> widgets.VBox:
    """Yearly expenses table + a stacked bar chart by category, in nominal or
    CPI-constant dollars: ``sim report expenses``' categories."""
    return _flow_widget(rc, "expenses", dollar_mode or DollarModePicker(),
                        time_range or TimeRangePicker(), caveat=_EXPENSES_CAVEAT)


def gains_widget(rc: RunConnection,
                 dollar_mode: DollarModePicker | None = None,
                 time_range: TimeRangePicker | None = None,
                 **_) -> widgets.VBox:
    """Yearly realized-vs-unrealized gains table + chart, with an expenses overlay."""
    dollar_mode = dollar_mode or DollarModePicker()
    time_range = time_range or TimeRangePicker()
    time_range.cover(run_years(rc))

    table_out = widgets.Output()
    chart_out = widgets.Output()

    def _redraw(*_):
        mode = dollar_mode.value
        gains_yearly = time_range.clip(reports.gains_by_year(rc, mode))
        with table_out:
            clear_output(wait=True)
            display(gains_yearly.style.format("${:,.0f}"))
        with chart_out:
            clear_output(wait=True)
            if _nothing_to_chart(gains_yearly, "gains", rc):
                return
            show_figure(run_charts.gains_figure(
                gains_yearly, f"Realized vs. unrealized gains ({DollarMode(mode).label}) ({rc.label})",
                deaths=rc.deaths()))

    observe_while_shown(dollar_mode, _redraw)
    observe_while_shown(time_range, _redraw)
    _redraw()

    return widgets.VBox([controls_row(dollar_mode, time_range), chart_out, table_out])


def economic_income_widget(rc: RunConnection,
                           dollar_mode: DollarModePicker | None = None,
                           time_range: TimeRangePicker | None = None,
                           after_tax: AfterTaxPicker | None = None,
                           **_) -> widgets.VBox:
    """Economic income carried down to the change in net worth (#249), as
    ``sim report income`` shows it: a chart of what each year added and
    spent, the year's table, and the tax reconciliation."""
    dollar_mode = dollar_mode or DollarModePicker()
    time_range = time_range or TimeRangePicker()
    after_tax = after_tax or AfterTaxPicker()
    time_range.cover(run_years(rc))

    table_out = widgets.Output()
    chart_out = widgets.Output()

    def _redraw(*_):
        mode = dollar_mode.value
        taxed = after_tax.value

        # The reconciliation keeps its reconciliation_year (the April a tax
        # year settles in), which is a year, not money.
        reconciliation = rc.tax_reconciliation_by_year()
        money_columns = [c for c in reconciliation.columns if c != "reconciliation_year"]
        if mode == DollarMode.CPI_CONSTANT:
            reconciliation = reconciliation.assign(
                **rc.to_cpi_constant_annual(reconciliation[money_columns]))
        reconciliation = time_range.clip(reconciliation)
        income = reports.flows_by_year(rc, "income", mode=mode, after_tax=taxed)
        story = time_range.clip(
            income[[c for c in (reports.ECONOMIC_INCOME, *reports.NET_WORTH_COLUMNS)
                    if c in income.columns]]
            .dropna(subset=[reports.NET_WORTH_CHANGE]))
        if story.empty:
            # An empty scenario (no accounts) has no net worth to follow.
            for out in (chart_out, table_out):
                with out:
                    clear_output(wait=True)
            with chart_out:
                print(f"No net worth in this run ({rc.label}).")
            return
        with chart_out:
            clear_output(wait=True)
            show_figure(run_charts.economic_income_figure(
                story, f"Economic income and net worth change "
                       f"({'after tax' if taxed else 'pre-tax'}, {DollarMode(mode).label}) ({rc.label})",
                deaths=rc.deaths()))

        with table_out:
            clear_output(wait=True)
            display(story.style.format("${:,.0f}", na_rep=""))
            display(reconciliation.style.format({c: "${:,.0f}" for c in money_columns}))

    observe_while_shown(dollar_mode, _redraw)
    observe_while_shown(time_range, _redraw)
    observe_while_shown(after_tax, _redraw)
    _redraw()
    return widgets.VBox([controls_row(dollar_mode, after_tax, time_range), chart_out, table_out])


def tax_widget(rc: RunConnection,
               dollar_mode: DollarModePicker | None = None,
               time_range: TimeRangePicker | None = None,
               **_) -> widgets.VBox:
    """Federal and state tax by year, as ``sim report tax`` shows it: a chart
    of each year's tax with its effective rate, then the table of AGI,
    deduction, taxable income and tax."""
    dollar_mode = dollar_mode or DollarModePicker()
    time_range = time_range or TimeRangePicker()
    time_range.cover(run_years(rc))

    table_out = widgets.Output()
    chart_out = widgets.Output()

    by_mode: dict[DollarMode, pd.DataFrame] = {}  # as in _flow_widget

    def _redraw(*_):
        mode = dollar_mode.value
        if mode not in by_mode:
            by_mode[mode] = reports.tax_by_year(rc, mode)
        whole = by_mode[mode]
        frame = time_range.clip(whole)
        for out in (chart_out, table_out):
            with out:
                clear_output(wait=True)
        if frame.empty:
            with chart_out:
                # Only the whole run says whether any tax was computed.
                if whole.empty:
                    print(f"No tax was computed in this run ({rc.label}).")
                else:
                    print(f"No tax in the years picked ({rc.label}).")
            return
        with chart_out:
            show_figure(run_charts.tax_figure(
                frame, f"Tax ({DollarMode(mode).label}) ({rc.label})", deaths=rc.deaths()))
        with table_out:
            formats = {c: {"effective_rate": "{:.1%}", "irmaa_tier": "{:.0f}"}.get(c, "${:,.0f}")
                       for c in frame.columns}
            display(frame.style.format(formats, na_rep=""))

    observe_while_shown(dollar_mode, _redraw)
    observe_while_shown(time_range, _redraw)
    _redraw()
    return widgets.VBox([controls_row(dollar_mode, time_range), chart_out, table_out])


def guard_rails_widget(rc: RunConnection,
                       dollar_mode: DollarModePicker | None = None,
                       time_range: TimeRangePicker | None = None,
                       **_) -> widgets.VBox:
    """Where the run stands against its scenario's guard rails (#305), as
    ``sim report guard-rails`` shows it: per measure, the year's measure
    against each rail, then each rail's cumulative margin (with the margin
    carried at the portfolio's return, dotted); the margins by year, red past
    a rail to stay inside and blue past a positive one; and a summary.

    Rails are read from the scenario's guard_rails.csv when shown, so an
    edited rail shows on the next draw without a re-run. Margins accumulate
    over the whole run; the years picked only limit what is drawn."""
    from analysis.sim import guard_rails
    from analysis.sim.guard_rail_charts import CARRIED_CAVEAT, guard_rail_figure

    dollar_mode = dollar_mode or DollarModePicker(DollarMode.CPI_CONSTANT)
    time_range = time_range or TimeRangePicker()
    time_range.cover(run_years(rc))
    carried = widgets.Checkbox(value=True, description="Carried at return",
                               indent=False, layout=widgets.Layout(width="auto"))
    chart_out = widgets.Output()
    table_out = widgets.Output()

    def _read_rails() -> list[guard_rails.GuardRail] | None:
        """The rails as the file has them now, or None, said why in chart_out."""
        try:
            scenarios_dir = rc.scenario.scenarios_dir
        except RuntimeError:  # a run file opened without its data repo
            print(f"{rc.name} was opened without its scenario, so its guard rails "
                  "can't be read; open it with open_run().")
            return None
        try:
            rails = guard_rails.run_rails(rc, scenarios_dir)
        except guard_rails.GuardRailError as exc:
            print(f"The guard rails of {rc.name} don't load; `sim check {rc.name}` lists:")
            for issue in exc.issues:
                print(f"  {issue}")
            return None
        if not rails:
            print(f"No guard rails in {rc.name}; `sim guard-rail add` adds one.")
            return None
        return rails

    with chart_out:
        if _read_rails() is None:
            return widgets.VBox([chart_out])

    def _redraw(*_):
        with chart_out:
            clear_output(wait=True)
            rails = _read_rails()
        if rails is None:
            with table_out:
                clear_output()
            return
        mode = dollar_mode.value
        unit = DollarMode(mode).label
        results = guard_rails.evaluate_run(rc, rails, mode)
        shown = [r for r in results if r.applies]
        low, high = time_range.value
        with chart_out:
            clear_output(wait=True)
            fig = guard_rail_figure(results, f"Guard rails ({rc.label})", unit=unit,
                                    carried=carried.value, years=(low, high),
                                    deaths=rc.deaths())
            show_figure(fig)
        senses = {}
        for r in shown:
            for suffix in ("", guard_rails.CUMULATIVE_SUFFIX, guard_rails.CARRIED_SUFFIX):
                senses[r.rail.name + suffix] = r.rail.sense

        def past_color(value: float, column: str) -> str:
            if pd.isna(value) or value >= 0 or column not in senses:
                return ""
            return "color: #d03b3b" if senses[column] == "negative" else "color: #2a78d6"

        with table_out:
            clear_output(wait=True)
            if not shown:
                print("No guard rail covers this run's years.")
                return
            margins = time_range.clip(guard_rails.margins_frame(shown, carried=carried.value))
            display(margins.style.format("${:,.0f}", na_rep="")
                    .apply(lambda col: [past_color(v, str(col.name)) for v in col]))
            display(guard_rails.summary_frame(shown).style.format(
                {"worst": "${:,.0f}", "end": "${:,.0f}", "rms_over": "${:,.0f}"}))
            display(StyledMarkdown(
                "A margin is the room left before a rail; negative is past it (red past a "
                "rail to stay inside, blue past a positive one). " + CARRIED_CAVEAT))

    observe_while_shown(dollar_mode, _redraw)
    observe_while_shown(time_range, _redraw)
    carried.observe(_redraw, names="value")
    _redraw()
    return widgets.VBox([controls_row(dollar_mode, time_range, carried), chart_out, table_out])
