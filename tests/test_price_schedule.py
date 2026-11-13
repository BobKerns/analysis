"""#560: the builder fetches prices on a schedule, on a branch merged into main
(#588: by merge commits both ways, then main is pushed when strictly ahead)."""

from __future__ import annotations

import plistlib
import subprocess
from collections.abc import Callable, Generator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import git
import pytest
from click.testing import CliRunner

from analysis.sim import builder, price_schedule
from analysis.sim.cli import sim_group_command
from analysis.sim.prices import PriceError

NY = price_schedule.MARKET


class FakeProvider:
    """Daily closes of 100 + the day of the month; BAD isn't listed."""

    name = "fake"

    def __init__(self, bump: Decimal = Decimal(0)) -> None:
        self.bump = bump

    def closes(self, symbol: str, start: date, end: date) -> list[tuple[date, Decimal]]:
        if symbol == "BAD":
            raise PriceError("no such symbol")
        day, out = start, []
        while day <= end:
            if day.weekday() < 5:
                out.append((day, Decimal(100 + day.day) + self.bump))
            day += timedelta(days=1)
        return out


@pytest.fixture
def fetching(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(price_schedule.SWITCH_ENV, raising=False)




@pytest.fixture
def priced(commit_files: Callable[..., str], data_repo_sim: Path) -> Generator[tuple[Path, git.Repo]]:
    """The data repo with one symbol already priced: (sim dir, repo)."""
    with git.Repo(data_repo_sim.parent) as repo:
        commit_files(repo, {"sim/prices/security_price__VTI.csv":
                       "effective_date,price\n2025-12-31,300.00\n"}, "VTI")
        yield data_repo_sim, repo


def _worktree(data: Path) -> Path:
    return builder.BuilderPaths.for_data(data).prices_worktree


NOW = datetime(2026, 6, 10, 23, 0, tzinfo=UTC)  # a Wednesday, 19:00 in New York


def test_the_close_that_counts_is_the_latest_weekday_after_1730_in_new_york() -> None:
    saturday = datetime(2026, 6, 13, 12, tzinfo=NY)
    assert price_schedule.last_close(saturday) == datetime(2026, 6, 12, 17, 30, tzinfo=NY)
    monday_morning = datetime(2026, 6, 15, 10, tzinfo=NY)
    assert price_schedule.last_close(monday_morning) == datetime(2026, 6, 12, 17, 30, tzinfo=NY)
    monday_evening = datetime(2026, 6, 15, 18, tzinfo=NY)
    assert price_schedule.last_close(monday_evening) == datetime(2026, 6, 15, 17, 30, tzinfo=NY)


def test_a_fetch_is_due_once_a_day_and_retried_hourly() -> None:
    now = NOW.timestamp()
    assert price_schedule.due({}, NOW)
    assert not price_schedule.due({"succeeded": now - 600, "attempted": now - 600}, NOW)
    yesterday = now - 86400
    assert price_schedule.due({"succeeded": yesterday, "attempted": yesterday}, NOW)
    # It failed ten minutes ago: an hour later, not now.
    assert not price_schedule.due({"succeeded": yesterday, "attempted": now - 600}, NOW)
    assert price_schedule.due({"succeeded": yesterday, "attempted": now - 4000}, NOW)
    # main moved since today's fetch: fetched again, but not in a loop.
    assert price_schedule.due({"succeeded": now - 4000, "attempted": now - 4000, "refetch": True}, NOW)
    assert not price_schedule.due({"succeeded": now - 60, "attempted": now - 60, "refetch": True}, NOW)


MERGED = "merged new prices into main"
NO_UPSTREAM = "not pushing: main has no upstream branch on a remote"


def test_a_fetch_commits_on_its_branch_and_merges_it_into_a_clean_main(
    priced: tuple[Path, git.Repo], fetching: None,
) -> None:
    data, repo = priced
    before = repo.head.commit.hexsha
    outcome = price_schedule.fetch(data, _worktree(data), provider=FakeProvider(), now=NOW)

    assert outcome.committed is not None and outcome.pending == outcome.committed
    assert repo.head.commit.hexsha == before  # the fetch never merges: the daemon does
    assert price_schedule.merge_pending(data, _worktree(data)) == [MERGED, NO_UPSTREAM]
    # #588: a merge commit, never a fast-forward.
    merge = repo.head.commit
    assert [p.hexsha for p in merge.parents] == [before, outcome.committed]
    assert merge.author.name == "sim builder"
    assert merge.parents[1].message.startswith("Prices: VTI to 2026-06-10")
    assert "2026-06-10,110.00" in (data / "prices" / "security_price__VTI.csv").read_text()
    # Only the price files changed, and the user's checkout is clean.
    assert repo.git.diff("--name-only", before, "HEAD").splitlines() == [
        "sim/prices/security_price__VTI.csv"]
    assert not repo.is_dirty(untracked_files=True)
    state = price_schedule.read_state(data)
    assert state["succeeded"] == NOW.timestamp() and not state.get("pending")

    again = price_schedule.fetch(data, _worktree(data), provider=FakeProvider(), now=NOW)
    assert again.committed is None and again.pending is None and "no new prices" in again.lines
    # Nothing new: the branch is main again, not main plus an empty merge.
    assert repo.commit(price_schedule.PRICES_BRANCH).hexsha == repo.head.commit.hexsha


def test_the_merge_waits_for_main_to_be_clean_and_checked_out(
    priced: tuple[Path, git.Repo], fetching: None,
) -> None:
    data, repo = priced
    before = repo.head.commit.hexsha
    (data / "persons.csv").write_text((data / "persons.csv").read_text() + "\n")
    outcome = price_schedule.fetch(data, _worktree(data), provider=FakeProvider(), now=NOW)
    assert price_schedule.merge_pending(data, _worktree(data)) == []
    assert outcome.committed and repo.head.commit.hexsha == before
    assert price_schedule.read_state(data)["waiting"] == "main has uncommitted changes"

    repo.git.checkout("sim/persons.csv")
    repo.git.checkout("-b", "edits")
    assert price_schedule.merge_pending(data, _worktree(data)) == []
    assert "has edits checked out" in price_schedule.read_state(data)["waiting"]

    repo.git.checkout("main")
    assert price_schedule.merge_pending(data, _worktree(data)) == [MERGED, NO_UPSTREAM]
    assert repo.head.commit.parents[1].hexsha == outcome.committed
    assert price_schedule.merge_pending(data, _worktree(data)) == []  # nothing left


def test_a_fetch_while_prices_wait_keeps_them_waiting(
    priced: tuple[Path, git.Repo], fetching: None,
) -> None:
    data, repo = priced
    (data / "persons.csv").write_text((data / "persons.csv").read_text() + "\n")
    first = price_schedule.fetch(data, _worktree(data), provider=FakeProvider(), now=NOW)
    again = price_schedule.fetch(data, _worktree(data), provider=FakeProvider(), now=NOW)
    assert again.committed is None and again.pending == first.committed
    assert again.lines == ["no new prices; earlier ones still wait to merge into main"]
    assert price_schedule.read_state(data)["pending"] == first.committed


def test_when_main_moves_first_it_is_merged_into_the_prices_branch(
    commit_files: Callable[..., str],
    priced: tuple[Path, git.Repo], fetching: None,
) -> None:
    data, repo = priced
    (data / "persons.csv").write_text((data / "persons.csv").read_text() + "\n")
    first = price_schedule.fetch(data, _worktree(data), provider=FakeProvider(), now=NOW)
    repo.git.checkout("sim/persons.csv")
    users = commit_files(repo, {"sim/notes.txt": "an edit\n"}, "the user's commit")

    assert price_schedule.merge_pending(data, _worktree(data)) == [MERGED, NO_UPSTREAM]
    merge = repo.head.commit
    assert merge.parents[0].hexsha == users
    # #588: main went into builder/prices by a merge commit, not a cherry-pick.
    prices_tip = merge.parents[1]
    assert [p.hexsha for p in prices_tip.parents] == [first.committed, users]
    assert prices_tip.author.name == "sim builder"
    assert "2026-06-10,110.00" in (data / "prices" / "security_price__VTI.csv").read_text()
    assert (data / "notes.txt").read_text() == "an edit\n"
    state = price_schedule.read_state(data)
    assert not state.get("pending") and not state.get("refetch")


def test_where_main_and_the_fetch_conflict_mains_side_wins(
    commit_files: Callable[..., str],
    priced: tuple[Path, git.Repo], fetching: None,
) -> None:
    data, repo = priced
    (data / "persons.csv").write_text((data / "persons.csv").read_text() + "\n")
    price_schedule.fetch(data, _worktree(data), provider=FakeProvider(), now=NOW)
    repo.git.checkout("sim/persons.csv")
    commit_files(repo, {"sim/prices/security_price__VTI.csv":
                   "effective_date,price\n2025-12-31,301.00\n2026-06-11,999.00\n"},
                 "a correction by hand")

    # The fetch's rows all fall in the hunk main changed, so main's file
    # stands whole: nothing is left to merge, and the next fetch adds them.
    assert price_schedule.merge_pending(data, _worktree(data)) == []
    assert repo.head.commit.message.strip() == "a correction by hand"
    assert repo.commit(price_schedule.PRICES_BRANCH).hexsha == repo.head.commit.hexsha
    assert (data / "prices" / "security_price__VTI.csv").read_text() == \
        "effective_date,price\n2025-12-31,301.00\n2026-06-11,999.00\n"
    state = price_schedule.read_state(data)
    assert not state.get("refetch") and not state.get("pending")


def test_prices_that_wont_merge_with_main_are_fetched_again(
    commit_files: Callable[..., str],
    priced: tuple[Path, git.Repo], fetching: None,
) -> None:
    data, repo = priced
    (data / "persons.csv").write_text((data / "persons.csv").read_text() + "\n")
    price_schedule.fetch(data, _worktree(data), provider=FakeProvider(), now=NOW)
    repo.git.checkout("sim/persons.csv")
    # Deleted on main, changed by the fetch: no side of the merge to prefer.
    repo.git.rm("-q", "sim/prices/security_price__VTI.csv")
    commit_files(repo, {}, "the user dropped VTI's prices")

    assert price_schedule.merge_pending(data, _worktree(data)) == []
    state = price_schedule.read_state(data)
    assert state["refetch"] and "didn't merge" in state["waiting"] and not state["pending"]
    assert repo.commit(price_schedule.PRICES_BRANCH).hexsha == repo.head.commit.hexsha
    later = NOW + timedelta(hours=2)
    assert price_schedule.due(state, later)
    # Nothing names VTI now, so the fetch from main has nothing to fetch.
    second = price_schedule.fetch(data, _worktree(data), provider=FakeProvider(), now=later)
    assert second.lines == ["no symbols to fetch", "no new prices"] and second.pending is None


def test_a_fetch_starts_the_branch_over_when_main_wont_merge_into_it(
    commit_files: Callable[..., str],
    priced: tuple[Path, git.Repo], fetching: None,
) -> None:
    data, repo = priced
    commit_files(repo, {"sim/securities.csv": "symbol,name\nVTI,Total market\n"}, "VTI")
    (data / "persons.csv").write_text((data / "persons.csv").read_text() + "\n")
    price_schedule.fetch(data, _worktree(data), provider=FakeProvider(), now=NOW)
    repo.git.checkout("sim/persons.csv")
    repo.git.rm("-q", "sim/prices/security_price__VTI.csv")
    users = commit_files(repo, {}, "the user dropped VTI's prices")

    later = NOW + timedelta(days=1)
    again = price_schedule.fetch(data, _worktree(data), provider=FakeProvider(), now=later)
    assert again.lines[0] == "builder/prices didn't merge with main; starting it over from main"
    assert again.committed and repo.commit(again.committed).parents[0].hexsha == users


def test_prices_merged_by_hand_are_no_longer_pending(
    priced: tuple[Path, git.Repo], fetching: None,
) -> None:
    data, repo = priced
    (data / "persons.csv").write_text((data / "persons.csv").read_text() + "\n")
    price_schedule.fetch(data, _worktree(data), provider=FakeProvider(), now=NOW)
    price_schedule.merge_pending(data, _worktree(data))
    assert price_schedule.read_state(data)["pending"]
    repo.git.checkout("sim/persons.csv")
    repo.git.merge("--ff-only", price_schedule.PRICES_BRANCH)

    assert price_schedule.merge_pending(data, _worktree(data)) == []
    state = price_schedule.read_state(data)
    assert not state["pending"] and not state["waiting"]
    assert not any("waiting to merge" in line for line in price_schedule.describe(state, NOW))


def test_after_its_merge_the_builder_pushes_main_when_strictly_ahead(
    priced: tuple[Path, git.Repo], data_repo_origin: Path, fetching: None,
) -> None:
    data, repo = priced
    price_schedule.fetch(data, _worktree(data), provider=FakeProvider(), now=NOW)
    assert price_schedule.merge_pending(data, _worktree(data)) == [
        MERGED, "pushed main to origin/main"]
    with git.Repo(data_repo_origin) as origin:
        assert origin.commit("main").hexsha == repo.head.commit.hexsha


def test_the_builder_holds_back_a_main_that_lacks_the_upstreams_commits(
    commit_files: Callable[..., str], tmp_path: Path,
    priced: tuple[Path, git.Repo], data_repo_origin: Path, fetching: None,
) -> None:
    data, repo = priced
    with git.Repo.clone_from(data_repo_origin, tmp_path / "elsewhere") as other:
        commit_files(other, {"notes.txt": "from elsewhere\n"}, "another machine's commit")
        other.git.push("-q", "origin", "main")
    pushed = git.Repo(data_repo_origin).commit("main").hexsha
    repo.git.fetch("-q", "origin")
    price_schedule.fetch(data, _worktree(data), provider=FakeProvider(), now=NOW)

    assert price_schedule.merge_pending(data, _worktree(data)) == [
        MERGED, "not pushing: main isn't strictly ahead of origin/main "
                "(it lacks commits origin/main has); try git pull on main"]
    assert git.Repo(data_repo_origin).commit("main").hexsha == pushed


def test_a_failed_push_is_logged_and_the_merge_stands(
    tmp_path: Path, priced: tuple[Path, git.Repo], data_repo_origin: Path, fetching: None,
) -> None:
    data, repo = priced
    repo.git.remote("set-url", "origin", str(tmp_path / "gone.git"))
    outcome = price_schedule.fetch(data, _worktree(data), provider=FakeProvider(), now=NOW)

    merged, pushed = price_schedule.merge_pending(data, _worktree(data))
    assert merged == MERGED and pushed.startswith("push of main to origin failed: ")
    assert repo.head.commit.parents[1].hexsha == outcome.committed
    assert not price_schedule.read_state(data).get("pending")


def test_a_repo_with_no_prices_folder_and_nothing_to_fetch_still_succeeds(
    commit_files: Callable[..., str],
    data_repo_sim: Path, fetching: None,
) -> None:
    import shutil

    with git.Repo(data_repo_sim.parent) as repo:
        shutil.rmtree(data_repo_sim / "prices")
        commit_files(repo, {}, "no prices folder")
    outcome = price_schedule.fetch(data_repo_sim, _worktree(data_repo_sim),
                                   provider=FakeProvider(), now=NOW)
    assert outcome.lines == ["no symbols to fetch", "no new prices"]
    assert price_schedule.read_state(data_repo_sim)["succeeded"] == NOW.timestamp()


def test_state_writes_keep_each_others_keys_and_leave_no_temp_files(
    priced: tuple[Path, git.Repo],
) -> None:
    import threading

    data, _ = priced
    top = data.parent
    writers = [threading.Thread(target=lambda n=n: [
        price_schedule._update_state(top, **{f"k{n}": i}) for i in range(20)])
        for n in range(4)]
    for w in writers:
        w.start()
    for w in writers:
        w.join()
    state = price_schedule.read_state(data)
    assert {state[f"k{n}"] for n in range(4)} == {19}
    common = price_schedule._common_dir(top)
    assert not list(common.glob("*.tmp"))


def test_a_failing_symbol_is_recorded_and_does_not_stop_the_others(
    commit_files: Callable[..., str],
    priced: tuple[Path, git.Repo], fetching: None,
) -> None:
    data, repo = priced
    commit_files(repo, {"sim/securities.csv": "symbol,name\nBAD,Gone fund\n"}, "BAD")
    outcome = price_schedule.fetch(data, _worktree(data), provider=FakeProvider(), now=NOW)
    assert "BAD: no such symbol (no lot holds it)" in outcome.lines
    assert outcome.committed  # VTI still fetched
    failing = price_schedule.read_state(data)["failing"]
    assert failing["BAD"]["since"] == NOW.timestamp() and not failing["BAD"]["held"]

    later = NOW + timedelta(days=4)
    price_schedule.fetch(data, _worktree(data), provider=FakeProvider(), now=later)
    state = price_schedule.read_state(data)
    assert state["failing"]["BAD"]["since"] == NOW.timestamp()  # when it first failed
    assert "price failing for 4 days: BAD: no such symbol (no lot holds it)" in \
        price_schedule.describe(state, later)
    assert price_schedule.stale_held(state, later) == []  # no lot holds it


def test_a_fetch_where_every_symbol_fails_is_tried_again_later(
    priced: tuple[Path, git.Repo], fetching: None,
) -> None:
    data, _ = priced

    class Offline(FakeProvider):
        def closes(self, symbol: str, start: date, end: date) -> list[tuple[date, Decimal]]:
            raise PriceError("network is unreachable")

    outcome = price_schedule.fetch(data, _worktree(data), provider=Offline(), now=NOW)
    assert outcome.lines[-1] == "every symbol failed; trying again in an hour"
    state = price_schedule.read_state(data)
    assert "succeeded" not in state
    assert not price_schedule.due(state, NOW + timedelta(minutes=10))
    assert price_schedule.due(state, NOW + timedelta(hours=2))


def _fake_run(calls: list[Any], crontab: list[str]) -> Any:
    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs.get("input")))
        if args == ["crontab", "-l"]:
            return subprocess.CompletedProcess(args, 0, "\n".join(crontab) + "\n", "")
        if args == ["crontab", "-"]:
            crontab[:] = kwargs["input"].splitlines()
        return subprocess.CompletedProcess(args, 0, "", "")
    return run


