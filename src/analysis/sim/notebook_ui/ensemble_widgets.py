"""Ensemble notebook widgets (ADR-006): pick a generated recipe, see its
members' net worth as a quintile-shaded ("fan") chart plus a year-end
mean/median/SD table with the 5th/95th percentiles and quintile
breakpoints."""

from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path
from typing import Any

import ipywidgets as widgets
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
from matplotlib.figure import Figure
import pandas as pd
from IPython.display import Image, clear_output, display

from analysis.sim import units
from analysis.sim.connection import RunConnection, year_end_values
from analysis.sim.ensemble import (
    comparison_deltas,
    latest_ensemble_batch,
    load_ensemble,
    member_pairs,
    member_run_statuses,
    members_balance_by_month,
    paired_deltas,
    recipe_reserve,
    risk_summary,
    summary_with_percentiles,
)
from analysis.sim.notebook_ui.first_draw import when_cell_is_done
from analysis.sim.notebook_ui.pickers import (
    DollarModePicker,
    LogLinearModePicker,
    find_data_dir,
    find_runs_dir,
)
from analysis.sim.plotting import FRAME_ALPHA
from analysis.sim.readme import description_view
from analysis.sim.types import DollarMode, LogLinearMode
from analysis.sim.notebook_ui.styled import StyledMarkdown

__all__ = [
    "list_ensemble_recipes", "ensemble_summary_view", "ensemble_fan_chart",
    "ensemble_widget", "ensemble_compare_figure", "ensemble_compare_widget", "NET_WORTH",
]


def list_ensemble_recipes(runs_dir: Path) -> list[str]:
    """Names of every subdirectory of ``runs_dir`` holding at least one
    generated batch (a ``<timestamp>/member_*.sqlite3`` run file) -- the
    recipes ``sim run --ensemble-size`` has generated and run."""
    runs_dir = Path(runs_dir)
    if not runs_dir.is_dir():
        return []
    return sorted(
        p.name for p in runs_dir.iterdir()
        if p.is_dir() and latest_ensemble_batch(p, mode="monte_carlo") is not None
    )


def ensemble_summary_view(
    monthly_summary: pd.DataFrame, member_count: int, failed_count: int = 0,
) -> tuple[str, pd.DataFrame]:
    """Heading and year-end table from a month-indexed ``summary()`` or
    ``summary_with_percentiles()`` frame, columns as given. ``member_count``
    is how many *succeeded* members the frame was built from; ``failed_count``
    (excluded from it) is reported alongside, not silently dropped."""
    yearly = year_end_values(monthly_summary)
    yearly.index.name = "year"
    heading = f"**{member_count} member(s)**"
    if failed_count:
        heading += f" ({failed_count} failed, excluded)"
    return heading, yearly


def _band_edges(monthly_summary: pd.DataFrame) -> list[str]:
    """The ``pNN`` percentile columns in ascending order -- the fan chart's
    band boundaries, outermost first and last -- or ``min``/``max`` for a
    plain ``summary()`` frame that has none."""
    edges = sorted(
        (c for c in monthly_summary.columns if c[:1] == "p" and c[1:].replace(".", "").isdigit()),
        key=lambda c: float(c[1:]),
    )
    return edges or ["min", "max"]


def _band_label(low: str, high: str) -> str:
    if (low, high) == ("min", "max"):
        return "range"
    return f"{float(low[1:]):g}–{float(high[1:]):g}%"


