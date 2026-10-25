"""#298: whether a scenario's latest run could differ if it ran again.

A run is deterministic in its inputs (ADR-004): the code, the Python that
ran it, and the data its load reads. Its run file records all three
(``simulation_run``), so when none has changed since, rerunning it would
write the same numbers again, and ``sim all`` can skip it.

The comparison is by tree, not commit: a commit that changes no file the
results depend on (a rebase, or docs alone) changes nothing. The code
counted is ``src/``, ``templates/`` (a template scenario the data repo
lacks is read from there, ADR-004 §2), ``pyproject.toml`` and ``uv.lock``;
the data is the
scenario's ``scenario_tree_id`` scope (its lineage and the shared files,
``provenance._scoped_paths``). A dirty tree on either side, now or when the
run was made, always reruns: its state isn't in any tree.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from git import BadName, Repo

from analysis.sim.loader import latest_ensemble_batch, latest_run_path
from analysis.sim.provenance import _open_repo, data_stamps

# The code a run's numbers depend on, relative to the code repo's root.
CODE_PATHS = ("src", "templates", "pyproject.toml", "uv.lock")
# Final member statuses: a member still 'running' was interrupted.
_FINISHED = {"succeeded", "failed", "halted", "failed_to_resolve"}


@dataclass(frozen=True)
class CodeTree:
    """The code as it stands: the trees of ``CODE_PATHS`` at HEAD, or None
    when they can't be known (no repo, or uncommitted changes in them)."""

    repo: Repo | None
    trees: tuple[str, ...] | None


def code_tree() -> CodeTree:
    import analysis

    return code_tree_of(_open_repo(Path(analysis.__file__).parent))


def code_tree_of(repo: Repo | None) -> CodeTree:
    if repo is None:
        return CodeTree(None, None)
    if any(repo.is_dirty(untracked_files=True, path=p) for p in CODE_PATHS):
        return CodeTree(repo, None)
    return CodeTree(repo, _trees(repo, "HEAD"))


def _trees(repo: Repo, commit: str) -> tuple[str, ...] | None:
    try:
        tree = repo.commit(commit).tree
    except (BadName, ValueError):
        return None
    out = []
    for path in CODE_PATHS:
        try:
            out.append((tree / path).hexsha)
        except KeyError:
            out.append("")
    return tuple(out)


def _latest(runs_dir: Path, scenario: str) -> tuple[Path, list[Path]] | None:
    """The scenario's newest run: a run file, or a Monte Carlo batch, with
    the run files that make it up."""
    run = latest_run_path(runs_dir, scenario)
    batch = latest_ensemble_batch(runs_dir / scenario, mode="monte_carlo")
    # Both names start with the same UTC timestamp format.
    if batch is not None and (run is None or batch.name >= run.name.split("_", 1)[0]):
        return batch, sorted(batch.glob("member_*.sqlite3"))
    if run is not None:
        return run, [run]
    return None


@dataclass(frozen=True)
class Unchanged:
    """The run that's still current, and why a rerun would be the same."""

    path: Path


def unchanged_run(
    data_dir: Path, runs_dir: Path, scenario: str, code: CodeTree,
) -> Unchanged | None:
    """``scenario``'s latest run if rerunning it couldn't change it: it
    finished, and the code trees, Python version and data tree are what it
    ran with, all clean then and now. Otherwise None."""
    if code.repo is None or code.trees is None:
        return None
    latest = _latest(runs_dir, scenario)
    if latest is None:
        return None
    path, files = latest
    if not files:
        return None
    if path.is_dir():
        from analysis.sim.ensemble import member_run_statuses, read_batch_manifest

        manifest = read_batch_manifest(path) or {}
        statuses = member_run_statuses(path)
        # A batch of another size (`sim run --ensemble-size N`) isn't the
        # one `sim all` would make.
        if not len(statuses) == manifest.get("member_count") == _ensemble_size(files[0]):
            return None
        if any(status not in _FINISHED for status in statuses.values()):
            return None
    data = data_stamps(data_dir, scenario)
    if data.dirty or not data.tree_id:
        return None
    python = sys.version.split()[0]
    for file in files:
        recorded = _recorded(file)
        if recorded is None:
            return None
        status, code_version, code_dirty, data_dirty, tree_id, python_version = recorded
        if path.is_file() and status != "succeeded":
            return None
        if code_dirty or data_dirty or tree_id != data.tree_id:
            return None
        if python_version.split()[0] != python:
            return None
        if _trees(code.repo, code_version) != code.trees:
            return None
    return Unchanged(path)


def _read(run_file: Path, sql: str) -> tuple | None:
    try:
        con = sqlite3.connect(f"{run_file.resolve().as_uri()}?mode=ro", uri=True)
        try:
            return con.execute(sql).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None


def _recorded(run_file: Path) -> tuple[str, str, int, int, str, str] | None:
    row = _read(run_file, "SELECT status, code_version, code_dirty, data_dirty, "
                          "scenario_tree_id, python_version FROM simulation_run")
    if row is None:
        return None
    status, code_version, code_dirty, data_dirty, tree_id, python_version = row
    return (str(status), str(code_version), int(code_dirty), int(data_dirty),
            str(tree_id), str(python_version))


def _ensemble_size(run_file: Path) -> int | None:
    """The ``ensemble_size`` a member ran under, from its scenario's meta."""
    row = _read(run_file, "SELECT meta_json FROM scenario")
    if row is None or not row[0]:
        return None
    return json.loads(row[0]).get("ensemble_size")
