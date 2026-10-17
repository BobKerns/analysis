"""CLI tests for ``analysis sim``."""

from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.sim import registries
from analysis.sim.loader import load_scenario
from analysis.sim.scenario import SERIES_README

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


def test_sim_run_loads_first_when_no_prior_load(
    test_data_dir: Path, tmp_path: Path,
) -> None:
    """With a resolvable data repo, `run` builds the intent itself -- no
    separate `sim load` required, and no stale intent left over from one."""
    (tmp_path / "intent").mkdir()
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "run", "retire_alice",
            "--data-dir", str(test_data_dir),
            "--intent-dir", str(tmp_path / "intent"),
            "--runs-dir", str(tmp_path / "runs"),
            "--allow-dirty-data",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "intent" / "retire_alice.sqlite3").exists()
    assert len(list((tmp_path / "runs").glob("*.sqlite3"))) == 1


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


@pytest.mark.uses_current_scenario_write
def test_sim_load_records_current_scenario_and_defaults_to_it(
    test_data_dir: Path, tmp_path: Path, monkeypatch,
) -> None:
    data = test_data_dir
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
    assert "=== base ===" in result.output
    assert "=== retire_alice ===" in result.output
    assert "all 2 scenario(s) succeeded" in result.output
    assert len(list(runs_dir.glob("*.sqlite3"))) == 2


def test_sim_all_reports_failures_without_stopping(test_data_dir: Path, tmp_path: Path, monkeypatch) -> None:
    data = test_data_dir
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
    assert "1 of 3 scenario(s) failed:" in result.output
    assert "  broken:" in result.output
    assert len(list((tmp_path / "runs").glob("*.sqlite3"))) == 2


@pytest.mark.uses_current_scenario_write
def test_sim_scenario_create_derives_and_activates(test_data_dir: Path, tmp_path: Path, monkeypatch) -> None:
    data = test_data_dir
    monkeypatch.setenv("ANALYSIS_DATA_DIR", str(data))
    runner = CliRunner()
    result = runner.invoke(main, ["sim", "scenario", "create", "late", "retire_alice"])
    assert result.exit_code == 0, result.output

    scenario_dir = data / "scenarios" / "late"
    assert (data / ".current_scenario").read_text().strip() == "late"
    for role in ("source", "destination"):
        assert (scenario_dir / "chains" / role / ".gitignore").read_text() == ""
    assert (scenario_dir / "chains" / "source" / "_example.csv").read_text() == (
        "account_name,lot,maximum,percent,series,cadence\n"
    )
    # series/ starts with a README rather than empty, so git keeps it (#91).
    assert (scenario_dir / "series" / "README.md").read_text() == SERIES_README
    assert "  series/README.md\n" in result.output
    for listed in ("chains/source/.gitignore", "chains/source/_example.csv", "chains/destination/.gitignore"):
        assert f"  {listed}\n" in result.output

    def header_of(name: str) -> list[str]:
        return (scenario_dir / name).read_text(encoding="utf-8").splitlines()

    scenario_rows = header_of("scenario.csv")
    assert scenario_rows[0].startswith("name,description,start")
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
    for table in ("account", "scheduled_transaction", "account_lot"):
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


def test_sim_check_all_checks_every_scenario(test_data_dir: Path) -> None:
    result = CliRunner().invoke(
        main, ["sim", "check", "--all", "--data-dir", str(test_data_dir)]
    )
    assert result.exit_code == 0, result.output
    assert "Checking base\n" in result.output
    assert "Checking retire_alice\n" in result.output
    assert result.output.count("\nok\n") == 2
    assert "all 2 scenario(s) ok" in result.output


