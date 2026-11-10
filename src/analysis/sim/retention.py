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
    logs:               # the background builder's daily log files (#314)
      - keep: 7
        since: 30 days

Items are grouped per scenario and kind (single runs, Monte Carlo batches,
sensitivity batches, and the member intents of each kind of batch: #454
keeps a sensitivity batch from pushing out Monte Carlo ones) and ranked
newest first. An item is kept when any rule keeps it, and
a rule keeps it when any condition it gives holds (``keep: N``: among the N
newest; ``since: <duration>``: made within that long). Whatever the rules
say, the newest item of each group is kept (a failed run's latest attempt
included), and so is each scenario's newest successful run and batch. A
missing or empty section prunes nothing of that kind.

``logs`` rules rank the builder's daily log files (``state/builder/logs/``)
newest first by the date in their names, aged from the end of that day; the daemon applies them as it starts and
each time it starts a new day's file (``builder_log.prune_logs``), and the
newest file is always kept.

A ``space`` section (#536) sets disk limits: ``max_size`` for all of
``state/``, ``min_free`` and ``min_free_percent`` for the disk it is on. When
one is breached after the rules have pruned, more is deleted, oldest first
across runs, batches, intent batches and log files, until the limits hold.
Only what is protected survives that: the items always kept above, pins, and
whatever a rule's ``protected:`` list keeps (same ``keep``/``since`` terms;
a protected item is never deleted, by the rules either)::

    runs:
      - keep: 5
        since: 7 days
        protected:
          - keep: 2
          - since: 1 day
    space:
      max_size: 20 GB
      min_free: 10 GB
      min_free_percent: 5%
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import yaml

from analysis.sim import builder_log
from analysis.sim.ensemble import member_run_statuses, read_batch_manifest
from analysis.sim.loader import BATCH_MODE_DIRS, DEFAULT_STATE_DIR, find_state_dir
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
    "Space",
    "format_size",
    "parse_duration",
    "parse_size",
    "parse_retention",
    "plan_prune",
    "plan_space_prune",
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
# Items are counted per scenario, newest first, and Monte Carlo and
# sensitivity batches apart. An item is kept when any rule
# in its list keeps it, and a rule keeps it when any of its conditions holds:
#   keep: N            among the N newest
#   since: 7 days      made within that long (minutes, hours, days or weeks)
# Each scenario's newest run and batch, and its newest successful ones, are
# always kept. Delete a section's rules to keep everything of that kind.
#
# A rule's `protected:` list (same terms) keeps an item whatever happens.
# It matters when a `space` limit below is breached: then more is deleted,
# oldest first, until the limits hold, sparing only what is protected,
# always kept, or pinned.

# Ensemble and sensitivity batches' member intents
# (state/intent/<scenario>/monte-carlo/ and .../sensitivity/).
intents:
  - keep: 5
    since: 7 days
    protected:
      - keep: 2
      - since: 1 day

# Single run files and ensemble/sensitivity batches (state/runs/; batches in
# state/runs/<scenario>/monte-carlo/ and .../sensitivity/, counted apart).
runs:
  - keep: 5
    since: 7 days
    protected:
      - keep: 2
      - since: 1 day

# Runs or batches never to prune, relative to the data repo root (beside
# state/), e.g. state/runs/<stamp>_<scenario>_<version>.sqlite3. Pinning a
# batch keeps both its runs and its member intents.
pins: []

# The background builder's log, one file per day (state/builder/logs/).
logs:
  - keep: 7
    since: 30 days
    protected:
      - keep: 2
      - since: 1 day

# Disk limits, checked after each `sim load` and `sim run`. Uncomment any:
# state/ as a whole may use at most max_size, and at least min_free and
# min_free_percent must stay free on its disk. Sizes in B, KB, MB, GB or TB
# (or KiB, MiB, GiB, TiB).
space:
  # max_size: 20 GB
  # min_free: 10 GB
  # min_free_percent: 5%
