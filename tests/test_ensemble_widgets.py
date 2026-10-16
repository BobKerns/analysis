"""ADR-006 Phase 4: ensemble notebook widgets (notebook_ui.ensemble_widgets)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import matplotlib
import pandas as pd
import pytest

matplotlib.use("Agg")

import matplotlib.pyplot as plt

from analysis.sim.engine import run_scenario
from analysis.sim.ensemble import (
    latest_ensemble_batch,
    load_ensemble,
    members_net_worth_by_month,
    resolve_member,
    summary_with_percentiles,
)
from analysis.sim.loader import LoadResult
from analysis.sim.notebook_ui.ensemble_widgets import (
    ensemble_fan_chart,
    ensemble_summary_view,
    ensemble_widget,
    list_ensemble_recipes,
)


def _monthly_summary() -> pd.DataFrame:
    index = pd.date_range("2026-01-31", periods=6, freq="ME")
    return pd.DataFrame({
        "mean": [100.0, 110, 120, 130, 140, 150],
        "median": [99.0, 109, 119, 129, 139, 149],
        "std": [5.0, 5, 5, 5, 5, 5],
        "min": [90.0, 95, 100, 105, 110, 115],
        "max": [110.0, 120, 130, 140, 150, 160],
    }, index=index)


def test_ensemble_summary_view_returns_year_end_rows_and_member_count() -> None:
    heading, yearly = ensemble_summary_view(_monthly_summary(), member_count=4)
    assert "4 member(s)" in heading
    assert list(yearly.columns) == ["mean", "median", "std", "min", "max"]
    assert yearly.index.name == "year"
    assert len(yearly) == 1  # all 6 months fall in 2026
    assert yearly.loc[2026, "mean"] == 150.0  # last month of the year


def test_ensemble_fan_chart_builds_without_error() -> None:
    ax = ensemble_fan_chart(_monthly_summary())
    assert len(ax.lines) == 3  # mean, median, zero rule
    assert len(ax.collections) == 1  # fill_between range band


def test_ensemble_fan_chart_shades_quintile_bands_darker_toward_the_middle() -> None:
    members = pd.DataFrame(
        [[float(m * 10 + month) for m in range(11)] for month in range(6)],
        index=pd.date_range("2026-01-31", periods=6, freq="ME"),
    )
    monthly = summary_with_percentiles(members)
    ax = ensemble_fan_chart(monthly)
    assert len(ax.collections) == 5  # p05-p20, p20-p40, p40-p60, p60-p80, p80-p95
    _handles, labels = ax.get_legend_handles_labels()
    assert labels == ["5–95%", "20–80%", "40–60%", "mean", "median"]
    # Lighter (closer to white) at the edges, darkest in the middle band,
    # and symmetric about it.
    lightness = [sum(c.get_facecolor()[0][:3]) for c in ax.collections]
    assert lightness[0] > lightness[1] > lightness[2]
    assert lightness[0] == pytest.approx(lightness[4])
    assert lightness[1] == pytest.approx(lightness[3])


def test_ensemble_fan_chart_zero_rule_does_not_distort_a_far_off_range() -> None:
    """The zero rule is drawn with axhline, which (left alone) would feed
    into autoscale and pull the whole chart down to include 0 even when the
    data never gets near it -- the exact distortion a fan chart is trying to
    avoid. The axis limits must come out identical to a chart with no rule
    at all when 0 is nowhere close."""
    far_from_zero = pd.DataFrame({
        "mean": [950_000.0, 1_020_000], "median": [945_000.0, 1_015_000],
        "std": [5_000.0, 5_000], "min": [900_000.0, 950_000], "max": [1_000_000.0, 1_100_000],
    }, index=pd.date_range("2026-01-31", periods=2, freq="ME"))

    fig1, ax1 = plt.subplots()
    ax1.fill_between(far_from_zero.index, far_from_zero["min"], far_from_zero["max"])
    ax1.plot(far_from_zero.index, far_from_zero["mean"])
    ax1.plot(far_from_zero.index, far_from_zero["median"])
    expected_ylim = ax1.get_ylim()

    ax2 = ensemble_fan_chart(far_from_zero)
    assert ax2.get_ylim() == expected_ylim
    assert expected_ylim[0] > 0  # 0 genuinely isn't in (or near) this range


def test_ensemble_fan_chart_zero_rule_shows_when_range_crosses_zero() -> None:
    crosses_zero = pd.DataFrame({
        "mean": [50.0, -10], "median": [45.0, -15],
        "std": [5.0, 5], "min": [10.0, -80], "max": [100.0, 50],
    }, index=pd.date_range("2026-01-31", periods=2, freq="ME"))
    ax = ensemble_fan_chart(crosses_zero)
    ylim = ax.get_ylim()
    assert ylim[0] < 0 < ylim[1]
    zero_rules = [line for line in ax.lines if list(line.get_ydata()) == [0, 0]]
    assert len(zero_rules) == 1


def test_ensemble_fan_chart_zero_rule_skipped_on_log_scale() -> None:
    far_from_zero = pd.DataFrame({
        "mean": [950_000.0, 1_020_000], "median": [945_000.0, 1_015_000],
        "std": [5_000.0, 5_000], "min": [900_000.0, 950_000], "max": [1_000_000.0, 1_100_000],
    }, index=pd.date_range("2026-01-31", periods=2, freq="ME"))
    fig, ax = plt.subplots()
    ax.set_yscale("log")
    ensemble_fan_chart(far_from_zero, ax=ax)
    assert len(ax.lines) == 2  # mean, median -- no zero rule added at all


def test_list_ensemble_recipes_finds_only_directories_with_members(tmp_path: Path) -> None:
    assert list_ensemble_recipes(tmp_path / "nope") == []

    runs_dir = tmp_path / "runs"
    (runs_dir / "recipe_a" / "2026-01-01T000000Z").mkdir(parents=True)
    (runs_dir / "recipe_a" / "2026-01-01T000000Z" / "member_0000.sqlite3").touch()
    (runs_dir / "recipe_b" / "2026-01-01T000000Z").mkdir(parents=True)  # no members yet
    (runs_dir / "not_a_recipe.sqlite3").touch()  # a plain, non-ensemble run file

    assert list_ensemble_recipes(runs_dir) == ["recipe_a"]


@pytest.fixture
def small_ensemble_runs_dir(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    intent_dir: Path, runs_dir: Path,
) -> Path:
    """Two already-resolved-and-run members of a tiny vary_distribution recipe."""
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="uniform",
        vary_low="4%", vary_high="5%",
    )
    recipe = load_test_copy().intent_path
    batch_dir = runs_dir / "retire_alice" / "2026-01-01T000000Z"
    for i in range(2):
        member_intent = resolve_member(
            recipe, intent_dir / f"intent_member_{i}.sqlite3",
            seed=1, member_index=i, member_count=2, recipe_tree_id="deadbeef",
        )
        run_scenario(
            member_intent, batch_dir / f"member_{i:04d}.sqlite3",
            run_by="test", allow_dirty_data=True,
        )
    return runs_dir


def test_ensemble_widget_returns_a_box_without_displaying_it(
    small_ensemble_runs_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ipywidgets import widgets

    from analysis.sim.notebook_ui import ensemble_widgets

    shown: list[object] = []
    monkeypatch.setattr(ensemble_widgets, "display", lambda *args, **kwargs: shown.extend(args))
    box = ensemble_widget(runs_dir=small_ensemble_runs_dir)
    assert isinstance(box, widgets.VBox)
    assert not any(isinstance(item, widgets.Widget) for item in shown)


def test_ensemble_widget_dropdown_lists_the_recipe(small_ensemble_runs_dir: Path) -> None:
    box = ensemble_widget(runs_dir=small_ensemble_runs_dir)
    dropdown, _row, _out = box.children
    assert dropdown.options == ("retire_alice",)
    assert dropdown.value == "retire_alice"


def test_ensemble_widget_matches_cli_summary_numbers(small_ensemble_runs_dir: Path) -> None:
    batch = latest_ensemble_batch(small_ensemble_runs_dir / "retire_alice")
    assert batch is not None
    connections = load_ensemble(batch)
    try:
        monthly_summary = summary_with_percentiles(members_net_worth_by_month(connections))
        _heading, yearly = ensemble_summary_view(monthly_summary, len(connections))
        assert len(yearly) > 0
        assert list(yearly.columns) == [
            "mean", "median", "std", "p05", "p20", "p40", "p60", "p80", "p95",
        ]
        # Each breakpoint sits between its neighbors, low to high.
        ordered = yearly[["p05", "p20", "p40", "p60", "p80", "p95"]]
        assert (ordered.diff(axis=1).iloc[:, 1:] >= -1e-6).all().all()
    finally:
        for con in connections.values():
            con.close()


def test_ensemble_widget_dollar_mode_is_passed_through_to_aggregation(
    small_ensemble_runs_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from analysis.sim.notebook_ui import ensemble_widgets
    from analysis.sim.notebook_ui.pickers import DollarModePicker
    from analysis.sim.types import DollarMode

    modes_seen: list[DollarMode] = []
    real = ensemble_widgets.members_balance_by_month

    def _spy(connections, account=None, mode=DollarMode.NOMINAL, lot=None):
        modes_seen.append(mode)
        return real(connections, account, mode, lot)

    monkeypatch.setattr(ensemble_widgets, "members_balance_by_month", _spy)
    dollar_mode = DollarModePicker()
    box = ensemble_widget(runs_dir=small_ensemble_runs_dir, dollar_mode=dollar_mode)
    _dropdown, row, _out = box.children
    assert dollar_mode in row.children
    assert modes_seen[-1] == DollarMode.NOMINAL

    dollar_mode.value = DollarMode.CPI_CONSTANT
    assert modes_seen[-1] == DollarMode.CPI_CONSTANT


def test_ensemble_widget_log_linear_mode_sets_the_yscale(
    small_ensemble_runs_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from analysis.sim.notebook_ui import ensemble_widgets
    from analysis.sim.notebook_ui.pickers import LogLinearModePicker
    from analysis.sim.types import LogLinearMode

    axes: list = []
    real_fan_chart = ensemble_widgets.ensemble_fan_chart

    def _spy_fan_chart(monthly_summary, ax=None):
        axes.append(ax)
        return real_fan_chart(monthly_summary, ax=ax)

    monkeypatch.setattr(ensemble_widgets, "ensemble_fan_chart", _spy_fan_chart)
    log_linear_mode = LogLinearModePicker()
    ensemble_widget(runs_dir=small_ensemble_runs_dir, log_linear_mode=log_linear_mode)
    assert axes[-1].get_yscale() == "linear"

    log_linear_mode.value = LogLinearMode.LOG
    assert axes[-1].get_yscale() == "log"


def test_ensemble_widget_log_mode_falls_back_to_linear_for_non_positive_net_worth(
    small_ensemble_runs_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Log scale can't represent zero or negative net worth -- silently
    switching to linear there would clip exactly the insolvency tail a fan
    chart exists to show, so LOG mode must fall back, not just render oddly."""
    from analysis.sim.notebook_ui import ensemble_widgets
    from analysis.sim.notebook_ui.pickers import LogLinearModePicker
    from analysis.sim.types import LogLinearMode

    axes: list = []
    real_fan_chart = ensemble_widgets.ensemble_fan_chart

    def _spy_fan_chart(monthly_summary, ax=None):
        axes.append(ax)
        return real_fan_chart(monthly_summary, ax=ax)

    monkeypatch.setattr(ensemble_widgets, "ensemble_fan_chart", _spy_fan_chart)
    # The reported frame's shape, with only the lowest band (p05) reaching
    # zero: mean and median stay positive, so only the p05 check can trip.
    negative_summary = pd.DataFrame({
        "mean": [100.0, 90.0], "median": [95.0, 85.0], "std": [5.0, 5.0],
        "p05": [50.0, -20.0], "p20": [70.0, 40.0], "p40": [90.0, 80.0],
        "p60": [100.0, 95.0], "p80": [120.0, 110.0], "p95": [150.0, 140.0],
    }, index=pd.date_range("2026-01-31", periods=2, freq="ME"))
    monkeypatch.setattr(ensemble_widgets, "summary_with_percentiles", lambda *a, **k: negative_summary)

    log_linear_mode = LogLinearModePicker(log_linear_mode=LogLinearMode.LOG)
    ensemble_widget(runs_dir=small_ensemble_runs_dir, log_linear_mode=log_linear_mode)
    assert axes[-1].get_yscale() == "linear"