def test_sim_check_all_keeps_going_past_a_failure_and_exits_nonzero(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    # A scenario sorting after retire_alice proves the loop goes on past it.
    runner = CliRunner()
    create = runner.invoke(
        main, ["sim", "scenario", "create", "zz_after", "base",
               "--data-dir", str(test_data_dir)],
    )
    assert create.exit_code == 0, create.output
    with retire_alice_csv("scheduled.csv").open("a", encoding="utf-8") as f:
        f.write(
            "broken,Income_Nowhere,,Asset_Checking,,monthly,,fixed_amount,,,,,,"
            "1.00,2026-05-31,,ordinary,,2026-05-31,,200,,,,{},,\n"
        )
    result = runner.invoke(
        main, ["sim", "check", "--all", "--data-dir", str(test_data_dir)]
    )
    assert result.exit_code == 1, result.output
    assert "Income_Nowhere" in result.output
    # base and zz_after pass, before and after the failing retire_alice
    assert "Checking zz_after\n" in result.output
    assert result.output.count("\nok\n") == 2
    assert "1 of 3 scenario(s) failed: retire_alice" in result.output


def test_sim_check_rejects_a_scenario_name_with_all() -> None:
    # No data dir at all: the usage error comes before data-dir resolution.
    result = CliRunner().invoke(main, ["sim", "check", "retire_alice", "--all"])
    assert result.exit_code == 2
    assert "not both" in result.output


def test_sim_add_fixed_appends_calendar_row_and_check_passes(test_data_dir: Path) -> None:
    data = test_data_dir
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
    assert "1500.00" in last

    check = runner.invoke(
        main, ["sim", "check", "retire_alice", "--data-dir", str(data)]
    )
    assert check.exit_code == 0, check.output
    assert "ok" in check.output


def test_sim_add_scheduled_appends_row_with_defaults_and_check_passes(
    test_data_dir: Path,
) -> None:
    data = test_data_dir
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


def test_sim_add_fixed_rejects_unknown_account(test_data_dir: Path) -> None:
    data = test_data_dir
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


def test_sim_add_fixed_prompts_when_args_missing(test_data_dir: Path) -> None:
    data = test_data_dir
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
    assert ",42.00," in cal


def test_sim_add_account_appends_accounts_and_model(test_data_dir: Path) -> None:
    data = test_data_dir
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


def test_sim_add_account_rejects_duplicate(test_data_dir: Path) -> None:
    data = test_data_dir
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


def test_sim_account_edit_updates_owner_with_prompt_defaults(test_data_dir: Path) -> None:
    import csv

    data = test_data_dir
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



def _accounts(scenario_dir: Path) -> dict[str, dict[str, str]]:
    with (scenario_dir / "accounts.csv").open(newline="", encoding="utf-8") as fh:
        return {row["name"]: row for row in csv.DictReader(fh)}


def _account_cmd(data: Path, *args: str) -> Result:
    return CliRunner().invoke(
        main, ["sim", "account", *args, "--scenario", "child", "--data-dir", str(data)],
    )


def test_editing_an_inherited_account_writes_a_whole_row_override(
    test_data_dir: Path, test_child_scenario: Path,
) -> None:
    parent_dir = test_data_dir / "scenarios" / "retire_alice"
    parent = _accounts(parent_dir)

    result = _account_cmd(test_data_dir, "edit", "Asset_Checking", "--owner", "alice")

    assert result.exit_code == 0, result.output
    assert "wrote an override of account 'Asset_Checking' (inherited from 'retire_alice')" in result.output
    assert _accounts(test_child_scenario) == {
        "Asset_Checking": {**parent["Asset_Checking"], "owner": "alice"},
    }
    assert _accounts(parent_dir) == parent


def test_renaming_an_inherited_account_renames_it_where_it_is_defined(
    test_data_dir: Path, test_child_scenario: Path,
) -> None:
    result = _account_cmd(test_data_dir, "edit", "Asset_Checking", "--new-name", "Asset_Spending")

    assert result.exit_code == 0, result.output
    assert "where it is defined, in 'retire_alice'" in result.output
    assert _accounts(test_child_scenario) == {}
    assert "Asset_Spending" in _accounts(test_data_dir / "scenarios" / "retire_alice")


def test_deleting_an_inherited_account_is_refused_but_an_override_goes(
    test_data_dir: Path, test_child_scenario: Path,
) -> None:
    # A derived scenario may leave accounts.csv out entirely.
    (test_child_scenario / "accounts.csv").unlink()

    refused = _account_cmd(test_data_dir, "delete", "Asset_Checking")

    assert refused.exit_code != 0
    assert "delete it in 'retire_alice'" in refused.output
    assert "Asset_Checking" in _accounts(test_data_dir / "scenarios" / "retire_alice")

    assert _account_cmd(test_data_dir, "edit", "Asset_Checking", "--owner", "alice").exit_code == 0
    restored = _account_cmd(test_data_dir, "delete", "Asset_Checking")

    assert restored.exit_code == 0, restored.output
    assert "inherited from 'retire_alice' again" in restored.output
    assert _accounts(test_child_scenario) == {}


def test_sim_account_rename_updates_references(test_data_dir: Path) -> None:
    import csv

    data = test_data_dir
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
    assert "renamed account Asset_Checking to Asset_Checking_Updated" in result.output

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


def test_sim_account_delete_lists_references_and_removes_them(test_data_dir: Path) -> None:
    import csv

    import pandas as pd

    data = test_data_dir
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

    # calendar.csv's set_rate row for the account's own growth series has no
    # reader once the account is gone, so it is listed and deleted with it.
    assert "calendar.csv row 2: series (its growth rate)" in result.output
    calendar_path = data / "scenarios" / "retire_alice" / "calendar.csv"
    calendar = pd.read_csv(calendar_path, dtype=str, keep_default_na=False)
    assert "Asset_Checking" not in set(calendar["series"])
    assert len(calendar[calendar["kind"] != "latent_factor"]) == 4

    check = runner.invoke(
        main, ["sim", "check", "retire_alice", "--data-dir", str(data)]
    )
    assert check.exit_code == 0, check.output


def test_sim_account_rename_updates_quicken_map(test_data_dir: Path) -> None:
    import csv

    data = test_data_dir
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


def test_sim_accounts_lists_with_balance_and_rate(test_data_dir: Path) -> None:
    data = test_data_dir
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


def test_sim_account_list_sorts_and_filters(test_data_dir: Path) -> None:
    import re

    data = test_data_dir
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


def test_sim_transfer_list_shows_one_off(test_data_dir: Path) -> None:
    data = test_data_dir
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


def test_sim_scheduled_list_shows_recurring(test_data_dir: Path) -> None:
    data = test_data_dir
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



def test_sim_tag_add_rm_round_trip(test_data_dir: Path) -> None:
    """tag add/rm mutates the CSV and load+tag list reflects it."""
    import pandas as pd

    data = test_data_dir
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


def test_sim_tag_add_rejects_bad_token(test_data_dir: Path) -> None:
    data = test_data_dir
    runner = CliRunner()
    r = runner.invoke(
        main,
        ["sim", "account", "tag", "add", "Asset_Checking", "no_colon",
         "--data-dir", str(data)],
    )
    assert r.exit_code != 0
    assert "invalid tag" in r.output


def test_sim_account_check_detects_missing_rows(test_data_dir: Path) -> None:
    """account check reports accounts missing from account_values.csv."""

    data = test_data_dir
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


def test_sim_account_sync_creates_missing_rows(test_data_dir: Path) -> None:
    """account sync creates account_values rows for accounts missing them."""
    import pandas as pd

    data = test_data_dir
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


def test_sim_account_sync_is_idempotent(test_data_dir: Path) -> None:
    """account sync can be run multiple times safely."""
    data = test_data_dir
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


def test_sim_account_import_quicken_updates_balances(test_data_dir: Path, tmp_path: Path) -> None:
    data = test_data_dir
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


def test_sim_account_import_quicken_year_end_sets_prior_year_only(test_data_dir: Path, tmp_path: Path) -> None:
    data = test_data_dir
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


def test_sim_account_import_quicken_rejects_unmapped_account(test_data_dir: Path, tmp_path: Path) -> None:
    data = test_data_dir
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


def test_sim_account_import_quicken_dry_run_does_not_write(test_data_dir: Path, tmp_path: Path) -> None:
    data = test_data_dir
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
    # run now reloads from the data repo first (fast, and never misses a
    # CSV edit since the last explicit load), so it needs the same flag.
    result = runner.invoke(main, ["sim", "run", "retire_alice", "--allow-dirty-data"])
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


def _sim_load(
    test_data_dir: Path, intent_dir: Path, runner: CliRunner, *, scenario: str = "retire_alice",
):
    return runner.invoke(
        main,
        [
            "sim", "load", scenario,
            "--data-dir", str(test_data_dir), "--intent-dir", str(intent_dir),
            "--allow-dirty-data", "--no-run",
        ],
    )


def test_sim_run_generates_and_runs_ensemble_for_a_recipe(
    test_data_dir: Path, append_calendar_row: Callable[..., None],
    intent_dir: Path, runs_dir: Path,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="uniform",
        vary_low="4%", vary_high="5%",
    )
    runner = CliRunner()
    load_result = _sim_load(test_data_dir, intent_dir, runner)
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
    assert "2/2 member(s) succeeded" in result.output

    member_intents = sorted((intent_dir / "retire_alice").glob("*/member_*.sqlite3"))
    member_runs = sorted((runs_dir / "retire_alice").glob("*/member_*.sqlite3"))
    assert [p.name for p in member_intents] == ["member_0000.sqlite3", "member_0001.sqlite3"]
    assert [p.name for p in member_runs] == ["member_0000.sqlite3", "member_0001.sqlite3"]
    assert member_intents[0].parent == member_intents[1].parent  # same batch

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


def test_sim_run_without_a_size_runs_the_base_case(
    test_data_dir: Path, append_calendar_row: Callable[..., None],
    intent_dir: Path, runs_dir: Path,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="uniform",
        vary_low="4%", vary_high="5%",
    )
    runner = CliRunner()
    load_result = _sim_load(test_data_dir, intent_dir, runner)
    assert load_result.exit_code == 0, load_result.output

    result = runner.invoke(
        main,
        [
            "sim", "run", "retire_alice",
            "--intent-dir", str(intent_dir), "--runs-dir", str(runs_dir),
            "--allow-dirty-data",
        ],
    )
    # #169: no ensemble_size means not an ensemble, so it runs once.
    assert result.exit_code == 0, result.output
    assert "retire_alice is not an ensemble: running its base case" in result.output
    assert len(list(runs_dir.glob("*_retire_alice_*.sqlite3"))) == 1


def test_sim_run_ensemble_rejects_zero_ensemble_size(
    test_data_dir: Path, append_calendar_row: Callable[..., None],
    intent_dir: Path, runs_dir: Path,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="uniform",
        vary_low="4%", vary_high="5%",
    )
    runner = CliRunner()
    load_result = _sim_load(test_data_dir, intent_dir, runner)
    assert load_result.exit_code == 0, load_result.output

    result = runner.invoke(
        main,
        [
            "sim", "run", "retire_alice",
            "--intent-dir", str(intent_dir), "--runs-dir", str(runs_dir),
            "--ensemble-size", "0", "--allow-dirty-data",
        ],
    )
    assert result.exit_code != 0
    assert "--ensemble-size must be a positive integer" in result.output
    assert not (runs_dir / "retire_alice").exists()


def test_sim_load_rejects_a_negative_metadata_size(
    test_data_dir: Path, append_calendar_row: Callable[..., None],
    retire_alice_csv: Callable[[str], Path], intent_dir: Path,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="uniform",
        vary_low="4%", vary_high="5%",
    )
    scenario_csv = retire_alice_csv("scenario.csv")
    df = pd.read_csv(scenario_csv, dtype=str, keep_default_na=False)
    df.loc[0, "meta_json"] = json.dumps({"ensemble_size": -3})
    df.to_csv(scenario_csv, index=False)

    # #169: checked at load, before `sim run` ever reads it.
    load_result = _sim_load(test_data_dir, intent_dir, CliRunner())
    assert load_result.exit_code != 0
    assert "invalid ensemble_size -3" in load_result.output


def test_sim_ensemble_create_writes_the_series_readme(test_data_dir: Path) -> None:
    result = CliRunner().invoke(
        main,
        ["sim", "ensemble", "create", "my_recipe", "retire_alice",
         "--data-dir", str(test_data_dir), "--seed", "7"],
    )
    assert result.exit_code == 0, result.output
    readme = test_data_dir / "scenarios" / "my_recipe" / "series" / "README.md"
    assert readme.read_text() == SERIES_README


def test_sim_ensemble_create_rejects_zero_size(test_data_dir: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "ensemble", "create", "my_recipe", "retire_alice",
            "--data-dir", str(test_data_dir), "--size", "0",
        ],
    )
    assert result.exit_code != 0
    assert "--size must be a positive integer" in result.output
    assert not (test_data_dir / "scenarios" / "my_recipe").exists()


