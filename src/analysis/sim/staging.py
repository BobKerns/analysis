"""Validate-then-write edits to a scenario's CSVs (issue #44).

Every CLI command that edits scenario CSVs makes its changes in a staged copy
of the data dir's ``scenarios/`` tree, never in place. Only once every
scenario the edit could affect still loads are the changed files copied back,
each by an atomic ``os.replace``, with the earlier swaps rolled back if a
later one fails. A rejected edit, a crash partway through a multi-file edit,
or a Ctrl-C at a prompt therefore leaves the data repo exactly as it was. A
file that changed on disk while the edit was staged is never overwritten:
the edit is refused instead.

The edited scenario is also loaded *before* the edit (the preflight). A
scenario that already fails to load is reported as such, up front, instead of
the failure being pinned on whatever row the current command was adding.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from analysis.sim.loader import (
    LoadError,
    _scenario_lineage_names,
    load_scenario,
    resolve_scenario_name,
)
from analysis.sim.lineage import TEMPLATE_SCENARIOS_DIR, is_pool, is_template
from analysis.sim.schema import connect_sim_file

SCENARIOS = "scenarios"


class ScenarioAlreadyBroken(LoadError):
    """The scenario failed to load before the edit was attempted."""

    def __init__(self, scenario: str, issues: list[str]) -> None:
        super().__init__(issues)
        self.scenario = scenario


def template_refusal(name: str) -> str:
    """Why an edit can't write to template scenario ``name`` (ADR-003 §4.5a)."""
    return (
        f"{name} is a template scenario, read from {TEMPLATE_SCENARIOS_DIR}, and is not "
        f"edited from a data repo: build on it with 'sim scenario create NEW {name}', "
        f"or copy its folder into scenarios/ to replace it whole"
    )


class EditRejected(LoadError):
    """The edit would leave a scenario that no longer loads; nothing was written.

    ``issues`` holds every issue, a dependent's prefixed ``scenario <name>: ``.
    ``own`` is the edited scenario's own issues, and ``dependents`` each
    scenario built on it that would fail, with its issues unprefixed (#233):
    an edit is checked against every scenario built on the one changed, and a
    failure there needs saying as such, or it reads as an edit to the wrong
    scenario."""

    def __init__(
        self, issues: list[str], *, scenario: str = "",
        own: list[str] | None = None, dependents: dict[str, list[str]] | None = None,
    ) -> None:
        super().__init__(issues)
        self.scenario = scenario
        self.own = list(issues if own is None and dependents is None else own or [])
        self.dependents = dict(dependents or {})


@dataclass
class StagedEdit:
    """Handle for an edit in progress; see :func:`staged_edit`."""

    real_data_dir: Path
    data_dir: Path
    """Staged data dir: ``scenarios/`` and top-level files are private copies,
    other top-level entries are symlinks to the real ones (read-only use)."""
    name: str
    """The preflighted scenario (the edited one, or a new scenario's parent)."""
    _con: sqlite3.Connection | None
    warnings: list[str] = field(default_factory=list)
    """Load warnings from the validating loads, for the caller to show."""
    written: list[Path] = field(default_factory=list)
    """Real paths changed (written or removed) by the commit."""

    @property
    def con(self) -> sqlite3.Connection:
        """The preflighted scenario's intent DB as it was *before* this edit,
        for duplicate and reference checks. A pool has none (ADR-009), so
        an edit that needs one is refused, naming what to edit instead."""
        if self._con is None:
            issue = (f"{self.name} is a pool (ADR-009): it has no accounts, chains or rows of its "
                     "own; edit one of its components, or the pool itself with "
                     "'sim ensemble pool edit'")
            raise EditRejected([issue], scenario=self.name, own=[issue])
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


def _load_issues(data_dir: Path, name: str) -> tuple[list[str], list[str]]:
    """Load ``name`` into a throwaway intent DB; its issues (none when it
    loads) and its warnings. A pool has nothing to load: its definition and
    components are checked instead (ADR-009)."""
    if is_pool(data_dir / SCENARIOS, name):
        from analysis.sim.pool import pool_issues

        return pool_issues(data_dir, name), []
    with tempfile.TemporaryDirectory() as td:
        try:
            result = load_scenario(
                data_dir, name, intent_dir=Path(td),
                compute_stamps=False, allow_dirty_data=True,
            )
        except LoadError as exc:
            return list(exc.issues) or [str(exc)], []
        except Exception as exc:  # noqa: BLE001 -- any loader crash is a failed validation
            return [f"loader failed: {type(exc).__name__}: {exc}"], []
    return [], list(result.warnings)


def _stage(real: Path, stage: Path) -> None:
    for entry in real.iterdir():
        target = stage / entry.name
        if entry.name == SCENARIOS:
            shutil.copytree(entry, target, symlinks=False)
        elif entry.is_file():
            shutil.copy2(entry, target)
        else:
            target.symlink_to(entry, target_is_directory=entry.is_dir())


