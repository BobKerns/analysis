"""Ensemble notebook widgets (ADR-006): pick a generated recipe, see its
members' net worth as a quintile-shaded ("fan") chart plus a year-end
mean/median/SD table with the 5th/95th percentiles and quintile
breakpoints."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import ipywidgets as widgets
from matplotlib.figure import Figure
import pandas as pd
from IPython.display import Image, clear_output, display

from analysis.sim import units
from analysis.sim.connection import RunConnection, year_end_values
from analysis.sim.ensemble import (
    EnsembleError,
    base_case_deaths,
    comparison_deltas,
    latest_ensemble_batch,
    load_ensemble,
    member_pairs,
    member_run_statuses,
    members_balance_by_month,
    recipe_reserve,
    risk_summary,
    summary_with_percentiles,
)
from analysis.sim.ensemble_plots import (
    band_edges,
    ensemble_compare_figure,
    ensemble_fan_chart,
    ensemble_fan_figure,
    figure_bytes,
)
from analysis.sim.notebook_ui.first_draw import when_cell_is_done
from analysis.sim.notebook_ui.pickers import (
    AccountDropdown,
    AfterTaxPicker,
    DollarModePicker,
    LotPicker,
    LogLinearModePicker,
    TimeRangePicker,
    find_data_dir,
    find_runs_dir,
    run_years,
)
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


def _png(fig: Figure) -> Image:
    """``fig`` as an inline PNG, cropped like matplotlib-inline's own output."""
    return Image(data=figure_bytes(fig), format="png")


NET_WORTH = "Net worth"
# The account picker's value for net worth. Not the label: an account may
# itself be named "Net worth", and must still be selectable.
_NET_WORTH_CHOICE = object()


def _account(choice: object) -> str | None:
    """The account a picker value names, or None for net worth."""
    return choice if isinstance(choice, str) else None


def recipe_accounts(
    runs_dir: Path, recipes: list[str],
) -> tuple[list[str], dict[str, dict[str, list[str]]], list[int]]:
    """Every account any of ``recipes`` has, by name, and each recipe's own
    accounts and their lots (#170), single-lot ones included so a union
    across recipes misses none (a Lot dropdown shows for two or more), and
    every calendar year any of them runs over (#245), read from one member
    of each recipe's latest batch, since a recipe's members share their
    accounts and dates."""
    accounts: set[str] = set()
    lots: dict[str, dict[str, list[str]]] = {}
    years: set[int] = set()
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
            years.update(run_years(con))
        finally:
            con.close()
    return sorted(accounts), lots, sorted(years)


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
    """Scale, dollars, account (and its lots), reserve and the years shown
    (#245): the controls the ensemble widgets draw by (#171). Build one and
    hand it to each widget as ``controls=``, and one change redraws them
    all; a widget given none builds its own.

    The account list is every account any recipe under ``runs_dir`` has,
    and the Years slider spans every year any of them runs over, both
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
                 dollar_picker: DollarModePicker | None = None,
                 time_range: TimeRangePicker | None = None,
                 after_tax_picker: AfterTaxPicker | None = None):
        resolved = find_runs_dir(data_dir=data_dir, state_dir=state_dir, runs_dir=runs_dir)
        accounts, self._recipe_lots, years = recipe_accounts(
            resolved, list_ensemble_recipes(resolved),
        )
        # Spanned here, not as each widget draws: the span would otherwise
        # move the slider mid-draw, and that redraws every widget.
        self.time_range = time_range or TimeRangePicker()
        self.time_range.cover(years)
        self.log_linear_picker = log_linear_picker or LogLinearModePicker(log_linear_mode)
        self.dollar_picker = dollar_picker or DollarModePicker(dollar_mode)
        self.after_tax_picker = after_tax_picker or AfterTaxPicker()
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

    @property
    def after_tax(self) -> bool:
        """After-tax net worth picked (#251); an account is always pre-tax."""
        return self.after_tax_picker.value and self.account is None

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
        for picker in (self.log_linear_picker, self.dollar_picker, self.after_tax_picker,
                       self.reserve_field, self.time_range):
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
            controls.log_linear_picker, controls.dollar_picker, controls.after_tax_picker,
            widgets.VBox([controls.account_picker, lot_picker]),
            controls.reserve_field, controls.time_range,
        ], layout=widgets.Layout(flex_flow="row wrap", align_items="center"))


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

    def remaining(self, steps: int) -> None:
        """Re-total the bar: ``steps`` more to go from where it stands."""
        self.bar.max = max(self.bar.value + steps, 1)

    def note(self, text: str) -> None:
        """Say what's happening now, without moving the bar."""
        self.label.value = text

    def finish(self) -> None:
        self.layout.display = "none"


class _MemberFrames:
    """The members' balances a widget has read, by batch, account, dollars
    and lot (#245): a change that needs no new data, such as the years
    shown, redraws without reading every member again. Holds the last few;
    a new run of a recipe is a new batch, so it is read afresh."""

    def __init__(self, size: int = 8) -> None:
        self._size = size
        self._frames: dict[tuple[Any, ...], pd.DataFrame] = {}

    def read(
        self, batch_dir: Path, connections: dict[str, RunConnection],
        account: str | None, mode: DollarMode, lot: str | None,
        on_member: Callable[[], None] | None = None, *, after_tax: bool = False,
    ) -> pd.DataFrame:
        """``members_balance_by_month``, read once per batch and choice."""
        key = (batch_dir, account, mode, lot, after_tax)
        if key not in self._frames:
            frame = members_balance_by_month(
                connections, account, mode, lot, on_member, after_tax=after_tax)
            if len(self._frames) >= self._size:
                del self._frames[next(iter(self._frames))]
            self._frames[key] = frame
        return self._frames[key]


