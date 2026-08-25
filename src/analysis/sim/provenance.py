"""Git-based provenance stamps per ADR-004 §1-2.

Computes ``code_version``, ``data_version``, ``data_dirty``, and
``scenario_tree_id`` via GitPython. ``scenario_tree_id`` is a real git tree
SHA, computed via a sidecar index (the user's index is never touched), over
exactly the paths the loader read for a scenario: ``scenarios/<name>/``,
``persons.csv``, ``accounts.csv``, ``account_models/<model>.csv``, ``aux/``.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

from git import InvalidGitRepositoryError, NoSuchPathError, Repo


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
        repo_path=repo.working_tree_dir,
    )


def data_stamps(
    data_dir: Path,
    scenario_name: str,
    account_model_name: str,
) -> DataStamps:
    data_dir = data_dir.resolve()
    repo = _open_repo(data_dir)
    if repo is None:
        return DataStamps(version="unversioned", dirty=True, tree_id="", dirty_paths=())
    scoped_abs = [
        str(data_dir / p)
        for p in _scoped_paths(data_dir, scenario_name, account_model_name)
    ]
    status = repo.git.status("--porcelain", "--", *scoped_abs)
    dirty_paths = tuple(line[3:] for line in status.splitlines()) if status.strip() else ()
    tree_id = _compute_tree_id(repo, scoped_abs)
    return DataStamps(
        version=repo.head.commit.hexsha,
        dirty=bool(dirty_paths),
        tree_id=tree_id,
        dirty_paths=dirty_paths,
    )


def _scoped_paths(data_dir: Path, scenario_name: str, model_name: str) -> list[str]:
    candidates = [
        f"scenarios/{scenario_name}",
        "persons.csv",
        "accounts.csv",
        "aux",
    ]
    if model_name:
        candidates.append(f"account_models/{model_name}.csv")
    return [p for p in candidates if (data_dir / p).exists()]


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
