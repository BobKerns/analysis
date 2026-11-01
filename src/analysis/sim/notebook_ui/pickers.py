"""Scenario pickers: ipywidgets that hand back live, open connections.

``scenario_picker`` is for single-scenario notebooks (net_worth, accounts);
``multi_scenario_picker`` is for notebooks comparing a baseline against one
or more variants (scenario_compare). Both return ``(widget, container)``
where ``container`` is a small object holding the currently open
connection(s) -- it's mutated in place on every picker interaction, so a
notebook cell that captured it once keeps seeing the current selection.
"""

from __future__ import annotations

from abc import abstractmethod
from contextvars import ContextVar
import html as html_lib
import os
import re
import weakref
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
import calendar
import datetime
from dateutil import relativedelta

import ipywidgets as widgets
import pandas as pd
import traitlets
from IPython.display import clear_output, display

from analysis.frames import clip_years, index_years
from analysis.sim.accounts import KIND_LABELS
from analysis.sim.allocation import GROUPINGS, Grouping, _NO_SPLIT
from analysis.sim.connection import RunConnection, ScenarioConnection
from analysis.sim.ensemble import latest_ensemble_batch, member_run_statuses
from analysis.sim.notebook_ui.first_draw import when_cell_is_done
from analysis.sim.loader import (
    PendingRun, ScenarioRef, list_scenario_names, read_disabled_scenarios, resolve_scenario_name,
)
from analysis.sim.accounts import plottable_accounts
from analysis.sim.readme import description_view
from analysis.sim.types import (
    AccountKind, AccountKindAny, DollarMode, LogLinearMode, ValuePlotMode,
    AccountName, ScenarioName, AttributionAxis, DeltaScale, HeatmapValue,
)
from analysis.sim.notebook_ui.styled import StyledMarkdown, styled_html


def to_month(year: int, month: int) -> datetime.date:
    """Return a date representing the last day of the given month and year."""
    last_day = calendar.monthrange(year, month)[1]
    return datetime.date(year, month, last_day)

def this_month() -> datetime.date:
    """Return a date representing the last day of the current month."""
    today = datetime.date.today()
    return to_month(today.year, today.month)


def warning_html(message: str) -> str:
    """Build the HTML for a legible warning box.

    Markdown blockquotes (`> ⚠️ ...`) pick up the ambient text color in some
    notebook themes, which can render as black-on-black. This builds plain
    HTML instead, with an explicit light-yellow background and dark text;
    ``notebook.css`` styles its inline code like the rest of our output.
    """
    # Escape first so literal "<...>" in messages (e.g. "sim load <name>")
    # isn't swallowed as an HTML tag; **bold**/`code` add real tags after.
    text = html_lib.escape(message)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    return styled_html(
        '<div style="background-color:#fff8c5; color:#111; padding:6px 10px; '
        'border-left:4px solid #e6b800; border-radius:3px; margin:4px 0;">'
        f"⚠️ {text}</div>"
    )


def display_warning(message: str) -> None:
    """Display a warning with a legible background (see `warning_html`)."""
    from IPython.display import HTML, display

    display(HTML(warning_html(message)))


# The detachers of the observers the widget now being drawn by
# Connections.show() puts on shared controls; None outside such a draw.
_shown_observers: ContextVar[list[Callable[[], None]] | None] = ContextVar(
    "_shown_observers", default=None,
)


def observe_while_shown(control: traitlets.HasTraits, handler: Callable[..., Any],
                        names: str = "value") -> None:
    """``control.observe(handler, names=names)``, for as long as the widget
    being drawn is shown.

    A shared control (the dollar toggle, the Years slider) outlives the
    widgets that observe it: ``Connections.show`` rebuilds its widget on a
    scenario change and closes the old run. Observed this way, the old
    widget's handler is detached before the rebuild, rather than going on
    redrawing a closed run, or raising and so stopping the handlers after
    it. Outside a ``show()`` draw it is a plain ``observe``."""
    control.observe(handler, names=names)
    detachers = _shown_observers.get()
    if detachers is not None:
        detachers.append(lambda: control.unobserve(handler, names=names))


def _detach(detachers: list[Callable[[], None]]) -> None:
    """Run and forget ``detachers``, so running them again does nothing."""
    while detachers:
        detachers.pop()()


def _trait(descriptor: traitlets.TraitType) -> Any:
    """``descriptor``, typed as the value it holds on an instance.

    A trait's class attribute is annotated with that value's type, but a type
    checker sees the descriptor object assigned to it and rejects the pair.

    A widget subclass redefining an inherited trait (a ToggleButtons'
    ``value`` or ``label``) leaves the annotation off: the base declares it
    ``Any | None``, and narrowing a writable attribute is an
    invalid-mutable-override."""
    return descriptor


class Connections(traitlets.HasTraits):
    """
    Holds the currently open scenario and run connections.
    This class is intended to be used as a singleton in notebooks, where
    it is mutated in place whenever the scenario picker changes.

    This is the base class for SingleScenarioConnections and MultiScenarioConnections.
    """

    # What show() redraws on, rather than the selection itself: a change can
    # keep the scenario and swap only the run (another ensemble member,
    # #111), and a scenario change must redraw once, after every connection
    # has been swapped, not once per attribute.
    revision: int = _trait(traitlets.Int(0))

    # The currently selected ensemble member within the scenario.
    # 0 if not selecting any but the default member.
    member: int = _trait(traitlets.Int(0))

    def notify_changed(self) -> None:
        """Redraw every show() output; call once a change has fully landed."""
        # Every shown widget lets go of the shared controls before any is
        # rebuilt: the first rebuild can move one (TimeRangePicker.cover),
        # and a widget not yet rebuilt would redraw its closed run.
        for detachers in self._shown_observers():
            _detach(detachers)
        self.revision += 1

    def _shown_observers(self) -> list[list[Callable[[], None]]]:
        """Each show() output's detachers (see observe_while_shown)."""
        # Not a trait, nor a dataclass field: set on first use.
        if "_shown_detachers" not in self.__dict__:
            self.__dict__["_shown_detachers"] = []
        return self.__dict__["_shown_detachers"]

    @abstractmethod
    def _invoke(self, fn: Callable, *args, **kwargs):
        """Invoke the given function with the current connections context."""
        raise NotImplementedError()

    def show(self, fn: Callable, *args, **kwargs) -> widgets.Output:
        """
        Return an output widget that displays the result of calling `fn`
        with the current run connection.

        `fn` must accept arbitrary keyword arguments (end its signature
        with ``**_``): besides ``**kwargs``, ``show()`` passes context such as
        ``member``, the selected ensemble member, and may add more. A widget
        uses what it needs and ignores the rest.

        Parameters
        ----------
        fn : callable
            The function to call with the current run connection.
        **kwargs
            Additional keyword arguments to pass to `fn`.
        
        """
        out = widgets.Output()
        # What the shown widget observes on shared controls (observe_while_shown).
        detachers: list[Callable[[], None]] = []
        self._shown_observers().append(detachers)

        def _draw():
            # Usually done already, by notify_changed for every output at once.
            _detach(detachers)
            with out:
                clear_output(wait=True)
                token = _shown_observers.set(detachers)
                try:
                    result = self._invoke(fn, *args, member=self.member, **kwargs)
                finally:
                    _shown_observers.reset(token)
                if result is not None:
                    display(result)

        def _redraw(*_):
            # The first draw waits for the end of the cell that called show(), so it
            # happens like any later redraw (see notebook_ui.first_draw).
            when_cell_is_done(_draw)
        self.observe(_redraw, names="revision")
        _redraw()
        return out

