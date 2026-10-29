"""Scenario-compare notebook widgets: one function per section.

All take the :class:`~analysis.sim.notebook_ui.pickers.MultiScenarioConnections`
container plus the baseline/variant picker widgets, and redraw whenever
either picker changes (the container itself is already refreshed by then --
see :func:`analysis.sim.notebook_ui.pickers.multi_scenario_picker`).
"""

from __future__ import annotations

import json
from pathlib import Path
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING

import ipywidgets as widgets
import pandas as pd
from IPython.display import clear_output, display

from analysis.sim import compare
from analysis.sim.compare import breakeven_month
from analysis.sim.compare_charts import comparison_figure
from analysis.sim.connection import RunConnection
from analysis.sim.notebook_ui.pickers import (
    _TOGGLE_LAYOUT,
    AccountPicker,
    AfterTaxPicker,
    DollarModePicker,
    MultiScenarioConnections,
    TimeRangePicker,
    ValuePlotModePicker,
    controls_row,
    display_warning,
    observe_while_shown,
    merged_lots_by_account,
    run_years,
    to_markdown,
)
from analysis.sim.plotting import FRAME_ALPHA, include_zero, mark_deaths
from analysis.sim.types import AccountKind, Death, DollarMode, ValuePlotMode
from analysis.sim.notebook_ui.styled import StyledMarkdown, show_figure

if TYPE_CHECKING:
    from analysis.sim.guard_rails import RailResult

# ---------- data helpers (pure; unchanged math from the old scenario_compare.py) ----------


def net_worth_by_month(
    container: MultiScenarioConnections,
    mode: DollarMode = DollarMode.NOMINAL, *, after_tax: bool = False,
) -> pd.DataFrame:
    """Net worth (USD) by month, one column per scenario, after tax with
    ``after_tax``; see :func:`analysis.sim.compare.net_worth_by_month`."""
    return compare.net_worth_by_month(
        container.connections, container.scenarios, mode, after_tax=after_tax)


def variant_delta(nw: pd.DataFrame, container: MultiScenarioConnections) -> pd.DataFrame:
    """Each variant's net worth minus the baseline's, by month."""
    return compare.variant_delta(nw, container.baseline, container.variants)


def breakeven_summary(
    nw: pd.DataFrame, delta: pd.DataFrame, container: MultiScenarioConnections,
) -> pd.DataFrame:
    """Per-variant break-even month, deepest deficit, and end-of-run comparison."""
    return compare.breakeven_summary(nw, delta, container.baseline, container.variants)


def filter_comparisons(comparisons: dict, available: list[str]) -> dict:
    """Drop comparison pairs that reference a scenario that wasn't loaded."""
    return {
        group: {
            label: pair for label, pair in pairs.items() if set(pair) <= set(available)
        }
        for group, pairs in comparisons.items()
    }


def has_any_comparisons(comparisons: dict) -> bool:
    """True if at least one comparison pair survived `filter_comparisons`."""
    return any(pairs for pairs in comparisons.values())


def comparison_frame(nw: pd.DataFrame, comparisons: dict) -> pd.DataFrame:
    """One column per comparison label: nw[a] - nw[b].

    Keeps `nw`'s index even when there are no comparisons, so callers like
    `year_end_values` don't choke on a columnless frame's default RangeIndex.
    """
    result = pd.DataFrame(index=nw.index)
    for pairs in comparisons.values():
        for label, (a, b) in pairs.items():
            result[label] = nw[a] - nw[b]
    return result


def _time_range(
    time_range: TimeRangePicker | None, container: MultiScenarioConnections,
) -> TimeRangePicker:
    """``time_range`` (or a new one) spanning every compared run's years."""
    time_range = time_range or TimeRangePicker()
    time_range.cover(run_years(*container.connections.values()))
    return time_range


# ---------- pure plotting (unchanged math from the old scenario_compare.py) ----------


def plot_net_worth_and_delta(
    nw: pd.DataFrame, delta: pd.DataFrame, container: MultiScenarioConnections,
    mode: str = "nominal",
) -> None:
    """Two-panel chart: net worth per scenario, and each variant's delta vs
    baseline, as ``sim compare --graph`` draws it. ``delta`` is
    ``variant_delta(nw, container)``, which the chart recomputes."""
    del delta
    mode_label = "CPI-constant" if mode == "cpi_constant" else "nominal"
    figure = comparison_figure(
        nw, container.baseline, container.variants, f"Net worth, {mode_label}",
        colors=container.colors,
        deaths=compare.scenario_deaths(container.connections, container.scenarios),
    )
    show_figure(figure)


