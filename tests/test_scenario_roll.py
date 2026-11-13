"""``sim scenario roll`` (#502): a scenario started again later, opening with
what its accounts hold then, and ``sim compare`` across the two starts."""

from __future__ import annotations

import csv
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.frames import named_rows
from analysis.sim import compare, roll
from analysis.sim.connection import RunConnection
from analysis.sim.dates import month_before


def _sim(data_dir: Path, *args: str, input: str | None = None) -> Result:  # noqa: A002 -- CliRunner's name
    return CliRunner().invoke(main, ["sim", *args, "--data-dir", str(data_dir)], input=input,
                              terminal_width=200)


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
    test_data_dir: Path, test_scenarios_dir: Path, latest_test_run: Callable[[str], Path],
) -> None:
    run = latest_test_run("retire_alice")
    with RunConnection(run, "retire_alice") as rc:
        projected = roll.projections(rc, month_before("2027-01"))
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
    # The months the run no longer covers stay in 2026's income (#564), so
    # nothing is left to record by hand for its April settlement.
    projected_rows = [row for name, row in ytd.items() if name.startswith(roll.PROJECTED_PREFIX)]
    assert projected_rows and {row["year"] for row in projected_rows} == {"2026"}
    assert "projected year-to-date row(s) for 2026-05 to 2026-12" in result.output
    assert "2026's tax is settled in April 2027" not in result.output
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


@pytest.mark.uses_current_scenario_write
def test_roll_to_a_later_month_offers_the_dec_31_balance_as_prior_year_end(
    test_data_dir: Path, test_scenarios_dir: Path, latest_test_run: Callable[[str], Path],
) -> None:
    run = latest_test_run("retire_alice")
    with RunConnection(run, "retire_alice") as rc:
        december = roll.projections(rc, month_before("2027-01"))
        june = roll.projections(rc, month_before("2027-07"))

    result = _sim(test_data_dir, "scenario", "roll", "retire_alice", "2027-07", "--no-keep", "--yes")

    assert result.exit_code == 0, result.output
    ira = _rows(test_scenarios_dir / "retire_alice" / "account_lots.csv")[
        "Asset_TraditionalIRA_Alice/default"]
    key = ("Asset_TraditionalIRA_Alice", "default")
    assert ira["opening_balance"] == f"{june[key].balance:.2f}"
    assert ira["prior_year_end_balance"] == f"{december[key].balance:.2f}"


def test_roll_leaves_a_ytd_amount_it_cant_make_a_row_and_says_so(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
    update_scheduled: Callable[..., None],
) -> None:
    rules = pd.read_csv(retire_alice_csv("scheduled.csv"), dtype=str, keep_default_na=False)
    rule = next(row for row in rules.to_dict("records") if row["from_account"] == "Asset_Checking")
    # A rule drawing through a chain: a year-to-date row takes accounts only.
    update_scheduled(rule["name"], ytd_amount="500.00", from_account="asset_checking_liquidity")

    result = _sim(test_data_dir, "scenario", "roll", "retire_alice", "2027-01", "--no-keep", "--yes")

    assert result.exit_code == 0, result.output
    assert f"rule {rule['name']}'s ytd_amount is 2026's catch-up, but it moves money through a chain" \
        in result.output
    assert _rows(retire_alice_csv("scheduled.csv"))[rule["name"]]["ytd_amount"] == "500.00"


def test_roll_keeps_a_quarterly_rule_with_no_start_on_its_months(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
    update_scheduled: Callable[..., None],
) -> None:
    rules = pd.read_csv(retire_alice_csv("scheduled.csv"), dtype=str, keep_default_na=False)
    rule = next(row for row in rules.to_dict("records") if row["amount_rule_kind"] == "fixed_amount")
    update_scheduled(rule["name"], start="", cadence="quarterly")

    result = _sim(test_data_dir, "scenario", "roll", "retire_alice", "2027-01", "--no-keep", "--yes")

    assert result.exit_code == 0, result.output
    assert _rows(retire_alice_csv("scheduled.csv"))[rule["name"]]["start"] == "2026-05-31"
    assert f"rule {rule['name']}: starts 2026-05" in result.output


