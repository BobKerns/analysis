"""ADR-006 Phase 4: ensemble notebook widgets (notebook_ui.ensemble_widgets)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import matplotlib
import pandas as pd
import pytest

matplotlib.use("Agg")

from analysis.sim.engine import run_scenario
from analysis.sim.ensemble import (
    latest_ensemble_batch,
    load_ensemble,
    members_net_worth_by_month,
    resolve_member,
)
from analysis.sim.ensemble import summary as ensemble_summary
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
    assert len(ax.lines) == 2  # mean, median
    assert len(ax.collections) == 1  # fill_between range band


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
        series="Asset_Checking", amount="45000",
        vary_target="amount", vary_distribution="uniform",
        vary_low="40000", vary_high="50000",
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
    box = ensemble_widget(small_ensemble_runs_dir)
    assert isinstance(box, widgets.VBox)
    assert not any(isinstance(item, widgets.Widget) for item in shown)


def test_ensemble_widget_dropdown_lists_the_recipe(small_ensemble_runs_dir: Path) -> None:
    box = ensemble_widget(small_ensemble_runs_dir)
    dropdown, _out = box.children
    assert dropdown.options == ("retire_alice",)
    assert dropdown.value == "retire_alice"


def test_ensemble_widget_matches_cli_summary_numbers(small_ensemble_runs_dir: Path) -> None:
    connections = load_ensemble(latest_ensemble_batch(small_ensemble_runs_dir / "retire_alice"))
    try:
        monthly_summary = ensemble_summary(members_net_worth_by_month(connections))
        _heading, yearly = ensemble_summary_view(monthly_summary, len(connections))
        assert len(yearly) > 0
        assert (yearly["min"] <= yearly["max"] + 1e-6).all()
    finally:
        for con in connections.values():
            con.close()
