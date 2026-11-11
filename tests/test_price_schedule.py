"""#560: the builder fetches prices on a schedule, on a branch merged into main."""

from __future__ import annotations

import plistlib
import subprocess
from collections.abc import Generator
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


def _commit(repo: git.Repo, files: dict[str, str], message: str) -> None:
    root = Path(str(repo.working_tree_dir))
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    repo.git.add(A=True)
    actor = git.Actor("t", "t@t")
    repo.index.commit(message, author=actor, committer=actor)


@pytest.fixture
def priced(data_repo_sim: Path) -> Generator[tuple[Path, git.Repo]]:
    """The data repo with one symbol already priced: (sim dir, repo)."""
    with git.Repo(data_repo_sim.parent) as repo:
        _commit(repo, {"sim/prices/security_price__VTI.csv":
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


def test_a_fetch_commits_on_its_branch_and_fast_forwards_a_clean_main(
    priced: tuple[Path, git.Repo], fetching: None,
) -> None:
    data, repo = priced
    before = repo.head.commit.hexsha
    outcome = price_schedule.fetch(data, _worktree(data), provider=FakeProvider(), now=NOW)

    assert outcome.committed is not None
    assert "merged new prices into main" in outcome.lines
    assert repo.head.commit.hexsha == outcome.committed
    assert repo.head.commit.parents[0].hexsha == before  # a fast-forward: no merge commit
    assert repo.head.commit.author.name == "sim builder"
    assert repo.head.commit.message.startswith("Prices: VTI to 2026-06-10")
    assert "2026-06-10,110.00" in (data / "prices" / "security_price__VTI.csv").read_text()
    # Only the price files changed, and the user's checkout is clean.
    assert repo.git.diff("--name-only", before, "HEAD").splitlines() == [
        "sim/prices/security_price__VTI.csv"]
    assert not repo.is_dirty(untracked_files=True)
    state = price_schedule.read_state(data)
    assert state["succeeded"] == NOW.timestamp() and not state.get("pending")

    again = price_schedule.fetch(data, _worktree(data), provider=FakeProvider(), now=NOW)
    assert again.committed is None and "no new prices" in again.lines


def test_the_merge_waits_for_main_to_be_clean_and_checked_out(
    priced: tuple[Path, git.Repo], fetching: None,
) -> None:
    data, repo = priced
    before = repo.head.commit.hexsha
    (data / "persons.csv").write_text((data / "persons.csv").read_text() + "\n")
    outcome = price_schedule.fetch(data, _worktree(data), provider=FakeProvider(), now=NOW)
    assert outcome.committed and repo.head.commit.hexsha == before
    assert price_schedule.read_state(data)["waiting"] == "main has uncommitted changes"

    repo.git.checkout("sim/persons.csv")
    repo.git.checkout("-b", "edits")
    assert price_schedule.merge_pending(data) is None
    assert "has edits checked out" in price_schedule.read_state(data)["waiting"]

    repo.git.checkout("main")
    assert price_schedule.merge_pending(data) == "merged new prices into main"
    assert repo.head.commit.hexsha == outcome.committed
    assert price_schedule.merge_pending(data) is None  # nothing left


def test_when_main_moves_first_the_next_fetch_starts_from_it(
    priced: tuple[Path, git.Repo], fetching: None,
) -> None:
    data, repo = priced
    (data / "persons.csv").write_text((data / "persons.csv").read_text() + "\n")
    first = price_schedule.fetch(data, _worktree(data), provider=FakeProvider(), now=NOW)
    repo.git.checkout("sim/persons.csv")
    _commit(repo, {"sim/notes.txt": "an edit\n"}, "the user's commit")

    assert price_schedule.merge_pending(data) is None
    state = price_schedule.read_state(data)
    assert state["refetch"] and "moved" in state["waiting"]
    later = NOW + timedelta(hours=2)
    assert price_schedule.due(state, later)

    second = price_schedule.fetch(data, _worktree(data), provider=FakeProvider(), now=later)
    assert second.committed and second.committed != first.committed
    assert repo.head.commit.hexsha == second.committed
    assert repo.head.commit.parents[0].message.strip() == "the user's commit"


def test_a_failing_symbol_is_recorded_and_does_not_stop_the_others(
    priced: tuple[Path, git.Repo], fetching: None,
) -> None:
    data, repo = priced
    _commit(repo, {"sim/securities.csv": "symbol,name\nBAD,Gone fund\n"}, "BAD")
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
    assert crontab[1].startswith(f"7 * * * * {sim} builder notify --quiet --data-dir {tmp_path}")

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
    assert agent["ProgramArguments"] == [str(sim), "builder", "notify", "--quiet",
                                         "--data-dir", str(tmp_path.resolve())]
    assert calls[-1][0][:2] == ["launchctl", "bootstrap"]

    assert price_schedule.uninstall_timer(tmp_path, run=_fake_run(calls, []), platform="darwin")
    assert not plist.exists()


def test_tests_never_fetch_or_install_a_timer(priced: tuple[Path, git.Repo]) -> None:
    data, _ = priced
    assert price_schedule.switched_off()
    assert "no timer" in price_schedule.install_timer(data, Path("sim"), run=_fake_run([], []))
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