def test_sim_calendar_add_rejects_non_zero_padded_vary_date(
    test_data_dir: Path,
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "calendar", "add", "retire-scheduled", "2029-06-30", "salary_bill",
            "--data-dir", str(test_data_dir), "--scenario", "retire_alice",
            "--name", "retire_bill_vary", "--vary-target", "date",
            "--vary-distribution", "uniform",
            "--vary-low", "2029-1-1", "--vary-high", "2029-12-31",
        ],
    )
    assert result.exit_code != 0
    assert "date must be YYYY-MM or YYYY-MM-DD" in result.output


def test_sim_run_ensemble_reports_member_failures_without_stopping(
    test_data_dir: Path, append_calendar_row: Callable[..., None],
    intent_dir: Path, runs_dir: Path, monkeypatch,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="uniform",
        vary_low="4%", vary_high="5%",
    )
    runner = CliRunner()
    load_result = _sim_load(test_data_dir, intent_dir, runner)
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
    assert "1/2 member(s) succeeded" in result.output
    intent_batch = next((intent_dir / "retire_alice").glob("*/member_0000.sqlite3")).parent
    runs_batch = next((runs_dir / "retire_alice").glob("*/member_0000.sqlite3")).parent
    assert (intent_batch / "member_0000.sqlite3").exists()
    assert (runs_batch / "member_0000.sqlite3").exists()
    assert not (runs_batch / "member_0001.sqlite3").exists()

    # member 1 never got a run file at all -- only the batch manifest
    # records that it was expected and why it failed.
    from analysis.sim.ensemble import member_run_statuses, read_batch_manifest
    manifest = read_batch_manifest(runs_batch)
    assert manifest is not None
    assert manifest["member_count"] == 2
    assert manifest["failures"] == [
        {"index": 1, "intent_path": str(intent_batch / "member_0001.sqlite3"), "error": "boom"},
    ]
    assert member_run_statuses(runs_batch) == {
        "member_0000": "succeeded", "member_0001": "failed_to_resolve",
    }


def test_sim_ensemble_summary_prints_a_header_row_with_percentile_breakpoints(
    test_data_dir: Path, append_calendar_row: Callable[..., None],
    intent_dir: Path, runs_dir: Path,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="uniform",
        vary_low="4%", vary_high="5%",
    )
    runner = CliRunner()
    load_result = _sim_load(test_data_dir, intent_dir, runner)
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
    assert "ensemble retire_alice (" in result.output
    assert "3 succeeded member(s), 0 failed" in result.output
    lines = result.output.splitlines()
    assert lines[1].split() == [
        "year", "mean", "median", "std", "p05", "p20", "p40", "p60", "p80", "p95",
        "cvar05", "cvar20",
    ]
    years = [line.split()[0] for line in lines[2:]]
    assert years[0] == "2026" and "2040" in years
    # Right-aligned: every row ends where the header does.
    assert len({len(line) for line in lines[1:len(years) + 2]}) == 1


def test_sim_ensemble_summary_errors_when_no_runs(runs_dir: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["sim", "ensemble", "summary", "retire_alice", "--runs-dir", str(runs_dir)],
    )
    assert result.exit_code != 0


