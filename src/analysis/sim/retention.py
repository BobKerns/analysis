"""Automatic pruning of ``state/`` by the data repo's ``.retention.yaml`` (#195).

ADR-004 §10 is the specification. Run files and ensemble batches pile up
under ``state/runs`` (and member intents under ``state/intent``), so
``sim load`` and ``sim run`` prune them after their own work, following
rules in ``$ANALYSIS_DATA_DIR/.retention.yaml``, which they create with
``DEFAULT_RETENTION`` when it is missing::

    intents:            # ensemble/sensitivity batches' member intents
      - keep: 5
        since: 7 days
    runs:               # single run files and ensemble/sensitivity batches
      - keep: 5
        since: 7 days
    pins:               # never pruned; relative to the data repo root
      - state/runs/<stamp>_<scenario>_<version>.sqlite3

Items are grouped per scenario and kind (single runs, run batches, intent
batches) and ranked newest first. An item is kept when any rule keeps it, and
a rule keeps it when any condition it gives holds (``keep: N``: among the N
newest; ``since: <duration>``: made within that long). Whatever the rules
say, the newest item of each group is kept (a failed run's latest attempt
included), and so is each scenario's newest successful run and batch. A
missing or empty section prunes nothing of that kind.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from analysis.sim.ensemble import member_run_statuses, read_batch_manifest
from analysis.sim.schema import connect_sim_file

__all__ = [
    "ACTIVE_WINDOW",
    "DEFAULT_RETENTION",
    "RETENTION_FILENAME",
    "StateItem",
    "PruneResult",
    "Retention",
    "RetentionError",
    "Rule",
    "parse_retention",
    "plan_prune",
    "prune_state",
    "read_retention",
]

RETENTION_FILENAME = ".retention.yaml"

# Written when a data dir has no .retention.yaml; templates/ and
# prototype_data/ ship this same text.
DEFAULT_RETENTION = """\
# Retention rules for state/: after each `sim load` and `sim run`, old run
# files and batches are deleted unless a rule here keeps them (ADR-004 §10).
#
# Items are counted per scenario, newest first. An item is kept when any rule
# in its list keeps it, and a rule keeps it when any of its conditions holds:
#   keep: N            among the N newest
#   since: 7 days      made within that long (minutes, hours, days or weeks)
# Each scenario's newest run and batch, and its newest successful ones, are
# always kept. Delete a section's rules to keep everything of that kind.

# Ensemble and sensitivity batches' member intents (state/intent/<scenario>/).
intents:
  - keep: 5
    since: 7 days

# Single run files and ensemble/sensitivity batches (state/runs/).
runs:
  - keep: 5
    since: 7 days

# Runs or batches never to prune, relative to the data repo root (beside
# state/), e.g. state/runs/<stamp>_<scenario>_<version>.sqlite3. Pinning a
# batch keeps both its runs and its member intents.
pins: []
"""

# `sim run` names: `<stamp>[-N]_<scenario>_<data version>.sqlite3` for a
# single run (locks.reserve_run_path), `<stamp>[-N]` for a batch folder
# (cli._reserve_batch); -N tells apart runs made in the same second. Only names that
# match are ever pruned, so anything else a user keeps there is left alone.
_STAMP = r"\d{4}-\d\d-\d\dT\d{6}Z"
_RUN_FILE = re.compile(rf"^({_STAMP})(?:-(\d+))?_(.+)_[^_]+\.sqlite3$")
_BATCH_DIR = re.compile(rf"^({_STAMP})(?:-(\d+))?$")
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(minute|hour|day|week)s?\s*$", re.IGNORECASE)
_SECTIONS = ("intents", "runs", "pins")
# Beyond this, `now - since` could leave datetime's range.
_LONGEST_SINCE = timedelta(days=366_000)
# A run or batch written to this recently may still be in progress in another
# `sim run` (concurrent runs are allowed, cli._reserve_batch), so it is never
# pruned, whatever its rank.
ACTIVE_WINDOW = timedelta(minutes=10)


class RetentionError(ValueError):
    """``.retention.yaml`` can't be read as retention rules."""


@dataclass(frozen=True)
class Rule:
    """One retention rule: keeps an item when any condition given holds, so
    ``keep: 5, since: 7 days`` keeps the five newest and everything from the
    last week, the way backup tools' keep-last and keep-within combine."""

    keep: int | None = None
    since: timedelta | None = None

    def keeps(self, rank: int, made: datetime, now: datetime) -> bool:
        return ((self.keep is not None and rank < self.keep)
                or (self.since is not None and made >= now - self.since))


@dataclass(frozen=True)
class Retention:
    """Parsed ``.retention.yaml``. ``None`` for a section means keep all."""

    intents: tuple[Rule, ...] | None = None
    runs: tuple[Rule, ...] | None = None
    pins: tuple[Path, ...] = ()