# ---------- single-scenario picker ----------


@dataclass
class SingleScenarioConnections(Connections):
    """Currently selected scenario, with its open connections.

    ``run`` is ``None`` only when the picker was built with
    ``require_run=False`` and the scenario has no run yet.
    """

    value: ScenarioName = _trait(traitlets.Any())
    scenario_ref: ScenarioRef = _trait(traitlets.Any())
    scenario: ScenarioConnection = _trait(traitlets.Any())
    run: RunConnection | None = _trait(traitlets.Any())

    def close(self) -> None:
        self.scenario.close()
        if self.run is not None:
            self.run.close()

    def show_scenario(self, fn: Callable, *args, **kwargs) -> widgets.Output:
        """
        Return an output widget that displays the result of calling `fn`
        with the current scenario connection.

        As with :meth:`show`, `fn` must accept arbitrary keyword arguments
        (end its signature with ``**_``): it receives the same context
        keywords, such as ``member``, and ``run``, which is None when the
        scenario has no run (``scenario_picker(require_run=False)``).

        Parameters
        ----------
        fn : callable
            The function to call with the current scenario connection.
        **kwargs
            Additional keyword arguments to pass to `fn`.
        
        """
        def _invoke_with_scenario(run: RunConnection | None, *args, **kwargs):
            return fn(self.scenario, *args, run=run, **kwargs)
        _scenario_only.add(_invoke_with_scenario)
        return self.show(_invoke_with_scenario, *args, **kwargs)

    def _invoke(self, fn: Callable, *args, **kwargs):
        """Invoke the given function with the current connections context."""
        if self.run is None and fn not in _scenario_only:
            raise RuntimeError("No run connection available")
        return fn(self.run, *args, **kwargs)


# show_scenario's wrappers: they read the scenario, so they run without a run
# (#312's lifespan notebook, with scenario_picker(require_run=False)).
_scenario_only: weakref.WeakSet[Callable] = weakref.WeakSet()


def _open_single(
    data_dir: Path, name: str, *,
    require_run: bool,
    scenarios_dir: Path | None = None,
    runs_dir: Path | None = None,
    run_path: Path | None = None,
) -> SingleScenarioConnections:
    ref = resolve_scenario_name(data_dir, name,
                                scenarios_dir=scenarios_dir,
                                runs_dir=runs_dir,
                                run_path=run_path)
    if require_run and ref.run_path is None:
        raise FileNotFoundError(
            f"no runs for {name!r} in {ref.runs_dir}; run "
            f"`analysis sim load {name} && analysis sim run {name}`"
        )
    scenario = ScenarioConnection(data_dir, name)
    run = RunConnection(ref.run_path, scenario) if ref.run_path is not None else None
    return SingleScenarioConnections(name, ref, scenario, run)

_META_COLS = [
    "scenario_name", "status", "started_at", "finished_at",
    "code_version", "code_dirty", "data_version", "data_dirty",
    "run_by",
]


def enabled_scenario_names(
    data_dir: Path, *, scenarios_dir: Path | None = None, keep: Iterable[str | None] = (),
) -> list[str]:
    """The scenarios a picker offers: every one but the disabled ones
    (`sim scenario disable`, #300), except those in ``keep`` (the ones it
    starts on)."""
    disabled = read_disabled_scenarios(data_dir) - set(keep)
    return [name for name in list_scenario_names(data_dir, scenarios_dir=scenarios_dir)
            if name not in disabled]


def note_picks(data_dir: Path, *pickers: widgets.ValueWidget) -> None:
    """Picking a scenario in a notebook is use of it (#322): when any of
    ``pickers`` changes, every scenario they now show moves to the top of
    the working set, the first picker's topmost. A value is a name, a tuple
    of names (a multiple select), or None. The pick the widget starts on
    isn't one."""
    from analysis.sim import working_set

    def _picked(*_: object) -> None:
        names: list[str] = []
        for picker in pickers:
            value = picker.value
            names.extend(value if isinstance(value, tuple) else [value] if value else [])
        working_set.use(data_dir, *names)

    for picker in pickers:
        picker.observe(_picked, names="value")


def find_data_dir(data_dir: Path | str | None = None) -> Path:
    """
    Find the data directory for the analysis repository. Works from notebook files
    and scripts within the repository.

    Parameters
    ----------
    data_dir : Path | str | None, optional
        The base directory for the data repository. If None, defaults to
        the current repo's data/shared/sim or the value of the
        $ANALYSIS_DATA_DIR environment variable.

    Returns
    -------
    Path
        The resolved data directory.
    """
    if data_dir is None:
        env = os.environ.get("ANALYSIS_DATA_DIR")
        return Path(env) if env is not None else _checkout_data_dir()
    return Path(data_dir)


def _checkout_data_dir() -> Path:
    """The data dir linked into this checkout, found from the working
    directory (a notebook runs in notebooks/). One function so the tests
    can point it away from a developer's real data repo."""
    repo = Path.cwd().parent if Path.cwd().name == "notebooks" else Path.cwd()
    return repo / "data" / "shared" / "sim"


def find_state_dir(*,
                   data_dir: Path | str | None = None,
                   state_dir: Path | None = None,
                   default_state_dir: Path | None = None) -> Path:
    """
    Find the state directory for the analysis repository. Works from notebook files
    and scripts within the repository.

    Parameters
    ----------
    state_dir : Path | None, optional
        The state directory to use. If None, defaults to the value of the
        $ANALYSIS_STATE_DIR environment variable or the repo's data/state directory.
    data_dir : Path | str | None, optional
        The base directory for the data repository. If None, defaults to
        the current repo's data/shared/sim or the value of the
        $ANALYSIS_DATA_DIR environment variable.
    default_state_dir : Path | None, optional
        The default state directory to use if the environment variable is not set.

    Returns
    -------
    Path
        The resolved state directory.
    """
    data_dir = find_data_dir(data_dir)
    if state_dir is not None:
        return data_dir / state_dir
    if default_state_dir is None:
        default_state_dir = data_dir.parent / "state"
    return Path(os.environ.get("ANALYSIS_STATE_DIR", default_state_dir))