def ensemble_fan_chart(monthly_summary: pd.DataFrame, ax: Any = None) -> Any:
    """Shade the member distribution month by month behind mean/median
    lines: one band between each pair of adjacent ``pNN`` columns
    (``ensemble.summary_with_percentiles()``'s 5th to 95th percentiles),
    darker toward the middle, so the chart shows where members cluster
    rather than how far apart one lucky and one unlucky member ended up.
    A plain ``summary()`` frame, with no ``pNN`` columns, gets the single
    min-max band."""
    if ax is None:
        _fig, ax = plt.subplots(figsize=(10, 5))
    edges = _band_edges(monthly_summary)
    bands = list(zip(edges, edges[1:], strict=False))
    depth_count = (len(bands) + 1) // 2
    base = mcolors.to_rgb("steelblue")
    labelled: set[int] = set()
    for i, (low, high) in enumerate(bands):
        depth = min(i, len(bands) - 1 - i)  # 0 for the outermost bands
        # Opaque blends with white rather than alpha: a translucent
        # steelblue (alpha=0.2) was visible in Jupyter but washed out of VS
        # Code's inline PNG rendering entirely, and opaque bands also can't
        # double up where adjacent fills overlap by a pixel. Even the
        # outermost band starts at half strength: a lighter one (~0.35) still
        # read as plain white in VS Code while looking fine in JupyterLab.
        strength = 0.5 + 0.4 * depth / max(depth_count - 1, 1)
        color = tuple(1 - strength * (1 - c) for c in base)
        # The band is labelled by the edges it shares a depth with -- the
        # outer pair is "5–95%", the next "20–80%", and so on -- once each.
        label = None
        if depth not in labelled:
            labelled.add(depth)
            label = _band_label(edges[depth], edges[len(edges) - 1 - depth])
        ax.fill_between(
            monthly_summary.index, monthly_summary[low], monthly_summary[high],
            # A full-strength edge outlines every band, so the outer
            # envelope stays visible however a renderer treats pale fills.
            color=color, edgecolor=base, linewidth=0.6, label=label,
        )
    ax.plot(monthly_summary.index, monthly_summary["mean"], color="midnightblue",
            linewidth=2, label="mean")
    ax.plot(monthly_summary.index, monthly_summary["median"], color="midnightblue",
            linewidth=1, linestyle="--", label="median")
    # No forced bottom=0: a shortfall scenario can carry a real negative net
    # worth, and clipping it would hide the low end of the range (and
    # potentially the mean/median themselves) rather than show it. A thin
    # zero rule instead -- but axhline, left to itself, feeds into autoscale
    # like any other artist and would drag the axis down to include 0 even
    # when the data is nowhere near it (the exact distortion this chart is
    # trying to avoid), so the autoscaled limits are captured first and
    # restored after: the rule only ends up visible when 0 already falls
    # within the range the data itself earned. Meaningless on a log axis
    # (zero has no position there), so skipped entirely rather than drawn
    # and silently invisible.
    if ax.get_yscale() != "log":
        ylim = ax.get_ylim()
        ax.axhline(0, color="red", linewidth=0.8, zorder=2.5)
        ax.set_ylim(ylim)
    ax.set_xlabel("month")
    ax.set_ylabel("USD")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:,.0f}"))
    ax.grid(True, axis="y", alpha=0.3)
    ax.legend(framealpha=FRAME_ALPHA)
    return ax


def _png(fig: Figure) -> Image:
    """``fig`` as an inline PNG, cropped like matplotlib-inline's own output."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    return Image(data=buf.getvalue(), format="png")


NET_WORTH = "Net worth"


def _account_options(connections: dict[str, RunConnection]) -> list[str]:
    """``NET_WORTH`` first, then every account in one member, by name."""
    con = next(iter(connections.values()))
    return [NET_WORTH, *sorted(con.account_kinds().index)]


def _account(choice: str | None) -> str | None:
    """The account a picker value names, or None for net worth."""
    return None if choice in (None, NET_WORTH) else choice


def _reserve_field() -> widgets.Text:
    return widgets.Text(
        description="Reserve:", placeholder="recipe's",
        tooltip="CPI-constant dollars, e.g. 250000, for what's shown; blank "
                "uses the recipe's scenario meta_json reserve, for net worth only",
        layout=widgets.Layout(width="16em"),
        # Redraw once the entry is done (Enter or leaving the field), not
        # on every keystroke.
        continuous_update=False,
    )


def _reserve(
    field: widgets.Text, connections: dict[str, RunConnection], account: str | None,
) -> float | None:
    """The reserve field's dollars, applied to whatever is shown; when it's
    blank, the recipe's own reserve, which is a net-worth floor and so
    applies to net worth only. An unreadable entry raises, so the output
    shows why rather than silently using the recipe's."""
    text = field.value.strip().replace(",", "").lstrip("$")
    if not text:
        return recipe_reserve(connections) if account is None else None
    return units.dollars_to_cents(text) / 100


def _sync_options(
    dropdown: widgets.Dropdown, options: list[str], redraw: Callable[..., None],
) -> None:
    """Swap ``dropdown``'s options, keeping its value where it still exists,
    without firing ``redraw``: the caller is mid-draw and already reads the
    new value, so a nested redraw would only draw everything twice."""
    if list(dropdown.options) == options:
        return
    keep = dropdown.value if dropdown.value in options else options[0]
    dropdown.unobserve(redraw, names="value")
    try:
        dropdown.options = options
        dropdown.value = keep
    finally:
        dropdown.observe(redraw, names="value")


