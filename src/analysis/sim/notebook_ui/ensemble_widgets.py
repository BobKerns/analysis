"""Ensemble notebook widgets (ADR-006): pick a generated recipe, see its
members' net worth as a quintile-shaded ("fan") chart plus a year-end
mean/median/SD table with the 5th/95th percentiles and quintile
breakpoints."""

from __future__ import annotations

import io
from collections.abc import Callable, Sequence
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
    EnsembleError,
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
    AccountDropdown,
    DollarModePicker,
    LotPicker,
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
    "EnsembleControls", "recipe_accounts", "LoadingIndicator",
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
# The account picker's value for net worth. Not the label: an account may
# itself be named "Net worth", and must still be selectable.
_NET_WORTH_CHOICE = object()


def _account(choice: object) -> str | None:
    """The account a picker value names, or None for net worth."""
    return choice if isinstance(choice, str) else None


def recipe_accounts(
    runs_dir: Path, recipes: list[str],
) -> tuple[list[str], dict[str, dict[str, list[str]]]]:
    """Every account any of ``recipes`` has, by name, and each recipe's own
    accounts and their lots (#170), single-lot ones included so a union
    across recipes misses none (a Lot dropdown shows for two or more), read from one member
    of each recipe's latest batch, since a recipe's members share their
    accounts."""
    accounts: set[str] = set()
    lots: dict[str, dict[str, list[str]]] = {}
    for recipe in recipes:
        batch_dir = latest_ensemble_batch(Path(runs_dir) / recipe, mode="monte_carlo")
        if batch_dir is None:
            continue
        member = next(
            (stem for stem, status in member_run_statuses(batch_dir).items()
             if status == "succeeded"), None,
        )
        if member is None:
            continue
        con = RunConnection(batch_dir / f"{member}.sqlite3")
        try:
            accounts.update(con.account_kinds().index)
            lots[recipe] = con.lots_by_account(single=True)
        finally:
            con.close()
    return sorted(accounts), lots


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


class EnsembleControls:
    """Scale, dollars, account (and its lots) and reserve: the controls the
    ensemble widgets draw by (#171). Build one and hand it to each widget
    as ``controls=``, and one change redraws them all; a widget given none
    builds its own.

    The account list is every account any recipe under ``runs_dir`` has,
    fixed when built, like the widgets' recipe lists: one recipe may lack
    an account another has, and the widgets say so rather than draw it.
    The Lot dropdown can't be shared across both kinds of widget, since each
    offers only the lots of the recipes it shows, so there are two: the
    single-recipe widgets share ``recipe_lot_picker`` and the comparisons
    share ``compare_lot_picker``. So each widget lays out its own row of
    the shared controls (``row``), with its Lot dropdown under Account.
    """

    def __init__(self, *,
                 data_dir: Path | str | None = None,
                 state_dir: Path | None = None,
                 runs_dir: Path | str | None = None,
                 dollar_mode: DollarMode = DollarMode.CPI_CONSTANT,
                 log_linear_mode: LogLinearMode = LogLinearMode.LINEAR,
                 log_linear_picker: LogLinearModePicker | None = None,
                 dollar_picker: DollarModePicker | None = None):
        resolved = find_runs_dir(data_dir=data_dir, state_dir=state_dir, runs_dir=runs_dir)
        accounts, self._recipe_lots = recipe_accounts(resolved, list_ensemble_recipes(resolved))
        self.log_linear_picker = log_linear_picker or LogLinearModePicker(log_linear_mode)
        self.dollar_picker = dollar_picker or DollarModePicker(dollar_mode)
        self.account_picker = AccountDropdown(
            [(NET_WORTH, _NET_WORTH_CHOICE), *((a, a) for a in accounts)],
        )
        self.reserve_field = _reserve_field()
        self.recipe_lot_picker = self._lot_picker()
        self.compare_lot_picker = self._lot_picker()

    @property
    def account(self) -> str | None:
        """The picked account, or None for net worth."""
        return _account(self.account_picker.account)

    def _lot_picker(self) -> LotPicker:
        picker = LotPicker(multiple=False)
        picker.follow(self.account)
        return picker

    def offer_lots(self, picker: LotPicker, recipes: Sequence[str]) -> bool:
        """Have ``picker`` offer the shared account's lots in ``recipes`` --
        the ones its widget shows -- and no others. True when that changed
        the picked lot, which has already redrawn the widget."""
        before = picker.lots
        lots: dict[str, list[str]] = {}
        for recipe in recipes:
            for account, names in self._recipe_lots.get(recipe, {}).items():
                seen = lots.setdefault(account, [])
                seen.extend(lot for lot in names if lot not in seen)
        picker.set_lots(lots)
        picker.follow(self.account)
        return picker.lots != before

    def row(self, lot_picker: LotPicker) -> ControlsRow:
        """One widget's row of these controls, ``lot_picker`` under Account."""
        return ControlsRow(self, lot_picker)

    def observe_all(self, redraw: Callable[..., None]) -> None:
        """Call ``redraw`` whenever any control changes."""
        for picker in (self.log_linear_picker, self.dollar_picker, self.reserve_field):
            picker.observe(redraw, names="value")
        self.account_picker.observe(redraw, names="selection")