def find_runs_dir(data_dir: Path | str | None = None,
                  state_dir: Path | None = None,
                  runs_dir: Path | str | None = None) -> Path:
    """
    Find the runs directory for the analysis repository. Works from notebook files
    and scripts within the repository.

    Parameters
    ----------
    data_dir : Path | str | None, optional
        The base directory for the data repository. If None, defaults to
        the current repo's data/shared/sim or the value of the
        $ANALYSIS_DATA_DIR environment variable.
    state_dir : Path | None, optional
    runs_dir : Path | str | None, optional
        The default runs directory to use if the environment variable is not set.

    Returns
    -------
    Path
        The resolved runs directory.
    """
    state_dir = find_state_dir(data_dir=data_dir,
                               state_dir=state_dir)
    if runs_dir is None:
        runs_dir = state_dir / "runs"
    return Path(os.environ.get("ANALYSIS_RUNS_DIR", runs_dir))


def scenario_picker(
    data_dir: Path | str | None = None,
    default: str | None = None, *,
    scenarios_dir: Path | None = None,
    runs_dir: Path | None = None,
    require_run: bool = True,
    show_metadata: bool = False,
    show_description: bool = False,
) -> tuple[widgets.VBox, SingleScenarioConnections, widgets.Dropdown]:
    """A ``Scenario:`` dropdown that keeps a live connection pair in sync.

    Parameters
    ----------
    data_dir : Path | str | None, optional
        The base directory for the data repository. Defaults to the current
        repo's data/shared/sim.
    default : str | None, optional
        The default scenario to select. If None, resolves using
        $ANALYSIS_SCENARIO, .current_scenario, or "base".
    scenarios_dir : Path | None, optional
        The directory containing scenario definitions. Defaults to None.
    runs_dir : Path | None, optional
        The directory containing run files. Defaults to None.
    require_run : bool, default True
        Whether a run connection is required. If True and no run is found,
        raises FileNotFoundError.
    show_metadata : bool, default False
        Whether to display run metadata below the scenario reference.
    show_description : bool, default False
        Whether to display the scenario description below the scenario reference.

    When the selected scenario is an ensemble recipe with generated members
    and ``require_run`` is true, a second ``Member:`` dropdown appears,
    defaulting to member 0 (the recipe's deterministic member -- ADR-006
    §9) and listing every other generated member, labeled with its status
    when it didn't succeed. Picking one points ``container.run`` at that
    member's own run file. Hidden for an ordinary scenario, and never shown
    at all when ``require_run=False``, since that only ever looks at the
    recipe's own (unresolved, member-independent) intent.

    Returns ``(widget, container)``; ``container`` is a
    :class:`SingleScenarioConnections`, refreshed in place (closing the
    previous connections) whenever the dropdown changes.
    """

    data_dir = find_data_dir(data_dir)
    if default is None:
    # Same resolution the CLI uses: $ANALYSIS_SCENARIO, else the data repo's
    # .current_scenario, else "current".
        default = resolve_scenario_name(
            data_dir, os.environ.get("ANALYSIS_SCENARIO") or None,
            scenarios_dir=scenarios_dir,
            runs_dir=runs_dir,
        ).name
    options = enabled_scenario_names(data_dir, scenarios_dir=scenarios_dir, keep=[default])
    dropdown = widgets.Dropdown(options=options, value=default, description="Scenario:")
    member_dropdown = widgets.Dropdown(description="Member:", layout=widgets.Layout(display="none"))
    ref_out = widgets.Output()

    container = _open_single(data_dir, default,
                             runs_dir=runs_dir,
                             require_run=require_run,
                             scenarios_dir=scenarios_dir)
    vbox = widgets.VBox([dropdown, member_dropdown, ref_out])

    def _redraw() -> None:
        with ref_out:
            clear_output(wait=True)
            display(container.scenario_ref)
            if show_metadata:
                rc = container.run
                if rc is not None:
                    run_meta = rc.metadata().copy()
                    run_meta["code_version"] = run_meta["code_version"].str[:12]
                    run_meta["data_version"] = run_meta["data_version"].str[:12]
                    display(run_meta.T)
            if show_description:
                display(description_view(container.scenario))

    def _on_member_change(change) -> None:
        # A notebook comparing scenarios (multi_scenario_picker) has no
        # equivalent of this -- comparing one member against another is
        # meaningless, every member already differs only by its draws.
        if change["new"] is None:
            # _sync_member_dropdown clearing member_dropdown.options (no
            # longer an ensemble, or no members at all) propagates to
            # .value here too -- nothing to switch to.
            container.member = 0
            return
        ref = container.scenario_ref
        batch = latest_ensemble_batch(ref.runs_dir / ref.name)
        assert batch is not None  # the member dropdown lists only an existing batch's members
        old_run = container.run
        run_path = batch / f"{change['new']}.sqlite3"
        container.run = RunConnection(run_path, container.scenario)
        member_name = change['new']
        container.member = int(member_name.split("_", maxsplit=1)[1])
        # The panel and metadata describe the member actually shown, not
        # the member 0 that resolve_scenario_name picked.
        container.scenario_ref = replace(ref, run=PendingRun(ref.runs_dir, ref.name, run_path))
        if old_run is not None:
            old_run.close()
        _redraw()
        if not changing_scenario:
            container.notify_changed()

    def _sync_member_dropdown() -> None:
        """Show a member picker when the current scenario is an ensemble
        recipe with generated members -- require_run=False never reaches
        this (it only ever looks at the recipe's own intent, identical
        regardless of member, since member resolution happens in each
        member's own *run*, not in the shared scenario-level intent)."""
        ref = container.scenario_ref
        batch = latest_ensemble_batch(ref.runs_dir / ref.name) if require_run else None
        if batch is None:
            member_dropdown.layout.display = "none"
            member_dropdown.options = ()
            return
        statuses = member_run_statuses(batch)
        member_options = [
            (stem if status == "succeeded" else f"{stem} ({status})", stem)
            for stem, status in sorted(statuses.items())
        ]
        current = container.run.run_path.stem if container.run is not None else None
        member_dropdown.options = member_options
        # Already loaded (_open_single/resolve_scenario_name already default
        # to member 0): setting the same value is a no-op in traitlets, so
        # this only ever triggers _on_member_change's rebuild when it's an
        # actual change -- switching scenarios to a *different* ensemble
        # recipe whose current member isn't 0, or one with no member 0 at
        # all.
        member_dropdown.value = current if current in statuses else (
            member_options[0][1] if member_options else None
        )
        member_dropdown.layout.display = None

    # _on_change notifies once at its end, so a member switch it causes
    # (via _sync_member_dropdown) doesn't redraw a half-swapped container.
    changing_scenario = False

    def _on_change(change) -> None:
        nonlocal changing_scenario
        old_scenario, old_run = container.scenario, container.run
        new = _open_single(data_dir, change["new"],
                           runs_dir=runs_dir,
                           require_run=require_run,
                           scenarios_dir=scenarios_dir)
        container.scenario_ref = new.scenario_ref
        container.scenario = new.scenario
        container.run = new.run
        old_scenario.close()
        if old_run is not None:
            old_run.close()
        changing_scenario = True
        try:
            _sync_member_dropdown()
        finally:
            changing_scenario = False
        _redraw()
        container.notify_changed()

    dropdown.observe(_on_change, names="value")
    member_dropdown.observe(_on_member_change, names="value")
    note_picks(data_dir, dropdown)
    traitlets.link((dropdown, "value"), (container, "value"))
    _sync_member_dropdown()
    _redraw()

    return vbox, container, dropdown