def test_sim_run_ensemble_batch_reservation_is_exclusive(
    test_data_dir: Path, append_calendar_row: Callable[..., None],
    intent_dir: Path, runs_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-existing directory at the stamp the CLI would naturally pick
    (standing in for a concurrent ``sim run`` that got there first, since
    true concurrency isn't practical to force deterministically in a unit
    test) must be skipped by advancing the suffix, exactly as if it had been
    detected mid-race -- the retry loop is the same whether the collision is
    pre-existing or won at the last instant by exclusive mkdir."""
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="uniform",
        vary_low="4%", vary_high="5%",
    )
    runner = CliRunner()
    load_result = _sim_load(test_data_dir, intent_dir, runner)
    assert load_result.exit_code == 0, load_result.output

    import analysis.sim.cli as sim_cli
    from datetime import UTC
    from datetime import datetime as real_datetime

    fixed_stamp = "2026-01-01T000000Z"

    class _FixedDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 1, 1, tzinfo=UTC)

    monkeypatch.setattr(sim_cli, "datetime", _FixedDatetime)
    (intent_dir / "retire_alice" / fixed_stamp).mkdir(parents=True)

    result = runner.invoke(
        main,
        [
            "sim", "run", "retire_alice",
            "--intent-dir", str(intent_dir), "--runs-dir", str(runs_dir),
            "--ensemble-size", "1", "--allow-dirty-data",
        ],
    )
    assert result.exit_code == 0, result.output
    batches = sorted(p.name for p in (runs_dir / "retire_alice").iterdir())
    assert batches == [f"{fixed_stamp}-2"]


def test_sim_run_ensemble_rerun_does_not_collide_with_prior_batch(
    test_data_dir: Path, append_calendar_row: Callable[..., None],
    intent_dir: Path, runs_dir: Path,
) -> None:
    """A recipe rerun (e.g. after tweaking a distribution) must not fail by
    colliding with the previous batch's member files."""
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="uniform",
        vary_low="4%", vary_high="5%",
    )
    runner = CliRunner()
    load_result = _sim_load(test_data_dir, intent_dir, runner)
    assert load_result.exit_code == 0, load_result.output

    run_args = [
        "sim", "run", "retire_alice",
        "--intent-dir", str(intent_dir), "--runs-dir", str(runs_dir),
        "--ensemble-size", "2", "--allow-dirty-data",
    ]
    first = runner.invoke(main, run_args)
    assert first.exit_code == 0, first.output
    second = runner.invoke(main, run_args)
    assert second.exit_code == 0, second.output
    assert "2/2 member(s) succeeded" in second.output

    batches = sorted((runs_dir / "retire_alice").iterdir())
    assert len(batches) == 2
    for batch in batches:
        assert sorted(p.name for p in batch.glob("member_*.sqlite3")) == [
            "member_0000.sqlite3", "member_0001.sqlite3",
        ]

    summary_result = runner.invoke(
        main,
        ["sim", "ensemble", "summary", "retire_alice", "--runs-dir", str(runs_dir)],
    )
    assert summary_result.exit_code == 0, summary_result.output
    assert batches[-1].name in summary_result.output  # defaults to the latest batch

    explicit = runner.invoke(
        main,
        [
            "sim", "ensemble", "summary", "retire_alice", "--runs-dir", str(runs_dir),
            "--batch", batches[0].name,
        ],
    )
    assert explicit.exit_code == 0, explicit.output
    assert batches[0].name in explicit.output


