"""Ensemble notebook widgets (ADR-006): pick a generated recipe, see its
members' net worth as a quintile-shaded ("fan") chart plus a year-end
mean/median/SD table with the 5th/95th percentiles and quintile
breakpoints."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

import ipywidgets as widgets
from matplotlib.figure import Figure
import pandas as pd
from IPython.display import Image, clear_output, display

from analysis.frames import by_year_and_label
from analysis.sim import registries, units
from analysis.sim.connection import RunConnection, year_end_values
from analysis.sim.ensemble import (
    EnsembleError,
    comparison_deltas,
    latest_ensemble_batch,
    member_run_statuses,
    risk_summary,
    summary_with_percentiles,
)
from analysis.sim.loader import is_ensemble_batch, read_disabled_scenarios
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
    note_picks,
    run_years,
)
from analysis.sim.readme import description_view
from analysis.sim.series_db import (
    EnsembleSeries,
    build_series_db,
    pair_members,
    series_db_current,
    series_db_path,
)
from analysis.sim.types import DollarMode, LogLinearMode
from analysis.sim.notebook_ui.styled import StyledMarkdown

__all__ = [
    "list_ensemble_recipes", "ensemble_summary_view", "ensemble_fan_chart",
    "ensemble_widget", "ensemble_compare_figure", "ensemble_compare_widget",
    "ensemble_table_widget", "ensemble_guard_rails_widget", "ensemble_guard_rails_table_widget",
    "ensemble_drift_widget", "NET_WORTH", "EnsembleControls", "recipe_accounts",
    "RecipeSeeds", "LoadingIndicator",
]


def list_ensemble_recipes(
    runs_dir: Path, data_dir: Path | None = None, keep: Iterable[str | None] = (),
) -> list[str]:
    """Names of every subdirectory of ``runs_dir`` holding at least one
    generated batch (a ``<timestamp>/member_*.sqlite3`` run file) -- the
    recipes ``sim run --ensemble-size`` has generated and run. Given
    ``data_dir``, less its disabled scenarios (#300) other than ``keep``."""
    runs_dir = Path(runs_dir)
    if not runs_dir.is_dir():
        return []
    disabled = read_disabled_scenarios(data_dir) - set(keep) if data_dir else frozenset()
    # Listing never waits for a background build (#300); opening a batch does.
    return sorted(
        p.name for p in runs_dir.iterdir()
        if p.is_dir() and p.name not in disabled
        and latest_ensemble_batch(p, mode="monte_carlo", wait=False) is not None
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


def settled_batch(runs_dir: Path, recipe: str) -> tuple[Path, list[int]] | None:
    """``recipe``'s newest Monte Carlo batch with a succeeded member, and
    the indexes of its succeeded members, without waiting for a background
    build (#300). Building the controls reads this; only drawing a recipe
    waits. A batch still being built may have no finished member yet, so
    an older batch stands in for it."""
    from analysis.sim.locks import run_name_key

    recipe_dir = Path(runs_dir) / recipe
    if not recipe_dir.is_dir():
        return None
    batches = sorted((p for p in recipe_dir.iterdir() if is_ensemble_batch(p, "monte_carlo")),
                     key=lambda p: run_name_key(p.name), reverse=True)
    for batch_dir in batches:
        indexes = sorted(int(stem.removeprefix("member_"))
                         for stem, status in member_run_statuses(batch_dir).items()
                         if status == "succeeded")
        if indexes:
            return batch_dir, indexes
    return None


def recipe_accounts(
    runs_dir: Path, recipes: list[str],
) -> tuple[list[str], dict[str, dict[str, list[str]]], list[int]]:
    """Every account any of ``recipes`` has, by name, and each recipe's own
    accounts and their lots (#170), single-lot ones included so a union
    across recipes misses none (a Lot dropdown shows for two or more), and
    every calendar year any of them runs over (#245), read from one member
    of each recipe's latest batch with one (``settled_batch``), since a
    recipe's members share their accounts and dates."""
    accounts: set[str] = set()
    lots: dict[str, dict[str, list[str]]] = {}
    years: set[int] = set()
    for recipe in recipes:
        settled = settled_batch(runs_dir, recipe)
        if settled is None:
            continue
        batch_dir, indexes = settled
        con = RunConnection(batch_dir / f"member_{indexes[0]:04d}.sqlite3")
        try:
            accounts.update(con.account_kinds().index)
            lots[recipe] = con.lots_by_account(single=True)
            years.update(run_years(con))
        finally:
            con.close()
    return sorted(accounts), lots, sorted(years)


class RecipeSeeds:
    """Which recipes' members pair (ADR-006 §3): two recipes pair when they
    share a seed, but a member file records only its own seed, derived
    from the recipe's and its index. So two recipes are compared at a
    member index both have succeeded, each member's seed read once."""

    def __init__(self, runs_dir: Path, recipes: Sequence[str]) -> None:
        self._batches: dict[str, Path] = {}
        self._indexes: dict[str, set[int]] = {}
        self._seeds: dict[tuple[str, int], int | None] = {}
        for recipe in recipes:
            # A rebuild keeps the recipe's seed, so pairing read from the
            # batch on hand holds for the one being built (#300).
            settled = settled_batch(runs_dir, recipe)
            if settled is None:
                continue
            self._batches[recipe], indexes = settled
            self._indexes[recipe] = set(indexes)

    def _seed(self, recipe: str, index: int) -> int | None:
        key = (recipe, index)
        if key not in self._seeds:
            path = self._batches[recipe] / f"member_{index:04d}.sqlite3"
            with RunConnection(path) as con:
                member = con.ensemble_member()
            self._seeds[key] = None if member is None else member[1]
        return self._seeds[key]

    def pair(self, baseline: str, other: str) -> bool:
        common = self._indexes.get(baseline, set()) & self._indexes.get(other, set())
        if not common:
            return False
        index = min(common)
        seed = self._seed(baseline, index)
        return seed is not None and seed == self._seed(other, index)

    def paired_with(self, baseline: str | None) -> list[str]:
        """The recipes other than ``baseline`` whose members pair with its own."""
        if not baseline:
            return []
        return [name for name in self._indexes
                if name != baseline and self.pair(baseline, name)]


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
    The Lot dropdown can't be shared across the kinds of widget, since each
    offers only the lots of the recipes it shows, so there are three: the
    single-recipe widgets share ``recipe_lot_picker``, the head-to-head
    comparisons ``compare_lot_picker`` and the tables ``table_lot_picker``.
    So each widget lays out its own row of the shared controls (``row``),
    with its Lot dropdown under Account. The recipes each widget shows are
    its own to pick, never shared (#298).
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
            resolved, list_ensemble_recipes(resolved, find_data_dir(data_dir)),
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
        self.table_lot_picker = self._lot_picker()

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


class _Stores:
    """The ensemble series stores (#298) the kernel's widgets have opened,
    by batch: every widget reads from the same one, and a store holds the
    frames it has read, so no widget reads a recipe another already has. A
    new run of a recipe is a new batch, read afresh; a store rebuilt since
    (say by ``sim ensemble``) is reopened. Holds the last few."""

    def __init__(self, size: int = 8) -> None:
        self._size = size
        self._stores: dict[Path, tuple[int, EnsembleSeries]] = {}

    def open(self, batch_dir: Path, recipe: str, succeeded: int,
             loading: LoadingIndicator) -> EnsembleSeries:
        """``batch_dir``'s store, built first, with ``loading`` counting its
        ``succeeded`` members, when it's missing or out of date."""
        key = Path(batch_dir).resolve()
        if not series_db_current(batch_dir):
            loading.start(succeeded, f"Loading {recipe}")
            build_series_db(batch_dir, loading.counter(f"Loading {recipe}", succeeded))
            self._stores.pop(key, None)
        path = series_db_path(batch_dir)
        stamp = path.stat().st_mtime_ns
        held = self._stores.pop(key, None)
        store = held[1] if held is not None and held[0] == stamp else EnsembleSeries(path)
        if len(self._stores) >= self._size:
            del self._stores[next(iter(self._stores))]
        self._stores[key] = (stamp, store)
        return store


_STORES = _Stores()


def _open_recipe(
    runs_dir: Path, recipe: str, loading: LoadingIndicator,
) -> tuple[Path, EnsembleSeries, int] | None:
    """``recipe``'s latest batch, its store and how many members failed, or
    None when it has no Monte Carlo batch."""
    batch_dir = latest_ensemble_batch(runs_dir / recipe, mode="monte_carlo")
    if batch_dir is None:
        return None
    statuses = member_run_statuses(batch_dir)
    succeeded = sum(1 for status in statuses.values() if status == "succeeded")
    store = _STORES.open(batch_dir, recipe, succeeded, loading)
    return batch_dir, store, len(statuses) - succeeded


AFTER_TAX_NET_WORTH = "After-tax net worth"


def _subject(account: str | None, lot: str | None, after_tax: bool = False) -> str:
    """What's shown: net worth (after tax, #251), an account, or one lot of it."""
    if account is None:
        return AFTER_TAX_NET_WORTH if after_tax else NET_WORTH
    return account + (f" lot {lot}" if lot is not None else "")


def _lot(picker: LotPicker) -> str | None:
    return picker.lots[0] if picker.lots else None


def _reserve(
    field: widgets.Text, store: EnsembleSeries, account: str | None,
) -> float | None:
    """The reserve field's dollars, applied to whatever is shown; when it's
    blank, the recipe's own reserve, which is a net-worth floor and so
    applies to net worth only. An unreadable entry raises, so the output
    shows why rather than silently using the recipe's."""
    text = field.value.strip().replace(",", "").lstrip("$")
    if not text:
        return store.reserve() if account is None else None
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
    recipes = list_ensemble_recipes(runs_dir, resolved_data_dir, keep=[default])
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
        opened = _open_recipe(runs_dir, recipe, loading)
        if opened is None:
            clear_output(wait=True)
            display(StyledMarkdown(f"**No Monte Carlo batch for {recipe}.**"))
            return
        batch_dir, store, failed_count = opened
        account, lot = controls.account, _lot(lot_picker)
        after_tax = controls.after_tax
        subject = _subject(account, lot, after_tax)
        mode = dollar_picker.value
        mode_label = DollarMode(mode).label
        reserve = _reserve(reserve_field, store, account)
        # Each member's own series is converted before aggregating, not
        # the aggregate after the fact: a member's CPI-constant series
        # reflects its own inflation path, which can differ member to
        # member when the varying row is itself inflation/CPI-linked.
        try:
            frame = store.frame(account, mode, lot, after_tax=after_tax)
        except EnsembleError:
            # The account list spans every recipe; this one lacks it.
            clear_output(wait=True)
            display(StyledMarkdown(f"**{recipe} has no {_described(subject)}.**"))
            return
        cpi_frame = (
            store.frame(account, DollarMode.CPI_CONSTANT, lot, after_tax=after_tax)
            if reserve is not None and mode != DollarMode.CPI_CONSTANT else None
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
            len(store), failed_count,
        )
        if reserve is not None:
            heading += f"; reserve ${reserve:,.0f} (CPI-constant)"
        description = None
        if show_description:
            with RunConnection(batch_dir / f"{store.stems[0]}.sqlite3") as con:
                description = description_view(con, data_dir=resolved_data_dir)
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
            log_scale=wants_log, deaths=store.base_case_deaths(),
        )))
        display(yearly.style.format(_share_format(yearly.columns)))

    dropdown.observe(_redraw, names="value")
    note_picks(resolved_data_dir, dropdown)
    lot_picker.observe(_redraw, names="lots")
    controls.observe_all(_redraw)
    when_cell_is_done(_redraw)
    return widgets.VBox([dropdown, controls.row(lot_picker), loading, out])


