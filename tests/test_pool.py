"""ADR-009: pooled ensembles (#292)."""

from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.sim import pool as pl
from analysis.sim.draws_db import component_frames, components, draws_frames
from analysis.sim.loader import LoadError, latest_ensemble_batch, load_scenario


def _sim(*args: str) -> Result:
    return CliRunner().invoke(main, ["sim", *args])


def _write_meta(data: Path, name: str, meta: dict, **row: str) -> None:
    """Rewrite scenario ``name``'s scenario.csv meta_json (and any ``row`` cells)."""
    path = data / "scenarios" / name / "scenario.csv"
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header, rows = list(reader.fieldnames or []), list(reader)
    rows[0].update(meta_json=json.dumps(meta), **row)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)


def _meta(data: Path, name: str) -> dict:
    with (data / "scenarios" / name / "scenario.csv").open(newline="", encoding="utf-8") as fh:
        return json.loads(next(csv.DictReader(fh))["meta_json"])


# ---------- member picking ----------

@pytest.mark.parametrize("weights", [(15, 30, 55), (1, 1), (1, 2, 3, 4), (5, 95)])
@pytest.mark.parametrize("base", [0, 1])
def test_assign_members_keeps_every_prefix_within_one_of_the_weights(
    weights: tuple[int, ...], base: int,
) -> None:
    size = 200
    picked = pl.assign_members(weights, size, base)
    assert picked[0] == base
    total = sum(weights)
    counts = [0] * len(weights)
    for j, c in enumerate(picked):
        counts[c] += 1
        for k, w in enumerate(weights):
            assert abs(counts[k] - w * (j + 1) / total) < 1, (j, counts)


def test_assign_members_gives_exact_counts_when_the_weights_divide_the_size() -> None:
    assert Counter(pl.assign_members([15, 30, 55], 20, 0)) == {0: 3, 1: 6, 2: 11}


def test_equal_shares_round_to_whole_micros_adding_to_100_percent() -> None:
    shares = pl._equal_shares(3)
    assert sum(shares) == 1_000_000 and max(shares) - min(shares) <= 1


# ---------- definition and checks ----------

def test_pool_create_writes_a_pool_that_reads_back(test_data_dir: Path, pool_scenarios: str) -> None:
    scenarios = test_data_dir / "scenarios"
    pool = pl.read_pool(scenarios, pool_scenarios)
    assert pool.components == ("e_a", "e_b")
    assert [pool.weight_text(c) for c in pool.components] == ["25%", "75%"]
    assert pool.base == "e_a"
    assert pl.pool_issues(test_data_dir, pool_scenarios) == []
    assert pl.pool_size(test_data_dir, pool) == 4  # the smallest component's
    assert (scenarios / pool_scenarios / "README.md").is_file()
    assert sorted(p.name for p in (scenarios / pool_scenarios).iterdir()) == ["README.md", "scenario.csv"]


@pytest.mark.parametrize(("spec", "issue"), [
    ({"components": {"e_a": "25%", "e_b": "70%"}}, "add up to 100%"),
    ({"components": ["e_a"]}, "at least two components"),
    ({"components": ["e_a", "e_b"], "base": "retire_alice"}, "is not one of its components"),
    ({"components": ["e_a", "e_b"], "weights": [1, 2]}, "unknown key"),
    ({"components": ["e_a", "mix"]}, "one of its own components"),
    ({"components": ["e_a", "retire_alice"]}, "is not an ensemble"),
    ({"components": ["e_a", "nope"]}, "component nope not found"),
])
def test_pool_issues_name_what_is_wrong(
    test_data_dir: Path, pool_scenarios: str, spec: dict, issue: str,
) -> None:
    _write_meta(test_data_dir, pool_scenarios, {"pool": spec})
    found = pl.pool_issues(test_data_dir, pool_scenarios)
    assert any(issue in i for i in found), found


def test_a_pool_takes_no_parent_dates_or_seed(test_data_dir: Path, pool_scenarios: str) -> None:
    _write_meta(test_data_dir, pool_scenarios, _meta(test_data_dir, pool_scenarios),
                parent_scenario="retire_alice", seed="3")
    found = pl.pool_issues(test_data_dir, pool_scenarios)
    assert any("no parent_scenario" in i for i in found), found
    assert any("takes its seed" in i for i in found), found


