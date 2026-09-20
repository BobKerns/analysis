"""Net-worth notebook widgets: one function per section, each self-contained.

All take a :class:`~analysis.sim.connection.RunConnection` and pull their
data from its cached derived-data methods -- no notebook-local pandas code.
"""

from __future__ import annotations
from statistics import mode

import ipywidgets as widgets
import matplotlib.pyplot as plt
import pandas as pd
from IPython.display import Markdown, clear_output, display

from analysis.sim.plotting import FRAME_ALPHA
from analysis.sim.connection import (
    EXPENSE_CATEGORIES,
    GAIN_CATEGORIES,
    RunConnection,
    year_end_values,
    yearly_totals,
)
from analysis.sim.notebook_ui.pickers import dollar_plot_mode
from analysis.sim.types import INCOME_CATEGORIES


def net_worth_widget(rc: RunConnection, **_) -> widgets.VBox:
    """Year-end table + monthly chart of net worth (nominal and CPI-constant)."""

    nw = rc.net_worth_frame()
    year_end = year_end_values(nw[["nominal", "cpi_constant"]])
    year_end.index.name = "year"

    table_out = widgets.Output()
    chart_out = widgets.Output()

    with chart_out:
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.plot(nw.index, nw["nominal"], label="nominal")
        if nw["cpi_constant"].notna().any():
            ax.plot(nw.index, nw["cpi_constant"], label="CPI-constant", linestyle="--")
        ax.set_ylim(bottom=0)
        ax.set_title(f"Net worth ({rc.label})")
        ax.set_xlabel("month")
        ax.set_ylabel("USD")
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:,.0f}"))
        ax.grid(True, axis="y", alpha=0.3)
        ax.legend(framealpha=FRAME_ALPHA)
        plt.xticks(rotation=90)
        plt.show()

        with table_out:
            clear_output(wait=True)
            display(year_end.style.format({"nominal": "${:,.0f}", "cpi_constant": "${:,.0f}"}))

    return widgets.VBox([chart_out, table_out])


def income_widget(rc: RunConnection, plot_mode: widgets.ToggleButtons | None = None) -> widgets.VBox:
    """Yearly income table + a nominal/CPI-constant stacked bar chart, by category."""
    plot_mode = plot_mode or dollar_plot_mode()

    table_out = widgets.Output()
    chart_out = widgets.Output()

    def _redraw(*_):
        mode = plot_mode.value
        mode_label = "CPI-constant" if mode == "cpi_constant" else "nominal"
        
        income = rc.income_by_category()
        income_based = (
            rc.to_cpi_constant(income) if mode == "cpi_constant"
            else income
        )
        income_yearly = yearly_totals(income_based)

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
            ax.legend(title="category", framealpha=FRAME_ALPHA)
            plt.show()
        with table_out:
            clear_output(wait=True)
            display(income_yearly.style.format("${:,.0f}"))

    plot_mode.observe(_redraw, names="value")
    _redraw()

    return widgets.VBox([plot_mode, chart_out, table_out])


_EXPENSES_CAVEAT = (
    "_Note that expenses in CPI-constant dollars decrease slightly, because "
    "constant-valued expenses scale down with inflation._  \n"
    "_For example, fixed-rate mortgage expenses do not increase with inflation, "
    "so after compensating for inflation, they appear smaller._  \n"
    "_Fixed expenses are a small part of the total, so the overall effect is "
    "small, but it is present._  \n"
)


def expenses_widget(rc: RunConnection, plot_mode: widgets.ToggleButtons | None = None) -> widgets.VBox:
    """Yearly expenses table + a nominal/CPI-constant stacked bar chart, by category."""
    plot_mode = plot_mode or dollar_plot_mode()

    table_out = widgets.Output()
    chart_out = widgets.Output()
    caveat_out = widgets.Output()
    with caveat_out:
        display(Markdown(_EXPENSES_CAVEAT))

    def _redraw(*_):
        mode = plot_mode.value
        mode_label = "CPI-constant" if mode == "cpi_constant" else "nominal"

        expenses = rc.expenses_by_category()
        expenses_based = (
            rc.to_cpi_constant(expenses) if mode == "cpi_constant"
            else expenses
        )
        expenses_yearly = yearly_totals(expenses_based)

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
            ax.legend(title="category", framealpha=0.5)
            plt.show()
        with table_out:
            clear_output(wait=True)
            display(expenses_yearly.style.format("${:,.0f}"))

    plot_mode.observe(_redraw, names="value")
    _redraw()

    return widgets.VBox([plot_mode, chart_out, caveat_out, table_out])