def plot_horizon_comparisons(compare: pd.DataFrame, comparisons: dict) -> None:
    """One panel per comparison group, with a break-even marker on each line."""
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, len(comparisons), figsize=(13, 5))
    if len(comparisons) == 1:
        axes = [axes]
    for ax, (group, pairs) in zip(axes, comparisons.items(), strict=True):
        for offset, label in enumerate(pairs):
            line, = ax.plot(compare.index, compare[label], label=label)
            month = breakeven_month(compare[label])
            if month is not None and compare[label].min() < 0:
                ax.axvline(month, color=line.get_color(), linestyle=":", alpha=0.7)
                ax.annotate(
                    f"break-even {month:%Y-%m}", xy=(month, 0),
                    xytext=(6, 10 + 16 * offset),  # stagger so labels don't collide
                    textcoords="offset points", color=line.get_color(), fontsize=9,
                )
        ax.axhline(0, color="grey", linewidth=0.8)
        ax.set_title(group)
        ax.set_xlabel("if the money stops mattering here…")
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:,.0f}"))
        ax.grid(True, alpha=0.3)
        ax.legend(framealpha=FRAME_ALPHA)
        include_zero(ax)
    axes[0].set_ylabel("difference in net worth")
    plt.tight_layout()
    plt.show()


def plot_ss_gross(
    ss: pd.DataFrame, ss_cumulative: pd.DataFrame, colors: dict[str, str],
    deaths: Sequence[Death] = (),
) -> None:
    """Monthly and cumulative gross Social Security, one line per scenario,
    with ``deaths`` as red lines (#255)."""
    import matplotlib.pyplot as plt

    fig, (ax_month, ax_cum) = plt.subplots(1, 2, figsize=(13, 4))
    for name in ss.columns:
        ax_month.plot(ss.index, ss[name], label=name, color=colors[name], alpha=0.85)
        ax_cum.plot(ss_cumulative.index, ss_cumulative[name], label=name, color=colors[name])
    ax_month.set_title("Monthly gross Social Security")
    ax_cum.set_title("Cumulative gross Social Security")
    for ax in (ax_month, ax_cum):
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:,.0f}"))
        ax.grid(True, axis="y", alpha=0.3)
        mark_deaths(ax, deaths)
        ax.legend(framealpha=FRAME_ALPHA)
        include_zero(ax)
    plt.tight_layout()
    plt.show()


# ---------- widget-builders ----------


_META_COLS = [
    "scenario_name", "status", "finished_at",
    "code_version", "code_dirty", "data_version", "data_dirty",
]


def comparability_widget(container: MultiScenarioConnections,
                         connections: dict[str, RunConnection] | None = None,
                         variants: Sequence[str] | None = None,
                         **_
                         ) -> None:
    """Whether the loaded runs share code/data version -- a comparison is only
    meaningful if a difference isn't just an unrelated code/data change."""

    runs = connections or container.connections
    variants = variants or list(runs.keys())

    def _redraw():
        meta, warnings = compare.comparability(runs, variants)
        display(meta.set_index("scenario_name").T)
        if warnings:
            for warning in warnings:
                display_warning(warning)
        else:
            display(StyledMarkdown(
                "> ✅ Same code and data version; differences below are the scenarios."
            ))

    if container.ok:
        _redraw()


def rules_and_events_widget(container: MultiScenarioConnections,
                            connections: Mapping[str, RunConnection],
                            scenarios: Sequence[str],
                            variants: Sequence[str],
                            baseline: str = "current",
                            **_
                            ) -> None:
    """What actually differs between scenarios: rules (as of run end) and calendar events."""

    def _redraw():
        differences = compare.rule_differences(connections, scenarios, baseline)
        if differences.empty:
            display_warning("**All scenarios have identical rules.**")
        else:
            display(differences.fillna("—"))
        if all(connections[name].items("calendar_event").empty for name in scenarios):
            display(StyledMarkdown("_No calendar events in any scenario._"))
            return
        events = compare.event_differences(connections, scenarios)
        if events.empty:
            display_warning("**All scenarios have identical calendar events.**")
        else:
            display(events.set_index(["scenario", "date"]))
        _show_strategy_differences(connections, scenarios, baseline)

    if container.ok:
        _redraw()


def _show_strategy_differences(
    connections: Mapping[str, RunConnection], scenarios: Sequence[str], baseline: str,
) -> None:
    """Strategies and target weights that differ (#359); nothing when no run has either."""
    if not compare.uses_strategies(connections):
        return
    strategies = compare.strategy_differences(connections, scenarios, baseline)
    if strategies.empty:
        display(StyledMarkdown("_All scenarios run the same strategies._"))
    else:
        display(StyledMarkdown("**Strategies** (— where a scenario doesn't run it)"))
        display(strategies.fillna("—"))
    targets = compare.target_differences(connections, scenarios, baseline)
    if not targets.empty:
        display(StyledMarkdown("**Target weights** (percent)"))
        display(targets.fillna("—"))