def test_sim_ensemble_create_sets_seed_and_size_and_loads_cleanly(test_data_dir: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "ensemble", "create", "my_recipe", "retire_alice",
            "--data-dir", str(test_data_dir), "--size", "5",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "inherits: retire_alice" in result.output
    assert "ensemble_size: 5" in result.output
    seed_line = next(line for line in result.output.splitlines() if "seed:" in line)
    seed = int(seed_line.split(":")[1].strip())

    scenario_csv = test_data_dir / "scenarios" / "my_recipe" / "scenario.csv"
    df = pd.read_csv(scenario_csv, dtype=str, keep_default_na=False)
    assert int(df.loc[0, "seed"]) == seed
    assert json.loads(df.loc[0, "meta_json"])["ensemble_size"] == 5


def test_sim_ensemble_create_defaults_seed_when_omitted(test_data_dir: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "ensemble", "create", "my_recipe", "retire_alice",
            "--data-dir", str(test_data_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "seed:" in result.output
    # --size omitted still records registries.DEFAULT_ENSEMBLE_SIZE, so
    # sim run/sim all never need --ensemble-size just to do *something*.
    assert f"ensemble_size: {registries.DEFAULT_ENSEMBLE_SIZE}" in result.output

    scenario_csv = test_data_dir / "scenarios" / "my_recipe" / "scenario.csv"
    df = pd.read_csv(scenario_csv, dtype=str, keep_default_na=False)
    assert df.loc[0, "seed"]  # some seed was picked
    meta = json.loads(df.loc[0, "meta_json"])
    assert meta["ensemble_size"] == registries.DEFAULT_ENSEMBLE_SIZE


def test_sim_ensemble_create_honors_explicit_seed(test_data_dir: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "ensemble", "create", "my_recipe", "retire_alice",
            "--data-dir", str(test_data_dir), "--seed", "12345",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "seed: 12345" in result.output


@pytest.mark.parametrize("typed", ["4.50", "4.50%"])
def test_sim_calendar_add_set_rate_writes_and_reports_the_canonical_percent(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path], typed: str,
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "calendar", "add", "set-rate", "2027-02-28", "Asset_Checking", typed,
            "--data-dir", str(test_data_dir), "--scenario", "retire_alice",
        ],
    )
    assert result.exit_code == 0, result.output
    df = pd.read_csv(retire_alice_csv("calendar.csv"), dtype=str, keep_default_na=False)
    row = df[df["series"] == "Asset_Checking"].iloc[-1]
    assert row["kind"] == "set_rate"
    assert row["amount"] == "4.5%"
    assert "-> 4.5% on 2027-02-28" in result.output


def test_sim_calendar_add_set_rate_with_vary_options(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "calendar", "add", "set-rate", "2027-02-28", "Asset_Checking", "4.5",
            "--data-dir", str(test_data_dir), "--scenario", "retire_alice",
            "--name", "checking_rate_vary", "--vary-target", "amount",
            "--vary-distribution", "uniform", "--vary-low", "4.0", "--vary-high", "5.0",
        ],
    )
    assert result.exit_code == 0, result.output
    df = pd.read_csv(retire_alice_csv("calendar.csv"), dtype=str, keep_default_na=False)
    row = df[df["name"] == "checking_rate_vary"].iloc[0]
    assert row["vary_target"] == "amount"
    assert row["vary_distribution"] == "uniform"
    assert row["vary_low"] == "4%"
    assert row["vary_high"] == "5%"


def test_sim_calendar_add_set_rate_defaults_to_rate_aux_kind(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    """The default --aux-kind='rate' writes no params_json at all -- the
    engine already defaults a set_rate event with none to 'rate' -- so an
    ordinary (non-inflation) rate row's CSV shape is unaffected by this
    option existing."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "calendar", "add", "set-rate", "2027-02-28", "Asset_Checking", "4.5",
            "--data-dir", str(test_data_dir), "--scenario", "retire_alice",
        ],
    )
    assert result.exit_code == 0, result.output
    df = pd.read_csv(retire_alice_csv("calendar.csv"), dtype=str, keep_default_na=False)
    row = df[df["series"] == "Asset_Checking"].iloc[-1]
    assert row["params_json"] == ""


def test_sim_calendar_add_set_rate_can_target_an_inflation_series(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    """A series like 'cpi' lives in the aux_table's 'inflation' kind, not the
    default 'rate' kind that an asset's own growth series uses -- --aux-kind
    is required to vary it at all (ADR-006 covers inflation/COLA per its own
    original motivation, not just asset growth rates)."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "calendar", "add", "set-rate", "2028-01-31", "cpi", "3.5",
            "--aux-kind", "inflation", "--data-dir", str(test_data_dir),
            "--scenario", "retire_alice", "--name", "cpi_vary", "--vary-target", "amount",
            "--vary-distribution", "uniform", "--vary-low", "2.0", "--vary-high", "4.0",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "aux_kind='inflation'" in result.output
    df = pd.read_csv(retire_alice_csv("calendar.csv"), dtype=str, keep_default_na=False)
    row = df[df["name"] == "cpi_vary"].iloc[0]
    assert row["kind"] == "set_rate"
    assert row["series"] == "cpi"
    assert json.loads(row["params_json"]) == {"aux_kind": "inflation"}
    assert row["vary_low"] == "2%" and row["vary_high"] == "4%"


def test_sim_calendar_add_set_rate_refuses_a_series_with_no_file(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    """(inflation, 'no_such_series') has no series file, so the row is an
    unresolved reference -- a typo -- and nothing is written (#204)."""
    path = retire_alice_csv("calendar.csv")
    result = CliRunner().invoke(main, [
        "sim", "calendar", "add", "set-rate", "2028-01-31", "no_such_series", "3.5",
        "--aux-kind", "inflation", "--data-dir", str(test_data_dir),
        "--scenario", "retire_alice",
    ])
    assert result.exit_code != 0
    assert "'no_such_series' under aux_kind 'inflation' doesn't exist" in result.output
    assert "no_such_series" not in path.read_text()


def test_only_check_warns_of_a_set_rate_series_nothing_reads(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    """Base has an apparel inflation series, but nothing in retire_alice
    reads it. `calendar add` writes the row without a word (a mix-in may set
    a series only the scenarios inheriting from it read, #92); only `sim
    check` warns (#204)."""
    path = retire_alice_csv("calendar.csv")
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "calendar", "add", "set-rate", "2028-01-31", "apparel", "3.5",
            "--aux-kind", "inflation", "--data-dir", str(test_data_dir),
            "--scenario", "retire_alice", "--name", "apparel_2028",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "WARN:" not in result.output
    assert "apparel_2028" in path.read_text()
    check = runner.invoke(main, ["sim", "check", "retire_alice", "--data-dir", str(test_data_dir)])
    assert check.exit_code == 0, check.output
    assert "WARN:" in check.output and "is never read" in check.output


def test_sim_run_ensemble_resolves_an_inflation_series_vary(
    test_data_dir: Path, intent_dir: Path, runs_dir: Path,
) -> None:
    """End to end: a varying cpi (inflation-kind) set_rate row must resolve
    to a literal, in-range value and run successfully -- not silently land
    in the wrong aux_table kind and have no effect."""
    runner = CliRunner()
    add_result = runner.invoke(
        main,
        [
            "sim", "calendar", "add", "set-rate", "2028-01-31", "cpi", "3.5",
            "--aux-kind", "inflation", "--data-dir", str(test_data_dir),
            "--scenario", "retire_alice", "--name", "cpi_vary", "--vary-target", "amount",
            "--vary-distribution", "uniform", "--vary-low", "2.0", "--vary-high", "4.0",
        ],
    )
    assert add_result.exit_code == 0, add_result.output

    load_result = _sim_load(test_data_dir, intent_dir, runner)
    assert load_result.exit_code == 0, load_result.output

    run_result = runner.invoke(
        main,
        [
            "sim", "run", "retire_alice",
            "--intent-dir", str(intent_dir), "--runs-dir", str(runs_dir),
            "--ensemble-size", "1", "--allow-dirty-data",
        ],
    )
    assert run_result.exit_code == 0, run_result.output

    member_intent = next((intent_dir / "retire_alice").glob("*/member_0000.sqlite3"))
    con = sqlite3.connect(member_intent)
    try:
        kind, amount, vary_distribution = con.execute(
            "SELECT kind, amount, vary_distribution FROM calendar_event WHERE name = 'cpi_vary'"
        ).fetchone()
        assert kind == "set_rate"
        assert 20_000 <= amount <= 40_000
        assert vary_distribution is None
    finally:
        con.close()


def test_sim_calendar_add_truncated_normal_accepts_low_and_high_alone(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    """vary_low/vary_high double as truncated_normal's own bounds (ADR-006):
    giving just those two infers vary_median as their midpoint and vary_sd as
    the spread that puts them 3 SDs out."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "calendar", "add", "set-rate", "2028-01-31", "cpi", "3.5",
            "--aux-kind", "inflation", "--data-dir", str(test_data_dir),
            "--scenario", "retire_alice", "--name", "cpi_vary", "--vary-target", "amount",
            "--vary-distribution", "truncated_normal", "--vary-low", "2.0", "--vary-high", "4.0",
        ],
    )
    assert result.exit_code == 0, result.output
    df = pd.read_csv(retire_alice_csv("calendar.csv"), dtype=str, keep_default_na=False)
    row = df[df["name"] == "cpi_vary"].iloc[0]
    assert row["vary_low"] == "2%" and row["vary_high"] == "4%"
    assert row["vary_median"] == "" and row["vary_sd"] == ""  # left for resolve_member to infer


def test_sim_calendar_add_rejects_clip_low_option_that_no_longer_exists(
    test_data_dir: Path,
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "calendar", "add", "set-rate", "2028-01-31", "cpi", "3.5",
            "--aux-kind", "inflation", "--data-dir", str(test_data_dir),
            "--scenario", "retire_alice", "--name", "cpi_vary", "--vary-target", "amount",
            "--vary-distribution", "truncated_normal", "--vary-median", "3.0", "--vary-sd", "0.5",
            "--vary-clip-low", "0",
        ],
    )
    assert result.exit_code != 0
    assert "--vary-clip-low" in result.output  # click: no such option


def test_sim_calendar_add_rejects_truncated_normal_with_insufficient_params(
    test_data_dir: Path,
) -> None:
    """--vary-median alone isn't enough to resolve a truncated_normal: sd needs
    either its own value or one of vary_low/vary_high to infer it from."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "calendar", "add", "set-rate", "2028-01-31", "cpi", "3.5",
            "--aux-kind", "inflation", "--data-dir", str(test_data_dir),
            "--scenario", "retire_alice", "--name", "cpi_vary", "--vary-target", "amount",
            "--vary-distribution", "truncated_normal", "--vary-median", "3.0",
        ],
    )
    assert result.exit_code != 0
    assert "vary_sd requires vary_low or vary_high" in result.output


def test_sim_calendar_add_set_amount_rejects_unknown_scheduled_name(
    test_data_dir: Path,
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "calendar", "add", "set-amount", "2028-01-31", "nonexistent_rule", "100.00",
            "--data-dir", str(test_data_dir), "--scenario", "retire_alice",
        ],
    )
    assert result.exit_code != 0
    assert "unknown scheduled_name" in result.output


def test_sim_calendar_add_retire_scheduled_rejects_amount_vary_target(
    test_data_dir: Path,
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "calendar", "add", "retire-scheduled", "2029-06-30", "salary_bill",
            "--data-dir", str(test_data_dir), "--scenario", "retire_alice",
            "--name", "bad", "--vary-target", "amount", "--vary-distribution", "uniform",
            "--vary-low", "1", "--vary-high", "2",
        ],
    )
    assert result.exit_code != 0
    assert "no amount column to vary" in result.output


def test_sim_calendar_add_retire_scheduled_with_date_vary(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "calendar", "add", "retire-scheduled", "2029-06-30", "salary_bill",
            "--data-dir", str(test_data_dir), "--scenario", "retire_alice",
            "--name", "retire_bill_vary", "--vary-target", "date",
            "--vary-distribution", "uniform",
            "--vary-low", "2029-01-01", "--vary-high", "2029-12-31",
        ],
    )
    assert result.exit_code == 0, result.output
    df = pd.read_csv(retire_alice_csv("calendar.csv"), dtype=str, keep_default_na=False)
    row = df[df["name"] == "retire_bill_vary"].iloc[0]
    assert row["kind"] == "retire_scheduled"
    assert row["vary_target"] == "date"
    assert row["vary_low"] == "2029-01-01"


