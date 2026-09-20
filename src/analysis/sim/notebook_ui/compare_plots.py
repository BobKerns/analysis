"""Scenario-compare notebook widgets: one function per section.

All take the :class:`~analysis.sim.notebook_ui.pickers.MultiScenarioConnections`
container plus the baseline/variant picker widgets, and redraw whenever
either picker changes (the container itself is already refreshed by then --
see :func:`analysis.sim.notebook_ui.pickers.multi_scenario_picker`).
"""

from __future__ import annotations

from datetime import datetime
from collections.abc import Sequence
import json
from typing import Any

import ipywidgets as widgets
import pandas as pd
from IPython.display import Markdown, clear_output, display

from analysis.sim.connection import RunConnection, ScenarioConnection, year_end_values
from analysis.sim.notebook_ui.pickers import (
    MultiScenarioConnections,
    display_warning,
    dollar_plot_mode,
)
from analysis.sim.plotting import include_zero, FRAME_ALPHA

# ---------- data helpers (pure; unchanged math from the old scenario_compare.py) ----------


def net_worth_by_month(
    container: MultiScenarioConnections, mode: str = "nominal",
) -> pd.DataFrame:
    """Net worth (USD) by month, one column per scenario."""
    value_column = "cpi_constant" if mode == "cpi_constant" else "nominal"
    return pd.DataFrame({
        name: container.connections[name].net_worth_frame()[value_column]
        for name in container.scenarios
    })


def variant_delta(nw: pd.DataFrame, container: MultiScenarioConnections) -> pd.DataFrame:
    """Each variant's net worth minus the baseline's, by month."""
    return nw[container.variants].sub(nw[container.baseline], axis=0)


def breakeven_month(series: pd.Series) -> datetime | None:
    """First month after which `series` never dips back to/below zero again."""
    ahead = series > 0
    if ahead.all():
        return series.index[0]
    if not ahead.any():
        return None
    after_last_deficit = series.index[series.index > series[~ahead].index.max()]
    return after_last_deficit.min() if len(after_last_deficit) else None


def breakeven_summary(
    nw: pd.DataFrame, delta: pd.DataFrame, container: MultiScenarioConnections,
) -> pd.DataFrame:
    """Per-variant break-even month, deepest deficit, and end-of-run comparison."""
    rows = {}
    for name in container.variants:
        series = delta[name]
        month = breakeven_month(series)
        rows[name] = {
            "break-even": f"{month:%Y-%m}" if month is not None else "never",
            "deepest deficit": series.min(),
            "when": f"{series.idxmin():%Y-%m}",
            "at scenario end": series.iloc[-1],
            "final net worth": nw[name].iloc[-1],
        }
    summary = pd.DataFrame(rows).T
    summary.index.name = f"vs {container.baseline}"
    return summary


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


def _observe_plot_mode(plot_mode: widgets.ToggleButtons, key: str, redraw) -> None:
    """Keep one observer per compare widget on the shared plot-mode toggle."""
    observers = getattr(plot_mode, "_compare_plot_redraws", {})
    previous = observers.get(key)
    if previous is not None:
        plot_mode.unobserve(previous, names="value")
    observers[key] = redraw
    plot_mode._compare_plot_redraws = observers
    plot_mode.observe(redraw, names="value")


# ---------- pure plotting (unchanged math from the old scenario_compare.py) ----------