def _staged_files(root: Path) -> set[Path]:
    """Files under ``root`` that a staged edit copies: ``scenarios/**`` and
    top-level regular files (not the symlinked top-level folders)."""
    files = {p.relative_to(root) for p in (root / SCENARIOS).rglob("*") if p.is_file()} \
        if (root / SCENARIOS).is_dir() else set()
    files.update(Path(p.name) for p in root.iterdir() if p.is_file() and not p.is_symlink())
    return files


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(root: Path) -> dict[Path, str]:
    return {rel: _digest(root / rel) for rel in _staged_files(root)}


def _diff(baseline: dict[Path, str], stage: Path) -> tuple[list[Path], list[Path]]:
    """Relative paths the edit created or changed, and those it removed --
    measured against the real files as they were staged, not as they are now,
    so a concurrent change to some other file is never mistaken for ours."""
    staged = _manifest(stage)
    changed = sorted(rel for rel, digest in staged.items() if baseline.get(rel) != digest)
    removed = sorted(set(baseline) - set(staged))
    return changed, removed


class ConcurrentEdit(RuntimeError):
    """A file this edit would write changed on disk after it was staged."""


def _check_unchanged(real: Path, baseline: dict[Path, str], paths: list[Path]) -> None:
    moved = [
        rel for rel in paths
        if ((real / rel).is_file() and _digest(real / rel)) != baseline.get(rel, False)
    ]
    if moved:
        raise ConcurrentEdit(
            "changed on disk while this edit was in progress; nothing was written: "
            + ", ".join(str(real / rel) for rel in moved)
        )


def _affected_scenarios(stage: Path, touched: set[str]) -> list[str]:
    """Touched scenarios that still exist, plus every scenario inheriting from
    one, as a parent or a mix-in, and every pool with a component that is
    or inherits from one (ADR-009)."""
    scenarios_dir = stage / SCENARIOS

    def depends_on(name: str) -> list[str]:
        if not is_pool(scenarios_dir, name):
            return _scenario_lineage_names(scenarios_dir, name)
        from analysis.sim.pool import PoolError, read_pool

        try:
            components = read_pool(scenarios_dir, name).components
        except PoolError:
            return [name]
        return [name, *(n for c in components if (scenarios_dir / c).is_dir()
                        for n in _scenario_lineage_names(scenarios_dir, c))]

    return sorted(
        d.name for d in scenarios_dir.iterdir()
        if d.is_dir() and touched.intersection(depends_on(d.name))
    )


def _commit(real: Path, stage: Path, changed: list[Path], removed: list[Path]) -> list[Path]:
    """Copy the edit into ``real``: every new file is written beside its
    target first, then the targets are swapped in by ``os.replace``. If any
    step fails or is interrupted, the files already swapped are put back from
    backups, so the scenario ends up wholly old or wholly new. (Only a hard
    kill mid-swap can leave a mix; the swap is just renames, so the window is
    small.)"""
    created_dirs: list[Path] = []
    pending: list[tuple[Path, Path]] = []   # (new temp, target)
    backups: list[tuple[Path, Path]] = []   # (backup, target): target's old bytes
    done: list[tuple[Path | None, Path]] = []  # (backup or None if new, target)

    def scratch(dest: Path, kind: str) -> Path:
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=f".{kind}")
        os.close(fd)
        return Path(tmp)

    try:
        for rel in changed:
            dest = real / rel
            missing = [p for p in (dest.parent, *dest.parent.parents) if not p.exists()]
            dest.parent.mkdir(parents=True, exist_ok=True)
            created_dirs.extend(reversed(missing))
            tmp = scratch(dest, "tmp")
            pending.append((tmp, dest))
            shutil.copy2(stage / rel, tmp)
        for rel in removed:
            dest = real / rel
            backup = scratch(dest, "bak")
            backups.append((backup, dest))
            shutil.copy2(dest, backup)
        for tmp, dest in pending:
            backup = None
            if dest.exists():
                backup = scratch(dest, "bak")
                backups.append((backup, dest))
                shutil.copy2(dest, backup)
            os.replace(tmp, dest)
            done.append((backup, dest))
        for backup, dest in backups:
            if any(target == dest for _, target in done):
                continue
            dest.unlink()
            done.append((backup, dest))
    except BaseException:
        for backup, dest in reversed(done):
            if backup is None:
                dest.unlink(missing_ok=True)
            else:
                os.replace(backup, dest)
        for tmp, _ in pending:
            tmp.unlink(missing_ok=True)
        for backup, _ in backups:
            backup.unlink(missing_ok=True)
        for directory in reversed(created_dirs):
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        raise
    for backup, _ in backups:
        backup.unlink(missing_ok=True)
    for rel in removed:
        parent = (real / rel).parent
        while parent != real / SCENARIOS and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    return [real / rel for rel in (*changed, *removed)]


