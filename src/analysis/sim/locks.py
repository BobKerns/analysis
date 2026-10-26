"""#300: keeping concurrent builds of a scenario apart.

Two things can collide when builds overlap -- a manual ``sim run`` beside
the background builder, or two terminals:

- **Run-file names.** A single run is named for the second it started in
  (ADR-004 §8), so two runs of one scenario in the same second want the same
  file. :func:`reserve_run_path` claims a name by exclusive create, adding
  ``-2``, ``-3``, ... to the timestamp until one is free, as
  ``cli._reserve_batch`` does for a batch folder.
- **The scenario's intent files.** A load rewrites
  ``state/intent/<scenario>.sqlite3`` (and its ``base_case.sqlite3``) in
  place. :func:`build_lock` holds an exclusive lock on the scenario for the
  load and run, so a second build of the same scenario waits; other
  scenarios build alongside it.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import socket
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

__all__ = [
    "BUILDER_CHILD_ENV",
    "BUILDER_SWITCH_ENV",
    "LockHolder",
    "RUN_STAMP_FORMAT",
    "build_lock",
    "building",
    "is_building",
    "lock_holder",
    "lock_path",
    "reserve_run_path",
    "run_name_key",
]

RUN_STAMP_FORMAT = "%Y-%m-%dT%H%M%SZ"
# Set (to 1) in a build the background builder started (sim/builder.py).
BUILDER_CHILD_ENV = "ANALYSIS_BUILDER_CHILD"
# "off" turns the background builder off: no daemon starts, nothing waits.
BUILDER_SWITCH_ENV = "ANALYSIS_BUILDER"
# A run file's leading `<stamp>[-N]_`, or a batch folder's whole `<stamp>[-N]`.
_RUN_STAMP = re.compile(r"^(\d{4}-\d\d-\d\dT\d{6}Z)(?:-(\d+))?(?:_|$)")
# Beside the intent files the lock protects. The dot keeps it apart from
# any scenario's own folder of member intents.
LOCK_DIR = ".locks"


def reserve_run_path(
    runs_dir: Path, scenario: str, version: str, *, now: datetime | None = None,
) -> Path:
    """Create and return an empty, unused run file
    ``<stamp>[-N]_<scenario>_<version>.sqlite3`` under ``runs_dir``.

    The name is claimed by exclusive create, never by an existence check
    first, so two processes can't both get it. Pass the result to
    ``run_scenario(..., reserved=True)``.
    """
    stamp = (now or datetime.now(UTC)).strftime(RUN_STAMP_FORMAT)
    runs_dir.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        prefix = stamp if n == 1 else f"{stamp}-{n}"
        path = runs_dir / f"{prefix}_{scenario}_{version}.sqlite3"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            n += 1
            continue
        os.close(fd)
        return path


def run_name_key(name: str) -> tuple[str, int, str]:
    """Sort key putting run files and batch folders in the order they were
    made: by stamp, then by the ``-N`` collision suffix, as a number. Plain
    name order gets the suffix wrong (``-`` sorts before ``_``, so ``…Z-2_x``
    would come before ``…Z_x``, and ``…Z-10`` before ``…Z-9``)."""
    match = _RUN_STAMP.match(name)
    if match is None:
        return (name, 0, name)
    return (match[1], int(match[2] or 1), name)


@dataclass(frozen=True)
class LockHolder:
    """Who holds a scenario's build lock, as they recorded it."""

    pid: int
    user: str
    host: str
    since: str
    by: str  # "manual" or "builder"

    def describe(self) -> str:
        who = "the background builder" if self.by == "builder" else self.user
        return f"{who} (pid {self.pid} on {self.host}, since {self.since})"


def lock_path(intent_dir: Path, scenario: str) -> Path:
    return Path(intent_dir) / LOCK_DIR / f"{scenario}.lock"


def lock_holder(intent_dir: Path, scenario: str) -> LockHolder | None:
    """The recorded holder of ``scenario``'s build lock, or None when it
    is free (or the record can't be read)."""
    path = lock_path(intent_dir, scenario)
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            pass  # held: the record is the holder's
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return None
        try:
            raw = json.loads(os.read(fd, 4096) or b"{}")
            return LockHolder(
                int(raw["pid"]), str(raw["user"]), str(raw["host"]),
                str(raw["since"]), str(raw.get("by", "manual")),
            )
        except (ValueError, KeyError, TypeError):
            return None
    finally:
        os.close(fd)


# Locks this process holds, by path: (fd, depth). flock locks belong to an
# open file, so a second open+flock of the same file in this process would
# wait on itself; `sim load` holds the lock and then invokes `sim run`.
_held: dict[Path, list[int]] = {}


@contextmanager
def build_lock(
    intent_dir: Path, scenario: str, *,
    on_wait: Callable[[LockHolder | None], None] | None = None,
) -> Generator[None]:
    """Hold ``scenario``'s exclusive build lock for the block, waiting for
    any other build of it to finish first (calling ``on_wait`` once with
    its holder before waiting). Reentrant within a process."""
    path = lock_path(intent_dir, scenario).resolve()
    held = _held.get(path)
    if held is not None:
        held[1] += 1
        try:
            yield
        finally:
            held[1] -= 1
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if on_wait is not None:
                on_wait(lock_holder(intent_dir, scenario))
            fcntl.flock(fd, fcntl.LOCK_EX)
        record = {
            "pid": os.getpid(),
            "user": os.environ.get("ANALYSIS_USER") or os.environ.get("USER") or "unknown",
            "host": socket.gethostname(),
            "since": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ"),
            "by": "builder" if os.environ.get(BUILDER_CHILD_ENV) else "manual",
        }
        os.ftruncate(fd, 0)
        os.pwrite(fd, json.dumps(record).encode(), 0)
        _held[path] = [fd, 1]
        try:
            yield
        finally:
            del _held[path]
            os.ftruncate(fd, 0)
    finally:
        os.close(fd)  # releases the lock



_building = 0


@contextmanager
def building() -> Generator[None]:
    """Mark this process as building (``sim load``, ``sim run``, ``sim
    all``) for the block. A build never waits on the background builder
    (``builder.await_fresh``): it may hold the very lock the builder's own
    build of that scenario is waiting for, and it is about to make the run
    a wait would wait for."""
    global _building
    _building += 1
    try:
        yield
    finally:
        _building -= 1


def is_building() -> bool:
    return _building > 0
