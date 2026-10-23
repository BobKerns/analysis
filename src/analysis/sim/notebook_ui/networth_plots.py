"""Net-worth notebook widgets: one function per section, each self-contained.

All take a :class:`~analysis.sim.connection.RunConnection` and pull their
data from its cached derived-data methods -- no notebook-local pandas code.
"""

from __future__ import annotations

import ipywidgets as widgets
import matplotlib.pyplot as plt
import pandas as pd
from IPython.display import clear_output, display

from analysis.sim import reports
from analysis.sim.plotting import FRAME_ALPHA, mark_deaths
from analysis.sim.connection import (
    EXPENSE_CATEGORIES,
    GAIN_CATEGORIES,
    RunConnection,
    year_end_values,
    yearly_totals,
)
from analysis.sim.notebook_ui.pickers import (
    AfterTaxPicker, DollarModePicker, TimeRangePicker, controls_row, observe_while_shown, run_years,
)
from analysis.sim.types import INCOME_CATEGORIES, DollarMode
from analysis.sim.notebook_ui.styled import StyledMarkdown


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
        with chart_out:
            clear_output(wait=True)
            fig, ax = plt.subplots(figsize=(10, 5))
            ax.plot(nw.index, nw["nominal"], label="nominal")
            if nw["cpi_constant"].notna().any():
                ax.plot(nw.index, nw["cpi_constant"], label="CPI-constant", linestyle="--")
            ax.set_ylim(bottom=0)
            suffix = "" if member == 0 else f"#{member}"
            ax.set_title(f"{'After-tax net' if taxed else 'Net'} worth ({rc.label}{suffix})")
            ax.set_xlabel("month")
            ax.set_ylabel("USD")
            ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:,.0f}"))
            ax.grid(True, axis="y", alpha=0.3)
            mark_deaths(ax, rc.deaths())
            ax.legend(framealpha=FRAME_ALPHA)
            plt.xticks(rotation=90)
            plt.show()
        with table_out:
            clear_output(wait=True)
            display(year_end.style.format("${:,.0f}"))

    observe_while_shown(time_range, _redraw)
    observe_while_shown(after_tax, _redraw)
    _redraw()

    return widgets.VBox([controls_row(after_tax, time_range), chart_out, table_out])


