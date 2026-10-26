"""#300: the background builder's queue, rules and waiting readers."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Callable, Generator
from pathlib import Path

import git
import pytest

from analysis.sim import builder, freshness, locks, provenance
from analysis.sim.builder import BuilderPaths


def _commit(repo: git.Repo, files: dict[str, str], message: str) -> None:
    root = Path(str(repo.working_tree_dir))
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    repo.git.add(A=True)
    actor = git.Actor("t", "t@t")
    repo.index.commit(message, author=actor, committer=actor)


@pytest.fixture
def code_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> git.Repo:
    """A stand-in code repo on main, so the main check doesn't depend on
    this checkout's branch or uncommitted changes."""
    repo = git.Repo.init(tmp_path / "code", b="main")
    with repo.config_writer() as cw:
        cw.set_value("commit", "gpgsign", "false")
    _commit(repo, {"src/engine.py": "v1\n", "templates/t.csv": "", "pyproject.toml": "",
                   "uv.lock": "", "docs/guide.md": "one\n"}, "init")
    monkeypatch.setattr(freshness, "code_tree", lambda: freshness.code_tree_of(repo))
    return repo


@pytest.fixture
def builder_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANALYSIS_BUILDER", raising=False)


def test_builds_only_from_committed_code_matching_main(
    code_repo: git.Repo, builder_on: None,
) -> None:
    trees, reason = builder.main_check()
    assert trees is not None and reason is None

    code_repo.git.checkout("-b", "feature")
    _commit(code_repo, {"docs/guide.md": "two\n"}, "docs only")
    assert builder.main_check()[0] == trees  # docs aren't code paths

    _commit(code_repo, {"src/engine.py": "v2\n"}, "code")
    assert builder.main_check() == (None, "code (src, templates, pyproject.toml, uv.lock) differs from main")
    assert builder.main_check("feature")[0] is not None  # the workspace's branch
    assert builder.main_check("nope") == (None, "no local nope branch")

    code_repo.git.checkout("main")
    (Path(str(code_repo.working_tree_dir)) / "src" / "engine.py").write_text("dirty\n")
    assert builder.main_check() == (None, "uncommitted code in src, templates, pyproject.toml, uv.lock")


