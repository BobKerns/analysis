"""``sim scenario roll`` (#502): a scenario started again later, opening with
what its accounts hold then, and ``sim compare`` across the two starts."""

from __future__ import annotations

import csv
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.sim import compare, roll
from analysis.sim.connection import RunConnection
from analysis.sim.loader import latest_run_path


def _sim(data_dir: Path, *args: str, input: str | None = None) -> Result:  # noqa: A002 -- CliRunner's name
    return CliRunner().invoke(main, ["sim", *args, "--data-dir", str(data_dir)], input=input,
                              terminal_width=200)


def _run(data_dir: Path, intent_dir: Path, runs_dir: Path, name: str) -> Path:
    result = CliRunner().invoke(main, [
        "sim", "run", name, "--data-dir", str(data_dir), "--intent-dir", str(intent_dir),
        "--runs-dir", str(runs_dir), "--allow-dirty-data"])
    assert result.exit_code == 0, result.output
    found = latest_run_path(runs_dir, name)
    assert found is not None
    return found


def _rows(path: Path) -> dict[str, dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    key = "account_name" if "account_name" in rows[0] else "name"
    return {row[key] + (f"/{row['lot'] or 'default'}" if key == "account_name" else ""): row
            for row in rows}


def test_default_start_is_the_next_january_not_before_this_year() -> None:
    assert roll.default_start("2026-05", date(2026, 11, 10)) == "2027-01"
    assert roll.default_start("2026-01", date(2027, 1, 15)) == "2027-01"
    assert roll.default_start("2024-01", date(2027, 3, 1)) == "2027-01"


@pytest.mark.uses_current_scenario_write
def test_roll_takes_the_projection_keeps_the_old_plan_and_pins_last_years_rows(
    test_data_dir: Path, test_scenarios_dir: Path, intent_dir: Path, runs_dir: Path,
) -> None:
    run = _run(test_data_dir, intent_dir, runs_dir, "retire_alice")
    with RunConnection(run, "retire_alice") as rc:
        projected = roll.projections(rc, roll.month_before("2027-01"))
    folder = test_scenarios_dir / "retire_alice"
    before = (folder / "account_lots.csv").read_bytes()

    result = _sim(test_data_dir, "scenario", "roll", "retire_alice", "2027-01", "--yes")

    assert result.exit_code == 0, result.output
    assert "Rolling retire_alice forward: 2026-05 -> 2027-01" in result.output
    with (folder / "scenario.csv").open(newline="", encoding="utf-8") as fh:
        assert next(csv.DictReader(fh))["start"] == "2027-01"
    lots = _rows(folder / "account_lots.csv")
    checking = projected[("Asset_Checking", "default")]
    assert lots["Asset_Checking/default"]["opening_balance"] == f"{checking.balance:.2f}"
    mortgage = projected[("Liability_Mortgage", "default")]
    assert lots["Liability_Mortgage/default"]["opening_balance"] == f"{mortgage.balance:.2f}"
    brokerage = projected[("Asset_Brokerage_Joint", "default")]
    assert brokerage.basis is not None
    assert lots["Asset_Brokerage_Joint/default"]["opening_cost_basis"] == f"{brokerage.basis:.2f}"
    # A Roth's basis is its contributions, written out now that the balance moves.
    assert lots["Asset_Roth_Bill/default"]["opening_cost_basis"] == "80000.00"
    # A January start: the Dec 31 balance is the opening one.
    ira = lots["Asset_TraditionalIRA_Alice/default"]
    assert ira["prior_year_end_balance"] == ira["opening_balance"]
    # Rows of the old first year stay in it; one with a year keeps it.
    ytd = _rows(folder / "year_to_date.csv")
    assert ytd["prior_ordinary_wages"]["year"] == "2026"
    assert ytd["prior_ltcg_loss_carryforward"]["year"] == "2025"
    assert "2026's tax is settled in April 2027" in result.output
    # The old plan is kept as it stood, and the rolled one stays current.
    kept = test_scenarios_dir / "retire_alice-2026"
    assert (kept / "account_lots.csv").read_bytes() == before
    assert (test_data_dir / ".current_scenario").read_text().strip() == "retire_alice"


def test_roll_asks_for_each_lot_offering_the_current_value_without_a_run(
    test_data_dir: Path, test_scenarios_dir: Path,
) -> None:
    # No run: each default is the opening value as it stands. Answers: the
    # checking balance, then Enter for every other question.
    answers = "6000\n" + "\n" * 20
    result = _sim(test_data_dir, "scenario", "roll", "retire_alice", "2027-01", "--no-keep",
                  input=answers)

    assert result.exit_code == 0, result.output
    assert "No run of retire_alice reaches 2026-12" in result.output
    assert "Asset_Checking (default) balance [5000.00]" in result.output
    assert "pin it to 2026?" in result.output
    lots = _rows(test_scenarios_dir / "retire_alice" / "account_lots.csv")
    assert lots["Asset_Checking/default"]["opening_balance"] == "6000.00"
    assert lots["Asset_Savings/default"]["opening_balance"] == "20000.00"
    assert not (test_scenarios_dir / "retire_alice-2026").exists()


def test_roll_refuses_a_start_that_is_not_later(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "scenario", "roll", "retire_alice", "2026-05", "--no-keep", "--yes")
    assert result.exit_code != 0
    assert "2026-05 is not after retire_alice's start, 2026-05" in result.output


def test_roll_makes_a_ytd_amount_a_row_of_the_old_year(
    test_data_dir: Path, test_scenarios_dir: Path,
) -> None:
    path = test_scenarios_dir / "retire_alice" / "scheduled.csv"
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header, rules = list(reader.fieldnames or []), list(reader)
    rule = next(row for row in rules if row["amount_rule_kind"] == "fixed_amount"
                and row["from_account"].startswith("Asset_") and row["tax_character"])
    rule["ytd_amount"] = "1234.00"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rules)

    result = _sim(test_data_dir, "scenario", "roll", "retire_alice", "2027-01", "--no-keep", "--yes")

    assert result.exit_code == 0, result.output
    assert _rows(path)[rule["name"]]["ytd_amount"] == ""
    converted = _rows(test_scenarios_dir / "retire_alice" / "year_to_date.csv")[f"{rule['name']}_2026"]
    assert (converted["amount"], converted["year"], converted["from_account"]) == (
        "1234.00", "2026", rule["from_account"])