def test_sim_calendar_add_transfer_matches_sim_transfer_add(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "sim", "calendar", "add", "transfer",
            "2030-06-30", "Asset_Checking", "Asset_Brokerage_Joint", "1500.00",
            "--to-lot", "default", "--data-dir", str(test_data_dir), "--scenario", "retire_alice",
        ],
    )
    assert result.exit_code == 0, result.output
    df = pd.read_csv(retire_alice_csv("calendar.csv"), dtype=str, keep_default_na=False)
    row = df.iloc[-1]
    assert row["kind"] == "one_off_transfer"
    assert row["amount"] == "1500.00"


def test_sim_calendar_add_transfer_vary_amount_is_not_shadowed_by_params_json(
    test_data_dir: Path, intent_dir: Path, runs_dir: Path,
) -> None:
    """A varying one_off_transfer's resolved amount must actually reach the
    posted transfer -- not get overwritten by a stale placeholder duplicated
    into payload_json (_build_cal_payload overlays payload_json onto the
    promoted `amount` column, so the two must never disagree)."""
    runner = CliRunner()
    add_result = runner.invoke(
        main,
        [
            "sim", "calendar", "add", "transfer",
            "2030-06-30", "Asset_Checking", "Asset_Brokerage_Joint", "1.00",
            "--to-lot", "default", "--data-dir", str(test_data_dir), "--scenario", "retire_alice",
            "--name", "transfer_vary", "--vary-target", "amount",
            "--vary-distribution", "uniform", "--vary-low", "1000.00", "--vary-high", "2000.00",
        ],
    )
    assert add_result.exit_code == 0, add_result.output

    load_result = _sim_load(test_data_dir, intent_dir, runner)
    assert load_result.exit_code == 0, load_result.output

    run_result = runner.invoke(
        main,
        [
            "sim", "run", "retire_alice",
            "--intent-dir", str(intent_dir), "--runs-dir", str(runs_dir),
            "--ensemble-size", "1", "--allow-dirty-data",
        ],
    )
    assert run_result.exit_code == 0, run_result.output

    member_run = next((runs_dir / "retire_alice").glob("*/member_0000.sqlite3"))
    con = sqlite3.connect(member_run)
    try:
        amount = con.execute(
            "SELECT te.amount FROM transfer_event te "
            "JOIN account a1 ON a1.id = te.from_account "
            "JOIN account a2 ON a2.id = te.to_account "
            "WHERE te.date = '2030-06-30' "
            "AND a1.name = 'Asset_Checking' AND a2.name = 'Asset_Brokerage_Joint'"
        ).fetchone()[0]
        assert 100_000 <= amount <= 200_000  # the drawn/resolved value, in cents
    finally:
        con.close()


# --- ADR-007 Phase 2: `sim run --sensitivity` -------------------------------


def _append_two_varying_rows(append_calendar_row: Callable[..., None]) -> None:
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="uniform",
        vary_low="4%", vary_high="5%",
    )
    append_calendar_row(
        date="2030-12-31", kind="retire_scheduled", name="retire_bill_vary",
        scheduled_name="salary_bill",
        vary_target="date", vary_distribution="uniform",
        vary_low="2030-06-30", vary_high="2031-06-30",
    )


def _sim_run_sensitivity(intent_dir: Path, runs_dir: Path, runner: CliRunner, *extra: str):
    return runner.invoke(
        main,
        [
            "sim", "run", "retire_alice", "--sensitivity",
            "--intent-dir", str(intent_dir), "--runs-dir", str(runs_dir),
            "--run-by", "tester", "--allow-dirty-data", *extra,
        ],
    )


def test_sim_run_sensitivity_generates_oat_batch(
    test_data_dir: Path, append_calendar_row: Callable[..., None],
    intent_dir: Path, runs_dir: Path,
) -> None:
    from analysis.sim.ensemble import read_batch_manifest

    _append_two_varying_rows(append_calendar_row)
    runner = CliRunner()
    assert _sim_load(test_data_dir, intent_dir, runner).exit_code == 0

    result = _sim_run_sensitivity(intent_dir, runs_dir, runner)
    assert result.exit_code == 0, result.output
    assert "5/5 member(s) succeeded" in result.output
    assert "checking_rate_vary@0.1" in result.output

    member_runs = sorted((runs_dir / "retire_alice").glob("*/member_*.sqlite3"))
    assert len(member_runs) == 5
    manifest = read_batch_manifest(member_runs[0].parent)
    assert manifest is not None
    assert manifest["mode"] == "sensitivity"
    assert manifest["quantiles"] == [0.1, 0.9]
    assert manifest["rows"] == ["checking_rate_vary", "retire_bill_vary"]

    con = sqlite3.connect(member_runs[2])  # checking_rate_vary @ 0.9
    try:
        mode, seed = con.execute("SELECT mode, seed FROM ensemble_manifest").fetchone()
        assert mode == "sensitivity"
        assert seed is None  # draws nothing, so records no seed (PR #23 review)
        assert con.execute(
            "SELECT amount FROM calendar_event WHERE name = 'checking_rate_vary'"
        ).fetchone()[0] == 49_000
    finally:
        con.close()


def test_sim_run_sensitivity_only_and_quantiles_set_member_count(
    test_data_dir: Path, append_calendar_row: Callable[..., None],
    intent_dir: Path, runs_dir: Path,
) -> None:
    _append_two_varying_rows(append_calendar_row)
    runner = CliRunner()
    assert _sim_load(test_data_dir, intent_dir, runner).exit_code == 0

    result = _sim_run_sensitivity(
        intent_dir, runs_dir, runner,
        "--only", "retire_bill_vary", "--quantiles", "0.1,0.25,0.75,0.9",
    )
    assert result.exit_code == 0, result.output
    assert "5/5 member(s) succeeded" in result.output  # 1 + 1 row x 4 quantiles


@pytest.mark.parametrize(("extra", "message"), [
    (("--ensemble-size", "3"), "exclusive"),
    (("--quantiles", "0.9,0.1"), "strictly increasing"),
    (("--only", "nope"), "nope"),
])
def test_sim_run_sensitivity_rejects_bad_options(
    test_data_dir: Path, append_calendar_row: Callable[..., None],
    intent_dir: Path, runs_dir: Path, extra: tuple[str, ...], message: str,
) -> None:
    _append_two_varying_rows(append_calendar_row)
    runner = CliRunner()
    assert _sim_load(test_data_dir, intent_dir, runner).exit_code == 0
    result = _sim_run_sensitivity(intent_dir, runs_dir, runner, *extra)
    assert result.exit_code != 0
    assert message in result.output


