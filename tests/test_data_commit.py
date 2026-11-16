"""#308: `sim run`/`load`/`all` offer to commit a scenario's dirty data, and
`sim commit` does it on its own."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import git
import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.sim import cli, data_commit
from analysis.sim.locks import BUILDER_CHILD_ENV
from analysis.sim.provenance import scoped_changes


@pytest.fixture
def data(commit_test_data: Callable[[], Path]) -> Path:
    return commit_test_data()


@pytest.fixture
def repo(data: Path) -> git.Repo:
    return git.Repo(data)


def _sim(*args: str, input: str | None = None) -> Result:
    return CliRunner().invoke(main, ["sim", *args], input=input)


def _state(intent_dir: Path, runs_dir: Path) -> tuple[str, ...]:
    return ("--intent-dir", str(intent_dir), "--runs-dir", str(runs_dir), "--allow-dirty-code")


def _edit(data: Path, relative: str, text: str = "# edited\n") -> None:
    path = data / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(text)


def _log(repo: git.Repo) -> list[str]:
    return [str(c.summary) for c in repo.iter_commits()]


def _status(repo: git.Repo) -> str:
    return repo.git.status("--porcelain")


# --- what is committed -------------------------------------------------------

def test_only_the_scenarios_scoped_changes_count(data: Path) -> None:
    _edit(data, "scenarios/base/README.md")  # a parent's folder is in scope
    _edit(data, "scenarios/retire_alice/notes/new.md")
    _edit(data, "persons.csv", "")  # unchanged content: not a change
    _edit(data, "unrelated.txt")
    changes = scoped_changes(data, ["retire_alice"])
    assert sorted(changes) == [(" M", "scenarios/base/README.md"),
                               ("??", "scenarios/retire_alice/notes/new.md")]


def test_commit_leaves_everything_else_alone(data: Path, repo: git.Repo) -> None:
    _edit(data, "scenarios/retire_alice/README.md")
    _edit(data, "unrelated.txt")
    _edit(data, "staged.txt")
    repo.git.add("staged.txt")
    sha, subject = data_commit.commit(data, scoped_changes(data, ["retire_alice"]),
                                      command="sim run retire_alice")
    assert subject == "Edit retire_alice: README.md"
    assert repo.head.commit.hexsha == sha
    assert list(repo.head.commit.stats.files) == ["scenarios/retire_alice/README.md"]
    assert sorted(_status(repo).splitlines()) == ["?? unrelated.txt", "A  staged.txt"]
    assert "Committed by `sim run retire_alice`" in repo.head.commit.message


def test_commit_records_a_rename_and_a_deletion(data: Path, repo: git.Repo) -> None:
    base = data / "scenarios" / "base"
    repo.git.mv(str(base / "README.md"), str(base / "NOTES.md"))
    (data / "scenarios" / "retire_alice" / "year_to_date.csv").unlink()
    data_commit.commit(data, scoped_changes(data, ["retire_alice"]))
    assert _status(repo) == ""
    message = repo.head.commit.message
    assert message.splitlines()[0] == "Edit base, retire_alice"
    assert "- scenarios/retire_alice/year_to_date.csv (deleted)" in message
    assert "- scenarios/base/README.md (deleted)" in message


def test_message_names_scenarios_and_shared_files(tmp_path: Path) -> None:
    data = tmp_path / "sim"
    changes = [(" M", "sim/scenarios/a/x.csv"), ("??", "sim/scenarios/b/y.csv"),
               (" M", "sim/persons.csv")]
    text = data_commit.message(data, tmp_path, changes, "sim all")
    assert text.splitlines()[0] == "Edit a, b, persons.csv"
    assert "- scenarios/b/y.csv (new)" in text


def test_a_long_subject_is_cut_with_a_count(tmp_path: Path) -> None:
    data = tmp_path / "sim"
    changes = [(" M", f"sim/scenarios/base/file_number_{i}.csv") for i in range(12)]
    subject = data_commit.message(data, tmp_path, changes, "").splitlines()[0]
    assert len(subject) <= 72
    assert subject.startswith("Edit base: file_number_0.csv, ")
    assert subject.endswith(" more")


@pytest.mark.parametrize("marker", ["MERGE_HEAD", "rebase-merge"])
def test_no_commit_in_the_middle_of_an_operation(data: Path, repo: git.Repo, marker: str) -> None:
    _edit(data, "scenarios/retire_alice/README.md")
    path = Path(repo.git_dir) / marker
    if marker == "MERGE_HEAD":
        path.write_text(repo.head.commit.hexsha + "\n")
    else:
        path.mkdir()
    with pytest.raises(data_commit.CommitError, match="middle of"):
        data_commit.commit(data, scoped_changes(data, ["retire_alice"]))


def test_no_commit_on_a_detached_head(data: Path, repo: git.Repo) -> None:
    repo.git.checkout("--detach")
    _edit(data, "scenarios/retire_alice/README.md")
    with pytest.raises(data_commit.CommitError, match="detached"):
        data_commit.commit(data, scoped_changes(data, ["retire_alice"]))


@pytest.mark.parametrize(("env_email", "email"), [
    ("", "alice@analysis.local"),
    # Set in the repo too: the builder or an editor may not have $EMAIL.
    ("alice@example.com", "alice@example.com"),
])
def test_a_missing_identity_is_set_in_the_data_repo_only(
    data: Path, repo: git.Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    env_email: str, email: str,
) -> None:
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("ANALYSIS_USER", "alice")
    monkeypatch.setenv("EMAIL", env_email)
    _edit(data, "scenarios/retire_alice/README.md")
    data_commit.commit(data, scoped_changes(data, ["retire_alice"]))
    reader = repo.config_reader("repository")
    assert reader.get_value("user", "email") == email
    assert str(reader.get_value("user", "name")).startswith("Alice")
    assert empty.read_text() == ""


# --- the CLI -------------------------------------------------------------------

def test_run_commit_commits_then_runs_clean(
    data: Path, repo: git.Repo, intent_dir: Path, runs_dir: Path,
) -> None:
    _edit(data, "scenarios/retire_alice/README.md")
    result = _sim("run", "retire_alice", "--data-dir", str(data), *_state(intent_dir, runs_dir),
                  "--commit")
    assert result.exit_code == 0, result.output
    assert "committed " in result.stderr
    assert _log(repo)[0] == "Edit retire_alice: README.md"
    assert _status(repo) == ""


def test_without_a_terminal_a_dirty_run_still_refuses(
    data: Path, repo: git.Repo, intent_dir: Path, runs_dir: Path,
) -> None:
    _edit(data, "scenarios/retire_alice/README.md")
    result = _sim("run", "retire_alice", "--data-dir", str(data), *_state(intent_dir, runs_dir))
    assert result.exit_code != 0
    assert "sim commit" in result.stderr and "--commit" in result.stderr
    assert _log(repo) == ["init"]


@pytest.mark.parametrize(("answer", "committed"), [("\n", True), ("y\n", True), ("n\n", False)])
def test_at_a_terminal_run_asks(
    data: Path, repo: git.Repo, intent_dir: Path, runs_dir: Path,
    monkeypatch: pytest.MonkeyPatch, answer: str, committed: bool,
) -> None:
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    _edit(data, "scenarios/retire_alice/README.md")
    result = _sim("run", "retire_alice", "--data-dir", str(data), *_state(intent_dir, runs_dir),
                  input=answer)
    assert result.stderr.count("Commit them so the run is reproducible?") == 1
    assert "scenarios/retire_alice/README.md" in result.stderr
    assert (result.exit_code == 0) == committed, result.output
    assert (_log(repo)[0] != "init") == committed


def test_allow_dirty_data_neither_asks_nor_commits(
    data: Path, repo: git.Repo, intent_dir: Path, runs_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    _edit(data, "scenarios/retire_alice/README.md")
    result = _sim("run", "retire_alice", "--data-dir", str(data), *_state(intent_dir, runs_dir),
                  "--allow-dirty-data")
    assert result.exit_code == 0, result.output
    assert "Commit them" not in result.stderr
    assert _log(repo) == ["init"]


def test_load_asks_once_for_its_run_too(
    data: Path, repo: git.Repo, intent_dir: Path, runs_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    _edit(data, "scenarios/retire_alice/README.md")
    result = _sim("load", "retire_alice", "--data-dir", str(data),
                  *_state(intent_dir, runs_dir), input="n\n")
    assert result.stderr.count("Commit them so") == 1
    assert result.exit_code != 0


def test_sim_all_asks_once_and_makes_one_commit(
    data: Path, repo: git.Repo, intent_dir: Path, runs_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    _edit(data, "scenarios/retire_alice/README.md")
    _edit(data, "scenarios/base/README.md")
    result = _sim("all", "--data-dir", str(data), *_state(intent_dir, runs_dir), input="\n")
    assert result.exit_code == 0, result.output
    assert result.stderr.count("Commit them so") == 1
    assert _log(repo) == ["Edit base, retire_alice", "init"]
    assert "all 2 scenario(s) succeeded" in result.stdout


@pytest.mark.parametrize(("edited", "held"), [
    ("scenarios/retire_alice/README.md", ["retire_alice"]),  # base doesn't read it
    ("scenarios/base/README.md", ["base", "retire_alice"]),  # retire_alice inherits base
])
def test_the_commit_holds_off_the_builder_for_the_scenarios_it_changes(
    data: Path, intent_dir: Path, runs_dir: Path, monkeypatch: pytest.MonkeyPatch,
    edited: str, held: list[str],
) -> None:
    from analysis.sim import builder

    calls: list[list[str]] = []
    monkeypatch.setattr(builder, "hold", lambda _data, names: calls.append(sorted(names)))
    _edit(data, edited)
    result = _sim("all", "--commit", "--data-dir", str(data), *_state(intent_dir, runs_dir))
    assert result.exit_code == 0, result.output
    assert calls == [held]


def test_sim_commit(data: Path, repo: git.Repo) -> None:
    result = _sim("commit", "retire_alice", "--data-dir", str(data))
    assert result.exit_code == 0, result.output
    assert "nothing to commit" in result.stdout

    _edit(data, "scenarios/retire_alice/README.md")
    _edit(data, "scenarios/base/README.md")
    result = _sim("commit", "base", "--data-dir", str(data), "-m", "Tidy notes")
    assert result.exit_code == 0, result.output
    assert _log(repo)[0] == "Tidy notes"
    assert "Committed by `sim commit base`" in repo.head.commit.message
    assert _status(repo) == "?? scenarios/retire_alice/README.md"  # not in base's scope

    result = _sim("commit", "--all", "--data-dir", str(data))
    assert result.exit_code == 0, result.output
    assert _log(repo)[0] == "Edit retire_alice: README.md"
    assert _status(repo) == ""


def test_sim_commit_refuses_a_scenario_and_all(data: Path) -> None:
    result = _sim("commit", "base", "--all", "--data-dir", str(data))
    assert result.exit_code == 2
    assert "not both" in result.output


def test_a_background_build_skips_a_run_made_while_it_waited(
    data: Path, intent_dir: Path, runs_dir: Path, code_repo: git.Repo,
    stamp_runs: Callable[[Path], None], monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = ("load", "retire_alice", "--data-dir", str(data), *_state(intent_dir, runs_dir))
    assert _sim(*args).exit_code == 0
    stamp_runs(runs_dir)
    monkeypatch.setenv(BUILDER_CHILD_ENV, "1")
    result = _sim(*args)
    assert result.exit_code == 0, result.output
    assert "nothing to build" in result.stdout


def test_sim_init_gives_a_new_repo_an_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from analysis.sim import data_repo

    empty = tmp_path / "empty-gitconfig"
    empty.write_text("[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("EMAIL", raising=False)
    root = tmp_path / "data"
    person = {"id": "alice", "display": "Alice Example", "birthdate": "1960-05-01", "sex": "f"}
    assert data_repo.create_data_repo(root, person, "MFJ") is None
    out = subprocess.run(["git", "-C", str(root), "log", "--format=%an <%ae>"],
                         capture_output=True, text=True, check=True).stdout
    assert out.strip() == "Alice Example <alice@analysis.local>"
