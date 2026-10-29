"""Git-based provenance stamps per ADR-004 §1-2.

Computes ``code_version``, ``data_version``, ``data_dirty``, and
``scenario_tree_id`` via GitPython. ``scenario_tree_id`` is a real git tree
SHA, computed via a sidecar index (the user's index is never touched), over
the data repo paths a scenario's load can read: ``scenarios/<name>/`` plus the
folder of every scenario it inherits from via ``parent_scenario`` (mix-ins
included), ``persons.csv``, ``securities.csv``, ``allocations.csv``, and the
whole shared ``aux/``, ``prices/``, and ``itemizations/`` folders (each also
resolved per scenario under ``scenarios/<name>/``, ADR-003 §4.1d; covered
automatically since that whole folder is already in scope). ``series/`` has no
shared root tier at all -- only the per-scenario ``scenarios/<name>/series/``
folders, already covered the same way.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

from git import InvalidGitRepositoryError, NoSuchPathError, Repo

from analysis.sim.lineage import load_order


class DataDirtyRefused(RuntimeError):
    def __init__(self, dirty_paths: list[str]) -> None:
        super().__init__(
            "data repo dirty in scoped paths:\n  "
            + "\n  ".join(dirty_paths)
            + "\nCommit/stash the changes, or pass allow_dirty_data=True"
            " (CLI: --allow-dirty-data)."
        )
        self.dirty_paths = dirty_paths


@dataclass(frozen=True)
class CodeStamps:
    version: str
    dirty: bool
    repo_path: str | None


@dataclass(frozen=True)
class DataStamps:
    version: str
    dirty: bool
    tree_id: str
    dirty_paths: tuple[str, ...]


EMPTY_DATA_STAMPS = DataStamps(version="", dirty=False, tree_id="", dirty_paths=())
EMPTY_CODE_STAMPS = CodeStamps(version="", dirty=False, repo_path=None)


def code_stamps() -> CodeStamps:
    import analysis

    repo = _open_repo(Path(analysis.__file__).parent)
    if repo is None:
        try:
            v = _pkg_version("analysis")
        except PackageNotFoundError:
            v = "unknown"
        return CodeStamps(version=v, dirty=False, repo_path=None)
    return CodeStamps(
        version=repo.head.commit.hexsha,
        dirty=repo.is_dirty(untracked_files=True),
        repo_path=None if repo.working_tree_dir is None else str(repo.working_tree_dir),
    )


def data_stamps(data_dir: Path, scenario_name: str) -> DataStamps:
    data_dir = data_dir.resolve()
    repo = _open_repo(data_dir)
    if repo is None:
        return DataStamps(version="unversioned", dirty=True, tree_id="", dirty_paths=())
    scoped_abs = [str(data_dir / p) for p in _scoped_paths(data_dir, scenario_name)]
    if scoped_abs:  # an exclude alone would mean everything else
        scoped_abs.extend(_NOT_DATA)
    status = repo.git.status("--porcelain", "--", *scoped_abs)
    dirty_paths = tuple(line[3:] for line in status.splitlines()) if status.strip() else ()
    tree_id = _compute_tree_id(repo, scoped_abs)
    return DataStamps(
        version=repo.head.commit.hexsha,
        dirty=bool(dirty_paths),
        tree_id=tree_id,
        dirty_paths=dirty_paths,
    )


# Files in a scenario folder that no run reads, so editing one must not
# change its tree (or its children's), nor make them dirty: `.disable`
# (loader.DISABLE_FILE, #300) says whether to build a scenario, not what it
# computes, and `guard_rails.csv` (#305) is read only by reports.
_NOT_DATA = (
    ":(exclude,glob)**/scenarios/*/.disable",
    ":(exclude,glob)**/scenarios/*/guard_rails.csv",
)


def _scoped_paths(data_dir: Path, scenario_name: str) -> list[str]:
    candidates = [
        *(f"scenarios/{n}" for n in _scenario_lineage(data_dir, scenario_name)),
        "persons.csv",
        "securities.csv",
        "allocations.csv",
        "strategies",  # strategy code (#326): an edit can change any run that uses it
        "aux",
        "prices",
        "itemizations",
    ]
    return [p for p in candidates if (data_dir / p).exists()]


def _scenario_lineage(data_dir: Path, scenario_name: str) -> list[str]:
    """``scenario_name`` and every scenario it inherits from, main parent's
    line and mix-ins alike.

    A derived scenario's load reads all of their folders too (ADR-003 §4.5),
    so the provenance scope has to cover them or an edit to a parent or a
    mix-in would leave a child's ``scenario_tree_id`` unchanged and its
    ``data_dirty`` false. Tolerant of a malformed scenario.csv: provenance
    must not be the thing that reports one -- the loader validates it
    properly and will raise.
    """
    return load_order(data_dir / "scenarios", scenario_name)


def _compute_tree_id(repo: Repo, scoped_abs: list[str]) -> str:
    with tempfile.TemporaryDirectory() as td:
        index_path = str(Path(td) / "index")
        with repo.git.custom_environment(GIT_INDEX_FILE=index_path):
            repo.git.add("--", *scoped_abs)
            return repo.git.write_tree()


@dataclass(frozen=True)
class RepoHead:
    """Where a working tree currently sits: commit, branch, dirtiness."""

    commit: str | None
    branch: str | None
    dirty: bool


NO_REPO_HEAD = RepoHead(commit=None, branch=None, dirty=False)


def repo_head(path: Path) -> RepoHead:
    """Describe the git checkout containing ``path``.

    Returns ``NO_REPO_HEAD`` when the path is not in a git repo; ``branch`` is
    ``None`` for a detached HEAD, and ``commit`` is ``None`` for a repo with no
    commits yet.
    """
    repo = _open_repo(Path(path))
    if repo is None:
        return NO_REPO_HEAD
    try:
        commit = repo.head.commit.hexsha
    except ValueError:
        commit = None
    branch = None if repo.head.is_detached else repo.active_branch.name
    return RepoHead(commit=commit, branch=branch, dirty=repo.is_dirty())


def _open_repo(start: Path) -> Repo | None:
    try:
        return Repo(start, search_parent_directories=True)
    except (InvalidGitRepositoryError, NoSuchPathError):
        return None
