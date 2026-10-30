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
    monkeypatch.delenv("ANALYSIS_NO_BUILDER", raising=False)
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


def test_init_into_an_existing_repo_keeps_its_gitignore(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".gitignore").write_text("*.log")

    result = _init(str(root), *_ALICE)

    assert result.exit_code == 0, result.output
    assert (root / ".gitignore").read_text() == "*.log\n/state/\n"


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


def test_init_with_no_builder_in_the_environment_touches_neither_repo(
    tmp_path: Path, code_repo: Path, installs: list[Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANALYSIS_NO_BUILDER", "1")
    result = _init(str(tmp_path / "repo"), *_ALICE)

    assert result.exit_code == 0, result.output
    assert not (code_repo / "data").exists()
    assert installs == []


def test_init_shows_paths_as_typed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Paths under the working directory print relative to it, and ones
    elsewhere in the home folder as ~/..., not as full absolute paths (#374)."""
    home = tmp_path / "home"
    (home / "work").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(home / "work")

    here = _init("retirement", *_ALICE, "--no-builder")
    there = _init(str(home / "other"), *_ALICE, "--no-builder")

    assert here.exit_code == 0 and there.exit_code == 0, here.output + there.output
    assert "created a data repo in retirement; its data dir is retirement/sim" in here.output
    assert "Next: source retirement/sim/activate-analysis," in here.output
    assert "created a data repo in ~/other; its data dir is ~/other/sim" in there.output
    assert str(tmp_path) not in here.output + there.output


def test_a_new_data_repo_takes_a_joint_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#368: `sim init` writes only real people to persons.csv, yet every
    account defaults to the household, `joint`; the loader supplies it. The
    messages name the files as typed, and dollars without column padding."""
    monkeypatch.chdir(tmp_path)
    assert _init("retirement", *_ALICE, "--no-builder").exit_code == 0
    monkeypatch.setenv("ANALYSIS_DATA_DIR", str(tmp_path / "retirement" / "sim"))
    monkeypatch.setattr(builder, "notify", lambda data_dir, **_: "notified")  # noqa: ARG005

    added = CliRunner().invoke(main, [
        "sim", "account", "add", "Asset_Checking", "asset", "cash", "--owner", "joint",
        "--opening-balance", "15000", "--scenario", "base",
    ])

    assert added.exit_code == 0, added.output
    assert "appended account 'Asset_Checking' to retirement/sim/scenarios/base/accounts.csv" in added.output
    assert "appended opening balance $15,000.00 to" in added.output
    assert all(row["id"] != "joint" for row in _rows(tmp_path / "retirement" / "sim" / "persons.csv"))


def test_account_list_shows_each_accounts_own_growth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#368: a growth rule's to_account names a chain, whose ids needn't match
    the accounts'; each account shows its own lots' series (several when its
    lots differ), and an income account none."""
    from analysis.sim.accounts import load_accounts_view
    from analysis.sim.connection import open_scenario

    monkeypatch.chdir(tmp_path)
    assert _init("retirement", *_ALICE, "--no-builder").exit_code == 0
    data = tmp_path / "retirement" / "sim"
    monkeypatch.setenv("ANALYSIS_DATA_DIR", str(data))
    monkeypatch.setattr(builder, "notify", lambda data_dir, **_: "notified")  # noqa: ARG005
    for args in (
        ["account", "add", "Asset_Checking", "asset", "cash", "--owner", "joint", "--opening-balance", "1000"],
        ["account", "add", "Asset_Savings", "asset", "cash", "--owner", "joint", "--series", "cash",
         "--tax-character", "ordinary", "--opening-balance", "1000"],
        ["account", "add", "Asset_IRA", "asset", "retirement", "--owner", "alice",
         "--lot-subkind", "traditional_ira", "--series", "stock", "--opening-balance", "1000"],
        ["account", "add", "Income_Salary", "income", "salary", "--owner", "alice"],
        ["account", "add", "Asset_Brokerage", "asset", "brokerage_taxable", "--owner", "joint",
         "--series", "stock_us", "--tax-character", "gains", "--opening-balance", "1000"],
        ["account", "lot", "add", "Asset_Brokerage", "bonds", "--series", "bond", "--opening-balance", "1000"],
    ):
        result = CliRunner().invoke(main, ["sim", *args, "--scenario", "base"])
        assert result.exit_code == 0, result.output

    with open_scenario(data, "base") as sc:
        rows = {r.name: r for r in load_accounts_view(sc)}
    assert rows["Asset_Savings"].rate_series == "cash"
    assert rows["Asset_IRA"].rate_series == "stock"
    assert rows["Asset_Brokerage"].rate_series == "bond, stock_us"
    assert rows["Asset_Brokerage"].annual_rate_micros is None
    assert rows["Asset_Checking"].rate_series is None
    assert rows["Income_Salary"].rate_series is None