class ControlsRow(widgets.HBox):
    """A widget's view of shared ``controls``: the same control instances,
    so a change in any row reaches every widget, with the widget's own Lot
    dropdown stacked under Account."""

    def __init__(self, controls: EnsembleControls, lot_picker: LotPicker):
        self.controls = controls
        self.lot_picker = lot_picker
        super().__init__([
            controls.log_linear_picker, controls.dollar_picker,
            widgets.VBox([controls.account_picker, lot_picker]),
            controls.reserve_field,
        ])


class LoadingIndicator(widgets.HBox):
    """A progress bar and a line saying what's being read, shown above a
    widget's output while it loads and analyzes ensemble members (#235),
    which can take a long while for a big ensemble on a cold disk cache.
    The previous chart stays below it until the new one is ready. Hidden
    when idle."""

    def __init__(self) -> None:
        self.bar = widgets.IntProgress(min=0, max=1, value=0, bar_style="info")
        self.label = widgets.Label()
        super().__init__([self.bar, self.label], layout=widgets.Layout(display="none"))

    def start(self, total: int, text: str) -> None:
        """Show the bar, empty, out of ``total`` steps."""
        self.bar.max = max(total, 1)
        self.bar.value = 0
        self.label.value = text
        self.layout.display = "flex"

    def counter(self, text: str, count: int) -> Callable[[], None]:
        """A callback for one pass over ``count`` members: each call
        advances the bar a step and says ``text: member k of count``."""
        done = 0

        def step() -> None:
            nonlocal done
            done += 1
            self.bar.value = min(self.bar.value + 1, self.bar.max)
            self.label.value = f"{text}: member {done} of {count}"
        return step

    def note(self, text: str) -> None:
        """Say what's happening now, without moving the bar."""
        self.label.value = text

    def finish(self) -> None:
        self.layout.display = "none"


def _subject(account: str | None, lot: str | None) -> str:
    """What's shown: net worth, an account, or one lot of it."""
    if account is None:
        return NET_WORTH
    return account + (f" lot {lot}" if lot is not None else "")


def _lot(picker: LotPicker) -> str | None:
    return picker.lots[0] if picker.lots else None


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


def _described(subject: str) -> str:
    """``subject`` in a sentence: "net worth", or "account <name>"."""
    return "net worth" if subject == NET_WORTH else f"account {subject}"


