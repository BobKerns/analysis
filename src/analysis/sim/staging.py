"""Validate-then-write edits to a scenario's CSVs (issue #44).

Every CLI command that edits scenario CSVs makes its changes in a staged copy
of the data dir's ``scenarios/`` tree, never in place. Only once every
scenario the edit could affect still loads are the changed files copied back,
each by an atomic ``os.replace``. A rejected edit, a crash partway through a
multi-file edit, or a Ctrl-C at a prompt therefore leaves the data repo
exactly as it was.

The edited scenario is also loaded *before* the edit (the preflight). A
scenario that already fails to load is reported as such, up front, instead of
the failure being pinned on whatever row the current command was adding.
"""

from __future__ import annotations

import filecmp
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from analysis.sim.loader import (
    LoadError,
    LoadResult,
    _scenario_lineage_names,
    load_scenario,
    resolve_scenario_name,
)

SCENARIOS = "scenarios"


class ScenarioAlreadyBroken(LoadError):
    """The scenario failed to load before the edit was attempted."""

    def __init__(self, scenario: str, issues: list[str]) -> None:
        super().__init__(issues)
        self.scenario = scenario


class EditRejected(LoadError):
    """The edit would leave a scenario that no longer loads; nothing was written."""


@dataclass
class StagedEdit:
    """Handle for an edit in progress; see :func:`staged_edit`."""

    real_data_dir: Path
    data_dir: Path
    """Staged data dir: ``scenarios/`` and top-level files are private copies,
    other top-level entries are symlinks to the real ones (read-only use)."""
    name: str
    """The preflighted scenario (the edited one, or a new scenario's parent)."""
    _con: sqlite3.Connection
    warnings: list[str] = field(default_factory=list)
    """Load warnings from the validating loads, for the caller to show."""
    written: list[Path] = field(default_factory=list)
    """Real paths changed (written or removed) by the commit."""

    @property
    def con(self) -> sqlite3.Connection:
        """The preflighted scenario's intent DB as it was *before* this edit,
        for duplicate and reference checks."""
        return self._con

    @property
    def scenario_dir(self) -> Path:
        """Staged folder of the preflighted scenario."""
        return self.data_dir / SCENARIOS / self.name

    def path(self, real: Path | str) -> Path:
        """The staged counterpart of ``real``, a path inside the real data dir."""
        rel = Path(real).resolve().relative_to(self.real_data_dir)
        staged = self.data_dir / rel
        # Anything outside the copied entries is a symlink back into the real
        # data dir, so a write there would bypass validation entirely.
        if rel.parts[0] != SCENARIOS and (len(rel.parts) > 1 or (self.data_dir / rel.parts[0]).is_symlink()):
            raise ValueError(f"not a stageable scenario path: {real}")
        return staged


def _load_issues(data_dir: Path, name: str) -> tuple[list[str], LoadResult | None]:
    """Load ``name`` into a throwaway intent DB; its issues, or its result."""
    with tempfile.TemporaryDirectory() as td:
        try:
            result = load_scenario(
                data_dir, name, intent_dir=Path(td),
                compute_stamps=False, allow_dirty_data=True,
            )
        except LoadError as exc:
            return list(exc.issues), None
        except Exception as exc:  # noqa: BLE001 -- any loader crash is a failed validation
            return [f"loader failed: {type(exc).__name__}: {exc}"], None
    return [], result


def _stage(real: Path, stage: Path) -> None:
    for entry in real.iterdir():
        target = stage / entry.name
        if entry.name == SCENARIOS:
            shutil.copytree(entry, target, symlinks=False)
        elif entry.is_file():
            shutil.copy2(entry, target)
        else:
            target.symlink_to(entry, target_is_directory=entry.is_dir())


def _files(root: Path) -> set[Path]:
    return {p.relative_to(root) for p in root.rglob("*") if p.is_file()}


def _diff(real: Path, stage: Path) -> tuple[list[Path], list[Path]]:
    """Relative paths that are new or changed in the stage, and those removed."""
    real_files = _files(real / SCENARIOS) if (real / SCENARIOS).is_dir() else set()
    stage_files = _files(stage / SCENARIOS)
    changed = [
        Path(SCENARIOS) / rel for rel in sorted(stage_files)
        if rel not in real_files
        or not filecmp.cmp(real / SCENARIOS / rel, stage / SCENARIOS / rel, shallow=False)
    ]
    removed = [Path(SCENARIOS) / rel for rel in sorted(real_files - stage_files)]
    for entry in stage.iterdir():
        if entry.is_file() and not entry.is_symlink():
            other = real / entry.name
            if not other.is_file() or not filecmp.cmp(other, entry, shallow=False):
                changed.append(Path(entry.name))
    return changed, removed


