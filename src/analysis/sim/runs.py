"""Run file creation and lifecycle. Mirrors ADR-002 §Ledger layer DDL."""

from __future__ import annotations

import json
import socket
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from analysis.sim.provenance import code_stamps
from analysis.sim.schema import SCHEMA_VERSION, SNAPSHOT_TABLES, apply_run_schema

__all__ = ["open_run_file", "finalize_run"]


def open_run_file(
    run_path: Path,
    intent_path: Path,
    *,
    run_by: str,
    data_mode: str,
    allow_dirty_data: bool = False,
    allow_dirty_code: bool = False,
    run_args: dict[str, Any] | None = None,
    engine_options: dict[str, Any] | None = None,
) -> sqlite3.Connection:
    """Create a new run file at ``run_path`` from ``intent_path``.

    Applies the run schema, snapshots the intent tables (preserving ids),
    and writes the ``simulation_run`` row with ``status='running'`` and
    code/data provenance copied from the intent manifest plus a fresh
    ``code_stamps()`` call.
    """
    run_path = Path(run_path)
    intent_path = Path(intent_path).resolve()
    run_path.parent.mkdir(parents=True, exist_ok=True)
    if run_path.exists():
        raise FileExistsError(f"run file already exists: {run_path}")

    con = sqlite3.connect(run_path)
    con.execute("PRAGMA foreign_keys = ON")
    apply_run_schema(con)

    con.execute("ATTACH DATABASE ? AS intent", (str(intent_path),))
    try:
        for tbl in SNAPSHOT_TABLES:
            con.execute(f"INSERT INTO main.{tbl} SELECT * FROM intent.{tbl}")
        manifest = con.execute(
            "SELECT scenario_name, code_version, data_version, data_dirty, "
            "scenario_tree_id, data_repo_path, data_repo_name "
            "FROM intent.intent_manifest WHERE id = 1"
        ).fetchone()
        con.commit()
    finally:
        con.execute("DETACH DATABASE intent")

    if manifest is None:
        raise ValueError(f"intent file has no intent_manifest row: {intent_path}")

    (scenario_name, intent_code_version, data_version, data_dirty,
     scenario_tree_id, data_repo_path, data_repo_name) = manifest

    code = code_stamps()
    con.execute(
        "INSERT INTO simulation_run("
        "id, scenario_name, schema_version, status, started_at, "
        "code_version, code_dirty, code_repo_path, "
        "data_version, data_dirty, scenario_tree_id, "
        "data_repo_path, data_repo_name, data_mode, "
        "allow_dirty_data, allow_dirty_code, "
        "run_by, host, python_version, "
        "run_args_json, engine_options_json) "
        "VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            scenario_name, SCHEMA_VERSION, "running", _now_iso(),
            code.version, int(code.dirty), code.repo_path,
            data_version, int(data_dirty), scenario_tree_id,
            data_repo_path, data_repo_name, data_mode,
            int(allow_dirty_data), int(allow_dirty_code),
            run_by, socket.gethostname(), sys.version.split()[0],
            json.dumps(run_args or {}, sort_keys=True),
            json.dumps(engine_options or {}, sort_keys=True),
        ),
    )
    con.commit()
    _ = intent_code_version  # reserved: cross-check intent vs run code version later
    return con


def finalize_run(
    con: sqlite3.Connection,
    *,
    status: str,
    failure_reason: str | None = None,
) -> None:
    """Set terminal status on the single ``simulation_run`` row."""
    con.execute(
        "UPDATE simulation_run SET status = ?, finished_at = ?, failure_reason = ? "
        "WHERE id = 1",
        (status, _now_iso(), failure_reason),
    )
    con.commit()


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