def _share_format(columns: Any) -> dict[Any, str | Callable[..., Any] | None]:
    """Table formats: shares as percents, everything else as dollars. A
    column may be a ``(recipe, statistic)`` pair, judged by its statistic."""

    def is_share(column: Any) -> bool:
        name = column[-1] if isinstance(column, tuple) else column
        return name.startswith("P(") or name.endswith("_reserve")

    return {c: "{:.0%}" if is_share(c) else "${:,.0f}" for c in columns}


def ensemble_widget(*_,
                    data_dir: Path | str | None = None,
                    state_dir: Path | None = None,
                    runs_dir: Path | str | None = None,
                    log_linear_mode: LogLinearModePicker | None = None,
                    dollar_mode: DollarModePicker | None = None,
                    show_description: bool = False,
                    **__,
                    ) -> widgets.VBox:
    """Recipe dropdown + fan chart + year-end summary table for an ensemble
    already generated and run via ``sim run <recipe> --ensemble-size N``:
    net worth, or any one account's balance (#163)."""
    resolved_data_dir = find_data_dir(data_dir)
    runs_dir = find_runs_dir(data_dir=data_dir, state_dir=state_dir, runs_dir=runs_dir)
    recipes = list_ensemble_recipes(runs_dir)
    dropdown = widgets.Dropdown(options=recipes, description="Recipe:")
    log_linear_picker = log_linear_mode or LogLinearModePicker()
    dollar_picker = dollar_mode or DollarModePicker()
    account_picker = widgets.Dropdown(options=[NET_WORTH], description="Account:")
    reserve_field = _reserve_field()
    out = widgets.Output()

    def _redraw(*_: object) -> None:
        # Everything runs inside `out` so a load error shows as a traceback
        # there instead of vanishing into the kernel log, and the clear is
        # issued only once the new outputs are ready: clearing first and then
        # loading (seconds for a large ensemble on a cold disk cache) left VS
        # Code showing an empty output on the first switch to a big recipe.
        with out:
            if not dropdown.value:
                clear_output()
                return
            _draw(dropdown.value)

    def _draw(recipe: str) -> None:
        batch_dir = latest_ensemble_batch(runs_dir / recipe, mode="monte_carlo")
        if batch_dir is None:
            clear_output(wait=True)
            display(StyledMarkdown(f"**No Monte Carlo batch for {recipe}.**"))
            return
        statuses = member_run_statuses(batch_dir)
        failed_count = sum(1 for status in statuses.values() if status != "succeeded")
        connections = load_ensemble(batch_dir)
        try:
            _sync_options(account_picker, _account_options(connections), _redraw)
            account = _account(account_picker.value)
            subject = account or "Net worth"
            mode = dollar_picker.value
            mode_label = "CPI-constant" if mode == DollarMode.CPI_CONSTANT else "nominal"
            # Each member's own series is converted before aggregating, not
            # the aggregate after the fact: a member's CPI-constant series
            # reflects its own inflation path, which can differ member to
            # member when the varying row is itself inflation/CPI-linked.
            frame = members_balance_by_month(connections, account, mode=mode)
            reserve = _reserve(reserve_field, connections, account)
            cpi_frame = (
                members_balance_by_month(connections, account, DollarMode.CPI_CONSTANT)
                if reserve is not None and mode != DollarMode.CPI_CONSTANT else None
            )
            monthly_summary = summary_with_percentiles(frame)
            heading, yearly = ensemble_summary_view(
                pd.concat([monthly_summary, risk_summary(frame, reserve, cpi_frame)], axis=1),
                len(connections), failed_count,
            )
            if reserve is not None:
                heading += f"; reserve ${reserve:,.0f} (CPI-constant)"
            description = (
                description_view(next(iter(connections.values())), data_dir=resolved_data_dir)
                if show_description else None
            )
            clear_output(wait=True)
            display(StyledMarkdown(f"{heading} (batch {batch_dir.name})"))
            if description is not None:
                display(description)
            # A pyplot-free Figure, rendered to a PNG here: pyplot's
            # open-figure list is global to the kernel, and under JupyterLab's
            # kernel subshells another widget message can run
            # matplotlib-inline's flush_figures on a second thread while this
            # one is still drawing. That flush rendered the half-built figure
            # concurrently and the chart came out blank (#119). A figure
            # pyplot doesn't know about can't be flushed by anyone else, and
            # rendering it ourselves doesn't depend on the inline backend
            # having registered a Figure formatter, which only pyplot use does.
            fig = Figure(figsize=(10, 5))
            ax = fig.subplots()
            # Scale before drawing: matplotlib's set_yscale resets the
            # major formatter to its own log-scale default, which would
            # otherwise clobber ensemble_fan_chart's dollar formatting.
            wants_log = log_linear_picker.value == LogLinearMode.LOG
            plotted = [_band_edges(monthly_summary)[0], "mean", "median"]
            if wants_log and (monthly_summary[plotted] <= 0).any().any():
                # Log scale can't represent zero or negative values --
                # silently switching to linear would clip exactly the
                # insolvency tail this chart exists to show, rather than
                # just rendering it oddly.
                display(StyledMarkdown(
                    f"_Log scale can't show {subject.lower() if account is None else subject} "
                    "that reaches zero or below; showing linear instead._"
                ))
                wants_log = False
            if wants_log:
                ax.set_yscale("log")
            ensemble_fan_chart(monthly_summary, ax=ax)
            if reserve is not None and mode == DollarMode.CPI_CONSTANT:
                ax.axhline(reserve, color="darkorange", linewidth=1, linestyle="--",
                           label="reserve")
                ax.legend(framealpha=FRAME_ALPHA)
            ax.set_title(f"{subject} fan chart ({mode_label}) ({recipe})")
            ax.tick_params(axis="x", labelrotation=90)
            display(_png(fig))
            display(yearly.style.format(_share_format(yearly.columns)))
        finally:
            for con in connections.values():
                con.close()

    dropdown.observe(_redraw, names="value")
    log_linear_picker.observe(_redraw, names="value")
    dollar_picker.observe(_redraw, names="value")
    account_picker.observe(_redraw, names="value")
    reserve_field.observe(_redraw, names="value")
    when_cell_is_done(_redraw)
    pickers = widgets.HBox([log_linear_picker, dollar_picker, account_picker, reserve_field])
    return widgets.VBox([dropdown, pickers, out])


