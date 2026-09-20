"""Tests for analysis.sim.runs (ADR-002 Ledger layer)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from analysis.sim.loader import load_scenario
from analysis.sim.runs import finalize_run, open_run_file

REPO_ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = REPO_ROOT / "prototype_data"


def _load_prototype(tmp_path: Path) -> Path:
    result = load_scenario(
        PROTOTYPE,
        "retire_alice",
        intent_dir=tmp_path / "intent",
        loaded_by="test",
        allow_dirty_data=True,
    )
    return result.intent_path


def test_open_run_file_snapshots_intent_and_writes_simulation_run(tmp_path: Path) -> None:
    intent_path = _load_prototype(tmp_path)
    run_path = tmp_path / "runs" / "r.sqlite3"

    con = open_run_file(
        run_path, intent_path,
        run_by="test", allow_dirty_data=True,
        run_args={"scenario": "retire_alice"},
    )
    try:
        con.row_factory = sqlite3.Row
        run = con.execute("SELECT * FROM simulation_run").fetchone()
        assert run["scenario_name"] == "retire_alice"
        assert run["status"] == "running"
        assert run["allow_dirty_data"] == 1
        assert run["allow_dirty_code"] == 0
        assert run["finished_at"] is None
        assert run["run_args_json"] == '{"scenario": "retire_alice"}'
        assert run["engine_options_json"] == "{}"
        assert run["python_version"]
        assert run["host"]

        intent_con = sqlite3.connect(intent_path)
        intent_con.row_factory = sqlite3.Row
        try:
            m = intent_con.execute("SELECT * FROM intent_manifest").fetchone()
        finally:
            intent_con.close()
        assert run["data_version"] == m["data_version"]
        assert run["data_dirty"] == m["data_dirty"]
        assert run["scenario_tree_id"] == m["scenario_tree_id"]
        assert run["data_repo_path"] == m["data_repo_path"]
        assert run["data_repo_name"] == m["data_repo_name"]

        for tbl in ("person", "account", "account_model", "account_model_member",
                    "scheduled_transaction", "scenario"):
            src = sqlite3.connect(intent_path).execute(
                f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            dst = con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            assert src == dst, f"{tbl} row count mismatch"

        src_ids = [r[0] for r in sqlite3.connect(intent_path).execute(
            "SELECT id FROM account ORDER BY id").fetchall()]
        dst_ids = [r[0] for r in con.execute(
            "SELECT id FROM account ORDER BY id").fetchall()]
        assert src_ids == dst_ids

        nw = con.execute("SELECT COUNT(*) FROM net_worth").fetchone()[0]
        assert nw == 0
    finally:
        con.close()


def test_finalize_run_sets_terminal_status(tmp_path: Path) -> None:
    intent_path = _load_prototype(tmp_path)
    run_path = tmp_path / "runs" / "r.sqlite3"
    con = open_run_file(
        run_path, intent_path,
        run_by="test", allow_dirty_data=True,
    )
    try:
        finalize_run(con, status="succeeded")
        row = con.execute(
            "SELECT status, finished_at, failure_reason FROM simulation_run"
        ).fetchone()
        assert row[0] == "succeeded"
        assert row[1] is not None
        assert row[2] is None
    finally:
        con.close()


def test_finalize_run_records_failure_reason(tmp_path: Path) -> None:
    intent_path = _load_prototype(tmp_path)
    run_path = tmp_path / "runs" / "r.sqlite3"
    con = open_run_file(
        run_path, intent_path,
        run_by="test", allow_dirty_data=True,
    )
    try:
        finalize_run(con, status="failed", failure_reason="WaterfallExhausted")
        row = con.execute(
            "SELECT status, failure_reason FROM simulation_run"
        ).fetchone()
        assert row == ("failed", "WaterfallExhausted")
    finally:
        con.close()


def test_open_run_file_rejects_existing_path(tmp_path: Path) -> None:
    intent_path = _load_prototype(tmp_path)
    run_path = tmp_path / "r.sqlite3"
    run_path.write_bytes(b"")
    with pytest.raises(FileExistsError):
        open_run_file(
            run_path, intent_path,
            run_by="test", allow_dirty_data=True,
        )


def test_simulation_run_status_check_constraint(tmp_path: Path) -> None:
    intent_path = _load_prototype(tmp_path)
    run_path = tmp_path / "r.sqlite3"
    con = open_run_file(
        run_path, intent_path,
        run_by="test", allow_dirty_data=True,
    )
    try:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("UPDATE simulation_run SET status = 'bogus' WHERE id = 1")
    finally:
        con.close()