def _default(recipes: list[str], default: str | None) -> str | None:
    """``default`` when it's one of ``recipes``; else None, leaving a
    dropdown on its first recipe."""
    return default if default in recipes else None


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
                    controls: EnsembleControls | None = None,
                    default: str | None = None,
                    **__,
                    ) -> widgets.VBox:
    """Recipe dropdown + fan chart + year-end summary table for an ensemble
    already generated and run via ``sim run <recipe> --ensemble-size N``:
    net worth, or any one account's balance (#163), or one lot of it
    (#170). ``controls`` shares scale, dollars, account and reserve with
    other ensemble widgets (#171); ``default`` is the recipe to start on,
    when it has a batch."""
    resolved_data_dir = find_data_dir(data_dir)
    runs_dir = find_runs_dir(data_dir=data_dir, state_dir=state_dir, runs_dir=runs_dir)
    recipes = list_ensemble_recipes(runs_dir)
    dropdown = widgets.Dropdown(options=recipes, description="Recipe:")
    if _default(recipes, default) is not None:
        dropdown.value = default
    controls = controls or EnsembleControls(
        runs_dir=runs_dir, dollar_mode=DollarMode.NOMINAL,
        log_linear_picker=log_linear_mode, dollar_picker=dollar_mode,
    )
    log_linear_picker = controls.log_linear_picker
    dollar_picker = controls.dollar_picker
    reserve_field = controls.reserve_field
    lot_picker = controls.recipe_lot_picker
    loading = LoadingIndicator()
    out = widgets.Output()

    def _redraw(*_: object) -> None:
        # Everything runs inside `out` so a load error shows as a traceback
        # there instead of vanishing into the kernel log, and the clear is
        # issued only once the new outputs are ready: clearing first and then
        # loading (seconds for a large ensemble on a cold disk cache) left VS
        # Code showing an empty output on the first switch to a big recipe.
        with out:
            if controls.offer_lots(lot_picker, [dropdown.value] if dropdown.value else []):
                return
            if not dropdown.value:
                clear_output()
                return
            try:
                _draw(dropdown.value)
            finally:
                loading.finish()

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
            account, lot = controls.account, _lot(lot_picker)
            subject = _subject(account, lot)
            mode = dollar_picker.value
            mode_label = "CPI-constant" if mode == DollarMode.CPI_CONSTANT else "nominal"
            reserve = _reserve(reserve_field, connections, account)
            needs_cpi = reserve is not None and mode != DollarMode.CPI_CONSTANT
            count = len(connections)
            loading.start(count * (1 + needs_cpi), f"Loading {recipe}")
            # Each member's own series is converted before aggregating, not
            # the aggregate after the fact: a member's CPI-constant series
            # reflects its own inflation path, which can differ member to
            # member when the varying row is itself inflation/CPI-linked.
            try:
                frame = members_balance_by_month(
                    connections, account, mode, lot,
                    loading.counter(f"Loading {recipe}", count),
                )
            except EnsembleError:
                # The account list spans every recipe; this one lacks it.
                clear_output(wait=True)
                display(StyledMarkdown(f"**{recipe} has no {_described(subject)}.**"))
                return
            cpi_frame = (
                members_balance_by_month(
                    connections, account, DollarMode.CPI_CONSTANT, lot,
                    loading.counter(f"{recipe} in CPI-constant dollars", count),
                )
                if needs_cpi else None
            )
            loading.note(f"Summarizing and drawing {recipe}")
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
    lot_picker.observe(_redraw, names="lots")
    controls.observe_all(_redraw)
    when_cell_is_done(_redraw)
    return widgets.VBox([dropdown, controls.row(lot_picker), loading, out])


def _symlog(ax: Any, values: pd.DataFrame) -> None:
    """A log scale for deltas, which run both ways: symmetric log, linear
    within 1% of the largest swing of ``values`` so small deltas near zero
    don't explode. Set before drawing: matplotlib resets the formatter."""
    peak = float(values.abs().max().max()) if values.size else 0.0
    ax.set_yscale("symlog", linthresh=max(peak / 100, 1.0))


def _delta_lines(
    deltas: pd.DataFrame, columns: list[str], ax: Any, title: str, shares: bool = False,
    log_scale: bool = False,
) -> None:
    """One line per column of ``deltas``, light to dark, labelled at the
    right end, over a zero rule; dollars, or percentage points for
    ``shares``, on a symmetric log scale for ``log_scale`` (dollars only)."""
    if log_scale and not shares:
        _symlog(ax, deltas[columns])
    shades = plt.get_cmap("Blues")
    for i, column in enumerate(columns):
        color = shades(0.35 + 0.6 * i / max(len(columns) - 1, 1))
        ax.plot(deltas.index, deltas[column], color=color, linewidth=2, label=column)
        ax.annotate(column, (deltas.index[-1], deltas[column].iloc[-1]),
                    xytext=(4, 0), textcoords="offset points", va="center", fontsize=8)
    ax.axhline(0, color="gray", linewidth=0.8)
    ax.set_title(title)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(
        (lambda v, _: f"{v * 100:+.0f} pts") if shares else (lambda v, _: f"${v:,.0f}")
    ))
    ax.grid(True, axis="y", alpha=0.3)
    ax.legend(framealpha=FRAME_ALPHA, ncol=4, fontsize=8)