def strategy_compare_widget(
    container: MultiScenarioConnections,
    time_range: TimeRangePicker | None = None,
    **_,
) -> widgets.VBox:
    """A strategy on vs off, or two strategies (#359), as ``sim compare
    --section strategies`` shows them: each run's allocation drift against
    the same target set, a panel per asset class and one of the largest
    drift either way, a line per scenario; then what each scenario's
    strategies did over the run. Targets and portfolio are a strategy's in
    any of the runs (the baseline's first)."""
    from analysis.sim.drift import MAX_ABS, year_end_rows
    from analysis.sim.drift_charts import drift_compare_figure

    time_range = _time_range(time_range, container)
    output = widgets.Output()
    connections, scenarios = container.connections, container.scenarios

    def _redraw(*_):
        with output:
            clear_output(wait=True)
            if not compare.uses_strategies(connections):
                display(StyledMarkdown("_No strategies or target sets in these runs._"))
                return
            try:
                target_set, portfolio, band = compare.drift_targets(connections, scenarios)
            except ValueError as exc:
                display_warning(f"**No drift comparison:** {exc}")
                return
            frames = compare.drift_by_scenario(connections, scenarios, target_set, portfolio)
            whose = f"accounts tagged {portfolio}" if portfolio else "every asset lot"
            low, high = time_range.value
            fig = drift_compare_figure(
                frames, f"Drift from {target_set} ({whose}), percentage points", band=band,
                years=(low, high), deaths=compare.scenario_deaths(connections, scenarios))
            show_figure(fig)
            table = pd.DataFrame({name: year_end_rows(frame)[MAX_ABS] for name, frame in frames.items()})
            table = table[[low <= int(str(m)[:4]) <= high for m in table.index]]
            display(StyledMarkdown("**Largest drift either way at year end**, percentage points"
                                   + (f" (band ±{band:g})" if band is not None else "")))
            display(table.style.format("{:.1f}", na_rep="—"))
            activity = compare.strategy_activity(connections, scenarios)
            if not activity.empty:
                display(StyledMarkdown("**Strategies over the run**: dollars asked of their chains, "
                                       "moved by the strategy, and left to the chain's steps"))
                display(activity.style.format({"asked": "${:,.0f}", "moved": "${:,.0f}", "steps": "${:,.0f}"}))

    observe_while_shown(time_range, _redraw)
    if container.ok:
        _redraw()
    return widgets.VBox([controls_row(time_range), output])


def net_worth_compare_widget(
    container: MultiScenarioConnections,
    dollar_mode: DollarModePicker | None = None,
    time_range: TimeRangePicker | None = None,
    after_tax: AfterTaxPicker | None = None,
    **_
) -> widgets.VBox:
    """Net worth per scenario, plus each variant's delta vs. the baseline,
    pre-tax or after tax (#251)."""

    dollar_mode = dollar_mode or DollarModePicker()
    after_tax = after_tax or AfterTaxPicker()
    time_range = _time_range(time_range, container)
    output = widgets.Output()

    def _redraw(*_):
        mode = dollar_mode.value
        nw = time_range.clip(net_worth_by_month(container, mode, after_tax=after_tax.value))
        delta = variant_delta(nw, container)
        with output:
            clear_output(wait=True)
            plot_net_worth_and_delta(nw, delta, container, mode)

    observe_while_shown(dollar_mode, _redraw)
    observe_while_shown(after_tax, _redraw)
    observe_while_shown(time_range, _redraw)
    if container.ok:
        _redraw()
    return widgets.VBox([controls_row(dollar_mode, after_tax, time_range), output])


def breakeven_widget(
    container: MultiScenarioConnections,
    dollar_mode: DollarModePicker | None = None,
    baseline: str = "current",
    after_tax: AfterTaxPicker | None = None,
    **_
) -> widgets.VBox:
    """Per-variant break-even summary table, pre-tax or after tax (#251)."""

    dollar_mode = dollar_mode or DollarModePicker()
    after_tax = after_tax or AfterTaxPicker()
    output = widgets.Output()

    def _redraw(*_):
        mode = dollar_mode.value
        nw = net_worth_by_month(container, mode, after_tax=after_tax.value)
        delta = variant_delta(nw, container)
        summary = breakeven_summary(nw, delta, container)
        with output:
            clear_output(wait=True)
            display(
                summary.style.format({
                    "deepest deficit": "${:,.0f}",
                    "at scenario end": "${:,.0f}",
                    "final net worth": "${:,.0f}",
                })
            )
            if nw.empty:  # two empty scenarios have no months (#174)
                return
            mode_label = "CPI-constant" if mode == "cpi_constant" else "nominal"
            display(StyledMarkdown(
                f"`{baseline}` final net worth ({mode_label}): "
                f"**${nw[baseline].iloc[-1]:,.0f}**"
            ))

    observe_while_shown(dollar_mode, _redraw)
    observe_while_shown(after_tax, _redraw)
    if container.ok:
        _redraw()
    return widgets.VBox([controls_row(dollar_mode, after_tax), output])