@dataclass(frozen=True)
class StateItem:
    """A prunable run file or batch folder under state/."""

    path: Path
    kind: str  # "run", "batch" (state/runs) or "intent" (state/intent batch)
    scenario: str
    made: datetime
    order: tuple[str, int]  # name sort key within the same second


@dataclass
class PruneResult:
    runs: list[Path] = field(default_factory=list)
    batches: list[Path] = field(default_factory=list)
    intents: list[Path] = field(default_factory=list)
    created: Path | None = None  # the default rules file, when this wrote it

    def __bool__(self) -> bool:
        return bool(self.runs or self.batches or self.intents)

    def summary(self) -> str:
        parts = [
            f"{len(paths)} {label}"
            for paths, label in ((self.runs, "run file(s)"), (self.batches, "run batch(es)"),
                                 (self.intents, "intent batch(es)"))
            if paths
        ]
        return f"pruned {', '.join(parts)} ({RETENTION_FILENAME})"


def read_retention(data_dir: Path) -> Retention | None:
    """The data repo's retention rules, or ``None`` when it has no file."""
    path = Path(data_dir) / RETENTION_FILENAME
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise RetentionError(f"not valid UTF-8: {exc}") from None
    return parse_retention(text, base=Path(data_dir).resolve().parent)


def parse_retention(text: str, *, base: Path) -> Retention:
    """Parse retention YAML; relative ``pins`` resolve against ``base``, the
    data repo root that holds ``state/``."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RetentionError(f"not valid YAML: {exc}") from None
    if doc is None:
        return Retention()
    if not isinstance(doc, dict):
        raise RetentionError("expected a mapping with intents, runs and pins")
    unknown = sorted(str(k) for k in doc if k not in _SECTIONS)
    if unknown:
        raise RetentionError(f"unknown section(s): {', '.join(unknown)} "
                             f"(expected {', '.join(_SECTIONS)})")
    pins = doc.get("pins")
    pins = [] if pins is None else pins
    if not isinstance(pins, list) or not all(isinstance(p, str) and p.strip() for p in pins):
        raise RetentionError("pins: expected a list of paths")
    return Retention(
        intents=_rules("intents", doc.get("intents")),
        runs=_rules("runs", doc.get("runs")),
        pins=tuple((base / p.strip()).resolve() for p in pins),
    )


def _rules(section: str, raw: Any) -> tuple[Rule, ...] | None:
    # An empty section keeping nothing would delete everything but the
    # newest items, which is never what a blank section means. Other falsy
    # values (0, "") are malformed, not empty, so they fall through.
    if raw is None or raw == []:
        return None
    if not isinstance(raw, list):
        raise RetentionError(f"{section}: expected a list of rules")
    rules = []
    for i, entry in enumerate(raw, start=1):
        where = f"{section} rule {i}"
        if not isinstance(entry, dict) or not entry:
            raise RetentionError(f"{where}: expected keep: N and/or since: <duration>, "
                                 f"got {entry!r}")
        unknown = sorted(str(k) for k in entry if k not in ("keep", "since"))
        if unknown:
            raise RetentionError(f"{where}: unknown key(s): {', '.join(unknown)}")
        keep = entry.get("keep")
        if keep is not None and (isinstance(keep, bool) or not isinstance(keep, int) or keep < 1):
            raise RetentionError(f"{where}: keep must be a positive whole number, got {keep!r}")
        since = entry.get("since")
        if keep is None and since is None:
            raise RetentionError(f"{where}: give keep, since, or both")
        rules.append(Rule(keep=keep, since=None if since is None else _duration(where, since)))
    return tuple(rules)


def _duration(where: str, raw: Any) -> timedelta:
    match = _DURATION.match(str(raw))
    if match is None:
        raise RetentionError(f"{where}: since must look like '3 days' (minutes, hours, "
                             f"days or weeks), got {raw!r}")
    amount, unit = float(match[1]), match[2].lower()
    try:
        since = timedelta(**{f"{unit}s": amount})
    except OverflowError:
        since = _LONGEST_SINCE + _LONGEST_SINCE
    if since > _LONGEST_SINCE:
        raise RetentionError(f"{where}: since is longer than 1000 years, got {raw!r}")
    return since


def plan_prune(
    retention: Retention, intent_dir: Path, runs_dir: Path, *, now: datetime | None = None,
) -> list[StateItem]:
    """What ``retention`` would prune, without touching anything."""
    now = now or datetime.now(UTC)
    pinned_batches = set()
    items = [*_run_items(Path(runs_dir)), *_intent_items(Path(intent_dir))]
    for item in items:
        if item.kind != "run" and _is_pinned(item.path, retention.pins):
            pinned_batches.add((item.scenario, item.path.name))

    doomed: list[StateItem] = []
    groups: dict[tuple[str, str], list[StateItem]] = {}
    for item in items:
        groups.setdefault((item.kind, item.scenario), []).append(item)
    for (kind, _scenario), group in sorted(groups.items()):
        rules = retention.intents if kind == "intent" else retention.runs
        if rules is None:
            continue
        group.sort(key=lambda it: (it.made, it.order), reverse=True)
        protected = {0}
        if kind != "intent":
            protected.add(_newest_succeeded(group))
        for rank, item in enumerate(group):
            if (rank in protected
                    or _active(item.path, now)
                    or any(rule.keeps(rank, item.made, now) for rule in rules)
                    or _is_pinned(item.path, retention.pins)
                    or (kind != "run" and (item.scenario, item.path.name) in pinned_batches)):
                continue
            doomed.append(item)
    return doomed


def prune_state(
    data_dir: Path, intent_dir: Path, runs_dir: Path, *, now: datetime | None = None,
) -> PruneResult:
    """Apply the data repo's ``.retention.yaml`` to ``intent_dir`` and
    ``runs_dir``. A missing file is first created with ``DEFAULT_RETENTION``
    (``result.created``); a bad file raises RetentionError before anything is
    deleted."""
    result = PruneResult()
    retention = read_retention(data_dir)
    if retention is None:
        path = Path(data_dir) / RETENTION_FILENAME
        path.write_text(DEFAULT_RETENTION)
        result.created = path
        retention = parse_retention(DEFAULT_RETENTION, base=Path(data_dir).resolve().parent)
    for item in plan_prune(retention, intent_dir, runs_dir, now=now):
        if item.kind == "run":
            item.path.unlink(missing_ok=True)
            Path(f"{item.path}-journal").unlink(missing_ok=True)
            result.runs.append(item.path)
        else:
            shutil.rmtree(item.path)
            _remove_if_empty(item.path.parent)
            (result.batches if item.kind == "batch" else result.intents).append(item.path)
    return result


def _run_items(runs_dir: Path) -> Iterable[StateItem]:
    if not runs_dir.is_dir():
        return
    for path in runs_dir.iterdir():
        if path.is_symlink():
            continue  # only what `sim run` wrote here, never what a link reaches
        match = _RUN_FILE.match(path.name)
        if match and path.is_file():
            yield StateItem(path, "run", match[3], _stamp(match[1]),
                            (match[1], int(match[2] or 1)))
        elif path.is_dir():
            yield from _batch_items(path, "batch")


def _intent_items(intent_dir: Path) -> Iterable[StateItem]:
    if not intent_dir.is_dir():
        return
    for path in intent_dir.iterdir():
        if path.is_dir() and not path.is_symlink():
            yield from _batch_items(path, "intent")


def _batch_items(scenario_dir: Path, kind: str) -> Iterable[StateItem]:
    for path in scenario_dir.iterdir():
        match = _BATCH_DIR.match(path.name)
        if match and path.is_dir() and not path.is_symlink():
            yield StateItem(path, kind, scenario_dir.name, _stamp(match[1]),
                        (match[1], int(match[2] or 1)))


def _stamp(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H%M%SZ").replace(tzinfo=UTC)


def _newest_succeeded(group: list[StateItem]) -> int:
    """Rank of the group's newest successful run or batch, or -1. Checked
    newest first and stopping at the first success, so a long history of
    ensemble batches isn't opened member by member on every prune."""
    for rank, item in enumerate(group):
        if _succeeded(item):
            return rank
    return -1


