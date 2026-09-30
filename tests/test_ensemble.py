"""ADR-006 Phases 2 and 4: resolving a recipe intent, and summarizing members."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from analysis.sim.engine import run_scenario
from analysis.sim.ensemble import (
    EnsembleError,
    load_ensemble,
    members_net_worth_by_month,
    resolve_member,
    summary,
)
from analysis.sim.loader import load_scenario
from analysis.sim.registries import CALENDAR_HEADER

REPO_ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = REPO_ROOT / "prototype_data"


def _append_calendar_vary_row(repo: Path, **fields: str) -> None:
    cal = repo / "scenarios" / "retire_alice" / "calendar.csv"
    df = pd.read_csv(cal, dtype=str, keep_default_na=False)
    df = df.reindex(columns=CALENDAR_HEADER, fill_value="")
    df.loc[len(df)] = [fields.get(column, "") for column in CALENDAR_HEADER]
    df.to_csv(cal, index=False)


def _load_recipe(repo: Path, tmp_path: Path) -> Path:
    return load_scenario(
        repo, "retire_alice", intent_dir=tmp_path / "intent", loaded_by="test",
        allow_dirty_data=True,
    ).intent_path


def test_resolve_member_amount_target_draws_within_bounds(tmp_path: Path) -> None:
    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)
    _append_calendar_vary_row(
        repo, date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset Checking", amount="45000",
        vary_target="amount", vary_distribution="uniform",
        vary_low="40000", vary_high="50000",
    )
    recipe = _load_recipe(repo, tmp_path)
    member = resolve_member(
        recipe, tmp_path / "member_0000.sqlite3",
        seed=1, member_index=0, member_count=3, recipe_tree_id="deadbeef",
    )
    con = sqlite3.connect(member)
    try:
        row = con.execute(
            "SELECT amount, vary_target, vary_distribution FROM calendar_event "
            "WHERE name = 'checking_rate_vary'"
        ).fetchone()
        amount, vary_target, vary_distribution = row
        assert 40_000 <= amount <= 50_000
        assert vary_target == "amount" and vary_distribution is None

        manifest = con.execute(
            "SELECT ensemble_name, member_index, member_count, recipe_tree_id "
            "FROM ensemble_manifest"
        ).fetchone()
        assert manifest == ("retire_alice", 0, 3, "deadbeef")

        draw = con.execute(
            "SELECT calendar_event_name, vary_target, drawn_value FROM ensemble_draw"
        ).fetchone()
        assert draw == ("checking_rate_vary", "amount", str(amount))
    finally:
        con.close()


def test_resolve_member_date_target_draws_within_bounds(tmp_path: Path) -> None:
    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)
    _append_calendar_vary_row(
        repo, date="2027-06-30", kind="retire_scheduled", name="retire_bob_vary",
        scheduled_name="salary_bob",
        vary_target="date", vary_distribution="uniform",
        vary_low="2029-01-01", vary_high="2029-12-31",
    )
    recipe = _load_recipe(repo, tmp_path)
    member = resolve_member(
        recipe, tmp_path / "member_0000.sqlite3",
        seed=1, member_index=0, member_count=3, recipe_tree_id="deadbeef",
    )
    con = sqlite3.connect(member)
    try:
        row = con.execute(
            "SELECT date, vary_target, vary_distribution FROM calendar_event "
            "WHERE name = 'retire_bob_vary'"
        ).fetchone()
        drawn_date, vary_target, vary_distribution = row
        assert "2029-01-01" <= drawn_date <= "2029-12-31"
        assert vary_target == "date" and vary_distribution is None
    finally:
        con.close()


def test_resolve_member_is_reproducible_for_same_seed_and_index(tmp_path: Path) -> None:
    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)
    _append_calendar_vary_row(
        repo, date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset Checking", amount="45000",
        vary_target="amount", vary_distribution="uniform",
        vary_low="40000", vary_high="50000",
    )
    recipe = _load_recipe(repo, tmp_path)

    def _drawn_amount(member_path: Path) -> int:
        member = resolve_member(
            recipe, member_path,
            seed=7, member_index=0, member_count=1, recipe_tree_id="deadbeef",
        )
        con = sqlite3.connect(member)
        try:
            return con.execute(
                "SELECT amount FROM calendar_event WHERE name = 'checking_rate_vary'"
            ).fetchone()[0]
        finally:
            con.close()

    assert _drawn_amount(tmp_path / "a.sqlite3") == _drawn_amount(tmp_path / "b.sqlite3")


def test_resolve_member_different_members_draw_differently(tmp_path: Path) -> None:
    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)
    _append_calendar_vary_row(
        repo, date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset Checking", amount="45000",
        vary_target="amount", vary_distribution="uniform",
        vary_low="0", vary_high="1000000",
    )
    recipe = _load_recipe(repo, tmp_path)

    amounts = set()
    for i in range(3):
        member = resolve_member(
            recipe, tmp_path / f"member_{i}.sqlite3",
            seed=7, member_index=i, member_count=3, recipe_tree_id="deadbeef",
        )
        con = sqlite3.connect(member)
        try:
            amounts.add(con.execute(
                "SELECT amount FROM calendar_event WHERE name = 'checking_rate_vary'"
            ).fetchone()[0])
        finally:
            con.close()
    assert len(amounts) == 3


def test_resolve_member_ramp_interpolates_and_lands_on_target(tmp_path: Path) -> None:
    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)
    # retire_alice/series/rate__Asset_Checking.csv already gives "Asset
    # Checking" a baseline rate (3.0% APR as of 2026-01-31); ramp from it.
    _append_calendar_vary_row(
        repo, date="2027-01-31", kind="set_rate", name="checking_rate_ramp",
        series="Asset Checking", amount="45000",
        vary_target="amount", vary_distribution="uniform",
        vary_low="60000", vary_high="60000", vary_ramp_months="3",
    )
    recipe = _load_recipe(repo, tmp_path)
    member = resolve_member(
        recipe, tmp_path / "member_0000.sqlite3",
        seed=1, member_index=0, member_count=1, recipe_tree_id="deadbeef",
    )
    con = sqlite3.connect(member)
    try:
        rows = con.execute(
            "SELECT date, amount, vary_distribution FROM calendar_event "
            "WHERE series = 'Asset Checking' AND kind = 'set_rate' AND date >= '2027-01-31' "
            "ORDER BY date"
        ).fetchall()
        # original row (baseline, unchanged value) + 3 ramp steps.
        assert len(rows) == 4
        assert all(vary_distribution is None for _, _, vary_distribution in rows)
        dates = [r[0] for r in rows]
        assert dates == sorted(dates)
        assert rows[0][0] == "2027-01-31"
        assert rows[-1][0] == "2027-04-30"
        assert rows[-1][1] == 60_000
        # monotonically increasing toward the target.
        amounts = [r[1] for r in rows]
        assert amounts == sorted(amounts)
    finally:
        con.close()


def test_resolve_member_ramp_without_baseline_rate_raises(tmp_path: Path) -> None:
    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)
    _append_calendar_vary_row(
        repo, date="2027-01-31", kind="set_rate", name="new_series_ramp",
        series="Never Before Seen Series", amount="45000",
        vary_target="amount", vary_distribution="uniform",
        vary_low="60000", vary_high="60000", vary_ramp_months="3",
    )
    recipe = _load_recipe(repo, tmp_path)
    with pytest.raises(EnsembleError, match="no existing"):
        resolve_member(
            recipe, tmp_path / "member_0000.sqlite3",
            seed=1, member_index=0, member_count=1, recipe_tree_id="deadbeef",
        )


def test_load_ensemble_and_summary_across_members(tmp_path: Path) -> None:
    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)
    _append_calendar_vary_row(
        repo, date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset Checking", amount="45000",
        vary_target="amount", vary_distribution="uniform",
        vary_low="0", vary_high="1000000",
    )
    recipe = _load_recipe(repo, tmp_path)
    runs_dir = tmp_path / "runs"
    for i in range(3):
        member_intent = resolve_member(
            recipe, tmp_path / f"intent_member_{i}.sqlite3",
            seed=1, member_index=i, member_count=3, recipe_tree_id="deadbeef",
        )
        run_scenario(
            member_intent, runs_dir / f"member_{i:04d}.sqlite3",
            run_by="test", allow_dirty_data=True,
        )

    connections = load_ensemble(runs_dir)
    try:
        assert set(connections) == {"member_0000", "member_0001", "member_0002"}
        frame = members_net_worth_by_month(connections)
        assert list(frame.columns) == ["member_0000", "member_0001", "member_0002"]
        table = summary(frame)
        assert list(table.columns) == ["mean", "median", "std", "min", "max"]
        # allow float rounding: before the varied row fires, all 3 members
        # are identical, and mean-of-equal-floats isn't always bit-exact.
        tol = 1e-6
        assert (table["min"] <= table["mean"] + tol).all()
        assert (table["mean"] <= table["max"] + tol).all()
        assert (table["min"] <= table["median"] + tol).all()
        assert (table["median"] <= table["max"] + tol).all()
    finally:
        for con in connections.values():
            con.close()


def test_load_ensemble_raises_when_no_member_runs_found(tmp_path: Path) -> None:
    with pytest.raises(EnsembleError, match="no member run files"):
        load_ensemble(tmp_path / "empty")
