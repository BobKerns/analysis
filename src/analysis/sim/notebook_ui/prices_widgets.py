"""Price notebook widgets (#436): the price check, the symbol table, and one
symbol's price history, from the readers ``sim prices check``, ``sim symbol
list`` and ``sim prices show SYMBOL`` use. All three read the scenario and the
data repo's shared ``prices/`` files, so they need no run.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

import pandas as pd
from IPython.display import display
from ipywidgets import widgets
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter

from analysis.frames import named_rows
from analysis.sim import prices, registries, symbols
from analysis.sim.connection import ScenarioConnection
from analysis.sim.notebook_ui.styled import StyledMarkdown, show_figure

__all__ = [
    "price_check_view", "price_check_widget",
    "price_history_figure", "price_history_widget",
    "symbol_table_view", "symbol_table_widget",
]


def _percent(value: float) -> str:
    return "" if pd.isna(value) else f"{value * 100:+.1f}%"


def _shares(value: float) -> str:
    return "-" if pd.isna(value) else (f"{value:,.6f}".rstrip("0").rstrip(".") or "0")


_CHECK_FORMATS: dict[str, str | Callable[..., Any] | None] = {
    "quantity": _shares,
    "balance": "${:,.2f}",
    "price": "${:,.4f}",
    "value": "${:,.2f}",
    "difference": _percent,
    "latest_price": "${:,.4f}",
    "latest_value": "${:,.2f}",
}


def price_check_view(
    source: ScenarioConnection, tolerance: float = 0.01,
) -> tuple[str, pd.DataFrame]:
    """Heading and table of ``prices.check_prices``: the flagged lots first
    (``stale``, then ``no price``), then the rest, each group in file order.
    ``tolerance`` is a fraction, as ``check_prices`` takes it."""
    frame = prices.check_prices(source, tolerance)
    if frame.empty:
        return f"_no lot with a symbol and a quantity in {source.name}_", frame
    order = frame["flag"].map({"stale": 0, "no price": 1}).fillna(2)
    frame = frame.assign(flag=frame["flag"].fillna(""), _order=order)
    frame = frame.sort_values("_order", kind="stable").drop(columns="_order")
    flagged = int((frame["flag"] != "").sum())
    heading = (f"**{flagged} of {len(frame)} lot(s) flagged** in {source.name}: stated balance "
               f"against quantity × price (tolerance {tolerance * 100:g}%)")
    if not flagged:
        heading = (f"**Every lot agrees with its price** in {source.name} "
                   f"(tolerance {tolerance * 100:g}%)")
    return heading, frame.set_index(["account", "lot"])


def _highlight_flagged(row: pd.Series) -> list[str]:
    color = {"stale": "background-color: #fbe3d6", "no price": "background-color: #fdf0c2"}
    return [color.get(row["flag"], "")] * len(row)


def price_check_widget(source: ScenarioConnection, **_) -> widgets.VBox:
    """``sim prices check`` as a table, flagged lots first and shaded, with a
    tolerance (percent) that re-runs the check."""
    tolerance = widgets.BoundedFloatText(
        value=1.0, min=0.0, max=100.0, step=0.5, description="Tolerance %:",
        style={"description_width": "initial"}, layout=widgets.Layout(width="max-content"),
    )
    out = widgets.Output()

    def _redraw(*_):
        heading, frame = price_check_view(source, tolerance.value / 100)
        with out:
            display(StyledMarkdown(heading), clear=True)
            if not frame.empty:
                display(frame.style.apply(_highlight_flagged, axis=1).format(_CHECK_FORMATS, na_rep="-"))

    tolerance.observe(_redraw, names="value")
    _redraw()
    return widgets.VBox([tolerance, out])


def symbol_table_view(source: ScenarioConnection) -> pd.DataFrame:
    """``symbols.symbol_table`` for the data repo, shaped for display: name,
    latest allocation (with a count of earlier sets), stored prices (``-`` for
    CASH and CD, which need none), latest price and date, and the lots holding
    it in every scenario, one per line."""
    frame = symbols.symbol_table(source.data_dir)
    rows = []
    for r in named_rows(frame, index=False):
        allocation = r.allocation or "none"
        if r.allocation_sets > 1:
            allocation += f" (+{r.allocation_sets - 1} earlier)"
        if r.symbol in registries.SYMBOL_ASSET_CLASS:
            count = "-"
        else:
            count = str(r.prices) if r.prices else "none"
            if r.scenario_prices:
                count += f" (own file in {', '.join(r.scenario_prices)})"
        rows.append({
            "symbol": r.symbol, "name": r.name, "allocation": allocation, "prices": count,
            "latest_price": r.latest_price, "latest_date": r.latest_date,
            "held_by": "\n".join(r.held_by),
        })
    columns = ["symbol", "name", "allocation", "prices", "latest_price", "latest_date", "held_by"]
    return pd.DataFrame(rows, columns=columns).set_index("symbol")


def _red_none(value: object) -> str:
    return "color: #c0392b" if isinstance(value, str) and value.startswith("none") else ""


def symbol_table_widget(source: ScenarioConnection, **_) -> widgets.VBox:
    """``sim symbol list`` as a table. A missing allocation or price is red,
    as in the terminal."""
    frame = symbol_table_view(source)
    out = widgets.Output()
    with out:
        if frame.empty:
            display(StyledMarkdown("_no symbols in securities.csv, allocations.csv, prices/ or any lot_"))
        else:
            styled = frame.style.map(_red_none, subset=["allocation", "prices"]).set_properties(
                subset=["held_by"], **{"white-space": "pre-wrap"})
            display(styled)
    return widgets.VBox([out])


def price_history_figure(history: pd.DataFrame, symbol: str) -> Figure:
    """``symbol``'s stored prices (``prices.price_history``) as a step line:
    a price holds until the next one."""
    fig = Figure(figsize=(9, 3.5), layout="constrained")
    ax = fig.subplots()
    when = pd.to_datetime(history["effective_date"])
    ax.step(when, history["price"].astype(float), where="post", marker=".")
    ax.set_title(f"{symbol} stored prices")
    ax.set_ylabel("price")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v:,.2f}"))
    ax.grid(True, alpha=0.3)
    return fig


def price_history_widget(source: ScenarioConnection, symbol: str | None = None, **_) -> widgets.VBox:
    """One symbol's stored prices: a chart, then the table (newest first) with
    each price's change from the one before. Offers the symbols with a
    ``prices/`` file; a "From" date limits both."""
    prices_dir = source.data_dir / "prices"
    choices = sorted(prices.price_files(prices_dir))
    if not choices:
        out = widgets.Output()
        with out:
            display(StyledMarkdown("_no prices in prices/; `analysis sim prices fetch` gets them_"))
        return widgets.VBox([out])
    table = symbols.symbol_table(source.data_dir)
    names = {s: n for s, n in zip(table["symbol"], table["name"], strict=True) if n}
    picker = widgets.Dropdown(
        options=[(f"{s} - {names[s]}" if s in names else s, s) for s in choices],
        value=symbol if symbol in choices else choices[0],
        description="Symbol:", layout=widgets.Layout(width="max-content"),
    )
    since = widgets.DatePicker(description="From:", value=None)
    out = widgets.Output()

    def _redraw(*_):
        start: date | None = since.value
        history = prices.price_history(prices_dir, picker.value, start)
        with out:
            if history is None or history.empty:
                display(StyledMarkdown(f"_no prices for {picker.value}"
                                       + (f" since {start}_" if start else "_")), clear=True)
                return
            display(StyledMarkdown(f"**{picker.value}**: {len(history)} price(s), "
                                   f"{history['effective_date'].iloc[0]} to "
                                   f"{history['effective_date'].iloc[-1]}"), clear=True)
            show_figure(price_history_figure(history, picker.value))
            table = history.iloc[::-1].set_index("effective_date")
            display(table.style.format({"price": prices.format_price, "change": _percent}, na_rep=""))

    picker.observe(_redraw, names="value")
    since.observe(_redraw, names="value")
    _redraw()
    return widgets.VBox([widgets.HBox([picker, since]), out])