def ensemble_guard_rails_widget(*_,
                                data_dir: Path | str | None = None,
                                state_dir: Path | None = None,
                                runs_dir: Path | str | None = None,
                                controls: EnsembleControls | None = None,
                                default: str | None = None,
                                **__,
                                ) -> widgets.VBox:
    """An ensemble's members against its recipe's guard rails (#305): per
    rail, annual and cumulative, the share of members past it each year,
    the share ever past it so far, and their RMS overshoot, charted and
    tabled, then a whole-run summary. ``controls`` shares the dollars and
    the years shown with the other ensemble widgets (the account doesn't
    apply: a rail names its own measure).

    The rails are read from the recipe's guard_rails.csv at each draw, so
    an edited rail shows without a rerun. The shares look back over the
    whole run; the years picked only limit what is shown."""
    from analysis.sim import guard_rails
    from analysis.sim.guard_rail_charts import ENSEMBLE_CAVEAT, ensemble_guard_rail_figure

    resolved_data_dir = find_data_dir(data_dir)
    runs_dir = find_runs_dir(data_dir=data_dir, state_dir=state_dir, runs_dir=runs_dir)
    recipes = list_ensemble_recipes(runs_dir, resolved_data_dir, keep=[default])
    dropdown = widgets.Dropdown(options=recipes, description="Recipe:")
    if _default(recipes, default) is not None:
        dropdown.value = default
    controls = controls or EnsembleControls(runs_dir=runs_dir)
    dollar_picker, time_range = controls.dollar_picker, controls.time_range
    loading = LoadingIndicator()
    out = widgets.Output()

    def _redraw(*_: object) -> None:
        with out:
            if not dropdown.value:
                clear_output()
                return
            try:
                _draw(dropdown.value)
            finally:
                loading.finish()

    def _draw(recipe: str) -> None:
        scenarios_dir = resolved_data_dir / "scenarios"
        if not scenarios_dir.is_dir():
            clear_output(wait=True)
            display(StyledMarkdown(f"**Guard rails are read from the data repo, and there is "
                                   f"none at {resolved_data_dir}.** Source `activate-analysis` "
                                   "or pass `data_dir=`."))
            return
        try:
            rails = guard_rails.ensemble_rails(scenarios_dir, recipe)
        except guard_rails.GuardRailError as exc:
            clear_output(wait=True)
            display(StyledMarkdown(f"**The guard rails of {recipe} don't load**; "
                                   f"`sim check {recipe}` lists:\n\n"
                                   + "\n".join(f"- {issue}" for issue in exc.issues)))
            return
        if not rails:
            clear_output(wait=True)
            display(StyledMarkdown(f"**No guard rails in {recipe}**; `sim guard-rail add` adds one."))
            return
        opened = _open_recipe(runs_dir, recipe, loading)
        if opened is None:
            clear_output(wait=True)
            display(StyledMarkdown(f"**No Monte Carlo batch for {recipe}.**"))
            return
        batch_dir, store, failed_count = opened
        mode = dollar_picker.value
        unit = DollarMode(mode).label
        loading.note(f"Evaluating {recipe}'s guard rails")
        shown = [r for r in guard_rails.ensemble_results(store, rails, mode) if r.applies]
        low, high = time_range.value
        clear_output(wait=True)
        display(StyledMarkdown(
            f"{recipe}: {len(store)} member(s)"
            + (f", {failed_count} failed" if failed_count else "")
            + f", {unit} dollars (batch {batch_dir.name})"))
        if not shown:
            display(StyledMarkdown("No guard rail covers these members' years."))
            return
        display(_png(ensemble_guard_rail_figure(
            shown, f"Guard rails ({recipe})", unit=unit, years=(low, high),
            deaths=store.base_case_deaths())))
        for r in shown:
            table = guard_rails.ensemble_rail_frame(r)
            table = table[(table.index >= low) & (table.index <= high)]
            display(StyledMarkdown(f"**{r.rail.name}** ({r.rail.sense}): {r.rail.describe()}"))
            display(table.style.format(
                {c: "{:.0%}" if not c.startswith("rms") else "${:,.0f}" for c in table.columns},
                na_rep=""))
        display(StyledMarkdown("**Over the whole run**"))
        display(guard_rails.ensemble_summary_frame(shown).style.format(
            {"past": "{:.0%}", "ever_past": "{:.0%}", "past_at_end": "{:.0%}",
             "rms_over": "${:,.0f}"}))
        display(StyledMarkdown(ENSEMBLE_CAVEAT))

    dropdown.observe(_redraw, names="value")
    note_picks(resolved_data_dir, dropdown)
    for picker in (dollar_picker, time_range):
        picker.observe(_redraw, names="value")
    when_cell_is_done(_redraw)
    return widgets.VBox([
        dropdown,
        widgets.HBox([dollar_picker, time_range],
                     layout=widgets.Layout(flex_flow="row wrap", align_items="center")),
        loading, out,
    ])


