"""#640: the builder researches a symbol a day; `sim symbol research` commits.

Nothing here runs Claude: the schedule's tests pass a runner that copies a
bundle from ``research_bundles`` into the run's folder, and the CLI's use
``--from-bundle`` or ``fake_claude``."""

from __future__ import annotations

import csv
import shutil
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import git
import pytest
from click.testing import CliRunner

from zabob.retirement.cli import main
from zabob.retirement.sim import builder, prices, research, research_schedule, symbols
from zabob.retirement.sim.research_schedule import RunReport

NY = research_schedule.MARKET
# A Tuesday, 07:00 in New York: past the day's 06:07.
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
FUND = "symbol,as_of,asset_class,weight\nABCDX,2026-06,stock_us,60%\nABCDX,2026-06,bond,40%\n"


@pytest.fixture
def researching(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(research_schedule.SWITCH_ENV, raising=False)


@pytest.fixture
def fund_repo(commit_files: Callable[..., str], data_repo_sim: Path) -> tuple[Path, git.Repo]:
    """The data repo holding ABCDX with a 60/40 set as of 2026-06: (sim dir, repo)."""
    repo = git.Repo(data_repo_sim.parent)
    commit_files(repo, {"sim/allocations.csv": FUND}, "ABCDX")
    return data_repo_sim, repo


def _runner(research_bundles: Path, name: str = "abcdx", *, ok: bool = True) -> research_schedule.Runner:
    def run(symbol: str, work: Path, data: Path, progress: Callable[[str], None]) -> RunReport:
        del symbol, data
        progress("  [ 0:01] WebSearch")
        if name:
            shutil.copytree(research_bundles / name, work, dirs_exist_ok=True)
        return RunReport(ok, "" if ok else "the skill gave up", cost_usd=1.25, seconds=300.0)
    return run


def _message(repo: git.Repo, ref: str) -> str:
    return repo.git.log("-1", "--format=%B", ref)


def _files(repo: git.Repo, ref: str) -> set[str]:
    return set(repo.git.show("--name-only", "--format=", ref).split())


def _config(data: Path) -> dict:
    config = builder.read_config(builder.BuilderPaths.for_data(data))
    assert config is not None
    return config


def _worktree(data: Path) -> Path:
    return builder.BuilderPaths.for_data(data).research_worktree


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# -- when, and which ------------------------------------------------------------------


def test_the_days_research_is_due_from_0607_new_york(fund_repo: tuple[Path, git.Repo],
                                                       researching: None) -> None:
    data, _ = fund_repo
    config = {"research": True}
    early = datetime(2026, 11, 17, 6, 6, tzinfo=NY)
    assert not research_schedule.due(data, config, early)
    assert research_schedule.due(data, config, datetime(2026, 11, 17, 6, 7, tzinfo=NY))
    assert not research_schedule.due(data, {"research": False}, NOW)


def test_research_is_off_under_the_switch(fund_repo: tuple[Path, git.Repo]) -> None:
    assert not research_schedule.due(fund_repo[0], {"research": True}, NOW)  # conftest sets it off


def test_last_researched_is_the_bundles_commit_date(
    fund_repo: tuple[Path, git.Repo], commit_files: Callable[..., str],
) -> None:
    data, repo = fund_repo
    commit_files(repo, {"sim/research/ABCDX/2026-10-01/ABCDX-tear-sheet.md": "# ABCDX\n"}, "research")
    stamp = repo.head.commit.committed_date
    assert research_schedule.last_researched(data) == {"ABCDX": float(stamp)}
    # Due a month on, not before.
    then = datetime.fromtimestamp(stamp, UTC)
    assert research_schedule.due_symbols(data, {}, then + timedelta(days=20)) == []
    assert research_schedule.due_symbols(data, {}, then + timedelta(days=32)) == ["ABCDX"]


def test_the_oldest_goes_first_and_a_failure_goes_to_the_back(
    fund_repo: tuple[Path, git.Repo], commit_files: Callable[..., str],
) -> None:
    data, repo = fund_repo
    commit_files(repo, {"sim/securities.csv": "symbol,name,no_fetch\nBBB,B fund,\nOLD,,1\n"}, "names")
    assert research_schedule.due_symbols(data, {}, NOW) == ["ABCDX", "BBB"]  # OLD is no_fetch
    failed = {"failing": {"ABCDX": {"error": "x", "at": NOW.timestamp(), "since": NOW.timestamp()}}}
    assert research_schedule.due_symbols(data, failed, NOW) == ["BBB", "ABCDX"]


@pytest.mark.parametrize(("proposed", "changed"), [
    ({"stock_us": "60.4%", "bond": "39.6%"}, False),
    ({"stock_us": "60.5%", "bond": "39.5%"}, False),   # 0.5 points is still unchanged
    ({"stock_us": "60.6%", "bond": "39.4%"}, True),
    ({"stock_us": "59.8%", "bond": "39.8%", "cash": "0.4%"}, False),
    ({"stock_us": "59.4%", "bond": "40%", "cash": "0.6%"}, True),
])
def test_weights_change_only_past_half_a_point(proposed: dict[str, str], changed: bool) -> None:
    latest = [("stock_us", "60%"), ("bond", "40%")]
    assert research_schedule.weights_changed(proposed, latest) is changed
    assert research_schedule.weights_changed(proposed, None)


# -- a scheduled run ------------------------------------------------------------------


def test_a_run_commits_the_bundle_and_new_weights_on_its_branch_then_merges(
    fund_repo: tuple[Path, git.Repo], research_bundles: Path, researching: None,
) -> None:
    data, repo = fund_repo
    said: list[str] = []
    outcome = research_schedule.research(data, _worktree(data), now=NOW,
                                         runner=_runner(research_bundles), progress=said.append)
    assert outcome.symbol == "ABCDX" and outcome.committed
    assert said == ["  [ 0:01] WebSearch"]
    assert "ABCDX: wrote weights as of 2026-09: stock_us 58.1234%, stock_intl 1.8766%, bond 39.5%, cash 0.5%" \
        in outcome.lines
    branch = repo.commit(research_schedule.RESEARCH_BRANCH)
    assert _message(repo, branch.hexsha).startswith("Research ABCDX: new weights")
    names = _files(repo, branch.hexsha)
    assert "sim/allocations.csv" in names and "sim/securities.csv" in names  # named from the tear sheet
    assert any(n.startswith("sim/research/ABCDX/2026-11-17/") for n in names)
    assert not (data / "research").exists()  # the user's checkout is untouched so far
    state = research_schedule.read_state(data)
    assert state["day"] == "2026-11-17" and state["pending"] == branch.hexsha
    assert state["last"]["outcome"] == "weights" and state["costs"] == [[NOW.timestamp(), 1.25]]

    assert research_schedule.merge_pending(data, _worktree(data))[0] == "merged new research into main"
    sets = {r["as_of"] for r in _rows(data / "allocations.csv")}
    assert sets == {"2026-06", "2026-09"}  # a new dated set beside the old
    assert prices.no_fetch_symbols(data) == set()
    assert research_schedule.last_researched(data)["ABCDX"] == repo.commit(branch.hexsha).committed_date
    assert not research_schedule.due(data, {"research": True}, NOW)  # done today, and a month on


def test_unchanged_weights_commit_only_the_bundle(
    fund_repo: tuple[Path, git.Repo], commit_files: Callable[..., str],
    research_bundles: Path, researching: None,
) -> None:
    data, repo = fund_repo
    near = ("symbol,as_of,asset_class,weight\nABCDX,2026-08,stock_us,58%\nABCDX,2026-08,stock_intl,2%\n"
            "ABCDX,2026-08,bond,39.6%\nABCDX,2026-08,cash,0.4%\n")
    commit_files(repo, {"sim/allocations.csv": near, "sim/securities.csv": "symbol,name\nABCDX,Mine\n"}, "near")
    outcome = research_schedule.research(data, _worktree(data), now=NOW, runner=_runner(research_bundles))
    assert any("within 0.5 points of the set as of 2026-08" in line for line in outcome.lines)
    files = _files(repo, research_schedule.RESEARCH_BRANCH)
    assert all(f.startswith("sim/research/ABCDX/") for f in files)


def test_a_failed_run_keeps_nothing_and_is_recorded(
    fund_repo: tuple[Path, git.Repo], research_bundles: Path, researching: None,
) -> None:
    data, repo = fund_repo
    main = repo.commit("main").hexsha
    outcome = research_schedule.research(data, _worktree(data), now=NOW,
                                         runner=_runner(research_bundles, ok=False))
    assert outcome.committed is None
    assert "ABCDX: research failed: the skill gave up" in outcome.lines
    assert repo.commit(research_schedule.RESEARCH_BRANCH).hexsha == main
    state = research_schedule.read_state(data)
    assert state["failing"]["ABCDX"]["error"] == "the skill gave up"
    tried = NOW.astimezone().strftime("%Y-%m-%d %H:%M")  # local time, as status shows it
    assert f"research failing: ABCDX (last tried {tried}): the skill gave up" \
        in research_schedule.describe(state, data, NOW)
    # Not again today.
    assert not research_schedule.due(data, {"research": True}, NOW + timedelta(hours=2))


def test_research_waits_for_a_clean_main(
    fund_repo: tuple[Path, git.Repo], research_bundles: Path, researching: None,
) -> None:
    data, _ = fund_repo
    research_schedule.research(data, _worktree(data), now=NOW, runner=_runner(research_bundles))
    (data / "persons.csv").write_text((data / "persons.csv").read_text() + "\n")
    assert research_schedule.merge_pending(data, _worktree(data)) == []
    assert research_schedule.read_state(data)["waiting"] == "main has uncommitted changes"


def test_weights_written_again_when_main_changed_them_meanwhile(
    fund_repo: tuple[Path, git.Repo], commit_files: Callable[..., str],
    research_bundles: Path, researching: None,
) -> None:
    data, repo = fund_repo
    research_schedule.research(data, _worktree(data), now=NOW, runner=_runner(research_bundles))
    # Meanwhile main gets another set at the end of the same file: a conflict.
    commit_files(repo, {"sim/allocations.csv": FUND + "ABCDX,2026-07,stock_us,55%\nABCDX,2026-07,bond,45%\n"},
                 "by hand")
    lines = research_schedule.merge_pending(data, _worktree(data))
    assert lines[0] == "merged new research into main"
    assert {r["as_of"] for r in _rows(data / "allocations.csv")} == {"2026-06", "2026-07", "2026-09"}
    assert any(p.name.endswith("tear-sheet.md") for p in (data / "research" / "ABCDX").rglob("*"))
    assert "written again" in _message(repo, research_schedule.RESEARCH_BRANCH)


def test_a_bundle_only_main_has_is_left_alone_when_written_again(
    fund_repo: tuple[Path, git.Repo], commit_files: Callable[..., str],
    research_bundles: Path, researching: None,
) -> None:
    data, repo = fund_repo
    research_schedule.research(data, _worktree(data), now=NOW, runner=_runner(research_bundles))
    # Meanwhile main gets research run by hand, and a conflicting set.
    commit_files(repo, {"sim/research/BBB/2026-11-16/BBB-tear-sheet.md": "# BBB\n",
                        "sim/allocations.csv": FUND + "ABCDX,2026-07,stock_us,55%\nABCDX,2026-07,bond,45%\n"},
                 "by hand")
    lines = research_schedule.merge_pending(data, _worktree(data))
    assert lines[0] == "merged new research into main"
    assert "BBB" not in _message(repo, research_schedule.RESEARCH_BRANCH)
    assert (data / "research" / "BBB" / "2026-11-16" / "BBB-tear-sheet.md").read_text() == "# BBB\n"


def test_research_older_than_the_latest_set_writes_nothing(
    fund_repo: tuple[Path, git.Repo], commit_files: Callable[..., str], research_bundles: Path,
) -> None:
    data, repo = fund_repo
    later = FUND + "ABCDX,2026-10,stock_us,70%\nABCDX,2026-10,bond,30%\n"
    commit_files(repo, {"sim/allocations.csv": later}, "later")
    applied = research_schedule.apply_weights(data, "ABCDX", research_bundles / "abcdx")
    assert not applied.wrote
    assert "ABCDX: the research is as of 2026-09, older than the set as of 2026-10; " \
        "allocations.csv unchanged" in applied.lines
    assert (data / "allocations.csv").read_text() == later


def test_nothing_due_is_remembered_for_the_day(
    fund_repo: tuple[Path, git.Repo], researching: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, _ = fund_repo
    monkeypatch.setattr(research_schedule, "due_symbols", lambda *_: [])
    assert not research_schedule.due(data, {"research": True}, NOW)
    assert research_schedule.read_state(data)["quiet"] == "2026-11-17"

    def no_scan(*_):
        raise AssertionError("scanned again the same day")
    monkeypatch.setattr(research_schedule, "due_symbols", no_scan)
    assert not research_schedule.due(data, {"research": True}, NOW + timedelta(minutes=1))
    monkeypatch.setattr(research_schedule, "due_symbols", lambda *_: ["ABCDX"])
    assert research_schedule.due(data, {"research": True}, NOW + timedelta(days=1))  # tomorrow looks again


def test_a_runner_that_raises_is_a_recorded_failure(
    fund_repo: tuple[Path, git.Repo], researching: None,
) -> None:
    data, _ = fund_repo

    def broken(*_) -> RunReport:
        raise RuntimeError("claude not found")
    outcome = research_schedule.research(data, _worktree(data), now=NOW, runner=broken)
    assert "ABCDX: research failed: RuntimeError: claude not found" in outcome.lines
    state = research_schedule.read_state(data)
    assert state["failing"]["ABCDX"]["error"] == "RuntimeError: claude not found"
    assert research_schedule.due_symbols(data, state, NOW)[-1] == "ABCDX"  # to the back of the queue


def test_a_symbol_named_by_hand_leaves_the_days_run_due(
    fund_repo: tuple[Path, git.Repo], commit_files: Callable[..., str],
    research_bundles: Path, researching: None,
) -> None:
    data, repo = fund_repo
    commit_files(repo, {"sim/securities.csv": "symbol,name\nBBB,B fund\n"}, "names")
    research_schedule.research(data, _worktree(data), now=NOW, symbol="ABCDX",
                               runner=_runner(research_bundles))
    assert "day" not in research_schedule.read_state(data)
    assert research_schedule.due(data, {"research": True}, NOW)  # BBB is still due today


def test_status_shows_the_last_run_its_cost_and_the_next(
    fund_repo: tuple[Path, git.Repo], research_bundles: Path, researching: None,
) -> None:
    data, _ = fund_repo
    research_schedule.research(data, _worktree(data), now=NOW, runner=_runner(research_bundles))
    lines = research_schedule.describe(research_schedule.read_state(data), data, NOW)
    assert lines[0].startswith("research: last ABCDX on ") and "wrote new weights; $1.25" in lines[0]
    assert lines[2] == "research next: nothing due"
    assert any(line.startswith("research cost, last 30 days: $1.25 over 1 run(s)") for line in lines)


def test_install_records_whether_to_research(code_repo, data_repo_sim: Path, monkeypatch) -> None:
    del code_repo
    monkeypatch.setattr(builder, "pair_problems", lambda *_: [])
    builder.install(data_repo_sim, research=False)
    assert _config(data_repo_sim)["research"] is False
    builder.install(data_repo_sim)  # again: kept
    assert _config(data_repo_sim)["research"] is False


def test_install_records_where_claude_is_for_the_timer(
    code_repo, data_repo_sim: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    del code_repo
    monkeypatch.setattr(builder, "pair_problems", lambda *_: [])
    monkeypatch.setattr(research, "CLAUDE_PLACES", ())
    claude = tmp_path / "bin" / "claude"
    claude.parent.mkdir()
    claude.write_text("#!/bin/sh\n")
    claude.chmod(0o755)
    bare = "/usr/bin:/bin:/usr/sbin:/sbin"  # launchd's, for git
    monkeypatch.setenv("PATH", f"{claude.parent}:{bare}")
    assert f"runs Claude Code at {claude}" in builder.install(data_repo_sim)
    assert _config(data_repo_sim)["claude"] == str(claude)
    # The timer's bare PATH lacks it: install again from there keeps it, and status finds it.
    monkeypatch.setenv("PATH", bare)
    builder.install(data_repo_sim)
    assert _config(data_repo_sim)["claude"] == str(claude)
    lines = research_schedule.describe({}, data_repo_sim, NOW, claude=str(claude))
    assert f"research runs: {claude}" in lines
    assert any(line.startswith("research can't find claude")
               for line in research_schedule.describe({}, data_repo_sim, NOW))


# -- `sim symbol research` commits ------------------------------------------------------


def _research(data: Path, *args: str):
    return CliRunner().invoke(main, ["sim", "symbol", "research", *args, "--data-dir", str(data)],
                              terminal_width=200)


def test_a_foreground_run_commits_the_bundle_and_weights_only(
    fund_repo: tuple[Path, git.Repo], research_bundles: Path,
) -> None:
    data, repo = fund_repo
    (data / "persons.csv").write_text((data / "persons.csv").read_text() + "\n")  # someone's edit
    repo.git.add("sim/persons.csv")  # staged, even
    result = _research(data, "ABCDX", "--from-bundle", str(research_bundles / "abcdx"), "--yes", "--no-open")
    assert result.exit_code == 0, result.output
    assert "Committed: Research ABCDX: new weights" in result.output
    files = _files(repo, "HEAD")
    assert {"sim/allocations.csv", "sim/securities.csv"} <= files
    assert "sim/persons.csv" not in files
    assert "sim/persons.csv" in repo.git.diff("--cached", "--name-only")  # still staged
    assert "ABCDX" in research_schedule.last_researched(data)


def test_own_edits_to_allocations_stay_out_of_the_commit(
    fund_repo: tuple[Path, git.Repo], research_bundles: Path,
) -> None:
    data, repo = fund_repo
    (data / "allocations.csv").write_text(FUND + "BBB,,bond,100%\n")
    result = _research(data, "ABCDX", "--from-bundle", str(research_bundles / "abcdx"), "--yes", "--no-open")
    assert result.exit_code == 0, result.output
    assert "allocations.csv already had uncommitted changes" in result.output
    files = _files(repo, "HEAD")
    assert "sim/allocations.csv" not in files
    assert any(f.startswith("sim/research/ABCDX/") for f in files)


def test_no_commit_leaves_it_all_uncommitted(fund_repo: tuple[Path, git.Repo], research_bundles: Path) -> None:
    data, repo = fund_repo
    head = repo.head.commit.hexsha
    result = _research(data, "ABCDX", "--from-bundle", str(research_bundles / "abcdx"), "--yes",
                       "--no-open", "--no-commit")
    assert result.exit_code == 0, result.output
    assert repo.head.commit.hexsha == head
    assert "ABCDX" not in research_schedule.last_researched(data)


def test_yes_writes_nothing_within_half_a_point(
    fund_repo: tuple[Path, git.Repo], commit_files: Callable[..., str], research_bundles: Path,
) -> None:
    data, repo = fund_repo
    near = ("symbol,as_of,asset_class,weight\nABCDX,2026-08,stock_us,58%\nABCDX,2026-08,stock_intl,2%\n"
            "ABCDX,2026-08,bond,39.6%\nABCDX,2026-08,cash,0.4%\n")
    commit_files(repo, {"sim/allocations.csv": near}, "near")
    result = _research(data, "ABCDX", "--from-bundle", str(research_bundles / "abcdx"), "--yes", "--no-open")
    assert result.exit_code == 0, result.output
    assert "Within 0.5 points of the set as of 2026-08 on every class: no weights written." in result.output
    assert (data / "allocations.csv").read_text() == near
    assert symbols.security_name(data, "ABCDX") == "A Balanced Index Fund"  # named all the same


def test_a_no_fetch_symbol_named_is_researched_with_a_warning(
    fund_repo: tuple[Path, git.Repo], commit_files: Callable[..., str], research_bundles: Path,
) -> None:
    data, repo = fund_repo
    commit_files(repo, {"sim/securities.csv": "symbol,name,no_fetch\nABCDX,,1\n"}, "mark")
    result = _research(data, "ABCDX", "--from-bundle", str(research_bundles / "abcdx"), "--yes", "--no-open")
    assert result.exit_code == 0, result.output
    assert "warning: ABCDX is marked no_fetch" in result.output
    # The blank name is filled from the tear sheet, the mark kept.
    assert _rows(data / "securities.csv")[0]["name"]
    assert prices.no_fetch_symbols(data) == {"ABCDX"}


def test_the_tick_runs_the_days_research(
    fund_repo: tuple[Path, git.Repo], monkeypatch: pytest.MonkeyPatch, researching: None,
) -> None:
    data, _ = fund_repo
    monkeypatch.setattr(builder, "read_config", lambda paths: {"research": True})
    asked: list[str | None] = []

    def fake(data_dir: Path, worktree: Path, *, symbol: str | None = None, progress=print, **_):
        del data_dir, worktree
        asked.append(symbol)
        progress("researching ABCDX")
        return research_schedule.ResearchOutcome(["no symbol is due for research"])

    monkeypatch.setattr(research_schedule, "research", fake)
    monkeypatch.setattr(research_schedule, "due", lambda *_: True)
    result = CliRunner().invoke(main, ["sim", "builder", "tick", "--data-dir", str(data)])
    assert result.exit_code == 0, result.output
    assert asked == [None]
    assert "researching ABCDX" in result.output
