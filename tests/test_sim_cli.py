"""CLI tests for ``analysis sim``."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from click.testing import CliRunner

from analysis.cli import main

REPO_ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = REPO_ROOT / "prototype_data"


def _load(tmp_path: Path) -> Path:
    intent_dir = tmp_path / "intent"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "load", "retire_alice",
            "--data-dir", str(PROTOTYPE),
            "--intent-dir", str(intent_dir),
            "--allow-dirty-data",
        ],
    )
    assert result.exit_code == 0, result.output
    return intent_dir


def test_sim_run_writes_named_run_file(tmp_path: Path) -> None:
    intent_dir = _load(tmp_path)
    runs_dir = tmp_path / "runs"

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "run", "retire_alice",
            "--intent-dir", str(intent_dir),
            "--runs-dir", str(runs_dir),
            "--data-mode", "prototype",
            "--run-by", "tester",
        ],
    )
    assert result.exit_code == 0, result.output

    runs = sorted(runs_dir.glob("*.sqlite3"))
    assert len(runs) == 1
    name = runs[0].name
    assert name.endswith("_retire_alice_" + name.split("_retire_alice_")[1])
    assert name.startswith("20")  # ISO year prefix
    assert "_retire_alice_" in name

    con = sqlite3.connect(runs[0])
    try:
        status, run_by = con.execute(
            "SELECT status, run_by FROM simulation_run"
        ).fetchone()
        assert status == "succeeded"
        assert run_by == "tester"
        steps = con.execute("SELECT COUNT(*) FROM simulation_step").fetchone()[0]
        assert steps == 176
    finally:
        con.close()


def test_sim_run_errors_when_intent_missing(tmp_path: Path) -> None:
    (tmp_path / "intent").mkdir()
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "run", "retire_alice",
            "--intent-dir", str(tmp_path / "intent"),
            "--runs-dir", str(tmp_path / "runs"),
            "--data-mode", "prototype",
        ],
    )
    assert result.exit_code != 0
    assert "intent file not found" in result.output


def _seed_one_run(tmp_path: Path) -> tuple[Path, Path]:
    intent_dir = _load(tmp_path)
    runs_dir = tmp_path / "runs"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "run", "retire_alice",
            "--intent-dir", str(intent_dir),
            "--runs-dir", str(runs_dir),
            "--data-mode", "prototype",
            "--run-by", "tester",
        ],
    )
    assert result.exit_code == 0, result.output
    runs = sorted(runs_dir.glob("*.sqlite3"))
    assert len(runs) == 1
    return runs_dir, runs[0]


def test_sim_list_empty_and_populated(tmp_path: Path) -> None:
    runner = CliRunner()
    empty = runner.invoke(main, ["sim", "list", "--runs-dir", str(tmp_path / "nope")])
    assert empty.exit_code == 0
    assert "no runs dir" in empty.output

    runs_dir, _ = _seed_one_run(tmp_path)
    result = runner.invoke(main, ["sim", "list", "--runs-dir", str(runs_dir)])
    assert result.exit_code == 0, result.output
    assert "succeeded" in result.output
    assert "retire_alice" in result.output
    assert "tester" in result.output


def test_sim_explain_dumps_provenance_and_yearly_nw(tmp_path: Path) -> None:
    _, runfile = _seed_one_run(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["sim", "explain", str(runfile)])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "scenario:       retire_alice" in out
    assert "status:         succeeded" in out
    assert "year-end net worth:" in out
    assert "  2026  " in out
    assert "  2040  " in out


def _copy_prototype(tmp_path: Path) -> Path:
    import shutil
    dest = tmp_path / "data"
    shutil.copytree(PROTOTYPE, dest)
    return dest


def test_sim_add_fixed_appends_calendar_row_and_check_passes(tmp_path: Path) -> None:
    data = _copy_prototype(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "add", "fixed",
            "2030-06-30", "Asset_Checking", "Asset_Brokerage_Joint", "1500.00",
            "--scenario", "retire_alice",
            "--data-dir", str(data),
            "--description", "Mid-year top-up",
        ],
    )
    assert result.exit_code == 0, result.output

    cal = (data / "scenarios" / "retire_alice" / "calendar.csv").read_text()
    last = cal.strip().splitlines()[-1]
    assert "2030-06-30" in last
    assert "one_off_transfer" in last
    assert "Asset_Checking" in last
    assert "Asset_Brokerage_Joint" in last
    assert "150000" in last  # cents

    check = runner.invoke(
        main, ["sim", "check", "retire_alice", "--data-dir", str(data)]
    )
    assert check.exit_code == 0, check.output
    assert "ok" in check.output


def test_sim_add_scheduled_appends_row_with_defaults_and_check_passes(
    tmp_path: Path,
) -> None:
    data = _copy_prototype(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "add", "scheduled",
            "2030-01-31", "Asset_Checking", "Asset_Brokerage_Joint", "250.00",
            "--scenario", "retire_alice",
            "--data-dir", str(data),
            "--description", "Recurring sweep",
        ],
    )
    assert result.exit_code == 0, result.output

    sched = (data / "scenarios" / "retire_alice" / "scheduled.csv").read_text()
    last = sched.strip().splitlines()[-1]
    assert "xfer_asset_checking_to_asset_brokerage_joint_2030-01-31" in last
    assert ",monthly," in last
    assert ",fixed_amount," in last
    assert ",250.00," in last
    assert ",2030-01-31," in last

    check = runner.invoke(
        main, ["sim", "check", "retire_alice", "--data-dir", str(data)]
    )
    assert check.exit_code == 0, check.output


def test_sim_add_fixed_rejects_unknown_account(tmp_path: Path) -> None:
    data = _copy_prototype(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "add", "fixed",
            "2030-06-30", "NopeAccount", "Asset_Brokerage_Joint", "100.00",
            "--scenario", "retire_alice",
            "--data-dir", str(data),
        ],
    )
    assert result.exit_code != 0
    assert "unknown from account" in result.output


def test_sim_add_fixed_prompts_when_args_missing(tmp_path: Path) -> None:
    data = _copy_prototype(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["sim", "add", "fixed", "--scenario", "retire_alice",
         "--data-dir", str(data)],
        input="2030-06-30\nAsset_Checking\nAsset_Brokerage_Joint\n42.00\n",
    )
    assert result.exit_code == 0, result.output
    cal = (data / "scenarios" / "retire_alice" / "calendar.csv").read_text()
    assert "2030-06-30" in cal
    assert "4200" in cal  # 42.00 in cents