def horizon_widget(container: MultiScenarioConnections, comparisons: dict) -> None:
    """Net-worth gap at every possible horizon, for a set of (scenario, counterfactual) pairs."""

    def _redraw():
        filtered = filter_comparisons(comparisons, container.scenarios)
        if not has_any_comparisons(filtered):
            display(StyledMarkdown(
                "_None of the configured `COMPARISONS` pairs apply to this selection "
                "— pick scenarios from `COMPARISONS`, or edit it above to add a pair "
                "for this selection._"
            ))
            return
        nw = net_worth_by_month(container)
        compare = comparison_frame(nw, filtered)
        plot_horizon_comparisons(compare, filtered)

    if container.ok:
        _redraw()


def year_end_widget(
    container: MultiScenarioConnections,
    dollar_mode: DollarModePicker | None = None,
    baseline: str | None = "current",
    time_range: TimeRangePicker | None = None,
    after_tax: AfterTaxPicker | None = None,
    **_
) -> widgets.VBox:
    """Year-end net worth per scenario, with each variant's delta vs. the
    baseline, pre-tax or after tax (#251)."""

    dollar_mode = dollar_mode or DollarModePicker()
    after_tax = after_tax or AfterTaxPicker()
    time_range = _time_range(time_range, container)
    output = widgets.Output()

    def _redraw(*_):
        mode = dollar_mode.value
        nw = time_range.clip(net_worth_by_month(container, mode, after_tax=after_tax.value))
        year_end = compare.year_end_comparison(nw, baseline or container.baseline, container.variants)
        with output:
            clear_output(wait=True)
            display(year_end.style.format("${:,.0f}"))

    observe_while_shown(dollar_mode, _redraw)
    observe_while_shown(after_tax, _redraw)
    observe_while_shown(time_range, _redraw)
    if container.ok:
        _redraw()
    return widgets.VBox([controls_row(dollar_mode, after_tax, time_range), output])