"""

# `sim run` names: `<stamp>[-N]_<scenario>_<data version>.sqlite3` for a
# single run (locks.reserve_run_path), `<stamp>[-N]` for a batch folder in
# its mode's folder (cli._reserve_batch, #454); -N tells apart runs made in the same second. Only names that
# match are ever pruned, so anything else a user keeps there is left alone.
_STAMP = r"\d{4}-\d\d-\d\dT\d{6}Z"
_RUN_FILE = re.compile(rf"^({_STAMP})(?:-(\d+))?_(.+)_[^_]+\.sqlite3$")
_BATCH_DIR = re.compile(rf"^({_STAMP})(?:-(\d+))?$")
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(minute|hour|day|week)s?\s*$", re.IGNORECASE)
_SECTIONS = ("intents", "runs", "pins", "logs", "space")
_RULE_SECTIONS = ("intents", "runs", "logs")
_SIZE_UNITS = {"b": 1}
for _power, _prefix in enumerate(("k", "m", "g", "t"), start=1):
    _SIZE_UNITS[f"{_prefix}b"] = 1000 ** _power
    _SIZE_UNITS[f"{_prefix}ib"] = 1024 ** _power
_SIZE = re.compile(rf"^\s*(\d+(?:\.\d+)?)\s*({'|'.join(_SIZE_UNITS)})\s*$", re.IGNORECASE)
_PERCENT = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*%\s*$")
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
class Space:
    """The ``space`` section's limits, in bytes and percent; ``None`` is no limit."""

    max_size: int | None = None
    min_free: int | None = None
    min_free_percent: float | None = None

    def __bool__(self) -> bool:
        return any(v is not None for v in (self.max_size, self.min_free, self.min_free_percent))


@dataclass(frozen=True)
class Retention:
    """Parsed ``.retention.yaml``. ``None`` for a section means keep all.
    ``protected`` holds each section's ``protected:`` rules, from all its
    rules together."""

    intents: tuple[Rule, ...] | None = None
    runs: tuple[Rule, ...] | None = None
    pins: tuple[Path, ...] = ()
    logs: tuple[Rule, ...] | None = None
    protected: Mapping[str, tuple[Rule, ...]] = field(default_factory=dict)
    space: Space = Space()


@dataclass(frozen=True)
class StateItem:
    """A prunable run file or batch folder under state/."""

    path: Path
    kind: str  # "run", "batch" (state/runs), "intent" (state/intent batch) or "log"
    scenario: str
    mode: str  # a batch's or intent batch's mode folder (#454); "" for a run
    made: datetime
    order: tuple[str, int]  # name sort key within the same second


@dataclass
class PruneResult:
    runs: list[Path] = field(default_factory=list)
    batches: list[Path] = field(default_factory=list)
    intents: list[Path] = field(default_factory=list)
    logs: list[Path] = field(default_factory=list)
    created: Path | None = None  # the default rules file, when this wrote it
    for_space: int = 0  # how many of the deletions were for the space limits
    # Limits still breached once everything unprotected is gone.
    shortfalls: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.runs or self.batches or self.intents or self.logs)

    def summary(self) -> str:
        parts = [
            f"{len(paths)} {label}"
            for paths, label in ((self.runs, "run file(s)"), (self.batches, "run batch(es)"),
                                 (self.intents, "intent batch(es)"), (self.logs, "log file(s)"))
            if paths
        ]
        why = f"; {self.for_space} for disk space" if self.for_space else ""
        return f"pruned {', '.join(parts)} ({RETENTION_FILENAME}{why})"


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
        raise RetentionError("expected a mapping with intents, runs, pins and logs")
    unknown = sorted(str(k) for k in doc if k not in _SECTIONS)
    if unknown:
        raise RetentionError(f"unknown section(s): {', '.join(unknown)} "
                             f"(expected {', '.join(_SECTIONS)})")
    pins = doc.get("pins")
    pins = [] if pins is None else pins
    if not isinstance(pins, list) or not all(isinstance(p, str) and p.strip() for p in pins):
        raise RetentionError("pins: expected a list of paths")
    sections = {name: _rules(name, doc.get(name)) for name in _RULE_SECTIONS}
    return Retention(
        intents=sections["intents"][0],
        runs=sections["runs"][0],
        pins=tuple((base / p.strip()).resolve() for p in pins),
        logs=sections["logs"][0],
        protected={name: rules[1] for name, rules in sections.items() if rules[1]},
        space=_space(doc.get("space")),
    )


