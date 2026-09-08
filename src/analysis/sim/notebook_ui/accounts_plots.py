"""Accounts notebook widget: wraps the low-level balance-history renderer.

Supplies its pivots from a :class:`~analysis.sim.connection.RunConnection`
instead of pre-built pandas structures.
"""

from __future__ import annotations
from collections.abc import Callable

from IPython.display import Markdown, clear_output, display
from ipywidgets import widgets
import matplotlib.pyplot as plt
import pandas as pd

from analysis.sim.accounts import account_summary_by_kind, accounts_dataframe
from analysis.sim.connection import RunConnection, ScenarioConnection
from analysis.sim.notebook_ui.pickers import dollar_plot_mode, value_plot_mode, y_axis_plot_mode
from analysis.sim.plotting import include_zero


# Account kinds a balance-history widget can switch between, in dropdown order.
KIND_LABELS: dict[str, str] = {
    "asset": "Asset", "liability": "Liability",
    "income": "Income", "expense": "Expense",
}

# Income/expense balances are cumulative totals, unlike asset/liability's
# point-in-time balances -- worth a standing note whenever one is selected.
_CUMULATIVE_KINDS = {"income", "expense"}
_CUMULATIVE_CAVEAT = "_Note that expense and income flows are cumulative totals._  \n"


def account_balance_history_widget(
    balance_pivot_by_kind: Callable[[str], pd.DataFrame],
    account_kinds: pd.Series,
    title: str,
    kind: str,
    initial_count: int = 1,
    cpi_balance_pivot_by_kind: Callable[[str], pd.DataFrame] | None = None,
    plot_mode: widgets.ToggleButtons | None = None,
    value_mode: widgets.ToggleButtons | None = None,
    y_axis_mode: widgets.ToggleButtons | None = None,
) -> widgets.VBox:
    """Build a Kind + Accounts + Output widget set that plots on selection change.

    ``kind`` is only the Kind dropdown's initial value -- switching it
    repopulates the Accounts picker from that kind's own accounts, so one
    widget covers asset/liability/income/expense instead of one apiece
    (unlike kinds stay off the same axis this way). Income/expense get an
    automatic cumulative-totals note; other kinds don't need one.
    Returns the combined ``ipywidgets.VBox`` ready to ``display()``.
    """

    kind_picker = widgets.Dropdown(
        options=[(label, k) for k, label in KIND_LABELS.items()],
        value=kind,
        description="Kind:",
    )
    picker = widgets.SelectMultiple(
        description="Accounts:", layout=widgets.Layout(width="50%"),
    )
    value_mode = value_mode or value_plot_mode()
    plot_mode = plot_mode or dollar_plot_mode()
    y_axis_mode = y_axis_mode or y_axis_plot_mode()
    caveat_out = widgets.Output()
    out = widgets.Output()

    def _sync_accounts_options(*_):
        plottable = plottable_accounts(
            balance_pivot_by_kind(kind_picker.value), account_kinds, kind_picker.value,
        )
        picker.rows = min(15, max(5, len(plottable)))
        picker.options = plottable
        picker.value = tuple(plottable[:initial_count])

    def _redraw_caveat(*_):
        with caveat_out:
            clear_output(wait=True)
            if kind_picker.value in _CUMULATIVE_KINDS:
                display(Markdown(_CUMULATIVE_CAVEAT))

    def _redraw(*_):
        selected = list(picker.value)
        mode = plot_mode.value
        zero_anchor = y_axis_mode.value
        values = value_mode.value
        pivot = (
            cpi_balance_pivot_by_kind(kind_picker.value)
            if mode == "cpi_constant" and cpi_balance_pivot_by_kind is not None
            else balance_pivot_by_kind(kind_picker.value)
        )
        with out:
            clear_output(wait=True)
            mode_title = "CPI-constant" if mode == "cpi_constant" else "nominal"
            values_title = "monthly change" if values == "monthly" else "total"
            kind_title = KIND_LABELS[kind_picker.value]
            fig = render_balance_history_figure(
                pivot, selected, f"{title} ({kind_title}) [{mode_title}, {values_title}]",
                zero_anchor=zero_anchor, value_mode=values,
            )
            if fig is None:
                print("(pick one or more accounts)")
                return
            plt.show()

    def _on_kind_change(*_):
        _redraw_caveat()
        _sync_accounts_options()

    kind_picker.observe(_on_kind_change, names="value")
    picker.observe(_redraw, names="value")
    plot_mode.observe(_redraw, names="value")
    y_axis_mode.observe(_redraw, names="value")
    value_mode.observe(_redraw, names="value")

    _redraw_caveat()
    _sync_accounts_options()
    _redraw()

    return widgets.VBox([
        widgets.HBox([value_mode, plot_mode, y_axis_mode]),
        kind_picker, picker, out, caveat_out,
    ])



def account_history_widget(
    rc: RunConnection, kind: str, title: str, initial_count: int = 1,
    plot_mode: widgets.ToggleButtons | None = None,
    value_mode: widgets.ToggleButtons | None = None,
    y_axis_mode: widgets.ToggleButtons | None = None,
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
        plot_mode=plot_mode,
        value_mode=value_mode,
        y_axis_mode=y_axis_mode,
    )


def plottable_accounts(
    balance_pivot: pd.DataFrame, account_kinds: pd.Series, kind: str
) -> list[str]:
    """Account names of one exact kind, sorted alphabetically."""
    names = [
        name for name in balance_pivot.columns
        if account_kinds.get(name) == kind
    ]
    names.sort(key=str.casefold)
    return names


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
    *, zero_anchor: bool = True, value_mode: str = "total",
) -> None | plt.Figure:
    """Build the matplotlib Figure for selected account balance histories, or
    None if empty.

    ``zero_anchor`` applies the include-zero policy by default; turn it off
    for a series whose true variation is tiny relative to its magnitude (e.g.
    a home's value in CPI-constant terms) -- zero-anchoring would otherwise
    flatten it to an invisible line hugging the top of the axis.

    ``value_mode``: ``"total"`` (default) plots the raw balance; ``"monthly"``
    plots the change from the previous month -- the first month has no prior
    value to diff against, so the visible line effectively starts one month
    later.
    """
    if not selected:
        return None

    balance_pivot = trim_to_last_nonzero(balance_pivot, selected)[selected].copy()
    if len(selected) > 1:
        balance_pivot["Total"] = balance_pivot.sum(axis=1)
    ylabel = "USD"
    if value_mode == "monthly":
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

def or_dash(fmt: str):
    return lambda x: fmt.format(x) if not pd.isna(x) else "-"

def show_account_setup(scenario: ScenarioConnection, as_of: str | None = AS_OF):
    df = accounts_dataframe(scenario, as_of=as_of)
    output = widgets.Output()
    with output:
        display(account_summary_by_kind(df).style.format({
            "opening_balance": or_dash("${:,.2f}"),
            
        }))
        with pd.option_context("display.max_rows", None, "display.max_columns", None):
            display(df.style.format({
                "opening_balance": or_dash("${:,.2f}"),
                "annual_rate_pct": or_dash("{:.2f}%"),
                "rate_series": or_dash("{!s}"),
                "rate_role": or_dash("{!s}")
            }))
    return output

