"""ADR-006 Phase 4: ensemble notebook widgets (notebook_ui.ensemble_widgets)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

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
from analysis.sim import ensemble_plots
from analysis.sim.notebook_ui.ensemble_widgets import (
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
    dropdown, _row, _loading, _out = box.children
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
    from analysis.sim.notebook_ui.pickers import DollarModePicker
    from analysis.sim.types import DollarMode

    from analysis.sim.series_db import EnsembleSeries

    modes_seen: list[DollarMode] = []
    real = EnsembleSeries.frame

    def _spy(self, account=None, mode=DollarMode.NOMINAL, lot=None, *, after_tax=False):
        modes_seen.append(mode)
        return real(self, account, mode, lot, after_tax=after_tax)

    monkeypatch.setattr(EnsembleSeries, "frame", _spy)
    dollar_mode = DollarModePicker()
    box = ensemble_widget(runs_dir=small_ensemble_runs_dir, dollar_mode=dollar_mode)
    _dropdown, row, _loading, _out = box.children
    assert dollar_mode in row.children
    assert modes_seen[-1] == DollarMode.NOMINAL

    dollar_mode.value = DollarMode.CPI_CONSTANT
    assert modes_seen[-1] == DollarMode.CPI_CONSTANT


def test_ensemble_widget_years_redraw_without_reading_members_again(
    small_ensemble_runs_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from analysis.sim.notebook_ui import ensemble_widgets
    from analysis.sim.notebook_ui.pickers import TimeRangePicker
    from analysis.sim.series_db import EnsembleSeries

    reads: list[object] = []
    real = EnsembleSeries._nominal

    def _spy(self, key):
        reads.append(key)
        return real(self, key)

    shown: list[Any] = []
    monkeypatch.setattr(EnsembleSeries, "_nominal", _spy)
    monkeypatch.setattr(ensemble_widgets, "display", lambda *args, **kwargs: shown.extend(args))
    time_range = TimeRangePicker()
    controls = ensemble_widgets.EnsembleControls(runs_dir=small_ensemble_runs_dir,
                                                 time_range=time_range)
    ensemble_widget(runs_dir=small_ensemble_runs_dir, controls=controls)
    years = time_range.min, time_range.max
    assert years[0] < years[1]  # spans the recipe's run
    read_before = len(reads)

    time_range.value = (years[0], years[0])

    assert len(reads) == read_before
    table = shown[-1].data
    assert list(table.index) == [years[0]]


def test_ensemble_widget_log_linear_mode_sets_the_yscale(
    small_ensemble_runs_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from analysis.sim.notebook_ui.pickers import LogLinearModePicker
    from analysis.sim.types import LogLinearMode

    axes: list = []
    real_fan_chart = ensemble_plots.ensemble_fan_chart

    def _spy_fan_chart(monthly_summary, ax=None):
        axes.append(ax)
        return real_fan_chart(monthly_summary, ax=ax)

    monkeypatch.setattr(ensemble_plots, "ensemble_fan_chart", _spy_fan_chart)
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
    real_fan_chart = ensemble_plots.ensemble_fan_chart

    def _spy_fan_chart(monthly_summary, ax=None):
        axes.append(ax)
        return real_fan_chart(monthly_summary, ax=ax)

    monkeypatch.setattr(ensemble_plots, "ensemble_fan_chart", _spy_fan_chart)
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


def test_ensemble_widget_shows_progress_per_member_then_hides_it(
    small_ensemble_runs_dir: Path,
) -> None:
    """#235: a progress line counts members while they load, and goes away
    once the chart is drawn."""
    from analysis.sim.notebook_ui.ensemble_widgets import LoadingIndicator

    labels: list[str] = []
    real_counter = LoadingIndicator.counter

    def _counter(self: LoadingIndicator, text: str, count: int):
        step = real_counter(self, text, count)

        def _step() -> None:
            step()
            labels.append(self.label.value)
        return _step

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(LoadingIndicator, "counter", _counter)
        box = ensemble_widget(runs_dir=small_ensemble_runs_dir)
    _dropdown, _row, loading, _out = box.children
    assert isinstance(loading, LoadingIndicator)
    assert labels == ["Loading retire_alice: member 1 of 2", "Loading retire_alice: member 2 of 2"]
    assert loading.bar.value == loading.bar.max == 2
    assert loading.layout.display == "none"


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
    real_fan_chart = ensemble_plots.ensemble_fan_chart

    def _spy_fan_chart(monthly_summary, ax=None):
        axes.append(ax)
        return real_fan_chart(monthly_summary, ax=ax)

    monkeypatch.setattr(ensemble_plots, "ensemble_fan_chart", _spy_fan_chart)
    box = ensemble_widget(runs_dir=small_ensemble_runs_dir)
    _dropdown, row, _loading, _out = box.children
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


@pytest.mark.slow
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
    _dropdown, row, _loading, _out = box.children
    controls = row.controls
    assert list(tables[-1].columns)[-2:] == ["cvar05", "cvar20"]
    controls.reserve_field.value = "1000000"
    assert list(tables[-1].columns)[-2:] == ["below_reserve", "ever_below_reserve"]


@pytest.mark.slow
def test_ensemble_compare_widget_offers_only_recipes_that_pair_with_the_baseline(
    seeded_ensemble_batch: Callable[[str, int], Path], runs_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#298: the second dropdown lists the recipes sharing the baseline's
    seed, never the baseline itself, and none is picked to start; picking
    one draws the head-to-head comparison, paired member by member, as
    charts alone: its tables are the table widget's."""
    from IPython.display import Image
    from pandas.io.formats.style import Styler

    from analysis.sim.notebook_ui import ensemble_widgets
    from analysis.sim.notebook_ui.styled import StyledMarkdown

    seeded_ensemble_batch("baseline", 1)
    seeded_ensemble_batch("child", 1)
    seeded_ensemble_batch("other", 2)
    plt.close("all")  # figures an earlier test in this worker left open
    shown: list[object] = []
    monkeypatch.setattr(ensemble_widgets, "display", lambda *args, **kwargs: shown.extend(args))
    box = ensemble_widgets.ensemble_compare_widget(runs_dir=runs_dir)
    recipe_pickers, _row, _loading, _out = box.children
    baseline_picker, variant_picker = recipe_pickers.children
    baseline_picker.value = "baseline"
    assert variant_picker.options == ("child",)
    assert variant_picker.value is None
    baseline_picker.value = "other"
    assert variant_picker.options == ()
    note = shown[-1]
    assert isinstance(note, StyledMarkdown) and "none shares its seed" in str(note.data)
    baseline_picker.value = "baseline"
    shown.clear()
    variant_picker.value = "child"
    headings = [str(item.data) for item in shown if isinstance(item, StyledMarkdown)]
    assert any("### child vs baseline" in h and "paired over 3 futures" in h for h in headings)
    assert sum(isinstance(item, Image) for item in shown) == 1
    assert not any(isinstance(item, Styler) for item in shown)
    assert plt.get_fignums() == []

    baseline_picker.value = "child"  # picking the compared recipe swaps the two
    assert variant_picker.options == ("baseline",)
    assert variant_picker.value == "baseline"