def _rules(section: str, raw: Any) -> tuple[tuple[Rule, ...] | None, tuple[Rule, ...]]:
    """A section's rules, and the ``protected:`` rules of all of them."""
    # An empty section keeping nothing would delete everything but the
    # newest items, which is never what a blank section means. Other falsy
    # values (0, "") are malformed, not empty, so they fall through.
    if raw is None or raw == []:
        return None, ()
    if not isinstance(raw, list):
        raise RetentionError(f"{section}: expected a list of rules")
    rules: list[Rule] = []
    protected: list[Rule] = []
    for i, entry in enumerate(raw, start=1):
        where = f"{section} rule {i}"
        rules.append(_rule(where, entry, ("keep", "since", "protected")))
        guard = entry.get("protected")
        if guard is None:
            continue
        if not isinstance(guard, list) or not guard:
            raise RetentionError(f"{where}: protected: expected a list of rules")
        protected.extend(_rule(f"{where} protected rule {j}", sub, ("keep", "since"))
                         for j, sub in enumerate(guard, start=1))
    return tuple(rules), tuple(protected)


def _rule(where: str, entry: Any, keys: tuple[str, ...]) -> Rule:
    if not isinstance(entry, dict) or not entry:
        raise RetentionError(f"{where}: expected keep: N and/or since: <duration>, "
                             f"got {entry!r}")
    unknown = sorted(str(k) for k in entry if k not in keys)
    if unknown:
        raise RetentionError(f"{where}: unknown key(s): {', '.join(unknown)}")
    keep = entry.get("keep")
    if keep is not None and (isinstance(keep, bool) or not isinstance(keep, int) or keep < 1):
        raise RetentionError(f"{where}: keep must be a positive whole number, got {keep!r}")
    since = entry.get("since")
    if keep is None and since is None:
        raise RetentionError(f"{where}: give keep, since, or both")
    return Rule(keep=keep, since=None if since is None else _duration(where, since))


def _space(raw: Any) -> Space:
    if raw is None:
        return Space()
    if not isinstance(raw, dict):
        raise RetentionError("space: expected max_size, min_free and/or min_free_percent")
    keys = ("max_size", "min_free", "min_free_percent")
    unknown = sorted(str(k) for k in raw if k not in keys)
    if unknown:
        raise RetentionError(f"space: unknown key(s): {', '.join(unknown)} "
                             f"(expected {', '.join(keys)})")

    def size(key: str) -> int | None:
        value = raw.get(key)
        if value is None:
            return None
        try:
            return parse_size(value)
        except RetentionError as exc:
            raise RetentionError(f"space: {key} {exc}") from None

    percent = raw.get("min_free_percent")
    if percent is not None:
        match = _PERCENT.match(str(percent))
        if match is None or not 0 < float(match[1]) < 100:
            raise RetentionError("space: min_free_percent must be a percentage between "
                                 f"0% and 100%, like 5%, got {percent!r}")
        percent = float(match[1])
    return Space(max_size=size("max_size"), min_free=size("min_free"),
                 min_free_percent=percent)


def parse_size(raw: Any) -> int:
    """``'20 GB'`` (B, KB, MB, GB, TB: powers of 1000; KiB ... TiB: of 1024)
    as a number of bytes. A bare number is refused: its unit would be a guess."""
    match = _SIZE.match(str(raw))
    if match is None or isinstance(raw, bool):
        raise RetentionError(f"must be a size like '20 GB' (B, KB, MB, GB, TB, "
                             f"or KiB, MiB, GiB, TiB), got {raw!r}")
    return int(float(match[1]) * _SIZE_UNITS[match[2].lower()])


def format_size(size: float) -> str:
    """Bytes as a short decimal size, ``'1.5 GB'``."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1000:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    return f"{size:.1f} TB"


def _duration(where: str, raw: Any) -> timedelta:
    try:
        return parse_duration(raw)
    except RetentionError as exc:
        raise RetentionError(f"{where}: since {exc}") from None


def parse_duration(raw: Any) -> timedelta:
    """``'3 days'`` (minutes, hours, days or weeks) as a timedelta; also
    what ``sim builder log --since`` takes."""
    match = _DURATION.match(str(raw))
    if match is None:
        raise RetentionError(f"must look like '3 days' (minutes, hours, "
                             f"days or weeks), got {raw!r}")
    amount, unit = float(match[1]), match[2].lower()
    try:
        since = timedelta(**{f"{unit}s": amount})
    except OverflowError:
        since = _LONGEST_SINCE + _LONGEST_SINCE
    if since > _LONGEST_SINCE:
        raise RetentionError(f"is longer than 1000 years, got {raw!r}")
    return since


def plan_prune(
    retention: Retention, intent_dir: Path, runs_dir: Path, *, now: datetime | None = None,
) -> list[StateItem]:
    """What ``retention``'s rules would prune, without touching anything."""
    return _classify(retention, intent_dir, runs_dir, None, now or datetime.now(UTC))[0]