LARGEST = "largest either way"


def ensemble_drift_widget(*_,
                          data_dir: Path | str | None = None,
                          state_dir: Path | None = None,
                          runs_dir: Path | str | None = None,
                          controls: EnsembleControls | None = None,
                          default: str | None = None,
                          **__,
                          ) -> widgets.VBox:
    """How far an ensemble's portfolios stray from a target set (#359), as
    ``sim ensemble drift`` tabulates it: the spread across members of the
    largest drift either way (or one class's, signed) month by month, the
    strategy's band shaded, then by year end the percentiles and the share
    of members past the band. **Targets** and **Portfolio** start at the
    recipe's strategy's own. ``controls`` shares the years shown."""
    from analysis.sim import drift
    from analysis.sim.drift_charts import drift_fan_figure

    resolved_data_dir = find_data_dir(data_dir)
    runs_dir = find_runs_dir(data_dir=data_dir, state_dir=state_dir, runs_dir=runs_dir)
    recipes = list_ensemble_recipes(runs_dir, resolved_data_dir, keep=[default])
    dropdown = widgets.Dropdown(options=recipes, description="Recipe:")
    if _default(recipes, default) is not None:
        dropdown.value = default
    controls = controls or EnsembleControls(runs_dir=runs_dir)
    time_range = controls.time_range
    targets = widgets.Dropdown(description="Targets:")
    portfolio = widgets.Dropdown(description="Portfolio:")
    measure = widgets.Dropdown(options=[LARGEST], description="Drift:")
    loading = LoadingIndicator()
    out = widgets.Output()
    # The recipe whose targets and portfolios the pickers list, so picking a
    # new recipe resets them while picking a target set doesn't.
    listed: list[str | None] = [None]
    updating = [False]  # while the pickers are refilled for a new recipe
    every = "(every asset lot)"

    def _redraw(*_: object) -> None:
        if updating[0]:
            return
        with out:
            if not dropdown.value:
                clear_output()
                return
            try:
                _draw(dropdown.value)
            finally:
                loading.finish()

    def _draw(recipe: str) -> None:
        opened = _open_recipe(runs_dir, recipe, loading)
        if opened is None:
            clear_output(wait=True)
            display(StyledMarkdown(f"**No Monte Carlo batch for {recipe}.**"))
            return
        batch_dir, store, failed_count = opened
        sets = sorted(store.target_sets())
        if not sets:
            clear_output(wait=True)
            display(StyledMarkdown(f"**No target sets in {recipe}**; `sim target set` adds one."))
            return
        if listed[0] != recipe:
            listed[0] = recipe
            try:
                chosen, tag, _band = drift.pick(store.watches(), sets)
            except ValueError:
                chosen, tag = sets[0], None
            updating[0] = True
            try:
                targets.options, targets.value = sets, chosen
                portfolio.options = [every, *(p for p in store.portfolios() if p)]
                portfolio.value = tag or every
            finally:
                updating[0] = False
        tag = None if portfolio.value == every else portfolio.value
        # The classes this portfolio holds, keeping the one picked if it's still there.
        held = {c for f in store.class_dollars(tag or "").values() for c in f.columns}
        offered = [LARGEST, *(c for c in registries.ASSET_CLASSES if c in held)]
        if list(measure.options) != offered:
            updating[0] = True
            try:
                kept = measure.value if measure.value in offered else LARGEST
                measure.options = offered
                measure.value = kept
            finally:
                updating[0] = False
        _set, _tag, band = drift.pick(store.watches(), sets, targets.value, tag)
        column = drift.MAX_ABS if measure.value == LARGEST else measure.value
        loading.note(f"Working out {recipe}'s drift")
        members = store.drift_frame(targets.value, tag, column)
        low, high = time_range.value
        whose = f"accounts tagged {tag}" if tag else "every asset lot"
        what = "largest drift either way" if column == drift.MAX_ABS else f"{column} share less target"
        clear_output(wait=True)
        display(StyledMarkdown(
            f"{recipe}: {len(store)} member(s)" + (f", {failed_count} failed" if failed_count else "")
            + f"; drift from {targets.value} ({whose}), percentage points (batch {batch_dir.name})"))
        display(_png(drift_fan_figure(members, f"Drift from {targets.value}: {what} ({recipe})",
                                      band=band, years=(low, high), deaths=store.base_case_deaths())))
        table = drift.ensemble_drift_table(members, band=band if column == drift.MAX_ABS else None)
        if not table.empty:
            table = table[[low <= int(str(m)[:4]) <= high for m in table.index]]
            shares = [c for c in ("past_band", "ever_past") if c in table.columns]
            display(table.style.format({**{c: "{:+.1f}" for c in table.columns if c not in shares},
                                        **dict.fromkeys(shares, "{:.0%}")}))

    dropdown.observe(_redraw, names="value")
    note_picks(resolved_data_dir, dropdown)
    for picker in (targets, portfolio, measure, time_range):
        picker.observe(_redraw, names="value")
    when_cell_is_done(_redraw)
    return widgets.VBox([
        dropdown,
        widgets.HBox([targets, portfolio, measure, time_range],
                     layout=widgets.Layout(flex_flow="row wrap", align_items="center")),
        loading, out,
    ])


