"""Structured logging for the analysis package.

Convention
----------
All domain-significant events are emitted via ``logging.getLogger(__name__)``
(or a child logger) with typed fields in the ``extra`` dict.  Every structured
call must include an ``"event"`` key naming the event type:

    logger.info("categorize.run.complete", extra={
        "event": "categorize.run.complete",
        "row_count": 42,
        "uncategorized_count": 3,
    })

Two handlers are configured by ``configure_logging``:

- StreamHandler  — human-readable for the terminal
- SQLiteEventHandler — structured event rows written to
                       ``<state_dir>/state.sqlite3`` (table ``events``)

Events are queryable directly with sqlite::

    import sqlite3
    conn = sqlite3.connect("state/state.sqlite3")
    runs = conn.execute(
        "select ts, event, message from events where event = ?",
        ("categorize.run.complete",),
    ).fetchall()

The file handler is the foundation for a future SQLiteHandler; switching
backends requires only replacing the storage handler, not the call sites.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from analysis.storage import JsonStateStore

# Standard LogRecord attributes that should not appear as extra fields in the
# JSON output.  Any key in __dict__ not in this set is treated as caller-
# supplied structured data.
_RECORD_BUILT_INS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname",
    "filename", "module", "exc_info", "exc_text", "stack_info",
    "lineno", "funcName", "created", "msecs", "relativeCreated",
    "thread", "threadName", "processName", "process", "message",
    "taskName",
})


class JsonLineFormatter(logging.Formatter):
    """Format each log record as a single JSON object on one line.

    Standard record fields (level, logger, message, timestamp) are always
    present.  Any keys added via ``extra={}`` appear as top-level fields.
    """

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        payload: dict = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.message,
        }
        for key, value in record.__dict__.items():
            if key not in _RECORD_BUILT_INS:
                payload[key] = value
        return json.dumps(payload, default=str)


class SQLiteEventHandler(logging.Handler):
    """Persist structured log records into the state SQLite events table."""

    def __init__(self, state_store: JsonStateStore) -> None:
        super().__init__()
        self._store = state_store

    def emit(self, record: logging.LogRecord) -> None:
        try:
            record.message = record.getMessage()
            timestamp = ""
            if self.formatter is not None:
                timestamp = self.formatter.formatTime(record, "%Y-%m-%dT%H:%M:%S")
            payload: dict = {
                "ts": timestamp,
                "level": record.levelname,
                "logger": record.name,
                "message": record.message,
            }
            for key, value in record.__dict__.items():
                if key not in _RECORD_BUILT_INS:
                    payload[key] = value
            self._store.insert_event(payload)
        except Exception:  # pragma: no cover
            self.handleError(record)


def configure_logging(
    state_dir: Path | str | None = None,
    level: int = logging.INFO,
) -> None:
    """Configure stream and optional file logging for the analysis package.

    Call once at application startup (e.g. in the CLI entry point).  Subsequent
    calls are no-ops.  When *state_dir* is provided, structured events are
    written to ``<state_dir>/state.sqlite3``.
    """
    logger = logging.getLogger("analysis")
    if logger.handlers:
        return
    logger.setLevel(level)
    logger.propagate = False

    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(stream)

    if state_dir is not None:
        state_store = JsonStateStore(state_dir)

        sqlite_handler = SQLiteEventHandler(state_store)
        sqlite_handler.setFormatter(JsonLineFormatter())
        logger.addHandler(sqlite_handler)