# ---------- multi-scenario (baseline + variants) picker ----------


@dataclass
class MultiScenarioConnections(Connections):
    """Currently selected baseline + variants, with their open run connections.

    ``connections`` only holds scenarios that resolved to a run;
    ``warnings`` explains anything dropped. ``ok`` is False when there's
    nothing sensible to render (e.g. the baseline itself has no run).
    """

    connections: dict[str, RunConnection]
    baseline: ScenarioName
    variants: list[ScenarioName]
    colors: dict[str, str]
    warnings: list[str] = field(default_factory=list)

    @property
    def scenarios(self) -> list[ScenarioName]:
        return [self.baseline, *self.variants] if self.baseline else []

    @property
    def ok(self) -> bool:
        return bool(self.baseline)

    def _invoke(self, fn: Callable, **kwargs) -> widgets.Widget:
        baseline_rc = self.connections[self.baseline] if self.baseline in self.connections else None
        variant_rcs = {
            variant: self.connections[variant]
            for variant in self.variants
            if variant in self.connections
        }
        return fn(self,
                  baseline=self.baseline,
                  baseline_rc=baseline_rc,
                  variants=self.variants,
                  variant_rcs=variant_rcs,
                  connections=self.connections,
                  scenarios=self.scenarios,
                  **kwargs)


def _load_multi(
    data_dir: Path,
    baseline: str,
    variants: list[str],
    max_variants: int,
) -> MultiScenarioConnections:
    if not variants:
        return MultiScenarioConnections(
            {}, "", [], {}, ["Select at least one variant to compare."]
        )
    if len(variants) > max_variants:
        return MultiScenarioConnections({}, "", [], {}, [
            f"Pick at most {max_variants} variants (selected {len(variants)})."
        ])

    connections: dict[str, RunConnection] = {}
    missing = []
    for name in [baseline, *variants]:
        ref = resolve_scenario_name(data_dir, name)
        if ref.run_path is None:
            missing.append(name)
            continue
        scenarios = _open_single(data_dir, name, require_run=True)
        connections[name] = RunConnection(ref.run_path, scenarios.scenario)

    if baseline not in connections:
        return MultiScenarioConnections(connections, "", [], {}, [
            f"No run for baseline `{baseline}` — run "
            f"`analysis sim load {baseline} && analysis sim run {baseline}`."
        ])

    resolved_variants = [name for name in variants if name in connections]
    scenarios = [baseline, *resolved_variants]

    import matplotlib.pyplot as plt

    colors = dict(
        zip(scenarios, plt.rcParams["axes.prop_cycle"].by_key()["color"], strict=False)
    )

    warnings = []
    if missing:
        warnings.append(
            "No run for: " + ", ".join(f"`{name}`" for name in missing)
            + " (excluded) — run `analysis sim load <name> && analysis sim run <name>`."
        )
    return MultiScenarioConnections(
        connections, baseline, resolved_variants, colors, warnings
    )


def multi_scenario_picker(
    data_dir: Path | str | None = None,
    default_baseline: str | None = None,
    default_variants: list[str] | None = None,
    intent_dir: Path | str | None = None,
    runs_dir: Path | str | None = None,
    max_variants: int = 4,
):
    """Baseline dropdown + variant multi-select, backed by live run connections.

    Parameters
    ----------
    data_dir : Path | str | None, optional
        The base directory for the data repository. Defaults to the current
        repo's data/shared/sim.
    default_baseline : str | None, optional
        The default baseline scenario to select. If None, resolves using
        $ANALYSIS_SCENARIO, .current_scenario, or "current".
    default_variants : list[str] | None, optional
        The default variant scenarios to select. If None, selects the first
        few available variants up to max_variants.
    intent_dir : Path | str | None, optional
        The directory containing scenario intents. Defaults to None.
    runs_dir : Path | str | None, optional
        The directory containing run files. Defaults to None.
    max_variants : int, default 4
        The maximum number of variants that can be selected.

    Returns ``(widget, container)``; ``container`` is a
    :class:`MultiScenarioConnections`, refreshed in place (closing dropped
    connections) whenever either picker changes.
    """

    if data_dir is None:
        data_dir = find_data_dir()
    data_dir = Path(data_dir)
    if default_baseline is None:
        # Same resolution the CLI uses: $ANALYSIS_SCENARIO, else the data repo's
        # .current_scenario, else "current".
        default_baseline = resolve_scenario_name(
            data_dir, os.environ.get("ANALYSIS_SCENARIO") or None,
            intent_dir=intent_dir, runs_dir=runs_dir,
        ).name
    available = enabled_scenario_names(data_dir, keep=[default_baseline, *(default_variants or [])])

    baseline_picker = widgets.Dropdown(
        options=available, value=default_baseline, description="Baseline:",
    )
    variant_picker = widgets.SelectMultiple(
        options=[name for name in available if name != default_baseline],
        # Only None picks defaults: an explicit [] means no variants yet.
        value=tuple(default_variants if default_variants is not None
                    else [name for name in available
                          if name != default_baseline][:max_variants]),
        description="Variants:",
        rows=min(len(available), 8),
    )
    warnings_out = widgets.Output()

    def _sync_variant_options(*_):
        variant_picker.options = [
            name for name in available if name != baseline_picker.value
        ]
        variant_picker.value = tuple(
            v for v in variant_picker.value if v != baseline_picker.value
        )

    baseline_picker.observe(_sync_variant_options, names="value")

    container = _load_multi(
        data_dir, baseline_picker.value, list(variant_picker.value), max_variants,
    )

    def _redraw() -> None:
        with warnings_out:
            clear_output(wait=True)
            for message in container.warnings:
                display_warning(message)

    def _on_change(*_) -> None:
        old_connections = dict(container.connections)
        new = _load_multi(
            data_dir,
            baseline_picker.value,
            list(variant_picker.value),
            max_variants,
        )
        container.connections = new.connections
        container.baseline = new.baseline
        container.variants = new.variants
        container.colors = new.colors
        container.warnings = new.warnings
        for name, rc in old_connections.items():
            if new.connections.get(name) is not rc:
                rc.close()
        _redraw()
        container.notify_changed()

    baseline_picker.observe(_on_change, names="value")
    variant_picker.observe(_on_change, names="value")
    note_picks(data_dir, baseline_picker, variant_picker)
    _redraw()

    picker_widget = widgets.VBox(
        [widgets.VBox([baseline_picker, variant_picker]), warnings_out]
    )
    return picker_widget, container


