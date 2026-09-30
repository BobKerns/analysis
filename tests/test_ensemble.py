"""ADR-006 Phases 2 and 4: resolving a recipe intent, and summarizing members."""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from analysis.sim.engine import run_scenario
from analysis.sim.ensemble import (
    EnsembleError,
    latest_ensemble_batch,
    load_ensemble,
    member_run_statuses,
    members_net_worth_by_month,
    resolve_member,
    summary,
)
from analysis.sim.loader import LoadResult


def test_resolve_member_amount_target_draws_within_bounds(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="45000",
        vary_target="amount", vary_distribution="uniform",
        vary_low="40000", vary_high="50000",
    )
    recipe = load_test_copy().intent_path
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


def test_resolve_member_date_target_draws_within_bounds(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    append_calendar_row(
        date="2027-06-30", kind="retire_scheduled", name="retire_bob_vary",
        scheduled_name="salary_bob",
        vary_target="date", vary_distribution="uniform",
        vary_low="2029-01-01", vary_high="2029-12-31",
    )
    recipe = load_test_copy().intent_path
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


def test_resolve_member_is_reproducible_for_same_seed_and_index(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="45000",
        vary_target="amount", vary_distribution="uniform",
        vary_low="40000", vary_high="50000",
    )
    recipe = load_test_copy().intent_path

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


def test_resolve_member_different_members_draw_differently(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="45000",
        vary_target="amount", vary_distribution="uniform",
        vary_low="0", vary_high="1000000",
    )
    recipe = load_test_copy().intent_path

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


def test_resolve_member_rejects_nonpositive_one_off_transfer_draw(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    """Belt-and-suspenders backstop: even if an invalid range somehow reached
    resolve_member (the loader should already have refused it at load time),
    a nonpositive draw for a one_off_transfer must never reach the engine
    silently -- it would reverse or omit the transfer."""
    append_calendar_row(
        date="2030-06-30", kind="one_off_transfer", name="transfer_vary",
        account_1="Asset_Checking", account_2="Asset_Brokerage_Joint", lot_2="default",
        amount="100000", tax_character="none",
        vary_target="amount", vary_distribution="uniform",
        vary_low="1", vary_high="2",
    )
    recipe = load_test_copy().intent_path
    # Simulate an invalid range slipping past load-time validation.
    con = sqlite3.connect(recipe)
    try:
        con.execute(
            "UPDATE calendar_event SET vary_low = '-50000', vary_high = '-40000' "
            "WHERE name = 'transfer_vary'"
        )
        con.commit()
    finally:
        con.close()

    with pytest.raises(EnsembleError, match="nonpositive one_off_transfer amount"):
        resolve_member(
            recipe, tmp_path / "member_0000.sqlite3",
            seed=1, member_index=0, member_count=1, recipe_tree_id="deadbeef",
        )


def test_resolve_member_ramp_interpolates_and_lands_on_target(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    # retire_alice/series/rate__Asset_Checking.csv already gives "Asset
    # Checking" a baseline rate (3.0% APR as of 2026-01-31); ramp from it.
    # (Deliberately a different date from prototype_data's own pre-existing
    # "Asset_Checking" set_rate row at 2027-01-31, which the calendar merge
    # now correctly keeps distinct from this named row rather than colliding
    # with it -- ADR-006's merge key includes `name`.)
    append_calendar_row(
        date="2027-06-30", kind="set_rate", name="checking_rate_ramp",
        series="Asset_Checking", amount="45000",
        vary_target="amount", vary_distribution="uniform",
        vary_low="60000", vary_high="60000", vary_ramp_months="3",
    )
    recipe = load_test_copy().intent_path
    member = resolve_member(
        recipe, tmp_path / "member_0000.sqlite3",
        seed=1, member_index=0, member_count=1, recipe_tree_id="deadbeef",
    )
    con = sqlite3.connect(member)
    try:
        rows = con.execute(
            "SELECT date, amount, vary_distribution FROM calendar_event "
            "WHERE series = 'Asset_Checking' AND kind = 'set_rate' AND date >= '2027-06-30' "
            "ORDER BY date"
        ).fetchall()
        # original row (baseline, unchanged value) + 3 ramp steps.
        assert len(rows) == 4
        assert all(vary_distribution is None for _, _, vary_distribution in rows)
        dates = [r[0] for r in rows]
        assert dates == sorted(dates)
        assert rows[0][0] == "2027-06-30"
        assert rows[-1][0] == "2027-09-30"
        assert rows[-1][1] == 60_000
        # monotonically increasing toward the target.
        amounts = [r[1] for r in rows]
        assert amounts == sorted(amounts)
    finally:
        con.close()


def test_resolve_member_ramp_starts_from_an_earlier_literal_rate_change(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    """prototype_data's retire_alice/calendar.csv already has a literal (not
    varying) `set_rate` row for 'Asset_Checking' at 2027-01-31 (45000).
    A ramp starting later must treat that as the current rate, not fall back
    to the load-time aux_table snapshot underneath it (30000) as though the
    intervening change never happened."""
    append_calendar_row(
        date="2027-06-30", kind="set_rate", name="checking_rate_ramp",
        series="Asset_Checking", amount="99999",
        vary_target="amount", vary_distribution="uniform",
        vary_low="90000", vary_high="90000", vary_ramp_months="1",
    )
    recipe = load_test_copy().intent_path
    member = resolve_member(
        recipe, tmp_path / "member_0000.sqlite3",
        seed=1, member_index=0, member_count=1, recipe_tree_id="deadbeef",
    )
    con = sqlite3.connect(member)
    try:
        baseline = con.execute(
            "SELECT amount FROM calendar_event WHERE name = 'checking_rate_ramp'"
        ).fetchone()[0]
        assert baseline == 45_000  # the 2027-01-31 literal change, not the 30000 aux baseline
    finally:
        con.close()


def test_resolve_member_ramp_without_baseline_rate_raises(
    append_calendar_row: Callable[..., None],
    update_account_value: Callable[..., None],
    load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    # A series with no consumer would fail the loader's own "nothing reads
    # this" check before ramping is even reached, so give it one: retarget
    # Asset_Brokerage_Joint's growth at this new series instead.
    update_account_value(
        "Asset_Brokerage_Joint", "default", series="never_before_seen_series",
    )
    append_calendar_row(
        date="2027-01-31", kind="set_rate", name="new_series_ramp",
        series="never_before_seen_series", amount="45000",
        vary_target="amount", vary_distribution="uniform",
        vary_low="60000", vary_high="60000", vary_ramp_months="3",
    )
    recipe = load_test_copy().intent_path
    with pytest.raises(EnsembleError, match="no existing"):
        resolve_member(
            recipe, tmp_path / "member_0000.sqlite3",
            seed=1, member_index=0, member_count=1, recipe_tree_id="deadbeef",
        )


def test_load_ensemble_and_summary_across_members(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    intent_dir: Path, runs_dir: Path,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="45000",
        vary_target="amount", vary_distribution="uniform",
        vary_low="0", vary_high="1000000",
    )
    recipe = load_test_copy().intent_path
    for i in range(3):
        member_intent = resolve_member(
            recipe, intent_dir / f"intent_member_{i}.sqlite3",
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


def test_load_ensemble_excludes_a_failed_member(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    intent_dir: Path, runs_dir: Path,
) -> None:
    """A member run file left behind with status='failed' (run_scenario
    writes that, then re-raises, rather than deleting the file) must not be
    summarized as though it were a succeeded member -- it would distort the
    mean/range and overstate the member count."""
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="45000",
        vary_target="amount", vary_distribution="uniform",
        vary_low="0", vary_high="1000000",
    )
    recipe = load_test_copy().intent_path
    for i in range(2):
        member_intent = resolve_member(
            recipe, intent_dir / f"intent_member_{i}.sqlite3",
            seed=1, member_index=i, member_count=2, recipe_tree_id="deadbeef",
        )
        run_scenario(
            member_intent, runs_dir / f"member_{i:04d}.sqlite3",
            run_by="test", allow_dirty_data=True,
        )
    # Stand in for a member whose run raised partway through: a real run
    # file (so it's discoverable by the same glob), status flipped to what
    # run_scenario itself would have left behind.
    shutil.copyfile(runs_dir / "member_0000.sqlite3", runs_dir / "member_0002.sqlite3")
    con = sqlite3.connect(runs_dir / "member_0002.sqlite3")
    try:
        con.execute("UPDATE simulation_run SET status = 'failed', failure_reason = 'boom'")
        con.commit()
    finally:
        con.close()

    statuses = member_run_statuses(runs_dir)
    assert statuses == {
        "member_0000": "succeeded", "member_0001": "succeeded", "member_0002": "failed",
    }

    connections = load_ensemble(runs_dir)
    try:
        assert set(connections) == {"member_0000", "member_0001"}
    finally:
        for con in connections.values():
            con.close()


def test_load_ensemble_raises_when_no_member_runs_found(tmp_path: Path) -> None:
    with pytest.raises(EnsembleError, match="no member run files"):
        load_ensemble(tmp_path / "empty")


def test_latest_ensemble_batch_picks_the_most_recent_timestamp(tmp_path: Path) -> None:
    recipe_dir = tmp_path / "runs" / "retire_alice"
    assert latest_ensemble_batch(recipe_dir) is None  # doesn't exist yet

    older = recipe_dir / "2026-01-01T000000Z"
    newer = recipe_dir / "2026-06-01T000000Z"
    empty = recipe_dir / "2026-12-01T000000Z"  # no member files -- not a real batch
    for d in (older, newer, empty):
        d.mkdir(parents=True)
    (older / "member_0000.sqlite3").touch()
    (newer / "member_0000.sqlite3").touch()

    assert latest_ensemble_batch(recipe_dir) == newer
