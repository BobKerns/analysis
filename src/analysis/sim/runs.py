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
from analysis.sim.schema import SCHEMA_VERSION, SNAPSHOT_TABLES, apply_run_schema, connect_sim_file

__all__ = ["open_run_file", "finalize_run"]


def open_run_file(
    run_path: Path,
    intent_path: Path,
    *,
    run_by: str,
    allow_dirty_data: bool = False,
    allow_dirty_code: bool = False,
    run_args: dict[str, Any] | None = None,
    engine_options: dict[str, Any] | None = None,
    reserved: bool = False,
) -> sqlite3.Connection:
    """Create a new run file at ``run_path`` from ``intent_path``.

    With ``reserved``, ``run_path`` is the empty file
    ``locks.reserve_run_path`` claimed for this run (#300); otherwise it
    must not exist yet.

    Applies the run schema, snapshots the intent tables (preserving ids),
    and writes the ``simulation_run`` row with ``status='running'`` and
    code/data provenance copied from the intent manifest plus a fresh
    ``code_stamps()`` call.
    """
    run_path = Path(run_path)
    intent_path = Path(intent_path).resolve()
    run_path.parent.mkdir(parents=True, exist_ok=True)
    if reserved:
        if not run_path.is_file() or run_path.stat().st_size:
            raise FileExistsError(f"not an empty reserved run file: {run_path}")
    elif run_path.exists():
        raise FileExistsError(f"run file already exists: {run_path}")

    con = connect_sim_file(run_path)
    con.execute("PRAGMA locking_mode = EXCLUSIVE")
    con.execute("PRAGMA foreign_keys = ON")
    apply_run_schema(con)

    con.execute("ATTACH DATABASE ? AS intent", (str(intent_path),))
    try:
        # member.refill_chain_id/overflow_chain_id and chain.lot_id form a
        # cycle (ADR-003 §4.7); defer FK checks to the transaction commit so
        # SNAPSHOT_TABLES order doesn't have to satisfy both directions.
        con.execute("PRAGMA defer_foreign_keys = ON")
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
    insert_run_metadata(
        con,
        scenario_name=scenario_name,
        status="running",
        scenario_tree_id=scenario_tree_id,
        data_repo_path=data_repo_path,
        data_repo_name=data_repo_name,
        allow_dirty_data=allow_dirty_data,
        allow_dirty_code=allow_dirty_code,
        run_by=run_by,
        version=code.version,
        data_version=data_version,
        data_dirty=data_dirty,
        run_args=run_args,
        engine_options=engine_options,
        # NOT NULL: blank when the code isn't running from a git checkout.
        code_repo_path=code.repo_path or "",
        code_dirty=code.dirty,
    )
    con.commit()
    _ = intent_code_version  # reserved: cross-check intent vs run code version later
    return con

def insert_run_metadata(
    con: sqlite3.Connection,
    *,
    scenario_name: str,
    status: str = "running",
    scenario_tree_id: str,
    data_repo_path: str,
    data_repo_name: str,
    allow_dirty_data: bool,
    allow_dirty_code: bool,
    run_by: str,
    version: str,
    code_repo_path: str,
    code_dirty: bool,
    data_version: str,
    data_dirty: bool,
    run_args: dict | None = None,
    engine_options: dict | None = None,
    finished_at: str | None = None,
) -> None:
    con.execute(
        "INSERT INTO simulation_run("
        "id, scenario_name, schema_version, status, started_at, "
        "code_version, code_dirty, code_repo_path, "
        "data_version, data_dirty, scenario_tree_id, "
        "data_repo_path, data_repo_name, "
        "allow_dirty_data, allow_dirty_code, "
        "run_by, host, python_version, "
        "run_args_json, engine_options_json, "
        "finished_at) "
        "VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            scenario_name, SCHEMA_VERSION, status, _now_iso(),
            version, int(code_dirty), code_repo_path,
            data_version, int(data_dirty), scenario_tree_id,
            data_repo_path, data_repo_name,
            int(allow_dirty_data), int(allow_dirty_code),
            run_by, socket.gethostname(), sys.version.split()[0],
            json.dumps(run_args or {}, sort_keys=True),
            json.dumps(engine_options or {}, sort_keys=True),
            finished_at,
        )
    )
    con.commit()


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
