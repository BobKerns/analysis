"""#298: an ensemble batch's series store (sim/series_db.py)."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest

from analysis.sim.ensemble import (
    EnsembleError,
    load_ensemble,
    member_pairs,
    members_balance_by_month,
)
from analysis.sim.series_db import (
    EnsembleSeries,
    build_series_db,
    open_series,
    pair_members,
    series_db_current,
    series_db_path,
)
from analysis.sim.types import DollarMode


def _every_choice(store: EnsembleSeries) -> list[tuple[str | None, DollarMode, str | None, bool]]:
    choices = [(None, mode, None, after_tax) for mode in DollarMode for after_tax in (False, True)]
    choices += [(account, mode, None, False) for account in store.accounts() for mode in DollarMode]
    choices += [(account, mode, lot, False)
                for account, lots in store.lots_by_account().items()
                for lot in lots for mode in DollarMode]
    return choices


@pytest.mark.slow
def test_sim_run_writes_a_store_matching_the_member_files_exactly(mc_batch: Path) -> None:
    """Every series the notebook and `sim ensemble` read comes out of the
    store to the last bit, for every account, lot and dollars."""
    assert series_db_current(mc_batch)
    store = open_series(mc_batch)
    connections = load_ensemble(mc_batch)
    try:
        assert store.stems == list(connections)
        choices = _every_choice(store)
        assert any(lot for _account, _mode, lot, _after_tax in choices)
        assert any(account and account.startswith("Income_") for account, *_ in choices)
        for account, mode, lot, after_tax in choices:
            got = store.frame(account, mode, lot, after_tax=after_tax)
            want = members_balance_by_month(connections, account, mode, lot, after_tax=after_tax)
            pd.testing.assert_frame_equal(got, want.astype(float), check_exact=True,
                                          check_freq=False, obj=f"{account} {lot} {mode}")
        assert store.reserve() is None
        assert pair_members(store.member_ids(), store.member_ids()) == member_pairs(
            connections, connections)
    finally:
        for con in connections.values():
            con.close()


def test_store_is_built_on_first_open_and_rebuilt_when_a_member_changes(
    seeded_ensemble_batch: Callable[[str, int], Path],
) -> None:
    """A batch from before the store, or a member file changed since it was
    written, is read afresh rather than trusted."""
    batch = seeded_ensemble_batch("recipe", 1)
    assert not series_db_path(batch).exists()
    store = open_series(batch)
    assert len(store) == 3
    assert "Asset_Checking" in store.accounts()
    assert series_db_current(batch)

    member = batch / "member_0001.sqlite3"
    with sqlite3.connect(member) as con:
        con.execute("UPDATE account SET name = 'Asset_Renamed' WHERE name = 'Asset_Checking'")
    stat = member.stat()
    os.utime(member, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert not series_db_current(batch)
    assert "Asset_Renamed" in open_series(batch).accounts()


def test_reading_members_does_not_make_the_store_stale(
    seeded_ensemble_batch: Callable[[str, int], Path],
) -> None:
    batch = seeded_ensemble_batch("recipe", 1)
    build_series_db(batch)
    connections = load_ensemble(batch)
    try:
        members_balance_by_month(connections, None, DollarMode.CPI_CONSTANT, after_tax=True)
    finally:
        for con in connections.values():
            con.close()
    assert series_db_current(batch)


def test_store_from_other_code_is_rebuilt(
    seeded_ensemble_batch: Callable[[str, int], Path],
) -> None:
    batch = seeded_ensemble_batch("recipe", 1)
    build_series_db(batch)
    with sqlite3.connect(series_db_path(batch)) as con:
        con.execute("UPDATE batch SET code_hash = 'older'")
    assert not series_db_current(batch)
    open_series(batch)
    assert series_db_current(batch)


def test_store_says_what_it_lacks_like_the_member_files(
    seeded_ensemble_batch: Callable[[str, int], Path],
) -> None:
    store = open_series(seeded_ensemble_batch("recipe", 1))
    with pytest.raises(EnsembleError, match="no account 'Nope'"):
        store.frame("Nope")
    with pytest.raises(EnsembleError, match="no lot 'x'"):
        store.frame("Asset_Checking", lot="x")
    with pytest.raises(EnsembleError, match="after-tax"):
        store.frame("Asset_Checking", after_tax=True)
    with pytest.raises(EnsembleError, match="needs its account"):
        store.frame(None, lot="x")


def test_members_pair_only_on_one_seed(
    seeded_ensemble_batch: Callable[[str, int], Path],
) -> None:
    left = open_series(seeded_ensemble_batch("left", 1)).member_ids()
    same = open_series(seeded_ensemble_batch("same", 1)).member_ids()
    other = open_series(seeded_ensemble_batch("other", 2)).member_ids()
    pairs, reason = pair_members(left, same)
    assert len(pairs) == 3 and reason is None
    assert pair_members(left, other) == (
        [], "the recipes have different seeds, so their members drew different futures")


def test_recipes_pair_though_their_first_succeeded_members_differ(
    seeded_ensemble_batch: Callable[[str, int], Path], runs_dir: Path,
) -> None:
    """A member file records its own seed, derived from the recipe's and its
    index, so recipes are compared at an index both have succeeded, not at
    each one's first success."""
    from analysis.sim.notebook_ui.ensemble_widgets import RecipeSeeds

    seeded_ensemble_batch("baseline", 1)
    with sqlite3.connect(seeded_ensemble_batch("child", 1) / "member_0000.sqlite3") as con:
        con.execute("UPDATE simulation_run SET status = 'failed'")
    seeded_ensemble_batch("other", 2)
    seeds = RecipeSeeds(runs_dir, ["baseline", "child", "other"])
    assert seeds.paired_with("baseline") == ["child"]
    assert seeds.paired_with("other") == []