class DollarModePicker(widgets.ToggleButtons):
    """A toggle button widget for selecting nominal vs. CPI-constant dollars."""
    value = _trait(traitlets.UseEnum(DollarMode))
    label = _trait(traitlets.Unicode("Nominal"))
    def __init__(self, dollar_mode: DollarMode = DollarMode.NOMINAL):
        """
        Initialize the dollar mode picker.

        Parameters
        ----------
        dollar_mode : DollarMode, default DollarMode.NOMINAL
            The initial dollar mode. Can be either nominal or CPI-constant.
        """
        super().__init__(
            options=[("Nominal", DollarMode.NOMINAL),
                     ("CPI-constant", DollarMode.CPI_CONSTANT)],
            description="Dollar basis:",
            layout=widgets.Layout(
                border="1px solid #888", padding="4px 6px", margin="0 20px 0 0",
            ),
        )
        self.value = dollar_mode

    def _on_value_change(self, change):
        self.label = "CPI-constant" if self.value == DollarMode.CPI_CONSTANT else "Nominal"


class AfterTaxPicker(widgets.ToggleButtons):
    """Pre-tax or after-tax net worth (#251): ``value`` is True for after
    tax (the default), which subtracts the tax a drawdown would owe and tax
    due unpaid."""
    value = _trait(traitlets.Bool(True))

    def __init__(self, after_tax: bool = True):
        super().__init__(
            options=[("Pre-tax", False), ("After-tax", True)],
            description="Net worth:",
            layout=widgets.Layout(
                border="1px solid #888", padding="4px 6px", margin="0 20px 0 0",
            ),
        )
        self.value = after_tax


class ValuePlotModePicker(widgets.ToggleButtons):
    """A toggle button widget for selecting total vs. monthly values."""
    value = _trait(traitlets.UseEnum(ValuePlotMode))
    label = _trait(traitlets.Unicode("Total"))
    def __init__(self, value_mode: ValuePlotMode = ValuePlotMode.TOTAL):
        """
        Initialize the value plot mode picker.

        Parameters
        ----------
        value_mode : ValuePlotMode, default ValuePlotMode.TOTAL
            The initial value plot mode. Can be either total or monthly.
        """
        super().__init__(
            options=[("Total", ValuePlotMode.TOTAL), ("Monthly", ValuePlotMode.MONTHLY)],
            description="Values:",
            layout=widgets.Layout(
                border="1px solid #888", padding="4px 6px", margin="0 20px 0 0",
            ),
        )
        self.value = value_mode
        
    def _on_value_change(self, change):
        self.label = "Monthly" if self.value == ValuePlotMode.MONTHLY else "Total"


class YAxisPlotModePicker(widgets.ToggleButtons):
    """A toggle button widget for selecting zero-anchored vs. auto-scaled Y-axis."""
    value = _trait(traitlets.Bool(True))
    label = _trait(traitlets.Unicode("Zero-anchored"))
    def __init__(self, y_axis_mode: bool = True):
        """
        Initialize the Y-axis plot mode picker.

        Parameters
        ----------
        y_axis_mode : bool, default True
            The initial Y-axis mode. True for zero-anchored, False for auto-scaled.
        """
        super().__init__(
            options=[("Zero-anchored", True), ("Auto-scale", False)],
            description="Y-axis:",
            layout=widgets.Layout(border="1px solid #888", padding="4px 6px"),
        )
        self.value = y_axis_mode

    def _on_value_change(self, change):
        self.label = "Zero-anchored" if self.value else "Auto-scale"


class LogLinearModePicker(widgets.ToggleButtons):
    """A toggle button widget for selecting log vs. linear scale."""
    value = _trait(traitlets.UseEnum(LogLinearMode))
    label = _trait(traitlets.Unicode("Linear"))
    def __init__(self, log_linear_mode: LogLinearMode = LogLinearMode.LINEAR):
        """
        Initialize the log-linear mode picker.

        Parameters
        ----------
        log_linear_mode : LogLinearMode, default LogLinearMode.LINEAR
            The initial log-linear mode. Can be either log or linear.
        """
        super().__init__(
            options=[("Log", LogLinearMode.LOG),
                     ("Linear", LogLinearMode.LINEAR)],
            description="Scale:",
            layout=widgets.Layout(
                border="1px solid #888", padding="4px 6px", margin="0 20px 0 0",
            ),
        )
        self.value = log_linear_mode

    def _on_value_change(self, change):
        self.label = "Log" if self.value == LogLinearMode.LOG else "Linear"


_TOGGLE_LAYOUT = dict(border="1px solid #888", padding="4px 6px", margin="0 20px 0 0")


class DeltaScalePicker(widgets.ToggleButtons):
    """Sensitivity deltas in dollars or as % of the base (ADR-007 §4).
    Composes with ``DollarModePicker``: the two together give all four of
    nominal/CPI-constant x absolute/relative."""
    value = _trait(traitlets.UseEnum(DeltaScale))

    def __init__(self, delta_scale: DeltaScale = DeltaScale.ABSOLUTE):
        super().__init__(
            options=[("$ delta", DeltaScale.ABSOLUTE), ("% of base", DeltaScale.RELATIVE)],
            description="Delta:",
            layout=widgets.Layout(**_TOGGLE_LAYOUT),
        )
        self.value = delta_scale


class HeatmapValuePicker(widgets.ToggleButtons):
    """Sensitivity heatmap cells: each row's share of the year's total
    impact (the importance measure, which counts the base, so a row moving
    the outcome the same way at both ends still registers), or its signed
    swing, for direction (ADR-007 §6.1, §9)."""
    value = _trait(traitlets.UseEnum(HeatmapValue))

    def __init__(self, heatmap_value: HeatmapValue = HeatmapValue.SHARE):
        super().__init__(
            options=[("Share", HeatmapValue.SHARE), ("Signed swing", HeatmapValue.SWING)],
            description="Cells:",
            layout=widgets.Layout(**_TOGGLE_LAYOUT),
        )
        self.value = heatmap_value


class CorrelationMeasurePicker(widgets.ToggleButtons):
    """Which per-row measure a Monte Carlo batch's correlation chart plots
    (ADR-007 §6.2, §9): Spearman rho, the rank-regression coefficient
    (SRRC), or the partial rank correlation (PRCC), as named in
    ``sensitivity.CORRELATION_MEASURES``."""

    def __init__(self, by: str = "rho"):
        super().__init__(
            options=[("rho", "rho"), ("SRRC", "srrc"), ("PRCC", "prcc")],
            description="Measure:",
            layout=widgets.Layout(**_TOGGLE_LAYOUT),
        )
        self.value = by


class AttributionPicker(widgets.ToggleButtons):
    """How a sensitivity delta is decomposed (ADR-007 §6.1): by income/
    expense category, by income/expense account, or by holding account."""
    value = _trait(traitlets.UseEnum(AttributionAxis))

    def __init__(self, axis: AttributionAxis = AttributionAxis.CATEGORY):
        super().__init__(
            options=[("Category", AttributionAxis.CATEGORY),
                     ("Flow account", AttributionAxis.FLOW_ACCOUNT),
                     ("Holding account", AttributionAxis.HOLDING_ACCOUNT)],
            description="Attribute by:",
            layout=widgets.Layout(**_TOGGLE_LAYOUT),
        )
        self.value = axis