@pytest.mark.slow
def test_ensemble_table_widget_lists_each_paired_recipe_by_year(
    seeded_ensemble_batch: Callable[[str, int], Path], runs_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#298: one table of deltas against the baseline and one of tails, both
    indexed by (year, recipe), for the paired recipes picked."""
    from pandas.io.formats.style import Styler

    from analysis.sim.notebook_ui import ensemble_widgets

    for name in ("baseline", "child", "sibling"):
        seeded_ensemble_batch(name, 1)
    seeded_ensemble_batch("other", 2)
    shown: list[object] = []
    monkeypatch.setattr(ensemble_widgets, "display", lambda *args, **kwargs: shown.extend(args))
    box = ensemble_widgets.ensemble_table_widget(runs_dir=runs_dir)
    recipe_pickers, _row, _loading, _out = box.children
    baseline_picker, variant_picker = recipe_pickers.children
    baseline_picker.value = "baseline"
    assert variant_picker.options == ("child", "sibling")
    shown.clear()
    variant_picker.value = ("child", "sibling")
    deltas, tails = (item.data for item in shown if isinstance(item, Styler))
    assert deltas.index.names == ["year", "recipe"]
    first_year = deltas.index[0][0]
    assert list(deltas.loc[first_year].index) == ["child", "sibling"]
    assert "P(ahead)" in deltas.columns
    assert list(tails.loc[first_year].index) == ["baseline", "child", "sibling"]


@pytest.mark.slow
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
    recipe_pickers, row, _loading, _out = box.children
    controls = row.controls
    baseline_picker, variant_picker = recipe_pickers.children
    account_picker = controls.account_picker.account_picker
    baseline_picker.value = "baseline"
    variant_picker.value = "child"
    assert {"Asset_Checking", "Asset_Renamed"} <= {label for label, _ in account_picker.options}
    shown.clear()
    account_picker.value = "Asset_Renamed"
    headings = [str(item.data) for item in shown if isinstance(item, StyledMarkdown)]
    assert any("No account Asset_Renamed in `baseline`" in h for h in headings)
    assert sum(isinstance(item, Image) for item in shown) == 1  # the child's own fan


@pytest.mark.slow
def test_ensemble_compare_widget_says_so_when_no_shown_recipe_has_the_account(
    seeded_ensemble_batch: Callable[[str, int], Path], runs_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared account list spans every recipe, so the pick can be in
    none of the compared ones: a note, not an error."""
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
    recipe_pickers, row, _loading, _out = box.children
    baseline_picker, variant_picker = recipe_pickers.children
    baseline_picker.value = "baseline"
    variant_picker.value = "other"
    shown.clear()
    row.controls.account_picker.account_picker.value = "Asset_Renamed"
    headings = [str(item.data) for item in shown if isinstance(item, StyledMarkdown)]
    assert any("No account Asset_Renamed in `baseline`, `other`" in h for h in headings)
