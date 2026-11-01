"""Sensitivity notebook widgets (ADR-007 §9): which of a recipe's varying
rows matter, and when.

For a sensitivity (OAT) batch: an overview heatmap (parameters x reporting
years), a tornado at one year, each parameter's delta over time, an exact
attribution of a delta, and -- for a quantile ladder -- a response curve.
For a Monte Carlo batch: R^2 by year and each row's rho, SRRC or PRCC
with its 95% interval.

Each chart is a ``*_view`` function in ``analysis.sim.sensitivity_charts``,
shared with ``sim ensemble sensitivity --chart``; ``sensitivity_widget`` wires
them to shared pickers. A picker shown next to more than one chart is one
widget displayed twice, so the views stay in sync.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import ipywidgets as widgets
import pandas as pd
from IPython.display import display

from analysis.sim import sensitivity
from analysis.sim.ensemble import EnsembleError, read_batch_manifest
from analysis.sim.loader import batch_mode, read_disabled_scenarios
from analysis.sim.notebook_ui.pickers import (
    AttributionPicker,
    CorrelationMeasurePicker,
    DeltaScalePicker,
    DollarModePicker,
    HeatmapValuePicker,
    ParameterPicker,
    ReportYearPicker,
    find_data_dir,
    find_runs_dir,
    note_picks,
)
from analysis.sim.notebook_ui.styled import StyledMarkdown, show_figure
from analysis.sim.sensitivity_charts import (
    attribution_view, correlation_title, correlation_view, delta_over_time_view, heatmap_view, r2_view,
    response_curve_view, signed_dollars, tornado_view,
)
from analysis.sim.types import DeltaScale, DollarMode

__all__ = [
    "list_sensitivity_recipes", "list_batches", "parameter_labels", "parameters_note",
    "sensitivity_widget",
]


def list_sensitivity_recipes(runs_dir: Path, data_dir: Path | None = None) -> list[str]:
    """Recipes with at least one generated batch of either mode; given
    ``data_dir``, less its disabled scenarios (#300). Listing never waits
    for a background build (#300); opening a batch does."""
    from analysis.sim.ensemble import latest_ensemble_batch

    runs_dir = Path(runs_dir)
    if not runs_dir.is_dir():
        return []
    disabled = read_disabled_scenarios(data_dir) if data_dir else frozenset()
    return sorted(p.name for p in runs_dir.iterdir()
                  if p.is_dir() and p.name not in disabled
                  and latest_ensemble_batch(p, wait=False) is not None)


def list_batches(recipe_dir: Path) -> list[tuple[str, Path]]:
    """``(label, batch dir)`` for every batch, newest first, labeled with its
    mode and member count."""
    recipe_dir = Path(recipe_dir)
    if not recipe_dir.is_dir():
        return []
    batches = []
    for p in sorted((p for p in recipe_dir.iterdir() if p.is_dir()), reverse=True):
        manifest = read_batch_manifest(p)
        members = manifest["member_count"] if manifest else len(list(p.glob("member_*.sqlite3")))
        if not members:
            continue
        label = "one-at-a-time" if batch_mode(p) == "sensitivity" else "Monte Carlo"
        batches.append((f"{p.name} ({label}, {members} members)", p))
    return batches


def parameter_labels(deltas: pd.DataFrame) -> dict[str, str]:
    """Row name -> "target, low to high" from an OAT batch's deltas, in the
    order rows first appear."""
    labels: dict[str, str] = {}
    for row, group in deltas.groupby("row", sort=False):
        by_q = group.drop_duplicates("quantile").sort_values("quantile")
        labels[str(row)] = (f"{by_q['target'].iloc[0]}, {by_q['value'].iloc[0]} "
                       f"to {by_q['value'].iloc[-1]}")
    return labels


def parameters_note(mode: str | None) -> str:
    """The explanation shown above a batch's parameters table."""
    note = (
        "Each varying row is a `calendar.csv` row with `vary_*` columns set: **affects** is "
        "what it acts on, **target** whether its date or amount varies, and **range**/"
        "**center**/**shape** come from its distribution; **moments** are what those settings "
        "imply. The center is the deterministic base case: member 0 resolves every row to it. "
        "A correlated row names its **correlate_with** parent and **rho**. Latent factors "
        "appear as their own rows with target **factor**: they have no distribution, so no "
        "range, center, shape or moments, and only a parent and rho if they have one."
    )
    if mode == "sensitivity":
        return note + (
            " Each other one-at-a-time member moves one row or latent factor to the "
            "**value@q** shown and holds the rest at their centers, with one exception: a "
            "latent factor (target **factor**, no distribution of its own; its value is a "
            "standard score) also carries every row under it to that row's likeliest value "
            "given the factor. The charts label it **(factor)**."
        )
    if mode == "monte_carlo":
        return note + (
            " Every other member draws every row at random; **drawn** is the range they "
            "actually sampled."
        )
    return note


def _section(title: str, controls: list[widgets.Widget], out: widgets.Output) -> widgets.VBox:
    return widgets.VBox([widgets.HTML(f"<h4>{title}</h4>"), widgets.HBox(controls), out])


def _show(out: widgets.Output, draw) -> None:
    out.clear_output(wait=True)
    with out:
        try:
            draw()
        except EnsembleError as exc:
            display(StyledMarkdown(f"_{exc}_"))


def sensitivity_widget(*,
                       data_dir: Path | str | None = None,
                       state_dir: Path | None = None,
                       runs_dir: Path | str | None = None,
                       dollar_mode: DollarModePicker | None = None,
                       negligible: float = sensitivity.DEFAULT_NEGLIGIBLE,
                       ) -> widgets.VBox:
    """Recipe and batch dropdowns, then the ADR-007 §9 views for that batch:
    the OAT views for a sensitivity batch, the correlation views for a Monte
    Carlo one."""
    runs_dir = find_runs_dir(data_dir=data_dir, state_dir=state_dir, runs_dir=runs_dir)
    recipe = widgets.Dropdown(options=list_sensitivity_recipes(runs_dir, find_data_dir(data_dir)),
                              description="Recipe:")
    batch = widgets.Dropdown(options=[], description="Batch:", layout=widgets.Layout(width="max-content"))
    dollar_picker = dollar_mode or DollarModePicker(DollarMode.CPI_CONSTANT)
    scale_picker = DeltaScalePicker()
    heatmap_picker = HeatmapValuePicker()
    attribution_picker = AttributionPicker()
    parameter_picker = ParameterPicker()
    year_picker = ReportYearPicker()
    by_picker = CorrelationMeasurePicker()
    body = widgets.VBox()
    cache: dict[tuple[Path, DollarMode], pd.DataFrame] = {}

    def _deltas() -> pd.DataFrame:
        key = (batch.value, dollar_picker.value)
        if key not in cache:
            cache[key] = sensitivity.oat_deltas(batch.value, dollar_picker.value)
        return cache[key]

    outs = {name: widgets.Output() for name in
            ("parameters", "heatmap", "tornado", "time", "attribution", "curve", "r2", "rho")}

    def _draw_heatmap(*_: object) -> None:
        def draw() -> None:
            grid = sensitivity.rank_by_year(
                _deltas(), relative=scale_picker.value == DeltaScale.RELATIVE,
                value=heatmap_picker.value.value,
            )
            ax = heatmap_view(grid, heatmap_picker.value, scale_picker.value)
            ax.set_title("Which parameter matters when", loc="left")
            show_figure(ax.figure)
        _show(outs["heatmap"], draw)

    def _draw_tornado(*_: object) -> None:
        def draw() -> None:
            table = sensitivity.oat_table(_deltas(), year_picker.value,
                                          relative=scale_picker.value == DeltaScale.RELATIVE)
            ax = tornado_view(table, scale_picker.value)
            ax.set_title(f"Effect on net worth in {year_picker.value}", loc="left")
            show_figure(ax.figure)
            fmt = "{:+.1%}" if scale_picker.value == DeltaScale.RELATIVE else signed_dollars
            display(table.style.format(
                {**{c: fmt for c in table.columns if c.startswith("delta@")},
                 "swing": fmt, "impact": fmt, "share": "{:.0%}"}, na_rep="-"))
        _show(outs["tornado"], draw)

    def _draw_time(*_: object) -> None:
        def draw() -> None:
            if not parameter_picker.value:
                return
            ax = delta_over_time_view(_deltas(), parameter_picker.value, scale_picker.value)
            ax.set_title(f"{parameter_picker.value}: change in net worth vs. the base", loc="left")
            show_figure(ax.figure)
        _show(outs["time"], draw)

    def _draw_attribution(*_: object) -> None:
        def draw() -> None:
            if not parameter_picker.value:
                return
            tables = sensitivity.oat_attribution(
                batch.value, parameter_picker.value, by=attribution_picker.value.value,
                dollars=dollar_picker.value,
            )
            deltas = _deltas()
            base = deltas.loc[(deltas["row"] == parameter_picker.value)
                              & (deltas["year"] == year_picker.value)
                              & (deltas["measure"] == "net_worth"), "base"]
            ax, numbers = attribution_view(
                tables, year_picker.value, scale=scale_picker.value,
                base=float(base.iloc[0]) if len(base) else None,
            )
            ax.set_title(f"{parameter_picker.value} in {year_picker.value}: where the change "
                         "comes from", loc="left")
            show_figure(ax.figure)
            relative = scale_picker.value == DeltaScale.RELATIVE
            display(numbers.style.format("{:+.2%}" if relative else signed_dollars))
        _show(outs["attribution"], draw)

    def _draw_curve(*_: object) -> None:
        def draw() -> None:
            if not parameter_picker.value:
                return
            ax = response_curve_view(_deltas(), parameter_picker.value, year_picker.value,
                                     scale_picker.value)
            ax.set_title(f"{parameter_picker.value} in {year_picker.value}: response across "
                         "its range", loc="left")
            show_figure(ax.figure)
        _show(outs["curve"], draw)

    def _draw_correlation(*_: object) -> None:
        def draw_r2() -> None:
            ax = r2_view(_correlation())
            ax.set_title("How much per-parameter effects explain, by year", loc="left")
            show_figure(ax.figure)

        def draw_rho() -> None:
            report = _report()
            table = report.table
            ax = correlation_view(table, year_picker.value, negligible, by=by_picker.value)
            ax.set_title(correlation_title(by_picker.value, year_picker.value), loc="left")
            show_figure(ax.figure)
            if report.correlated:
                display(StyledMarkdown(
                    "_This batch's rows are correlated, so a row's rank correlation includes its "
                    "correlates' effects. Rows are ranked and pinned by their rank-regression "
                    "coefficient (SRRC), which holds the others fixed._"
                ))
            if by_picker.value != report.by:
                # As in the CLI, the status doesn't follow the chart.
                never = (" PRCC is never pinned by: its square isn't a share of net worth's "
                         "variance." if by_picker.value == "prcc" else "")
                display(StyledMarkdown(
                    f"_The status below is by {report.by}, what this batch is ranked by.{never}_"
                ))
            status = sensitivity.classify(table, negligible, by=report.by)
            n = int(table["n"].iloc[0])
            needed = sensitivity.required_members(negligible)
            note = (f" -- {n} members can't call a row negligible at {negligible:.0%}; "
                    f"that needs about {needed}") if report.by == "rho" and n < needed else ""
            # As in the CLI: that count follows from rho's interval, and
            # SRRC's has no fixed one (it narrows as R^2 rises).
            display(StyledMarkdown(f"**{n} drawn members**{note}"))
            display(status.to_frame())
            if not report.scores.empty:
                display(StyledMarkdown(
                    "**Did the batch reproduce its correlations?** The correlation each "
                    "correlated pair achieved, estimated from its scores' rank agreement "
                    "(Kendall's tau), with its 95% interval, against the expected one "
                    "(**link**: the pair's own rho; **implied**: the product along "
                    "the path joining them). **outside** means the batch didn't reproduce it."
                ))
                display(report.scores.round(3))
        _show(outs["r2"], draw_r2)
        _show(outs["rho"], draw_rho)

    correlation_cache: dict[tuple[Path, DollarMode], sensitivity.McReport] = {}

    def _report() -> sensitivity.McReport:
        key = (batch.value, dollar_picker.value)
        if key not in correlation_cache:
            correlation_cache[key] = sensitivity.mc_report(batch.value, dollars=dollar_picker.value)
        return correlation_cache[key]

    def _correlation() -> pd.DataFrame:
        return _report().table

    oat_sections = {
        "heatmap": (_draw_heatmap, _section("Overview", [heatmap_picker], outs["heatmap"])),
        "tornado": (_draw_tornado, _section("At one year", [year_picker], outs["tornado"])),
        "time": (_draw_time, _section("Over time", [parameter_picker], outs["time"])),
        "attribution": (_draw_attribution, _section(
            "Why", [parameter_picker, year_picker, attribution_picker], outs["attribution"])),
        "curve": (_draw_curve, _section(
            "Across the range", [parameter_picker, year_picker], outs["curve"])),
    }
    parameters_section = _section("Parameters", [], outs["parameters"])

    def _draw_parameters() -> None:
        def draw() -> None:
            table = sensitivity.parameters(batch.value)
            if table.empty:
                display(StyledMarkdown("_This batch has no varying rows._"))
                return
            display(StyledMarkdown(parameters_note(state["mode"])))
            display(table)
        _show(outs["parameters"], draw)

    mc_sections = [
        _section("Can per-parameter numbers be trusted?", [], outs["r2"]),
        _section("Which rows could be pinned?", [year_picker, by_picker], outs["rho"]),
    ]
    state: dict[str, Any] = {"mode": None, "ladder": False, "loading": False}

    def _draw_all(*_: object) -> None:
        if state["mode"] == "sensitivity":
            for name, (draw, _box) in oat_sections.items():
                if name != "curve" or state["ladder"]:
                    draw()
        elif state["mode"] == "monte_carlo":
            _draw_correlation()

    def _on_batch(*_: object) -> None:
        if batch.value is None:
            body.children = []
            state["mode"] = None
            return
        state["mode"] = batch_mode(batch.value)
        try:
            if state["mode"] == "sensitivity":
                deltas = _deltas()
                manifest = read_batch_manifest(batch.value) or {}
                state["ladder"] = len(manifest.get("quantiles", [])) >= 4
                years = sorted(int(y) for y in deltas["year"].unique())
                # Repopulating the pickers fires their observers; the single
                # _draw_all below is the redraw, so suppress theirs meanwhile.
                state["loading"] = True
                try:
                    parameter_picker.set_parameters(parameter_labels(deltas))
                    year_picker.set_years(years)
                finally:
                    state["loading"] = False
                body.children = [parameters_section] + [
                    box for name, (_draw, box) in oat_sections.items()
                    if name != "curve" or state["ladder"]
                ]
            else:
                table = _correlation()
                state["loading"] = True
                try:
                    year_picker.set_years(sorted(int(y) for y in table["year"].unique()))
                    # Each batch opens on what it's ranked by.
                    by_picker.value = _report().by
                finally:
                    state["loading"] = False
                body.children = [parameters_section, *mc_sections]
        except EnsembleError as exc:
            message = widgets.Output()
            with message:
                display(StyledMarkdown(f"_{exc}_"))
            body.children = [message]
            state["mode"] = None
            return
        _draw_parameters()  # depends only on the batch, not on units or pickers
        _draw_all()

    def _on_recipe(*_: object) -> None:
        options = list_batches(runs_dir / recipe.value) if recipe.value else []
        batch.options = options
        batch.value = options[0][1] if options else None

    def _on_units(*_: object) -> None:
        _draw_all()

    def _on_year(*_: object) -> None:
        if state["loading"]:
            return
        if state["mode"] == "sensitivity":
            _draw_tornado()
            _draw_attribution()
            if state["ladder"]:
                _draw_curve()
        elif state["mode"] == "monte_carlo":
            _draw_correlation()

    def _on_by(*_: object) -> None:
        if not state["loading"] and state["mode"] == "monte_carlo":
            _draw_correlation()

    def _on_parameter(*_: object) -> None:
        if state["loading"]:
            return
        if state["mode"] == "sensitivity":
            _draw_time()
            _draw_attribution()
            if state["ladder"]:
                _draw_curve()

    # Fill options before observing, so setting them doesn't trigger a draw.
    _on_recipe()
    _on_batch()
    recipe.observe(_on_recipe, names="value")
    note_picks(find_data_dir(data_dir), recipe)
    batch.observe(_on_batch, names="value")
    dollar_picker.observe(_on_units, names="value")
    scale_picker.observe(_on_units, names="value")
    heatmap_picker.observe(_draw_heatmap, names="value")
    attribution_picker.observe(_draw_attribution, names="value")
    year_picker.observe(_on_year, names="value")
    parameter_picker.observe(_on_parameter, names="value")
    by_picker.observe(_on_by, names="value")
    return widgets.VBox([
        widgets.HBox([recipe, batch]),
        widgets.HBox([dollar_picker, scale_picker]),
        body,
    ])