def test_ensemble_widget_accepts_extra_arguments(small_ensemble_runs_dir: Path) -> None:
    """Like every widget `Connections.show()` may be handed, it tolerates
    positional and keyword arguments it doesn't use."""
    box = ensemble_widget("ignored", runs_dir=small_ensemble_runs_dir, member=3)
    assert box.children


def test_ensemble_widget_leaves_no_figure_open_in_pyplot(small_ensemble_runs_dir: Path) -> None:
    """The chart is never registered with pyplot, so matplotlib-inline's
    flush_figures, run by another widget message on a kernel subshell, can't
    render it while it's still being drawn (#119)."""
    plt.close("all")
    ensemble_widget(runs_dir=small_ensemble_runs_dir)
    assert plt.get_fignums() == []


def test_ensemble_widget_account_picker_lists_net_worth_then_accounts(
    small_ensemble_runs_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#163: any one account's spread, not just net worth's."""
    from analysis.sim.notebook_ui import ensemble_widgets

    axes: list = []
    real_fan_chart = ensemble_widgets.ensemble_fan_chart

    def _spy_fan_chart(monthly_summary, ax=None):
        axes.append(ax)
        return real_fan_chart(monthly_summary, ax=ax)

    monkeypatch.setattr(ensemble_widgets, "ensemble_fan_chart", _spy_fan_chart)
    box = ensemble_widget(runs_dir=small_ensemble_runs_dir)
    _dropdown, row, _out = box.children
    controls = row.controls
    account_picker = controls.account_picker.account_picker
    assert account_picker.label == ensemble_widgets.NET_WORTH
    assert ("Asset_Checking", "Asset_Checking") in account_picker.options
    account_picker.value = "Asset_Checking"
    assert axes[-1].get_title().startswith("Asset_Checking fan chart")


def test_account_named_net_worth_is_not_mistaken_for_net_worth() -> None:
    """The picker's net-worth value is a sentinel, not its label, so an
    account that happens to be named "Net worth" can still be picked."""
    from analysis.sim.notebook_ui import ensemble_widgets

    assert ensemble_widgets._account(ensemble_widgets._NET_WORTH_CHOICE) is None
    assert ensemble_widgets._account(ensemble_widgets.NET_WORTH) == ensemble_widgets.NET_WORTH


def test_ensemble_widget_table_adds_tail_means_and_reserve_shares(
    small_ensemble_runs_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from analysis.sim.notebook_ui import ensemble_widgets

    tables: list[pd.DataFrame] = []
    real_view = ensemble_widgets.ensemble_summary_view

    def _spy_view(monthly_summary, member_count, failed_count=0):
        heading, yearly = real_view(monthly_summary, member_count, failed_count)
        tables.append(yearly)
        return heading, yearly

    monkeypatch.setattr(ensemble_widgets, "ensemble_summary_view", _spy_view)
    box = ensemble_widget(runs_dir=small_ensemble_runs_dir)
    _dropdown, row, _out = box.children
    controls = row.controls
    assert list(tables[-1].columns)[-2:] == ["cvar05", "cvar20"]
    controls.reserve_field.value = "1000000"
    assert list(tables[-1].columns)[-2:] == ["below_reserve", "ever_below_reserve"]


def test_ensemble_compare_widget_draws_one_section_per_variant(
    seeded_ensemble_batch: Callable[[str, int], Path], runs_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two recipes from one seed pair member by member, so the paired-fan
    panel is drawn too; one from another seed is said not to pair."""
    from IPython.display import Image

    from analysis.sim.notebook_ui import ensemble_widgets
    from analysis.sim.notebook_ui.styled import StyledMarkdown

    seeded_ensemble_batch("baseline", 1)
    seeded_ensemble_batch("child", 1)
    seeded_ensemble_batch("other", 2)
    plt.close("all")  # figures an earlier test in this worker left open
    shown: list[object] = []
    monkeypatch.setattr(ensemble_widgets, "display", lambda *args, **kwargs: shown.extend(args))
    box = ensemble_widgets.ensemble_compare_widget(runs_dir=runs_dir)
    recipe_pickers, _row, _out = box.children
    baseline_picker, variant_picker = recipe_pickers.children
    baseline_picker.value = "baseline"
    shown.clear()
    variant_picker.value = ("child", "other")
    headings = [str(item.data) for item in shown if isinstance(item, StyledMarkdown)]
    assert any("### child vs baseline" in h and "paired over 3 futures" in h for h in headings)
    assert any("### other vs baseline" in h and "not paired" in h for h in headings)
    assert sum(isinstance(item, Image) for item in shown) == 2
    assert plt.get_fignums() == []


def test_ensemble_compare_figure_skips_the_paired_panel_when_unpaired() -> None:
    from analysis.sim.notebook_ui.ensemble_widgets import ensemble_compare_figure

    months = pd.date_range("2027-01-31", periods=3, freq="ME")
    baseline = pd.DataFrame({"member_0000": [1.0, 2.0, 3.0], "member_0001": [2.0, 3.0, 4.0]},
                            index=months)
    paired = ensemble_compare_figure("a", "b", baseline, baseline + 1,
                                     [("member_0000", "member_0000")], "nominal")
    unpaired = ensemble_compare_figure("a", "b", baseline, baseline + 1, [], "nominal")
    assert (len(paired.axes), len(unpaired.axes)) == (3, 2)
    with_reserve = ensemble_compare_figure("a", "b", baseline, baseline + 1, [], "nominal", 2.5)
    assert len(with_reserve.axes) == 3
    assert "reserve" in with_reserve.axes[-1].get_title()


def test_ensemble_compare_widget_lists_every_recipes_accounts_and_skips_one_sided_deltas(
    seeded_ensemble_batch: Callable[[str, int], Path], runs_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An account only the variant has is offered, and drawn as the
    variant's own fan with a note rather than as a delta (#163)."""
    import sqlite3

    from IPython.display import Image

    from analysis.sim.notebook_ui import ensemble_widgets
    from analysis.sim.notebook_ui.styled import StyledMarkdown

    seeded_ensemble_batch("baseline", 1)
    for member in seeded_ensemble_batch("child", 1).glob("member_*.sqlite3"):
        with sqlite3.connect(member) as con:
            con.execute("UPDATE account SET name = 'Asset_Renamed' WHERE name = 'Asset_Checking'")
    shown: list[object] = []
    monkeypatch.setattr(ensemble_widgets, "display", lambda *args, **kwargs: shown.extend(args))
    box = ensemble_widgets.ensemble_compare_widget(runs_dir=runs_dir)
    recipe_pickers, row, _out = box.children
    controls = row.controls
    baseline_picker, variant_picker = recipe_pickers.children
    account_picker = controls.account_picker.account_picker
    baseline_picker.value = "baseline"
    variant_picker.value = ("child",)
    assert {"Asset_Checking", "Asset_Renamed"} <= {label for label, _ in account_picker.options}
    shown.clear()
    account_picker.value = "Asset_Renamed"
    headings = [str(item.data) for item in shown if isinstance(item, StyledMarkdown)]
    assert any("`baseline` has no account Asset_Renamed" in h for h in headings)
    assert sum(isinstance(item, Image) for item in shown) == 1  # the child's own fan


def test_ensemble_compare_widget_says_so_when_no_shown_recipe_has_the_account(
    seeded_ensemble_batch: Callable[[str, int], Path], runs_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared account list spans every recipe, so the pick can be in
    none of the compared ones: a note, not an error from an empty tail table."""
    import sqlite3

    from analysis.sim.notebook_ui import ensemble_widgets
    from analysis.sim.notebook_ui.styled import StyledMarkdown

    seeded_ensemble_batch("baseline", 1)
    seeded_ensemble_batch("other", 1)
    for member in seeded_ensemble_batch("child", 1).glob("member_*.sqlite3"):
        with sqlite3.connect(member) as con:
            con.execute("UPDATE account SET name = 'Asset_Renamed' WHERE name = 'Asset_Checking'")
    shown: list[object] = []
    monkeypatch.setattr(ensemble_widgets, "display", lambda *args, **kwargs: shown.extend(args))
    box = ensemble_widgets.ensemble_compare_widget(runs_dir=runs_dir)
    recipe_pickers, row, _out = box.children
    baseline_picker, variant_picker = recipe_pickers.children
    baseline_picker.value = "baseline"
    variant_picker.value = ("other",)
    shown.clear()
    row.controls.account_picker.account_picker.value = "Asset_Renamed"
    headings = [str(item.data) for item in shown if isinstance(item, StyledMarkdown)]
    assert any("None of these recipes has account Asset_Renamed" in h for h in headings)