def test_a_pool_bigger_than_a_component_is_refused(test_data_dir: Path, pool_scenarios: str) -> None:
    _write_meta(test_data_dir, pool_scenarios, {**_meta(test_data_dir, pool_scenarios), "ensemble_size": 5})
    assert any("e_a" in i and "4" in i for i in pl.pool_issues(test_data_dir, pool_scenarios))


def test_a_pool_folder_holds_nothing_else(test_data_dir: Path, pool_scenarios: str) -> None:
    (test_data_dir / "scenarios" / pool_scenarios / "calendar.csv").write_text("date\n", encoding="utf-8")
    assert any("a pool holds only" in i for i in pl.pool_issues(test_data_dir, pool_scenarios))


def test_nothing_loads_a_pool_or_builds_on_one(
    test_data_dir: Path, pool_scenarios: str, intent_dir: Path,
    derive_scenario: Callable[..., Path],
) -> None:
    with pytest.raises(LoadError, match="is a pool"):
        load_scenario(test_data_dir, pool_scenarios, intent_dir=intent_dir, allow_dirty_data=True)
    derive_scenario("on_pool", pool_scenarios)
    with pytest.raises(LoadError, match="can't be a parent or a mix-in"):
        load_scenario(test_data_dir, "on_pool", intent_dir=intent_dir, allow_dirty_data=True)


def test_sim_check_checks_a_pool(test_data_dir: Path, pool_scenarios: str) -> None:
    ok = _sim("check", pool_scenarios, "--data-dir", str(test_data_dir))
    assert ok.exit_code == 0, ok.output
    _write_meta(test_data_dir, pool_scenarios, {"pool": {"components": ["e_a"]}})
    bad = _sim("check", pool_scenarios, "--data-dir", str(test_data_dir))
    assert bad.exit_code != 0 and "at least two components" in bad.output


# ---------- editing ----------

def test_pool_edit_replaces_components_and_sets_size_and_base(
    test_data_dir: Path, pool_scenarios: str,
) -> None:
    result = _sim("ensemble", "pool", "edit", pool_scenarios, "e_a", "e_b", "--size", "3",
                  "--base", "e_b", "--data-dir", str(test_data_dir))
    assert result.exit_code == 0, result.output
    assert _meta(test_data_dir, pool_scenarios) == {
        "pool": {"components": ["e_a", "e_b"], "base": "e_b"}, "ensemble_size": 3}
    result = _sim("ensemble", "pool", "edit", pool_scenarios, "--size", "", "--base", "",
                  "--data-dir", str(test_data_dir))
    assert result.exit_code == 0, result.output
    assert _meta(test_data_dir, pool_scenarios) == {"pool": {"components": ["e_a", "e_b"]}}


def test_pool_edit_with_no_options_asks_for_every_field(test_data_dir: Path, pool_scenarios: str) -> None:
    # components, base and size kept; a reserve; '-' for the generated description
    answers = "\n\n\n50000\n-\n"
    result = CliRunner().invoke(main, ["sim", "ensemble", "pool", "edit", pool_scenarios,
                                       "--data-dir", str(test_data_dir)], input=answers)
    assert result.exit_code == 0, result.output
    assert "reserve" in result.output and "description" in result.output
    assert _meta(test_data_dir, pool_scenarios) == {
        "pool": {"components": {"e_a": "25%", "e_b": "75%"}, "base": "e_a"}, "reserve": 50000}


def test_a_blank_pool_description_restores_the_generated_one(test_data_dir: Path, pool_scenarios: str) -> None:
    data = ("--data-dir", str(test_data_dir))
    assert _sim("ensemble", "pool", "edit", pool_scenarios, "--description", "mine", *data).exit_code == 0
    assert _sim("ensemble", "pool", "edit", pool_scenarios, "--description", "", *data).exit_code == 0
    with (test_data_dir / "scenarios" / pool_scenarios / "scenario.csv").open(newline="", encoding="utf-8") as fh:
        assert next(csv.DictReader(fh))["description"] == "Pooled from e_a + e_b"


def test_the_readme_names_the_pools_components_after_an_edit(test_data_dir: Path, pool_scenarios: str) -> None:
    result = _sim("ensemble", "pool", "edit", pool_scenarios, "e_a", "e_b", "--data-dir", str(test_data_dir))
    assert result.exit_code == 0, result.output
    readme = (test_data_dir / "scenarios" / pool_scenarios / "README.md").read_text(encoding="utf-8")
    assert "Pools: `e_a` equal share, `e_b` equal share (ADR-009).\n" in readme


