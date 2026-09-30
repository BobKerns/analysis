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
import html as html_lib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Collection, Mapping
import calendar
import datetime
from dateutil import relativedelta

import ipywidgets as widgets
import traitlets
from IPython.display import Markdown, clear_output, display

from analysis.sim.accounts import KIND_LABELS
from analysis.sim.allocation import GROUPINGS, Grouping, _NO_SPLIT
from analysis.sim.connection import RunConnection, ScenarioConnection
from analysis.sim.notebook_ui.first_draw import when_cell_is_done
from analysis.sim.loader import ScenarioRef, list_scenario_names, resolve_scenario_name
from analysis.sim.accounts import plottable_accounts
from analysis.sim.registries import AssetClass
from analysis.sim.types import (
    AccountKind, AccountKindAny, DollarMode, ValuePlotMode,
    AccountName, ScenarioName,
)


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
    HTML instead, with an explicit light-yellow background and dark text.
    """
    # Escape first so literal "<...>" in messages (e.g. "sim load <name>")
    # isn't swallowed as an HTML tag; **bold**/`code` add real tags after.
    text = html_lib.escape(message)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    return (
        '<div style="background-color:#fff8c5; color:#111; padding:6px 10px; '
        'border-left:4px solid #e6b800; border-radius:3px; margin:4px 0;">'
        f"⚠️ {text}</div>"
    )


def display_warning(message: str) -> None:
    """Display a warning with a legible background (see `warning_html`)."""
    from IPython.display import HTML, display

    display(HTML(warning_html(message)))

class Connections(traitlets.HasTraits):
    """
    Holds the currently open scenario and run connections.
    This class is intended to be used as a singleton in notebooks, where
    it is mutated in place whenever the scenario picker changes.

    This is the base class for SingleScenarioConnections and MultiScenarioConnections.
    """

    @abstractmethod
    def _invoke(self, fn: callable, *args, **kwargs):
        """Invoke the given function with the current connections context."""
        raise NotImplementedError()

    def show(self, fn: callable, *args, **kwargs) -> widgets.Output:
        """
        Return an output widget that displays the result of calling `fn`
        with the current run connection.

        Parameters
        ----------
        fn : callable
            The function to call with the current run connection.
        **kwargs
            Additional keyword arguments to pass to `fn`.
        
        """
        out = widgets.Output()
        def _draw():
            with out:
                clear_output(wait=True)
                result = self._invoke(fn, *args, **kwargs)
                if result is not None:
                    display(result)

        def _redraw(*_):
            # The first draw waits for the end of the cell that called show(), so it
            # happens like any later redraw (see notebook_ui.first_draw).
            when_cell_is_done(_draw)
        self.observe(_redraw, names="value")
        _redraw()
        return out

    def show_scenario(self, fn: callable, *args, **kwargs) -> widgets.Output:
        """
        Return an output widget that displays the result of calling `fn`
        with the current scenario connection.

        Parameters
        ----------
        fn : callable
            The function to call with the current scenario connection.
        **kwargs
            Additional keyword arguments to pass to `fn`.
        
        """
        return self.show(lambda _, *args, **kwargs: fn(self.scenario, *args, **kwargs), *args, **kwargs)

# ---------- single-scenario picker ----------


@dataclass
class SingleScenarioConnections(Connections):
    """Currently selected scenario, with its open connections.

    ``run`` is ``None`` only when the picker was built with
    ``require_run=False`` and the scenario has no run yet.
    """

    value: ScenarioName = traitlets.Any()
    scenario_ref: ScenarioRef = traitlets.Any()
    scenario: ScenarioConnection = traitlets.Any()
    run: RunConnection | None = traitlets.Any()

    def close(self) -> None:
        self.scenario.close()
        if self.run is not None:
            self.run.close()

    def _invoke(self, fn: callable, *args, **kwargs):
        """Invoke the given function with the current connections context."""
        if self.run is None:
            raise RuntimeError("No run connection available")
        return fn(self.run, *args, **kwargs)


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

def find_data_dir(data_dir: Path | str | None = None) -> Path:
    REPO = Path.cwd().parent if Path.cwd().name == "notebooks" else Path.cwd()
    if data_dir is None:
        return Path(os.environ.get("ANALYSIS_DATA_DIR", REPO / "data/shared/sim"))
    return Path(data_dir)


def scenario_picker(
    data_dir: Path | str | None = None,
    default: str | None = None, *,
    scenarios_dir: Path | None = None,
    runs_dir: Path | None = None,
    require_run: bool = True,
    show_metadata: bool = False,
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
    options = list_scenario_names(data_dir, scenarios_dir=scenarios_dir)
    dropdown = widgets.Dropdown(options=options, value=default, description="Scenario:")
    ref_out = widgets.Output()

    container = _open_single(data_dir, default,
                             runs_dir=runs_dir,
                             require_run=require_run,
                             scenarios_dir=scenarios_dir)
    vbox = widgets.VBox([dropdown, ref_out])

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

    def _on_change(change) -> None:
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
        _redraw()

    dropdown.observe(_on_change, names="value")
    traitlets.link((dropdown, "value"), (container, "value"))
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
    value: object = traitlets.Any()

    @property
    def scenarios(self) -> list[ScenarioName]:
        return [self.baseline, *self.variants] if self.baseline else []

    @property
    def ok(self) -> bool:
        return bool(self.baseline)

    def _invoke(self, fn: callable, **kwargs) -> widgets.Widget:
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
        REPO = Path.cwd().parent if Path.cwd().name == "notebooks" else Path.cwd()
        data_dir = Path(os.environ.get("ANALYSIS_DATA_DIR", REPO / "data" / "shared" / "sim"))  
    data_dir = Path(data_dir)
    available = list_scenario_names(data_dir)

    if default_baseline is None:
        # Same resolution the CLI uses: $ANALYSIS_SCENARIO, else the data repo's
        # .current_scenario, else "current".
        default_baseline = resolve_scenario_name(
            data_dir, os.environ.get("ANALYSIS_SCENARIO") or None,
            intent_dir=intent_dir, runs_dir=runs_dir,
        ).name

    baseline_picker = widgets.Dropdown(
        options=available, value=default_baseline, description="Baseline:",
    )
    variant_picker = widgets.SelectMultiple(
        options=[name for name in available if name != default_baseline],
        value=tuple(default_variants
                    or (*[name
                          for name in available
                          if name != default_baseline][:max_variants],)),
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
        container.value = object()

    baseline_picker.observe(_on_change, names="value")
    variant_picker.observe(_on_change, names="value")
    _redraw()

    picker_widget = widgets.VBox(
        [widgets.VBox([baseline_picker, variant_picker]), warnings_out]
    )
    return picker_widget, container


class DollarModePicker(widgets.ToggleButtons):
    """A toggle button widget for selecting nominal vs. CPI-constant dollars."""
    value: DollarMode = traitlets.UseEnum(DollarMode)
    label: str = traitlets.Unicode("Nominal")
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


class ValuePlotModePicker(widgets.ToggleButtons):
    """A toggle button widget for selecting total vs. monthly values."""
    value: ValuePlotMode = traitlets.UseEnum(ValuePlotMode)
    label: str = traitlets.Unicode("Total")
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
    value: bool = traitlets.Bool(True)
    label: str = traitlets.Unicode("Zero-anchored")
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


class AccountKindPicker(widgets.Dropdown):
    """A dropdown widget for selecting account kind."""
    value: AccountKindAny = traitlets.Unicode("asset")
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
        options = [
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


def to_markdown(*lines: str) -> widgets.Output:
    out = widgets.Output()
    with out:
        display(
            Markdown("\n".join(lines))
        )
    return out


class AccountPicker(widgets.HBox):
    """
    A widget for selecting account kind and specific accounts.
    """
    rc: RunConnection = traitlets.Instance(RunConnection)
    kind: AccountKind = traitlets.Any("asset")
    accounts: list[AccountName] = traitlets.List()
    kind_picker: AccountKindPicker
    def __init__(self, rc: RunConnection,
                 accounts: list[AccountName] | None = None,
                 kind: str = "asset",
                 allow_kind: Collection[AccountKind] | None = None,
                 allow_any: bool = False,
                 accounts_by_kind: Mapping[AccountKind, Collection[AccountName]] | None = None,
                 max_accounts: int | None = None):
        """
        Parameters
        ----------
        rc : RunConnection
            The run connection to fetch account data from.
        accounts : list[AccountName] | None, optional
            The initially selected accounts. If None, no accounts are selected initially.
        kind : str, default "asset"
            The initially selected account kind.
        allow_kind : Collection[AccountKind] | None, optional
            A collection of allowed account kinds. If None, all kinds are allowed.
        allow_any : bool, default False
            Whether to include an "Any" option in the kind picker.
        accounts_by_kind : Mapping[AccountKind, Collection[AccountName]] | None, optional
            Precomputed mapping of account kinds to account names.
            If None, accounts are fetched from the run connection.
        max_accounts : int | None, optional
            The maximum number of accounts that can be selected. If None, no limit is enforced.
        """
        self.rc = rc
        self._accounts_by_kind = accounts_by_kind
        self._max_accounts = max_accounts
        self.kind_picker = AccountKindPicker(
            kind=kind,
            allow_kind=allow_kind,
            allow_any=allow_any,
        )
        initial_accounts = (
            list(accounts_by_kind.get(kind, ()))
            if accounts_by_kind is not None
            else list(accounts or ())
        )
        self.account_picker = widgets.SelectMultiple(
            description="Accounts:",
            layout=widgets.Layout(width="50%"),
            value=tuple(initial_accounts[:max_accounts]) if max_accounts else tuple(initial_accounts[:1]),
            options=initial_accounts,
        )
        super().__init__((self.kind_picker, self.account_picker))
        traitlets.link((self.kind_picker, "value"), (self, "kind"))
        traitlets.link((self.account_picker, "value"), (self, "accounts"))
        self.kind_picker.observe(self._on_kind_change, names="value")
        self.account_picker.observe(self._on_accounts_change, names="value")

    def _on_kind_change(self, change):
        self.kind = change["new"]
        if self._accounts_by_kind is not None:
            plottable = list(self._accounts_by_kind.get(change["new"], ()))
        else:
            plottable = plottable_accounts(
                self.rc.balance_pivot(change["new"]), self.rc.account_kinds(), change["new"],
            )
        self.account_picker.rows = min(15, max(5, len(plottable)))
        self.account_picker.options = plottable
        count = self._max_accounts or len(plottable)
        self.account_picker.value = tuple(plottable[:count])

    def _on_accounts_change(self, change):
        if self._max_accounts is not None and len(change["new"]) > self._max_accounts:
            self.account_picker.value = tuple(change["new"][:self._max_accounts])


class AssetClassPicker(widgets.HBox):
    """Widget to pick asset classes, with checkboxes for each class."""
    value: dict[AssetClass, bool] = traitlets.Dict()
    def __init__(self,
                 asset_classes: list[AssetClass],
                 exclude: tuple[AssetClass, ...] = ()):
        """
        Parameters
        ----------
        asset_classes : list[AssetClass]
            The list of all available asset classes.
        exclude : tuple[AssetClass, ...], optional
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
    value: Grouping | None = traitlets.Enum(values=GROUPINGS + (_NO_SPLIT,))

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

    