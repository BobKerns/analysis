"""#560: a scenario's tree counts only the price rows its load reads."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import git

from analysis.sim import provenance


def _commit(repo: git.Repo, message: str) -> None:
    repo.git.add(A=True)
    actor = git.Actor("t", "t@t")
    repo.index.commit(message, author=actor, committer=actor)


def _prices(data: Path, *rows: str) -> None:
    path = data / "prices" / "security_price__VTI.csv"
    path.write_text("effective_date,price\n" + "".join(f"{row}\n" for row in rows))


def test_prices_after_the_start_leave_the_tree_unchanged(
    test_data_dir: Path, commit_test_data: Callable[[], Path],
) -> None:
    # retire_alice starts 2026-05-31.
    _prices(test_data_dir, "2026-04-30,300.00")
    repo = git.Repo(commit_test_data())
    before = provenance.data_stamps(test_data_dir, "retire_alice").tree_id

    _prices(test_data_dir, "2026-04-30,300.00", "2026-05-31,310.00", "2026-06-12,320.00")
    _commit(repo, "fetch")
    after = provenance.data_stamps(test_data_dir, "retire_alice")
    assert not after.dirty
    assert after.tree_id != before  # 2026-05-31 is on the start: a lot is valued at it

    before = after.tree_id
    _prices(test_data_dir, "2026-04-30,300.00", "2026-05-31,310.00", "2026-06-12,321.00",
            "2026-06-30,330.00")
    _commit(repo, "fetch again")
    assert provenance.data_stamps(test_data_dir, "retire_alice").tree_id == before

    _prices(test_data_dir, "2026-04-30,301.00", "2026-05-31,310.00", "2026-06-30,330.00")
    _commit(repo, "a correction before the start")
    assert provenance.data_stamps(test_data_dir, "retire_alice").tree_id != before


def test_a_new_price_file_changes_the_tree_even_when_empty_before_the_start(
    test_data_dir: Path, commit_test_data: Callable[[], Path],
) -> None:
    repo = git.Repo(commit_test_data())
    before = provenance.data_stamps(test_data_dir, "retire_alice").tree_id
    _prices(test_data_dir, "2026-06-30,330.00")
    _commit(repo, "a new symbol")
    # The header alone is still a file the load reads (and validates).
    assert provenance.data_stamps(test_data_dir, "retire_alice").tree_id != before


def test_a_scenario_starts_where_its_lineage_last_sets_it(test_data_dir: Path) -> None:
    from analysis.sim.lineage import scenario_start

    assert scenario_start(test_data_dir / "scenarios", "retire_alice") == "2026-05-31"
    text = ("effective_date,price\n2026-05-31,1\n2026-06-01,2\nbad\n"
            "2026-07-01,abc\n2026-02-30,5\n2026-08-01\n")
    # Rows the loader rejects stay, wherever they are dated: they fail the load.
    assert provenance._rows_read(text, "2026-05-31") == (
        "effective_date,price\n2026-05-31,1\nbad\n2026-07-01,abc\n2026-02-30,5\n2026-08-01\n")


def test_a_bad_row_after_the_start_changes_the_tree(
    test_data_dir: Path, commit_test_data: Callable[[], Path],
) -> None:
    _prices(test_data_dir, "2026-04-30,300.00")
    repo = git.Repo(commit_test_data())
    before = provenance.data_stamps(test_data_dir, "retire_alice").tree_id
    _prices(test_data_dir, "2026-04-30,300.00", "2026-07-01,abc")
    _commit(repo, "a typo")
    assert provenance.data_stamps(test_data_dir, "retire_alice").tree_id != before
