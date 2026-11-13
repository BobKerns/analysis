"""#564: the builder rolls marked scenarios forward at night, on a branch
merged into main as the prices are (#560), tagging the plan before a roll
into a new year rather than keeping a copy of it."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable, Generator
from datetime import date, datetime, time
from pathlib import Path

import git
import pytest
from click.testing import CliRunner

from analysis.sim import builder, roll_schedule
from analysis.sim.cli import sim_group_command

SCENARIO = "sim/scenarios/retire_alice/scenario.csv"


def _local(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    """A moment in this machine's local time, as the window is."""
    return datetime(year, month, day, hour, minute).astimezone()  # noqa: DTZ001 -- local on purpose


def _scenario_row(path: Path) -> dict[str, str]:
    with path.open(newline="", encoding="utf-8") as fh:
        return next(csv.DictReader(fh))


def _csv_text(row: dict[str, str]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(row), lineterminator="\n")
    writer.writeheader()
    writer.writerow(row)
    return out.getvalue()


@pytest.fixture
def marked(data_repo_sim: Path, commit_files: Callable[..., str]) -> Generator[tuple[Path, git.Repo]]:
    """The data repo with retire_alice (start 2026-05) marked to roll."""
    with git.Repo(data_repo_sim.parent) as repo:
        row = _scenario_row(data_repo_sim.parent / SCENARIO) | {"meta_json": '{"roll": "monthly"}'}
        commit_files(repo, {SCENARIO: _csv_text(row)}, "mark retire_alice to roll")
        yield data_repo_sim, repo


def test_a_window_may_run_past_midnight() -> None:
    assert roll_schedule.parse_window("01:00-05:00") == (time(1), time(5))
    late = roll_schedule.parse_window("23:30-04:00")
    assert roll_schedule.night_of(_local(2027, 1, 5, 23, 45), late) == date(2027, 1, 5)
    assert roll_schedule.night_of(_local(2027, 1, 6, 3, 0), late) == date(2027, 1, 5)
    assert roll_schedule.night_of(_local(2027, 1, 6, 12, 0), late) is None
    window = roll_schedule.parse_window(roll_schedule.DEFAULT_WINDOW)
    assert roll_schedule.night_of(_local(2027, 1, 6, 2, 0), window) == date(2027, 1, 6)
    assert roll_schedule.night_of(_local(2027, 1, 6, 5, 0), window) is None
    for bad in ("1-5", "01:00", "01:00-01:00"):
        with pytest.raises(ValueError):
            roll_schedule.parse_window(bad)
    # A bad configured window falls back to the default rather than never rolling.
    assert roll_schedule.window_of({"roll_window": "soon"}) == window


def _derived(name: str, meta: str = "{}") -> dict[str, str]:
    """A scenario built on retire_alice, inheriting its start, as files."""
    return {
        f"sim/scenarios/{name}/scenario.csv": _csv_text({
            "name": name, "description": "", "start": "", "end": "", "seed": "",
            "parent_scenario": "retire_alice", "created_by": "alice", "meta_json": meta}),
        f"sim/scenarios/{name}/README.md": f"# {name}\n",
    }


def test_a_marked_scenario_rolls_once_its_start_month_is_over(
    marked: tuple[Path, git.Repo],
) -> None:
    data, _ = marked
    assert roll_schedule.marked(data) == ["retire_alice"]
    assert roll_schedule.due_scenarios(data, date(2026, 5, 31)) == []
    assert roll_schedule.due_scenarios(data, date(2026, 6, 1)) == [("retire_alice", "2026-05")]
    # Disabled: no run is kept current for it to project from.
    (data / "scenarios" / "retire_alice" / ".disable").write_text("off\n")
    assert roll_schedule.due_scenarios(data, date(2026, 6, 1)) == []


def test_the_mark_is_inherited_and_a_derived_scenario_may_opt_out(
    marked: tuple[Path, git.Repo], commit_files: Callable[..., str],
) -> None:
    data, repo = marked
    commit_files(repo, {**_derived("alice_more"), **_derived("alice_fixed", '{"roll": "off"}')},
                 "two what-ifs")
    # Parents first, so a child's roll reads its parent's new start.
    assert roll_schedule.marked(data) == ["retire_alice", "alice_more"]
    assert roll_schedule.due_scenarios(data, date(2026, 6, 1)) == [
        ("retire_alice", "2026-05"), ("alice_more", "2026-05")]


