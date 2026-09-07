"""Interactive account balance history plot widget for notebooks.

Keeps matplotlib/ipywidgets plumbing out of notebooks so they can focus on
the data itself.
"""

from __future__ import annotations

import matplotlib.pyplot as plt
import pandas as pd
from IPython.display import clear_output
from ipywidgets import widgets

from analysis.sim.notebook_ui.pickers import dollar_plot_mode, value_plot_mode, y_axis_plot_mode
from analysis.sim.plotting import include_zero


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
):
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

    import matplotlib.pyplot as plt

    balance_pivot = trim_to_last_nonzero(balance_pivot, selected)[selected]
    ylabel = "USD"
    if value_mode == "monthly":
        balance_pivot = balance_pivot.diff()
        ylabel = "USD / month"
    fig, ax = plt.subplots(figsize=(10, 5))
    for name in selected:
        ax.plot(balance_pivot.index, balance_pivot[name], label=name)
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda value, _: f"${value:,.0f}"))
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best", fontsize="small")
    if zero_anchor:
        include_zero(ax)
    fig.autofmt_xdate()
    return fig


def account_balance_history_widget(
    balance_pivot: pd.DataFrame,
    account_kinds: pd.Series,
    title: str,
    kind: str,
    initial_count: int = 1,
    cpi_balance_pivot: pd.DataFrame | None = None,
    plot_mode: widgets.ToggleButtons | None = None,
    value_mode: widgets.ToggleButtons | None = None,
    y_axis_mode: widgets.ToggleButtons | None = None,
):
    """Build a SelectMultiple + Output widget pair that plots on selection change.

    ``kind`` restricts the picker to one account kind (asset, liability, income,
    or expense), which keeps unlike histories on separate scales.
    Returns the combined ``ipywidgets.VBox`` ready to ``display()``.
    """

    plottable = plottable_accounts(balance_pivot, account_kinds, kind)

    picker = widgets.SelectMultiple(
        options=plottable,
        value=tuple(plottable[:initial_count]),
        rows=min(15, max(5, len(plottable))),
        description="Accounts:",
        layout=widgets.Layout(width="50%"),
    )
    value_mode = value_mode or value_plot_mode()
    plot_mode = plot_mode or dollar_plot_mode()
    y_axis_mode = y_axis_mode or y_axis_plot_mode()
    out = widgets.Output()

    def _redraw(change=None):
        selected = list(change["new"] if change and change.get("owner") is picker else picker.value)
        mode = plot_mode.value
        if change and change.get("owner") is plot_mode:
            mode = change["new"]
        zero_anchor = y_axis_mode.value
        if change and change.get("owner") is y_axis_mode:
            zero_anchor = change["new"]
        values = value_mode.value
        if change and change.get("owner") is value_mode:
            values = change["new"]
        pivot = cpi_balance_pivot if mode == "cpi_constant" and cpi_balance_pivot is not None else balance_pivot
        with out:
            clear_output(wait=True)
            mode_title = "CPI-constant" if mode == "cpi_constant" else "nominal"
            values_title = "monthly change" if values == "monthly" else "total"
            fig = render_balance_history_figure(
                pivot, selected, f"{title} ({mode_title}, {values_title})",
                zero_anchor=zero_anchor, value_mode=values,
            )
            if fig is None:
                print("(pick one or more accounts)")
                return
            plt.show()

    picker.observe(_redraw, names="value")
    plot_mode.observe(_redraw, names="value")
    y_axis_mode.observe(_redraw, names="value")
    value_mode.observe(_redraw, names="value")
    _redraw()

    return widgets.VBox([widgets.HBox([value_mode, plot_mode, y_axis_mode]), picker, out])
