"""`sim init`: a new data repo from the code repository's templates (#289)."""

from __future__ import annotations

import csv
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from zabob.retirement import checkout
from zabob.retirement.cli import main
from zabob.retirement.sim import builder, data_repo


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
    # Not in the checkout the tests run from: `sim init` would take that as
    # the one to set up, and run its own sim.
    monkeypatch.chdir(tmp_path)
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


_OS_JUNK = (".DS_Store", "scenarios/.DS_Store", "scenarios/base/._scenario.csv",
            "Thumbs.db", "aux/desktop.ini")


@pytest.fixture
def junk_in_templates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[str, ...]:
    """templates/ copied with Finder's and Explorer's files added, as a
    browsed checkout has them; returns their paths beneath it."""
    templates = tmp_path / "templates"
    shutil.copytree(data_repo.TEMPLATES_DIR, templates)
    for name in _OS_JUNK:
        (templates / name).write_text("junk")
    monkeypatch.setattr(data_repo, "TEMPLATES_DIR", templates)
    return _OS_JUNK


def test_init_leaves_os_junk_files_behind(tmp_path: Path, junk_in_templates: tuple[str, ...]) -> None:
    """They aren't copied into the new data repo (#401)."""
    result = _init(str(tmp_path / "repo"), *_ALICE)

    assert result.exit_code == 0, result.output
    sim = tmp_path / "repo" / "sim"
    assert [name for name in junk_in_templates if (sim / name).exists()] == []
    assert sorted(p.name for p in (sim / "scenarios").iterdir()) == ["base"]