AFTER_TAX_NET_WORTH = "After-tax net worth"


def _subject(account: str | None, lot: str | None, after_tax: bool = False) -> str:
    """What's shown: net worth (after tax, #251), an account, or one lot of it."""
    if account is None:
        return AFTER_TAX_NET_WORTH if after_tax else NET_WORTH
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
    if subject in (NET_WORTH, AFTER_TAX_NET_WORTH):
        return subject.lower()
    return f"account {subject}"


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
    time_range = controls.time_range
    loading = LoadingIndicator()
    out = widgets.Output()
    member_frames = _MemberFrames()

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
            after_tax = controls.after_tax
            subject = _subject(account, lot, after_tax)
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
                frame = member_frames.read(
                    batch_dir, connections, account, mode, lot,
                    loading.counter(f"Loading {recipe}", count), after_tax=after_tax,
                )
            except EnsembleError:
                # The account list spans every recipe; this one lacks it.
                clear_output(wait=True)
                display(StyledMarkdown(f"**{recipe} has no {_described(subject)}.**"))
                return
            cpi_frame = (
                member_frames.read(
                    batch_dir, connections, account, DollarMode.CPI_CONSTANT, lot,
                    loading.counter(f"{recipe} in CPI-constant dollars", count),
                    after_tax=after_tax,
                )
                if needs_cpi else None
            )
            loading.note(f"Summarizing and drawing {recipe}")
            # Summarized over the whole run, then cut to the years shown:
            # the share ever below the reserve looks back to the start.
            whole_run = summary_with_percentiles(frame)
            monthly_summary = time_range.clip(whole_run)
            heading, yearly = ensemble_summary_view(
                time_range.clip(pd.concat(
                    [whole_run, risk_summary(frame, reserve, cpi_frame)], axis=1,
                )),
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
            wants_log = log_linear_picker.value == LogLinearMode.LOG
            plotted = [band_edges(monthly_summary)[0], "mean", "median"]
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
            display(_png(ensemble_fan_figure(
                monthly_summary, f"{subject} fan chart ({mode_label}) ({recipe})",
                reserve=reserve if mode == DollarMode.CPI_CONSTANT else None,
                log_scale=wants_log, deaths=base_case_deaths(connections),
            )))
            display(yearly.style.format(_share_format(yearly.columns)))
        finally:
            for con in connections.values():
                con.close()

    dropdown.observe(_redraw, names="value")
    lot_picker.observe(_redraw, names="lots")
    controls.observe_all(_redraw)
    when_cell_is_done(_redraw)
    return widgets.VBox([dropdown, controls.row(lot_picker), loading, out])


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
    time_range = controls.time_range
    loading = LoadingIndicator()
    out = widgets.Output()
    member_frames = _MemberFrames()

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
        batches: dict[str, Path] = {}
        try:
            for name in (baseline, *variants):
                batch_dir = latest_ensemble_batch(runs_dir / name, mode="monte_carlo")
                assert batch_dir is not None  # listed recipes all have a batch
                batches[name] = batch_dir
                sides[name] = load_ensemble(batch_dir)
            account, lot = controls.account, _lot(lot_picker)
            after_tax = controls.after_tax
            subject = _subject(account, lot, after_tax)
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
                    frames[name] = member_frames.read(
                        batches[name], conns, account, mode, lot,
                        loading.counter(f"Loading {name}", len(conns)), after_tax=after_tax,
                    )
                except EnsembleError:
                    pass
            if needs_cpi:
                # A side without the account skips the CPI pass too.
                loading.remaining(sum(len(sides[name]) for name in frames))
            cpi_frames = {
                name: (
                    frame if not needs_cpi
                    else member_frames.read(
                        batches[name], sides[name], account, DollarMode.CPI_CONSTANT, lot,
                        loading.counter(f"{name} in CPI-constant dollars", len(sides[name])),
                        after_tax=after_tax,
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
                    reserve, cpi_pair, log_scale, clip=time_range.clip,
                    deaths=base_case_deaths(sides[baseline]),
                )))
                table = year_end_values(time_range.clip(comparison_deltas(
                    frames[baseline], frames[variant], pairs, reserve, cpi_pair,
                )))
                table.index.name = "year"
                outputs.append(table.style.format(_share_format(table.columns)))
            # A one-sided account's own spread, where no delta shows it:
            # each variant that has it when the baseline doesn't, or the
            # baseline when no variant has it.
            compared = baseline in frames and any(v in frames for v in variants)
            if not compared:
                for name, frame in frames.items():
                    outputs.append(_png(ensemble_fan_figure(
                        time_range.clip(summary_with_percentiles(frame)),
                        f"{subject} fan chart ({mode_label}) ({name})",
                        reserve=reserve if mode == DollarMode.CPI_CONSTANT else None,
                        log_scale=log_scale and bool((time_range.clip(frame) > 0).all().all()),
                        height=4, deaths=base_case_deaths(sides[name]),
                    )))
            tails = [risk_summary(frame, reserve, cpi_frames.get(name))
                     for name, frame in frames.items()]
            tail_table = year_end_values(
                time_range.clip(pd.concat(tails, axis=1, keys=list(frames))),
            )
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
