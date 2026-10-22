"""ADR-006 Phases 2 and 4: resolving a recipe intent, and summarizing members;
ADR-007 Phase 1: OAT sensitivity members."""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from analysis.sim.engine import run_scenario
from analysis.sim.ensemble import (
    EnsembleError,
    latest_ensemble_batch,
    load_ensemble,
    member_run_statuses,
    members_net_worth_by_month,
    oat_plan,
    percentiles,
    read_batch_manifest,
    resolve_member,
    summary,
    summary_with_percentiles,
    write_batch_manifest,
)
from analysis.sim.loader import LoadResult


def test_resolve_member_amount_target_draws_within_bounds(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="uniform",
        vary_low="4%", vary_high="5%",
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
        date="2027-06-30", kind="retire_scheduled", name="retire_bill_vary",
        scheduled_name="salary_bill",
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
            "WHERE name = 'retire_bill_vary'"
        ).fetchone()
        drawn_date, vary_target, vary_distribution = row
        assert "2029-01-01" <= drawn_date <= "2029-12-31"
        assert vary_target == "date" and vary_distribution is None
    finally:
        con.close()


def test_resolve_member_zero_uses_the_uniform_midpoint_not_a_draw(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    """ADR-006 §8: member 0 is always deterministic -- a plausible, in-bounds
    "base case" -- so widening the range doesn't change its resolved value,
    only the spread of members 1+."""
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="uniform",
        vary_low="4%", vary_high="5%",
    )
    recipe = load_test_copy().intent_path
    member = resolve_member(
        recipe, tmp_path / "member_0000.sqlite3",
        seed=1, member_index=0, member_count=3, recipe_tree_id="deadbeef",
    )
    con = sqlite3.connect(member)
    try:
        amount = con.execute(
            "SELECT amount FROM calendar_event WHERE name = 'checking_rate_vary'"
        ).fetchone()[0]
        assert amount == 45_000
    finally:
        con.close()


def test_resolve_member_zero_uses_the_truncated_normal_median(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="truncated_normal",
        vary_median="4.5%", vary_sd="0.2%", vary_low="4%", vary_high="5%",
    )
    recipe = load_test_copy().intent_path
    member = resolve_member(
        recipe, tmp_path / "member_0000.sqlite3",
        seed=1, member_index=0, member_count=3, recipe_tree_id="deadbeef",
    )
    con = sqlite3.connect(member)
    try:
        amount = con.execute(
            "SELECT amount FROM calendar_event WHERE name = 'checking_rate_vary'"
        ).fetchone()[0]
        assert amount == 45_000
    finally:
        con.close()


