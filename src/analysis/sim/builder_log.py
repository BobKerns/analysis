"""#314: the background builder's log, as records.

The daemon (``builder._Daemon``) writes every line itself: its events
unindented, as ``YYYY-MM-DD HH:MM:SS text``, and each build's output (the
child ``sim load``, read from a pipe) indented by :data:`INDENT`. A record
starts at an event line and runs to just before the next one. Anything else
unindented (a traceback on the daemon's own stderr) belongs to the record
it follows and is shown even without ``-v``.

A new file is started each day (local time, like the timestamps),
``state/builder/logs/builder-YYYY-MM-DD.log``, switched only at an event so
a record never straddles two files; read together, oldest first, they are
the log. The
``logs:`` section of ``.retention.yaml`` prunes old files (ADR-004 §10).
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, TYPE_CHECKING

if TYPE_CHECKING:
    from analysis.sim.retention import Rule

__all__ = [
    "FAILED_TAIL",
    "INDENT",
    "LogWriter",
    "Record",
    "failure_tails",
    "follow",
    "log_files",
    "plan_log_prune",
    "prune_logs",
    "read_records",
    "render",
]

INDENT = "  "
# Lines of a failed build's output shown under its failure without -v.
FAILED_TAIL = 10

_FILE = re.compile(r"^builder-(\d{4}-\d\d-\d\d)\.log$")
_STAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
_EVENT = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (.*)$")
# The daemon's own event texts, which the reader pairs up: a failure's
# output is the body of the `building X` record before it.
_BUILDING = re.compile(r"^building (\S+)$")
_FAILED = re.compile(r"^(\S+) (?:failed \(exit -?\d+\b|built incomplete\b)")


def file_for(log_dir: Path, now: datetime) -> Path:
    """The log file for ``now``'s (local) date."""
    return log_dir / f"builder-{now:%Y-%m-%d}.log"


def log_files(builder_dir: Path) -> list[Path]:
    """The log's daily files, oldest first."""
    log_dir = builder_dir / "logs"
    if not log_dir.is_dir():
        return []
    return sorted(p for p in log_dir.iterdir() if _FILE.match(p.name) and p.is_file())


class LogWriter:
    """Appends to the day's file. Events pick the file; output lines go to
    the file of the record they belong to. Safe across threads (the daemon's
    loop and its build-output reader)."""

    def __init__(self, log_dir: Path, on_new_day: Callable[[], None] | None = None) -> None:
        self.log_dir = log_dir
        self.on_new_day = on_new_day
        self._lock = threading.Lock()
        self._path: Path | None = None

    def event(self, text: str, now: datetime | None = None) -> str:
        """Write an event line (starting a record); returns it."""
        now = now or datetime.now()
        line = f"{now.strftime(_STAMP_FORMAT)} {text}"
        path = file_for(self.log_dir, now)
        with self._lock:
            new_day = self._path is not None and path != self._path
            self._path = path
            self._append(path, line)
        if new_day and self.on_new_day is not None:
            self.on_new_day()
        return line

    def output(self, line: str) -> None:
        """Write a line of a build's output, indented, into the current record."""
        with self._lock:
            path = self._path or file_for(self.log_dir, datetime.now())
            self._path = path
            self._append(path, INDENT + line)

    def _append(self, path: Path, line: str) -> None:
        # Opened for each line: a pruned or moved file is simply recreated.
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", errors="replace") as f:
            f.write(line.rstrip("\n") + "\n")

    def copy(self, stream: IO[bytes]) -> None:
        """Copy a build's output into the log until it closes (a thread's
        body). A carriage return redraws a terminal line, so only what follows
        the last one is kept."""
        for raw in iter(stream.readline, b""):
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n").rsplit("\r", 1)[-1]
            self.output(text)
        stream.close()


# -- reading -----------------------------------------------------------------


@dataclass
class Record:
    """One event line and the lines after it, up to the next event."""

    at: datetime | None  # lines before a file's first event: its last write
    header: str
    body: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        match = _EVENT.match(self.header)
        return match[2] if match else self.header

    def output(self) -> list[str]:
        """The build-output lines (the indented ones)."""
        return [ln for ln in self.body if _indented(ln)]

    def always(self) -> list[str]:
        """Body lines shown even without -v: unindented ones, which only the
        daemon's own stderr (a traceback) writes."""
        return [ln for ln in self.body if ln.strip() and not _indented(ln)]


def _indented(line: str) -> bool:
    return line[:1].isspace()


def _parse(lines: Iterable[str], written: datetime | None = None) -> Iterator[Record]:
    """Records of one file. Lines before its first event (a traceback the
    daemon wrote before it could log anything) are dated ``written``, the
    file's last write, so a time window still finds them."""
    record: Record | None = None
    for line in lines:
        line = line.rstrip("\n")
        match = _EVENT.match(line)
        if match:
            if record is not None:
                yield record
            record = Record(datetime.strptime(match[1], _STAMP_FORMAT), line)
        elif record is None:
            if line.strip():
                record = Record(written, line)
        else:
            record.body.append(line)
    if record is not None:
        yield record