def gains_widget(rc: RunConnection, plot_mode: widgets.ToggleButtons | None = None) -> widgets.VBox:
    """Yearly realized-vs-unrealized gains table + chart, with an expenses overlay."""
    plot_mode = plot_mode or dollar_plot_mode()

    table_out = widgets.Output()
    chart_out = widgets.Output()

    def _redraw(*_):
        mode = plot_mode.value
        mode_label = "CPI-constant" if mode == "cpi_constant" else "nominal"

        gains_nominal = rc.gains_by_category()
        gains_yearly = (
            rc.to_cpi_constant_annual(gains_nominal) if mode == "cpi_constant"
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
            ax.legend(title="category", framealpha=0.5)
            plt.setp(ax.get_xticklabels(), rotation=90)
            plt.show()
        with table_out:
            clear_output(wait=True)
            display(gains_yearly.style.format("${:,.0f}"))

    plot_mode.observe(_redraw, names="value")
    _redraw()

    return widgets.VBox([plot_mode, chart_out, table_out])


def after_tax_widget(rc: RunConnection, plot_mode: widgets.ToggleButtons | None = None) -> widgets.VBox:
    """Tax reconciliation table + after-tax income vs. non-tax-expense chart."""
    plot_mode = plot_mode or dollar_plot_mode()

    table_out = widgets.Output()
    chart_out = widgets.Output()

    def _redraw(*_):
        mode = plot_mode.value
        mode_label = "CPI-constant" if mode == "cpi_constant" else "nominal"

        reconciliation_nominal = rc.tax_reconciliation_by_year()
        income_yearly_nominal = yearly_totals(rc.income_by_category())
        expenses_yearly_nominal = yearly_totals(rc.expenses_by_category())

        money_columns = [c for c in reconciliation_nominal.columns if c != "reconciliation_year"]
        reconciliation = rc.to_cpi_constant_annual(reconciliation_nominal) if mode == "cpi_constant" else reconciliation_nominal
        income_yearly = rc.to_cpi_constant_annual(income_yearly_nominal) if mode == "cpi_constant" else income_yearly_nominal
        expenses_yearly = rc.to_cpi_constant_annual(expenses_yearly_nominal) if mode == "cpi_constant" else expenses_yearly_nominal

        net = pd.DataFrame({
            "gross_income": income_yearly["total"],
            "tax_liability": rc.tax_liability_by_year(),
            "expenses": expenses_yearly["total"],
            "tax_expenses": expenses_yearly["taxes"],
        })
        net[["gross_income", "expenses", "tax_expenses"]] = net[[
            "gross_income", "expenses", "tax_expenses",
        ]].fillna(0)
        net["after_tax_income"] = net["gross_income"] - net["tax_liability"]
        net["non_tax_expenses"] = net["expenses"] - net["tax_expenses"]
        net["after_tax_surplus"] = net["after_tax_income"] - net["non_tax_expenses"]
        chart = rc.to_cpi_constant_annual(net) if mode == "cpi_constant" else net
        with chart_out:
            clear_output(wait=True)
            fig, ax = plt.subplots(figsize=(10, 5))
            years = chart.index.astype(str)
            ax.bar(
                years, chart["after_tax_income"],
                label="after-tax income", color="#2ca02c", alpha=0.7,
            )
            ax.bar(
                years, -chart["non_tax_expenses"],
                label="non-tax expenses", color="#d62728", alpha=0.7,
            )
            ax.plot(
                years, chart["tax_liability"],
                label="tax liability", color="#9467bd", marker="o", linewidth=2,
            )
            ax.plot(
                years, chart["after_tax_surplus"],
                label="after-tax surplus", color="black", marker="o", linewidth=2,
            )
            ax.axhline(0, color="grey", linewidth=0.8)
            ax.set_title(f"Annual after-tax income ({mode_label}) ({rc.label})")
            ax.set_xlabel("year")
            ax.set_ylabel("USD")
            ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:,.0f}"))
            ax.grid(True, axis="y", alpha=0.3)
            ax.legend(framealpha=0.5)
            plt.setp(ax.get_xticklabels(), rotation=90)
            plt.show()

        with table_out:
            clear_output(wait=True)
            display(reconciliation.style.format({c: "${:,.0f}" for c in money_columns}))
            display(net.style.format("${:,.0f}"))

    plot_mode.observe(_redraw, names="value")
    _redraw()

    return widgets.VBox([plot_mode, chart_out, table_out])
