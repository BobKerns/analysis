"""Scenario picker widget and loading logic for the scenario_compare notebook.

Keeps ipywidgets/matplotlib plumbing and data prep out of the notebook so it
reads as analysis, not setup code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

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


def render_load_or_warn(
    data_dir: Path, baseline: str, variants: tuple[str, ...] | list[str], max_variants: int = 4
) -> ScenarioLoadResult | None:
    """Load scenarios, displaying any warnings; None means there's nothing to render."""
    from IPython.display import Markdown, display

    result = load_scenarios(data_dir, baseline, list(variants), max_variants)
    for warning in result.warnings:
        display(Markdown(f"> ⚠️ {warning}"))
    return result if result.ok else None


def scenario_picker_widgets(available: list[str], default_baseline: str, default_variants: list[str]):
    """Build the baseline dropdown and variant multi-select (variants excludes the baseline)."""
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

    def _sync_variant_options(*_):
        variant_picker.options = [name for name in available if name != baseline_picker.value]
        variant_picker.value = tuple(v for v in variant_picker.value if v != baseline_picker.value)

    baseline_picker.observe(_sync_variant_options, names="value")

    return baseline_picker, variant_picker


def year_end_values(frame: pd.DataFrame | pd.Series) -> pd.DataFrame | pd.Series:
    """Last row of each calendar year, re-indexed by year (int)."""
    year_end = frame.groupby(frame.index.year).tail(1)
    year_end.index = year_end.index.year
    return year_end


def net_worth_by_month(runs: dict[str, RunConnection], scenarios: list[str]) -> pd.DataFrame:
    """Nominal net worth (USD) by month, one column per scenario."""

    def _one(name: str) -> pd.Series:
        df = runs[name].items("net_worth")[["month", "net_worth"]].copy()
        df["month"] = pd.to_datetime(df["month"])
        return df.set_index("month")["net_worth"] / 100

    return pd.DataFrame({name: _one(name) for name in scenarios})


def variant_delta(nw: pd.DataFrame, baseline: str, variants: list[str]) -> pd.DataFrame:
    """Each variant's net worth minus the baseline's, by month."""
    return nw[variants].sub(nw[baseline], axis=0)


def breakeven_month(series: pd.Series):
    """First month after which `series` never dips back to/below zero again."""
    ahead = series > 0
    if ahead.all():
        return series.index[0]
    if not ahead.any():
        return None
    after_last_deficit = series.index[series.index > series[~ahead].index.max()]
    return after_last_deficit.min() if len(after_last_deficit) else None


def plot_net_worth_and_delta(
    nw: pd.DataFrame,
    delta: pd.DataFrame,
    baseline: str,
    variants: list[str],
    colors: dict[str, str],
) -> None:
    """Two-panel chart: net worth per scenario, and each variant's delta vs baseline."""
    import matplotlib.pyplot as plt

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
    ax_nw.set_title("Net worth, nominal")
    ax_nw.legend()

    for name in variants:
        ax_delta.plot(delta.index, delta[name], label=name, color=colors[name])
    ax_delta.axhline(0, color="grey", linewidth=0.8)
    ax_delta.set_ylabel(f"difference vs {baseline}")
    ax_delta.set_xlabel("month")
    ax_delta.legend()

    for ax in (ax_nw, ax_delta):
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:,.0f}"))
        ax.grid(True, axis="y", alpha=0.3)
    plt.tight_layout()
    plt.show()


def breakeven_summary(
    nw: pd.DataFrame, delta: pd.DataFrame, baseline: str, variants: list[str]
) -> pd.DataFrame:
    """Per-variant break-even month, deepest deficit, and end-of-run comparison."""
    rows = {}
    for name in variants:
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
    summary.index.name = f"vs {baseline}"
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


def plot_horizon_comparisons(compare: pd.DataFrame, comparisons: dict) -> None:
    """One panel per comparison group, with a break-even marker on each line."""
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, len(comparisons), figsize=(13, 5))
    if len(comparisons) == 1:
        axes = [axes]
    for ax, (group, pairs) in zip(axes, comparisons.items()):
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
        ax.legend()
    axes[0].set_ylabel("difference in net worth")
    plt.tight_layout()
    plt.show()


DEFAULT_SETTLE_BUCKETS = ["federal_tax_due", "state_tax_due"]


def outstanding_tax_by_year(
    runs: dict[str, RunConnection], name: str, buckets: list[str] = DEFAULT_SETTLE_BUCKETS
) -> pd.Series:
    """Year X's tax liability still unpaid at year end; it settles the April after.

    Only `*_tax_due` buckets are used. `*_tax_overpaid` looks like a refund but
    also fires when the *previous* April's settlement lands in a zero-liability
    year, so crediting it here would double-count that payment.
    """
    df = runs[name].items("tax_year_summary")
    df = df[df["bucket"].isin(buckets)]
    wide = (
        df.pivot(index="year", columns="bucket", values="amount")
        .reindex(columns=buckets).fillna(0) / 100
    )
    return wide.sum(axis=1)


def ss_gross_by_month(runs: dict[str, RunConnection], name: str) -> pd.Series:
    """Gross Social Security received per month (before Medicare withholding)."""
    te = runs[name].items("transfer_event")
    te = te[te["tax_character"] == "ss_benefit"].copy()
    te["month"] = pd.to_datetime(te["month"])
    return te.groupby("month")["amount"].sum() / 100


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
        ax.legend()
    plt.tight_layout()
    plt.show()


DEFAULT_TAX_BUCKETS = {
    "federal_tax_liability": "federal tax",
    "state_tax_liability": "state tax",
    "federal_ss_income": "SS gross",
    "federal_taxable_ss": "SS taxed",
}


def tax_summary_by_year(
    runs: dict[str, RunConnection], name: str, buckets: dict[str, str] = DEFAULT_TAX_BUCKETS
) -> pd.DataFrame:
    """Yearly federal/state tax and SS income/taxed-portion, plus a total-tax column."""
    df = runs[name].items("tax_year_summary")
    df = df[df["bucket"].isin(buckets)]
    wide = df.pivot(index="year", columns="bucket", values="amount") / 100
    wide = wide.rename(columns=buckets).reindex(columns=list(buckets.values()))
    wide["total tax"] = wide["federal tax"] + wide["state tax"]
    return wide