def test_install_adds_one_hourly_crontab_line_and_uninstall_removes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fetching: None,
) -> None:
    monkeypatch.setattr(price_schedule.shutil, "which", lambda name: f"/usr/bin/{name}")
    crontab, calls = ["0 3 * * * backup"], []
    run = _fake_run(calls, crontab)
    sim = tmp_path / "code" / ".venv" / "bin" / "sim"
    for _ in range(2):  # installing again replaces the line
        price_schedule.install_timer(tmp_path, sim, run=run, platform="linux")
    assert crontab[0] == "0 3 * * * backup" and len(crontab) == 2
    assert crontab[1].startswith(f"7 * * * * {sim} builder prices --if-due --data-dir {tmp_path}")

    assert price_schedule.uninstall_timer(tmp_path, run=run, platform="linux")
    assert crontab == ["0 3 * * * backup"]
    assert price_schedule.uninstall_timer(tmp_path, run=run, platform="linux") is None


def test_install_on_macos_loads_an_hourly_launch_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fetching: None,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    calls: list[Any] = []
    sim = tmp_path / "sim"
    price_schedule.install_timer(tmp_path, sim, run=_fake_run(calls, []), platform="darwin")
    plist = tmp_path / "home" / "Library" / "LaunchAgents" / \
        f"{price_schedule.timer_label(tmp_path)}.plist"
    agent = plistlib.loads(plist.read_bytes())
    assert agent["StartInterval"] == 3600
    assert agent["ProgramArguments"] == [str(sim), "builder", "prices", "--if-due",
                                         "--data-dir", str(tmp_path.resolve())]
    assert calls[-1][0][:2] == ["launchctl", "bootstrap"]

    assert price_schedule.uninstall_timer(tmp_path, run=_fake_run(calls, []), platform="darwin")
    assert not plist.exists()