def guard_rails_compare_widget(
    container: MultiScenarioConnections,
    dollar_mode: DollarModePicker | None = None,
    time_range: TimeRangePicker | None = None,
    **_
) -> widgets.VBox:
    """Each scenario's guard rail margins side by side (#305), as ``sim
    compare --section guard-rails`` prints them: a chart of every rail's
    annual and cumulative margin, a line per scenario; per rail, a table of
    each scenario's margins by year and each variant's change; then each
    scenario's whole-run summary. Rails are each scenario's own, matched by
    name, and read when drawn, so an edited rail shows without a re-run.
    Margins sum over the whole run; the years picked only limit what is
    shown."""
    from analysis.sim import guard_rails
    from analysis.sim.guard_rail_charts import CARRIED_CAVEAT, guard_rail_comparison_figure

    dollar_mode = dollar_mode or DollarModePicker(DollarMode.CPI_CONSTANT)
    time_range = _time_range(time_range, container)
    output = widgets.Output()
    # Evaluated rails by (run file, rails, dollars): the rails are re-read at
    # each draw, so an edit shows, but a time-range change only re-clips.
    evaluated: dict[tuple[Path, tuple[guard_rails.GuardRail, ...], DollarMode],
                    list[guard_rails.RailResult]] = {}

    def _results(mode: DollarMode) -> dict[str, list[guard_rails.RailResult]] | None:
        """Each scenario's rails evaluated, or None, said why."""
        results: dict[str, list[guard_rails.RailResult]] = {}
        for name, rc in container.connections.items():
            try:
                scenarios_dir = rc.scenario.scenarios_dir
            except RuntimeError:  # a run file opened without its data repo
                display_warning(f"{name} was opened without its scenario, so its guard rails "
                                "can't be read.")
                return None
            try:
                rails = guard_rails.run_rails(rc, scenarios_dir)
            except guard_rails.GuardRailError as exc:
                display(StyledMarkdown(f"**The guard rails of {name} don't load**; "
                                       f"`sim check {name}` lists:\n\n"
                                       + "\n".join(f"- {issue}" for issue in exc.issues)))
                return None
            if rails and mode == DollarMode.CPI_CONSTANT and rc.cpi_index().isna().all():
                display_warning(f"No 'cpi' inflation index in the run of {name}, so no "
                                "CPI-constant margins; pick nominal dollars.")
                return None
            key = (rc.run_path, tuple(rails), mode)
            if key not in evaluated:
                evaluated[key] = guard_rails.evaluate_run(rc, rails, mode) if rails else []
            results[name] = evaluated[key]
        return results

    def _redraw(*_):
        mode = dollar_mode.value
        unit = "CPI-constant" if mode == DollarMode.CPI_CONSTANT else "nominal"
        baseline, variants = container.baseline, list(container.variants)
        with output:
            clear_output(wait=True)
            results = _results(mode)
            if results is None:
                return
            frames = guard_rails.comparison_frames(results, baseline, variants)
            if not frames:
                display(StyledMarkdown(
                    "_No guard rails in these scenarios; `sim guard-rail add` adds one._"))
                return
            low, high = time_range.value
            fig = guard_rail_comparison_figure(
                results, baseline, variants, "Guard rails", unit=unit,
                colors=container.colors, years=(low, high),
                deaths=compare.scenario_deaths(container.connections, container.scenarios))
            show_figure(fig)
            senses = _rail_senses(results, container.scenarios)
            for rail, frame in frames.items():
                frame = time_range.clip(frame)
                display(StyledMarkdown(f"**{rail}** ({senses[rail]})"))
                display(frame.style.format("${:,.0f}", na_rep="")
                        .apply(lambda col, rail=rail: [_past_color(v, str(col.name), senses[rail])
                                                       for v in col]))
            display(StyledMarkdown("**Over the whole run**"))
            display(guard_rails.comparison_summary_frame(results, container.scenarios)
                    .style.format({"worst": "${:,.0f}", "end": "${:,.0f}",
                                   "rms_over": "${:,.0f}"}))
            display(StyledMarkdown(
                f"{unit} dollars. A margin is the room left before a rail; negative is past it "
                "(red past a rail to stay inside, blue past a positive one); a Δ column is the "
                "variant less the baseline, so a positive Δ is more room. " + CARRIED_CAVEAT))

    observe_while_shown(dollar_mode, _redraw)
    observe_while_shown(time_range, _redraw)
    if container.ok:
        _redraw()
    return widgets.VBox([controls_row(dollar_mode, time_range), output])


def _rail_senses(results: Mapping[str, Sequence[RailResult]], names: Sequence[str]) -> dict[str, str]:
    """Each rail's sense as ``guard_rail_comparison_figure`` takes it, so the
    tables color what the chart tints: from the first of ``names`` (baseline,
    then variants) whose rail of that name covers its run's years, else the
    first that has it at all. Rails are matched by name, and a variant may
    redefine one with the other sense."""
    senses: dict[str, str] = {}
    for covering in (True, False):
        for name in names:
            for result in results.get(name, ()):
                if result.applies or not covering:
                    senses.setdefault(result.rail.name, result.rail.sense)
    return senses


def _past_color(value: float, column: str, sense: str) -> str:
    """A margin cell's color: red past a rail to stay inside, blue past a
    positive one. A Δ column's sign isn't past or inside, so it stays plain."""
    if pd.isna(value) or value >= 0 or column.startswith("Δ"):
        return ""
    return "color: #d03b3b" if sense == "negative" else "color: #2a78d6"


def ss_gross_widget(container: MultiScenarioConnections) -> None:
    """Monthly and cumulative gross Social Security, plus lifetime totals vs. baseline."""

    def _redraw():
        ss = pd.DataFrame({
            name: container.connections[name].ss_gross_by_month()
            for name in container.scenarios
        }).fillna(0).sort_index()
        ss_cumulative = ss.cumsum()
        plot_ss_gross(ss, ss_cumulative, container.colors,
                      compare.scenario_deaths(container.connections, container.scenarios))

        lifetime_ss = ss_cumulative.iloc[-1]
        display(
            pd.DataFrame({
                "lifetime gross SS": lifetime_ss,
                f"vs {container.baseline}": lifetime_ss - lifetime_ss[container.baseline],
            }).style.format("${:,.0f}")
        )

    if container.ok:
        _redraw()