def ensemble_compare_figure(
    baseline: str, variant: str,
    baseline_frame: pd.DataFrame, variant_frame: pd.DataFrame,
    pairs: list[tuple[str, str]], mode_label: str,
    reserve: float | None = None,
    cpi_frames: tuple[pd.DataFrame, pd.DataFrame] | None = None,
    log_scale: bool = False,
) -> Figure:
    """The comparison's panels, top to bottom (#163): each percentile
    breakpoint's delta; the fan of paired per-future deltas when the
    recipes pair; the delta in each tail mean (CVaR); and, given a
    ``reserve``, the change in the shares below it (see
    ``comparison_deltas`` for ``cpi_frames``). ``log_scale`` puts the dollar
    panels on a symmetric log scale (#171), since deltas run both ways."""
    deltas = comparison_deltas(baseline_frame, variant_frame, reserve=reserve, cpi_frames=cpi_frames)
    panels = 2 + bool(pairs) + (reserve is not None)
    fig = Figure(figsize=(10, 4 * panels))
    axes = list(fig.subplots(panels, 1, sharex=True))
    _delta_lines(
        deltas, [c for c in deltas.columns if c.startswith("Δp")], axes[0],
        f"{variant} − {baseline}: each percentile vs the same percentile ({mode_label})",
        log_scale=log_scale,
    )
    if pairs:
        paired = summary_with_percentiles(paired_deltas(baseline_frame, variant_frame, pairs))
        if log_scale:
            _symlog(axes[1], paired)
        ensemble_fan_chart(paired, ax=axes[1])
        axes[1].set_title(
            f"{variant} − {baseline}, future by future ({len(pairs)} paired members)"
        )
        axes[1].set_xlabel("")
    _delta_lines(
        deltas, [c for c in deltas.columns if c.startswith("Δcvar")], axes[1 + bool(pairs)],
        f"{variant} − {baseline}: mean of the worst 5% and 20% (CVaR)",
        log_scale=log_scale,
    )
    if reserve is not None:
        _delta_lines(
            deltas, ["Δbelow_reserve", "Δever_below_reserve"], axes[-1],
            f"{variant} − {baseline}: share of members below the ${reserve:,.0f} reserve",
            shares=True,
        )
    axes[-1].tick_params(axis="x", labelrotation=90)
    fig.tight_layout()
    return fig