class _Sides:
    """The recipes a comparison shows, opened: each one's store, and its
    frames in the dollars shown and, for a reserve, CPI-constant. A recipe
    without the account shown has no frame."""

    def __init__(self, runs_dir: Path, names: Sequence[str], controls: EnsembleControls,
                 lot: str | None, reserve_field: widgets.Text, loading: LoadingIndicator):
        self.stores: dict[str, EnsembleSeries] = {}
        for name in names:
            opened = _open_recipe(runs_dir, name, loading)
            assert opened is not None  # listed recipes all have a batch
            self.stores[name] = opened[1]
        baseline = names[0]
        self.account, self.lot = controls.account, lot
        self.after_tax = controls.after_tax
        self.subject = _subject(self.account, lot, self.after_tax)
        self.mode = controls.dollar_picker.value
        self.mode_label = DollarMode(self.mode).label
        self.reserve = _reserve(reserve_field, self.stores[baseline], self.account)
        self.frames: dict[str, pd.DataFrame] = {}
        for name, store in self.stores.items():
            try:
                self.frames[name] = store.frame(
                    self.account, self.mode, lot, after_tax=self.after_tax)
            except EnsembleError:
                pass
        self.cpi_frames: dict[str, pd.DataFrame] = {
            name: (
                frame if self.mode == DollarMode.CPI_CONSTANT
                else self.stores[name].frame(
                    self.account, DollarMode.CPI_CONSTANT, lot, after_tax=self.after_tax)
            )
            for name, frame in self.frames.items()
        } if self.reserve is not None else {}

    def heading(self, baseline: str) -> str:
        heading = (
            f"**{self.subject}**, {self.mode_label} dollars; baseline "
            f"`{baseline}` ({len(self.stores[baseline])} members)"
        )
        if self.reserve is not None:
            heading += f"; reserve ${self.reserve:,.0f} (CPI-constant)"
        return heading

    def pairs(self, baseline: str, variant: str) -> tuple[list[tuple[str, str]], str | None]:
        return pair_members(self.stores[baseline].member_ids(),
                            self.stores[variant].member_ids())

    def cpi_pair(self, baseline: str, variant: str) -> tuple[pd.DataFrame, pd.DataFrame] | None:
        return (self.cpi_frames[baseline], self.cpi_frames[variant]) if self.cpi_frames else None

    def deltas(self, baseline: str, variant: str, clip: Callable[[pd.DataFrame], pd.DataFrame],
               ) -> pd.DataFrame:
        """``variant`` against ``baseline`` by year end, in the years shown."""
        pairs, _unpaired = self.pairs(baseline, variant)
        table = year_end_values(clip(comparison_deltas(
            self.frames[baseline], self.frames[variant], pairs, self.reserve,
            self.cpi_pair(baseline, variant),
        )))
        table.index.name = "year"
        return table

    def tails(self, clip: Callable[[pd.DataFrame], pd.DataFrame]) -> pd.DataFrame:
        """Each recipe's tail means and reserve shares, by year end."""
        return by_year_and_label({
            name: year_end_values(clip(risk_summary(frame, self.reserve,
                                                    self.cpi_frames.get(name))))
            for name, frame in self.frames.items()
        })

    def missing_note(self, names: Sequence[str]) -> StyledMarkdown | None:
        """Which of ``names`` lack the account shown, or None."""
        missing = [name for name in names if name not in self.frames]
        if not missing:
            return None
        return StyledMarkdown(
            f"_No {_described(self.subject)} in {', '.join(f'`{n}`' for n in missing)}._")