class ParameterPicker(widgets.Dropdown):
    """One of a sensitivity batch's varying rows, labeled with its target
    and perturbed range ("gift -- amount, $12,000 to $28,000")."""

    def __init__(self, parameters: dict[str, str] | None = None):
        """``parameters`` maps row name to its label suffix; see ``set_parameters``."""
        super().__init__(options=[], description="Parameter:",
                         layout=widgets.Layout(width="max-content"))
        self.set_parameters(parameters or {})

    def set_parameters(self, parameters: dict[str, str]) -> None:
        """Replace the options, keeping the selection when it's still offered
        and otherwise selecting the first -- never leaving the views that
        depend on it blank."""
        previous = self.value
        self.options = [(f"{name} — {label}" if label else name, name)
                        for name, label in parameters.items()]
        if previous in parameters:
            self.value = previous
        elif parameters:
            self.value = next(iter(parameters))


class ReportYearPicker(widgets.SelectionSlider):
    """One of a batch's annual reporting years (ADR-007 §4)."""

    def __init__(self, years: list[int] | None = None, value: int | None = None):
        years = years or [0]
        super().__init__(options=years, value=value if value in years else years[-1],
                         description="Year:", continuous_update=False,
                         layout=widgets.Layout(width="60%"))

    def set_years(self, years: list[int]) -> None:
        """Replace the options, keeping the selected year when it's still offered."""
        previous = self.value
        self.options = years
        self.value = previous if previous in years else years[-1]


class AccountKindPicker(widgets.Dropdown):
    """A dropdown widget for selecting account kind."""
    value: AccountKindAny = _trait(traitlets.Unicode("asset"))
    def __init__(self, kind: AccountKindAny = "asset", *,
                 allow_kind: Collection[AccountKind] | None = None,
                 allow_any: bool = False):
        """
        Initialize the account kind picker dropdown.

        Parameters
        ----------
        kind : AccountKindAny, default "asset" or first element of `allow_kind` if provided.
            The initially selected account kind.
        allow_kind : Collection[AccountKind] | None, optional
            A collection of allowed account kinds. If None, all kinds are allowed.
        allow_any : bool, default False
            Whether to include an "Any" option in the dropdown.
        """
        if kind == "any":
            allow_any = True
        if allow_kind is not None and kind not in allow_kind:
            kind = next(iter(allow_kind))
        options: list[tuple[str, AccountKindAny]] = [
            (label, k)
            for k, label in KIND_LABELS.items()
            if allow_kind is None or k in allow_kind
        ]
        if allow_any:
            options.append(("Any", "any"))
        super().__init__(
            options=options,
            value=kind,
            description="Kind:",
        )


def merged_lots_by_account(
    *connections: RunConnection,
) -> dict[str, list[str]]:
    """``lots_by_account()`` across several runs (scenarios being compared,
    say): each account's lots from every run that has them, in first-seen
    order, for the accounts with more than one lot among them all -- so
    a run's only lot of an account another run splits is offered too."""
    merged: dict[str, list[str]] = {}
    for connection in connections:
        for account, lots in connection.lots_by_account(single=True).items():
            seen = merged.setdefault(account, [])
            seen.extend(lot for lot in lots if lot not in seen)
    return {account: lots for account, lots in merged.items() if len(lots) > 1}


def to_markdown(*lines: str) -> widgets.Output:
    out = widgets.Output()
    with out:
        display(
            StyledMarkdown("\n".join(lines))
        )
    return out


ALL_LOTS = "All lots"


class LotPicker(widgets.HBox):
    """Drill one account down to its lots (#170).

    Shown only while it follows a single account with more than one lot (a
    lone ``default`` lot has nothing to drill into); hidden otherwise.
    ``lots`` is the picked lots, empty for the whole account. ``multiple``
    picks any number of lots, for views that draw several series; without
    it, a dropdown picks one lot or "All lots", for views that draw one.
    """
    lots: tuple[str, ...] = _trait(traitlets.Tuple())

    def __init__(self, lots_by_account: Mapping[str, Sequence[str]] | None = None, *,
                 multiple: bool = True):
        self.lots_by_account = dict(lots_by_account or {})
        self.account: str | None = None
        self._requested: str | None = None
        self._offered: list[str] = []
        self.picker: widgets.SelectMultiple | widgets.Dropdown = (
            widgets.SelectMultiple(description="Lots:", options=[], rows=4)
            if multiple else
            widgets.Dropdown(description="Lot:", options=[(ALL_LOTS, ())], value=())
        )
        super().__init__((self.picker,))
        self.layout.display = "none"
        self.picker.observe(self._on_pick, names="value")

    def follow(self, account: str | None) -> None:
        """Offer ``account``'s lots, or hide when it has fewer than two
        (``None`` for no single account). Picked lots stay picked while the
        account is unchanged and still has them."""
        self._requested = account
        lots = list(self.lots_by_account.get(account, ())) if account is not None else []
        if len(lots) < 2:
            account, lots = None, []
        if account == self.account and lots == self._offered:
            return
        keep = tuple(lot for lot in self.lots if lot in lots) if account == self.account else ()
        self.account, self._offered = account, lots
        self.layout.display = None if account is not None else "none"
        if isinstance(self.picker, widgets.SelectMultiple):
            self.picker.options = lots
            self.picker.value = keep
        else:
            self.picker.options = [(ALL_LOTS, ()), *((lot, (lot,)) for lot in lots)]
            self.picker.value = keep[:1]
        self.lots = tuple(self.picker.value)

    def set_lots(self, lots_by_account: Mapping[str, Sequence[str]]) -> None:
        """Offer a different set of lots (another recipe's, say), keeping
        the followed account and whatever picked lots it still has."""
        self.lots_by_account = dict(lots_by_account)
        self.follow(self._requested)

    def _on_pick(self, change: dict[str, Any]) -> None:
        self.lots = tuple(change["new"])