def test_controls_read_the_last_finished_batch_while_one_builds(
    seeded_ensemble_batch: Callable[[str, int], Path], runs_dir: Path,
) -> None:
    """Building the notebook controls doesn't wait for a background build
    (#300), so a recipe's newest batch may have no finished member yet: a
    member file whose run hasn't written its tables, under a provisional
    manifest. The batch before it stands in."""
    from analysis.sim.ensemble import member_run_statuses
    from analysis.sim.notebook_ui.ensemble_widgets import (
        RecipeSeeds, recipe_accounts, settled_batch,
    )

    seeded_ensemble_batch("baseline", 1)
    finished = seeded_ensemble_batch("child", 1)
    building = runs_dir / "child" / "2026-01-02T000000Z"
    building.mkdir()
    (building / "batch_manifest.json").write_text('{"mode": "monte_carlo"}')
    (building / "member_0000.sqlite3").touch()
    assert member_run_statuses(building) == {"member_0000": "running"}
    assert settled_batch(runs_dir, "child") == (finished, [0, 1, 2])
    assert RecipeSeeds(runs_dir, ["baseline", "child"]).paired_with("baseline") == ["child"]
    accounts, lots, years = recipe_accounts(runs_dir, ["child"])
    assert accounts and "child" in lots and years


def test_member_statuses_still_raise_on_a_damaged_member(tmp_path: Path) -> None:
    """Only a member whose tables aren't written yet reads as running."""
    from analysis.sim.ensemble import member_run_statuses

    with sqlite3.connect(tmp_path / "member_0000.sqlite3") as con:
        con.execute("CREATE TABLE simulation_run (id INTEGER)")  # no status column
    with pytest.raises(sqlite3.OperationalError, match="no such column"):
        member_run_statuses(tmp_path)


def test_a_batch_with_no_succeeded_member_gets_no_store(tmp_path: Path) -> None:
    """An empty store would be trusted, and read as an ensemble of no futures."""
    from analysis.sim.cli import _write_series_db

    _write_series_db(tmp_path, [], 0)
    assert not series_db_path(tmp_path).exists()
    with pytest.raises(EnsembleError, match="no succeeded member"):
        build_series_db(tmp_path)