def _offer(dropdown: widgets.Dropdown | widgets.SelectMultiple, options: Sequence[str]) -> None:
    """Change ``dropdown``'s options, keeping what's picked that's still offered."""
    if isinstance(dropdown, widgets.SelectMultiple):
        kept = tuple(v for v in dropdown.value if v in options)
        dropdown.options = list(options)
        dropdown.value = kept
    else:
        kept_value = dropdown.value if dropdown.value in options else None
        dropdown.options = list(options)
        dropdown.value = kept_value


def ensemble_compare_widget(*_,
                            data_dir: Path | str | None = None,
                            state_dir: Path | None = None,
                            runs_dir: Path | str | None = None,
                            dollar_mode: DollarModePicker | None = None,
                            log_linear_mode: LogLinearModePicker | None = None,
                            controls: EnsembleControls | None = None,
                            default: str | None = None,
                            variant: str | None = None,
                            **__,
                            ) -> widgets.VBox:
    """Compare two ensembles head to head on risk (#163, #298): a baseline
    recipe and one other that pairs with it (shares its seed, so member
    *i* of each drew the same future), net worth or one account (or one
    lot of it, #170): percentile-breakpoint deltas, paired per-future
    deltas and tail-mean (CVaR) deltas, as charts; the year-end tables are
    ``ensemble_table_widget``'s. ``controls`` shares
    scale, dollars, account and reserve with other ensemble widgets (#171);
    ``default`` is the baseline to start on and ``variant`` the recipe to
    compare it with, when they have a batch; with no ``variant`` none is
    picked."""
    runs_dir = find_runs_dir(data_dir=data_dir, state_dir=state_dir, runs_dir=runs_dir)
    resolved_data_dir = find_data_dir(data_dir)
    recipes = list_ensemble_recipes(runs_dir, resolved_data_dir, keep=[default, variant])
    seeds = RecipeSeeds(runs_dir, recipes)
    baseline_picker = widgets.Dropdown(options=recipes, description="Baseline:")
    if _default(recipes, default) is not None:
        baseline_picker.value = default
    variant_picker = widgets.Dropdown(
        options=seeds.paired_with(baseline_picker.value), value=None, description="Compare:")
    if variant in variant_picker.options:
        variant_picker.value = variant
    # Default dollars are CPI-constant here: a comparison over decades, and
    # the reserve, are in today's dollars.
    controls = controls or EnsembleControls(
        runs_dir=runs_dir, dollar_mode=DollarMode.CPI_CONSTANT,
        log_linear_picker=log_linear_mode, dollar_picker=dollar_mode,
    )
    lot_picker = controls.compare_lot_picker
    time_range = controls.time_range
    loading = LoadingIndicator()
    out = widgets.Output()
    holding = False

    def _new_baseline(change: dict[str, Any]) -> None:
        # The recipes on offer follow the baseline; redraw once, after.
        # Picking the compared recipe as the baseline swaps the two.
        nonlocal holding
        swapped = change["old"] if change["new"] == variant_picker.value else None
        holding = True
        try:
            _offer(variant_picker, seeds.paired_with(baseline_picker.value))
            if swapped in variant_picker.options:
                variant_picker.value = swapped
        finally:
            holding = False
        _redraw()

    def _redraw(*_: object) -> None:
        if holding:
            return
        with out:
            baseline, other = baseline_picker.value, variant_picker.value
            if controls.offer_lots(lot_picker, [name for name in (baseline, other) if name]):
                return
            if not baseline or not other:
                clear_output()
                display(StyledMarkdown(
                    "_Pick a baseline and a recipe to compare it with._" if variant_picker.options
                    else f"_No other recipe pairs with `{baseline}`: none shares its seed._"
                    if baseline else "_Pick a baseline._"))
                return
            try:
                _draw(baseline, other)
            finally:
                loading.finish()

    def _draw(baseline: str, other: str) -> None:
        sides = _Sides(runs_dir, [baseline, other], controls, _lot(lot_picker),
                       controls.reserve_field, loading)
        loading.note("Comparing and drawing")
        log_scale = controls.log_linear_picker.value == LogLinearMode.LOG
        outputs: list[Any] = [StyledMarkdown(sides.heading(baseline))]
        if baseline in sides.frames and other in sides.frames:
            pairs, unpaired = sides.pairs(baseline, other)
            note = f"paired over {len(pairs)} futures" if pairs else f"not paired: {unpaired}"
            outputs.append(StyledMarkdown(
                f"### {other} vs {baseline}\n\n{len(sides.stores[other])} members, {note}."))
            outputs.append(_png(ensemble_compare_figure(
                baseline, other, sides.frames[baseline], sides.frames[other], pairs,
                sides.mode_label, sides.reserve, sides.cpi_pair(baseline, other), log_scale,
                clip=time_range.clip, deaths=sides.stores[baseline].base_case_deaths(),
            )))
        else:
            # The shared account list spans every recipe, so a side may lack
            # the pick: a note, and the other side's own spread, not a delta.
            note = sides.missing_note([baseline, other])
            assert note is not None
            outputs.append(note)
            for name, frame in sides.frames.items():
                outputs.append(_png(ensemble_fan_figure(
                    time_range.clip(summary_with_percentiles(frame)),
                    f"{sides.subject} fan chart ({sides.mode_label}) ({name})",
                    reserve=sides.reserve if sides.mode == DollarMode.CPI_CONSTANT else None,
                    log_scale=log_scale and bool((time_range.clip(frame) > 0).all().all()),
                    height=4, deaths=sides.stores[name].base_case_deaths(),
                )))
        clear_output(wait=True)
        for item in outputs:
            display(item)

    baseline_picker.observe(_new_baseline, names="value")
    variant_picker.observe(_redraw, names="value")
    note_picks(resolved_data_dir, baseline_picker, variant_picker)
    lot_picker.observe(_redraw, names="lots")
    controls.observe_all(_redraw)
    when_cell_is_done(_redraw)
    return widgets.VBox([
        widgets.HBox([baseline_picker, variant_picker]), controls.row(lot_picker), loading, out,
    ])