def test_a_derived_scenario_rolls_its_own_rows_after_its_parent(
    marked: tuple[Path, git.Repo], commit_files: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from analysis.sim.roll import PARENT_FIRST

    data, repo = marked
    commit_files(repo, _derived("alice_more"), "a what-if")
    calls: list[tuple[str, bool]] = []

    def fake(data: Path, name: str, month: str, *, own: bool = False) -> tuple[int, list[str]]:
        calls.append((name, own))
        if own:
            return 1, [f"Error: alice_more's start is inherited ...: {PARENT_FIRST}, or ..."]
        (data / "scenarios" / name / "README.md").write_text("rolled\n")
        return 0, []

    monkeypatch.setattr(roll_schedule, "_roll_one", fake)
    worktree = builder.BuilderPaths.for_data(data).roll_worktree

    outcome = roll_schedule.roll(data, worktree, {"roll": True}, now=_local(2026, 11, 13, 2, 0))

    assert calls == [("retire_alice", False), ("alice_more", True)]
    assert outcome.rolled == {"retire_alice": "2026-05 -> 2026-11"}
    scenarios = roll_schedule.read_state(data)["scenarios"]
    assert scenarios["alice_more"]["outcome"] == "skipped"


def test_a_derived_scenario_with_its_own_start_waits_when_its_parent_fails(
    marked: tuple[Path, git.Repo], commit_files: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, repo = marked
    files = _derived("alice_own")
    path = "sim/scenarios/alice_own/scenario.csv"
    files[path] = files[path].replace(",,,,retire_alice", ",2026-05,,,retire_alice")
    commit_files(repo, files, "a what-if with its own start")
    calls: list[str] = []

    def fake(data: Path, name: str, month: str, *, own: bool = False) -> tuple[int, list[str]]:
        calls.append(name)
        return 1, ["Error: it broke"]

    monkeypatch.setattr(roll_schedule, "_roll_one", fake)
    worktree = builder.BuilderPaths.for_data(data).roll_worktree

    roll_schedule.roll(data, worktree, {"roll": True}, now=_local(2026, 11, 13, 2, 0))

    # Its own start moved over its parent's old lots would be a plan of two months.
    assert calls == ["retire_alice"]
    entry = roll_schedule.read_state(data)["scenarios"]["alice_own"]
    assert entry["outcome"] == "skipped" and "retire_alice didn't roll" in entry["why"]


def test_a_roll_is_due_once_a_night_in_the_window_after_the_prices(
    marked: tuple[Path, git.Repo], monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, repo = marked
    config = {"prices": False, "roll": True}
    night = _local(2026, 11, 13, 2, 0)
    assert not roll_schedule.due(data, config, night)  # the tests switch rolls off
    monkeypatch.delenv(roll_schedule.SWITCH_ENV)
    assert roll_schedule.due(data, config, night)
    assert not roll_schedule.due(data, config, _local(2026, 11, 13, 14, 0))
    assert not roll_schedule.due(data, {**config, "roll": False}, night)
    top = Path(str(repo.working_tree_dir))
    roll_schedule._update_state(top, night="2026-11-13")
    assert not roll_schedule.due(data, config, night)
    assert roll_schedule.due(data, config, _local(2026, 11, 14, 2, 0))
    # Prices first, so the roll values the lots at the latest close.
    from analysis.sim import price_schedule

    monkeypatch.delenv(price_schedule.SWITCH_ENV)
    price_schedule._update_state(top, pending="abc")
    assert not roll_schedule.due(data, {**config, "prices": True}, _local(2026, 11, 14, 2, 0))


@pytest.mark.slow
def test_a_roll_into_a_new_year_commits_on_its_branch_tags_the_plan_and_merges(
    marked: tuple[Path, git.Repo], data_repo_origin: Path,
) -> None:
    data, repo = marked
    load = CliRunner().invoke(sim_group_command, ["load", "retire_alice", "--data-dir", str(data)])
    assert load.exit_code == 0, load.output
    before = repo.head.commit.hexsha
    worktree = builder.BuilderPaths.for_data(data).roll_worktree

    outcome = roll_schedule.roll(data, worktree, {"roll": True}, now=_local(2027, 1, 5, 2, 0))

    assert outcome.rolled == {"retire_alice": "2026-05 -> 2027-01"}, outcome.lines
    assert repo.head.commit.hexsha == before  # the roll never merges: the daemon does
    assert "plan-2026" not in [t.name for t in repo.tags]  # nor tags, till it merges
    rolled = repo.commit(roll_schedule.ROLL_BRANCH)
    assert str(rolled.message).startswith("Roll retire_alice forward to 2027-01")
    assert sorted(repo.git.diff("--name-only", before, rolled.hexsha).splitlines()) == [
        "sim/scenarios/retire_alice/account_lots.csv",
        "sim/scenarios/retire_alice/scenario.csv",
        "sim/scenarios/retire_alice/year_to_date.csv",
    ]

    lines = roll_schedule.merge_pending(data)
    assert lines == ["merged the roll into main", "tagged the plan before the roll plan-2026",
                     "pushed main to origin/main", "pushed plan-2026 to origin"]
    assert repo.tags["plan-2026"].commit.hexsha == before
    assert [p.hexsha for p in repo.head.commit.parents] == [before, rolled.hexsha]
    assert _scenario_row(data / "scenarios" / "retire_alice" / "scenario.csv")["start"] == "2027-01"
    ytd = (data / "scenarios" / "retire_alice" / "year_to_date.csv").read_text()
    assert "projected_2026_1" in ytd, outcome.lines
    assert not repo.is_dirty(untracked_files=True)
    with git.Repo(data_repo_origin) as origin:
        assert "plan-2026" in [t.name for t in origin.tags]

    state = roll_schedule.read_state(data)
    assert state["scenarios"]["retire_alice"]["outcome"] == "rolled"
    assert not state.get("pending")
    status = roll_schedule.describe(state, {"roll": True}, data)
    assert status[0].startswith("roll: retire_alice (nightly 01:00-05:00")
    assert any("retire_alice: rolled 2026-05 -> 2027-01" in line and "plan-2026" in line
               for line in status)
    # Its start month isn't over now: nothing more to roll this month.
    again = roll_schedule.roll(data, worktree, {"roll": True}, now=_local(2027, 1, 6, 2, 0))
    assert again.lines == ["no scenario to roll"] and again.committed is None


def _pending_roll(repo: git.Repo, data: Path, commit_files: Callable[..., str]) -> str:
    """A roll of retire_alice on builder/roll, as :func:`roll_schedule.roll`
    leaves one; its commit."""
    main = repo.head.commit.hexsha
    path = data.parent / SCENARIO
    original = path.read_text()
    rolled = commit_files(repo, {SCENARIO: original.replace("2026-05", "2026-11")}, "Roll retire_alice")
    repo.git.update_ref(f"refs/heads/{roll_schedule.ROLL_BRANCH}", rolled)
    repo.git.reset("--hard", main)
    top = Path(str(repo.working_tree_dir))
    roll_schedule._update_state(top, pending=rolled, night="2026-11-13",
                                pending_paths=["sim/scenarios/base", "sim/scenarios/retire_alice"],
                                pending_tags=[])
    return rolled


def test_a_roll_waits_for_a_clean_main_then_merges(
    marked: tuple[Path, git.Repo], commit_files: Callable[..., str],
) -> None:
    data, repo = marked
    _pending_roll(repo, data, commit_files)
    (data / "scenarios" / "base" / "README.md").write_text("edited\n")
    assert roll_schedule.merge_pending(data) == []
    assert roll_schedule.read_state(data)["waiting"] == "main has uncommitted changes"
    assert "roll waiting to merge into main: main has uncommitted changes" in roll_schedule.describe(
        roll_schedule.read_state(data), None, data)
    repo.git.checkout("--", ".")
    # main moved, but not under the rolled scenarios: the roll still merges.
    commit_files(repo, {"sim/notes.txt": "x\n"}, "elsewhere")
    assert roll_schedule.merge_pending(data)[0] == "merged the roll into main"
    assert "2026-11" in (data.parent / SCENARIO).read_text()


def test_a_roll_main_changed_the_scenario_of_is_dropped_and_done_again(
    marked: tuple[Path, git.Repo], commit_files: Callable[..., str],
) -> None:
    data, repo = marked
    _pending_roll(repo, data, commit_files)
    commit_files(repo, {"sim/scenarios/base/README.md": "changed\n"}, "a parent changed")

    lines = roll_schedule.merge_pending(data)

    assert lines == ["dropped the roll: main changed a rolled scenario since; rolling again from it"]
    state = roll_schedule.read_state(data)
    assert not state["pending"] and state["night"] is None
    assert repo.commit(roll_schedule.ROLL_BRANCH).hexsha == repo.head.commit.hexsha
    assert "2026-05" in (data.parent / SCENARIO).read_text()


def test_a_tag_is_reused_while_it_holds_the_plan_and_never_moved(
    marked: tuple[Path, git.Repo], commit_files: Callable[..., str],
) -> None:
    data, repo = marked
    top = Path(str(repo.working_tree_dir))
    first = repo.head.commit.hexsha
    repo.create_tag("plan-2026", ref=first)
    paths = ["sim/scenarios/base", "sim/scenarios/retire_alice"]
    # Another scenario rolled since: retire_alice's plan is as the tag has it.
    later = commit_files(repo, {"sim/scenarios/other/README.md": "x\n"}, "another")
    assert roll_schedule._tag_for(top, later, 2026, paths) == ("plan-2026", False)
    # retire_alice changed since: a new tag, so plan-2026 keeps what it held.
    changed = commit_files(repo, {"sim/scenarios/retire_alice/README.md": "x\n"}, "edited")
    assert roll_schedule._tag_for(top, changed, 2026, paths) == ("plan-2026-2", True)
    assert repo.tags["plan-2026"].commit.hexsha == first


def test_a_roll_that_needs_an_answer_is_skipped_not_failed() -> None:
    lines = ["Rolling x forward: 2026-05 -> 2026-11",
             "Error: Asset_Checking (default) balance: no default to take with --yes (no run "
             "reaches the month, and the current value isn't one number); run without --yes"]
    assert roll_schedule._NEEDS_YOU in roll_schedule._why(lines)
    assert roll_schedule._why(["  error: a.csv line 2: bad", "Error: this change would leave 1 "
                               "issue(s); nothing was written"]).startswith("error: a.csv line 2: bad;")


def test_scenario_edit_marks_a_scenario_to_roll_and_the_loader_checks_it(
    test_data_dir: Path, test_scenarios_dir: Path,
) -> None:
    def edit(*args: str) -> str:
        result = CliRunner().invoke(sim_group_command, [
            "scenario", "edit", "retire_alice", *args, "--data-dir", str(test_data_dir)])
        assert result.exit_code == 0, result.output
        return _scenario_row(test_scenarios_dir / "retire_alice" / "scenario.csv")["meta_json"]

    assert json.loads(edit("--roll", "monthly")) == {"roll": "monthly"}
    assert roll_schedule.marked(test_data_dir) == ["retire_alice"]
    assert json.loads(edit("--roll", "off")) == {"roll": "off"}
    assert roll_schedule.marked(test_data_dir) == []
    assert json.loads(edit("--roll", "")) == {}
    path = test_scenarios_dir / "retire_alice" / "scenario.csv"
    path.write_text(_csv_text(_scenario_row(path) | {"meta_json": '{"roll": "weekly"}'}))
    check = CliRunner().invoke(sim_group_command, ["check", "retire_alice", "--data-dir", str(test_data_dir)])
    assert check.exit_code != 0
    assert "meta_json roll must be one of monthly, off (got 'weekly')" in check.output


def test_install_records_whether_to_roll_and_when(
    data_repo_sim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = data_repo_sim
    code = tmp_path / "code"
    (code / "data").mkdir(parents=True)
    (code / "data" / "shared").symlink_to(data.parent)
    (data / ".analysis").write_text(f"{code}\n")
    monkeypatch.setattr(builder, "code_dir", lambda: code.resolve())
    paths = builder.BuilderPaths.for_data(data)

    assert "rolls scenarios marked to roll forward each night, 01:00-05:00 local time" \
        in builder.install(data)
    builder.install(data, roll_window="2:30-6:00")  # normalized when recorded
    config = builder.read_config(paths) or {}
    assert config["roll"] is True and config["roll_window"] == "02:30-06:00"
    assert "doesn't roll scenarios forward" in builder.install(data, roll=False)
    builder.install(data)  # kept when not given
    config = builder.read_config(paths) or {}
    assert config["roll"] is False and config["roll_window"] == "02:30-06:00"
    with pytest.raises(ValueError, match="not a window"):
        builder.install(data, roll_window="nights")
    bad = CliRunner().invoke(sim_group_command, [
        "builder", "install", "--roll-window", "nights", "--data-dir", str(data)])
    assert bad.exit_code != 0 and "not a window" in bad.output


def test_a_failed_price_fetch_still_lets_the_tick_roll(
    data_repo_sim: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click import ClickException

    from analysis.sim import price_schedule

    monkeypatch.setattr(builder, "read_config", lambda paths: {"prices": True, "roll": True})

    def broken() -> bool:
        raise ClickException("the fetch broke")

    monkeypatch.setattr(price_schedule, "switched_off", broken)
    tick = CliRunner().invoke(sim_group_command, ["builder", "tick", "--data-dir", str(data_repo_sim)])

    # The roll still has its turn (switched off in the tests), and the tick fails.
    assert "the fetch broke" in tick.output and "rolls are off" in tick.output
    assert tick.exit_code == 1


def test_a_tag_that_fails_is_said_and_the_roll_still_merges(
    marked: tuple[Path, git.Repo], commit_files: Callable[..., str],
) -> None:
    data, repo = marked
    _pending_roll(repo, data, commit_files)
    top = Path(str(repo.working_tree_dir))
    roll_schedule._update_state(top, pending_tags=[
        {"tag": "plan-2026", "base": "0" * 40, "message": "The plan before retire_alice"}])

    lines = roll_schedule.merge_pending(data)

    assert lines[0] == "merged the roll into main"
    assert lines[1].startswith("couldn't tag the plan plan-2026:")
    assert not roll_schedule.read_state(data)["pending"]
    assert "plan-2026" not in [t.name for t in repo.tags]


def test_a_night_that_rolls_nothing_leaves_a_waiting_roll_waiting(
    marked: tuple[Path, git.Repo], commit_files: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, repo = marked
    rolled = _pending_roll(repo, data, commit_files)
    monkeypatch.setattr(roll_schedule, "_roll_one",
                        lambda data, name, month, *, own=False: (1, ["Error: it broke"]))
    worktree = builder.BuilderPaths.for_data(data).roll_worktree

    roll_schedule.roll(data, worktree, {"roll": True}, now=_local(2026, 11, 14, 2, 0))

    state = roll_schedule.read_state(data)
    assert state["pending"] == rolled
    # The hint names the month the night tried, as the command takes it.
    assert any("`sim scenario roll retire_alice 2026-11` does it by hand" in line
               for line in roll_schedule.describe(state, {"roll": True}, data))
    assert roll_schedule.merge_pending(data)[0] == "merged the roll into main"


def test_a_roll_committed_after_the_state_was_read_waits_for_its_state(
    marked: tuple[Path, git.Repo], commit_files: Callable[..., str],
) -> None:
    data, repo = marked
    _pending_roll(repo, data, commit_files)
    newer = commit_files(repo, {"sim/notes.txt": "x\n"}, "a newer roll")
    repo.git.update_ref(f"refs/heads/{roll_schedule.ROLL_BRANCH}", newer)
    repo.git.reset("--hard", "HEAD^")

    assert roll_schedule.merge_pending(data) == []
    assert newer not in [c.hexsha for c in repo.head.commit.parents]


def test_a_quiet_night_reads_the_scenarios_once(
    marked: tuple[Path, git.Repo], monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, _ = marked
    monkeypatch.delenv(roll_schedule.SWITCH_ENV)
    calls: list[date] = []

    def none_due(data_dir: Path, today: date) -> list[tuple[str, str]]:
        calls.append(today)
        return []

    monkeypatch.setattr(roll_schedule, "due_scenarios", none_due)
    config = {"prices": False, "roll": True}
    for minute in (0, 1, 2):
        assert not roll_schedule.due(data, config, _local(2026, 11, 20, 2, minute))
    assert len(calls) == 1
