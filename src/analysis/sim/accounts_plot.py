"""Interactive account balance history plot widget for notebooks.

Keeps matplotlib/ipywidgets plumbing out of notebooks so they can focus on
the data itself.
"""

from __future__ import annotations

from io import BytesIO

import pandas as pd

PLOTTABLE_KINDS = {"asset", "liability"}


def plottable_accounts(balance_pivot: pd.DataFrame, account_kinds: pd.Series) -> list[str]:
    """Account names eligible for plotting, sorted by latest |balance| descending."""
    names = [
        name for name in balance_pivot.columns
        if account_kinds.get(name) in PLOTTABLE_KINDS
    ]
    names.sort(key=lambda name: abs(balance_pivot[name].iloc[-1] or 0), reverse=True)
    return names


def render_balance_history_png(
    balance_pivot: pd.DataFrame,
    selected: list[str],
    title: str,
) -> bytes | None:
    """Render selected account balance histories to PNG bytes, or None if empty."""
    if not selected:
        return None

    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 5))
    for name in selected:
        ax.plot(balance_pivot.index, balance_pivot[name], label=name)
    ax.set_title(title)
    ax.set_ylabel("USD")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda value, _: f"${value:,.0f}"))
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best", fontsize="small")
    fig.autofmt_xdate()

    buffer = BytesIO()
    fig.savefig(buffer, format="png", bbox_inches="tight")
    plt.close(fig)
    return buffer.getvalue()


def account_balance_history_widget(
    balance_pivot: pd.DataFrame,
    account_kinds: pd.Series,
    title: str,
    initial_count: int = 1,
):
    """Build a SelectMultiple + Output widget pair that plots on selection change.

    Returns the combined ``ipywidgets.VBox`` ready to ``display()``.
    """
    import ipywidgets as widgets
    from IPython.display import Image, clear_output, display

    plottable = plottable_accounts(balance_pivot, account_kinds)

    picker = widgets.SelectMultiple(
        options=plottable,
        value=tuple(plottable[:initial_count]),
        rows=min(15, max(5, len(plottable))),
        description="Accounts:",
        layout=widgets.Layout(width="50%"),
    )
    out = widgets.Output()

    def _redraw(change=None):
        del change
        with out:
            clear_output(wait=True)
            selected = list(picker.value)
            png_bytes = render_balance_history_png(balance_pivot, selected, title)
            if png_bytes is None:
                print("(pick one or more accounts)")
                return
            display(Image(data=png_bytes, format="png"))

    picker.observe(_redraw, names="value")
    _redraw()

    return widgets.VBox([picker, out])