@pytest.mark.slow
@pytest.mark.uses_current_scenario_write
def test_compare_across_starts_shares_one_dollar_and_shows_where_the_new_plan_starts(
    test_data_dir: Path, intent_dir: Path, runs_dir: Path,
) -> None:
    _run(test_data_dir, intent_dir, runs_dir, "retire_alice")
    result = _sim(test_data_dir, "scenario", "roll", "retire_alice", "2027-01", "--yes",
                  "--keep", "plan_2026")
    assert result.exit_code == 0, result.output
    old = _run(test_data_dir, intent_dir, runs_dir, "plan_2026")
    new = _run(test_data_dir, intent_dir, runs_dir, "retire_alice")

    with RunConnection(old, "plan_2026") as before, RunConnection(new, "retire_alice") as after:
        connections = {"plan_2026": before, "retire_alice": after}
        assert set(compare.align_cpi(connections, "plan_2026")) == {"retire_alice"}
        # The same inflation series: once rebased, a month's dollars convert alike.
        month = after.cpi_index().index[6]
        assert after.cpi_index()[month] == pytest.approx(before.cpi_index()[month])
        # Rules that only start with their scenario are no difference.
        rules = compare.rule_differences(connections, list(connections), "plan_2026")
        assert "start" not in rules.index.get_level_values("field")
        # Rolled with --yes, it opens with what the old plan projected.
        _, start = compare.start_comparison(connections, "plan_2026", "retire_alice")
        assert start.loc[("total", ""), "Δ"] == pytest.approx(0, abs=0.01)

    shown = CliRunner().invoke(main, [
        "sim", "compare", "plan_2026", "retire_alice", "--section", "start",
        "--data-dir", str(test_data_dir), "--runs-dir", str(runs_dir)], terminal_width=200)
    assert shown.exit_code == 0, shown.output
    assert "retire_alice in plan_2026's dollars: plan_2026 starts 2026-05" in shown.output
    assert ("Where retire_alice starts: plan_2026's projection for 2026-12 against "
            "retire_alice's opening balances (nominal):") in shown.output


def test_roll_lists_calendar_rows_it_leaves_behind(
    test_data_dir: Path, append_calendar_row: Callable[..., None],
) -> None:
    append_calendar_row(date="2026-08", kind="one_off_transfer", account_1="Asset_Savings",
                        account_2="Asset_Checking", amount="1000.00", description="roof repair")
    append_calendar_row(date="2027-03", kind="one_off_transfer", account_1="Asset_Savings",
                        account_2="Asset_Checking", amount="1000.00", description="new car")

    result = _sim(test_data_dir, "scenario", "roll", "retire_alice", "2027-01", "--no-keep", "--yes")

    assert result.exit_code == 0, result.output
    assert ("  calendar 2026-08 one_off_transfer roof repair: before 2027-01, "
            "so it no longer happens") in result.output
    assert "new car" not in result.output