def test_init_asks_for_what_its_options_leave_out(code_repo: Path) -> None:
    # No directory is asked for: the data repo is the checkout's data/shared.
    result = _init("--no-commit", input="alice\nAlice Example\n1960-05-01\nf\nSingle\n")

    assert result.exit_code == 0, result.output
    sim = code_repo / "data" / "shared" / "sim"
    assert _rows(sim / "persons.csv") == [{
        "id": "alice", "display": "Alice Example", "birthdate": "1960-05-01", "sex": "f",
        "role": "primary", "meta_json": "{}", "kind": "",
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


def test_init_without_a_builder_touches_neither_repo(
    tmp_path: Path, code_repo: Path, installs: list[Path],
) -> None:
    result = _init(str(tmp_path / "repo"), *_ALICE, "--no-builder")

    assert result.exit_code == 0, result.output
    assert not (code_repo / "data").exists()
    assert installs == []


def test_init_without_a_builder_and_no_dir_still_makes_the_checkout_s_data_shared(
    code_repo: Path, installs: list[Path],
) -> None:
    # Code and data are one to one: that is where it was asked for.
    usual = _init(*_ALICE, "--no-builder")
    assert usual.exit_code == 0, usual.output
    assert (code_repo / "data" / "shared" / "sim" / "persons.csv").is_file() and installs == []


def test_init_without_commit_links_data_shared_and_installs_no_builder(
    tmp_path: Path, code_repo: Path, installs: list[Path],
) -> None:
    root = tmp_path / "repo"
    result = _init(str(root), *_ALICE, "--no-commit")

    assert result.exit_code == 0, result.output
    # The checkout finds the data repo; the builder needs a git repository.
    assert (code_repo / "data" / "shared").resolve() == root.resolve() and not (root / ".git").exists()
    assert "linked code/data/shared to repo\n" in result.output
    assert "source code/data/shared/sim/activate-analysis" in result.output
    assert installs == []


@pytest.mark.parametrize("linked", [False, True])
def test_init_with_no_dir_refuses_a_data_shared_that_is_there(
    tmp_path: Path, code_repo: Path, linked: bool,
) -> None:
    shared = code_repo / "data" / "shared"
    shared.parent.mkdir()
    if linked:
        (tmp_path / "elsewhere").mkdir()
        shared.symlink_to(tmp_path / "elsewhere")
    else:
        shared.mkdir()

    result = _init(*_ALICE)

    assert result.exit_code != 0
    assert "code/data/shared is already there" in result.output
    assert ("leading to elsewhere" in result.output) == linked
    assert "source code/data/shared/sim/activate-analysis" in result.output
    assert not (shared / "sim").exists()


def test_init_names_the_link_script_through_the_checkout(tmp_path: Path, code_repo: Path) -> None:
    (code_repo / "data" / "shared").mkdir(parents=True)

    result = _init("repo", *_ALICE, "--no-commit")

    assert result.exit_code == 0, result.output
    assert f"remove it and run: code/scripts/link-data.sh {(tmp_path / 'repo').resolve()}" \
        in " ".join(result.output.split())


# ---------- run from another checkout ----------

@pytest.fixture
def other_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A second code checkout, which the test is in a subfolder of, whose
    own ``sim`` writes its arguments and environment to ``argv`` beside it
    and ends with status 7."""
    other = tmp_path / "other"
    for marker in checkout.MARKERS:
        (other / marker).parent.mkdir(parents=True, exist_ok=True)
        (other / marker).touch()
    (other / "docs").mkdir()
    monkeypatch.chdir(other / "docs")
    return other


_STUB_SIM = (
    '#!/bin/sh\n'
    'here="$(cd "$(dirname "$0")/../.." && pwd)"\n'
    '{ for arg in "$@"; do printf "[%s]\\n" "$arg"; done; echo "again=$ANALYSIS_INIT_AGAIN venv=${VIRTUAL_ENV-}"; }'
    ' > "$here/argv"\n'
    'exit 7\n')


def _stub_sim(other: Path) -> None:
    sim = other / ".venv" / "bin" / "sim"
    sim.parent.mkdir(parents=True)
    sim.write_text(_STUB_SIM)
    sim.chmod(0o755)


def test_init_from_another_checkout_s_folder_runs_that_checkout_s_sim(
    tmp_path: Path, other_checkout: Path, code_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_sim(other_checkout)
    monkeypatch.setenv("VIRTUAL_ENV", str(code_repo / ".venv"))

    result = _init("repo", "--id", "alice", "--display", "Alice Example", "--birthdate", "1960-05-01",
                   "--sex", "", "--filing-status", "MFJ", "--no-commit", "--no-builder")

    # Its result is this one's, and every option reaches it as given, an empty one too.
    assert result.exit_code == 7, result.output
    assert "running that checkout's sim init" in result.output
    assert (other_checkout / "argv").read_text().splitlines() == [
        "[init]", "[repo]", "[--id]", "[alice]", "[--display]", "[Alice Example]", "[--birthdate]",
        "[1960-05-01]", "[--sex]", "[]", "[--filing-status]", "[MFJ]", "[--no-commit]", "[--no-builder]",
        "again=1 venv="]
    assert not (tmp_path / "other" / "docs" / "repo").exists()

    # With nothing given, nothing is passed: that sim asks, and makes its own data/shared.
    assert _init().exit_code == 7
    assert (other_checkout / "argv").read_text().splitlines() == ["[init]", "again=1 venv="]


def test_init_started_by_another_checkout_s_sim_starts_no_third(
    other_checkout: Path, code_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_sim(other_checkout)
    monkeypatch.setenv("ANALYSIS_INIT_AGAIN", "1")

    result = _init(*_ALICE, "--no-builder")

    assert result.exit_code == 0, result.output
    assert not (other_checkout / "argv").exists()
    assert (code_repo / "data" / "shared" / "sim" / "persons.csv").is_file()


def test_init_makes_the_other_checkout_s_venv_or_says_what_is_missing(
    tmp_path: Path, other_checkout: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    monkeypatch.setenv("PATH", str(tools))
    none = _init(*_ALICE)
    assert none.exit_code != 0 and "has no .venv, and uv isn't installed" in none.output

    # A uv that syncs without leaving a sim: said, not a traceback.
    uv = tools / "uv"
    uv.write_text('#!/bin/sh\necho "$@" > uv-args\n[ -z "$MAKE_SIM" ] || { mkdir -p .venv/bin; '
                  'printf "#!/bin/sh\\nexit 3\\n" > .venv/bin/sim; chmod +x .venv/bin/sim; }\n')
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}:/usr/bin:/bin")
    empty = _init(*_ALICE)
    assert empty.exit_code != 0 and "has no .venv/bin/sim, even after uv sync" in empty.output
    assert (other_checkout / "uv-args").read_text() == "sync --group dev\n"     # run in that checkout

    monkeypatch.setenv("MAKE_SIM", "1")
    made = _init(*_ALICE)
    assert made.exit_code == 3 and f"making {other_checkout / '.venv'} (uv sync)" in made.output

    # One that can't be run is said too.
    (other_checkout / ".venv" / "bin" / "sim").chmod(0o644)
    broken = _init(*_ALICE)
    assert broken.exit_code == 1 and "can't be run" in broken.output and "scripts/setup.sh" in broken.output


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
    # The one step nothing works without stands apart from the rest.
    assert "\n\nNext, activate the data repo in this shell" in here.output
    assert "\n\n    source retirement/sim/activate-analysis\n\n" in here.output
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
    from zabob.retirement.sim.accounts import load_accounts_view
    from zabob.retirement.sim.connection import open_scenario

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


# ---------- activate-analysis ----------

def _checkout(path: Path) -> Path:
    """A folder activate-analysis takes for a code checkout, whose venv
    says which it is when activated."""
    for marker in checkout.MARKERS:
        (path / marker).parent.mkdir(parents=True, exist_ok=True)
        (path / marker).touch()
    (path / ".venv" / "bin").mkdir(parents=True)
    (path / ".venv" / "bin" / "activate").write_text(f"ACTIVATED={path.name}\n")
    (path / "templates").mkdir()
    shutil.copy(data_repo.TEMPLATES_DIR / "activate-analysis", path / "templates")
    return path


def test_activate_takes_the_code_of_the_checkout_it_is_sourced_through(tmp_path: Path) -> None:
    data = tmp_path / "data"
    (data / "sim").mkdir(parents=True)
    shutil.copy(data_repo.TEMPLATES_DIR / "activate-analysis", data / "sim")
    (data / "sim" / "persons.csv").write_text("id,display\nalice,Alice Example\n")
    (data / "sim" / ".analysis_user").write_text("alice\n")
    main_checkout, worktree = _checkout(tmp_path / "main"), _checkout(tmp_path / "worktree")
    for each in (main_checkout, worktree):
        (each / "data").mkdir()
        (each / "data" / "shared").symlink_to(data)
    # The data repo was made by, and last used with, the main checkout.
    (data / "sim" / ".analysis").write_text(f"{main_checkout}\n")

    def activate(script: str, cwd: Path, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-c", f'source {script} && echo "$ACTIVATED $ANALYSIS_REPO_DIR"'], cwd=cwd,
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), **env}, capture_output=True, text=True, check=False)

    # From the worktree, though the data repo remembers the main checkout and the shell still names it.
    there = activate("data/shared/sim/activate-analysis", worktree, ANALYSIS_REPO_DIR=str(main_checkout))
    assert there.stdout == f"worktree {worktree}\n", there.stderr
    assert f"using the code in {worktree}; the data repo stays paired with {main_checkout}" in there.stderr
    # The pair is the builder's: its hooks run the sim of the checkout .analysis names.
    assert (data / "sim" / ".analysis").read_text() == f"{main_checkout}\n"
    again = activate(str(worktree / "data" / "shared" / "sim" / "activate-analysis"), tmp_path)
    assert again.stdout == f"worktree {worktree}\n" and "differs" not in again.stderr
    # The paired checkout itself says nothing.
    paired = activate("data/shared/sim/activate-analysis", main_checkout)
    assert paired.stdout == f"main {main_checkout}\n" and "using the code" not in paired.stderr

    # A shell that follows links on cd still sees the checkout in the path as typed.
    chased = subprocess.run(
        ["bash", "-c", 'set -P; source data/shared/sim/activate-analysis && echo "$ACTIVATED $ANALYSIS_REPO_DIR"'],
        cwd=worktree, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "ANALYSIS_REPO_DIR": str(main_checkout)},
        capture_output=True, text=True, check=False)
    assert chased.stdout == f"worktree {worktree}\n", chased.stderr

    # A pair that names no checkout is written by whichever activates.
    (data / "sim" / ".analysis").write_text(f"{tmp_path / 'gone'}\n")
    adopted = activate("data/shared/sim/activate-analysis", worktree)
    assert f"using the code in {worktree} (it was {tmp_path / 'gone'})" in adopted.stderr
    assert (data / "sim" / ".analysis").read_text() == f"{worktree}\n"

    # Sourced by its own path, the data repo has no checkout around it: the shell's, else the remembered one.
    named = activate(str(data / "sim" / "activate-analysis"), tmp_path, ANALYSIS_REPO_DIR=str(main_checkout))
    assert named.stdout == f"main {main_checkout}\n", named.stderr
    assert f"using the code in {main_checkout} (it was {worktree})" in named.stderr
    remembered = activate(str(data / "sim" / "activate-analysis"), tmp_path)
    assert remembered.stdout == f"main {main_checkout}\n", remembered.stderr
    (data / "sim" / ".analysis").write_text(f"{worktree}\n")

    # A copy the code has since changed is said, with how to refresh it.
    (worktree / "templates" / "activate-analysis").write_text("# newer\n")
    stale = activate("data/shared/sim/activate-analysis", worktree)
    assert f"this copy differs from {worktree}/templates/activate-analysis." in stale.stderr
    assert "If that checkout has the newest code of those using this data repo" in stale.stderr
    assert f"cp {worktree}/templates/activate-analysis {worktree}/data/shared/sim/" in stale.stderr

    # One checkout spelled two ways (under a linked folder) is the same code: nothing is said.
    (tmp_path / "linked").symlink_to(tmp_path)
    (data / "sim" / ".analysis").write_text(f"{tmp_path / 'linked' / 'worktree'}\n")
    shutil.copy(data_repo.TEMPLATES_DIR / "activate-analysis", worktree / "templates")
    same = activate("data/shared/sim/activate-analysis", worktree)
    assert same.stdout == f"worktree {worktree}\n" and "using the code" not in same.stderr


def test_the_script_and_the_program_agree_on_what_a_checkout_is() -> None:
    script = (data_repo.TEMPLATES_DIR / "activate-analysis").read_text(encoding="utf-8")
    body = script[script.index("_analysis_validate_repo_dir() {"):script.index("_analysis_enable_completion() {")]
    assert re.findall(r'\[ -f "\$1/([^"]+)" \]', body) == list(checkout.MARKERS)
    assert checkout.checkout_of(data_repo.TEMPLATES_DIR) == data_repo.TEMPLATES_DIR.parent
    assert "\t" not in script
    prototype = data_repo.TEMPLATES_DIR.parent / "prototype_data" / "activate-analysis"
    assert script == prototype.read_text(encoding="utf-8")
