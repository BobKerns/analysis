"""`sim init`: a new data repo from the code repository's templates (#289)."""

from __future__ import annotations

import csv
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.sim import builder, data_repo


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A git that commits without the machine's settings (signing, identity)."""
    config = tmp_path / "gitconfig"
    config.write_text("[commit]\n\tgpgsign = false\n[init]\n\tdefaultBranch = main\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "t")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "t@t")


@pytest.fixture(autouse=True)
def code_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A stand-in code worktree, so no test links the real data/shared."""
    code = tmp_path / "code"
    code.mkdir()
    monkeypatch.setattr(data_repo, "CODE_REPO_DIR", code)
    return code


@pytest.fixture(autouse=True)
def installs(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """The data dirs the builder was installed for, in place of installing."""
    done: list[Path] = []

    def install(data_dir: Path, branch: str | None = None) -> list[str]:  # noqa: ARG001
        done.append(data_dir)
        return [f"installed hooks for {data_dir}"]
    monkeypatch.setattr(builder, "install", install)
    monkeypatch.setattr(builder, "notify", lambda data_dir: "notified")  # noqa: ARG005
    return done


def _init(*args: str, input: str | None = None) -> Result:
    return CliRunner().invoke(main, ["sim", "init", *args], input=input)


_ALICE = ("--id", "alice", "--display", "Alice Example", "--birthdate", "1960-05-01",
          "--sex", "f", "--filing-status", "MFJ")


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def test_init_asks_for_what_its_options_leave_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = _init(input="repo\nalice\nAlice Example\n1960-05-01\nf\nSingle\n")

    assert result.exit_code == 0, result.output
    sim = tmp_path / "repo" / "sim"
    assert _rows(sim / "persons.csv") == [{
        "id": "alice", "display": "Alice Example", "birthdate": "1960-05-01", "sex": "f",
        "role": "primary", "meta_json": "{}",
    }]
    assert _rows(sim / "aux" / "filing_status.csv") == [
        {"effective_date": "2026-01", "filing_status": "Single"}]
    assert "checking base: ok" in result.output


def test_init_copies_what_the_household_edits_and_reads_the_rest_from_templates(
    tmp_path: Path, code_repo: Path,
) -> None:
    result = _init(str(tmp_path / "repo"), *_ALICE)

    assert result.exit_code == 0, result.output
    sim = tmp_path / "repo" / "sim"
    assert sorted(p.name for p in (sim / "scenarios").iterdir()) == ["base"]
    assert [p.name for p in (sim / "aux").iterdir()] == ["filing_status.csv"]
    assert (sim / "activate-analysis").is_file()
    assert (sim / ".retention.yaml").is_file()
    assert not (sim / "README.md").exists()  # describes the code repo's templates/
    assert _rows(sim / "scenarios" / "base" / "scenario.csv")[0]["created_by"] == "alice"
    # activate-analysis needn't ask for the code repo or the user.
    assert (sim / ".analysis").read_text() == f"{code_repo}\n"
    assert (sim / ".analysis_user").read_text() == "alice\n"
    assert (sim / ".current_scenario").read_text() == "base\n"


def test_init_commits_everything_but_the_per_machine_files(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    result = _init(str(root), *_ALICE)

    assert result.exit_code == 0, result.output

    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                              check=True).stdout
    assert len(git("rev-list", "--all").splitlines()) == 1
    assert git("status", "--porcelain") == ""
    tracked = set(git("ls-files").split())
    assert {"sim/persons.csv", "sim/aux/filing_status.csv", "sim/scenarios/base/scenario.csv",
            ".gitignore", ".cspell/data-words.txt"} <= tracked
    assert not tracked & {"sim/.analysis", "sim/.analysis_user", "sim/.current_scenario",
                          "sim/.retention.yaml"}


def test_init_without_commit_leaves_git_alone(tmp_path: Path) -> None:
    result = _init(str(tmp_path / "repo"), *_ALICE, "--no-commit")

    assert result.exit_code == 0, result.output
    assert not (tmp_path / "repo" / ".git").exists()


def test_init_refuses_a_directory_that_holds_a_data_repo(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    assert _init(str(root), *_ALICE).exit_code == 0
    persons = (root / "sim" / "persons.csv").read_bytes()

    result = _init(str(root), *_ALICE)

    assert result.exit_code != 0
    assert "already holds a data repo" in result.output
    assert (root / "sim" / "persons.csv").read_bytes() == persons


def test_init_refuses_a_sim_folder_with_something_in_it(tmp_path: Path) -> None:
    (tmp_path / "repo" / "sim").mkdir(parents=True)
    (tmp_path / "repo" / "sim" / "notes.txt").write_text("mine\n")

    result = _init(str(tmp_path / "repo"), *_ALICE)

    assert result.exit_code != 0
    assert "isn't empty" in result.output


def test_init_refuses_an_id_of_more_than_one_word(tmp_path: Path) -> None:
    result = _init(str(tmp_path / "repo"), *_ALICE[2:], "--id", "alice example")

    assert result.exit_code != 0
    assert "one word" in result.output
    assert not (tmp_path / "repo").exists()


# ---------- background builds ----------

def test_init_links_data_shared_and_installs_the_builder(
    tmp_path: Path, code_repo: Path, installs: list[Path],
) -> None:
    root = tmp_path / "repo"
    result = _init(str(root), *_ALICE)

    assert result.exit_code == 0, result.output
    assert (code_repo / "data" / "shared").resolve() == root.resolve()
    assert installs == [root.resolve() / "sim"]
    assert "notified" in result.output


def test_init_leaves_an_existing_data_shared_alone(
    tmp_path: Path, code_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (code_repo / "data" / "shared").mkdir(parents=True)

    def install(data_dir: Path, branch: str | None = None) -> list[str]:  # noqa: ARG001
        raise ValueError("data/shared doesn't lead to the data repo")
    monkeypatch.setattr(builder, "install", install)

    result = _init(str(tmp_path / "repo"), *_ALICE)

    assert result.exit_code == 0, result.output
    assert not (code_repo / "data" / "shared").is_symlink()
    assert "background builds not turned on" in result.output


@pytest.mark.parametrize("flag", ["--no-builder", "--no-commit"])
def test_init_without_a_builder_touches_neither_repo(
    tmp_path: Path, code_repo: Path, installs: list[Path], flag: str,
) -> None:
    result = _init(str(tmp_path / "repo"), *_ALICE, flag)

    assert result.exit_code == 0, result.output
    assert not (code_repo / "data").exists()
    assert installs == []