@pytest.fixture
def pair(tmp_path: Path, test_data_dir: Path, commit_test_data: Callable[[], Path],
         monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A code worktree and data dir naming each other: (code dir, data dir)."""
    data_repo = tmp_path / "datarepo"
    data_dir = data_repo / "sim"
    data_repo.mkdir()
    test_data_dir.rename(data_dir)
    repo = git.Repo.init(data_repo, b="main")
    with repo.config_writer() as cw:
        cw.set_value("commit", "gpgsign", "false")
    _commit(repo, {".gitignore": "sim/.analysis\nstate/\n"}, "init")
    code = tmp_path / "code"
    (code / "data").mkdir(parents=True, exist_ok=True)
    (code / "data" / "shared").symlink_to(data_repo)
    (data_dir / ".analysis").write_text(f"{code}\n")
    monkeypatch.setattr(builder, "code_dir", lambda: code.resolve())
    return code.resolve(), data_dir.resolve()


def test_a_pair_must_name_each_other(pair: tuple[Path, Path], tmp_path: Path) -> None:
    code, data_dir = pair
    assert builder.pair_problems(code, data_dir) == []
    (data_dir / ".analysis").write_text(f"{tmp_path / 'elsewhere'}\n")
    assert builder.pair_problems(code, data_dir) == [f"{data_dir / '.analysis'} doesn't name {code}"]


def test_install_records_the_pair_and_chains_existing_hooks(
    pair: tuple[Path, Path], tmp_path: Path,
) -> None:
    _code, data_dir = pair
    hooks = data_dir.parent / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    seen = tmp_path / "seen"
    # An early exit: anything appended after it would never run.
    mine = f'#!/bin/sh\necho "$1" > {seen}\nexit 3\necho never\n'
    (hooks / "post-merge").write_text(mine)
    (hooks / "post-merge").chmod(0o755)
    builder.install(data_dir)
    assert builder.read_config(BuilderPaths.for_data(data_dir)) is not None
    assert builder.HOOK_MARKER in (hooks / "post-commit").read_text()
    assert builder.HOOK_MARKER in (hooks / "post-merge").read_text()
    assert (hooks / "post-merge.before-builder").read_text() == mine
    builder.install(data_dir)  # again: nothing moved twice
    assert (hooks / "post-merge.before-builder").read_text() == mine
    ran = subprocess.run([str(hooks / "post-merge"), "1"], cwd=data_dir, check=False)
    assert ran.returncode == 3 and seen.read_text() == "1\n"  # theirs ran, status kept


def test_a_reader_asks_again_once_a_repo_changes(
    pair: tuple[Path, Path], builder_on: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _code, data_dir = pair
    builder.install(data_dir)
    asked: list[str] = []
    monkeypatch.setattr(builder, "_pending", lambda _p, _d, name: asked.append(name) or False)
    runs = BuilderPaths.for_data(data_dir).runs_dir
    builder.await_fresh(runs, "base")
    builder.await_fresh(runs, "base")
    assert asked == ["base"]  # nothing changed in between
    time.sleep(0.01)
    _commit(git.Repo(data_dir.parent), {"sim/notes.md": "x\n"}, "a data commit")
    builder.await_fresh(runs, "base")
    assert asked == ["base", "base"]


def test_install_records_the_branch_every_reader_uses(pair: tuple[Path, Path]) -> None:
    _code, data_dir = pair
    paths = BuilderPaths.for_data(data_dir)
    builder.install(data_dir)
    assert builder.config_branch(builder.read_config(paths)) == "main"
    builder.install(data_dir, "testing")
    builder.install(data_dir)  # reinstalling (setup.sh) keeps it
    assert builder.config_branch(builder.read_config(paths)) == "testing"


def test_notify_starts_the_builder_only_where_installed(
    pair: tuple[Path, Path], builder_on: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _code, data_dir = pair
    started: list[Path] = []
    monkeypatch.setattr(builder, "spawn_daemon", lambda _paths, d: started.append(d))
    assert "aren't installed" in builder.notify(data_dir)
    builder.install(data_dir)
    assert builder.notify(data_dir) == "started the background builder"
    assert started == [data_dir]
    monkeypatch.setenv("ANALYSIS_BUILDER", "off")
    assert "off" in builder.notify(data_dir)
    assert started == [data_dir]


@pytest.fixture
def daemon(
    pair: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> Generator[builder._Daemon]:
    """A daemon over the pair, its code on main, every scenario stale, its
    builds a stand-in command (FAKE_BUILD: sleep seconds and exit code)."""
    _code, data_dir = pair
    monkeypatch.setattr(builder, "main_check", lambda *_a: (("t1",), None))
    monkeypatch.setattr(freshness, "unchanged_run", lambda *_a: None)
    monkeypatch.setattr(freshness, "code_tree", lambda: freshness.CodeTree(None, ("t1",)))
    monkeypatch.setenv("FAKE_BUILD", "0 0")
    monkeypatch.setattr(builder, "_sim_executable", lambda: [
        "sh", "-c", 'set -- $FAKE_BUILD; sleep "$1"; exit "$2"', "sim"])
    d = builder._Daemon(data_dir, echo=lambda _text: None)
    d.started_trees = ("t1",)
    yield d
    d.cancel("test over")


def test_a_daemon_from_other_code_does_not_start(
    daemon: builder._Daemon, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder._write_json(daemon.paths.config, {
        "data_dir": str(daemon.data_dir), "code_dir": str(builder.code_dir())})
    monkeypatch.setattr(builder, "code_dir", lambda: tmp_path / "another-worktree")
    lines: list[str] = []
    daemon.echo = lines.append
    assert daemon.run() == 1
    assert any("but the pair's code is" in line for line in lines)
    assert not builder.daemon_alive(daemon.paths)


def _wait_until_done(d: builder._Daemon) -> None:
    assert d.build is not None
    d.build.process.wait(timeout=10)
    assert d.finished()


def test_queue_puts_the_current_scenario_first(daemon: builder._Daemon) -> None:
    (daemon.data_dir / ".current_scenario").write_text("retire_alice\n")
    daemon.scan()
    assert daemon.queue[0] == "retire_alice" and "base" in daemon.queue
    assert daemon.state == "building"
    daemon.first = ["base"]
    daemon.scan()
    assert daemon.queue[0] == "base"


def test_a_failed_build_waits_for_its_inputs_to_change(
    daemon: builder._Daemon, monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon.scan()
    first = daemon.queue[0]
    monkeypatch.setenv("FAKE_BUILD", "0 3")
    daemon.start_next(("t1",))
    _wait_until_done(daemon)
    assert daemon.failed[first]["exit"] == 3
    daemon.scan()
    assert first not in daemon.queue  # same inputs: not retried
    tree_id = provenance.data_stamps(daemon.data_dir, first).tree_id
    daemon.failed[first]["key"] = [["t1"], f"not {tree_id}"]
    daemon.scan()
    assert first in daemon.queue  # its data changed since


@pytest.mark.slow
def test_a_build_whose_inputs_changed_is_cancelled(
    daemon: builder._Daemon, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_BUILD", "30 0")
    daemon.scan()
    daemon.start_next(("t1",))
    build = daemon.build
    assert build is not None
    daemon.scan()
    assert daemon.build is build  # nothing changed
    build.key = [["t1"], "an older data tree"]
    daemon.scan()
    assert daemon.build is None and build.process.poll() is not None
    assert build.scenario in daemon.queue  # requeued


def test_the_daemon_checks_the_installed_branch(
    daemon: builder._Daemon, monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder.install(daemon.data_dir, "testing")
    seen: list[str] = []
    monkeypatch.setattr(builder, "main_check",
                        lambda branch, *_a: seen.append(branch) or (("t1",), None))
    daemon.scan()
    assert seen == ["testing"]


def test_a_wanted_scenario_preempts_the_build_in_progress(
    daemon: builder._Daemon, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_BUILD", "30 0")
    daemon.scan()
    daemon.start_next(("t1",))
    running = daemon.build
    assert running is not None
    wanted = daemon.queue[0]
    daemon.first = [wanted]
    daemon.scan()
    daemon.preempt()
    assert daemon.build is None and running.process.poll() is not None
    assert daemon.queue[:2] == [wanted, running.scenario]  # requeued next
    daemon.start_next(("t1",))
    assert daemon.build is not None and daemon.build.wanted
    # A build someone is waiting on is never preempted in turn.
    daemon.first = [running.scenario]
    daemon.preempt()
    assert daemon.build is not None and daemon.build.scenario == wanted


def test_code_off_main_disables_the_builder(
    daemon: builder._Daemon, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(builder, "main_check", lambda *_a: (None, "code differs from main"))
    daemon.scan()
    assert (daemon.state, daemon.queue, daemon.reason) == ("disabled", [], "code differs from main")


def test_a_code_change_restarts_the_builder(
    daemon: builder._Daemon, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(builder, "main_check", lambda *_a: (("t2",), None))
    daemon.scan()
    assert daemon.restart


def test_readers_dont_wait_where_the_builder_is_off_or_not_installed(
    pair: tuple[Path, Path], builder_on: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _code, data_dir = pair
    paths = BuilderPaths.for_data(data_dir)
    monkeypatch.setattr(builder, "_pending", lambda *_a: pytest.fail("checked"))
    builder.await_fresh(paths.runs_dir, "base")  # not installed
    builder.install(data_dir)
    # A build (sim load/run/all) may hold the lock the builder's own build
    # of the scenario waits for, so it never waits on the builder.
    with locks.building():
        builder.await_fresh(paths.runs_dir, "base")
    monkeypatch.setenv("ANALYSIS_BUILDER", "off")
    builder.await_fresh(paths.runs_dir, "base")


@pytest.fixture
def running_daemon(pair: tuple[Path, Path]) -> Generator[BuilderPaths]:
    """The pair, installed, with its daemon lock held as a daemon would."""
    import fcntl

    _code, data_dir = pair
    builder.install(data_dir)
    paths = BuilderPaths.for_data(data_dir)
    fd = os.open(paths.daemon_lock, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    yield paths
    os.close(fd)


@pytest.mark.slow
def test_a_reader_waits_for_a_queued_build(
    running_daemon: BuilderPaths, builder_on: None,
) -> None:
    paths = running_daemon
    builder._write_json(paths.status, {"state": "building", "queue": ["base"], "updated": 0})

    def finish() -> None:
        time.sleep(1.0)
        requests = list(paths.requests.glob("*.json"))
        assert requests, "the reader asked for base first"
        builder._write_json(paths.status, {"state": "idle", "queue": [], "updated": time.time()})

    lines: list[str] = []
    finisher = threading.Thread(target=finish)
    finisher.start()
    started = time.monotonic()
    builder.await_fresh(paths.runs_dir, "base", echo=lines.append)
    finisher.join()
    assert time.monotonic() - started >= 0.9
    assert lines == [
        "waiting for the background build of base (Ctrl-C to stop waiting)...",
        "the background build of base is done",
    ]