def test_tests_never_fetch_or_install_a_timer(priced: tuple[Path, git.Repo]) -> None:
    data, _ = priced
    assert price_schedule.switched_off()
    assert "left as it is" in price_schedule.install_timer(data, Path("sim"), run=_fake_run([], []))
    assert not price_schedule.enabled({"prices": True})
    out = CliRunner().invoke(sim_group_command, ["builder", "prices", "--data-dir", str(data)])
    assert "aren't installed" in out.output  # no builder config either


def test_install_records_whether_to_fetch_prices(
    priced: tuple[Path, git.Repo], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, _ = priced
    code = tmp_path / "code"
    (code / "data").mkdir(parents=True)
    (code / "data" / "shared").symlink_to(data.parent)
    (data / ".analysis").write_text(f"{code}\n")
    monkeypatch.setattr(builder, "code_dir", lambda: code.resolve())
    paths = builder.BuilderPaths.for_data(data)

    assert "fetches prices after each weekday's close" in builder.install(data)
    assert (builder.read_config(paths) or {}).get("prices") is True
    assert "doesn't fetch prices" in builder.install(data, prices=False)
    builder.install(data)  # kept when not given
    assert (builder.read_config(paths) or {}).get("prices") is False
    monkeypatch.delenv(price_schedule.SWITCH_ENV)
    assert not price_schedule.enabled(builder.read_config(paths))
