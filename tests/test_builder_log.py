"""#314: the background builder's log as records: daily files, `sim builder
log`, and pruning by `.retention.yaml`'s `logs:` rules."""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim import builder_log as blog
from analysis.sim.retention import DEFAULT_RETENTION, RETENTION_FILENAME, parse_retention


@pytest.fixture
def builder_dir(test_data_dir: Path, state_dir: Path) -> Path:
    """`state/builder/` beside the test data dir, as `sim builder` finds it."""
    return state_dir / "builder"


def _at(minutes_ago: float) -> datetime:
    return datetime.now().replace(microsecond=0) - timedelta(minutes=minutes_ago)


def _write_build(writer: blog.LogWriter, name: str, minutes_ago: float, outcome: str,
                 output: list[str]) -> None:
    writer.event(f"building {name}", _at(minutes_ago))
    for line in output:
        writer.output(line)
    writer.event(outcome, _at(minutes_ago - 1))


def _log(*args: str, data_dir: Path) -> list[str]:
    result = CliRunner().invoke(main, ["sim", "builder", "log", "--data-dir", str(data_dir), *args])
    assert result.exit_code == 0, result.output
    return result.output.splitlines()


def test_a_record_runs_from_one_event_to_the_next(builder_dir: Path) -> None:
    writer = blog.LogWriter(builder_dir / "logs")
    _write_build(writer, "base", 5, "built base (1m00s)", ["Loaded base", "  nested"])
    records = blog.read_records(builder_dir)
    assert [r.text for r in records] == ["building base", "built base (1m00s)"]
    assert records[0].body == ["  Loaded base", "    nested"]
    assert records[0].output() == records[0].body and records[0].always() == []


def test_the_log_shows_the_last_hour_newest_first_without_output(
    builder_dir: Path, test_data_dir: Path,
) -> None:
    writer = blog.LogWriter(builder_dir / "logs")
    _write_build(writer, "old", 120, "built old (1m00s)", ["old output"])
    _write_build(writer, "base", 10, "built base (1m00s)", ["base output"])
    lines = _log(data_dir=test_data_dir)
    assert [ln[20:] for ln in lines] == ["built base (1m00s)", "building base"]
    verbose = _log("-v", data_dir=test_data_dir)
    assert verbose[-1] == "  base output"
    assert len(_log("-a", data_dir=test_data_dir)) == 4
    assert [ln[20:] for ln in _log("-n", "3", data_dir=test_data_dir)][-1] == "built old (1m00s)"
    assert len(_log("--since", "3 hours", data_dir=test_data_dir)) == 4
    assert len(_log("-n", "1", "--since", "3 hours", data_dir=test_data_dir)) == 1


def test_a_failed_build_shows_the_end_of_its_output(
    builder_dir: Path, test_data_dir: Path,
) -> None:
    writer = blog.LogWriter(builder_dir / "logs")
    output = [f"line {i}" for i in range(15)]
    _write_build(writer, "base", 10, "base failed (exit 3, 1m00s)", output)
    lines = _log(data_dir=test_data_dir)
    assert lines[0].endswith("base failed (exit 3, 1m00s)")
    assert lines[1] == f"  ... last {blog.FAILED_TAIL} line(s) of its output:"
    assert lines[2:2 + blog.FAILED_TAIL] == [f"  line {i}" for i in range(5, 15)]
    assert lines[-1].endswith("building base")  # its own output stays hidden


def test_unindented_lines_after_an_event_are_always_shown(
    builder_dir: Path, test_data_dir: Path,
) -> None:
    writer = blog.LogWriter(builder_dir / "logs")
    writer.event("started (pid 1)", _at(1))
    with open(blog.file_for(builder_dir / "logs", datetime.now()), "a") as f:
        f.write("Traceback (most recent call last):\n  File x\nValueError: boom\n")
    assert _log(data_dir=test_data_dir)[1:] == ["Traceback (most recent call last):",
                                               "ValueError: boom"]


def test_a_day_change_starts_a_new_file_and_the_files_read_as_one_log(builder_dir: Path) -> None:
    days: list[str] = []
    writer = blog.LogWriter(builder_dir / "logs", on_new_day=lambda: days.append("new"))
    yesterday = _at(24 * 60)
    writer.event("building base", yesterday)
    writer.output("before midnight")
    writer.event("built base (1s)", _at(0))
    writer.output("after")  # follows its record into today's file
    files = blog.log_files(builder_dir)
    assert [f.name for f in files] == [f"builder-{yesterday:%Y-%m-%d}.log",
                                       f"builder-{datetime.now():%Y-%m-%d}.log"]
    assert days == ["new"]
    records = blog.read_records(builder_dir)
    assert [r.text for r in records] == ["building base", "built base (1s)"]
    assert records[0].body == ["  before midnight"] and records[1].body == ["  after"]