def income_widget(rc: RunConnection,
                  dollar_mode: DollarModePicker | None = None,
                  time_range: TimeRangePicker | None = None, **_,
                  ) -> widgets.VBox:
    """Yearly income table + a nominal/CPI-constant stacked bar chart, by category."""
    dollar_mode = dollar_mode or DollarModePicker()
    time_range = time_range or TimeRangePicker()
    time_range.cover(run_years(rc))

    table_out = widgets.Output()
    chart_out = widgets.Output()

    def _redraw(*_):
        mode = dollar_mode.value
        mode_label = "CPI-constant" if mode == DollarMode.CPI_CONSTANT else "nominal"
        
        income = rc.income_by_category()
        income_based = (
            rc.to_cpi_constant(income) if mode == DollarMode.CPI_CONSTANT
            else income
        )
        income_yearly = time_range.clip(yearly_totals(income_based))

        chart = income_yearly
        with chart_out:
            clear_output(wait=True)
            fig, ax = plt.subplots(figsize=(10, 5))
            chart[INCOME_CATEGORIES].plot(
                kind="bar", stacked=True, ax=ax,
                color=["#1f77b4", "#2ca02c", "#ff7f0e"],
            )
            ax.set_title(f"Annual income by category ({mode_label}) ({rc.label})")
            ax.set_xlabel("year")
            ax.set_ylabel("USD")
            ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:,.0f}"))
            ax.grid(True, axis="y", alpha=0.3)
            mark_deaths(ax, rc.deaths(), years=chart.index)
            ax.legend(title="category", framealpha=FRAME_ALPHA)
            plt.show()
        with table_out:
            clear_output(wait=True)
            display(income_yearly.style.format("${:,.0f}"))

    observe_while_shown(dollar_mode, _redraw)
    observe_while_shown(time_range, _redraw)
    _redraw()

    return widgets.VBox([controls_row(dollar_mode, time_range), chart_out, table_out])


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
    """Yearly expenses table + a nominal/CPI-constant stacked bar chart, by category."""
    dollar_mode = dollar_mode or DollarModePicker()
    time_range = time_range or TimeRangePicker()
    time_range.cover(run_years(rc))

    table_out = widgets.Output()
    chart_out = widgets.Output()
    caveat_out = widgets.Output()
    with caveat_out:
        display(StyledMarkdown(_EXPENSES_CAVEAT))

    def _redraw(*_):
        mode = dollar_mode.value
        mode_label = "CPI-constant" if mode == DollarMode.CPI_CONSTANT else "nominal"

        expenses = rc.expenses_by_category()
        expenses_based = (
            rc.to_cpi_constant(expenses) if mode == DollarMode.CPI_CONSTANT
            else expenses
        )
        expenses_yearly = time_range.clip(yearly_totals(expenses_based))

        chart = expenses_yearly
        with chart_out:
            clear_output(wait=True)
            fig, ax = plt.subplots(figsize=(10, 5))
            chart[EXPENSE_CATEGORIES].plot(
                kind="bar", stacked=True, ax=ax,
                color=["#1f77b4", "#d62728", "#9467bd", "#8c564b", "#7f7f7f"],
            )
            ax.set_title(f"Annual expenses by category ({mode_label}) ({rc.label})")
            ax.set_xlabel("year")
            ax.set_ylabel("USD")
            ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:,.0f}"))
            ax.grid(True, axis="y", alpha=0.3)
            mark_deaths(ax, rc.deaths(), years=chart.index)
            ax.legend(title="category", framealpha=0.5)
            plt.show()
        with table_out:
            clear_output(wait=True)
            display(expenses_yearly.style.format("${:,.0f}"))

    observe_while_shown(dollar_mode, _redraw)
    observe_while_shown(time_range, _redraw)
    _redraw()

    return widgets.VBox([controls_row(dollar_mode, time_range), chart_out, caveat_out, table_out])


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
        mode_label = "CPI-constant" if mode == DollarMode.CPI_CONSTANT else "nominal"

        gains_nominal = rc.gains_by_category()
        gains_yearly = time_range.clip(
            rc.to_cpi_constant_annual(gains_nominal) if mode == DollarMode.CPI_CONSTANT
            else gains_nominal
        )
        chart = gains_yearly
        with chart_out:
            clear_output(wait=True)
            fig, ax = plt.subplots(figsize=(10, 5))
            years = chart.index.astype(str)
            chart[GAIN_CATEGORIES].plot(
                kind="bar", stacked=True, ax=ax,
                color=["#2ca02c", "#1f77b4", "#7f7f7f"],
            )
            ax.plot(
                years, chart["expenses"],
                label="expenses", color="#d62728", marker="o", linewidth=2,
            )
            ax.axhline(0, color="grey", linewidth=0.8)
            ax.set_title(f"Realized vs. unrealized gains ({mode_label}) ({rc.label})")
            ax.set_xlabel("year")
            ax.set_ylabel("USD")
            ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:,.0f}"))
            ax.grid(True, axis="y", alpha=0.3)
            mark_deaths(ax, rc.deaths(), years=chart.index)
            ax.legend(title="category", framealpha=0.5)
            plt.setp(ax.get_xticklabels(), rotation=90)
            plt.show()
        with table_out:
            clear_output(wait=True)
            display(gains_yearly.style.format("${:,.0f}"))

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
        mode_label = "CPI-constant" if mode == DollarMode.CPI_CONSTANT else "nominal"

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
        kept = "after_tax_income" if taxed else reports.ECONOMIC_INCOME
        tax = "tax" if taxed else "tax_paid"
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
            fig, ax = plt.subplots(figsize=(10, 5))
            years = story.index.astype(str)
            ax.bar(years, story[kept], label=kept.replace("_", " "),
                   color="#2ca02c", alpha=0.7)
            # What the year spent or lost, stacked below zero.
            bottom = pd.Series(0.0, index=story.index)
            for column, color in (("spending", "#d62728"), ("inflation", "#ff7f0e")):
                if column in story.columns:
                    ax.bar(years, -story[column], bottom=bottom,
                           label=column, color=color, alpha=0.7)
                    bottom = bottom - story[column]
            ax.plot(years, story[tax], label=tax.replace("_", " "),
                    color="#9467bd", marker="o", linewidth=2)
            ax.plot(years, story[reports.NET_WORTH_CHANGE], label="Δnet worth",
                    color="black", marker="o", linewidth=2)
            ax.axhline(0, color="grey", linewidth=0.8)
            ax.set_title(f"Economic income and net worth change "
                         f"({'after tax' if taxed else 'pre-tax'}, {mode_label}) ({rc.label})")
            ax.set_xlabel("year")
            ax.set_ylabel("USD")
            ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:,.0f}"))
            ax.grid(True, axis="y", alpha=0.3)
            mark_deaths(ax, rc.deaths(), years=story.index)
            ax.legend(framealpha=0.5)
            plt.setp(ax.get_xticklabels(), rotation=90)
            plt.show()

        with table_out:
            clear_output(wait=True)
            display(story.style.format("${:,.0f}", na_rep=""))
            display(reconciliation.style.format({c: "${:,.0f}" for c in money_columns}))

    observe_while_shown(dollar_mode, _redraw)
    observe_while_shown(time_range, _redraw)
    observe_while_shown(after_tax, _redraw)
    _redraw()
    return widgets.VBox([controls_row(dollar_mode, after_tax, time_range), chart_out, table_out])
