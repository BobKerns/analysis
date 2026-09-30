"""CLI tests for ``analysis sim``."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim.cli import DEFAULT_INTENT_DIR, DEFAULT_RUNS_DIR, _resolve_state_dir
from analysis.sim.connection import ScenarioConnection
from analysis.sim.loader import DEFAULT_STATE_DIR, load_scenario
from tests.conftest import data_dir

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
            "--no-run",
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


def test_sim_items_shows_account_events_and_originating_rule(tmp_path: Path) -> None:
    _, runfile = _seed_one_run(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "items", "--account", "Asset_Checking", "--start", "2026-05-31",
            "--end", "2026-05-31", "--runfile", str(runfile),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Asset_Checking" in result.output
    assert "salary_alice (fixed_amount)" in result.output
    assert "living (fixed_amount)" in result.output


def _copy_prototype(tmp_path: Path) -> Path:
    import shutil
    dest = tmp_path / "data"
    shutil.copytree(PROTOTYPE, dest)
    return dest


@pytest.mark.uses_current_scenario_write
def test_sim_load_records_current_scenario_and_defaults_to_it(
    tmp_path: Path, monkeypatch
) -> None:
    data = _copy_prototype(tmp_path)
    monkeypatch.setenv("ANALYSIS_DATA_DIR", str(data))
    intent_dir = tmp_path / "intent"
    runner = CliRunner()

    result = runner.invoke(
        main,
        ["sim", "load", "retire_alice", "--intent-dir", str(intent_dir),
         "--allow-dirty-data", "--no-run"],
    )
    assert result.exit_code == 0, result.output
    assert (data / ".current_scenario").read_text().strip() == "retire_alice"

    # No scenario argument: falls back to the recorded one.
    again = runner.invoke(
        main, ["sim", "load", "--intent-dir", str(intent_dir), "--allow-dirty-data", "--no-run"]
    )
    assert again.exit_code == 0, again.output
    assert "current scenario: retire_alice" in again.output

    check = runner.invoke(main, ["sim", "check"])
    assert check.exit_code == 0, check.output


def test_sim_load_runs_by_default(tmp_path: Path) -> None:
    intent_dir = tmp_path / "intent"
    runs_dir = tmp_path / "runs"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "load", "retire_alice",
            "--data-dir", str(PROTOTYPE),
            "--intent-dir", str(intent_dir),
            "--runs-dir", str(runs_dir),
            "--allow-dirty-data",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "loaded " in result.output
    assert "run " in result.output
    assert len(list(runs_dir.glob("*.sqlite3"))) == 1


def test_sim_load_no_run_skips_running(tmp_path: Path) -> None:
    intent_dir = tmp_path / "intent"
    runs_dir = tmp_path / "runs"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "load", "retire_alice",
            "--data-dir", str(PROTOTYPE),
            "--intent-dir", str(intent_dir),
            "--runs-dir", str(runs_dir),
            "--allow-dirty-data",
            "--no-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "run " not in result.output
    assert not runs_dir.exists()


def test_sim_all_loads_and_runs_every_scenario(tmp_path: Path) -> None:
    intent_dir = tmp_path / "intent"
    runs_dir = tmp_path / "runs"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "all",
            "--data-dir", str(PROTOTYPE),
            "--intent-dir", str(intent_dir),
            "--runs-dir", str(runs_dir),
            "--allow-dirty-data",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "=== retire_alice ===" in result.output
    assert "all 1 scenario(s) succeeded" in result.output
    assert len(list(runs_dir.glob("*.sqlite3"))) == 1


def test_sim_all_reports_failures_without_stopping(tmp_path: Path, monkeypatch) -> None:
    data = _copy_prototype(tmp_path)
    (data / "scenarios" / "broken").mkdir()  # empty scenario dir: fails to load
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "all",
            "--data-dir", str(data),
            "--intent-dir", str(tmp_path / "intent"),
            "--runs-dir", str(tmp_path / "runs"),
            "--allow-dirty-data",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "=== broken ===" in result.output
    assert "=== retire_alice ===" in result.output
    assert "1 of 2 scenario(s) failed:" in result.output
    assert "  broken:" in result.output
    assert len(list((tmp_path / "runs").glob("*.sqlite3"))) == 1


@pytest.mark.uses_current_scenario_write
def test_sim_scenario_create_derives_and_activates(tmp_path: Path, monkeypatch) -> None:
    data = _copy_prototype(tmp_path)
    monkeypatch.setenv("ANALYSIS_DATA_DIR", str(data))
    runner = CliRunner()
    result = runner.invoke(main, ["sim", "scenario", "create", "late", "retire_alice"])
    assert result.exit_code == 0, result.output

    scenario_dir = data / "scenarios" / "late"
    assert (data / ".current_scenario").read_text().strip() == "late"
    assert (scenario_dir / "chains").is_dir()
    assert (scenario_dir / "chains" / ".gitignore").read_text() == ""
    assert (scenario_dir / "chains" / "_asset__example.csv").read_text() == (
        "account_name,maximum,percent,series,cadence\n"
    )

    def header_of(name: str) -> list[str]:
        return (scenario_dir / name).read_text(encoding="utf-8").splitlines()

    scenario_rows = header_of("scenario.csv")
    assert scenario_rows[0].startswith("name,description,account_model")
    assert scenario_rows[1].startswith("late,Derived from retire_alice")
    assert "retire_alice" in scenario_rows[1]

    # Overlay files exist, header-only.
    for name in ("accounts.csv", "account_values.csv", "scheduled.csv", "calendar.csv"):
        lines = header_of(name)
        assert len(lines) == 1, f"{name} should be a header-only skeleton"
    assert header_of("accounts.csv")[0].split(",")[:3] == ["name", "kind", "subkind"]

    # Everything is inherited: the derived scenario loads with the parent's rows.
    parent = load_scenario(
        data, "retire_alice", intent_dir=tmp_path / "p", allow_dirty_data=True,
    )
    child = load_scenario(
        data, "late", intent_dir=tmp_path / "c", allow_dirty_data=True,
    )
    for table in ("account", "scheduled_transaction", "account_model_member"):
        with sqlite3.connect(parent.intent_path) as pcon, \
                sqlite3.connect(child.intent_path) as ccon:
            assert (
                ccon.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                == pcon.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )


def test_sim_scenario_create_rejects_bad_input(test_data_dir: Path, monkeypatch) -> None:
    monkeypatch.setenv("ANALYSIS_DATA_DIR", str(test_data_dir))
    runner = CliRunner()

    dup = runner.invoke(
        main, ["sim", "scenario", "create", "retire_alice", "retire_alice"]
    )
    assert dup.exit_code != 0
    assert "already exists" in dup.output

    unknown = runner.invoke(main, ["sim", "scenario", "create", "ok_name", "nope"])
    assert unknown.exit_code != 0
    assert "scenario not found" in unknown.output

    bad_name = runner.invoke(
        main, ["sim", "scenario", "create", "Has Spaces", "retire_alice"]
    )
    assert bad_name.exit_code != 0
    assert "invalid scenario name" in bad_name.output
    assert not (test_data_dir / "scenarios" / "Has Spaces").exists()


def test_sim_add_fixed_appends_calendar_row_and_check_passes(tmp_path: Path) -> None:
    data = _copy_prototype(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "transfer", "add",
            "2030-06-30", "Asset_Checking", "Asset_Brokerage_Joint", "1500.00",
            "--to-lot", "default",
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
            "sim", "scheduled", "add",
            "2030-01-31", "Asset_Checking", "Asset_Brokerage_Joint", "250.00",
            "--to-lot", "default",
            "--scenario", "retire_alice",
            "--data-dir", str(data),
            "--description", "Recurring sweep",
        ],
    )
    assert result.exit_code == 0, result.output

    sched = (data / "scenarios" / "retire_alice" / "scheduled.csv").read_text()
    last = sched.strip().splitlines()[-1]
    assert "xfer_asset_checking_to_asset_brokerage_joint_default_2030-01-31" in last
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
            "sim", "transfer", "add",
            "2030-06-30", "NopeAccount", "Asset_Brokerage_Joint", "100.00",
            "--to-lot", "default",
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
        ["sim", "transfer", "add", "--scenario", "retire_alice",
         "--data-dir", str(data)],
        input="2030-06-30\nAsset_Checking\nAsset_Brokerage_Joint\ndefault\n42.00\n",
    )
    assert result.exit_code == 0, result.output
    cal = (data / "scenarios" / "retire_alice" / "calendar.csv").read_text()
    assert "2030-06-30" in cal
    assert "4200" in cal  # 42.00 in cents


def test_sim_add_account_appends_accounts_and_model(tmp_path: Path) -> None:
    data = _copy_prototype(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "account", "add",
            "Asset_NewBrokerage", "asset", "brokerage_taxable",
            "--owner", "joint",
            "--opening-balance", "12345.67",
            "--scenario", "retire_alice",
            "--data-dir", str(data),
        ],
    )
    assert result.exit_code == 0, result.output


    accounts = (data / "scenarios" / "retire_alice" / "accounts.csv").read_text()
    assert "Asset_NewBrokerage,asset,brokerage_taxable,joint,taxable" in accounts

    check = runner.invoke(
        main, ["sim", "check", "retire_alice", "--data-dir", str(data)]
    )
    assert check.exit_code == 0, check.output


def test_sim_add_account_rejects_duplicate(tmp_path: Path) -> None:
    data = _copy_prototype(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "account", "add", "Asset_Checking", "asset", "checking",
            "--owner", "joint", "--data-dir", str(data),
        ],
    )
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_sim_account_edit_updates_owner_with_prompt_defaults(tmp_path: Path) -> None:
    import csv

    data = _copy_prototype(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "account", "edit",
            "Asset_Checking",
            "--owner", "alice",
            "--scenario", "retire_alice",
            "--data-dir", str(data),
        ],
    )
    assert result.exit_code == 0, result.output

    with (data / "scenarios" / "retire_alice" / "accounts.csv").open(
        newline="", encoding="utf-8"
    ) as fh:
        rows = {r["name"]: r for r in csv.DictReader(fh)}
    assert rows["Asset_Checking"]["owner"] == "alice"


def test_sim_account_rename_updates_references(tmp_path: Path) -> None:
    import csv

    data = _copy_prototype(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "account", "rename",
            "Asset_Checking",
            "Asset_Checking_Updated",
            "--scenario", "retire_alice",
            "--data-dir", str(data),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "renamed account 'Asset_Checking'" in result.output

    with (data / "scenarios" / "retire_alice" / "accounts.csv").open(
        newline="", encoding="utf-8"
    ) as fh:
        accounts = list(csv.DictReader(fh))
    renamed = {r["name"] for r in accounts}
    assert "Asset_Checking_Updated" in renamed
    assert "Asset_Checking" not in renamed

    with (data / "scenarios" / "retire_alice" / "account_values.csv").open(
        newline="", encoding="utf-8"
    ) as fh:
        values = list(csv.DictReader(fh))
    names = {r["account_name"] for r in values}
    assert "Asset_Checking_Updated" in names
    assert "Asset_Checking" not in names


def test_sim_account_delete_lists_references_and_removes_them(tmp_path: Path) -> None:
    import csv

    import pandas as pd

    data = _copy_prototype(tmp_path)
    accounts_path = data / "scenarios" / "retire_alice" / "accounts.csv"
    values_path = data / "scenarios" / "retire_alice" / "account_values.csv"
    values = pd.read_csv(values_path, dtype=str, keep_default_na=False)
    values.loc[values["account_name"] == "Asset_Savings", "refill_chain"] = "Asset_Checking"
    values.to_csv(values_path, index=False)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "account", "delete", "Asset_Checking",
            "--scenario", "retire_alice", "--data-dir", str(data),
        ],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    assert "account 'Asset_Checking' is referenced by:" in result.output
    assert "account_values.csv row 3: refill_chain" in result.output
    assert "account_values.csv row 2: account_name" in result.output
    assert "scheduled.csv row 2: to_account" in result.output
    assert "deleted account 'Asset_Checking'" in result.output

    with accounts_path.open(
        newline="", encoding="utf-8"
    ) as fh:
        accounts = list(csv.DictReader(fh))
    assert "Asset_Checking" not in {row["name"] for row in accounts}
    with values_path.open(newline="", encoding="utf-8") as fh:
        values = list(csv.DictReader(fh))
    assert next(row for row in values if row["account_name"] == "Asset_Savings")["refill_chain"] == ""

    check = runner.invoke(
        main, ["sim", "check", "retire_alice", "--data-dir", str(data)]
    )
    assert check.exit_code == 0, check.output


def test_sim_account_rename_updates_quicken_map(tmp_path: Path) -> None:
    import csv

    data = _copy_prototype(tmp_path)
    map_path = data / "scenarios" / "retire_alice" / "quicken_account_map.csv"
    map_path.write_text(
        "quicken_name,account_name,action,note\n"
        "Checking,Asset_Checking,map,\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "account", "rename",
            "Asset_Checking",
            "Asset_Checking_Updated",
            "--scenario", "retire_alice",
            "--data-dir", str(data),
        ],
    )
    assert result.exit_code == 0, result.output

    with map_path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["account_name"] == "Asset_Checking_Updated"

    with (data / "scenarios" / "retire_alice" / "scheduled.csv").open(
        newline="", encoding="utf-8"
    ) as fh:
        scheduled = list(csv.DictReader(fh))
    assert any((r["from_account"] == "Asset_Checking_Updated")
               or (r["to_account"] == "Asset_Checking_Updated")
                for r in scheduled)
    assert not any(r["from_account"] == "Asset_Checking" or r["to_account"] == "Asset_Checking" for r in scheduled)

    check = runner.invoke(
        main, ["sim", "check", "retire_alice", "--data-dir", str(data)]
    )
    assert check.exit_code == 0, check.output


def test_sim_accounts_lists_with_balance_and_rate(tmp_path: Path) -> None:
    data = _copy_prototype(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["sim", "account", "list", "--scenario", "retire_alice",
         "--data-dir", str(data), "--as-of", "2030-01-01"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Asset_Checking" in out
    assert "Asset_Brokerage_Joint" in out
    # opening balance for checking is 5000.00 in prototype
    assert "5,000.00" in out


def test_sim_account_list_sorts_and_filters(tmp_path: Path) -> None:
    import re

    data = _copy_prototype(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "account", "list",
            "--scenario", "retire_alice",
            "--data-dir", str(data),
            "--kind", "asset",
            "--owner", "joint",
        ],
        env={"COLUMNS": "300"},
    )
    assert result.exit_code == 0, result.output
    out = result.output
    names = re.findall(r"Asset_[A-Za-z0-9_]+", out)
    assert names
    assert names[0] == "Asset_Brokerage_Joint"
    assert "Asset_Checking" in names
    assert "Asset_Savings" in names
    assert "Income_Salary_Alice" not in out


def test_sim_transfer_list_shows_one_off(tmp_path: Path) -> None:
    data = _copy_prototype(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["sim", "transfer", "list", "--scenario", "retire_alice",
         "--data-dir", str(data)],
        env={"COLUMNS": "240"},
    )
    assert result.exit_code == 0, result.output
    out = result.output
    # one-off inheritance transfer from calendar.csv
    assert "Income_Inheritance" in out
    # scheduled transactions are NOT shown here
    assert "salary_alice" not in out


def test_sim_scheduled_list_shows_recurring(tmp_path: Path) -> None:
    data = _copy_prototype(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["sim", "scheduled", "list", "--scenario", "retire_alice",
         "--data-dir", str(data)],
        env={"COLUMNS": "240"},
    )
    assert result.exit_code == 0, result.output
    out = result.output
    assert "salary_alice" in out
    assert "monthly" in out



def test_sim_tag_add_rm_round_trip(tmp_path: Path) -> None:
    """tag add/rm mutates the CSV and load+tag list reflects it."""
    import pandas as pd

    data = _copy_prototype(tmp_path)
    runner = CliRunner()

    # Pick a known scheduled name from the prototype.
    sched_csv = data / "scenarios" / "retire_alice" / "scheduled.csv"
    df = pd.read_csv(sched_csv, dtype=str, keep_default_na=False)
    target = df.iloc[0]["name"]

    r = runner.invoke(
        main,
        ["sim", "scheduled", "tag", "add", target, "demo:test", "owner:alice",
         "--scenario", "retire_alice", "--data-dir", str(data)],
    )
    assert r.exit_code == 0, r.output
    assert "demo:test" in r.output and "owner:alice" in r.output

    df2 = pd.read_csv(sched_csv, dtype=str, keep_default_na=False)
    new_tags = df2.loc[df2["name"] == target, "tags"].iloc[0]
    assert "demo:test" in new_tags and "owner:alice" in new_tags

    # tags should now show both tags via the loader's intent DB.
    r = runner.invoke(
        main,
        ["sim", "tags", "--scenario", "retire_alice",
         "--data-dir", str(data)],
    )
    assert r.exit_code == 0, r.output
    assert "demo:test" in r.output
    assert "owner:alice" in r.output

    # scheduled tag rm strips one and leaves the other.
    r = runner.invoke(
        main,
        ["sim", "scheduled", "tag", "rm", target, "demo:test",
         "--scenario", "retire_alice", "--data-dir", str(data)],
    )
    assert r.exit_code == 0, r.output
    df3 = pd.read_csv(sched_csv, dtype=str, keep_default_na=False)
    final_tags = df3.loc[df3["name"] == target, "tags"].iloc[0]
    assert "demo:test" not in final_tags
    assert "owner:alice" in final_tags


def test_sim_tag_add_rejects_bad_token(tmp_path: Path) -> None:
    data = _copy_prototype(tmp_path)
    runner = CliRunner()
    r = runner.invoke(
        main,
        ["sim", "account", "tag", "add", "Asset_Checking", "no_colon",
         "--data-dir", str(data)],
    )
    assert r.exit_code != 0
    assert "invalid tag" in r.output


def test_sim_account_check_detects_missing_rows(tmp_path: Path) -> None:
    """account check reports accounts missing from account_values.csv."""

    data = _copy_prototype(tmp_path)
    accounts_path = data / "scenarios" / "retire_alice" / "accounts.csv"

    # Add a new account to accounts.csv using pandas to avoid line-ending issues
    import pandas as pd
    df = pd.read_csv(accounts_path, dtype=str, keep_default_na=False)
    new_row = pd.DataFrame([{
        "name": "Asset_NewAccount",
        "kind": "asset",
        "subkind": "cash",
        "owner": "joint",
        "tax_treatment": "taxable",
        "opened": "",
        "closed": "",
        "meta_json": "{}",
        "tags": "",
    }])
    df = pd.concat([df, new_row], ignore_index=True)
    df.to_csv(accounts_path, index=False)

    # Check should detect the missing row
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["sim", "account", "check", "--scenario", "retire_alice",
         "--data-dir", str(data)],
    )
    assert result.exit_code == 0
    assert "missing account_values rows for:" in result.output
    # Verify our new account is in the missing list
    assert "Asset_NewAccount" in result.output


def test_sim_account_sync_creates_missing_rows(tmp_path: Path) -> None:
    """account sync creates account_values rows for accounts missing them."""
    import pandas as pd

    data = _copy_prototype(tmp_path)
    accounts_path = data / "scenarios" / "retire_alice" / "accounts.csv"
    values_path = data / "scenarios" / "retire_alice" / "account_values.csv"

    # Add a new account to accounts.csv using pandas
    df = pd.read_csv(accounts_path, dtype=str, keep_default_na=False)
    new_row = pd.DataFrame([{
        "name": "Asset_NewAccount",
        "kind": "asset",
        "subkind": "brokerage_taxable",
        "owner": "joint",
        "tax_treatment": "taxable",
        "opened": "",
        "closed": "",
        "meta_json": "{}",
        "tags": "",
    }])
    df = pd.concat([df, new_row], ignore_index=True)
    df.to_csv(accounts_path, index=False)

    # Sync should create the missing rows
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["sim", "account", "sync", "--scenario", "retire_alice",
         "--data-dir", str(data)],
    )
    assert result.exit_code == 0, result.output
    assert "synced" in result.output and "missing account" in result.output
    assert "Asset_NewAccount" in result.output

    # Verify the row was added to account_values.csv
    import csv as csv_mod
    with values_path.open(newline="", encoding="utf-8") as f:
        reader = csv_mod.DictReader(f)
        rows = {r["account_name"]: r for r in reader}
    assert "Asset_NewAccount" in rows
    assert rows["Asset_NewAccount"]["opening_balance"] == "0.00"


def test_sim_account_sync_is_idempotent(tmp_path: Path) -> None:
    """account sync can be run multiple times safely."""
    data = _copy_prototype(tmp_path)
    runner = CliRunner()

    # First sync
    result1 = runner.invoke(
        main,
        ["sim", "account", "sync", "--scenario", "retire_alice",
         "--data-dir", str(data)],
    )
    assert result1.exit_code == 0

    # Second sync should say no missing accounts now
    result2 = runner.invoke(
        main,
        ["sim", "account", "sync", "--scenario", "retire_alice",
         "--data-dir", str(data)],
    )
    assert result2.exit_code == 0
    assert "no missing accounts" in result2.output


def _write_quicken_export(path: Path) -> None:
    path.write_text(
        '"",,"2026-08-18"\n'
        ',"Checking",\n'
        ',"  - Asset_Checking","1,234.56"\n'
        ',"  - Total Checking","1,234.56"\n'
        ',"Investments",\n'
        ',"  - Asset_Brokerage_Joint","310,000.00"\n'
        ',"  - Asset_TraditionalIRA_Alice","510,000.00"\n'
        '"Liabilities",,\n'
        ',"Loan",\n'
        ',"  - Liability_Mortgage","-240,000.00"\n'
        'Net Worth,,"0.00"\n',
        encoding="utf-8",
    )


def _write_quicken_map(path: Path) -> None:
    path.write_text(
        "quicken_name,account_name,action,note\n"
        "Asset_Checking,Asset_Checking,map,\n"
        "Asset_Brokerage_Joint,Asset_Brokerage_Joint,map,\n"
        "Asset_TraditionalIRA_Alice,Asset_TraditionalIRA_Alice,map,\n"
        "Liability_Mortgage,Liability_Mortgage,map,\n",
        encoding="utf-8",
    )


def test_sim_account_import_quicken_updates_balances(tmp_path: Path) -> None:
    data = _copy_prototype(tmp_path)
    export = tmp_path / "export.csv"
    _write_quicken_export(export)
    map_file = tmp_path / "map.csv"
    _write_quicken_map(map_file)

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "account", "import-quicken", str(export),
            "--map-file", str(map_file),
            "--scenario", "retire_alice",
            "--data-dir", str(data),
        ],
    )
    assert result.exit_code == 0, result.output

    import csv as _csv
    with (data / "scenarios" / "retire_alice" / "account_values.csv").open(
        newline="", encoding="utf-8"
    ) as f:
        rows = {r["account_name"]: r for r in _csv.DictReader(f)}
    assert rows["Asset_Checking"]["opening_balance"] == "1234.56"
    # non-investment asset: prior_year_end_balance is never touched
    assert rows["Asset_Checking"]["prior_year_end_balance"] == ""
    # investment asset with no existing prior_year_end_balance: seeded from opening
    assert rows["Asset_Brokerage_Joint"]["opening_balance"] == "310000.00"
    assert rows["Asset_Brokerage_Joint"]["prior_year_end_balance"] == "310000.00"
    # investment asset that already has a prior_year_end_balance: left alone
    assert rows["Asset_TraditionalIRA_Alice"]["opening_balance"] == "510000.00"
    assert rows["Asset_TraditionalIRA_Alice"]["prior_year_end_balance"] == "480000.00"
    # liability magnitude is stored positive even though Quicken shows it negative
    assert rows["Liability_Mortgage"]["opening_balance"] == "240000.00"
    assert rows["Liability_Mortgage"]["prior_year_end_balance"] == ""

    check = runner.invoke(
        main, ["sim", "check", "retire_alice", "--data-dir", str(data)]
    )
    assert check.exit_code == 0, check.output


def test_sim_account_import_quicken_year_end_sets_prior_year_only(tmp_path: Path) -> None:
    data = _copy_prototype(tmp_path)
    export = tmp_path / "year_end.csv"
    _write_quicken_export(export)
    map_file = tmp_path / "map.csv"
    _write_quicken_map(map_file)

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "account", "import-quicken", str(export),
            "--map-file", str(map_file),
            "--year-end",
            "--scenario", "retire_alice",
            "--data-dir", str(data),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "2 ineligible" in result.output  # Asset_Checking, Liability_Mortgage

    import csv as _csv
    with (data / "scenarios" / "retire_alice" / "account_values.csv").open(
        newline="", encoding="utf-8"
    ) as f:
        rows = {r["account_name"]: r for r in _csv.DictReader(f)}
    # opening_balance is untouched by --year-end
    assert rows["Asset_Brokerage_Joint"]["opening_balance"] == "300000.00"
    assert rows["Asset_Brokerage_Joint"]["prior_year_end_balance"] == "310000.00"
    # --year-end always overwrites, even if already present
    assert rows["Asset_TraditionalIRA_Alice"]["prior_year_end_balance"] == "510000.00"
    # non-investment accounts are left untouched entirely
    assert rows["Asset_Checking"]["opening_balance"] == "5000.00"
    assert rows["Liability_Mortgage"]["prior_year_end_balance"] == ""


def test_sim_account_import_quicken_rejects_unmapped_account(tmp_path: Path) -> None:
    data = _copy_prototype(tmp_path)
    export = tmp_path / "export.csv"
    _write_quicken_export(export)
    map_file = tmp_path / "map.csv"
    map_file.write_text(
        "quicken_name,account_name,action,note\n"
        "Asset_Checking,Asset_Checking,map,\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "account", "import-quicken", str(export),
            "--map-file", str(map_file),
            "--scenario", "retire_alice",
            "--data-dir", str(data),
        ],
    )
    assert result.exit_code != 0
    assert "unmapped" in result.output
    assert "Liability_Mortgage" in result.output


def test_sim_account_import_quicken_dry_run_does_not_write(tmp_path: Path) -> None:
    data = _copy_prototype(tmp_path)
    export = tmp_path / "export.csv"
    _write_quicken_export(export)
    map_file = tmp_path / "map.csv"
    _write_quicken_map(map_file)

    values_path = data / "scenarios" / "retire_alice" / "account_values.csv"
    before = values_path.read_text(encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "account", "import-quicken", str(export),
            "--map-file", str(map_file),
            "--scenario", "retire_alice",
            "--data-dir", str(data),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "dry run" in result.output
    assert values_path.read_text(encoding="utf-8") == before


def test_sim_load_puts_state_beside_the_data_dir(tmp_path: Path, test_data_dir: Path) -> None:
    result = CliRunner().invoke(
        main, ["sim", "load", "retire_alice", "--data-dir", str(test_data_dir),
               "--allow-dirty-data"],
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "state" / "intent" / "retire_alice.sqlite3").exists()
    assert len(list((tmp_path / "state" / "runs").glob("*.sqlite3"))) == 1
    assert not (tmp_path / "intent").exists()
    assert not (tmp_path / "runs").exists()


def test_sim_run_finds_state_through_the_activated_data_dir(
    tmp_path: Path, test_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    loaded = runner.invoke(
        main, ["sim", "load", "retire_alice", "--data-dir", str(test_data_dir),
               "--allow-dirty-data", "--no-run"],
    )
    assert loaded.exit_code == 0, loaded.output
    monkeypatch.setenv("ANALYSIS_DATA_DIR", str(test_data_dir))
    result = runner.invoke(main, ["sim", "run", "retire_alice"])
    assert result.exit_code == 0, result.output
    assert len(list((tmp_path / "state" / "runs").glob("*.sqlite3"))) == 1
    assert not (tmp_path / "runs").exists()


def test_transfer_list_shows_calendar_and_scheduled_one_offs(test_data_dir: Path) -> None:
    runner = CliRunner()
    added = runner.invoke(main, [
        "sim", "scheduled", "add", "2030-03-15", "Asset_Checking", "Asset_Savings", "75.00",
        "--cadence", "one_off", "--description", "Moving money", "--scenario", "retire_alice",
        "--data-dir", str(test_data_dir),
    ])
    assert added.exit_code == 0, added.output

    result = runner.invoke(main, [
        "sim", "transfer", "list", "--scenario", "retire_alice", "--data-dir", str(test_data_dir),
    ], env={"COLUMNS": "300"})
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if "│" in line]
    inheritance = next(line for line in lines if "inheritance" in line)
    moving = next(line for line in lines if "Moving money" in line)
    assert "calendar" in inheritance and "Asset_Brokerage_Joint (default)" in inheritance
    assert "scheduled" in moving and "Asset_Savings" in moving and "75.00" in moving
    assert lines.index(inheritance) < lines.index(moving)  # dated 2029 before 2030


def _append_calendar_vary_row(repo: Path, **fields: str) -> None:
    """ADR-006: append one calendar.csv row, addressing columns by name."""
    import pandas as pd

    from analysis.sim.registries import CALENDAR_HEADER

    cal = repo / "scenarios" / "retire_alice" / "calendar.csv"
    df = pd.read_csv(cal, dtype=str, keep_default_na=False)
    df = df.reindex(columns=CALENDAR_HEADER, fill_value="")
    df.loc[len(df)] = [fields.get(column, "") for column in CALENDAR_HEADER]
    df.to_csv(cal, index=False)


def test_sim_run_generates_and_runs_ensemble_for_a_recipe(tmp_path: Path) -> None:
    data = _copy_prototype(tmp_path)
    _append_calendar_vary_row(
        data, date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset Checking", amount="45000",
        vary_target="amount", vary_distribution="uniform",
        vary_low="40000", vary_high="50000",
    )
    intent_dir = tmp_path / "intent"
    runs_dir = tmp_path / "runs"
    runner = CliRunner()
    load_result = runner.invoke(
        main,
        [
            "sim", "load", "retire_alice",
            "--data-dir", str(data), "--intent-dir", str(intent_dir),
            "--allow-dirty-data", "--no-run",
        ],
    )
    assert load_result.exit_code == 0, load_result.output

    result = runner.invoke(
        main,
        [
            "sim", "run", "retire_alice",
            "--intent-dir", str(intent_dir), "--runs-dir", str(runs_dir),
            "--run-by", "tester", "--ensemble-size", "2", "--allow-dirty-data",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "ensemble retire_alice: 2/2 member(s) succeeded" in result.output

    member_intents = sorted((intent_dir / "retire_alice").glob("member_*.sqlite3"))
    member_runs = sorted((runs_dir / "retire_alice").glob("member_*.sqlite3"))
    assert [p.name for p in member_intents] == ["member_0000.sqlite3", "member_0001.sqlite3"]
    assert [p.name for p in member_runs] == ["member_0000.sqlite3", "member_0001.sqlite3"]

    con = sqlite3.connect(member_runs[0])
    try:
        status = con.execute("SELECT status FROM simulation_run").fetchone()[0]
        assert status == "succeeded"
        amount = con.execute(
            "SELECT amount FROM calendar_event WHERE name = 'checking_rate_vary'"
        ).fetchone()[0]
        assert 40_000 <= amount <= 50_000
        manifest = con.execute(
            "SELECT ensemble_name, member_index, member_count FROM ensemble_manifest"
        ).fetchone()
        assert manifest == ("retire_alice", 0, 2)
    finally:
        con.close()


def test_sim_run_ensemble_requires_size(tmp_path: Path) -> None:
    data = _copy_prototype(tmp_path)
    _append_calendar_vary_row(
        data, date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset Checking", amount="45000",
        vary_target="amount", vary_distribution="uniform",
        vary_low="40000", vary_high="50000",
    )
    intent_dir = tmp_path / "intent"
    runner = CliRunner()
    load_result = runner.invoke(
        main,
        [
            "sim", "load", "retire_alice",
            "--data-dir", str(data), "--intent-dir", str(intent_dir),
            "--allow-dirty-data", "--no-run",
        ],
    )
    assert load_result.exit_code == 0, load_result.output

    result = runner.invoke(
        main,
        [
            "sim", "run", "retire_alice",
            "--intent-dir", str(intent_dir), "--runs-dir", str(tmp_path / "runs"),
            "--allow-dirty-data",
        ],
    )
    assert result.exit_code != 0
    assert "ensemble recipe requires --ensemble-size" in result.output


def test_sim_run_ensemble_reports_member_failures_without_stopping(
    tmp_path: Path, monkeypatch,
) -> None:
    data = _copy_prototype(tmp_path)
    _append_calendar_vary_row(
        data, date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset Checking", amount="45000",
        vary_target="amount", vary_distribution="uniform",
        vary_low="40000", vary_high="50000",
    )
    intent_dir = tmp_path / "intent"
    runs_dir = tmp_path / "runs"
    runner = CliRunner()
    load_result = runner.invoke(
        main,
        [
            "sim", "load", "retire_alice",
            "--data-dir", str(data), "--intent-dir", str(intent_dir),
            "--allow-dirty-data", "--no-run",
        ],
    )
    assert load_result.exit_code == 0, load_result.output

    import analysis.sim.cli as sim_cli

    real_resolve_member = sim_cli.resolve_member

    def _flaky_resolve_member(*args, member_index, **kwargs):
        if member_index == 1:
            raise RuntimeError("boom")
        return real_resolve_member(*args, member_index=member_index, **kwargs)

    monkeypatch.setattr(sim_cli, "resolve_member", _flaky_resolve_member)

    result = runner.invoke(
        main,
        [
            "sim", "run", "retire_alice",
            "--intent-dir", str(intent_dir), "--runs-dir", str(runs_dir),
            "--ensemble-size", "2", "--allow-dirty-data",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "member 1: FAILED (boom)" in result.output
    assert "ensemble retire_alice: 1/2 member(s) succeeded" in result.output
    assert (intent_dir / "retire_alice" / "member_0000.sqlite3").exists()
    assert (runs_dir / "retire_alice" / "member_0000.sqlite3").exists()
    assert not (runs_dir / "retire_alice" / "member_0001.sqlite3").exists()


def test_sim_ensemble_summary_reports_mean_median_range(tmp_path: Path) -> None:
    data = _copy_prototype(tmp_path)
    _append_calendar_vary_row(
        data, date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset Checking", amount="45000",
        vary_target="amount", vary_distribution="uniform",
        vary_low="40000", vary_high="50000",
    )
    intent_dir = tmp_path / "intent"
    runs_dir = tmp_path / "runs"
    runner = CliRunner()
    load_result = runner.invoke(
        main,
        [
            "sim", "load", "retire_alice",
            "--data-dir", str(data), "--intent-dir", str(intent_dir),
            "--allow-dirty-data", "--no-run",
        ],
    )
    assert load_result.exit_code == 0, load_result.output

    run_result = runner.invoke(
        main,
        [
            "sim", "run", "retire_alice",
            "--intent-dir", str(intent_dir), "--runs-dir", str(runs_dir),
            "--ensemble-size", "3", "--allow-dirty-data",
        ],
    )
    assert run_result.exit_code == 0, run_result.output

    result = runner.invoke(
        main,
        [
            "sim", "ensemble", "summary", "retire_alice",
            "--runs-dir", str(runs_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "ensemble retire_alice: 3 member(s)" in result.output
    assert "mean=" in result.output and "median=" in result.output
    assert "min=" in result.output and "max=" in result.output
    assert "2026" in result.output and "2040" in result.output


def test_sim_ensemble_summary_errors_when_no_runs(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "ensemble", "summary", "retire_alice",
            "--runs-dir", str(tmp_path / "runs"),
        ],
    )
    assert result.exit_code != 0
    assert "no ensemble runs found" in result.output