def _preflight(real: Path, name: str, intent_dir: Path) -> sqlite3.Connection:
    """Load ``name`` as it stands: its intent DB, or :class:`ScenarioAlreadyBroken`."""
    try:
        pre = load_scenario(
            real, name, intent_dir=intent_dir,
            compute_stamps=False, allow_dirty_data=True,
        )
    except LoadError as exc:
        raise ScenarioAlreadyBroken(name, list(exc.issues)) from None
    except Exception as exc:  # noqa: BLE001 -- a crash is a failed load too
        raise ScenarioAlreadyBroken(
            name, [f"loader failed: {type(exc).__name__}: {exc}"]
        ) from exc
    return connect_sim_file(pre.intent_path)


@contextmanager
def staged_edit(
    data_dir: Path | str, scenario: str | None, *, template_parent: bool = False,
) -> Generator[StagedEdit]:
    """Stage an edit to the data dir's scenario CSVs; write it only if it loads.

    ``scenario`` (or the current scenario) is preflighted: if it does not load
    as it stands, :class:`ScenarioAlreadyBroken` is raised and the body never
    runs. The body makes its changes under ``edit.data_dir`` (``edit.path``
    maps a real path to its staged copy). On a clean exit, every scenario
    whose folder changed, and every scenario inheriting from one, is loaded
    from the stage -- every scenario, when a shared top-level file changed.
    Any that fails -- unless it already failed before the edit -- raises
    :class:`EditRejected` with nothing written. If a file the edit would write
    changed on disk meanwhile, :class:`ConcurrentEdit` is raised, also with
    nothing written; otherwise the changed files are copied back. If the body raises, nothing is written.

    ``scenario`` may be a template only with ``template_parent``: the
    preflight of a scenario created from one, which writes only the new
    scenario's folder. Any edit that writes into a template's folder is
    still refused.
    """
    real = Path(data_dir).resolve()
    name = resolve_scenario_name(real, scenario).name
    if is_template(real / SCENARIOS, name) and not template_parent:
        raise EditRejected([template_refusal(name)], scenario=name, own=[template_refusal(name)])
    with ExitStack() as stack:
        intent_dir = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        con: sqlite3.Connection | None = None
        if is_pool(real / SCENARIOS, name):
            # A pool has no intent (ADR-009): its definition is checked
            # instead, and an edit that reads the intent is refused.
            if found := _load_issues(real, name)[0]:
                raise ScenarioAlreadyBroken(name, found)
        else:
            con = _preflight(real, name, intent_dir)
            con.row_factory = sqlite3.Row
            stack.callback(con.close)

        stage = Path(stack.enter_context(tempfile.TemporaryDirectory())) / real.name
        stage.mkdir()
        _stage(real, stage)
        baseline = _manifest(stage)
        edit = StagedEdit(real_data_dir=real, data_dir=stage, name=name, _con=con)
        yield edit

        changed, removed = _diff(baseline, stage)
        if not changed and not removed:
            return
        touched = {
            rel.parts[1] for rel in (*changed, *removed)
            if rel.parts[0] == SCENARIOS and len(rel.parts) > 2
        }
        # A folder written for a template name would replace the whole
        # template with just the edited files.
        templates = sorted(n for n in touched if is_template(real / SCENARIOS, n))
        if templates:
            refusals = [template_refusal(n) for n in templates]
            raise EditRejected(refusals, scenario=name, own=refusals)
        # A top-level file (persons.csv) is shared by every scenario, so
        # every scenario must still load with it.
        if any(rel.parts[0] != SCENARIOS for rel in (*changed, *removed)):
            touched |= {d.name for d in (stage / SCENARIOS).iterdir() if d.is_dir()}
        issues: list[str] = []
        own: list[str] = []
        dependents: dict[str, list[str]] = {}
        for affected in _affected_scenarios(stage, touched or {name}):
            found, warnings = _load_issues(stage, affected)
            label = "" if affected == name else f"scenario {affected}: "
            if not found:
                edit.warnings.extend(f"{label}{w}" for w in warnings)
                continue
            # Only a scenario that loaded before this edit can be broken by
            # it; one that was already failing is reported, not blamed.
            existed = (real / SCENARIOS / affected).is_dir()
            if affected != name and existed and _load_issues(real, affected)[0]:
                edit.warnings.append(
                    f"scenario {affected} already failed to load before this edit; "
                    f"run 'sim check {affected}'"
                )
                continue
            found = [issue.replace(str(stage), str(real)) for issue in found]
            issues.extend(f"{label}{issue}" for issue in found)
            if affected == name:
                own.extend(found)
            else:
                dependents[affected] = found
        if issues:
            raise EditRejected(issues, scenario=name, own=own, dependents=dependents)
        _check_unchanged(real, baseline, [*changed, *removed])
        edit.written = _commit(real, stage, changed, removed)
        # A background build of an edited scenario, or of one built on it, is
        # stale now: have the builder look again, which cancels it (#309).
        from analysis.sim.builder import notify

        notify(real, start=False)