def tax_impact_widget(container: MultiScenarioConnections) -> None:
    """Per-year tax delta vs. baseline, plus lifetime tax/SS/net-worth summary."""

    def _redraw():
        nw = net_worth_by_month(container)
        ss_cumulative = pd.DataFrame({
            name: container.connections[name].ss_gross_by_month()
            for name in container.scenarios
        }).fillna(0).sort_index().cumsum()
        lifetime_ss = ss_cumulative.iloc[-1]

        tax = {
            name: container.connections[name].tax_summary_by_year()
            for name in container.scenarios
        }
        tax_delta = pd.DataFrame({
            name: tax[name]["total tax"] - tax[container.baseline]["total tax"]
            for name in container.variants
        }).fillna(0)

        # Only the years where somebody's tax bill actually moves are worth reading.
        moved = tax_delta[tax_delta.abs().max(axis=1) >= 1]
        display(StyledMarkdown(
            f"Tax owed vs `{container.baseline}`, for the {len(moved)} of {len(tax_delta)} years "
            "where it differs (negative = owes less):"
        ))
        display(moved.style.format("${:,.0f}").background_gradient(cmap="RdYlGn_r", axis=None))

        lifetime_tax = pd.Series({
            name: tax[name]["total tax"].sum() for name in container.scenarios
        })
        display(
            pd.DataFrame({
                "lifetime tax": lifetime_tax,
                f"vs {container.baseline}": lifetime_tax - lifetime_tax[container.baseline],
                "lifetime gross SS": lifetime_ss,
                "SS net of tax, vs " + container.baseline: (
                    (lifetime_ss - lifetime_ss[container.baseline])
                    - (lifetime_tax - lifetime_tax[container.baseline])
                ),
                "net worth at end": nw.iloc[-1],
            }).style.format("${:,.0f}")
        )

    if container.ok:
        _redraw()


def group_account_balances(base: RunConnection) -> pd.DataFrame:
    """
    Get the account balances grouped by account.

    Parameters
    ----------
    base : RunConnection
        The run connection for the baseline scenario.

    Returns
    -------
    pd.DataFrame
        Monthly balances, one column per account.
    """
    balances = base.items("account_balance").copy()
    balances['month'] = pd.to_datetime(balances['month']).dt.strftime("%Y-%m")

    return balances.pivot(index="month", columns="account", values="balance")


def compare_account_balances(_,
                    baseline_rc: RunConnection,
                    variant_rcs: Mapping[str, RunConnection],
                    **kwargs
            ) -> tuple[
                dict[str, set[str]], dict[str, set[str]],
                dict[str, dict[str, tuple[pd.Series, pd.Series]]], pd.DataFrame,
            ]:
    """
    Compare the account balances across multiple scenario connections.

    Parameters
    ----------
    _ : Any
        Placeholder for the first argument.
    baseline_rc : RunConnection
        The run connection for the baseline scenario.
    variant_rcs : Mapping[str, RunConnection]
        The run connections for the variant scenarios, by name.
    **kwargs : dict
        Additional keyword arguments.

    Returns
    -------
    dict[str, set[str]], dict[str, set[str]], dict[str, dict[str, ...]], dict[str, ...]
    """
    bg = group_account_balances(baseline_rc)
    vgs = {
        name: group_account_balances(variant)
        for name, variant in variant_rcs.items()
        if variant is not baseline_rc}
    bg_keys = set(bg.keys())
    vgs_keys = {
        variant: set(groups.keys())
        for variant, groups in vgs.items()
    }
    added = {variant: keys - bg_keys for variant, keys in vgs_keys.items()}
    deleted = {variant: bg_keys - keys for variant, keys in vgs_keys.items()}
    # A scenario without an account (or without any months, like an empty
    # scenario) holds zero in it, so compare every account over every month.
    accounts = sorted(bg_keys.union(*vgs_keys.values()))
    months = sorted(set(bg.index).union(*(groups.index for groups in vgs.values())))
    bg = bg.reindex(index=months, columns=accounts, fill_value=0)
    vgs = {
        variant: groups.reindex(index=months, columns=accounts, fill_value=0)
        for variant, groups in vgs.items()
    }
    diff = {
        variant: {
            acct: (bg[acct], groups[acct])
            for acct in accounts
            if (bg[acct] != groups[acct]).any()
        }
        for variant, groups in vgs.items()
    }
    return (added, deleted, diff, bg)