def test_roll_within_the_year_keeps_the_lots_dec_31_balance_without_a_run(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    before = _rows(retire_alice_csv("account_lots.csv"))["Asset_TraditionalIRA_Alice/default"]

    result = _sim(test_data_dir, "scenario", "roll", "retire_alice", "2026-09", "--no-keep", "--yes")

    assert result.exit_code == 0, result.output
    after = _rows(retire_alice_csv("account_lots.csv"))["Asset_TraditionalIRA_Alice/default"]
    assert after["prior_year_end_balance"] == f"{float(before['prior_year_end_balance']):.2f}"


def test_roll_keeps_an_authored_zero_cost_basis(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
    update_account_value: Callable[..., None],
) -> None:
    update_account_value("Asset_Brokerage_Joint", "default", opening_cost_basis="0")

    result = _sim(test_data_dir, "scenario", "roll", "retire_alice", "2027-01", "--no-keep", "--yes")

    assert result.exit_code == 0, result.output
    rolled = _rows(retire_alice_csv("account_lots.csv"))["Asset_Brokerage_Joint/default"]
    assert rolled["opening_cost_basis"] == "0.00"


def test_roll_with_yes_refuses_a_value_it_has_no_default_for(
    test_data_dir: Path, update_account_value: Callable[..., None],
) -> None:
    update_account_value("Asset_Savings", "", opening_balance="@1000+2000")

    result = _sim(test_data_dir, "scenario", "roll", "retire_alice", "2027-01", "--no-keep", "--yes")

    assert result.exit_code != 0
    assert "Asset_Savings (default) balance: no default to take with --yes" in result.output


def test_roll_to_a_spring_start_reminds_of_last_years_tax(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "scenario", "roll", "retire_alice", "2027-03", "--no-keep", "--yes")
    assert result.exit_code == 0, result.output
    assert "2026's tax is settled in April 2027" in result.output


def test_roll_refuses_a_start_that_is_not_later(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "scenario", "roll", "retire_alice", "2026-05", "--no-keep", "--yes")
    assert result.exit_code != 0
    assert "2026-05 is not after retire_alice's start, 2026-05" in result.output


def test_roll_makes_a_ytd_amount_a_row_of_the_old_year(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
    update_scheduled: Callable[..., None],
) -> None:
    path = retire_alice_csv("scheduled.csv")
    rules = pd.read_csv(path, dtype=str, keep_default_na=False)
    rule = next(row for row in rules.to_dict("records") if row["amount_rule_kind"] == "fixed_amount"
                and row["from_account"].startswith("Asset_") and row["tax_character"])
    update_scheduled(rule["name"], ytd_amount="1234.00")

    result = _sim(test_data_dir, "scenario", "roll", "retire_alice", "2027-01", "--no-keep", "--yes")

    assert result.exit_code == 0, result.output
    assert _rows(path)[rule["name"]]["ytd_amount"] == ""
    converted = _rows(retire_alice_csv("year_to_date.csv"))[f"{rule['name']}_2026"]
    assert (converted["amount"], converted["year"], converted["from_account"]) == (
        "1234.00", "2026", rule["from_account"])


@pytest.mark.slow
@pytest.mark.uses_current_scenario_write
def test_compare_across_starts_shares_one_dollar_and_shows_where_the_new_plan_starts(
    test_data_dir: Path, runs_dir: Path, latest_test_run: Callable[[str], Path],
) -> None:
    latest_test_run("retire_alice")
    result = _sim(test_data_dir, "scenario", "roll", "retire_alice", "2027-01", "--yes",
                  "--keep", "plan_2026")
    assert result.exit_code == 0, result.output
    old = latest_test_run("plan_2026")
    new = latest_test_run("retire_alice")

    with RunConnection(old, "plan_2026") as before, RunConnection(new, "retire_alice") as after:
        connections = {"plan_2026": before, "retire_alice": after}
        recorded = after.net_worth_frame()["cumulative_index_micros"].copy()
        assert set(compare.align_cpi(connections, "plan_2026")) == {"retire_alice"}
        # Only the deflator moves: the run's own index (a rail's threshold
        # reads it) stays as recorded, and aligning again changes nothing.
        pd.testing.assert_series_equal(after.net_worth_frame()["cumulative_index_micros"], recorded)
        scale = after.cpi_scale
        compare.align_cpi(connections, "plan_2026")
        assert after.cpi_scale == pytest.approx(scale)
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


def test_keep_name_is_the_old_year_then_its_month_then_numbered() -> None:
    assert roll.keep_name("plan", "2026-05", set()) == "plan-2026"
    assert roll.keep_name("plan", "2026-05", {"plan-2026"}) == "plan-2026-05"
    assert roll.keep_name("plan", "2026-05", {"plan-2026", "plan-2026-05", "plan-2026-05-2"}) \
        == "plan-2026-05-3"


def test_roll_refuses_a_keep_name_that_is_not_a_scenario_name(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    before = retire_alice_csv("scenario.csv").read_bytes()

    result = _sim(test_data_dir, "scenario", "roll", "retire_alice", "2027-01", "--keep", "Old Plan",
                  "--yes")

    assert result.exit_code != 0
    assert "invalid scenario name: 'Old Plan'" in result.output
    assert "Rolling" not in result.output
    assert retire_alice_csv("scenario.csv").read_bytes() == before


def test_a_second_roll_in_a_year_keeps_the_plan_under_its_month(
    test_data_dir: Path, test_scenarios_dir: Path,
) -> None:
    first = _sim(test_data_dir, "scenario", "roll", "retire_alice", "2026-09", "--yes")
    assert first.exit_code == 0, first.output

    second = _sim(test_data_dir, "scenario", "roll", "retire_alice", "2026-11", "--yes")

    assert second.exit_code == 0, second.output
    assert "kept the plan as it stood as retire_alice-2026-09" in second.output
    assert (test_scenarios_dir / "retire_alice-2026").exists()


def test_roll_writes_nothing_when_a_file_changed_while_it_asked(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from analysis.sim import cli

    lots = retire_alice_csv("account_lots.csv")
    asked = cli._ask_money
    edited: list[str] = []

    def ask_while_someone_edits(prompt: str, default: float | None, accept: bool) -> str:
        # Another edit lands between the questions: the rows change places.
        if not edited:
            frame = pd.read_csv(lots, dtype=str, keep_default_na=False)
            frame.iloc[::-1].to_csv(lots, index=False)
            edited.append(prompt)
        return asked(prompt, default, accept)

    monkeypatch.setattr(cli, "_ask_money", ask_while_someone_edits)
    result = _sim(test_data_dir, "scenario", "roll", "retire_alice", "2027-01", "--no-keep", "--yes")

    assert result.exit_code != 0
    assert "changed while retire_alice was being rolled forward" in result.output
    with retire_alice_csv("scenario.csv").open(newline="", encoding="utf-8") as fh:
        assert next(csv.DictReader(fh))["start"].startswith("2026-05")


@pytest.mark.slow
@pytest.mark.uses_current_scenario_write
@pytest.mark.parametrize("start", ["2026-09", "2027-01"])
def test_a_roll_keeps_the_skipped_months_in_their_tax_year(
    start: str, test_data_dir: Path, latest_test_run: Callable[[str], Path],
    add_state_tax: Callable[..., None],
) -> None:
    """#564: the old run's flows in the months the roll skips become
    projected year-to-date rows, so the rolled run taxes the year as the old
    one did: within the year, and across it (the April settlement)."""
    add_state_tax()
    old = latest_test_run("retire_alice")

    result = _sim(test_data_dir, "scenario", "roll", "retire_alice", start, "--no-keep", "--yes")
    assert result.exit_code == 0, result.output
    new = latest_test_run("retire_alice")

    def taxes(run: Path) -> dict[tuple[int, str], int]:
        with RunConnection(run, "retire_alice") as rc:
            frame = rc._read_sql("SELECT year, bucket, amount FROM tax_year_summary "
                                 "WHERE year = 2026 AND bucket IN "
                                 "('federal_agi', 'federal_tax', 'state_tax')")
        return {(int(row.year), str(row.bucket)): int(row.amount) for row in named_rows(frame)}

    before = taxes(old)
    assert before and taxes(new) == before


def test_projected_rows_add_to_an_earlier_rolls_and_drop_years_that_no_longer_count() -> None:
    earlier = [
        {"name": "prior_wages", "from_account": "Income_Salary_Alice", "from_lot": "",
         "to_account": "Asset_Checking", "to_lot": "", "amount": "9000.00",
         "tax_character": "ordinary", "year": "", "description": ""},
        {"name": "projected_2025_1", "from_account": "Income_Salary_Alice", "from_lot": "default",
         "to_account": "Asset_Checking", "to_lot": "default", "amount": "100.00",
         "tax_character": "ordinary", "year": "2025", "description": ""},
        {"name": "projected_2026_1", "from_account": "Income_Salary_Alice", "from_lot": "default",
         "to_account": "Asset_Checking", "to_lot": "default", "amount": "1000.00",
         "tax_character": "ordinary", "year": "2026",
         "description": "projected by sim scenario roll, 2026-05 to 2026-10"},
    ]
    projected = [
        roll.ProjectedRow(2026, "Income_Salary_Alice", "default", "Asset_Checking", "default",
                          "ordinary", 50_025),
        roll.ProjectedRow(2027, "Asset_Checking", "default", "Expense_Living", "default",
                          "none", 12_000),
    ]

    rows, dropped = roll.merge_projected(earlier, projected, "2026-11", "2027-02")

    by_name = {row["name"]: row for row in rows}
    # 2025 is settled before a 2027 start can see it.
    assert dropped == 1 and "projected_2025_1" not in by_name
    assert by_name["prior_wages"] == earlier[0]
    assert by_name["projected_2026_1"]["amount"] == "1500.25"
    # Each row says which months it covers, so a derived scenario's roll
    # knows which of them its own run covers again.
    assert by_name["projected_2026_1"]["description"] == "projected by sim scenario roll, 2026-05 to 2027-01"
    assert roll.covers(by_name["projected_2027_1"]) == ("2026-11", "2027-01")
    assert by_name["projected_2027_1"]["amount"] == "120.00"
    assert by_name["projected_2027_1"]["year"] == "2027"


@pytest.mark.slow
@pytest.mark.uses_current_scenario_write
def test_a_derived_scenario_rolls_only_its_own_rows_after_its_parent(
    test_data_dir: Path, test_scenarios_dir: Path, latest_test_run: Callable[[str], Path],
) -> None:
    """#564: a scenario built on a rolled one rolls with --own: its own lot,
    no start of its own, and its own run's flows in place of the projected
    rows it would inherit, so none are counted twice."""
    assert _sim(test_data_dir, "scenario", "create", "alice_more", "retire_alice").exit_code == 0
    edited = _sim(test_data_dir, "account", "lot", "edit", "Asset_Savings", "default",
                  "--opening-balance", "30000.00")
    assert edited.exit_code == 0, edited.output
    latest_test_run("retire_alice")
    child_run = latest_test_run("alice_more")
    with RunConnection(child_run, "alice_more") as rc:
        savings = roll.projections(rc, month_before("2027-01"))[("Asset_Savings", "default")]
        flows = roll.projected_year_to_date(rc, "2026-05", "2027-01")
    child = test_scenarios_dir / "alice_more"

    # Its start is retire_alice's, which hasn't moved yet.
    early = _sim(test_data_dir, "scenario", "roll", "alice_more", "2027-01", "--own", "--yes",
                 "--no-keep")
    assert early.exit_code != 0 and roll.PARENT_FIRST in early.output
    # With no START, it would take the inherited one: so it asks for one.
    assert "give START" in _sim(test_data_dir, "scenario", "roll", "alice_more", "--own").output

    parent = _sim(test_data_dir, "scenario", "roll", "retire_alice", "2027-01", "--yes", "--no-keep")
    assert parent.exit_code == 0, parent.output
    assert "sim scenario roll alice_more 2027-01 --own" in parent.output

    result = _sim(test_data_dir, "scenario", "roll", "alice_more", "2027-01", "--own", "--yes",
                  "--no-keep")

    assert result.exit_code == 0, result.output
    assert "Rolling alice_more forward: 2026-05 -> 2027-01" in result.output
    with (child / "scenario.csv").open(newline="", encoding="utf-8") as fh:
        assert next(csv.DictReader(fh))["start"] == ""
    lots = _rows(child / "account_lots.csv")
    assert list(lots) == ["Asset_Savings/default"]
    assert lots["Asset_Savings/default"]["opening_balance"] == f"{savings.balance:.2f}"
    # Every projected row it inherits is its own now, holding its own flows.
    inherited = {name for name in _rows(test_scenarios_dir / "retire_alice" / "year_to_date.csv")
                 if name.startswith(roll.PROJECTED_PREFIX)}
    own = {name: row for name, row in _rows(child / "year_to_date.csv").items()
           if name.startswith(roll.PROJECTED_PREFIX)}
    assert inherited and inherited <= set(own)
    total = sum(round(float(row["amount"]) * 100) for row in own.values())
    assert total == sum(one.amount for one in flows)
    assert _sim(test_data_dir, "check", "alice_more").exit_code == 0
    # Twice would add the same months again.
    again = _sim(test_data_dir, "scenario", "roll", "alice_more", "2027-01", "--own", "--yes",
                 "--no-keep")
    assert again.exit_code != 0 and "already rolled to 2027-01" in again.output


@pytest.mark.uses_current_scenario_write
def test_a_derived_scenario_replaces_only_the_projected_rows_its_run_covers_again(
    test_data_dir: Path, test_scenarios_dir: Path,
) -> None:
    assert _sim(test_data_dir, "scenario", "create", "alice_more", "retire_alice").exit_code == 0
    path = test_scenarios_dir / "retire_alice" / "year_to_date.csv"
    with path.open("a", newline="", encoding="utf-8") as fh:
        for name, described in (("projected_2026_1", "projected by sim scenario roll, 2026-01 to 2026-02"),
                                ("projected_2026_2", "projected by sim scenario roll, 2026-03 to 2026-04"),
                                ("projected_2026_3", "projected by sim scenario roll, 2026-02 to 2026-04"),
                                ("projected_2026_4", "Alice's W-2")):
            csv.writer(fh).writerow([name, "Income_Salary_Alice", "default", "Asset_Checking",
                                     "default", "100.00", "ordinary", "2026", described])

    replaced, mixed = roll.inherited_projected(test_scenarios_dir / "alice_more", "2026-03", "2026-05")

    # Jan-Feb is before the child's run: retire_alice's row stays.
    assert [row["name"] for row in replaced] == ["projected_2026_2"]
    assert replaced[0]["amount"] == "0.00"
    assert mixed == ["projected_2026_3 (retire_alice)", "projected_2026_4 (retire_alice)"]