def plot_net_worth_and_delta(
    nw: pd.DataFrame, delta: pd.DataFrame, container: MultiScenarioConnections,
    mode: str = "nominal",
) -> None:
    """Two-panel chart: net worth per scenario, and each variant's delta vs baseline."""
    import matplotlib.pyplot as plt

    baseline, variants, colors = container.baseline, container.variants, container.colors
    scenarios = [baseline, *variants]
    fig, (ax_nw, ax_delta) = plt.subplots(
        2, 1, figsize=(11, 9), sharex=True, height_ratios=[2, 1]
    )
    for name in scenarios:
        ax_nw.plot(
            nw.index, nw[name], label=name, color=colors[name],
            linestyle="-" if name == baseline else "--",
            linewidth=2 if name == baseline else 1.5,
        )
    ax_nw.set_ylabel("net worth (USD)")
    mode_label = "CPI-constant" if mode == "cpi_constant" else "nominal"
    ax_nw.set_title(f"Net worth, {mode_label}")
    # ax_nw.legend(framealpha=FRAME_ALPHA)

    for name in variants:
        ax_delta.plot(delta.index, delta[name], label=name, color=colors[name])
    ax_delta.axhline(0, color="grey", linewidth=0.8)
    ax_delta.set_ylabel(f"difference vs {baseline}")
    ax_delta.set_xlabel("month")
    ax_delta.legend(framealpha=FRAME_ALPHA)

    for ax in (ax_nw, ax_delta):
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:,.0f}"))
        ax.grid(True, axis="y", alpha=0.3)
        include_zero(ax)
    plt.tight_layout()
    plt.show()


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


def plot_ss_gross(ss: pd.DataFrame, ss_cumulative: pd.DataFrame, colors: dict[str, str]) -> None:
    """Monthly and cumulative gross Social Security, one line per scenario."""
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
                         connections: dict[str, ScenarioConnection] | None = None,
                         variants: Sequence[str] | None = None,
                         **_
                         ) -> None:
    """Whether the loaded runs share code/data version -- a comparison is only
    meaningful if a difference isn't just an unrelated code/data change."""

    connections = connections or container.connections
    variants = variants or list(connections.keys())

    def _redraw():
        meta = pd.concat(
            [
                connections[name].items("simulation_run")[_META_COLS]
                for name in variants
            ],
            ignore_index=True,
        )
        for column in ("code_version", "data_version"):
            meta[column] = meta[column].str[:12]
        display(meta.set_index("scenario_name").T)

        warnings = []
        if meta["data_version"].nunique() > 1:
            warnings.append("different **data versions** — the data repo changed between runs")
        if meta["code_version"].nunique() > 1:
            warnings.append("different **code versions** — the engine changed between runs")
        if meta[["code_dirty", "data_dirty"]].to_numpy().any():
            warnings.append("at least one run was made from **uncommitted** code or data")
        if (meta["status"] != "succeeded").any():
            warnings.append("at least one run did **not succeed**")
        if warnings:
            for warning in warnings:
                display_warning(warning)
        else:
            display(Markdown(
                "> ✅ Same code and data version; differences below are the scenarios."
            ))

    if container.ok:
        _redraw()


_RULE_COMPARE_COLS = ["amount_cents", "start", "end", "series", "cadence", "tax_character"]


def rules_and_events_widget(container: MultiScenarioConnections,
                            connections: MultiScenarioConnections,
                            scenarios: Sequence[str],
                            variants: Sequence[str],
                            baseline: str = "current",
                            **_
                            ) -> None:
    """What actually differs between scenarios: rules (as of run end) and calendar events."""

    def _redraw():
        rules = {
            name: connections[name].items("scheduled")
                .set_index("name").reindex(columns=_RULE_COMPARE_COLS)
            for name in scenarios
        }
        index = sorted(set().union(*(frame.index for frame in rules.values())))
        rules = {name: frame.reindex(index) for name, frame in rules.items()}

        base_rules = rules[baseline]
        differs = pd.DataFrame(False, index=index, columns=_RULE_COMPARE_COLS)
        for name in variants:
            other = rules[name]
            differs |= (base_rules != other) & ~(base_rules.isna() & other.isna())

        rows = [
            {"rule": rule, "field": field,
             **{name: rules[name].at[rule, field] for name in scenarios}}
            for rule in index if differs.loc[rule].any()
            for field in _RULE_COMPARE_COLS if differs.at[rule, field]
        ]
        differences = pd.DataFrame(rows)
        if differences.empty:
            display_warning("**All scenarios have identical rules.**")
        else:
            display(differences.set_index(["rule", "field"]).fillna("—"))

        events = pd.concat(
            [
                connections[name].items("calendar_event").assign(scenario=name)
                for name in scenarios
            ],
            ignore_index=True,
        )
        if events.empty:
            display(Markdown("_No calendar events in any scenario._"))
            return

        events["amount"] = (events["amount"].astype("Float64") / 100).map(
            lambda v: "" if pd.isna(v) else f"${v:,.2f}"
        )
        # Drop events whose (date, kind, scheduled_name, amount, description) is
        # identical across every scenario -- only differences are worth showing.
        sig_cols = ["date", "kind", "scheduled_name", "amount", "description"]
        sig = events[sig_cols].apply(tuple, axis=1)
        sig_scenario_count = events.groupby(sig)["scenario"].transform("nunique")
        events = events[sig_scenario_count < len(scenarios)]

        if events.empty:
            display_warning("**All scenarios have identical calendar events.**")
        else:
            display(
                events[["scenario", "date", "kind", "scheduled_name", "amount", "description"]]
                .set_index(["scenario", "date"])
            )

    if container.ok:
        _redraw()