class _Usage(Protocol):
    @property
    def total(self) -> int: ...
    @property
    def free(self) -> int: ...


# shutil.disk_usage, or a stand-in for tests.
DiskUsage = Callable[[Path], _Usage]


def plan_space_prune(
    space: Space, spare: list[StateItem], state_roots: Iterable[Path], *,
    disk_usage: DiskUsage = shutil.disk_usage,
) -> tuple[list[StateItem], list[str]]:
    """Of ``spare`` (items no rule needs, or kept but unprotected), those to
    delete, oldest first, until ``space``'s limits hold; and a line for each
    limit still breached after that. ``state_roots`` are the folders whose
    total ``max_size`` limits; free space is checked on each disk that holds
    one of them or an item. Freed space is estimated from the items' sizes,
    so nothing is touched."""
    if not space:
        return [], []
    roots = _distinct_roots(state_roots)
    used = sum(_disk_size(root) for root in roots) if space.max_size is not None else 0
    disks: dict[int, list[int]] = {}  # device: [total, free]
    where: dict[int, Path] = {}

    def disk(path: Path) -> int | None:
        try:
            dev = path.stat().st_dev
        except OSError:
            return None
        if dev not in disks:
            usage = disk_usage(path)
            disks[dev] = [usage.total, usage.free]
            where[dev] = path
        return dev

    for root in roots:
        disk(root)

    def short_disks() -> set[int]:
        return {dev for dev, (total, free) in disks.items()
                if (space.min_free is not None and free < space.min_free)
                or (space.min_free_percent is not None
                    and free * 100 < space.min_free_percent * total)}

    def over() -> bool:
        return space.max_size is not None and used > space.max_size

    chosen = []
    for item in sorted(spare, key=lambda it: (it.made, it.order)):
        dev = disk(item.path)
        short = short_disks()
        if not over() and not short:
            break
        if not over() and dev not in short:
            continue  # on another disk: deleting it frees nothing that's short
        size = _disk_size(item.path)
        chosen.append(item)
        used -= size
        if dev is not None:
            disks[dev][1] += size
    shortfalls = []
    if over():
        shortfalls.append(f"state/ still uses {format_size(used)}, over max_size "
                          f"{format_size(space.max_size or 0)}")
    for dev in sorted(short_disks()):
        total, free = disks[dev]
        shortfalls.append(f"the disk holding {where[dev]} has {format_size(free)} free "
                          f"({free * 100 / total:.1f}%), under the space limits")
    return chosen, shortfalls


def prune_state(
    data_dir: Path, intent_dir: Path, runs_dir: Path, *, now: datetime | None = None,
    disk_usage: DiskUsage = shutil.disk_usage,
) -> PruneResult:
    """Apply the data repo's ``.retention.yaml`` to ``intent_dir`` and
    ``runs_dir``, and its ``space`` limits to them and the builder's log files
    too. A missing file is first created with ``DEFAULT_RETENTION``
    (``result.created``); a bad file raises RetentionError before anything is
    deleted."""
    now = now or datetime.now(UTC)
    result = PruneResult()
    retention = read_retention(data_dir)
    if retention is None:
        path = Path(data_dir) / RETENTION_FILENAME
        path.write_text(DEFAULT_RETENTION)
        result.created = path
        retention = parse_retention(DEFAULT_RETENTION, base=Path(data_dir).resolve().parent)
    state_dir = find_state_dir(data_dir, DEFAULT_STATE_DIR)
    builder_dir = state_dir / "builder"
    doomed, spare = _classify(retention, intent_dir, runs_dir, builder_dir, now)
    for item in doomed:
        _delete(item, result)
    if retention.space:
        extra, result.shortfalls = plan_space_prune(
            retention.space, spare, (state_dir, Path(runs_dir), Path(intent_dir)),
            disk_usage=disk_usage)
        for item in extra:
            _delete(item, result)
        result.for_space = len(extra)
    return result


