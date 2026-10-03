"""ADR-008: correlated parameters -- Phase 0: latent factors, the correlation
columns, their validation across scenario inheritance, and load warnings."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim import scenario
from analysis.sim.engine import run_scenario
from analysis.sim.loader import LoadError, LoadResult, load_scenario
from analysis.sim.registries import CALENDAR_HEADER


def _factor(append_calendar_row: Callable[..., None], name: str, **extra: str) -> None:
    append_calendar_row(kind="latent_factor", name=name, **extra)


def _varying_rate(append_calendar_row: Callable[..., None], name: str, **extra: str) -> None:
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name=name, series="Asset_Checking", amount="45000",
        vary_target="amount", vary_distribution="uniform", vary_low="40000", vary_high="50000",
        **extra,
    )


def _issues(load: Callable[[], LoadResult]) -> list[str]:
    with pytest.raises(LoadError) as ei:
        load()
    return ei.value.issues


def test_two_level_tree_loads_and_is_stored(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    """factor 'economy' -> factor 'inflation' -> two rows, plus one row hung
    directly on 'economy'; a factor's blank date becomes the scenario start."""
    _factor(append_calendar_row, "economy", description="the broad economy")
    _factor(append_calendar_row, "inflation", vary_correlate_with="economy",
            vary_correlation="0.7")
    _varying_rate(append_calendar_row, "rate_a", vary_correlate_with="inflation",
                  vary_correlation="0.8")
    append_calendar_row(
        date="2028-02-29", kind="set_rate", name="rate_b", series="Asset_Checking",
        amount="45000", vary_target="amount", vary_distribution="truncated_normal",
        vary_median="45000", vary_sd="3000", vary_correlate_with="economy",
        vary_correlation="-0.4",
    )
    result = load_test_copy()
    assert result.warnings == []
    con = sqlite3.connect(result.intent_path)
    try:
        rows = dict(
            (name, (kind, date, parent, rho)) for name, kind, date, parent, rho in con.execute(
                "SELECT name, kind, date, vary_correlate_with, vary_correlation "
                "FROM calendar_event WHERE name IS NOT NULL"
            )
        )
        start = con.execute("SELECT start FROM scenario").fetchone()[0]
    finally:
        con.close()
    assert rows["economy"] == ("latent_factor", start, None, None)
    assert rows["inflation"] == ("latent_factor", start, "economy", 0.7)
    assert rows["rate_a"][2:] == ("inflation", 0.8)
    assert rows["rate_b"][2:] == ("economy", -0.4)