def read_records(builder_dir: Path) -> list[Record]:
    """Every record of the log, oldest first."""
    out = []
    for path in log_files(builder_dir):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            written = datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            continue
        out.extend(_parse(text.splitlines(), written=written))
    return out


def failure_tails(records: list[Record], tail: int = FAILED_TAIL) -> dict[int, list[str]]:
    """For each failure record (by index), the last ``tail`` lines of the
    output of the build it reports: the ``building X`` record before it."""
    out: dict[int, list[str]] = {}
    last_output: dict[str, list[str]] = {}
    for i, record in enumerate(records):
        building = _BUILDING.match(record.text)
        if building:
            last_output[building[1]] = record.output()
            continue
        failed = _FAILED.match(record.text)
        if failed and last_output.get(failed[1]):
            out[i] = last_output[failed[1]][-tail:]
    return out


def render(record: Record, verbose: bool, tail: list[str] | None = None) -> list[str]:
    """A record's lines as `sim builder log` shows them."""
    if verbose:
        return [record.header, *record.body]
    lines = [record.header, *record.always()]
    if tail:
        lines.append(f"{INDENT}... last {len(tail)} line(s) of its output:")
        lines.extend(ln if _indented(ln) else INDENT + ln for ln in tail)
    return lines


def follow(
    builder_dir: Path, echo: Callable[[str], None], *, verbose: bool,
    stop: Callable[[], bool] = lambda: False, poll: float = 0.5,
) -> None:
    """Print lines as they are added to the log, across the daily switch,
    until ``stop()`` (or Ctrl-C). Starts at the current end."""
    files = log_files(builder_dir)
    current = files[-1] if files else None
    offset = current.stat().st_size if current is not None else 0
    pending = ""
    tails: dict[str, deque[str]] = {}
    building: str | None = None
    # A build already running when -f starts: its output so far counts
    # toward the tail shown if it fails.
    records = read_records(builder_dir)
    if records:
        started = _BUILDING.match(records[-1].text)
        if started:
            building = started[1]
            tails[building] = deque(records[-1].output(), maxlen=FAILED_TAIL)

    def show(line: str) -> None:
        nonlocal building
        match = _EVENT.match(line)
        if match is None:
            if _indented(line):
                if building is not None:
                    tails.setdefault(building, deque(maxlen=FAILED_TAIL)).append(line)
                if verbose:
                    echo(line)
            elif line.strip():
                echo(line)
            return
        echo(line)
        text = match[2]
        started = _BUILDING.match(text)
        building = started[1] if started else None
        if started:
            tails[started[1]] = deque(maxlen=FAILED_TAIL)
        failed = _FAILED.match(text)
        if failed and not verbose and tails.get(failed[1]):
            tail = list(tails.pop(failed[1]))
            echo(f"{INDENT}... last {len(tail)} line(s) of its output:")
            for ln in tail:
                echo(ln)

    while not stop():
        if current is not None:
            try:
                with open(current, encoding="utf-8", errors="replace") as f:
                    f.seek(offset)
                    chunk = f.read()
                    offset = f.tell()
            except FileNotFoundError:
                chunk = ""
            pending += chunk
            *complete, pending = pending.split("\n")
            for line in complete:
                show(line)
        files = log_files(builder_dir)
        newer = files[files.index(current) + 1:] if current in files else files
        if newer and newer[0] != current and not pending:
            current, offset = newer[0], 0
            continue  # read the new file at once
        time.sleep(poll)


# -- pruning -----------------------------------------------------------------


def plan_log_prune(
    builder_dir: Path, rules: tuple[Rule, ...] | None, *, now: datetime | None = None,
) -> list[Path]:
    """Log files the ``logs:`` rules would delete. Files are ranked newest
    first by the date in their name, and a file's age is from the end of
    that day (Bob: a late write to an older file is from a build that
    started that day, so it belongs to that day). The newest file is always
    kept."""
    if rules is None:
        return []
    now = now or datetime.now(UTC)
    files = list(reversed(log_files(builder_dir)))
    doomed = []
    for rank, path in enumerate(files):
        if rank == 0:
            continue
        if not any(rule.keeps(rank, _file_end(path), now) for rule in rules):
            doomed.append(path)
    return doomed


def _file_end(path: Path) -> datetime:
    """The latest a daily file's records can be from: the end of its (local) day."""
    match = _FILE.match(path.name)
    assert match is not None  # log_files lists only daily files
    return (datetime.strptime(match[1], "%Y-%m-%d") + timedelta(days=1)).astimezone(UTC)


def prune_logs(data_dir: Path, builder_dir: Path, *, now: datetime | None = None) -> list[Path]:
    """Apply ``.retention.yaml``'s ``logs:`` rules. A missing file or
    section keeps everything; a bad file raises ``RetentionError``."""
    from analysis.sim.retention import read_retention

    retention = read_retention(data_dir)
    if retention is None:
        return []
    doomed = plan_log_prune(builder_dir, retention.logs, now=now)
    for path in doomed:
        path.unlink(missing_ok=True)
    return doomed


def since_cutoff(since: timedelta, now: datetime | None = None) -> datetime:
    """The local naive time ``since`` ago, comparable with ``Record.at``."""
    return (now or datetime.now()) - since