class AccountPicker(widgets.HBox):
    """
    A widget for selecting account kind and specific accounts, and -- given
    ``lots_by_account`` -- the lots of a single selected account (#170).

    ``selection`` is ``(accounts, lots)``, set once per change however many
    of the parts moved: observe it rather than the parts to redraw once.
    """
    rc: RunConnection | None = _trait(traitlets.Instance(RunConnection, allow_none=True))
    kind: AccountKindAny = _trait(traitlets.Any("asset"))
    accounts: list[AccountName] = _trait(traitlets.List())
    lots: tuple[str, ...] = _trait(traitlets.Tuple())
    selection: tuple[tuple[AccountName, ...], tuple[str, ...]] = _trait(traitlets.Tuple())
    kind_picker: AccountKindPicker
    lot_picker: LotPicker | None

    def __init__(self, rc: RunConnection | None = None,
                 accounts: Collection[AccountName] | None = None,
                 kind: AccountKindAny = "asset",
                 allow_kind: Collection[AccountKind] | None = None,
                 allow_any: bool = False,
                 # Keyed by Any: Mapping's key is invariant, so a mapping keyed
                 # by AccountKind (no "any") wouldn't match AccountKindAny.
                 accounts_by_kind: (Mapping[Any, Collection[AccountName]]
                                    | Callable[[AccountKindAny], Collection[AccountName]]
                                    | None) = None,
                 max_accounts: int | None = None,
                 initial_count: int | None = None,
                 lots_by_account: Mapping[str, Sequence[str]] | None = None,
                 lot_multiple: bool = True,
                 show_kind: bool = True):
        """
        Parameters
        ----------
        rc : RunConnection | None
            The run connection to fetch account data from, when
            ``accounts_by_kind`` doesn't say.
        accounts : Collection[AccountName] | None, optional
            The initially offered accounts when ``accounts_by_kind`` is None.
        kind : str, default "asset"
            The initially selected account kind.
        allow_kind : Collection[AccountKind] | None, optional
            A collection of allowed account kinds. If None, all kinds are allowed.
        allow_any : bool, default False
            Whether to include an "Any" option in the kind picker.
        accounts_by_kind : mapping or callable, optional
            The accounts each kind offers, as a mapping or a function of the
            kind. If None, plottable accounts are fetched from ``rc``.
        max_accounts : int | None, optional
            The maximum number of accounts that can be selected. If None, no limit is enforced.
        initial_count : int | None, optional
            How many of a kind's accounts start selected, initially and
            whenever the kind changes; default ``max_accounts`` or 1.
        lots_by_account : Mapping[str, Sequence[str]] | None, optional
            Each account's lots; given, a lot picker follows a single
            selected account that has more than one.
        lot_multiple : bool, default True
            Whether the lot picker picks any number of lots, or one lot or
            all of them.
        show_kind : bool, default True
            Whether to show the kind picker; hidden, ``kind`` stays fixed.
        """
        self.rc = rc
        self._accounts_by_kind = accounts_by_kind
        self._max_accounts = max_accounts
        self._initial_count = initial_count if initial_count is not None else (max_accounts or 1)
        self._settling = False
        self.kind_picker = AccountKindPicker(
            kind=kind,
            allow_kind=allow_kind,
            allow_any=allow_any,
        )
        # One account at a time is a dropdown, like the other single-account
        # pickers; its option values are 1-tuples, so ``value`` reads the same
        # as the multi-select's either way.
        self._single = max_accounts == 1
        self.account_picker: widgets.SelectMultiple | widgets.Dropdown = (
            widgets.Dropdown(description="Account:", layout=widgets.Layout(width="100%"))
            if self._single else
            widgets.SelectMultiple(description="Accounts:", layout=widgets.Layout(width="100%"))
        )
        self.lot_picker = (
            LotPicker(lots_by_account, multiple=lot_multiple)
            if lots_by_account is not None else None
        )
        # The lot picker sits under the accounts it drills into: narrower,
        # and it reads as belonging to them.
        self.account_column = widgets.VBox(
            [self.account_picker,
             *((self.lot_picker,) if self.lot_picker is not None else ())],
            # Boxes scroll on overflow, and a full-width dropdown's margins
            # overflow by a few pixels: let them, rather than show a scrollbar.
            layout=widgets.Layout(width="50%", overflow="visible"),
        )
        parts: list[widgets.Widget] = [self.kind_picker] if show_kind else []
        parts.append(self.account_column)
        super().__init__(parts)
        self._fill(cast(AccountKindAny, self.kind_picker.value), accounts)
        traitlets.link((self.kind_picker, "value"), (self, "kind"))
        self.kind_picker.observe(self._on_kind_change, names="value")
        self.account_picker.observe(self._on_accounts_change, names="value")
        if self.lot_picker is not None:
            self.lot_picker.observe(self._publish, names="lots")

    def _offered(self, kind: AccountKindAny,
                 accounts: Collection[AccountName] | None = None) -> list[AccountName]:
        if isinstance(self._accounts_by_kind, Mapping):
            return list(self._accounts_by_kind.get(kind, ()))
        if self._accounts_by_kind is not None:
            return list(self._accounts_by_kind(kind))
        if accounts is not None:
            return list(accounts)
        if self.rc is None:
            return []
        return plottable_accounts(self.rc.balance_pivot(kind), self.rc.account_kinds(), kind)

    def _fill(self, kind: AccountKindAny,
              accounts: Collection[AccountName] | None = None) -> None:
        """Offer ``kind``'s accounts with the first ``initial_count`` selected,
        then publish the whole selection once."""
        offered = self._offered(kind, accounts)
        self._settling = True
        try:
            if isinstance(self.account_picker, widgets.Dropdown):
                self.account_picker.options = [(name, (name,)) for name in offered]
                self.account_picker.value = (offered[0],) if offered else None
            else:
                self.account_picker.rows = min(15, max(5, len(offered)))
                self.account_picker.options = offered
                self.account_picker.value = tuple(offered[:self._initial_count])
        finally:
            self._settling = False
        self._publish()

    def _on_kind_change(self, change: dict[str, Any]) -> None:
        self.kind = change["new"]
        self._fill(change["new"])

    def _on_accounts_change(self, change: dict[str, Any]) -> None:
        if self._max_accounts is not None and len(change["new"]) > self._max_accounts:
            # Re-enters this handler with the trimmed value, which publishes.
            self.account_picker.value = tuple(change["new"][:self._max_accounts])
            return
        self._publish()

    def _publish(self, *_: object) -> None:
        if self._settling:
            return
        accounts = tuple(self.account_picker.value or ())
        self.accounts = list(accounts)
        if self.lot_picker is not None:
            self.lot_picker.follow(accounts[0] if len(accounts) == 1 else None)
        self.lots = self.lot_picker.lots if self.lot_picker is not None else ()
        self.selection = (accounts, self.lots)


class AccountDropdown(widgets.VBox):
    """One account from a dropdown, and -- given ``lots_by_account`` -- one of
    its lots or all of them (#170), under it.

    ``options`` are account names or ``(label, value)`` pairs, so a choice
    that isn't an account (net worth, say) can lead the list. ``selection``
    is ``(account, lots)``, set once per change: observe it to redraw once.
    """
    account: Any = _trait(traitlets.Any(None))
    lots: tuple[str, ...] = _trait(traitlets.Tuple())
    selection: tuple[Any, tuple[str, ...]] = _trait(traitlets.Tuple())
    lot_picker: LotPicker | None

    def __init__(self, options: Sequence[Any], value: Any = None, *,
                 lots_by_account: Mapping[str, Sequence[str]] | None = None,
                 description: str = "Account:"):
        self.account_picker = widgets.Dropdown(options=options, description=description)
        if value is not None and value in [
            o[1] if isinstance(o, tuple) else o for o in options
        ]:
            self.account_picker.value = value
        self.lot_picker = (
            LotPicker(lots_by_account, multiple=False) if lots_by_account is not None else None
        )
        super().__init__([self.account_picker,
                          *((self.lot_picker,) if self.lot_picker is not None else ())])
        self.account_picker.observe(self._publish, names="value")
        if self.lot_picker is not None:
            self.lot_picker.observe(self._publish, names="lots")
        self._publish()

    def _publish(self, *_: object) -> None:
        account = self.account_picker.value
        if self.lot_picker is not None:
            self.lot_picker.follow(account if isinstance(account, str) else None)
        self.account = account
        self.lots = self.lot_picker.lots if self.lot_picker is not None else ()
        self.selection = (account, self.lots)