def ensemble_table_widget(*_,
                          data_dir: Path | str | None = None,
                          state_dir: Path | None = None,
                          runs_dir: Path | str | None = None,
                          controls: EnsembleControls | None = None,
                          default: str | None = None,
                          **__,
                          ) -> widgets.VBox:
    """A baseline recipe against any number of others that pair with it
    (#298), in two year-end tables indexed by (year, recipe): each recipe's
    deltas against the baseline (as ``ensemble_compare_widget`` shows one),
    and every recipe's tail means and reserve shares. ``controls`` shares
    dollars, account and reserve with the other ensemble widgets; ``default``
    is the baseline to start on."""
    runs_dir = find_runs_dir(data_dir=data_dir, state_dir=state_dir, runs_dir=runs_dir)
    resolved_data_dir = find_data_dir(data_dir)
    recipes = list_ensemble_recipes(runs_dir, resolved_data_dir, keep=[default])
    seeds = RecipeSeeds(runs_dir, recipes)
    baseline_picker = widgets.Dropdown(options=recipes, description="Baseline:")
    if _default(recipes, default) is not None:
        baseline_picker.value = default
    variant_picker = widgets.SelectMultiple(
        options=seeds.paired_with(baseline_picker.value), description="Compare:", rows=min(max(len(recipes), 2), 8),
    )
    controls = controls or EnsembleControls(runs_dir=runs_dir, dollar_mode=DollarMode.CPI_CONSTANT)
    lot_picker = controls.table_lot_picker
    time_range = controls.time_range
    loading = LoadingIndicator()
    out = widgets.Output()
    holding = False

    def _new_baseline(*_: object) -> None:
        nonlocal holding
        holding = True
        try:
            _offer(variant_picker, seeds.paired_with(baseline_picker.value))
        finally:
            holding = False
        _redraw()

    def _redraw(*_: object) -> None:
        if holding:
            return
        with out:
            baseline, others = baseline_picker.value, list(variant_picker.value)
            if controls.offer_lots(lot_picker, [baseline, *others] if baseline else []):
                return
            if not baseline or not others:
                clear_output()
                display(StyledMarkdown(
                    "_Pick a baseline and the recipes to compare it with._"
                    if variant_picker.options or not baseline
                    else f"_No other recipe pairs with `{baseline}`: none shares its seed._"))
                return
            try:
                _draw(baseline, others)
            finally:
                loading.finish()

    def _draw(baseline: str, others: list[str]) -> None:
        sides = _Sides(runs_dir, [baseline, *others], controls, _lot(lot_picker),
                       controls.reserve_field, loading)
        loading.note("Comparing")
        outputs: list[Any] = [StyledMarkdown(sides.heading(baseline))]
        note = sides.missing_note([baseline, *others])
        if note is not None:
            outputs.append(note)
        compared = [name for name in others if name in sides.frames]
        if baseline in sides.frames and compared:
            deltas = by_year_and_label({
                name: sides.deltas(baseline, name, time_range.clip) for name in compared})
            outputs.append(StyledMarkdown(f"### Against {baseline}, by year end"))
            outputs.append(deltas.style.format(_share_format(deltas.columns)))
        if sides.frames:
            tails = sides.tails(time_range.clip)
            outputs.append(StyledMarkdown("### Tail and reserve, by recipe"))
            outputs.append(tails.style.format(_share_format(tails.columns)))
        clear_output(wait=True)
        for item in outputs:
            display(item)

    baseline_picker.observe(_new_baseline, names="value")
    variant_picker.observe(_redraw, names="value")
    note_picks(resolved_data_dir, baseline_picker, variant_picker)
    lot_picker.observe(_redraw, names="lots")
    controls.observe_all(_redraw)
    when_cell_is_done(_redraw)
    return widgets.VBox([
        widgets.HBox([baseline_picker, variant_picker]), controls.row(lot_picker), loading, out,
    ])