def account_balance_diffs(baseline: str,
                          baseline_rc: RunConnection,
                          variant_rcs: Mapping[str, RunConnection],
                          **kwargs) -> tuple[str, str, dict[str, pd.DataFrame]]:
    added, deleted, diff, bg = compare_account_balances(None, baseline_rc, variant_rcs)
    diff_accounts = {
        key
        for d in diff.values()
        for key in d.keys()
    }
    diff_months = {
        m
        for d in diff.values()
        for key, (base, variant) in d.items()
        for m in base.index
        if base[m] != variant[m]
    }
    diff_accounts = set(sorted(diff_accounts))
    diff_months = list(sorted(diff_months))

    # Every scenario's accounts, so a baseline without an account (an empty
    # one, say) still excludes the variants' builtins.
    accounts = pd.concat(
        [connection.items("account") for connection in (baseline_rc, *variant_rcs.values())]
    ).drop_duplicates("name").set_index("name")

    metas = accounts["meta_json"].apply(json.loads)
    builtins = {
        acct for acct in metas.keys()
        if metas[acct].get("builtin", False)
    }
    transients = {
        acct for acct in metas.keys()
        if metas[acct].get("diff", None) == "transient"
    }
    excluded = builtins | transients


    d_add = "\n".join(
        f"-- Added accounts in `{variant}`: {", ".join(accts)}"
        for variant in variant_rcs.keys()
        for accts in ((added[variant] - excluded),)
        if accts
    )
    d_del= "\n".join(
        f"-- Deleted accounts in `{variant}`: {", ".join(accts)}"
        for variant in variant_rcs.keys()
        for accts in ((deleted[variant] - excluded),)
        if accts
    )
    def diff_acct(acct: str):
        base_col = bg[acct]
        def diffs_(variant: str):
            vaccts = diff.get(variant, None)
            if vaccts is None:
                return ()
            return tuple(
                c
                for cs in (
                    (
                        col.copy().to_frame(variant),
                        delta.to_frame(f"Δ{variant}")
                    )
                    for col in vaccts.get(acct, ())
                    for delta in ((col - base_col),)
                    if delta.any()
                )
                if len(cs) > 0
                for c in cs
            )
        dd = [diffs_(variant) for variant in variant_rcs.keys()]
        if len(dd) == 0:
            return ()
        retval = (
            base_col.to_frame("baseline"),
            *(
                x
                for d in dd
                for x in d
            )
        )
        return retval
    return d_add, d_del, {
        acct: cl[0].join(cl[1:])
        for acct in (diff_accounts - excluded)
        for cl in (diff_acct(acct),)
        if len(cl) > 1
    }

# What the account diffs show of one asset lot (ADR-005): cost basis and
# quantity often explain a difference in balance or in gains. Only for a
# picked lot: summed over an account's lots (or for a single-lot account)
# they mislead, since lots can hold different securities.
MEASURE_LABELS = {"balance": "Balance", "cost_basis": "Cost basis", "quantity": "Quantity"}


def _measure_series(
    connection: RunConnection, account: str, lot: str, measure: str,
    mode: DollarMode, dates: pd.DatetimeIndex,
) -> pd.Series:
    """One asset lot's cost basis or quantity by month. Zero in a scenario
    without the lot; missing where its basis or quantity isn't tracked."""
    pivot = (
        connection.cpi_lot_balance_pivot(measure) if mode == DollarMode.CPI_CONSTANT
        else connection.lot_balance_pivot(measure)
    )
    label = f"{account}/{lot}"
    if label not in pivot:
        return pd.Series(0.0, index=dates)
    return pivot[label].reindex(dates)


def _measure_format(measure: str) -> Callable[[float], str]:
    if measure == "quantity":
        return lambda v: "-" if pd.isna(v) else (f"{v:,.6f}".rstrip("0").rstrip(".") or "0")
    return lambda v: "-" if pd.isna(v) else f"${v:,.2f}"