def _delta_lines(deltas: pd.DataFrame, columns: list[str], ax: Any, title: str) -> None:
    """One line per column of ``deltas``, light to dark, labelled at the
    right end, over a zero rule."""
    shades = plt.get_cmap("Blues")
    for i, column in enumerate(columns):
        color = shades(0.35 + 0.6 * i / max(len(columns) - 1, 1))
        ax.plot(deltas.index, deltas[column], color=color, linewidth=2, label=column)
        ax.annotate(column, (deltas.index[-1], deltas[column].iloc[-1]),
                    xytext=(4, 0), textcoords="offset points", va="center", fontsize=8)
    ax.axhline(0, color="gray", linewidth=0.8)
    ax.set_title(title)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:,.0f}"))
    ax.grid(True, axis="y", alpha=0.3)
    ax.legend(framealpha=FRAME_ALPHA, ncol=4, fontsize=8)


def ensemble_compare_figure(
    baseline: str, variant: str,
    baseline_frame: pd.DataFrame, variant_frame: pd.DataFrame,
    pairs: list[tuple[str, str]], mode_label: str,
) -> Figure:
    """The comparison's panels, top to bottom (#163): each percentile
    breakpoint's delta; the fan of paired per-future deltas when the
    recipes pair; the delta in each tail mean (CVaR)."""
    deltas = comparison_deltas(baseline_frame, variant_frame)
    panels = 3 if pairs else 2
    fig = Figure(figsize=(10, 4 * panels))
    axes = list(fig.subplots(panels, 1, sharex=True))
    _delta_lines(
        deltas, [c for c in deltas.columns if c.startswith("Δp")], axes[0],
        f"{variant} − {baseline}: each percentile vs the same percentile ({mode_label})",
    )
    if pairs:
        paired = summary_with_percentiles(paired_deltas(baseline_frame, variant_frame, pairs))
        ensemble_fan_chart(paired, ax=axes[1])
        axes[1].set_title(
            f"{variant} − {baseline}, future by future ({len(pairs)} paired members)"
        )
        axes[1].set_xlabel("")
    _delta_lines(
        deltas, [c for c in deltas.columns if c.startswith("Δcvar")], axes[-1],
        f"{variant} − {baseline}: mean of the worst 5% and 20% (CVaR)",
    )
    axes[-1].tick_params(axis="x", labelrotation=90)
    fig.tight_layout()
    return fig