def _rail_share_format(columns: Any) -> dict[Any, str | Callable[..., Any] | None]:
    """Shares (``past``, ``ever_past``, ``past_at_end`` and their ``Δ``) as
    percents, the RMS as dollars, a change signed."""
    formats: dict[Any, str | Callable[..., Any] | None] = {}
    for column in columns:
        name = str(column)
        delta = name.startswith("Δ")
        if name.lstrip("Δ").startswith(("past", "ever_past")):
            formats[column] = "{:+.0%}" if delta else "{:.0%}"
        elif name.lstrip("Δ").startswith("rms_over"):
            formats[column] = "{:+,.0f}" if delta else "${:,.0f}"
    return formats


def ensemble_guard_rails_table_widget(*_,
                                      data_dir: Path | str | None = None,
                                      state_dir: Path | None = None,
                                      runs_dir: Path | str | None = None,
                                      controls: EnsembleControls | None = None,
                                      default: str | None = None,
                                      **__,
                                      ) -> widgets.VBox:
    """A baseline recipe's guard rails against any number of recipes that
    pair with it (#305), as ``ensemble_table_widget`` sets out net worth: per
    rail both have (matched by name), a table indexed by (year, recipe) of
    each recipe's change in the share of members past it, the share ever
    past it so far and their RMS overshoot, annual and cumulative; then each
    recipe's whole-run summary beside its change against the baseline.
    ``controls`` shares the dollars and the years shown with the other
    ensemble widgets; ``default`` is the baseline to start on. Each recipe's
    own rails are read at each draw, so an edited rail shows without a rerun."""
    from analysis.sim import guard_rails
    from analysis.sim.guard_rail_charts import ENSEMBLE_CAVEAT

    resolved_data_dir = find_data_dir(data_dir)
    runs_dir = find_runs_dir(data_dir=data_dir, state_dir=state_dir, runs_dir=runs_dir)
    recipes = list_ensemble_recipes(runs_dir, resolved_data_dir, keep=[default])
    seeds = RecipeSeeds(runs_dir, recipes)
    baseline_picker = widgets.Dropdown(options=recipes, description="Baseline:")
    if _default(recipes, default) is not None:
        baseline_picker.value = default
    variant_picker = widgets.SelectMultiple(
        options=seeds.paired_with(baseline_picker.value), description="Compare:",
        rows=min(max(len(recipes), 2), 8),
    )
    controls = controls or EnsembleControls(runs_dir=runs_dir, dollar_mode=DollarMode.CPI_CONSTANT)
    dollar_picker, time_range = controls.dollar_picker, controls.time_range
    loading = LoadingIndicator()
    out = widgets.Output()
    holding = False

    def _new_baseline(*_: object) -> None:
        nonlocal holding
        holding = True
        try:
            _offer(variant_picker, seeds.paired_with(baseline_picker.value))
        finally:
            holding = False
        _redraw()

    def _redraw(*_: object) -> None:
        if holding:
            return
        with out:
            baseline, others = baseline_picker.value, list(variant_picker.value)
            if not baseline or not others:
                clear_output()
                display(StyledMarkdown(
                    "_Pick a baseline and the recipes to compare it with._"
                    if variant_picker.options or not baseline
                    else f"_No other recipe pairs with `{baseline}`: none shares its seed._"))
                return
            try:
                _draw(baseline, others)
            finally:
                loading.finish()

    # Evaluated rails and member counts by (batch, rails, dollars), as
    # guard_rails_compare_widget keeps them: the rails are re-read at each
    # draw, so an edit shows, but a change of years only re-clips.
    evaluated: dict[tuple[Path | None, tuple[guard_rails.GuardRail, ...], DollarMode],
                    tuple[list[guard_rails.EnsembleRailResult], int]] = {}

    def _draw(baseline: str, others: list[str]) -> None:
        scenarios_dir = resolved_data_dir / "scenarios"
        if not scenarios_dir.is_dir():
            clear_output(wait=True)
            display(StyledMarkdown(f"**Guard rails are read from the data repo, and there is "
                                   f"none at {resolved_data_dir}.** Source `activate-analysis` "
                                   "or pass `data_dir=`."))
            return
        mode = dollar_picker.value
        unit = DollarMode(mode).label
        results: dict[str, list[guard_rails.EnsembleRailResult]] = {}
        sizes: dict[str, int] = {}
        for name in (baseline, *others):
            try:
                rails = guard_rails.ensemble_rails(scenarios_dir, name)
            except guard_rails.GuardRailError as exc:
                clear_output(wait=True)
                display(StyledMarkdown(f"**The guard rails of {name} don't load**; "
                                       f"`sim check {name}` lists:\n\n"
                                       + "\n".join(f"- {issue}" for issue in exc.issues)))
                return
            if not rails:  # nothing to evaluate: don't open (or build) its store
                results[name] = []
                continue
            batch_dir = latest_ensemble_batch(runs_dir / name, mode="monte_carlo")
            key = (batch_dir, tuple(rails), mode)
            if batch_dir is None or key not in evaluated:
                opened = _open_recipe(runs_dir, name, loading)
                if opened is None:  # its batch went away since the recipes were listed
                    clear_output(wait=True)
                    display(StyledMarkdown(f"**No Monte Carlo batch for {name}.**"))
                    return
                store = opened[1]
                loading.note(f"Evaluating {name}'s guard rails")
                key = (opened[0], tuple(rails), mode)
                evaluated[key] = (guard_rails.ensemble_results(store, rails, mode), len(store))
            results[name], sizes[name] = evaluated[key]
        tables = guard_rails.ensemble_comparison_table(results, baseline, others)
        clear_output(wait=True)
        members = f" ({sizes[baseline]} members)" if baseline in sizes else ""
        display(StyledMarkdown(
            f"**Guard rails**, {unit} dollars; baseline `{baseline}`{members}"))
        lacking = [name for name in (baseline, *others) if not results[name]]
        if lacking:
            display(StyledMarkdown(
                f"_No guard rails in {', '.join(f'`{n}`' for n in lacking)}._"))
        if not tables:
            display(StyledMarkdown(f"_No guard rail is in both `{baseline}` and a recipe "
                                   "compared with it._"))
            return
        senses = {r.rail.name: r.rail.sense for r in results[baseline]}
        display(StyledMarkdown(f"### Against {baseline}, by year"))
        for rail, table in tables.items():
            years = table.index.get_level_values("year")
            low, high = time_range.value
            table = table[(years >= low) & (years <= high)]
            display(StyledMarkdown(f"**{rail}** ({senses[rail]}): Δ is the recipe less "
                                   f"`{baseline}`"))
            display(table.style.format(_rail_share_format(table.columns), na_rep=""))
        summary = guard_rails.ensemble_summary_comparison(results, baseline, others)
        display(StyledMarkdown("### Over the whole run, by recipe"))
        display(summary.style.format(_rail_share_format(summary.columns), na_rep=""))
        display(StyledMarkdown(
            "For a rail to stay inside, a negative Δpast is fewer members past it; for a "
            "positive rail, fewer members with room to spend more. " + ENSEMBLE_CAVEAT))

    baseline_picker.observe(_new_baseline, names="value")
    variant_picker.observe(_redraw, names="value")
    note_picks(resolved_data_dir, baseline_picker, variant_picker)
    for picker in (dollar_picker, time_range):
        picker.observe(_redraw, names="value")
    when_cell_is_done(_redraw)
    return widgets.VBox([
        widgets.HBox([baseline_picker, variant_picker]),
        widgets.HBox([dollar_picker, time_range],
                     layout=widgets.Layout(flex_flow="row wrap", align_items="center")),
        loading, out,
    ])