def _show_account_balance_diffs(
    heading: str,
    d_add: str,
    d_del: str,
    diffs: dict[str, pd.DataFrame],
    rc: RunConnection,
    accounts: Sequence[str],
    accounts_by_kind: Mapping[AccountKind, Sequence[str]],
    variant_rcs: Mapping[str, RunConnection],
    dollar_mode: DollarModePicker,
    value_mode: ValuePlotModePicker,
    account_kinds: Mapping[str, AccountKind],
    scenario_picker: widgets.Widget | None = None,
    time_range: TimeRangePicker | None = None,
) -> widgets.VBox:
    measure_picker = widgets.ToggleButtons(
        options=[(text, key) for key, text in MEASURE_LABELS.items()],
        value="balance", description="Measure:",
        layout=widgets.Layout(**_TOGGLE_LAYOUT),  # framed like the dollar and value toggles
    )
    measure_picker.layout.display = "none"
    time_range = time_range or TimeRangePicker()
    mode_controls = controls_row(dollar_mode, value_mode, time_range, measure_picker)
    if not accounts:
        return widgets.VBox([
            *( (scenario_picker,) if scenario_picker is not None else () ),
            mode_controls,
            to_markdown(heading),
            to_markdown("_No account balance differences._"),
        ])

    initial_kind = next(iter(accounts_by_kind))
    account_picker = AccountPicker(
        rc,
        accounts=list(accounts),
        kind=initial_kind,
        allow_kind=tuple(accounts_by_kind),
        accounts_by_kind=accounts_by_kind,
        max_accounts=1,
        lots_by_account=merged_lots_by_account(rc, *variant_rcs.values()),
        lot_multiple=False,
    )
    account_output = widgets.Output()

    def display_frame(
        account: str, lot: str | None = None, measure: str = "balance",
    ) -> pd.DataFrame:
        raw = diffs[account]
        dates = pd.PeriodIndex(raw.index, freq="M").to_timestamp("M")
        kind = account_kinds[account]
        mode = dollar_mode.value
        values = {}
        for name, connection in (("baseline", rc), *variant_rcs.items()):
            if lot is not None and measure != "balance":
                values[name] = _measure_series(connection, account, lot, measure, mode, dates)
                continue
            if lot is not None:
                # One lot of the account (#170); absent from a scenario, it's zero.
                series = connection.lot_balance(account, lot, mode)
                values[name] = series.reindex(dates).fillna(0)
                continue
            pivot = (
                connection.cpi_balance_pivot(kind)
                if mode == DollarMode.CPI_CONSTANT
                else connection.balance_pivot(kind)
            )
            values[name] = (
                pivot[account].reindex(dates).fillna(0)
                if account in pivot
                else pd.Series(0.0, index=dates)
            )
        frame = pd.DataFrame(values, index=dates)
        for name in variant_rcs:
            frame[f"Δ{name}"] = frame[name] - frame["baseline"]
        if value_mode.value == ValuePlotMode.MONTHLY:
            frame = frame.diff()
        # After the monthly change, so a window's first month keeps its own.
        return time_range.clip(frame)

    def show_selected(*_):
        selected, lots = account_picker.selection
        if not selected:
            return
        account = selected[0]
        lot = lots[0] if lots else None
        # Cost basis and quantity are tracked for asset lots only.
        measured = lot is not None and account_kinds[account] == "asset"
        measure_picker.layout.display = None if measured else "none"
        measure = measure_picker.value if measured else "balance"
        df = display_frame(account, lot, measure)
        df = df.loc[df.fillna(0).ne(0).any(axis=1)].style.format(_measure_format(measure))
        heading = f"### {account}" + (f", lot {lot}" if lot else "")
        if measure != "balance":
            heading += f" - {MEASURE_LABELS[measure]}"
        with account_output:
            clear_output(wait=True)
            display(to_markdown(heading))
            display(df)

    account_picker.observe(show_selected, names="selection")
    measure_picker.observe(show_selected, names="value")
    observe_while_shown(dollar_mode, show_selected)
    observe_while_shown(time_range, show_selected)
    observe_while_shown(value_mode, show_selected)
    show_selected()

    with pd.option_context('display.max_rows', None,
                           'display.max_columns', None):
        return widgets.VBox([
            *((scenario_picker,) if scenario_picker is not None else ()),
            to_markdown(heading),
            account_picker,
            mode_controls,
            *((to_markdown(d_add),) if d_add else ()),
            *((to_markdown(d_del),) if d_del else ()),
            account_output,
        ])

def show_account_balance_diffs(
    _,
    baseline: str,
    baseline_rc: RunConnection,
    variant_rcs: Mapping[str, RunConnection],
    dollar_mode: DollarModePicker | None = None,
    value_mode: ValuePlotModePicker | None = None,
    scenario_picker: widgets.Widget | None = None,
    time_range: TimeRangePicker | None = None,
    **kwargs,
) -> widgets.VBox:
    heading = f"## Account Balance Diffs for Baseline `{baseline}`"
    d_add, d_del, diffs = account_balance_diffs(
        baseline=baseline,
        baseline_rc=baseline_rc,
        variant_rcs=variant_rcs,
        **kwargs
    )
    accounts = sorted(diffs)
    all_connections = (baseline_rc, *variant_rcs.values())
    time_range = time_range or TimeRangePicker()
    time_range.cover(run_years(*all_connections))
    account_rows = pd.concat(
        [connection.items("account").set_index("name") for connection in all_connections]
    ).loc[lambda frame: ~frame.index.duplicated(keep="first")]
    account_kinds: dict[str, AccountKind] = {name: account_rows.loc[name, "kind"] for name in accounts}
    kinds: tuple[AccountKind, ...] = ("asset", "liability", "income", "expense")
    accounts_by_kind = {
        kind: [name for name in accounts if account_rows.loc[name, "kind"] == kind]
        for kind in kinds
        if any(account_rows.loc[name, "kind"] == kind for name in accounts)
    }
    return _show_account_balance_diffs(
        heading, d_add, d_del, diffs, baseline_rc, accounts, accounts_by_kind,
        variant_rcs, dollar_mode or DollarModePicker(), value_mode or ValuePlotModePicker(),
        account_kinds, scenario_picker, time_range,
    )