def _succeeded(item: StateItem) -> bool:
    try:
        if item.kind == "run":
            con = connect_sim_file(f"file:{item.path}?mode=ro", uri=True)
            try:
                row = con.execute("SELECT status FROM simulation_run WHERE id = 1").fetchone()
            finally:
                con.close()
            return row is not None and row[0] == "succeeded"
        manifest = read_batch_manifest(item.path)
        if manifest is None or manifest.get("failures"):
            return False
        statuses = member_run_statuses(item.path)
        return (len(statuses) == manifest.get("member_count")
                and all(s == "succeeded" for s in statuses.values()))
    except (sqlite3.DatabaseError, OSError, ValueError):
        return False  # unreadable: not a success to protect


def _active(path: Path, now: datetime) -> bool:
    """Written within ACTIVE_WINDOW of ``now``. A batch folder's own mtime
    moves each time a member file is added to it."""
    try:
        return path.stat().st_mtime >= (now - ACTIVE_WINDOW).timestamp()
    except OSError:
        return False


def _is_pinned(path: Path, pins: tuple[Path, ...]) -> bool:
    """A pin names the item itself or something inside it (a member of a
    batch pins the whole batch, which is never pruned member by member)."""
    resolved = path.resolve()
    return any(pin == resolved or pin.is_relative_to(resolved) for pin in pins)


def _remove_if_empty(directory: Path) -> None:
    try:
        directory.rmdir()
    except OSError:
        pass  # not empty (other batches, base_case.sqlite3): keep it
