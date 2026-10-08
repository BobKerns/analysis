"""Accounts notebook widget: wraps the low-level balance-history renderer.

Supplies its pivots from a :class:`~analysis.sim.connection.RunConnection`
instead of pre-built pandas structures.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from IPython.display import clear_output, display
from ipywidgets import widgets
from analysis.sim.notebook_ui.pickers import AccountKindPicker
import matplotlib.pyplot as plt
import pandas as pd

from analysis.sim.accounts import CASHFLOW_KINDS, KIND_LABELS, account_summary_by_kind, accounts_dataframe
from analysis.sim.accounts import plottable_accounts
from analysis.sim.connection import RunConnection, ScenarioConnection
from analysis.sim.plotting import include_zero
from analysis.sim.types import AccountKind, DollarMode, ValuePlotMode
from analysis.sim.notebook_ui.pickers import (
    DollarModePicker, ValuePlotModePicker, YAxisPlotModePicker,
)
from analysis.sim.notebook_ui.styled import StyledMarkdown


# Income/expense balances are cumulative totals, unlike asset/liability's
# point-in-time balances -- worth a standing note whenever one is selected.
_CUMULATIVE_KINDS = CASHFLOW_KINDS
_CUMULATIVE_CAVEAT = "_Note that expense and income flows are cumulative totals._  \n"


def account_balance_history_widget(
    balance_pivot_by_kind: Callable[[AccountKind], pd.DataFrame],
    account_kinds: pd.Series,
    title: str,
    kind: AccountKind,
    initial_count: int = 1,
    cpi_balance_pivot_by_kind: Callable[[AccountKind], pd.DataFrame] | None = None,
    scenario_picker: widgets.Dropdown | None = None,
    dollar_mode: DollarModePicker | None = None,
    value_mode: ValuePlotModePicker | None = None,
    y_axis_mode: YAxisPlotModePicker | None = None,
) -> widgets.VBox:
    """Build a Kind + Accounts + Output widget set that plots on selection change.

    ``kind`` is only the Kind dropdown's initial value -- switching it
    repopulates the Accounts picker from that kind's own accounts, so one
    widget covers asset/liability/income/expense instead of one apiece
    (unlike kinds stay off the same axis this way). Income/expense get an
    automatic cumulative-totals note; other kinds don't need one.
    Returns the combined ``ipywidgets.VBox``; it is not displayed here, because
    ``Connections.show`` displays what a widget function returns.
    """

    kind_picker = AccountKindPicker(kind)

    def _kind() -> AccountKind:
        # No "Any" option here, so the picker always holds a single kind.
        return cast(AccountKind, kind_picker.value)
    picker = widgets.SelectMultiple(
        description="Accounts:", layout=widgets.Layout(width="50%"),
    )
    value_mode = value_mode or ValuePlotModePicker()
    dollar_mode = dollar_mode or DollarModePicker()
    y_axis_mode = y_axis_mode or YAxisPlotModePicker()
    caveat_out = widgets.Output()
    out = widgets.Output()

    def _sync_accounts_options(*_):
        plottable = plottable_accounts(
            balance_pivot_by_kind(_kind()), account_kinds, _kind(),
        )
        picker.rows = min(15, max(5, len(plottable)))
        picker.options = plottable
        picker.value = tuple(plottable[:initial_count])

    def _redraw_caveat(*_):
        with caveat_out:
            clear_output(wait=True)
            if _kind() in _CUMULATIVE_KINDS:
                display(StyledMarkdown(_CUMULATIVE_CAVEAT))

    def _redraw(*_):
        selected = list(picker.value)
        mode = dollar_mode.value
        zero_anchor = y_axis_mode.value
        values = value_mode.value
        pivot = (
            cpi_balance_pivot_by_kind(_kind())
            if mode == DollarMode.CPI_CONSTANT and cpi_balance_pivot_by_kind is not None
            else balance_pivot_by_kind(_kind())
        )
        with out:
            clear_output(wait=True)
            mode_title = "CPI-constant" if mode == DollarMode.CPI_CONSTANT else "nominal"
            values_title = "monthly change" if values == ValuePlotMode.MONTHLY else "total"
            kind_title = KIND_LABELS[_kind()]
            fig = render_balance_history_figure(
                pivot, selected, f"{title} ({kind_title}) [{mode_title}, {values_title}]",
                zero_anchor=zero_anchor, value_mode=values,
            )
            if fig is None:
                print("(pick one or more accounts)")
                return
            # Show the figure itself: plt.show() did not draw it on the first pass
            # under Jupyter. Closing it keeps the inline backend from showing it again.
            display(fig)
            plt.close(fig)

    def _on_kind_change(*_):
        _redraw_caveat()
        _sync_accounts_options()

    # Fill the picker before observing it, so setting its options does not draw.
    _sync_accounts_options()
    kind_picker.observe(_on_kind_change, names="value")
    picker.observe(_redraw, names="value")
    dollar_mode.observe(_redraw, names="value")
    y_axis_mode.observe(_redraw, names="value")
    value_mode.observe(_redraw, names="value")

    _redraw_caveat()
    _redraw()

    box = widgets.VBox([
        *( [scenario_picker] if scenario_picker is not None else () ),
        widgets.HBox([value_mode, dollar_mode, y_axis_mode]),
        kind_picker, picker, out, caveat_out,
    ])
    return box


def account_history_widget(
    rc: RunConnection, kind: AccountKind, title: str, initial_count: int = 1,
    scenario_picker: widgets.Dropdown | None = None,
    dollar_mode: DollarModePicker | None = None,
    value_mode: ValuePlotModePicker | None = None,
    y_axis_mode: YAxisPlotModePicker | None = None, **_,
):
    """Balance-history widget covering every account kind.

    ``kind`` only sets the Kind dropdown's initial selection
    (asset/liability/income/expense); switching it repopulates the Accounts
    picker instead of needing a separate widget per kind.
    """
    title = f"{title} - {rc.label}"
    return account_balance_history_widget(
        rc.balance_pivot, rc.account_kinds(), title, kind,
        initial_count=initial_count, cpi_balance_pivot_by_kind=rc.cpi_balance_pivot,
        scenario_picker=scenario_picker,
        dollar_mode=dollar_mode,
        value_mode=value_mode,
        y_axis_mode=y_axis_mode,
    )


def trim_to_last_nonzero(
    balance_pivot: pd.DataFrame, selected: list[str]
) -> pd.DataFrame:
    """Trim trailing months where every selected account is zero (or missing).

    Keeps one month past the last non-zero balance -- the month it actually
    settles to its final zero -- instead of stretching the x-axis out to the
    scenario's full end for accounts that closed out early.
    """
    nonzero = balance_pivot[selected].fillna(0).ne(0).any(axis=1)
    if not nonzero.any():
        return balance_pivot
    last_nonzero_pos = nonzero.to_numpy().nonzero()[0].max()
    end_pos = min(last_nonzero_pos + 1, len(balance_pivot.index) - 1)
    return balance_pivot.iloc[: end_pos + 1]


def render_balance_history_figure(
    balance_pivot: pd.DataFrame,
    selected: list[str],
    title: str,
    *,
    zero_anchor: bool = True,
    value_mode: ValuePlotMode = ValuePlotMode.TOTAL,
) -> None | plt.Figure:
    """Build the matplotlib Figure for selected account balance histories, or
    None if empty.

    ``zero_anchor`` applies the include-zero policy by default; turn it off
    for a series whose true variation is tiny relative to its magnitude (e.g.
    a home's value in CPI-constant terms) -- zero-anchoring would otherwise
    flatten it to an invisible line hugging the top of the axis.

    ``value_mode``: ``ValuePlotMode.TOTAL`` (default) plots the raw balance;
    ``ValuePlotMode.MONTHLY`` plots the change from the previous month -- the
    first month has no prior value to diff against, so the visible line
    effectively starts one month later.
    """
    if not selected:
        return None

    balance_pivot = trim_to_last_nonzero(balance_pivot, selected)[selected].copy()
    if len(selected) > 1:
        balance_pivot["Total"] = balance_pivot.sum(axis=1)
    ylabel = "USD"
    if value_mode == ValuePlotMode.MONTHLY:
        balance_pivot = balance_pivot.diff()
        ylabel = "USD / month"
    fig, ax = plt.subplots(figsize=(10, 5))
    for name in balance_pivot.columns:
        ax.plot(balance_pivot.index, balance_pivot[name], label=name)
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda value, _: f"${value:,.0f}"))
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best", fontsize="small", framealpha=0.5)
    if zero_anchor:
        include_zero(ax)
    fig.autofmt_xdate()
    return fig

def to_month(date: str) -> str:
    if not date:
        return "-"
    ts = pd.to_datetime(date)
    if not ts or ts is pd.NaT:
        return "-"
    return ts.strftime("%Y-%m")

def show_account_setup(scenario: ScenarioConnection, as_of: str | None = None, **_):
    """
    Show the account setup for the given scenario as of the specified date.

    Parameters
    ----------
    scenario : ScenarioConnection
        The scenario connection object.
    as_of : str | None, optional
        The date as of which to show the account setup, by default None.

    Returns
    -------
    widgets.Output
        The output widget containing the account setup display.
    """
    df = accounts_dataframe(scenario, as_of=as_of).copy()
    output = widgets.Output()
    with output:
        display(account_summary_by_kind(df).style.format({
                "opening_balance": "${:,.2f}",
                "annual_rate_pct": "{:.2f}%",
            },
            na_rep="-"))
        with pd.option_context("display.max_rows", None, "display.max_columns", None):
            display(df.style.format({
                    "opening_balance": "${:,.2f}",
                    "cost_basis": "${:,.2f}",
                    "annual_rate": "{:.2f}%",
                    "rate_series": "{!s}",
                    "rate_role": "{!s}",
                    "opened": to_month,
                    "closed": to_month,
                },
                na_rep="-",
                ))
    return output