def test_a_component_with_no_seed_is_refused(
    test_data_dir: Path, pool_scenarios: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A lineage that never reaches a seed (the base template sets one, so
    # the test data's always do): its members couldn't be drawn.
    from analysis.sim import scenario

    real = scenario.ensemble_settings

    def unseeded(data_dir: Path, name: str):  # noqa: ANN202
        seed, size = real(data_dir, name)
        return (None, size) if name == "e_a" else (seed, size)

    monkeypatch.setattr(scenario, "ensemble_settings", unseeded)
    found = pl.pool_issues(test_data_dir, pool_scenarios)
    assert any("component e_a has no seed" in i for i in found), found


def test_a_rail_tag_only_an_overridden_row_had_is_unknown() -> None:
    rows = [{"name": "Food", "tags": "need:food"}, {"name": "", "x": "1"}, {"name": "Food", "tags": ""}]
    assert pl._by_name(rows) == [{"name": "", "x": "1"}, {"name": "Food", "tags": ""}]


def test_pool_edit_that_breaks_the_pool_writes_nothing(test_data_dir: Path, pool_scenarios: str) -> None:
    before = _meta(test_data_dir, pool_scenarios)
    result = _sim("ensemble", "pool", "edit", pool_scenarios, "e_a=50%", "e_b=40%",
                  "--data-dir", str(test_data_dir))
    assert result.exit_code != 0 and "add up to 100%" in result.output
    assert _meta(test_data_dir, pool_scenarios) == before


def test_renaming_a_component_renames_it_in_the_pool(test_data_dir: Path, pool_scenarios: str) -> None:
    result = _sim("ensemble", "rename", "e_b", "e_child", "--data-dir", str(test_data_dir))
    assert result.exit_code == 0, result.output
    assert _meta(test_data_dir, pool_scenarios)["pool"]["components"] == {"e_a": "25%", "e_child": "75%"}
    readme = (test_data_dir / "scenarios" / pool_scenarios / "README.md").read_text(encoding="utf-8")
    assert "Pooled from e_a + e_child\n" in readme
    assert "Pools: `e_a` 25%, `e_child` 75% (ADR-009).\n" in readme
    assert pl.pool_issues(test_data_dir, pool_scenarios) == []


def test_a_pool_renames_like_any_scenario(test_data_dir: Path, pool_scenarios: str) -> None:
    result = _sim("ensemble", "rename", pool_scenarios, "mix2", "--data-dir", str(test_data_dir))
    assert result.exit_code == 0, result.output
    assert pl.pool_issues(test_data_dir, "mix2") == []


def test_deleting_a_component_is_refused_while_a_pool_names_it(test_data_dir: Path, pool_scenarios: str) -> None:
    result = _sim("ensemble", "delete", "e_b", "--yes", "--data-dir", str(test_data_dir))
    assert result.exit_code != 0 and "component of pool(s) mix" in result.output
    assert (test_data_dir / "scenarios" / "e_b").is_dir()


def test_scenario_edit_points_a_pool_at_pool_edit(test_data_dir: Path, pool_scenarios: str) -> None:
    result = _sim("ensemble", "edit", pool_scenarios, "--description", "x", "--data-dir", str(test_data_dir))
    assert result.exit_code != 0 and "sim ensemble pool edit" in result.output


# ---------- building ----------

@pytest.fixture
def pool_batch(test_data_dir: Path, pool_scenarios: str, state_dir: Path) -> Path:
    """``mix`` run (both components run first), returning its batch dir."""
    result = _sim("run", pool_scenarios, "--data-dir", str(test_data_dir), "--allow-dirty-data")
    assert result.exit_code == 0, result.output
    assert "component e_a: running" in result.output
    assert "pool of 2, seed 7: e_a 1 (25%), e_b 3 (75%)" in result.output, result.output
    batch = latest_ensemble_batch(state_dir / "runs" / pool_scenarios)
    assert batch is not None
    return batch


def test_a_component_member_that_failed_mid_run_is_a_pool_failure(
    test_data_dir: Path, pool_scenarios: str, tmp_path: Path,
) -> None:
    from analysis.sim.ensemble import read_batch_manifest, write_batch_manifest

    pool = pl.read_pool(test_data_dir / "scenarios", pool_scenarios)
    sources = {}
    for component in pool.components:
        batch = tmp_path / "runs" / component / "b"
        batch.mkdir(parents=True)
        for j in range(4):
            (batch / f"member_{j:04d}.sqlite3").write_bytes(b"")
        failed = [(1, batch / "member_0001.sqlite3", "boom")] if component == "e_b" else []
        write_batch_manifest(batch, member_count=4, failures=failed)
        sources[component] = batch
    runs_batch, intent_batch = tmp_path / "runs" / "mix" / "b", tmp_path / "intent" / "mix" / "b"
    runs_batch.mkdir(parents=True)
    intent_batch.mkdir(parents=True)
    failures = pl.build_pool_batch(pool, 4, sources, runs_batch, intent_batch, tmp_path / "intent")
    # member 1 is e_b's (e_a 25%: member 0 only), and failed there
    assert failures == [(1, intent_batch / "member_0001.sqlite3", "boom")]
    assert (runs_batch / "member_0001.sqlite3").is_file()
    assert [f["index"] for f in (read_batch_manifest(runs_batch) or {})["failures"]] == [1]


def test_a_pools_meta_is_its_base_components_when_member_0_failed(tmp_path: Path) -> None:
    import numpy as np
    import pandas as pd

    from analysis.sim.ensemble import write_batch_manifest
    from analysis.sim.series_db import MemberSeries, series_db_path, write_series_db

    batch = tmp_path / "runs" / "mix" / "b"
    batch.mkdir(parents=True)
    write_batch_manifest(batch, member_count=3, failures=[(0, batch / "member_0000.sqlite3", "boom")],
                         pool={"base": "e_a", "member_components": ["e_a", "e_b", "e_a"]})
    months = pd.date_range("2027-01-31", periods=1, freq="ME")

    def member(j: int, reserve: int) -> MemberSeries:
        return MemberSeries(f"member_{j:04d}", j, 7, months, np.ones(1), np.ones(1), [],
                            json.dumps({"reserve": reserve}))

    # member 1 is e_b's and comes first; member 2 is the base component's
    write_series_db(batch, [member(1, 2), member(2, 1)])
    with sqlite3.connect(series_db_path(batch)) as con:
        assert json.loads(con.execute("SELECT meta_json FROM batch").fetchone()[0]) == {"reserve": 1}


@pytest.mark.slow
def test_a_pool_batch_links_each_member_from_its_component(pool_batch: Path, state_dir: Path) -> None:
    record = pl.pool_record(pool_batch)
    assert record is not None
    assert record["member_components"] == ["e_a", "e_b", "e_b", "e_b"]
    assert pl.shared_seed(record) == 7
    for j, component in enumerate(record["member_components"]):
        source = next(s for s in record["sources"] if s["component"] == component)
        original = state_dir / "runs" / component / source["batch"] / f"member_{j:04d}.sqlite3"
        assert (pool_batch / original.name).stat().st_ino == original.stat().st_ino
    # The draws database keeps each component's nodes apart: only e_b draws the gift.
    frames = draws_frames(pool_batch)
    assert components(frames) == ["e_a", "e_b"]
    nodes = frames["node"].groupby("component")["name"].apply(set)
    assert "gift" in nodes["e_b"] and "gift" not in nodes["e_a"]
    e_b = component_frames(frames, "e_b")
    assert set(e_b["member"]["member_index"]) == {1, 2, 3}
    assert set(e_b["draw_check"]["node"]) >= {"gift", "checking_rate_vary"}
    with sqlite3.connect(pool_batch / "draws.sqlite3") as con:
        assert con.execute("SELECT ensemble_name, master_seed FROM batch").fetchone() == ("mix", 7)


@pytest.mark.slow
def test_a_pool_reports_and_pairs_with_its_components_parent(
    pool_batch: Path, test_data_dir: Path, pool_scenarios: str,
) -> None:
    summary = _sim("ensemble", "summary", pool_scenarios, "--data-dir", str(test_data_dir))
    assert summary.exit_code == 0, summary.output
    assert "pool of 2, seed 7: e_a 1 (25%), e_b 3 (75%)" in summary.output
    assert "worst 5% (1) by lowest CPI-constant value:" in summary.output
    compare = _sim("ensemble", "compare", "e_a", pool_scenarios, "--data-dir", str(test_data_dir))
    assert compare.exit_code == 0, compare.output
    assert "paired: 4 future(s)" in compare.output


@pytest.mark.slow
def test_a_pool_reuses_components_that_are_up_to_date(
    test_data_dir: Path, pool_scenarios: str, state_dir: Path,
    commit_test_data: Callable[[], Path], stamp_runs: Callable[[Path], None],
) -> None:
    from analysis.sim import freshness

    commit_test_data()
    args = ("--data-dir", str(test_data_dir), "--allow-dirty-code")
    first = _sim("run", pool_scenarios, *args)
    assert first.exit_code == 0, first.output
    runs = state_dir / "runs"
    stamp_runs(runs)
    again = _sim("run", pool_scenarios, *args)
    assert again.exit_code == 0, again.output
    assert "component e_a: unchanged" in again.output and "component e_b: unchanged" in again.output
    code = freshness.code_tree()
    assert freshness.unchanged_run(test_data_dir, runs, pool_scenarios, code) is not None
    # Its own definition is part of what it was built from.
    _write_meta(test_data_dir, pool_scenarios, {"pool": {"components": {"e_a": "50%", "e_b": "50%"}}})
    commit_test_data()
    assert freshness.unchanged_run(test_data_dir, runs, pool_scenarios, code) is None
    rebuilt = _sim("run", pool_scenarios, "--rebuild", *args)
    assert rebuilt.exit_code == 0, rebuilt.output
    assert "component e_a: running" in rebuilt.output
    assert "e_a 2 (50%), e_b 2 (50%)" in rebuilt.output


def test_rebuild_is_for_pools_only(test_data_dir: Path, pool_scenarios: str) -> None:
    result = _sim("run", "e_a", "--rebuild", "--data-dir", str(test_data_dir), "--allow-dirty-data")
    assert result.exit_code != 0 and "--rebuild" in result.output


@pytest.mark.slow
def test_sim_all_builds_a_pool_after_its_components_without_running_them_twice(
    test_data_dir: Path, pool_scenarios: str,
) -> None:
    result = _sim("all", "--data-dir", str(test_data_dir), "--allow-dirty-data")
    assert result.exit_code == 0, result.output
    out = result.output
    assert out.index("ensemble e_b") < out.index("pool mix")
    assert "component e_a: running" not in out and "component e_b: running" not in out


# ---------- guard rails ----------

_FOUR_PERCENT = ("--measure", "withdrawals", "--side", "above", "--basis", "initial_portfolio",
                 "--threshold", "4%")


def test_a_pool_has_its_base_components_rails_and_its_own(test_data_dir: Path, pool_scenarios: str) -> None:
    data = ("--data-dir", str(test_data_dir))
    added = _sim("guard-rail", "add", "wd_cap", *_FOUR_PERCENT, "--scenario", "e_a", *data)
    assert added.exit_code == 0, added.output
    own = _sim("guard-rail", "add", "pool_budget", "--measure", "spending", "--side", "above",
               "--basis", "cpi_constant", "--threshold", "90000", "--scenario", pool_scenarios, *data)
    assert own.exit_code == 0, own.output
    listed = _sim("guard-rail", "list", "--scenario", pool_scenarios, *data)
    assert listed.exit_code == 0, listed.output
    assert {"four_percent", "wd_cap", "pool_budget"} <= set(listed.output.split())
    assert pl.pool_issues(test_data_dir, pool_scenarios) == []


def test_a_pools_rail_naming_an_unknown_tag_fails_its_check(
    test_data_dir: Path, pool_scenarios: str,
) -> None:
    (test_data_dir / "scenarios" / pool_scenarios / "guard_rails.csv").write_text(
        "name,measure,select,side,basis,threshold\n"
        "fun,spending,purpose:nope,above,cpi_constant,1000.00\n", encoding="utf-8")
    assert any("purpose:nope" in i for i in pl.pool_issues(test_data_dir, pool_scenarios))


@pytest.mark.slow
def test_a_pools_report_judges_its_member_0_by_the_pools_rails(
    pool_batch: Path, test_data_dir: Path, pool_scenarios: str,
) -> None:
    data = ("--data-dir", str(test_data_dir))
    added = _sim("guard-rail", "add", "wd_cap", *_FOUR_PERCENT, "--scenario", pool_scenarios, *data)
    assert added.exit_code == 0, added.output
    report = _sim("report", "guard-rails", "--scenario", pool_scenarios, *data)
    assert report.exit_code == 0, report.output
    assert "wd_cap" in report.output and "(mix, run member_0000.sqlite3)" in report.output