def test_sim_run_only_requires_sensitivity(intent_dir: Path, runs_dir: Path) -> None:
    result = CliRunner().invoke(
        main,
        ["sim", "run", "retire_alice", "--intent-dir", str(intent_dir),
         "--runs-dir", str(runs_dir), "--only", "x"],
    )
    assert result.exit_code != 0
    assert "require --sensitivity" in result.output


def test_sim_run_sensitivity_with_nothing_to_perturb(
    test_data_dir: Path, intent_dir: Path, runs_dir: Path,
) -> None:
    runner = CliRunner()
    assert _sim_load(test_data_dir, intent_dir, runner).exit_code == 0
    result = _sim_run_sensitivity(intent_dir, runs_dir, runner)
    assert result.exit_code != 0
    assert "nothing to perturb" in result.output


def test_sim_ensemble_summary_skips_sensitivity_batches(
    test_data_dir: Path, append_calendar_row: Callable[..., None],
    intent_dir: Path, runs_dir: Path,
) -> None:
    _append_two_varying_rows(append_calendar_row)
    runner = CliRunner()
    assert _sim_load(test_data_dir, intent_dir, runner).exit_code == 0
    mc = runner.invoke(main, [
        "sim", "run", "retire_alice", "--intent-dir", str(intent_dir),
        "--runs-dir", str(runs_dir), "--ensemble-size", "2", "--allow-dirty-data",
    ])
    assert mc.exit_code == 0, mc.output
    oat = _sim_run_sensitivity(intent_dir, runs_dir, runner, "--only", "checking_rate_vary")
    assert oat.exit_code == 0, oat.output

    batches = sorted(p for p in (runs_dir / "retire_alice").iterdir() if p.is_dir())
    assert len(batches) == 2
    result = runner.invoke(
        main, ["sim", "ensemble", "summary", "retire_alice", "--runs-dir", str(runs_dir)],
    )
    assert result.exit_code == 0, result.output
    assert f"({batches[0].name})" in result.output  # the Monte Carlo batch, not the newer OAT one

    explicit = runner.invoke(main, [
        "sim", "ensemble", "summary", "retire_alice", "--runs-dir", str(runs_dir),
        "--batch", batches[1].name,
    ])
    assert explicit.exit_code != 0
    assert "sensitivity batch" in explicit.output


def test_sim_run_sensitivity_reports_member_failures_without_stopping(
    test_data_dir: Path, append_calendar_row: Callable[..., None],
    intent_dir: Path, runs_dir: Path, monkeypatch,
) -> None:
    _append_two_varying_rows(append_calendar_row)
    runner = CliRunner()
    assert _sim_load(test_data_dir, intent_dir, runner).exit_code == 0

    import analysis.sim.cli as sim_cli

    real_resolve_member = sim_cli.resolve_member

    def _flaky_resolve_member(*args, member_index, **kwargs):
        if member_index == 2:
            raise RuntimeError("boom")
        return real_resolve_member(*args, member_index=member_index, **kwargs)

    monkeypatch.setattr(sim_cli, "resolve_member", _flaky_resolve_member)
    result = _sim_run_sensitivity(intent_dir, runs_dir, runner)
    assert result.exit_code == 0, result.output
    assert "member 2: FAILED (boom)" in result.output
    assert "4/5 member(s) succeeded" in result.output


# --- ADR-007 Phase 7: parallel members (--jobs) --------------------------------


def _batch_results(runs_dir: Path) -> dict[str, tuple[list, list]]:
    """Per member: its draws and its monthly net worth -- what must match
    between sequential and parallel runs (file bytes won't: timestamps)."""
    batch = max(p for p in (runs_dir / "retire_alice").iterdir() if p.is_dir())
    results = {}
    for path in sorted(batch.glob("member_*.sqlite3")):
        con = sqlite3.connect(path)
        try:
            draws = con.execute(
                "SELECT calendar_event_name, drawn_value, quantile FROM ensemble_draw ORDER BY 1"
            ).fetchall()
            net_worth = con.execute("SELECT month, net_worth FROM net_worth ORDER BY month").fetchall()
        finally:
            con.close()
        results[path.stem] = (draws, net_worth)
    return results


@pytest.mark.slow
@pytest.mark.parametrize("mode_args", [("--ensemble-size", "3"), ("--sensitivity",)])
def test_sim_run_jobs_matches_sequential(
    test_data_dir: Path, append_calendar_row: Callable[..., None],
    intent_dir: Path, tmp_path: Path, mode_args: tuple[str, ...],
) -> None:
    _append_two_varying_rows(append_calendar_row)
    runner = CliRunner()
    assert _sim_load(test_data_dir, intent_dir, runner).exit_code == 0
    con = sqlite3.connect(intent_dir / "retire_alice.sqlite3")
    con.execute("UPDATE scenario SET seed = 11")
    con.commit()
    con.close()
    results = {}
    for jobs in ("1", "2"):
        runs = tmp_path / f"runs_{jobs}"
        result = runner.invoke(main, [
            "sim", "run", "retire_alice", "--intent-dir", str(intent_dir),
            "--runs-dir", str(runs), "--allow-dirty-data", "--allow-dirty-code",
            "--jobs", jobs, *mode_args,
        ])
        assert result.exit_code == 0, result.output
        assert "FAILED" not in result.output
        results[jobs] = _batch_results(runs)
    assert results["1"] == results["2"]
    assert len(results["2"]) == (3 if "--ensemble-size" in mode_args else 5)


@pytest.mark.slow
def test_sim_run_jobs_reports_failures_without_taking_down_the_pool(
    test_data_dir: Path, append_calendar_row: Callable[..., None],
    update_account_value: Callable[..., None], retire_alice_csv: Callable[[str], Path],
    intent_dir: Path, runs_dir: Path,
) -> None:
    """A ramp on a series with no baseline rate fails in resolve_member for
    every member -- in worker processes, each is still reported."""
    # The series' file (#204) starts after the ramp, so there's nothing to
    # ramp from.
    retire_alice_csv("series/rate__never_before_seen_series.csv").write_text(
        "effective_date,annual_rate\n2035-01-31,3%\n"
    )
    update_account_value("Asset_Brokerage_Joint", "default", series="never_before_seen_series")
    append_calendar_row(
        date="2027-01-31", kind="set_rate", name="no_baseline_ramp",
        series="never_before_seen_series", amount="4.5%",
        vary_target="amount", vary_distribution="uniform",
        vary_low="4%", vary_high="5%", vary_ramp_months="3",
    )
    runner = CliRunner()
    assert _sim_load(test_data_dir, intent_dir, runner).exit_code == 0
    result = runner.invoke(main, [
        "sim", "run", "retire_alice", "--sensitivity", "--intent-dir", str(intent_dir),
        "--runs-dir", str(runs_dir), "--allow-dirty-data", "--allow-dirty-code", "--jobs", "2",
    ])
    assert result.exit_code == 0, result.output
    assert "0/3 member(s) succeeded" in result.output
    assert result.output.count("FAILED") == 3