def _delete(item: StateItem, result: PruneResult) -> None:
    if item.kind == "run":
        item.path.unlink(missing_ok=True)
        Path(f"{item.path}-journal").unlink(missing_ok=True)
        result.runs.append(item.path)
    elif item.kind == "log":
        item.path.unlink(missing_ok=True)
        result.logs.append(item.path)
    else:
        shutil.rmtree(item.path)
        _remove_if_empty(item.path.parent)  # the mode's folder
        _remove_if_empty(item.path.parent.parent)  # the scenario's
        (result.batches if item.kind == "batch" else result.intents).append(item.path)


def _classify(
    retention: Retention, intent_dir: Path, runs_dir: Path, builder_dir: Path | None,
    now: datetime,
) -> tuple[list[StateItem], list[StateItem]]:
    """``(doomed, spare)``: what the rules prune, and what only a breached
    space limit may (kept or not by the rules, but not protected). Log files
    are never doomed here, since the builder applies the ``logs`` rules; they
    are only ever spare, when ``builder_dir`` is given."""
    pinned_batches = set()
    items = [*_run_items(Path(runs_dir)), *_intent_items(Path(intent_dir))]
    for item in items:
        if item.kind != "run" and _is_pinned(item.path, retention.pins):
            pinned_batches.add((item.scenario, item.mode, item.path.name))
    if builder_dir is not None:
        items.extend(StateItem(path, "log", "", "", builder_log.file_end(path), (path.name, 1))
                     for path in builder_log.log_files(builder_dir))

    doomed: list[StateItem] = []
    spare: list[StateItem] = []
    groups: dict[tuple[str, str, str], list[StateItem]] = {}
    for item in items:
        groups.setdefault((item.kind, item.scenario, item.mode), []).append(item)
    for (kind, _scenario, _mode), group in sorted(groups.items()):
        section = {"intent": "intents", "log": "logs"}.get(kind, "runs")
        rules: tuple[Rule, ...] | None = getattr(retention, section)
        if rules is None:
            continue
        protected = retention.protected.get(section, ())
        group.sort(key=lambda it: (it.made, it.order), reverse=True)
        always = {0}
        if kind in ("run", "batch"):
            always.add(_newest_succeeded(group))
        for rank, item in enumerate(group):
            if (rank in always
                    or _active(item.path, now)
                    or _is_pinned(item.path, retention.pins)
                    or (kind in ("batch", "intent")
                        and (item.scenario, item.mode, item.path.name) in pinned_batches)
                    or any(rule.keeps(rank, item.made, now) for rule in protected)):
                continue
            if kind != "log" and not any(rule.keeps(rank, item.made, now) for rule in rules):
                doomed.append(item)
            else:
                spare.append(item)
    return doomed, spare


def _distinct_roots(paths: Iterable[Path]) -> list[Path]:
    """The existing folders among ``paths``, resolved, without one inside another
    (a symlinked state/runs is counted where it leads)."""
    resolved = sorted({Path(p).resolve() for p in paths if Path(p).is_dir()},
                      key=lambda p: len(p.parts))
    roots: list[Path] = []
    for path in resolved:
        if not any(path.is_relative_to(root) for root in roots):
            roots.append(path)
    return roots


def _disk_size(path: Path) -> int:
    """Disk space ``path`` takes, as ``du`` counts it: allocated blocks,
    symlinks not followed, each file once however many links it has."""
    seen: set[tuple[int, int]] = set()
    total = 0
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            st = os.lstat(current)
        except OSError:
            continue  # gone meanwhile
        if (st.st_dev, st.st_ino) in seen:
            continue
        seen.add((st.st_dev, st.st_ino))
        total += st.st_blocks * 512
        if os.path.isdir(current) and not os.path.islink(current):
            try:
                with os.scandir(current) as entries:
                    stack.extend(entry.path for entry in entries)
            except OSError:
                continue
    return total


def _run_items(runs_dir: Path) -> Iterable[StateItem]:
    if not runs_dir.is_dir():
        return
    for path in runs_dir.iterdir():
        if path.is_symlink():
            continue  # only what `sim run` wrote here, never what a link reaches
        match = _RUN_FILE.match(path.name)
        if match and path.is_file():
            yield StateItem(path, "run", match[3], "", _stamp(match[1]),
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
    for mode in BATCH_MODE_DIRS.values():
        folder = scenario_dir / mode
        if not folder.is_dir() or folder.is_symlink():
            continue
        for path in folder.iterdir():
            match = _BATCH_DIR.match(path.name)
            if match and path.is_dir() and not path.is_symlink():
                yield StateItem(path, kind, scenario_dir.name, mode, _stamp(match[1]),
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