def ensemble_compare_widget(*_,
                            data_dir: Path | str | None = None,
                            state_dir: Path | None = None,
                            runs_dir: Path | str | None = None,
                            dollar_mode: DollarModePicker | None = None,
                            **__,
                            ) -> widgets.VBox:
    """Compare ensembles on risk (#163): a baseline recipe and one or more
    variants, net worth or one account, each variant against the baseline
    as percentile-breakpoint deltas, paired per-future deltas (when the
    recipes share a seed), and tail-mean (CVaR) deltas, with year-end
    tables of the deltas and of each recipe's tail and reserve shares."""
    runs_dir = find_runs_dir(data_dir=data_dir, state_dir=state_dir, runs_dir=runs_dir)
    recipes = list_ensemble_recipes(runs_dir)
    baseline_picker = widgets.Dropdown(options=recipes, description="Baseline:")
    variant_picker = widgets.SelectMultiple(
        options=recipes, description="Variants:", rows=min(max(len(recipes), 2), 8),
    )
    # Default dollars are CPI-constant here: a comparison over decades, and
    # the reserve, are in today's dollars.
    dollar_picker = dollar_mode or DollarModePicker(DollarMode.CPI_CONSTANT)
    account_picker = widgets.Dropdown(options=[NET_WORTH], description="Account:")
    reserve_field = _reserve_field()
    out = widgets.Output()

    def _redraw(*_: object) -> None:
        with out:
            baseline = baseline_picker.value
            variants = [v for v in variant_picker.value if v != baseline]
            if not baseline or not variants:
                clear_output()
                display(StyledMarkdown("_Pick a baseline and at least one other recipe._"))
                return
            _draw(baseline, variants)

    def _draw(baseline: str, variants: list[str]) -> None:
        sides: dict[str, dict[str, RunConnection]] = {}
        try:
            for name in (baseline, *variants):
                batch_dir = latest_ensemble_batch(runs_dir / name, mode="monte_carlo")
                assert batch_dir is not None  # listed recipes all have a batch
                sides[name] = load_ensemble(batch_dir)
            _sync_options(account_picker, _account_options(sides[baseline]), _redraw)
            account = _account(account_picker.value)
            mode = dollar_picker.value
            mode_label = "CPI-constant" if mode == DollarMode.CPI_CONSTANT else "nominal"
            reserve = _reserve(reserve_field, sides[baseline], account)
            frames = {
                name: members_balance_by_month(conns, account, mode)
                for name, conns in sides.items()
            }
            outputs: list[Any] = []
            heading = (
                f"**{account or NET_WORTH}**, {mode_label} dollars; baseline "
                f"`{baseline}` ({len(sides[baseline])} members)"
            )
            if reserve is not None:
                heading += f"; reserve ${reserve:,.0f} (CPI-constant)"
            outputs.append(StyledMarkdown(heading))
            for variant in variants:
                pairs, unpaired = member_pairs(sides[baseline], sides[variant])
                note = (
                    f"paired over {len(pairs)} futures" if pairs
                    else f"not paired: {unpaired}"
                )
                outputs.append(StyledMarkdown(
                    f"### {variant} vs {baseline}\n\n{len(sides[variant])} members, {note}."
                ))
                outputs.append(_png(ensemble_compare_figure(
                    baseline, variant, frames[baseline], frames[variant], pairs, mode_label,
                )))
                table = year_end_values(comparison_deltas(frames[baseline], frames[variant], pairs))
                table.index.name = "year"
                outputs.append(table.style.format(_share_format(table.columns)))
            tails = []
            for name, conns in sides.items():
                cpi_frame = (
                    members_balance_by_month(conns, account, DollarMode.CPI_CONSTANT)
                    if reserve is not None and mode != DollarMode.CPI_CONSTANT else None
                )
                tails.append(risk_summary(frames[name], reserve, cpi_frame))
            tail_table = year_end_values(pd.concat(tails, axis=1, keys=list(sides)))
            tail_table.index.name = "year"
            outputs.append(StyledMarkdown("### Tail and reserve, by recipe"))
            outputs.append(tail_table.style.format(_share_format(tail_table.columns)))
            clear_output(wait=True)
            for item in outputs:
                display(item)
        finally:
            for conns in sides.values():
                for con in conns.values():
                    con.close()

    for picker in (baseline_picker, variant_picker, dollar_picker, account_picker, reserve_field):
        picker.observe(_redraw, names="value")
    when_cell_is_done(_redraw)
    pickers = widgets.HBox([dollar_picker, account_picker, reserve_field])
    return widgets.VBox([widgets.HBox([baseline_picker, variant_picker]), pickers, out])