def test_logs_rules_prune_old_files_and_keep_the_newest(builder_dir: Path) -> None:
    log_dir = builder_dir / "logs"
    log_dir.mkdir(parents=True)
    now = time.time()
    for days_ago in range(10):
        day = datetime.now() - timedelta(days=days_ago)
        path = blog.file_for(log_dir, day)
        path.write_text("x\n")
        os.utime(path, (now - days_ago * 86400, now - days_ago * 86400))
    rules = parse_retention("logs:\n  - keep: 3\n", base=builder_dir).logs
    doomed = blog.plan_log_prune(builder_dir, rules)
    assert len(doomed) == 7 and blog.file_for(log_dir, datetime.now()) not in doomed
    rules = parse_retention("logs:\n  - since: 5 days\n", base=builder_dir).logs
    # Aged from the end of each file's day: the one from 5 days ago ended
    # within the last 5 days, the one from 6 days ago didn't.
    assert len(blog.plan_log_prune(builder_dir, rules)) == 4
    assert blog.plan_log_prune(builder_dir, None) == []  # no section: keep all


def test_prune_logs_follows_the_data_repos_rules(builder_dir: Path, test_data_dir: Path) -> None:
    log_dir = builder_dir / "logs"
    log_dir.mkdir(parents=True)
    old = blog.file_for(log_dir, datetime.now() - timedelta(days=60))
    old.write_text("x\n")
    then = time.time() - 60 * 86400
    os.utime(old, (then, then))
    blog.file_for(log_dir, datetime.now()).write_text("x\n")
    assert parse_retention(DEFAULT_RETENTION, base=test_data_dir).logs is not None
    assert blog.prune_logs(test_data_dir, builder_dir) == []  # no .retention.yaml: keep all
    (test_data_dir / RETENTION_FILENAME).write_text("logs:\n  - since: 30 days\n")
    assert blog.prune_logs(test_data_dir, builder_dir) == [old]
    assert not old.exists()


def test_follow_prints_new_lines_across_files(builder_dir: Path) -> None:
    log_dir = builder_dir / "logs"
    writer = blog.LogWriter(log_dir)
    writer.event("started (pid 1)", _at(24 * 60))
    seen: list[str] = []
    done = threading.Event()
    thread = threading.Thread(target=blog.follow, args=(builder_dir, seen.append),
                              kwargs={"verbose": False, "stop": done.is_set, "poll": 0.01})
    thread.start()
    try:
        time.sleep(0.05)
        writer.event("building base", _at(1))  # today: a new file
        writer.output("hidden without -v")
        writer.output("the error")
        writer.event("base failed (exit 1, 1s)", _at(0))
        deadline = time.monotonic() + 5
        while len(seen) < 5 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        done.set()
        thread.join(timeout=5)
    assert [ln[20:] if ln[:1].isdigit() else ln for ln in seen] == [
        "building base", "base failed (exit 1, 1s)",
        "  ... last 2 line(s) of its output:", "  hidden without -v", "  the error",
    ]


def test_all_cannot_be_combined_with_a_limit(test_data_dir: Path) -> None:
    result = CliRunner().invoke(main, ["sim", "builder", "log", "--data-dir", str(test_data_dir),
                                       "-a", "-n", "3"])
    assert result.exit_code == 2 and "--all" in result.output


def test_retention_ranks_log_files_by_the_date_in_their_names(builder_dir: Path) -> None:
    log_dir = builder_dir / "logs"
    log_dir.mkdir(parents=True)
    today = blog.file_for(log_dir, datetime.now())
    yesterday = blog.file_for(log_dir, datetime.now() - timedelta(days=1))
    older = blog.file_for(log_dir, datetime.now() - timedelta(days=2))
    for path in (today, yesterday, older):
        path.write_text("x\n")
    # A late write to an older file (a build begun before midnight) doesn't
    # move it up: it ranks by its name.
    then = time.time() - 86400 * 5
    os.utime(yesterday, (then, then))
    os.utime(older, None)
    rules = parse_retention("logs:\n  - keep: 2\n", base=builder_dir).logs
    assert blog.plan_log_prune(builder_dir, rules) == [older]


def test_lines_before_a_files_first_event_are_dated_by_its_last_write(
    builder_dir: Path, test_data_dir: Path,
) -> None:
    path = blog.file_for(builder_dir / "logs", datetime.now())
    path.parent.mkdir(parents=True)
    path.write_text("Traceback (most recent call last):\nImportError: boom\n")
    assert _log(data_dir=test_data_dir) == ["Traceback (most recent call last):",
                                            "ImportError: boom"]


def test_follow_counts_the_output_of_a_build_already_running(builder_dir: Path) -> None:
    writer = blog.LogWriter(builder_dir / "logs")
    writer.event("building base", _at(1))
    writer.output("written before -f")
    seen: list[str] = []
    done = threading.Event()
    thread = threading.Thread(target=blog.follow, args=(builder_dir, seen.append),
                              kwargs={"verbose": False, "stop": done.is_set, "poll": 0.01})
    thread.start()
    try:
        time.sleep(0.05)
        writer.output("after")
        writer.event("base failed (exit 1, 1s)", _at(0))
        deadline = time.monotonic() + 5
        while len(seen) < 4 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        done.set()
        thread.join(timeout=5)
    assert seen[1:] == ["  ... last 2 line(s) of its output:", "  written before -f", "  after"]
