"""Accounts notebook widget: wraps the low-level balance-history renderer.

Supplies its pivots from a :class:`~analysis.sim.connection.RunConnection`
instead of pre-built pandas structures.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import cast

from IPython.display import clear_output, display
from ipywidgets import widgets
import pandas as pd

from analysis.sim.accounts import CASHFLOW_KINDS, KIND_LABELS, account_summary_by_kind, accounts_dataframe
from analysis.sim.accounts import plottable_accounts
from analysis.sim.connection import RunConnection, ScenarioConnection
from analysis.sim.run_charts import balance_history_figure
from analysis.sim.types import AccountKind, AccountKindAny, Death, DollarMode, ValuePlotMode
from analysis.sim.notebook_ui.pickers import (
    AccountPicker, DollarModePicker, TimeRangePicker, ValuePlotModePicker,
    YAxisPlotModePicker, controls_row, observe_while_shown, run_years,
)
from analysis.sim.notebook_ui.styled import StyledMarkdown, show_figure


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
    lots_by_account: Mapping[str, Sequence[str]] | None = None,
    lot_balance: Callable[[str, str, DollarMode], pd.Series] | None = None,
    time_range: TimeRangePicker | None = None,
    deaths: Sequence[Death] = (),
) -> widgets.VBox:
    """Build a Kind + Accounts + Output widget set that plots on selection change.

    ``kind`` is only the Kind dropdown's initial value -- switching it
    repopulates the Accounts picker from that kind's own accounts, so one
    widget covers asset/liability/income/expense instead of one apiece
    (unlike kinds stay off the same axis this way). Income/expense get an
    automatic cumulative-totals note; other kinds don't need one.
    Given ``lots_by_account`` and ``lot_balance``, a single account with
    several lots can be drilled into: picked lots plot one line apiece in
    place of the account's (#170). ``time_range`` picks the years shown; the
    caller has already set its ends (``TimeRangePicker.cover``). ``deaths``
    are red lines (#255).
    Returns the combined ``ipywidgets.VBox``; it is not displayed here, because
    ``Connections.show`` displays what a widget function returns.
    """

    def _plottable(kind: AccountKindAny) -> list[str]:
        return plottable_accounts(balance_pivot_by_kind(cast(AccountKind, kind)), account_kinds, kind)

    picker = AccountPicker(
        kind=kind, accounts_by_kind=_plottable, initial_count=initial_count,
        lots_by_account=lots_by_account if lot_balance is not None else None,
    )

    def _kind() -> AccountKind:
        # No "Any" option here, so the picker always holds a single kind.
        return cast(AccountKind, picker.kind)
    value_mode = value_mode or ValuePlotModePicker()
    dollar_mode = dollar_mode or DollarModePicker()
    y_axis_mode = y_axis_mode or YAxisPlotModePicker()
    time_range = time_range or TimeRangePicker()
    caveat_out = widgets.Output()
    out = widgets.Output()

    def _redraw_caveat(*_):
        with caveat_out:
            clear_output(wait=True)
            if _kind() in _CUMULATIVE_KINDS:
                display(StyledMarkdown(_CUMULATIVE_CAVEAT))

    def _pivot(mode: DollarMode) -> tuple[pd.DataFrame, list[str]]:
        """The balances to plot and the columns selected in them: the
        picked lots' own series when there are any, else the accounts'."""
        accounts, lots = picker.selection
        if lots and lot_balance is not None:
            (account,) = accounts
            series = [lot_balance(account, lot, mode) for lot in lots]
            return pd.concat(series, axis=1), [str(s.name) for s in series]
        pivot = (
            cpi_balance_pivot_by_kind(_kind())
            if mode == DollarMode.CPI_CONSTANT and cpi_balance_pivot_by_kind is not None
            else balance_pivot_by_kind(_kind())
        )
        return pivot, list(accounts)

    def _redraw(*_):
        mode = dollar_mode.value
        zero_anchor = y_axis_mode.value
        values = value_mode.value
        pivot, selected = _pivot(mode)
        with out:
            clear_output(wait=True)
            mode_title = DollarMode(mode).label
            values_title = "monthly change" if values == ValuePlotMode.MONTHLY else "total"
            kind_title = KIND_LABELS[_kind()]
            fig = balance_history_figure(
                pivot, selected, f"{title} ({kind_title}) [{mode_title}, {values_title}]",
                zero_anchor=zero_anchor, value_mode=values, clip=time_range.clip,
                deaths=deaths,
            )
            if fig is None:
                print("(pick one or more accounts)")
                return
            show_figure(fig)

    picker.observe(_redraw_caveat, names="kind")
    picker.observe(_redraw, names="selection")
    for control in (dollar_mode, y_axis_mode, value_mode, time_range):
        observe_while_shown(control, _redraw)

    _redraw_caveat()
    _redraw()

    box = widgets.VBox([
        *( [scenario_picker] if scenario_picker is not None else () ),
        controls_row(value_mode, dollar_mode, y_axis_mode, time_range),
        picker, out, caveat_out,
    ])
    return box


def account_history_widget(
    rc: RunConnection, kind: AccountKind, title: str, initial_count: int = 1,
    scenario_picker: widgets.Dropdown | None = None,
    dollar_mode: DollarModePicker | None = None,
    value_mode: ValuePlotModePicker | None = None,
    y_axis_mode: YAxisPlotModePicker | None = None,
    time_range: TimeRangePicker | None = None, **_,
):
    """Balance-history widget covering every account kind.

    ``kind`` only sets the Kind dropdown's initial selection
    (asset/liability/income/expense); switching it repopulates the Accounts
    picker instead of needing a separate widget per kind. A single account
    with several lots can be drilled into, lot by lot (#170).
    """
    title = f"{title} - {rc.label}"
    time_range = time_range or TimeRangePicker()
    time_range.cover(run_years(rc))
    return account_balance_history_widget(
        rc.balance_pivot, rc.account_kinds(), title, kind,
        initial_count=initial_count, cpi_balance_pivot_by_kind=rc.cpi_balance_pivot,
        scenario_picker=scenario_picker,
        dollar_mode=dollar_mode,
        value_mode=value_mode,
        y_axis_mode=y_axis_mode,
        lots_by_account=rc.lots_by_account(),
        lot_balance=rc.lot_balance,
        time_range=time_range,
        deaths=rc.deaths(),
    )


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