def ensemble_compare_widget(*_,
                            data_dir: Path | str | None = None,
                            state_dir: Path | None = None,
                            runs_dir: Path | str | None = None,
                            dollar_mode: DollarModePicker | None = None,
                            log_linear_mode: LogLinearModePicker | None = None,
                            controls: EnsembleControls | None = None,
                            default: str | None = None,
                            **__,
                            ) -> widgets.VBox:
    """Compare ensembles on risk (#163): a baseline recipe and one or more
    variants, net worth or one account, each variant against the baseline
    as percentile-breakpoint deltas, paired per-future deltas (when the
    recipes share a seed), and tail-mean (CVaR) deltas, with year-end
    tables of the deltas and of each recipe's tail and reserve shares; or
    one lot of the account (#170). ``controls`` shares scale, dollars,
    account and reserve with other ensemble widgets (#171); ``default`` is
    the baseline to start on, when it has a batch."""
    runs_dir = find_runs_dir(data_dir=data_dir, state_dir=state_dir, runs_dir=runs_dir)
    recipes = list_ensemble_recipes(runs_dir)
    baseline_picker = widgets.Dropdown(options=recipes, description="Baseline:")
    if _default(recipes, default) is not None:
        baseline_picker.value = default
    variant_picker = widgets.SelectMultiple(
        options=recipes, description="Variants:", rows=min(max(len(recipes), 2), 8),
    )
    # Default dollars are CPI-constant here: a comparison over decades, and
    # the reserve, are in today's dollars.
    controls = controls or EnsembleControls(
        runs_dir=runs_dir, dollar_mode=DollarMode.CPI_CONSTANT,
        log_linear_picker=log_linear_mode, dollar_picker=dollar_mode,
    )
    dollar_picker = controls.dollar_picker
    reserve_field = controls.reserve_field
    lot_picker = controls.compare_lot_picker
    loading = LoadingIndicator()
    out = widgets.Output()

    def _redraw(*_: object) -> None:
        with out:
            baseline = baseline_picker.value
            variants = [v for v in variant_picker.value if v != baseline]
            shown = [name for name in (baseline, *variants) if name]
            if controls.offer_lots(lot_picker, shown):
                return
            if not baseline or not variants:
                clear_output()
                display(StyledMarkdown("_Pick a baseline and at least one other recipe._"))
                return
            try:
                _draw(baseline, variants)
            finally:
                loading.finish()

    def _draw(baseline: str, variants: list[str]) -> None:
        sides: dict[str, dict[str, RunConnection]] = {}
        try:
            for name in (baseline, *variants):
                batch_dir = latest_ensemble_batch(runs_dir / name, mode="monte_carlo")
                assert batch_dir is not None  # listed recipes all have a batch
                sides[name] = load_ensemble(batch_dir)
            account, lot = controls.account, _lot(lot_picker)
            subject = _subject(account, lot)
            log_scale = controls.log_linear_picker.value == LogLinearMode.LOG
            mode = dollar_picker.value
            mode_label = "CPI-constant" if mode == DollarMode.CPI_CONSTANT else "nominal"
            reserve = _reserve(reserve_field, sides[baseline], account)
            needs_cpi = reserve is not None and mode != DollarMode.CPI_CONSTANT
            loading.start(
                sum(len(conns) for conns in sides.values()) * (1 + needs_cpi),
                f"Loading {', '.join(sides)}",
            )
            # The picker lists every recipe's accounts, so an account may be
            # missing on some sides: those get a note, not a delta.
            frames: dict[str, pd.DataFrame] = {}
            for name, conns in sides.items():
                try:
                    frames[name] = members_balance_by_month(
                        conns, account, mode, lot,
                        loading.counter(f"Loading {name}", len(conns)),
                    )
                except EnsembleError:
                    pass
            cpi_frames = {
                name: (
                    frame if not needs_cpi
                    else members_balance_by_month(
                        sides[name], account, DollarMode.CPI_CONSTANT, lot,
                        loading.counter(f"{name} in CPI-constant dollars", len(sides[name])),
                    )
                )
                for name, frame in frames.items()
            } if reserve is not None else {}
            loading.note("Comparing and drawing")
            outputs: list[Any] = []
            heading = (
                f"**{subject}**, {mode_label} dollars; baseline "
                f"`{baseline}` ({len(sides[baseline])} members)"
            )
            if reserve is not None:
                heading += f"; reserve ${reserve:,.0f} (CPI-constant)"
            outputs.append(StyledMarkdown(heading))
            if not frames:
                # The shared account list spans every recipe, so the pick may
                # be in none of these: nothing to compare and no tails.
                outputs.append(StyledMarkdown(
                    f"**None of these recipes has {_described(subject)}.**"
                ))
                clear_output(wait=True)
                for item in outputs:
                    display(item)
                return
            for variant in variants:
                if baseline not in frames or variant not in frames:
                    missing = baseline if baseline not in frames else variant
                    outputs.append(StyledMarkdown(
                        f"### {variant} vs {baseline}\n\n`{missing}` has no "
                        f"{_described(subject)}, so there is nothing to compare; its tail is below."
                    ))
                    continue
                pairs, unpaired = member_pairs(sides[baseline], sides[variant])
                note = (
                    f"paired over {len(pairs)} futures" if pairs
                    else f"not paired: {unpaired}"
                )
                outputs.append(StyledMarkdown(
                    f"### {variant} vs {baseline}\n\n{len(sides[variant])} members, {note}."
                ))
                cpi_pair = (cpi_frames[baseline], cpi_frames[variant]) if cpi_frames else None
                outputs.append(_png(ensemble_compare_figure(
                    baseline, variant, frames[baseline], frames[variant], pairs, mode_label,
                    reserve, cpi_pair, log_scale,
                )))
                table = year_end_values(comparison_deltas(
                    frames[baseline], frames[variant], pairs, reserve, cpi_pair,
                ))
                table.index.name = "year"
                outputs.append(table.style.format(_share_format(table.columns)))
            # A one-sided account's own spread, where no delta shows it:
            # each variant that has it when the baseline doesn't, or the
            # baseline when no variant has it.
            compared = baseline in frames and any(v in frames for v in variants)
            if not compared:
                for name, frame in frames.items():
                    fig = Figure(figsize=(10, 4))
                    ax = fig.subplots()
                    if log_scale and (frame > 0).all().all():
                        ax.set_yscale("log")
                    ensemble_fan_chart(summary_with_percentiles(frame), ax=ax)
                    ax.set_title(f"{subject} fan chart ({mode_label}) ({name})")
                    outputs.append(_png(fig))
            tails = [risk_summary(frame, reserve, cpi_frames.get(name))
                     for name, frame in frames.items()]
            tail_table = year_end_values(pd.concat(tails, axis=1, keys=list(frames)))
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

    for picker in (baseline_picker, variant_picker):
        picker.observe(_redraw, names="value")
    lot_picker.observe(_redraw, names="lots")
    controls.observe_all(_redraw)
    when_cell_is_done(_redraw)
    return widgets.VBox([
        widgets.HBox([baseline_picker, variant_picker]), controls.row(lot_picker), loading, out,
    ])