def test_two_factors_in_one_scenario_both_survive_the_merge(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    """Factors have blank event fields; keyed by those, the second would
    collide with the first (PR #36 review). They're keyed by name instead."""
    _factor(append_calendar_row, "f1")
    _factor(append_calendar_row, "f2")
    _varying_rate(append_calendar_row, "r1", vary_correlate_with="f1", vary_correlation="0.5")
    append_calendar_row(
        date="2028-02-29", kind="set_rate", name="r2", series="Asset_Checking", amount="45000",
        vary_target="amount", vary_distribution="uniform", vary_low="40000", vary_high="50000",
        vary_correlate_with="f2", vary_correlation="0.5",
    )
    con = sqlite3.connect(load_test_copy().intent_path)
    try:
        factors = sorted(r[0] for r in con.execute(
            "SELECT name FROM calendar_event WHERE kind = 'latent_factor'"
        ))
    finally:
        con.close()
    assert factors == ["f1", "f2"]


def test_child_scenario_overrides_and_uses_a_parent_factor(
    append_calendar_row: Callable[..., None], test_data_dir: Path,
    test_scenarios_dir: Path, intent_dir: Path,
) -> None:
    """A child scenario hangs a row on its parent's factor, and its own
    same-named factor row replaces the parent's (merged by name)."""
    _factor(append_calendar_row, "economy", description="parent's")
    scenario.create_scenario(test_data_dir, "variant", "retire_alice")
    child = pd.DataFrame([
        dict.fromkeys(CALENDAR_HEADER, "") | {
            "kind": "latent_factor", "name": "economy", "description": "child's",
        },
        dict.fromkeys(CALENDAR_HEADER, "") | {
            "date": "2027-02-28", "kind": "set_rate", "name": "rate_a",
            "series": "Asset_Checking", "amount": "45000", "vary_target": "amount",
            "vary_distribution": "uniform", "vary_low": "40000", "vary_high": "50000",
            "vary_correlate_with": "economy", "vary_correlation": "0.6",
        },
    ])
    child.to_csv(test_scenarios_dir / "variant" / "calendar.csv", index=False)
    result = load_scenario(test_data_dir, "variant", intent_dir=intent_dir,
                           scenarios_dir=test_scenarios_dir, allow_dirty_data=True)
    assert result.warnings == []  # the parent's factor now has a dependent
    con = sqlite3.connect(result.intent_path)
    try:
        factors = con.execute(
            "SELECT name, description FROM calendar_event WHERE kind = 'latent_factor'"
        ).fetchall()
    finally:
        con.close()
    assert factors == [("economy", "child's")]


@pytest.mark.parametrize(("rows", "message"), [
    ([("rate_a", {"vary_correlate_with": "nowhere", "vary_correlation": "0.5"})],
     "names no varying row or latent_factor"),
    ([("rate_a", {"vary_correlate_with": "rate_a", "vary_correlation": "0.5"})],
     "correlation cycle rate_a -> rate_a"),
    ([("rate_a", {"vary_correlate_with": "rate_b", "vary_correlation": "0.5"}),
      ("rate_b", {"vary_correlate_with": "rate_a", "vary_correlation": "0.5"})],
     "correlation cycle"),
    ([("rate_a", {"vary_correlate_with": "f", "vary_correlation": "1.5"})],
     "must be between -1 and 1"),
    ([("rate_a", {"vary_correlate_with": "f", "vary_correlation": "lots"})],
     "is not a number"),
    ([("rate_a", {"vary_correlate_with": "f"})], "go together"),
    ([("rate_a", {"vary_correlation": "0.5"})], "go together"),
])
def test_correlation_rejections(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    rows: list[tuple[str, dict[str, str]]], message: str,
) -> None:
    _factor(append_calendar_row, "f")
    for i, (name, extra) in enumerate(rows):
        append_calendar_row(
            date=f"202{7 + i}-02-28", kind="set_rate", name=name, series="Asset_Checking",
            amount="45000", vary_target="amount", vary_distribution="uniform",
            vary_low="40000", vary_high="50000", **extra,
        )
    assert any(message in issue for issue in _issues(load_test_copy)), _issues(load_test_copy)


def test_correlation_on_a_literal_row_is_rejected(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    _factor(append_calendar_row, "f")
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="fixed", series="Asset_Checking",
        amount="45000", vary_correlate_with="f", vary_correlation="0.5",
    )
    assert any("only valid on a varying row" in i for i in _issues(load_test_copy))


@pytest.mark.parametrize(("extra", "message"), [
    ({"vary_distribution": "uniform"}, "has no vary_distribution"),
    ({"amount": "100"}, "has no amount"),
    ({"series": "cpi"}, "has no series"),
])
def test_latent_factor_carries_nothing_but_a_name(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    extra: dict[str, str], message: str,
) -> None:
    _factor(append_calendar_row, "f", **extra)
    _varying_rate(append_calendar_row, "rate_a", vary_correlate_with="f", vary_correlation="0.5")
    assert any(message in i for i in _issues(load_test_copy))


def test_latent_factor_needs_a_name(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    append_calendar_row(kind="latent_factor")
    assert any("needs a name" in i for i in _issues(load_test_copy))


def test_unused_factor_is_a_warning_not_an_error(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    _factor(append_calendar_row, "someday")
    result = load_test_copy()
    assert len(result.warnings) == 1 and "someday" in result.warnings[0]
    assert "no dependents" in result.warnings[0]


def test_engine_treats_a_factor_as_inert(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    """An ordinary (non-recipe) scenario with a factor runs normally, and
    the factor isn't reported as an unsupported calendar kind."""
    _factor(append_calendar_row, "someday")
    run_path = tmp_path / "run.sqlite3"
    run_scenario(load_test_copy().intent_path, run_path, run_by="test", allow_dirty_data=True)
    con = sqlite3.connect(run_path)
    try:
        status, run_args = con.execute("SELECT status, run_args_json FROM simulation_run").fetchone()
    finally:
        con.close()
    assert status == "succeeded"
    assert "latent_factor" not in json.loads(run_args or "{}").get("skipped_calendar_kinds", {})


def test_cli_prints_a_warning_once(
    append_calendar_row: Callable[..., None], test_data_dir: Path,
    intent_dir: Path, runs_dir: Path,
) -> None:
    """`sim check` prints WARN: lines and exits 0 (ADR-004 §6); `sim load`,
    which goes on to run (and so loads again), prints each warning once."""
    _factor(append_calendar_row, "someday")
    runner = CliRunner()
    check = runner.invoke(main, ["sim", "check", "retire_alice", "--data-dir", str(test_data_dir)])
    assert check.exit_code == 0, check.output
    assert check.output.count("WARN:") == 1 and "someday" in check.output
    load = runner.invoke(main, [
        "sim", "load", "retire_alice", "--data-dir", str(test_data_dir),
        "--intent-dir", str(intent_dir), "--runs-dir", str(runs_dir), "--allow-dirty-data",
    ])
    assert load.exit_code == 0, load.output
    assert load.output.count("WARN:") == 1