def net_worth_compare_widget(
    container: MultiScenarioConnections,
    plot_mode: widgets.ToggleButtons | None = None,
    **_
) -> widgets.VBox:
    """Net worth per scenario, plus each variant's delta vs. the baseline."""

    plot_mode = plot_mode or dollar_plot_mode()
    output = widgets.Output()

    def _redraw(*_):
        mode = plot_mode.value
        nw = net_worth_by_month(container, mode)
        delta = variant_delta(nw, container)
        with output:
            clear_output(wait=True)
            plot_net_worth_and_delta(nw, delta, container, mode)

    _observe_plot_mode(plot_mode, "net_worth", _redraw)
    if container.ok:
        _redraw()
    return widgets.VBox([plot_mode, output])


def breakeven_widget(
    container: MultiScenarioConnections,
    plot_mode: widgets.ToggleButtons | None = None,
    baseline: str = "current",
    **_
) -> widgets.VBox:
    """Per-variant break-even summary table."""

    plot_mode = plot_mode or dollar_plot_mode()
    output = widgets.Output()

    def _redraw(*_):
        mode = plot_mode.value
        nw = net_worth_by_month(container, mode)
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
            mode_label = "CPI-constant" if mode == "cpi_constant" else "nominal"
            display(Markdown(
                f"`{baseline}` final net worth ({mode_label}): "
                f"**${nw[baseline].iloc[-1]:,.0f}**"
            ))

    _observe_plot_mode(plot_mode, "breakeven", _redraw)
    if container.ok:
        _redraw()
    return widgets.VBox([plot_mode, output])


