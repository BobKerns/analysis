"""Scenario picker widget and loading logic for the scenario_compare notebook.

Keeps ipywidgets plumbing and scenario resolution out of the notebook so it
reads as analysis, not setup code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from analysis.sim.connection import RunConnection
from analysis.sim.loader import ScenarioRef, resolve_scenario_name


def available_scenarios(data_dir: Path) -> list[str]:
    """Scenario folder names under data_dir/scenarios, sorted."""
    scenarios_dir = data_dir / "scenarios"
    if not scenarios_dir.is_dir():
        raise FileNotFoundError(f"no scenarios/ found under {data_dir}")
    return sorted(p.name for p in scenarios_dir.iterdir() if p.is_dir())


@dataclass
class ScenarioLoadResult:
    """Outcome of resolving a baseline + variants to their latest runs."""

    scenarios: list[str]
    baseline: str
    variants: list[str]
    colors: dict[str, str]
    refs: dict[str, ScenarioRef]
    runs: dict[str, RunConnection]
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.baseline)


def load_scenarios(
    data_dir: Path, baseline: str, variants: list[str], max_variants: int = 4
) -> ScenarioLoadResult:
    """Resolve a baseline + variant scenarios to their latest runs.

    Scenarios without a run are dropped with a warning; if the baseline itself
    has no run, the result is empty (``ok`` is False).
    """
    empty = ScenarioLoadResult([], "", [], {}, {}, {})

    if not variants:
        empty.warnings.append("Select at least one variant to compare.")
        return empty
    if len(variants) > max_variants:
        empty.warnings.append(f"Pick at most {max_variants} variants (selected {len(variants)}).")
        return empty

    refs: dict[str, ScenarioRef] = {}
    runs: dict[str, RunConnection] = {}
    missing = []
    for name in [baseline, *variants]:
        ref = resolve_scenario_name(data_dir, name)
        if ref.run_path is None:
            missing.append(name)
            continue
        refs[name], runs[name] = ref, RunConnection(ref.run_path)

    if baseline not in refs:
        return ScenarioLoadResult([], "", [], {}, refs, runs, [
            f"No run for baseline `{baseline}` — run "
            f"`analysis sim load {baseline} && analysis sim run {baseline}`."
        ])

    resolved_variants = [name for name in variants if name in refs]
    scenarios = [baseline, *resolved_variants]

    import matplotlib.pyplot as plt

    colors = dict(zip(scenarios, plt.rcParams["axes.prop_cycle"].by_key()["color"]))

    warnings = []
    if missing:
        warnings.append(
            "No run for: " + ", ".join(f"`{name}`" for name in missing)
            + " (excluded) — run `analysis sim load <name> && analysis sim run <name>`."
        )
    return ScenarioLoadResult(scenarios, baseline, resolved_variants, colors, refs, runs, warnings)


def scenario_picker_widgets(available: list[str], default_baseline: str, default_variants: list[str]):
    """Build the baseline dropdown, variant multi-select, apply button, and output area."""
    import ipywidgets as widgets

    baseline_picker = widgets.Dropdown(
        options=available, value=default_baseline, description="Baseline:",
    )
    variant_picker = widgets.SelectMultiple(
        options=[name for name in available if name != default_baseline],
        value=tuple(default_variants),
        description="Variants:",
        rows=min(len(available), 8),
    )
    apply_button = widgets.Button(description="Load scenarios", button_style="primary")
    status_output = widgets.Output()

    def _sync_variant_options(*_):
        variant_picker.options = [name for name in available if name != baseline_picker.value]
        variant_picker.value = tuple(v for v in variant_picker.value if v != baseline_picker.value)

    baseline_picker.observe(_sync_variant_options, names="value")

    return baseline_picker, variant_picker, apply_button, status_output
