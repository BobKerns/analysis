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

import ipywidgets as widgets
import traitlets
from IPython.display import clear_output, display

from analysis.sim.connection import RunConnection, ScenarioConnection
from analysis.sim.loader import ScenarioRef, list_scenario_names, resolve_scenario_name


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
        Return an output widget that displays the result of calling `fn` with the current run connection.
        Parameters
        ----------
        fn : callable
            The function to call with the current run connection.
        **kwargs
            Additional keyword arguments to pass to `fn`.
        
        """
        out = widgets.Output()
        def _redraw(*_):
            with out:
                clear_output(wait=True)
                result = self._invoke(fn, *args, **kwargs)
                if result is not None:
                    display(result)
        self.observe(_redraw, names="value")
        _redraw()
        return out

    def show_scenario(self, fn: callable, *args, **kwargs) -> widgets.Output:
        """
        Return an output widget that displays the result of calling `fn` with the current scenario connection.
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

    value: str = traitlets.Any()
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
    data_dir: Path, name: str, *, require_run: bool,
) -> SingleScenarioConnections:
    ref = resolve_scenario_name(data_dir, name)
    if require_run and ref.run_path is None:
        raise FileNotFoundError(
            f"no runs for {name!r} in {ref.runs_dir}; run "
            f"`analysis sim load {name} && analysis sim run {name}`"
        )
    scenario = ScenarioConnection(data_dir, name)
    run = RunConnection(scenario,ref.run_path) if ref.run_path is not None else None
    return SingleScenarioConnections(name, ref, scenario, run)

_META_COLS = [
    "scenario_name", "status", "started_at", "finished_at",
    "code_version", "code_dirty", "data_version", "data_dirty",
    "data_mode", "run_by",
]

def scenario_picker(
    data_dir: Path | str | None = None, default: str | None = None, *,
    require_run: bool = True,
    show_metadata: bool = False,
):
    """A ``Scenario:`` dropdown that keeps a live connection pair in sync.

    Returns ``(widget, container)``; ``container`` is a
    :class:`SingleScenarioConnections`, refreshed in place (closing the
    previous connections) whenever the dropdown changes.
    """

    REPO = Path.cwd().parent if Path.cwd().name == "notebooks" else Path.cwd()
    if data_dir is None:
        data_dir = Path(os.environ.get("ANALYSIS_DATA_DIR", REPO / "data/shared/sim"))
    else:
        data_dir = Path(data_dir)
    if default is None:
    # Same resolution the CLI uses: $ANALYSIS_SCENARIO, else the data repo's
    # .current_scenario, else "current".
        default = resolve_scenario_name(
            data_dir, os.environ.get("ANALYSIS_SCENARIO") or None
        ).name
    options = list_scenario_names(data_dir)
    dropdown = widgets.Dropdown(options=options, value=default, description="Scenario:")
    ref_out = widgets.Output()

    container = _open_single(data_dir, default, require_run=require_run)
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
        new = _open_single(data_dir, change["new"], require_run=require_run)
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

    return vbox, container


# ---------- multi-scenario (baseline + variants) picker ----------


@dataclass
class MultiScenarioConnections(Connections):
    """Currently selected baseline + variants, with their open run connections.

    ``connections`` only holds scenarios that resolved to a run;
    ``warnings`` explains anything dropped. ``ok`` is False when there's
    nothing sensible to render (e.g. the baseline itself has no run).
    """

    connections: dict[str, RunConnection]
    baseline: str
    variants: list[str]
    colors: dict[str, str]
    warnings: list[str] = field(default_factory=list)
    value: object = traitlets.Any()

    @property
    def scenarios(self) -> list[str]:
        return [self.baseline, *self.variants] if self.baseline else []

    @property
    def ok(self) -> bool:
        return bool(self.baseline)

    def _invoke(self, fn: callable, **kwargs) -> widgets.Widget:
        return fn(self, **kwargs)


def _load_multi(
    data_dir: Path, baseline: str, variants: list[str], max_variants: int,
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
        scenario = _open_single(data_dir, name, require_run=True)
        connections[name] = RunConnection(scenario, ref.run_path)

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
    data_dir: Path | str,
    default_baseline: str,
    default_variants: list[str],
    max_variants: int = 4,
):
    """Baseline dropdown + variant multi-select, backed by live run connections.

    Returns ``(widget, container)``; ``container`` is a
    :class:`MultiScenarioConnections`, refreshed in place (closing dropped
    connections) whenever either picker changes.
    """

    data_dir = Path(data_dir)
    available = list_scenario_names(data_dir)

    baseline_picker = widgets.Dropdown(
        options=available, value=default_baseline, description="Baseline:",
    )
    variant_picker = widgets.SelectMultiple(
        options=[name for name in available if name != default_baseline],
        value=tuple(default_variants),
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
            data_dir, baseline_picker.value, list(variant_picker.value), max_variants,
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


def dollar_plot_mode() -> widgets.ToggleButtons:
    """Return a toggle button widget for selecting nominal vs. CPI-constant dollars."""
    return widgets.ToggleButtons(
        options=[("Nominal", "nominal"), ("CPI-constant", "cpi_constant")],
        value="nominal",
        description="Dollar basis:",
        layout=widgets.Layout(
            border="1px solid #888", padding="4px 6px", margin="0 20px 0 0",
        ),
    )

def value_plot_mode() -> widgets.ToggleButtons:
    """Return a toggle button for selecting total vs. monthly values."""
    return widgets.ToggleButtons(
        options=[("Total", "total"), ("Monthly", "monthly")],
        value="total",
        description="Values:",
        layout=widgets.Layout(
            border="1px solid #888", padding="4px 6px", margin="0 20px 0 0",
        ),
    )

def y_axis_plot_mode() -> widgets.ToggleButtons:
    """Return a toggle button for selecting zero-anchored vs. auto-scaled Y-axis."""
    return widgets.ToggleButtons(
        options=[("Zero-anchored", True), ("Auto-scale", False)],
        value=True,
        description="Y-axis:",
        layout=widgets.Layout(border="1px solid #888", padding="4px 6px"),
    )