def _affected_scenarios(stage: Path, touched: set[str]) -> list[str]:
    """Touched scenarios that still exist, plus every scenario inheriting from one."""
    scenarios_dir = stage / SCENARIOS
    return sorted(
        d.name for d in scenarios_dir.iterdir()
        if d.is_dir() and touched.intersection(_scenario_lineage_names(scenarios_dir, d.name))
    )


def _commit(real: Path, stage: Path, changed: list[Path], removed: list[Path]) -> list[Path]:
    # Copy every change next to its target first, then swap them all in: the
    # window where some files are new and others old is just the renames.
    pending: list[tuple[Path, Path]] = []
    try:
        for rel in changed:
            dest = real / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
            os.close(fd)
            shutil.copy2(stage / rel, tmp)
            pending.append((Path(tmp), dest))
    except BaseException:
        for tmp, _ in pending:
            tmp.unlink(missing_ok=True)
        raise
    for tmp, dest in pending:
        os.replace(tmp, dest)
    for rel in removed:
        (real / rel).unlink(missing_ok=True)
    for rel in removed:
        parent = (real / rel).parent
        while parent != real / SCENARIOS and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    return [real / rel for rel in (*changed, *removed)]


@contextmanager
def staged_edit(data_dir: Path | str, scenario: str | None) -> Iterator[StagedEdit]:
    """Stage an edit to the data dir's scenario CSVs; write it only if it loads.

    ``scenario`` (or the current scenario) is preflighted: if it does not load
    as it stands, :class:`ScenarioAlreadyBroken` is raised and the body never
    runs. The body makes its changes under ``edit.data_dir`` (``edit.path``
    maps a real path to its staged copy). On a clean exit, every scenario
    whose folder changed, and every scenario inheriting from one, is loaded
    from the stage. Any that fails -- unless it already failed before the
    edit -- raises :class:`EditRejected` with nothing written; otherwise the
    changed files are copied back. If the body raises, nothing is written.
    """
    real = Path(data_dir).resolve()
    name = resolve_scenario_name(real, scenario).name
    with ExitStack() as stack:
        intent_dir = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        try:
            pre = load_scenario(
                real, name, intent_dir=intent_dir,
                compute_stamps=False, allow_dirty_data=True,
            )
        except LoadError as exc:
            raise ScenarioAlreadyBroken(name, list(exc.issues)) from None
        con = sqlite3.connect(pre.intent_path)
        con.row_factory = sqlite3.Row
        stack.callback(con.close)

        stage = Path(stack.enter_context(tempfile.TemporaryDirectory())) / real.name
        stage.mkdir()
        _stage(real, stage)
        edit = StagedEdit(real_data_dir=real, data_dir=stage, name=name, _con=con)
        yield edit

        changed, removed = _diff(real, stage)
        if not changed and not removed:
            return
        touched = {
            rel.parts[1] for rel in (*changed, *removed)
            if rel.parts[0] == SCENARIOS and len(rel.parts) > 2
        }
        issues: list[str] = []
        for affected in _affected_scenarios(stage, touched or {name}):
            found, result = _load_issues(stage, affected)
            label = "" if affected == name else f"scenario {affected}: "
            if result is not None:
                edit.warnings.extend(f"{label}{w}" for w in result.warnings)
                continue
            # Only a scenario that loaded before this edit can be broken by
            # it; one that was already failing is reported, not blamed.
            existed = (real / SCENARIOS / affected).is_dir()
            if affected != name and existed and _load_issues(real, affected)[1] is None:
                edit.warnings.append(
                    f"scenario {affected} already failed to load before this edit; "
                    f"run 'sim check {affected}'"
                )
                continue
            issues.extend(
                f"{label}{issue}".replace(str(stage), str(real)) for issue in found
            )
        if issues:
            raise EditRejected(issues)
        edit.written = _commit(real, stage, changed, removed)
