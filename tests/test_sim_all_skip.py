"""#298: `sim all` skips a scenario whose latest run can't have changed."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import git
import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim import freshness


def _commit(repo: git.Repo, files: dict[str, str], message: str) -> str:
    root = Path(str(repo.working_tree_dir))
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    repo.git.add(A=True)
    actor = git.Actor("t", "t@t")
    return repo.index.commit(message, author=actor, committer=actor).hexsha


@pytest.fixture
def code_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> git.Repo:
    """A stand-in code repo `sim all` compares runs against, so the test
    doesn't depend on whether this checkout has uncommitted changes."""
    repo = git.Repo.init(tmp_path / "code", b="main")
    with repo.config_writer() as cw:
        cw.set_value("commit", "gpgsign", "false")
    _commit(repo, {"src/engine.py": "v1\n", "pyproject.toml": "", "uv.lock": "",
                   "docs/guide.md": "one\n"}, "init")

    monkeypatch.setattr(freshness, "code_tree", lambda: freshness.code_tree_of(repo))
    return repo


def _stamp_runs(runs_dir: Path, repo: git.Repo) -> None:
    """Record the stand-in repo's HEAD, clean, as the code every run ran,
    and date the plain runs earlier, in order, so a rerun is always the
    newest."""
    for n, run_file in enumerate(sorted(runs_dir.glob("*_*.sqlite3"))):
        run_file.rename(run_file.with_name(
            f"2000-01-01T{n:06d}Z_{run_file.name.split('_', 1)[1]}"))
    for run_file in runs_dir.rglob("*.sqlite3"):
        with sqlite3.connect(run_file) as con:
            if con.execute("SELECT 1 FROM sqlite_master WHERE name = 'simulation_run'").fetchone():
                con.execute("UPDATE simulation_run SET code_version = ?, code_dirty = 0",
                            (repo.head.commit.hexsha,))


@pytest.fixture
def sim_all(
    commit_test_data: Callable[[], Path], intent_dir: Path, runs_dir: Path,
) -> Callable[..., str]:
    data_dir = commit_test_data()

    def _run(*args: str) -> str:
        result = CliRunner().invoke(main, [
            "sim", "all", "--data-dir", str(data_dir), "--intent-dir", str(intent_dir),
            "--runs-dir", str(runs_dir), "--allow-dirty-code", *args,
        ])
        assert result.exit_code == 0, result.output
        return result.stdout
    return _run


def _skipped(output: str) -> list[str]:
    lines = output.splitlines()
    return [lines[i - 1].strip("= ") for i, line in enumerate(lines)
            if line.strip().startswith("unchanged since")]


def test_sim_all_skips_scenarios_whose_runs_cannot_have_changed(
    sim_all: Callable[..., str], code_repo: git.Repo, runs_dir: Path,
) -> None:
    first = sim_all()
    assert _skipped(first) == []
    _stamp_runs(runs_dir, code_repo)

    second = sim_all()
    assert _skipped(second) == ["base", "retire_alice"]
    assert "all 2 scenario(s) succeeded (2 unchanged, skipped)" in second
    assert _skipped(sim_all("--force")) == []


def test_a_change_to_code_reruns_but_a_docs_commit_does_not(
    sim_all: Callable[..., str], code_repo: git.Repo, runs_dir: Path,
) -> None:
    sim_all()
    _stamp_runs(runs_dir, code_repo)
    _commit(code_repo, {"docs/guide.md": "two\n"}, "docs only")
    assert _skipped(sim_all()) == ["base", "retire_alice"]

    _commit(code_repo, {"src/engine.py": "v2\n"}, "engine change")
    assert _skipped(sim_all()) == []


def test_uncommitted_code_always_reruns(
    sim_all: Callable[..., str], code_repo: git.Repo, runs_dir: Path,
) -> None:
    sim_all()
    _stamp_runs(runs_dir, code_repo)
    (Path(str(code_repo.working_tree_dir)) / "src" / "engine.py").write_text("edited\n")
    assert _skipped(sim_all()) == []


def test_a_data_change_reruns_only_the_scenarios_that_read_it(
    sim_all: Callable[..., str], code_repo: git.Repo, runs_dir: Path, test_data_dir: Path,
) -> None:
    sim_all()
    _stamp_runs(runs_dir, code_repo)
    data = git.Repo(test_data_dir)
    readme = test_data_dir / "scenarios" / "retire_alice" / "notes.txt"
    readme.write_text("a note\n")
    assert _skipped(sim_all()) == ["base"]  # retire_alice's data is dirty

    _commit(data, {}, "a note")
    assert _skipped(sim_all()) == ["base"]  # changed since its run
    _stamp_runs(runs_dir, code_repo)
    assert _skipped(sim_all()) == ["base", "retire_alice"]


def test_a_run_made_from_dirty_code_is_never_trusted(
    sim_all: Callable[..., str], code_repo: git.Repo, runs_dir: Path,
) -> None:
    sim_all()
    _stamp_runs(runs_dir, code_repo)
    for run_file in runs_dir.glob("*retire_alice*.sqlite3"):
        with sqlite3.connect(run_file) as con:
            con.execute("UPDATE simulation_run SET code_dirty = 1")
    assert _skipped(sim_all()) == ["base"]


@pytest.mark.slow
def test_an_ensemble_batch_of_another_size_reruns(
    sim_all: Callable[..., str], code_repo: git.Repo, runs_dir: Path,
    test_data_dir: Path, intent_dir: Path,
) -> None:
    """A batch `sim run --ensemble-size N` made at another size isn't the
    one `sim all` would make; the batch it then makes is skipped next time."""
    def sim(*args: str) -> None:
        result = CliRunner().invoke(main, ["sim", *args, "--data-dir", str(test_data_dir)])
        assert result.exit_code == 0, result.output

    sim("ensemble", "create", "recipe", "retire_alice", "--size", "2")
    sim("calendar", "add", "set-rate", "2027-02-28", "Asset_Checking", "4.5",
        "--scenario", "recipe", "--name", "checking_rate_vary", "--vary-target", "amount",
        "--vary-distribution", "uniform", "--vary-low", "4.0", "--vary-high", "5.0")
    _commit(git.Repo(test_data_dir), {}, "recipe")
    result = CliRunner().invoke(main, [
        "sim", "run", "recipe", "--ensemble-size", "3", "--data-dir", str(test_data_dir),
        "--intent-dir", str(intent_dir), "--runs-dir", str(runs_dir), "--allow-dirty-code"])
    assert result.exit_code == 0, result.output
    _stamp_runs(runs_dir, code_repo)
    assert "recipe" not in _skipped(sim_all())
    _stamp_runs(runs_dir, code_repo)
    assert _skipped(sim_all()) == ["base", "recipe", "retire_alice"]


def test_a_batch_with_unreadable_state_reruns(
    code_repo: git.Repo, runs_dir: Path, test_data_dir: Path,
) -> None:
    """An interrupted batch's half-written manifest or member file makes
    it not fresh, rather than stopping `sim all`."""
    batch = runs_dir / "recipe" / "2000-01-01T000000Z"
    batch.mkdir(parents=True)
    (batch / "member_0000.sqlite3").write_bytes(b"not a database, cut off")
    (batch / "batch_manifest.json").write_text('{"member_count": ')
    code = freshness.code_tree_of(code_repo)
    assert freshness.unchanged_run(test_data_dir, runs_dir, "recipe", code) is None