def horizon_widget(container: MultiScenarioConnections, comparisons: dict) -> None:
    """Net-worth gap at every possible horizon, for a set of (scenario, counterfactual) pairs."""

    def _redraw():
        filtered = filter_comparisons(comparisons, container.scenarios)
        if not has_any_comparisons(filtered):
            display(Markdown(
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
    plot_mode: widgets.ToggleButtons | None = None,
    baseline: str | None = "current",
    **_
) -> widgets.VBox:
    """Year-end net worth per scenario, with each variant's delta vs. the baseline."""

    plot_mode = plot_mode or dollar_plot_mode()
    output = widgets.Output()

    def _redraw(*_):
        mode = plot_mode.value
        nw = net_worth_by_month(container, mode)
        year_end = year_end_values(nw)
        year_end.index.name = "year"
        for name in container.variants:
            year_end[f"Δ {name}"] = year_end[name] - year_end[baseline]
        with output:
            clear_output(wait=True)
            display(year_end.style.format("${:,.0f}"))

    _observe_plot_mode(plot_mode, "year_end", _redraw)
    if container.ok:
        _redraw()
    return widgets.VBox([plot_mode, output])


def ss_gross_widget(container: MultiScenarioConnections) -> None:
    """Monthly and cumulative gross Social Security, plus lifetime totals vs. baseline."""

    def _redraw():
        ss = pd.DataFrame({
            name: container.connections[name].ss_gross_by_month()
            for name in container.scenarios
        }).fillna(0).sort_index()
        ss_cumulative = ss.cumsum()
        plot_ss_gross(ss, ss_cumulative, container.colors)

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
        display(Markdown(
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


def group_account_balances(base: RunConnection) -> dict[str, float]:
    """
    Get the account balances grouped by account.

    Parameters
    ----------
    base : RunConnection
        The run connection for the baseline scenario.

    Returns
    -------
    dict[str, float]
        A dictionary mapping account names to their monthly balances.
    """
    balances = base.items("account_balance").copy()
    balances['month'] = pd.to_datetime(balances['month']).dt.strftime("%Y-%m")

    return balances.pivot(index="month", columns="account", values="balance")


def compare_account_balances(_,
                    baseline_rc: RunConnection,
                    variant_rcs: Sequence[RunConnection],
                    **kwargs
            ) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str,Any], dict[str, Any]]:
    """
    Compare the account balances across multiple scenario connections.

    Parameters
    ----------
    _ : Any
        Placeholder for the first argument.
    baseline_rc : RunConnection
        The run connection for the baseline scenario.
    variants_rcs : Sequence[RunConnection]
        The run connections for the variant scenarios.
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
    diff = {
        variant: {
            acct: (bg[acct], vgs[variant][acct])
            for acct in bg_keys
            if (bg[acct] != vgs[variant][acct]).any()
        }
        for variant in vgs_keys.keys()
        for acct in bg_keys
        if (bg[acct] != vgs[variant][acct]).any()
    }
    return (added, deleted, diff, bg)


def to_markdown(*lines: str) -> widgets.Output:
    out = widgets.Output()
    with out:
        display(
            Markdown("\n".join(lines))
        )
    return out


def account_balance_diffs(baseline: str,
                          baseline_rc: RunConnection,
                          variant_rcs: Sequence[RunConnection],
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

    accounts = baseline_rc.items("account")
    accounts = accounts.set_index(accounts["name"], drop=True)

    metas = accounts["meta_json"].apply(json.loads)
    builtins = {
        acct for acct in metas.keys()
        if metas[acct].get("builtin", False)
    }
    transients = {
        acct for acct in metas.keys()
        if metas[acct].get("diff", None) == "transient"
    }
    balance_sheet_accts = {
        acct
        for acct in accounts.index
        if accounts.loc[acct, "kind"] in ("asset", "liability")
    }
    excluded = builtins | balance_sheet_accts | transients


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

def _show_account_balance_diffs(heading: str,
                                d_add: str,
                                d_del: str,
                                diffs: dict[str, pd.DataFrame],
                            ) -> widgets.VBox:
    with pd.option_context('display.max_rows', None, 'display.max_columns', None):
        def show(label: str, df: pd.DataFrame) -> widgets.Output:
            df = df.loc[(df != 0).all(axis=1)]
            df = df / 100.0
            df = df.style.format("${:.2f}")
            out = widgets.Output()
            with out:
                display(df)
            return (to_markdown(f"### {label}"), out)
        outs = [
            b
            for acct, cl in diffs.items()
            for b in show(acct, cl)
        ]
        return widgets.VBox([
            to_markdown(heading),
            *((to_markdown(d_add),) if d_add else ()),
            *((to_markdown(d_del),) if d_del else ()),
            *outs])

def show_account_balance_diffs(_,
                          baseline: str,
                          baseline_rc: RunConnection,
                          variant_rcs: Sequence[RunConnection],
                          **kwargs) -> pd.DataFrame:
    heading = f"## Account Balance Diffs for Baseline `{baseline}`"
    d_add, d_del, diffs = account_balance_diffs(
        baseline=baseline,
        baseline_rc=baseline_rc,
        variant_rcs=variant_rcs,
        **kwargs
    )
    return _show_account_balance_diffs(heading, d_add, d_del, diffs)