def test_sim_run_rejects_negative_jobs(intent_dir: Path, runs_dir: Path) -> None:
    result = CliRunner().invoke(main, [
        "sim", "run", "retire_alice", "--intent-dir", str(intent_dir),
        "--runs-dir", str(runs_dir), "--jobs", "-1",
    ])
    assert result.exit_code != 0
    assert "--jobs" in result.output


def test_sensitivity_manifest_is_written_before_members_run(
    test_data_dir: Path, append_calendar_row: Callable[..., None],
    intent_dir: Path, runs_dir: Path, monkeypatch,
) -> None:
    """An interrupted OAT batch must still read as a sensitivity batch (PR #25
    review): the manifest exists before any member runs."""
    from analysis.sim.loader import batch_mode

    _append_two_varying_rows(append_calendar_row)
    runner = CliRunner()
    assert _sim_load(test_data_dir, intent_dir, runner).exit_code == 0
    import analysis.sim.cli as sim_cli

    seen: list[str] = []
    real = sim_cli._run_one_member

    def _spy(recipe, member_intent_path, member_run_path, *args, **kwargs):
        seen.append(batch_mode(member_run_path.parent))
        return real(recipe, member_intent_path, member_run_path, *args, **kwargs)

    monkeypatch.setattr(sim_cli, "_run_one_member", _spy)
    assert _sim_run_sensitivity(intent_dir, runs_dir, runner).exit_code == 0
    assert seen and set(seen) == {"sensitivity"}


def test_batch_mode_falls_back_to_member_manifests(oat_batch: Path) -> None:
    from analysis.sim.loader import batch_mode

    (oat_batch / "batch_manifest.json").unlink()
    assert batch_mode(oat_batch) == "sensitivity"


@pytest.mark.parametrize("value", ["0", "1", "-0.1", "1.5"])
def test_ensemble_sensitivity_rejects_bad_negligible(runs_dir: Path, value: str) -> None:
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "sensitivity", "retire_alice", "--runs-dir", str(runs_dir),
        "--negligible", value,
    ])
    assert result.exit_code == 2
    assert "--negligible" in result.output



# --- #31: shape and triangular options on `sim calendar add` --------------------------


def test_sim_calendar_add_writes_shape_and_mode(test_data_dir: Path) -> None:
    runner = CliRunner()
    skewed = runner.invoke(main, [
        "sim", "calendar", "add", "set-rate", "2027-02-28", "Asset_Checking", "4.5",
        "--data-dir", str(test_data_dir), "--name", "checking_vary",
        "--vary-target", "amount", "--vary-distribution", "truncated_normal",
        "--vary-median", "4.5", "--vary-sd", "1.0", "--vary-skew", "0.5", "--vary-tailweight", "0.8",
    ])
    assert skewed.exit_code == 0, skewed.output
    triangle = runner.invoke(main, [
        "sim", "calendar", "add", "retire-scheduled", "2030-12-31", "salary_bill",
        "--data-dir", str(test_data_dir), "--name", "retire_bill_vary",
        "--vary-target", "date", "--vary-distribution", "triangular",
        "--vary-low", "2030-06-30", "--vary-mode", "2030-12-31", "--vary-high", "2031-12-31",
    ])
    assert triangle.exit_code == 0, triangle.output
    rows = pd.read_csv(test_data_dir / "scenarios" / "retire_alice" / "calendar.csv",
                       dtype=str, keep_default_na=False).set_index("name")
    assert rows.loc["checking_vary", ["vary_median", "vary_sd", "vary_skew", "vary_tailweight"]].tolist() \
        == ["4.5%", "1%", "0.5", "0.8"]
    assert rows.loc["retire_bill_vary", "vary_mode"] == "2030-12-31"


def test_sim_calendar_add_rejects_skew_without_sd(test_data_dir: Path) -> None:
    result = CliRunner().invoke(main, [
        "sim", "calendar", "add", "set-rate", "2027-02-28", "Asset_Checking", "4.5",
        "--data-dir", str(test_data_dir), "--name", "checking_vary",
        "--vary-target", "amount", "--vary-distribution", "truncated_normal",
        "--vary-low", "3.0", "--vary-high", "6.0", "--vary-skew", "0.5",
    ])
    assert result.exit_code != 0
    assert "vary_sd is required" in result.output


def test_sim_calendar_add_accepts_year_month(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path], intent_dir: Path,
) -> None:
    """#34: a bare YYYY-MM is written as typed and loads as the month's end."""
    result = CliRunner().invoke(
        main,
        [
            "sim", "calendar", "add", "set-rate", "2027-02", "Asset_Checking", "4.5",
            "--data-dir", str(test_data_dir), "--scenario", "retire_alice",
        ],
    )
    assert result.exit_code == 0, result.output
    df = pd.read_csv(retire_alice_csv("calendar.csv"), dtype=str, keep_default_na=False)
    assert df[df["series"] == "Asset_Checking"].iloc[-1]["date"] == "2027-02"
    loaded = load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir,
                           allow_dirty_data=True)
    con = sqlite3.connect(loaded.intent_path)
    assert ("2027-02-28",) in con.execute(
        "SELECT date FROM calendar_event WHERE kind = 'set_rate' AND amount = 45000"
    ).fetchall()


@pytest.mark.parametrize(("start", "ok"), [("2030-03", False), ("2030-03-13", True)])
def test_scheduled_add_biweekly_start_needs_a_full_date(
    test_data_dir: Path, start: str, ok: bool,
) -> None:
    """The loader rejects a bare-month biweekly start (it is the 14-day
    anchor), so the CLI must not write one."""
    result = CliRunner().invoke(main, [
        "sim", "scheduled", "add", start, "Asset_Checking", "Asset_Savings", "75.00",
        "--cadence", "biweekly", "--end", "2031-06", "--scenario", "retire_alice",
        "--data-dir", str(test_data_dir),
    ])
    assert (result.exit_code == 0) == ok, result.output
    if not ok:
        assert "biweekly start needs a full YYYY-MM-DD" in result.output


@pytest.mark.uses_real_cpu_count
@pytest.mark.parametrize(("available", "expected"), [(6, 6), (None, 1)])
def test_sim_run_jobs_defaults_to_available_cpus(
    monkeypatch, available: int | None, expected: int,
) -> None:
    """#42: with no --jobs, members run one per CPU available to the process
    (1 if that can't be determined)."""
    import analysis.sim.cli as sim_cli

    jobs_param = next(p for p in sim_cli.run.params if p.name == "jobs")
    default = jobs_param.default
    assert isinstance(default, int) and default == 0
    monkeypatch.setattr(sim_cli.os, "process_cpu_count", lambda: available)
    *_, jobs = sim_cli._batch_options(
        sensitivity=False, ensemble_size=3, only=None, quantiles_text=None,
        jobs=default,
    )
    assert jobs == expected
