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

import csv
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

from git import InvalidGitRepositoryError, NoSuchPathError, Repo

from analysis.sim.lineage import is_pool, load_order, pool_components


class DataDirtyRefused(RuntimeError):
    def __init__(self, dirty_paths: list[str]) -> None:
        super().__init__(
            "data repo dirty in scoped paths:\n  "
            + "\n  ".join(dirty_paths)
            + "\nCommit them (CLI: `sim commit`, or --commit), or pass"
            " allow_dirty_data=True (CLI: --allow-dirty-data)."
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
        scoped_abs.extend(_not_data(data_dir, [scenario_name]))
    status = repo.git.status("--porcelain", "--", *scoped_abs)
    dirty_paths = tuple(line[3:] for line in status.splitlines()) if status.strip() else ()
    tree_id = _compute_tree_id(repo, scoped_abs)
    return DataStamps(
        version=repo.head.commit.hexsha,
        dirty=bool(dirty_paths),
        tree_id=tree_id,
        dirty_paths=dirty_paths,
    )


def scoped_changes(data_dir: Path, scenario_names: Sequence[str]) -> list[tuple[str, str]]:
    """The uncommitted changes that make any of ``scenario_names`` dirty,
    as ``(status, path)`` pairs, ``path`` relative to the repository's top
    and ``status`` git's two porcelain letters. Untracked files are listed
    one by one, so a new scenario folder names each file. Empty when the
    data dir isn't in a git repository (nothing could be committed)."""
    data_dir = data_dir.resolve()
    repo = _open_repo(data_dir)
    if repo is None:
        return []
    scoped = sorted({str(data_dir / p) for name in scenario_names
                     for p in _scoped_paths(data_dir, name)})
    if not scoped:
        return []
    out = repo.git.status("--porcelain", "-z", "--untracked-files=all", "--",
                          *scoped, *_not_data(data_dir, scenario_names))
    changes: list[tuple[str, str]] = []
    fields = iter(out.split("\0"))
    for field in fields:
        if not field:
            continue
        status, path = field[:2], field[3:]
        changes.append((status, path))
        if "R" in status or "C" in status:
            # -z puts a rename's source in the next field; it's gone from
            # the tree, so committing it records the removal.
            changes.append(("D ", next(fields, "")))
    return changes


def changed_scenarios(
    data_dir: Path, scenario_names: Sequence[str], changes: Sequence[tuple[str, str]],
) -> list[str]:
    """Those of ``scenario_names`` whose scope holds one of ``changes``
    (:func:`scoped_changes`'s pairs): the ones committing them changes."""
    data_dir = data_dir.resolve()
    repo = _open_repo(data_dir)
    if repo is None or repo.working_tree_dir is None:
        return []
    top = Path(repo.working_tree_dir).resolve()
    changed = [top / path for _status, path in changes]
    out = []
    for name in scenario_names:
        scope = [data_dir / p for p in _scoped_paths(data_dir, name)]
        if any(path == s or s in path.parents for path in changed for s in scope):
            out.append(name)
    return out


# Files in a scenario folder that no run reads, so editing one must not
# change its tree (or its children's), nor make them dirty: `.disable`
# (loader.DISABLE_FILE, #300) says whether to build a scenario, not what it
# computes, and `guard_rails.csv` (#305) is read only by reports -- unless a
# strategy's trigger names a rail (#441), when the run reads them too.
_NOT_DATA = (
    ":(exclude,glob)**/scenarios/*/.disable",
)


def _not_data(data_dir: Path, scenario_names: Sequence[str]) -> list[str]:
    """The excludes for ``scenario_names``' scope: ``_NOT_DATA``, and each
    lineage folder's ``guard_rails.csv`` unless some lineage holding that
    folder has a strategy with a ``trigger``, whose rails the run evaluates.
    A folder two lineages share keeps its rails in scope if either reads
    them, which can rebuild the other one needlessly but never misses an
    edit."""
    folders: set[str] = set()
    read: set[str] = set()
    for name in scenario_names:
        lineage = _scenario_lineage(data_dir, name)
        folders.update(lineage)
        if _has_trigger(data_dir, lineage):
            read.update(lineage)
    return [*_NOT_DATA, *(f":(exclude,glob)**/scenarios/{folder}/guard_rails.csv"
                         for folder in sorted(folders - read))]


def _has_trigger(data_dir: Path, lineage: Sequence[str]) -> bool:
    """Whether a strategy file in ``lineage``'s folders has a ``trigger``.
    Tolerant, as the lineage is: the loader reports a malformed file."""
    for folder in lineage:
        for path in sorted((data_dir / "scenarios" / folder / "strategies").glob("*.csv")):
            try:
                with path.open(newline="") as f:
                    if any((row.get("trigger") or "").strip() for row in csv.DictReader(f)):
                        return True
            except (OSError, UnicodeDecodeError, csv.Error):
                continue
    return False


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

    A pool (ADR-009) loads nothing, but its batch is made of its components'
    members, so its scope is its own folder and each component's lineage:
    an edit to a component changes the pool's tree, and a dirty component
    makes the pool dirty.
    """
    scenarios = data_dir / "scenarios"
    if not is_pool(scenarios, scenario_name):
        return load_order(scenarios, scenario_name)
    names = [scenario_name]
    for component in pool_components(scenarios, scenario_name):
        if component == scenario_name or is_pool(scenarios, component):
            continue  # a pool of pools is refused at the pool's check
        names += [n for n in load_order(scenarios, component) if n not in names]
    return names


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