def test_resolve_member_zero_date_target_uses_the_midpoint(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    from datetime import date as _date
    append_calendar_row(
        date="2027-06-30", kind="retire_scheduled", name="retire_bill_vary",
        scheduled_name="salary_bill",
        vary_target="date", vary_distribution="uniform",
        vary_low="2029-01-01", vary_high="2029-12-31",
    )
    recipe = load_test_copy().intent_path
    member = resolve_member(
        recipe, tmp_path / "member_0000.sqlite3",
        seed=1, member_index=0, member_count=3, recipe_tree_id="deadbeef",
    )
    expected = _date.fromordinal(round(
        (_date(2029, 1, 1).toordinal() + _date(2029, 12, 31).toordinal()) / 2
    )).isoformat()
    con = sqlite3.connect(member)
    try:
        drawn_date = con.execute(
            "SELECT date FROM calendar_event WHERE name = 'retire_bill_vary'"
        ).fetchone()[0]
        assert drawn_date == expected
    finally:
        con.close()


def test_resolve_member_is_reproducible_for_same_seed_and_index(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="uniform",
        vary_low="4%", vary_high="5%",
    )
    recipe = load_test_copy().intent_path

    def _drawn_amount(member_path: Path) -> int:
        # member_index=1, not 0: member 0 is always the deterministic
        # central value (ADR-006 §8), which would make this trivially
        # "reproducible" without exercising member_rng at all.
        member = resolve_member(
            recipe, member_path,
            seed=7, member_index=1, member_count=2, recipe_tree_id="deadbeef",
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
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="uniform",
        vary_low="0%", vary_high="100%",
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
        amount="1000.00", tax_character="none",
        vary_target="amount", vary_distribution="uniform",
        vary_low="0.01", vary_high="0.02",
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
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="uniform",
        vary_low="6%", vary_high="6%", vary_ramp_months="3",
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
        series="Asset_Checking", amount="9.9999%",
        vary_target="amount", vary_distribution="uniform",
        vary_low="9%", vary_high="9%", vary_ramp_months="1",
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
    retire_alice_csv: Callable[[str], Path],
    tmp_path: Path,
) -> None:
    # The series needs a file (#204) and a reader to load; its file starts
    # after the ramp, so there's no rate yet to ramp from.
    retire_alice_csv("series/rate__never_before_seen_series.csv").write_text(
        "effective_date,annual_rate\n2035-01-31,3%\n"
    )
    update_account_value(
        "Asset_Brokerage_Joint", "default", series="never_before_seen_series",
    )
    append_calendar_row(
        date="2027-01-31", kind="set_rate", name="new_series_ramp",
        series="never_before_seen_series", amount="4.5%",
        vary_target="amount", vary_distribution="uniform",
        vary_low="6%", vary_high="6%", vary_ramp_months="3",
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
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="uniform",
        vary_low="0%", vary_high="100%",
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
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="uniform",
        vary_low="0%", vary_high="100%",
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


def test_latest_ensemble_batch_recognizes_a_manifest_only_batch(tmp_path: Path) -> None:
    """A batch where every member failed to resolve has no member_*.sqlite3
    files at all -- the manifest alone must still make it discoverable."""
    recipe_dir = tmp_path / "runs" / "retire_alice"
    batch = recipe_dir / "2026-06-01T000000Z"
    batch.mkdir(parents=True)
    write_batch_manifest(batch, member_count=3, failures=[
        (0, tmp_path / "member_0000.sqlite3", "boom"),
        (1, tmp_path / "member_0001.sqlite3", "boom"),
        (2, tmp_path / "member_0002.sqlite3", "boom"),
    ])
    assert latest_ensemble_batch(recipe_dir) == batch


def test_member_run_statuses_includes_resolution_failures_from_the_manifest(
    tmp_path: Path,
) -> None:
    batch = tmp_path / "batch"
    batch.mkdir()
    (batch / "member_0000.sqlite3").touch()  # a real run file, no manifest entry
    con = sqlite3.connect(batch / "member_0000.sqlite3")
    con.execute("CREATE TABLE simulation_run(status TEXT)")
    con.execute("INSERT INTO simulation_run VALUES ('succeeded')")
    con.commit()
    con.close()

    write_batch_manifest(batch, member_count=2, failures=[
        (1, tmp_path / "intent" / "member_0001.sqlite3", "resolve_member blew up"),
    ])

    statuses = member_run_statuses(batch)
    assert statuses == {"member_0000": "succeeded", "member_0001": "failed_to_resolve"}


def test_read_batch_manifest_round_trips(tmp_path: Path) -> None:
    write_batch_manifest(tmp_path, member_count=5, failures=[
        (2, tmp_path / "member_0002.sqlite3", "went negative"),
    ])
    manifest = read_batch_manifest(tmp_path)
    assert manifest is not None
    assert manifest["member_count"] == 5
    assert manifest["failures"] == [
        {"index": 2, "intent_path": str(tmp_path / "member_0002.sqlite3"), "error": "went negative"},
    ]


def test_read_batch_manifest_none_when_absent(tmp_path: Path) -> None:
    assert read_batch_manifest(tmp_path) is None


# --- ADR-007 Phase 1: OAT sensitivity members ------------------------------


def _two_varying_rows(append_calendar_row: Callable[..., None]) -> None:
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="uniform",
        vary_low="4%", vary_high="5%",
    )
    append_calendar_row(
        date="2027-06-30", kind="retire_scheduled", name="retire_bill_vary",
        scheduled_name="salary_bill",
        vary_target="date", vary_distribution="uniform",
        vary_low="2029-01-01", vary_high="2029-12-31",
    )


def _calendar_and_draws(member: Path) -> tuple[list[tuple], list[tuple], str]:
    con = sqlite3.connect(member)
    try:
        calendar = con.execute(
            "SELECT name, date, amount, vary_distribution FROM calendar_event ORDER BY id"
        ).fetchall()
        draws = con.execute(
            "SELECT calendar_event_name, drawn_value, quantile FROM ensemble_draw "
            "ORDER BY calendar_event_name"
        ).fetchall()
        mode = con.execute("SELECT mode FROM ensemble_manifest").fetchone()[0]
    finally:
        con.close()
    return calendar, draws, mode


@pytest.mark.usefixtures("without_prototype_defaults")
def test_resolve_member_override_lands_on_quantile_others_central(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    _two_varying_rows(append_calendar_row)
    recipe = load_test_copy().intent_path
    member = resolve_member(
        recipe, tmp_path / "member_0001.sqlite3",
        seed=1, member_index=1, member_count=5, recipe_tree_id="deadbeef",
        overrides={"checking_rate_vary": 0.1}, mode="sensitivity",
    )
    _, draws, mode = _calendar_and_draws(member)
    assert mode == "sensitivity"
    con = sqlite3.connect(member)
    try:
        distributions = con.execute(
            "SELECT DISTINCT distribution FROM ensemble_draw"
        ).fetchall()
    finally:
        con.close()
    assert distributions == [("uniform",)]  # recorded, since resolving clears it
    retire_mid = date.fromordinal(round(
        (date(2029, 1, 1).toordinal() + date(2029, 12, 31).toordinal()) / 2
    )).isoformat()
    assert draws == [
        ("checking_rate_vary", "41000", 0.1),
        ("retire_bill_vary", retire_mid, None),
    ]


def test_resolve_member_date_override_uses_quantile(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    _two_varying_rows(append_calendar_row)
    recipe = load_test_copy().intent_path
    member = resolve_member(
        recipe, tmp_path / "member_0004.sqlite3",
        seed=1, member_index=4, member_count=5, recipe_tree_id="deadbeef",
        overrides={"retire_bill_vary": 1.0}, mode="sensitivity",
    )
    _, draws, _ = _calendar_and_draws(member)
    assert ("retire_bill_vary", "2029-12-31", 1.0) in draws


def test_oat_base_member_matches_monte_carlo_member_zero(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    """ADR-007 §3: the OAT base *is* ADR-006's member 0, row for row."""
    _two_varying_rows(append_calendar_row)
    recipe = load_test_copy().intent_path
    mc = resolve_member(
        recipe, tmp_path / "mc" / "member_0000.sqlite3",
        seed=1, member_index=0, member_count=5, recipe_tree_id="deadbeef",
    )
    oat = resolve_member(
        recipe, tmp_path / "oat" / "member_0000.sqlite3",
        seed=1, member_index=0, member_count=5, recipe_tree_id="deadbeef",
        overrides={}, mode="sensitivity",
    )
    mc_calendar, mc_draws, mc_mode = _calendar_and_draws(mc)
    oat_calendar, oat_draws, oat_mode = _calendar_and_draws(oat)
    assert mc_calendar == oat_calendar
    assert mc_draws == oat_draws
    assert (mc_mode, oat_mode) == ("monte_carlo", "sensitivity")


def test_resolve_member_with_overrides_never_draws(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    """A nonzero member index with overrides still resolves the un-overridden
    rows centrally -- never from member_rng."""
    _two_varying_rows(append_calendar_row)
    recipe = load_test_copy().intent_path
    a = resolve_member(
        recipe, tmp_path / "a.sqlite3", seed=1, member_index=3, member_count=5,
        recipe_tree_id="deadbeef", overrides={"retire_bill_vary": 0.9}, mode="sensitivity",
    )
    b = resolve_member(
        recipe, tmp_path / "b.sqlite3", seed=999, member_index=7, member_count=5,
        recipe_tree_id="deadbeef", overrides={"retire_bill_vary": 0.9}, mode="sensitivity",
    )
    assert _calendar_and_draws(a)[1] == _calendar_and_draws(b)[1]


def test_resolve_member_ramp_at_quantile_lands_on_perturbed_target(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    append_calendar_row(
        date="2027-06-30", kind="set_rate", name="checking_rate_ramp",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="uniform",
        vary_low="5%", vary_high="7%", vary_ramp_months="3",
    )
    recipe = load_test_copy().intent_path
    member = resolve_member(
        recipe, tmp_path / "member_0001.sqlite3",
        seed=1, member_index=1, member_count=3, recipe_tree_id="deadbeef",
        overrides={"checking_rate_ramp": 0.9}, mode="sensitivity",
    )
    con = sqlite3.connect(member)
    try:
        last = con.execute(
            "SELECT date, amount FROM calendar_event WHERE series = 'Asset_Checking' "
            "AND kind = 'set_rate' AND date >= '2027-06-30' ORDER BY date DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    assert last == ("2027-09-30", 68_000)


def test_resolve_member_rejects_unknown_override(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    _two_varying_rows(append_calendar_row)
    recipe = load_test_copy().intent_path
    with pytest.raises(EnsembleError, match="no_such_row"):
        resolve_member(
            recipe, tmp_path / "m.sqlite3", seed=1, member_index=1, member_count=3,
            recipe_tree_id="deadbeef", overrides={"no_such_row": 0.1}, mode="sensitivity",
        )


def test_resolve_member_sensitivity_needs_no_seed_and_records_none(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    _two_varying_rows(append_calendar_row)
    recipe = load_test_copy().intent_path
    member = resolve_member(
        recipe, tmp_path / "m.sqlite3", seed=None, member_index=1, member_count=5,
        recipe_tree_id="deadbeef", overrides={"checking_rate_vary": 0.1}, mode="sensitivity",
    )
    con = sqlite3.connect(member)
    try:
        assert con.execute("SELECT seed FROM ensemble_manifest").fetchone() == (None,)
    finally:
        con.close()
    with pytest.raises(EnsembleError, match="seed"):
        resolve_member(
            recipe, tmp_path / "mc.sqlite3", seed=None, member_index=1, member_count=5,
            recipe_tree_id="deadbeef",
        )


def test_resolve_member_rejects_unknown_mode(
    load_test_copy: Callable[[], LoadResult], tmp_path: Path,
) -> None:
    with pytest.raises(EnsembleError, match="mode"):
        resolve_member(
            load_test_copy().intent_path, tmp_path / "m.sqlite3", seed=1,
            member_index=0, member_count=1, recipe_tree_id="deadbeef", mode="bogus",
        )


def test_oat_plan_layout() -> None:
    plan = oat_plan(["b_row", "a_row"], (0.1, 0.9))
    assert plan == [{}, {"a_row": 0.1}, {"a_row": 0.9}, {"b_row": 0.1}, {"b_row": 0.9}]
    assert len(oat_plan(["a", "b", "c"], (0.1, 0.25, 0.75, 0.9))) == 1 + 3 * 4


def test_oat_plan_only_restricts_rows() -> None:
    assert oat_plan(["a", "b", "c"], (0.1, 0.9), only=["b"]) == [{}, {"b": 0.1}, {"b": 0.9}]
    with pytest.raises(EnsembleError, match="nope"):
        oat_plan(["a", "b"], (0.1, 0.9), only=["nope"])


def test_sensitivity_batch_manifest_round_trips(tmp_path: Path) -> None:
    write_batch_manifest(
        tmp_path, member_count=5, failures=[], mode="sensitivity",
        quantiles=(0.1, 0.9), rows=["a", "b"],
    )
    manifest = read_batch_manifest(tmp_path)
    assert manifest is not None
    assert manifest["mode"] == "sensitivity"
    assert manifest["quantiles"] == [0.1, 0.9]
    assert manifest["rows"] == ["a", "b"]


def test_read_batch_manifest_defaults_old_batches_to_monte_carlo(tmp_path: Path) -> None:
    (tmp_path / "batch_manifest.json").write_text('{"member_count": 3, "failures": []}')
    manifest = read_batch_manifest(tmp_path)
    assert manifest is not None
    assert manifest["mode"] == "monte_carlo"


def test_latest_ensemble_batch_filters_by_mode(tmp_path: Path) -> None:
    recipe_dir = tmp_path / "runs" / "retire_alice"
    mc = recipe_dir / "2026-01-01T000000Z"
    oat = recipe_dir / "2026-06-01T000000Z"
    legacy = recipe_dir / "2025-06-01T000000Z"  # no manifest: Monte Carlo
    for d in (mc, oat, legacy):
        d.mkdir(parents=True)
        (d / "member_0000.sqlite3").touch()
    write_batch_manifest(mc, member_count=1, failures=[])
    write_batch_manifest(oat, member_count=1, failures=[], mode="sensitivity",
                         quantiles=(0.1, 0.9), rows=[])
    assert latest_ensemble_batch(recipe_dir) == oat
    assert latest_ensemble_batch(recipe_dir, mode="monte_carlo") == mc
    assert latest_ensemble_batch(recipe_dir, mode="sensitivity") == oat
    mc_manifest = mc / "batch_manifest.json"
    mc_manifest.unlink()
    assert latest_ensemble_batch(recipe_dir, mode="monte_carlo") == mc  # legacy-style


# --- #31: shaped truncated_normal and triangular rows -------------------------------


def test_resolve_member_zero_lands_on_a_skewed_rows_authored_median(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    """The base case is exactly the authored median even with skew and
    asymmetric bounds (#31)."""
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="truncated_normal",
        vary_median="4.5%", vary_sd="0.5%", vary_low="4%", vary_high="7%",
        vary_skew="0.7",
    )
    recipe = load_test_copy().intent_path
    member = resolve_member(
        recipe, tmp_path / "member_0000.sqlite3",
        seed=1, member_index=0, member_count=3, recipe_tree_id="deadbeef",
    )
    con = sqlite3.connect(member)
    try:
        assert con.execute(
            "SELECT amount FROM calendar_event WHERE name = 'checking_rate_vary'"
        ).fetchone()[0] == 45_000
    finally:
        con.close()


def test_resolve_member_triangular_date_row(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    """A triangular date: member 0 at its median (between the mode and the
    long tail), drawn members inside the bounds, distribution recorded."""
    append_calendar_row(
        date="2030-12-31", kind="retire_scheduled", name="retire_bill_vary",
        scheduled_name="salary_bill",
        vary_target="date", vary_distribution="triangular",
        vary_low="2030-06-30", vary_mode="2030-12-31", vary_high="2031-12-31",
    )
    recipe = load_test_copy().intent_path
    dates = []
    for i in range(6):
        member = resolve_member(
            recipe, tmp_path / f"member_{i:04d}.sqlite3",
            seed=9, member_index=i, member_count=6, recipe_tree_id="deadbeef",
        )
        con = sqlite3.connect(member)
        try:
            dates.append(con.execute(
                "SELECT date FROM calendar_event WHERE name = 'retire_bill_vary'"
            ).fetchone()[0])
            if i == 0:
                assert con.execute(
                    "SELECT distribution FROM ensemble_draw"
                ).fetchone() == ("triangular",)
        finally:
            con.close()
    assert "2030-12-31" < dates[0] < "2031-06-30"  # median: past the mode, toward the long side
    assert all("2030-06-30" <= d <= "2031-12-31" for d in dates)


def test_percentiles_names_columns_by_percent_and_interpolates() -> None:
    members = pd.DataFrame(
        [[0.0, 10, 20, 30, 40, 50]],
        index=pd.date_range("2026-01-31", periods=1, freq="ME"),
    )
    result = percentiles(members)
    assert list(result.columns) == ["p20", "p40", "p60", "p80"]
    assert result.iloc[0].tolist() == pytest.approx([10.0, 20, 30, 40])
    assert list(percentiles(members, (0.25,)).columns) == ["p25"]
    assert list(percentiles(members, (0.05, 0.95)).columns) == ["p05", "p95"]


def test_summary_with_percentiles_reports_p05_to_p95_instead_of_min_and_max() -> None:
    members = pd.DataFrame(
        [[float(v) for v in range(0, 101, 5)]],  # 0, 5, ..., 100
        index=pd.date_range("2026-01-31", periods=1, freq="ME"),
    )
    result = summary_with_percentiles(members)
    assert list(result.columns) == [
        "mean", "median", "std", "p05", "p20", "p40", "p60", "p80", "p95",
    ]
    row = result.iloc[0]
    assert [row["p05"], row["p20"], row["p80"], row["p95"]] == pytest.approx([5.0, 20, 80, 95])
    assert list(summary_with_percentiles(members, (0.8, 0.2)).columns)[3:] == ["p20", "p80"]