class AssetClassPicker(widgets.HBox):
    """Widget to pick asset classes, with checkboxes for each class."""
    value: dict[str, bool] = _trait(traitlets.Dict())
    def __init__(self,
                 asset_classes: list[str],
                 exclude: tuple[str, ...] = ()):
        """
        Parameters
        ----------
        asset_classes : list[str]
            The list of all available asset classes.
        exclude : tuple[str, ...], optional
            Asset classes to exclude from the initial selection, by default ().
        """
        include = {
            name: widgets.Checkbox(value=name not in exclude, description=name, indent=False,
                                   layout=widgets.Layout(width="max-content"))
            for name in asset_classes
        }
        super().__init__([widgets.Label("Include:"), *include.values()],
                         layout=widgets.Layout(flex_flow="row wrap"))

        def _update_values(*_):
            self.value = {name: box.value for name, box in include.items()}
        for box in include.values():
            box.observe(_update_values, names="value")
        _update_values()


class GroupingPicker(widgets.Dropdown):
    """Dropdown to pick a grouping for portfolio allocation."""
    value: Grouping | None = _trait(traitlets.Enum(values=GROUPINGS + (_NO_SPLIT,)))

    def __init__(self, value: Grouping | None = None):
        """
        Parameters
        ----------
        value : Grouping | None, optional
            The initially selected grouping. If None, defaults to `_NO_SPLIT`.
        """
        super().__init__(
            options=[_NO_SPLIT, *GROUPINGS],
            value=value or _NO_SPLIT,
            description="Split by:",
        )


class DateRangeSlider(widgets.SelectionRangeSlider):
    """Widget to pick a date range, with the end date defaulting to the last day of the month."""
    start: datetime.date
    end: datetime.date
    months: int
    def __init__(self,
                 start: datetime.date | None = None,
                 end: datetime.date | None = None,
                 months: int = 12,
                 layout: widgets.Layout | None = None,
                 description: str = "Date range:",
                 value: tuple[datetime.date, datetime.date] | None = None,
                 index: tuple[int, int] | None = None,
                 disabled: bool = False,
                 ):
        """
        Parameters
        ----------
        start : datetime.date | None, optional
            The start date of the range. Defaults to the current month if None.
        end : datetime.date | None, optional
            The end date of the range. Defaults to 12 months after the start if None.
        months : int, default 12
            The number of months to include in the range.
        layout : widgets.Layout | None, optional
            The layout for the widget. Defaults to None.
        description : str, default "Date range:"
            The description for the widget.
        value : tuple[datetime.date, datetime.date] | None, optional
            The initially selected date range. Defaults to (start, end) if None.
        index : tuple[int, int] | None, optional
            The initially selected index range. Defaults to (0, months) if None.
        disabled : bool, default False
            Whether the widget is disabled.
        """
        start = start or this_month()
        end = end or this_month() + relativedelta.relativedelta(months=months)
        self.start = start
        self.end = end
        months = (end.year - start.year) * 12 + (end.month - start.month)
        self.months = months
        dates = [m for m in (start + relativedelta.relativedelta(months=i) for i in range(months + 1))]
        super().__init__(value=value or (start, end),
                         index=index or (value and (dates.index(value[0]), dates.index(value[1])) or (0, months)),
                         description=description,
                         options=[(d.strftime("%Y-%m"), d) for d in dates],
                         layout=layout or widgets.Layout(width="85%"),
                         disabled=disabled,
                         )

    

def run_years(*runs: RunConnection | None) -> list[int]:
    """The calendar years the given runs' months fall in, in order."""
    years: set[int] = set()
    for run in runs:
        if run is not None:
            years.update(int(year) for year in index_years(run.net_worth_frame()))
    return sorted(years)


class TimeRangePicker(widgets.IntRangeSlider):
    """The calendar years the graphs and tables show (#245).

    Build one per notebook and hand it to each widget as ``time_range=``, as
    with :class:`DollarModePicker`: every widget shows the same slider, and a
    change anywhere redraws them all. Each widget calls :meth:`cover` with
    the years of the run(s) it shows, which sets the slider's ends; a widget
    cuts what it draws with :meth:`clip`.

    It starts at the whole run. Left there, it follows a scenario change to
    the new run's whole span, and :meth:`clip` cuts nothing -- not even rows
    dated outside the run, such as a year-to-date catch-up's prior year.
    """

    def __init__(self, first: int | None = None, last: int | None = None):
        first = datetime.date.today().year if first is None else first
        last = first if last is None else last
        super().__init__(
            min=first, max=last, value=(first, last), step=1,
            description="Years:", continuous_update=False,
            layout=widgets.Layout(width="60%"),
        )
        # Until the first cover() the ends are a placeholder, so it takes
        # whatever span it's given, rather than clamping to the placeholder.
        self._covered = first != last

    @property
    def is_full(self) -> bool:
        """Whether every year is selected."""
        return tuple(self.value) == (self.min, self.max)

    def cover(self, years: Iterable[int]) -> None:
        """Make the ends ``years``' first and last.

        A full selection stays full; a narrowed one keeps its years, clamped
        to the new span, unless none of them is left in it."""
        years = list(years)
        if not years:
            return
        first, last = min(years), max(years)
        if self._covered and (first, last) == (self.min, self.max):
            return
        low, high = self.value
        full = self.is_full or not self._covered
        self._covered = True
        # Widen first: the slider refuses a min above its max (and the reverse).
        self.max = max(self.max, last)
        self.min = min(self.min, first)
        if full or high < first or low > last:
            low, high = first, last
        self.value = (max(low, first), min(high, last))
        self.min, self.max = first, last

    def clip(self, data: pd.DataFrame | pd.Series) -> Any:
        """The rows of ``data`` in the selected years (``frames.clip_years``:
        by month or by year). A full selection returns ``data`` itself."""
        return data if self.is_full else clip_years(data, self.value)


def controls_row(*controls: widgets.Widget) -> widgets.HBox:
    """Shared controls laid out side by side, wrapping when the cell is narrow."""
    return widgets.HBox(list(controls), layout=widgets.Layout(flex_flow="row wrap",
                                                              align_items="center"))
