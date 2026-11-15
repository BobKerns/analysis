"""The `sim import` commands (issue #527)."""

from __future__ import annotations

import csv
import json
import os
import subprocess
import re
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.expenses.baseline import Occasional
from analysis.sim import cli
from analysis.sim.imports import yearly as import_yearly


MapWriter = Callable[[str, str], Path]


def _import(data: Path, *args: str, input: str | None = None,  # noqa: A002
            scenario: str = "retire_alice"):
    return CliRunner().invoke(
        main, ["sim", "import", *args, "--scenario", scenario, "--data-dir", str(data)],
        input=input,
    )


def _lots(data: Path, scenario: str = "retire_alice") -> dict[str, dict[str, str]]:
    path = data / "scenarios" / scenario / "account_lots.csv"
    with path.open(newline="", encoding="utf-8") as f:
        return {r["account_name"]: r for r in csv.DictReader(f)}


def _generated(data: Path) -> dict[str, dict[str, str]]:
    path = data / "scenarios" / "retire_alice" / "scheduled.csv"
    with path.open(newline="", encoding="utf-8") as f:
        return {r["name"]: r for r in csv.DictReader(f) if "generated:expense_baseline" in r["tags"]}


@pytest.fixture
def at_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_interactive", lambda: True)


# ---------- sources ----------

def test_sim_import_sources_lists_them_and_names_a_files_source(
    quicken_net_worth_export: Path, quicken_transaction_export: Path,
) -> None:
    listed = CliRunner().invoke(main, ["sim", "import", "sources"])
    assert listed.exit_code == 0, listed.output
    assert "quicken" in listed.output and "Net Worth Export" in listed.output

    found = CliRunner().invoke(main, ["sim", "import", "sources", str(quicken_net_worth_export),
                                      str(quicken_transaction_export)])
    assert found.exit_code == 0, found.output
    assert found.output.splitlines() == [
        f"{quicken_net_worth_export}: quicken holdings",
        f"{quicken_transaction_export}: quicken transactions",
    ]


# ---------- holdings ----------

@pytest.mark.usefixtures("quicken_account_map")
def test_sim_import_holdings_updates_balances(
    test_data_dir: Path, quicken_net_worth_export: Path,
) -> None:
    result = _import(test_data_dir, "holdings", str(quicken_net_worth_export))
    assert result.exit_code == 0, result.output
    assert "snapshot as of 2026-08-18" in result.output

    rows = _lots(test_data_dir)
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

    check = CliRunner().invoke(
        main, ["sim", "check", "retire_alice", "--data-dir", str(test_data_dir)]
    )
    assert check.exit_code == 0, check.output


@pytest.mark.usefixtures("quicken_account_map")
def test_sim_import_holdings_year_end_sets_prior_year_only(
    test_data_dir: Path, quicken_net_worth_export: Path,
) -> None:
    result = _import(test_data_dir, "holdings", str(quicken_net_worth_export), "--year-end",
                     "--source", "quicken")
    assert result.exit_code == 0, result.output
    assert "2 ineligible" in result.output  # Asset_Checking, Liability_Mortgage

    rows = _lots(test_data_dir)
    # opening_balance is untouched by --year-end
    assert rows["Asset_Brokerage_Joint"]["opening_balance"] == "300000.00"
    assert rows["Asset_Brokerage_Joint"]["prior_year_end_balance"] == "310000.00"
    # --year-end always overwrites, even if already present
    assert rows["Asset_TraditionalIRA_Alice"]["prior_year_end_balance"] == "510000.00"
    # non-investment accounts are left untouched entirely
    assert rows["Asset_Checking"]["opening_balance"] == "5000.00"
    assert rows["Liability_Mortgage"]["prior_year_end_balance"] == ""


def test_sim_import_holdings_rejects_unmapped_account(
    test_data_dir: Path, quicken_net_worth_export: Path, write_import_map: MapWriter,
) -> None:
    write_import_map("quicken/accounts.csv", "Asset_Checking,,map,Asset_Checking,,\n")
    before = _lots(test_data_dir)

    result = _import(test_data_dir, "holdings", str(quicken_net_worth_export))

    assert result.exit_code != 0
    assert "unmapped" in result.output
    assert "Liability_Mortgage" in result.output
    assert _lots(test_data_dir) == before


@pytest.mark.usefixtures("quicken_account_map")
def test_sim_import_holdings_dry_run_shows_before_and_after_and_does_not_write(
    test_data_dir: Path, quicken_net_worth_export: Path,
) -> None:
    before = _lots(test_data_dir)

    result = _import(test_data_dir, "holdings", str(quicken_net_worth_export), "--dry-run")

    assert result.exit_code == 0, result.output
    assert "(dry run) 4 lot(s) would change" in result.output
    line = next(line for line in result.output.splitlines() if line.strip().startswith("Asset_Checking"))
    assert line.split() == ["Asset_Checking", "opening_balance", "5000.00", "1234.56"]
    assert _lots(test_data_dir) == before


def test_sim_import_holdings_refuses_a_file_no_source_recognizes(
    test_data_dir: Path, quicken_transaction_export: Path,
) -> None:
    result = _import(test_data_dir, "holdings", str(quicken_transaction_export))
    assert result.exit_code != 0
    assert "no source recognizes" in result.output
    assert "quicken transactions" in result.output


@pytest.mark.usefixtures("at_a_terminal")
def test_sim_import_holdings_asks_for_unmapped_accounts_and_saves_the_answers(
    test_data_dir: Path, quicken_net_worth_export: Path, write_import_map: MapWriter,
) -> None:
    saved = write_import_map(
        "quicken/accounts.csv",
        "Asset_Checking,,map,Asset_Checking,,\n"
        "Asset_Brokerage_Joint,,map,Asset_Brokerage_Joint,,\n")
    # In the file's order: the IRA, then the mortgage; a name that isn't a
    # choice is asked for again.
    answers = "nope\nAsset_TraditionalIRA_Alice\nskip\n"

    dry = _import(test_data_dir, "holdings", str(quicken_net_worth_export), "--dry-run",
                  input=answers)
    assert dry.exit_code == 0, dry.output
    assert "were not saved" in dry.output
    assert "Asset_TraditionalIRA_Alice" not in saved.read_text(encoding="utf-8")

    # A name that isn't a choice is offered as a new account, which is declined here.
    result = _import(test_data_dir, "holdings", str(quicken_net_worth_export),
                     input=answers.replace("nope\n", "nope\nn\n"))
    assert result.exit_code == 0, result.output
    assert "Add account nope?" in result.output and "Add account" not in dry.output
    assert "1 skipped" in result.output

    assert saved.read_text(encoding="utf-8").splitlines() == [
        "outside_account,asset,action,account,lot,note",
        "Asset_Checking,,map,Asset_Checking,,",
        "Asset_Brokerage_Joint,,map,Asset_Brokerage_Joint,,",
        "Asset_TraditionalIRA_Alice,,map,Asset_TraditionalIRA_Alice,,",
        "Liability_Mortgage,,skip,,,",
    ]
    rows = _lots(test_data_dir)
    assert rows["Asset_TraditionalIRA_Alice"]["opening_balance"] == "510000.00"
    assert rows["Liability_Mortgage"]["opening_balance"] == "250000.00"

    # Nothing is left to ask the second time.
    again = _import(test_data_dir, "holdings", str(quicken_net_worth_export))
    assert again.exit_code == 0, again.output
    assert "0 lot(s)" in again.output


@pytest.mark.usefixtures("at_a_terminal")
def test_sim_import_holdings_keeps_a_saved_mapping_another_scenario_may_use(
    test_data_dir: Path, quicken_net_worth_export: Path, quicken_account_map: Path,
    write_import_map: MapWriter,
) -> None:
    rows = quicken_account_map.read_text(encoding="utf-8").split("\n", 1)[1].replace(
        "map,Asset_Brokerage_Joint", "map,Asset_Brokerage")  # not an account here
    write_import_map("quicken/accounts.csv", rows)
    before = quicken_account_map.read_text(encoding="utf-8")

    # The answer is used, and the shared row is left as it was: N to replacing it.
    result = _import(test_data_dir, "holdings", str(quicken_net_worth_export),
                     input="Asset_Brokerage_Joint\nn\n")
    assert result.exit_code == 0, result.output
    assert "is mapped to Asset_Brokerage, which retire_alice doesn't have" in result.output
    assert "saved mappings" not in result.output
    assert quicken_account_map.read_text(encoding="utf-8") == before
    assert _lots(test_data_dir)["Asset_Brokerage_Joint"]["opening_balance"] == "310000.00"

    # Y replaces it for every scenario.
    result = _import(test_data_dir, "holdings", str(quicken_net_worth_export),
                     input="Asset_Brokerage_Joint\ny\n")
    assert result.exit_code == 0, result.output
    assert "Asset_Brokerage_Joint,,map,Asset_Brokerage_Joint,," in \
        quicken_account_map.read_text(encoding="utf-8")


@pytest.mark.usefixtures("quicken_account_map", "test_child_scenario")
def test_sim_import_holdings_overrides_the_lots_a_scenario_inherits(
    test_data_dir: Path, quicken_net_worth_export: Path,
) -> None:
    parent = _lots(test_data_dir)
    assert _lots(test_data_dir, "child") == {}

    result = _import(test_data_dir, "holdings", str(quicken_net_worth_export), scenario="child")

    assert result.exit_code == 0, result.output
    assert "4 as override(s) of inherited lots" in result.output
    assert _lots(test_data_dir) == parent
    child = _lots(test_data_dir, "child")
    assert child["Asset_Checking"] == {**parent["Asset_Checking"], "opening_balance": "1234.56"}
    # The inherited prior-year balance is kept, not seeded over.
    assert child["Asset_TraditionalIRA_Alice"] == {
        **parent["Asset_TraditionalIRA_Alice"], "opening_balance": "510000.00"}

    # The second time the rows are the child's own, and nothing differs.
    again = _import(test_data_dir, "holdings", str(quicken_net_worth_export), scenario="child")
    assert again.exit_code == 0, again.output
    assert "0 lot(s)" in again.output and "override" not in again.output
    assert _lots(test_data_dir, "child") == child


@pytest.mark.usefixtures("quicken_account_map")
def test_sim_import_holdings_refuses_the_same_file_twice(
    test_data_dir: Path, quicken_net_worth_export: Path,
) -> None:
    before = _lots(test_data_dir)
    result = _import(test_data_dir, "holdings", str(quicken_net_worth_export),
                     str(quicken_net_worth_export))
    assert result.exit_code != 0
    assert "is in more than one file" in result.output
    assert _lots(test_data_dir) == before


def test_renames_follow_into_the_saved_mappings_and_say_so(
    test_data_dir: Path, write_import_map: MapWriter,
) -> None:
    saved = write_import_map(
        "quicken/accounts.csv",
        "Checking,,map,Asset_Checking,,\nFunds,,map,Asset_Brokerage_Joint,default,\n")
    where = ["--scenario", "retire_alice", "--data-dir", str(test_data_dir)]

    result = CliRunner().invoke(main, [
        "sim", "account", "rename", "Asset_Checking", "Asset_Bank", *where])
    assert result.exit_code == 0, result.output
    assert "imports/quicken/accounts.csv" in result.output

    result = CliRunner().invoke(main, [
        "sim", "account", "edit", "Asset_Bank", "--new-name", "Asset_Cash", *where])
    assert result.exit_code == 0, result.output
    assert "also renamed in imports/quicken/accounts.csv" in result.output

    result = CliRunner().invoke(main, [
        "sim", "account", "lot", "rename", "Asset_Brokerage_Joint", "default", "funds", *where])
    assert result.exit_code == 0, result.output
    assert "imports/quicken/accounts.csv" in result.output

    assert saved.read_text(encoding="utf-8").splitlines()[1:] == [
        "Checking,,map,Asset_Cash,,", "Funds,,map,Asset_Brokerage_Joint,funds,"]


# ---------- transactions ----------

def _transactions(data: Path, export: Path, *args: str, input: str | None = None):  # noqa: A002
    return _import(data, "transactions", str(export), "--from-account", "Asset_Checking", *args,
                   input=input)


@pytest.mark.usefixtures("quicken_category_map")
def test_sim_import_transactions_writes_rules_and_a_rerun_shows_the_difference(
    test_data_dir: Path, quicken_transaction_export: Path, write_import_map: MapWriter,
) -> None:
    write_import_map("expense_accounts.csv", "Food,Expense_Living\nGifts,-\n")

    dry = _transactions(test_data_dir, quicken_transaction_export, "--dry-run")
    assert dry.exit_code == 0, dry.output
    assert "(dry run)" in dry.output and "1 added" in dry.output
    assert _generated(test_data_dir) == {}

    result = _transactions(test_data_dir, quicken_transaction_export)
    assert result.exit_code == 0, result.output
    assert "warning" not in result.output  # Gifts is skipped on purpose
    assert "best-effort reading of the file: review them (sim scheduled list)" in result.output
    rules = _generated(test_data_dir)
    assert list(rules) == ["baseline_expense_food"]
    food = rules["baseline_expense_food"]
    assert (food["amount"], food["to_account"], food["from_account"]) == \
        ("420.00", "Expense_Living", "Asset_Checking")
    meta = json.loads(food["meta_json"])
    assert meta["source"] == "quicken" and len(meta["files_sha256"]) == 16

    # A different month's spending changes the rule; mapping Gifts adds one.
    quicken_transaction_export.write_text(
        quicken_transaction_export.read_text(encoding="utf-8").replace('"-500.00"', '"-700.00"'),
        encoding="utf-8")
    write_import_map("expense_accounts.csv", "Food,Expense_Living\nGifts,Expense_Living\n")
    rerun = _transactions(test_data_dir, quicken_transaction_export, "--dry-run")
    assert rerun.exit_code == 0, rerun.output
    lines = {line.split()[0]: line.split()[1:] for line in rerun.output.splitlines()
             if line.strip().startswith("baseline_expense_")}
    assert lines == {
        "baseline_expense_food": ["Expense_Living", "420.00", "520.00", "changed"],
        "baseline_expense_gifts": ["Expense_Living", "30.00", "added"],
    }


@pytest.mark.usefixtures("quicken_category_map")
def test_sim_import_transactions_summary_shows_what_the_rules_come_from_and_writes_nothing(
    test_data_dir: Path, quicken_transaction_export: Path,
) -> None:
    # No account to pay from, and no expense accounts: a summary needs neither.
    result = _import(test_data_dir, "transactions", str(quicken_transaction_export), "--summary")

    assert result.exit_code == 0, result.output
    assert "4 transaction(s) used, January 2026 to February 2026 (2 months)" in result.output
    lines = [line.split() for line in result.output.splitlines()]
    assert ["category", "total", "a", "month", "(of", "2)"] in lines
    assert ["Food", "840.00", "420.00"] in lines and ["Gifts", "60.00", "30.00"] in lines
    assert ["all", "900.00", "450.00"] in lines
    assert "can't be told from monthly ones in less than 360 days" in result.output
    assert _generated(test_data_dir) == {}


@pytest.mark.usefixtures("quicken_category_map")
def test_sim_import_transactions_summary_lists_large_payments_that_are_not_monthly(
    test_data_dir: Path, quicken_year_export: Path, write_import_map: MapWriter,
) -> None:
    write_import_map("quicken/categories.csv", "Groceries,Food\nGifts,Gifts\nAuto Insurance,Insurance\n")

    result = _import(test_data_dir, "transactions", str(quicken_year_export), "--summary")

    assert result.exit_code == 0, result.output
    assert "15 transaction(s) used, January 2026 to December 2026 (12 months)" in result.output
    lines = [line.split(maxsplit=3) for line in result.output.splitlines()]
    # What went out and what came in are added up apart.
    assert ["1", "1000.00", "0.00", "moves between your own accounts"] in lines
    assert ["1", "0.00", "5000.00", "money in"] in lines
    # The premium is found; the groceries are monthly, and the small fee isn't worth a rule.
    assert ("  Mutual Insurance (Insurance): Mar 2026 900.00, Sep 2026 900.00  "
            "(not seen to come round again)") in result.output
    assert "Market" not in result.output and "Web Host" not in result.output
    assert "These are candidates" in result.output
    assert "--yearly-script FILE writes both as a script to edit and run." in result.output


@pytest.mark.slow
@pytest.mark.usefixtures("quicken_category_map")
def test_sim_import_transactions_yearly_script_adds_the_rules_and_leaves_the_payee_out_when_run(
    test_data_dir: Path, quicken_year_export: Path, write_import_map: MapWriter, tmp_path: Path,
) -> None:
    write_import_map("quicken/categories.csv", "Groceries,Food\nGifts,Gifts\nAuto Insurance,Insurance\n")
    write_import_map("expense_accounts.csv", "Food,Expense_Living\nInsurance,Expense_Living\nGifts,-\n")
    script = tmp_path / "yearly rules.sh"

    result = _import(test_data_dir, "transactions", str(quicken_year_export), "--yearly-script", str(script),
                     "--from-account", "Asset_Checking")

    # It implies --summary: nothing is written but the script.
    assert result.exit_code == 0, result.output
    assert f"wrote 1 payee(s) as yearly rules to {script}" in result.output
    assert "--yearly-script FILE writes" not in result.output and _generated(test_data_dir) == {}
    text = script.read_text(encoding="utf-8")
    assert "EDIT IT BEFORE RUNNING IT" in text and "EDIT_ME" not in text.split("set -eu")[1]
    assert "# Mutual Insurance (Insurance): Mar 2026 900.00, Sep 2026 900.00" in text
    assert "yearly yearly_mutual_insurance_mar Expense_Living 900.00 3 2026-03 " in text

    # As the household would run it, with `sim` on the PATH.
    env = {**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"}
    ran = subprocess.run(["sh", str(script)], capture_output=True, text=True, env=env, check=False)

    assert ran.returncode == 0, ran.stdout + ran.stderr
    with (test_data_dir / "scenarios" / "retire_alice" / "scheduled.csv").open(newline="", encoding="utf-8") as f:
        rules = {row["name"]: row for row in csv.DictReader(f) if row["name"].startswith("yearly_")}
    assert sorted(rules) == ["yearly_mutual_insurance_mar", "yearly_mutual_insurance_sep"]
    march = rules["yearly_mutual_insurance_mar"]
    assert (march["from_account"], march["to_account"], march["amount"], march["cadence"],
            march["month_of_year"], march["series"], march["base_date"]) == \
        ("Asset_Checking", "Expense_Living", "900.00", "annual", "3", "cpi", "2026-03")
    excluded = test_data_dir / "imports" / "quicken" / "excluded_payees.csv"
    assert excluded.read_text(encoding="utf-8").splitlines()[-1] == r"mutual\s.*insurance"
    # The next import no longer spreads the premium over the months.
    again = _import(test_data_dir, "transactions", str(quicken_year_export), "--summary")
    assert again.exit_code == 0, again.output
    assert "Mutual Insurance" not in again.output and "Insurance" not in again.output.split("category")[1]


@pytest.mark.usefixtures("quicken_category_map")
def test_sim_import_transactions_yearly_script_leaves_what_it_cannot_know_to_be_filled_in(
    test_data_dir: Path, quicken_year_export: Path, quicken_transaction_export: Path,
    write_import_map: MapWriter, tmp_path: Path,
) -> None:
    write_import_map("quicken/categories.csv", "Groceries,Food\nGifts,Gifts\nAuto Insurance,Insurance\n")
    script = tmp_path / "yearly.sh"

    # No account to pay from and no expense account for the category: both are marked.
    result = _import(test_data_dir, "transactions", str(quicken_year_export), "--yearly-script", str(script))

    assert result.exit_code == 0, result.output
    text = script.read_text(encoding="utf-8")
    assert "FROM=EDIT_ME " in text
    assert "# No expense account is saved for Insurance: name one in place of EDIT_ME." in text
    assert "# Not seen to come round again: a yearly bill, or a one-time expense?" in text
    assert "yearly yearly_mutual_insurance_sep EDIT_ME 900.00 9 2026-09 " in text

    # Two months of transactions have no candidates, and write no script.
    short = tmp_path / "short.sh"
    none = _import(test_data_dir, "transactions", str(quicken_transaction_export), "--yearly-script", str(short))
    assert none.exit_code == 0, none.output
    assert f"no candidates for yearly rules: {short} was not written" in none.output and not short.exists()


def test_yearly_script_takes_the_latest_year_and_finds_a_payee_past_its_numbers() -> None:
    seen = Occasional("Taxes", "COUNTY TAX #4411 PMT (2027)", (
        (date(2026, 4, 1), 210_000), (date(2026, 12, 1), 210_000),
        (date(2027, 4, 1), 220_000), (date(2027, 12, 1), 225_000)), repeats=True)

    assert import_yearly.latest_year(seen) == ((date(2027, 4, 1), 220_000), (date(2027, 12, 1), 225_000))
    pattern = import_yearly.payee_pattern(seen.payee)
    assert pattern == r"county\s.*tax\s.*pmt"
    assert re.search(pattern, "County Tax #5522 pmt (2028)", re.IGNORECASE)
    text = import_yearly.script(
        [seen], data_dir=Path("/data dir"), scenario="retire_alice", start="2028-01", from_account=None,
        expense_accounts={"Taxes": "-"}, excluded_payees=Path("/data dir/imports/bank/excluded_payees.csv"))
    # A path with a space, and text with a quote's worth of punctuation, stay one word each.
    assert "DATA_DIR='/data dir'" in text
    assert "yearly yearly_county_tax_4411_pmt_2027_apr EDIT_ME 2200.00 4 2027-04 " \
           "'Yearly: COUNTY TAX #4411 PMT (2027) (Taxes)'" in text
    assert "Not seen to come round again" not in text


def test_sim_import_transactions_needs_the_account_to_pay_from_before_it_reads_a_file(
    test_data_dir: Path, quicken_transaction_export: Path,
) -> None:
    # No category map either, which reading the file would have refused.
    result = _import(test_data_dir, "transactions", str(quicken_transaction_export))

    assert result.exit_code == 2
    assert "Missing option '--from-account'" in result.output and "category map" not in result.output


@pytest.mark.usefixtures("quicken_category_map", "at_a_terminal")
def test_sim_import_transactions_asks_for_the_lot_of_an_investment_account_to_pay_from(
    test_data_dir: Path, quicken_transaction_export: Path, write_import_map: MapWriter,
) -> None:
    write_import_map("expense_accounts.csv", "Food,Expense_Living\nGifts,-\n")

    result = _import(test_data_dir, "transactions", str(quicken_transaction_export),
                     input="Asset_Brokerage_Joint\ndefault\n")

    assert result.exit_code == 0, result.output
    food = _generated(test_data_dir)["baseline_expense_food"]
    assert (food["from_account"], food["from_lot"]) == ("Asset_Brokerage_Joint", "default")


@pytest.mark.usefixtures("quicken_category_map")
def test_sim_import_transactions_holds_a_given_account_to_what_one_asked_for_would_be(
    test_data_dir: Path, quicken_transaction_export: Path,
) -> None:
    def run(*args: str):
        return _import(test_data_dir, "transactions", str(quicken_transaction_export), *args)

    # Each is refused before the file is read: no expense accounts are saved, which would warn.
    mistyped = run("--from-account", "Asset_Nowhere")
    assert mistyped.exit_code != 0 and "unknown from account or chain: 'Asset_Nowhere'" in mistyped.output
    no_lot = run("--from-account", "Asset_Brokerage_Joint")
    assert no_lot.exit_code != 0 and "needs its lot named with --from-lot" in no_lot.output
    assert "warning" not in mistyped.output + no_lot.output


@pytest.mark.usefixtures("quicken_category_map", "at_a_terminal")
def test_sim_import_transactions_asks_for_the_account_to_pay_from(
    test_data_dir: Path, quicken_transaction_export: Path, write_import_map: MapWriter,
) -> None:
    write_import_map("expense_accounts.csv", "Food,Expense_Living\nGifts,-\n")

    result = _import(test_data_dir, "transactions", str(quicken_transaction_export), input="Asset_Checking\n")

    assert result.exit_code == 0, result.output
    assert _generated(test_data_dir)["baseline_expense_food"]["from_account"] == "Asset_Checking"


@pytest.mark.usefixtures("quicken_category_map")
def test_sim_import_transactions_warns_about_a_category_with_no_account(
    test_data_dir: Path, quicken_transaction_export: Path, write_import_map: MapWriter,
) -> None:
    write_import_map("expense_accounts.csv", "Food,Expense_Nowhere\n")

    result = _transactions(test_data_dir, quicken_transaction_export)

    assert result.exit_code == 0, result.output
    assert "ignoring category 'Food'; target account 'Expense_Nowhere' does not exist" in result.output
    assert "ignoring category 'Gifts'; no target expense account mapping exists" in result.output
    assert _generated(test_data_dir) == {}


@pytest.mark.usefixtures("quicken_category_map", "at_a_terminal")
def test_sim_import_transactions_asks_for_expense_accounts_and_saves_them(
    test_data_dir: Path, quicken_transaction_export: Path,
) -> None:
    result = _transactions(test_data_dir, quicken_transaction_export,
                           input="Expense_Living\nskip\n")

    assert result.exit_code == 0, result.output
    assert "category 'Food' (about 420.00 a month) has no expense account" in result.output
    saved = test_data_dir / "imports" / "expense_accounts.csv"
    assert saved.read_text(encoding="utf-8") == "category,account\nFood,Expense_Living\nGifts,-\n"
    assert list(_generated(test_data_dir)) == ["baseline_expense_food"]


@pytest.mark.usefixtures("quicken_category_map", "at_a_terminal")
def test_sim_import_transactions_keeps_a_saved_account_another_scenario_may_use(
    test_data_dir: Path, quicken_transaction_export: Path, write_import_map: MapWriter,
) -> None:
    saved = write_import_map("expense_accounts.csv", "Food,Expense_Nowhere\nGifts,-\n")
    before = saved.read_text(encoding="utf-8")

    result = _transactions(test_data_dir, quicken_transaction_export, input="skip\nn\n")
    assert result.exit_code == 0, result.output
    assert saved.read_text(encoding="utf-8") == before
    assert _generated(test_data_dir) == {}

    result = _transactions(test_data_dir, quicken_transaction_export, input="Expense_Living\ny\n")
    assert result.exit_code == 0, result.output
    assert saved.read_text(encoding="utf-8") == "category,account\nFood,Expense_Living\nGifts,-\n"
    assert list(_generated(test_data_dir)) == ["baseline_expense_food"]


def test_sim_import_transactions_needs_a_category_map(
    test_data_dir: Path, quicken_transaction_export: Path,
) -> None:
    result = _transactions(test_data_dir, quicken_transaction_export)
    assert result.exit_code != 0
    assert "category map not found" in result.output
    assert "imports/quicken/categories.csv" in result.output


@pytest.mark.usefixtures("quicken_category_map")
def test_sim_import_transactions_says_when_an_export_has_no_transactions(
    test_data_dir: Path, quicken_transaction_export: Path, quicken_empty_transaction_export: Path,
    quicken_net_worth_export: Path, write_import_map: MapWriter,
) -> None:
    write_import_map("expense_accounts.csv", "Food,Expense_Living\nGifts,-\n")

    both = _transactions(test_data_dir, quicken_transaction_export,
                         str(quicken_empty_transaction_export))
    assert both.exit_code == 0, both.output
    assert _generated(test_data_dir)["baseline_expense_food"]["amount"] == "420.00"

    for export, layout in ((quicken_empty_transaction_export, ()),
                           (quicken_net_worth_export, ("--source", "quicken"))):
        empty = _transactions(test_data_dir, export, *layout)
        assert empty.exit_code != 0
        assert "no transactions in" in empty.output


# ---------- moving a data repo's old mapping files ----------

def _migration():
    import importlib.util
    import sys

    path = Path(__file__).parents[1] / "scripts" / "migrate_527.py"
    spec = importlib.util.spec_from_file_location("migrate_527", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses look their module up there
    spec.loader.exec_module(module)
    return module


def test_migrate_527_moves_and_merges_the_old_mapping_files(
    test_data_dir: Path, quicken_net_worth_export: Path, quicken_transaction_export: Path,
    write_old_import_map: MapWriter,
) -> None:
    old = [
        write_old_import_map("base", "Asset_Checking,Asset_Checking,map,\n"),
        write_old_import_map(
            "retire_alice",
            "Asset_Checking,Asset_Checking,map,\n"
            "Asset_Brokerage_Joint,Asset_Brokerage_Joint,map,\n"
            "Asset_TraditionalIRA_Alice,Asset_TraditionalIRA_Alice,map,\n"
            "Closed Card,,map,\n"  # a stale row the old importer put up with
            "Liability_Mortgage,,skip,modeled by hand\n"),
        write_old_import_map("expense_category_map.csv",
                             "Groceries,Food\nCoffee Shops,Food\nGifts,-\n"),
        write_old_import_map("expense_category_split.csv", "Food,Checking,Alice Food\n"),
        write_old_import_map("expense_account_map.csv", "Alice Food,Expense_Living\n"),
    ]

    migration = _migration()
    assert migration.main([str(test_data_dir)]) == 0
    assert all(path.exists() for path in old)
    assert not (test_data_dir / "imports").exists()

    assert migration.main([str(test_data_dir), "--write"]) == 0
    assert not any(path.exists() for path in old)
    imports = test_data_dir / "imports"
    assert (imports / "quicken" / "accounts.csv").read_text(encoding="utf-8").splitlines() == [
        "outside_account,asset,action,account,lot,note",
        "Asset_Checking,,map,Asset_Checking,,",
        "Asset_Brokerage_Joint,,map,Asset_Brokerage_Joint,,",
        "Asset_TraditionalIRA_Alice,,map,Asset_TraditionalIRA_Alice,,",
        "Liability_Mortgage,,skip,,,modeled by hand",
    ]
    assert (imports / "quicken" / "splits.csv").read_text(encoding="utf-8") == \
        "category,outside_account,target_category\nFood,Checking,Alice Food\n"

    # The importers read what it wrote.
    result = _import(test_data_dir, "holdings", str(quicken_net_worth_export))
    assert result.exit_code == 0, result.output
    assert "1 skipped" in result.output
    result = _transactions(test_data_dir, quicken_transaction_export)
    assert result.exit_code == 0, result.output
    assert _generated(test_data_dir)["baseline_expense_alice_food"]["amount"] == "420.00"
    assert migration.main([str(test_data_dir)]) == 0  # nothing left to migrate


@pytest.mark.usefixtures("quicken_category_map", "quicken_account_map", "at_a_terminal")
@pytest.mark.parametrize(("where", "rows"), [
    ("base", "Asset_Checking,Asset_Checking,map,\n"),  # not the scenario imported into
    ("expense_category_split.csv", "Food,Checking,Alice Food\n"),
])
def test_sim_import_stops_until_old_mappings_anywhere_are_migrated(
    test_data_dir: Path, quicken_net_worth_export: Path, quicken_transaction_export: Path,
    write_old_import_map: MapWriter, where: str, rows: str,
) -> None:
    old = write_old_import_map(where, rows)

    for result in (_import(test_data_dir, "holdings", str(quicken_net_worth_export)),
                   _transactions(test_data_dir, quicken_transaction_export)):
        assert result.exit_code != 0
        assert "scripts/migrate_527.py" in result.output and str(old) in result.output


def test_migrate_527_stops_when_two_scenarios_map_an_account_differently(
    test_data_dir: Path, capsys: pytest.CaptureFixture[str], write_old_import_map: MapWriter,
) -> None:
    write_old_import_map("base", "Savings,Asset_Checking,map,\n")
    old = write_old_import_map("retire_alice", "Savings,Asset_Brokerage_Joint,map,\n")

    assert _migration().main([str(test_data_dir), "--write"]) == 1

    assert "'Savings' is mapped differently: base: map Asset_Checking; " \
        "retire_alice: map Asset_Brokerage_Joint" in capsys.readouterr().err
    assert old.exists() and not (test_data_dir / "imports").exists()


def test_migrate_527_lets_two_scenarios_skip_an_account_their_own_way(
    test_data_dir: Path, write_old_import_map: MapWriter,
) -> None:
    write_old_import_map("base", "Old Card,,skip,closed\n")
    write_old_import_map("retire_alice", "Old Card,Liability_Mortgage,skip,closed in 2024\n")

    assert _migration().main([str(test_data_dir), "--write"]) == 0

    saved = test_data_dir / "imports" / "quicken" / "accounts.csv"
    assert saved.read_text(encoding="utf-8").splitlines()[1:] == ["Old Card,,skip,,,closed"]


# ---------- what a second review found ----------

def _export_with(export: Path, old: str, new: str) -> Path:
    export.write_text(export.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
    return export


@pytest.mark.usefixtures("test_child_scenario")
def test_sim_import_holdings_writes_into_a_lot_it_zeroed_before(
    test_data_dir: Path, quicken_net_worth_export: Path, write_import_map: MapWriter,
) -> None:
    write_import_map(
        "quicken/accounts.csv",
        "Asset_Checking,,map,Asset_Savings,,\nAsset_Brokerage_Joint,,skip,,,\n"
        "Asset_TraditionalIRA_Alice,,skip,,,\nLiability_Mortgage,,skip,,,\n")
    lots = test_data_dir / "scenarios" / "child" / "account_lots.csv"

    # A zero balance in an inherited lot is the override `lot delete` writes.
    _export_with(quicken_net_worth_export, '"1,234.56"', '"0.00"')
    first = _import(test_data_dir, "holdings", str(quicken_net_worth_export), scenario="child")
    assert first.exit_code == 0, first.output
    assert _lots(test_data_dir, "child")["Asset_Savings"]["opening_balance"] == "0.00"

    _export_with(quicken_net_worth_export, '"0.00"', '"1,234.56"')
    again = _import(test_data_dir, "holdings", str(quicken_net_worth_export), scenario="child")
    assert again.exit_code == 0, again.output
    assert "override" not in again.output
    assert lots.read_text(encoding="utf-8").count("Asset_Savings") == 1
    assert _lots(test_data_dir, "child")["Asset_Savings"]["opening_balance"] == "1234.56"


@pytest.mark.usefixtures("quicken_account_map")
def test_sim_import_holdings_keeps_a_lots_other_columns(
    test_data_dir: Path, quicken_net_worth_export: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    path = retire_alice_csv("account_lots.csv")
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0] + ",note", *(line + ",mine" for line in lines[1:])]) + "\n",
                    encoding="utf-8")

    result = _import(test_data_dir, "holdings", str(quicken_net_worth_export))

    assert result.exit_code == 0, result.output
    rows = _lots(test_data_dir)
    assert rows["Asset_Checking"]["opening_balance"] == "1234.56"
    assert {row["note"] for row in rows.values()} == {"mine"}
    assert rows["Asset_Checking"]["minimum_balance"] == "2000.00"


@pytest.mark.usefixtures("quicken_account_map", "test_child_scenario")
def test_sim_import_holdings_takes_an_accounts_kind_as_the_scenario_loads_it(
    test_data_dir: Path, quicken_net_worth_export: Path,
) -> None:
    # An override that leaves kind and subkind to the parent (ADR-003 §4.5).
    (test_data_dir / "scenarios" / "child" / "accounts.csv").write_text(
        "name,owner\nLiability_Mortgage,joint\n", encoding="utf-8")

    result = _import(test_data_dir, "holdings", str(quicken_net_worth_export), scenario="child")

    assert result.exit_code == 0, result.output
    assert _lots(test_data_dir, "child")["Liability_Mortgage"]["opening_balance"] == "240000.00"


@pytest.mark.usefixtures("at_a_terminal")
@pytest.mark.usefixtures("at_a_terminal")
def test_sim_import_holdings_adds_an_account_named_at_the_mapping_question(
    test_data_dir: Path, quicken_net_worth_export: Path, write_import_map: MapWriter,
    retire_alice_csv: Callable[[str], Path],
) -> None:
    saved = write_import_map(
        "quicken/accounts.csv",
        "Asset_Checking,,map,Asset_Checking,,\nAsset_Brokerage_Joint,,skip,,,\nLiability_Mortgage,,skip,,,\n")
    # The outside IRA is a new savings account here: its name, yes, then what `sim account add` asks.
    answers = "Asset_Savings_New\ny\nasset\ncash\njoint\n"

    result = _import(test_data_dir, "holdings", str(quicken_net_worth_export), input=answers)

    assert result.exit_code == 0, result.output
    assert "or a new name, to add the account to the scenario" in result.output
    assert "appended account 'Asset_Savings_New'" in result.output
    assert "set opening balance now?" not in result.output  # the file has the balance
    with retire_alice_csv("accounts.csv").open(newline="", encoding="utf-8") as f:
        added = next(row for row in csv.DictReader(f) if row["name"] == "Asset_Savings_New")
    assert (added["kind"], added["subkind"], added["owner"]) == ("asset", "cash", "joint")
    assert _lots(test_data_dir)["Asset_Savings_New"]["opening_balance"] == "510000.00"
    assert "Asset_TraditionalIRA_Alice,,map,Asset_Savings_New,," in saved.read_text(encoding="utf-8")


def _map(data: Path, *args: str, input: str | None = None):  # noqa: A002
    return CliRunner().invoke(main, ["sim", "import", "map", *args, "--data-dir", str(data)], input=input)


def test_sim_import_map_lists_the_shared_mappings_and_each_source_s(
    test_data_dir: Path, write_import_map: MapWriter,
) -> None:
    empty = _map(test_data_dir)
    assert empty.exit_code == 0 and "no import mappings are saved yet" in empty.output

    write_import_map("expense_accounts.csv", "Food,Expense_Living\nGifts,-\n")
    write_import_map("bank/payees.csv", "corner market,Food\n")
    write_import_map("quicken/categories.csv", "Groceries,Food\n")

    result = _map(test_data_dir)

    assert result.exit_code == 0, result.output
    lines = [line.split() for line in result.output.splitlines()]
    # The shared file first, then each source's, numbered through.
    assert lines.index(["imports/expense_accounts.csv:"]) < lines.index(["imports/bank/payees.csv:"]) \
        < lines.index(["imports/quicken/categories.csv:"])
    assert ["1", "Food", "Expense_Living"] in lines and ["2", "Gifts", "-"] in lines
    assert ["3", "corner", "market", "Food"] in lines and ["4", "Groceries", "Food"] in lines

    one = _map(test_data_dir, "bank", "--list")
    assert one.exit_code == 0, one.output
    assert "expense_accounts.csv" not in one.output and "quicken" not in one.output
    assert ["1", "corner", "market", "Food"] in [line.split() for line in one.output.splitlines()]

    unknown = _map(test_data_dir, "other")
    assert unknown.exit_code != 0
    assert "no mappings are saved for a source 'other'; there are: bank, quicken" in unknown.output


@pytest.mark.usefixtures("at_a_terminal")
def test_sim_import_map_changes_a_row_removes_one_and_keeps_no_change_the_file_refuses(
    test_data_dir: Path, write_import_map: MapWriter,
) -> None:
    accounts = write_import_map("expense_accounts.csv", "Food,Expense_Living\nGifts,-\n")
    choices = write_import_map("budget_choices.csv", "Food,actual,710.00,420.00\n")

    # Row 2's account, then remove row 1; row 3 (now 2) is given a use the file refuses.
    result = _map(test_data_dir, "--scenario", "retire_alice", input=(
        "9\n2\nchange\nExpense_Housing\n"
        "1\nremove\n"
        "2\nchange\neither\n\n\n"
        "done\n"))

    assert result.exit_code == 0, result.output
    assert "a row's number from the list, or done" in result.output
    assert "category: Gifts, account: -" in result.output
    assert accounts.read_text(encoding="utf-8") == "category,account\nGifts,Expense_Housing\n"
    assert "not kept: " in result.output and "use must be budget or actual, not 'either'" in result.output
    assert choices.read_text(encoding="utf-8") == "category,use,budget,actual\nFood,actual,710.00,420.00\n"

    # --list asks nothing, even at a terminal.
    listed = _map(test_data_dir, "--list")
    assert listed.exit_code == 0 and "row to change" not in listed.output


def test_sim_import_holdings_asks_nothing_of_a_scenario_that_does_not_load(
    test_data_dir: Path, quicken_net_worth_export: Path,
    append_scheduled_row: Callable[..., None],
) -> None:
    append_scheduled_row(name="broken", from_account="No_Such_Account", to_account="Expense_Living",
                         amount_rule_kind="fixed_amount", amount="1.00", start="2026-01")

    result = _import(test_data_dir, "holdings", str(quicken_net_worth_export), input="skip\n" * 4)

    assert result.exit_code != 0
    assert "already fails to load" in result.output
    assert "has no saved mapping" not in result.output
    assert not (test_data_dir / "imports").exists()


@pytest.mark.usefixtures("at_a_terminal")
def test_sim_import_holdings_refuses_a_lot_that_changed_while_it_asked(
    test_data_dir: Path, quicken_net_worth_export: Path, write_import_map: MapWriter,
    retire_alice_csv: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = write_import_map(
        "quicken/accounts.csv",
        "Asset_Checking,,map,Asset_Checking,,\nAsset_Brokerage_Joint,,skip,,,\n"
        "Asset_TraditionalIRA_Alice,,skip,,,\n")
    before = saved.read_text(encoding="utf-8")
    lots = retire_alice_csv("account_lots.csv")
    ask = cli._ask_holding_target

    def edited_meanwhile(*args, **kwargs):
        lots.write_text(lots.read_text(encoding="utf-8").replace("2000.00", "3000.00"),
                        encoding="utf-8")
        return ask(*args, **kwargs)

    monkeypatch.setattr(cli, "_ask_holding_target", edited_meanwhile)
    result = _import(test_data_dir, "holdings", str(quicken_net_worth_export), input="skip\n")

    assert result.exit_code != 0
    assert "changed while this import was being answered" in result.output
    row = _lots(test_data_dir)["Asset_Checking"]
    assert (row["opening_balance"], row["minimum_balance"]) == ("5000.00", "3000.00")
    # The edit was refused, so the answer wasn't saved either.
    assert saved.read_text(encoding="utf-8") == before


def test_sim_import_holdings_tells_a_missing_account_from_a_missing_lot(
    test_data_dir: Path, quicken_net_worth_export: Path, write_import_map: MapWriter,
) -> None:
    rest = "Asset_TraditionalIRA_Alice,,skip,,,\nLiability_Mortgage,,skip,,,\n"
    saved = write_import_map(
        "quicken/accounts.csv",
        "Asset_Checking,,map,Asset_Gone,,\nAsset_Brokerage_Joint,,skip,,,\n" + rest)
    gone = _import(test_data_dir, "holdings", str(quicken_net_worth_export))
    assert gone.exit_code != 0
    assert f"{saved} maps to account(s) retire_alice doesn't have" in gone.output
    assert "Asset_Checking -> Asset_Gone" in gone.output and "lot add" not in gone.output

    write_import_map(
        "quicken/accounts.csv",
        "Asset_Checking,,skip,,,\nAsset_Brokerage_Joint,,map,Asset_Brokerage_Joint,vti,\n" + rest)
    no_lot = _import(test_data_dir, "holdings", str(quicken_net_worth_export))
    assert no_lot.exit_code != 0
    assert "sim account lot add" in no_lot.output
    assert "Asset_Brokerage_Joint -> Asset_Brokerage_Joint:vti" in no_lot.output


def test_sim_import_holdings_says_when_a_report_has_no_balances(
    test_data_dir: Path, quicken_transaction_export: Path,
) -> None:
    result = _import(test_data_dir, "holdings", str(quicken_transaction_export),
                     "--source", "quicken")
    assert result.exit_code != 0
    assert "no account balances in" in result.output


@pytest.mark.usefixtures("quicken_category_map", "test_child_scenario")
def test_sim_import_transactions_shows_the_rules_a_scenario_inherits(
    test_data_dir: Path, quicken_transaction_export: Path, write_import_map: MapWriter,
) -> None:
    write_import_map("expense_accounts.csv", "Food,Expense_Living\nGifts,Expense_Living\n")
    assert _transactions(test_data_dir, quicken_transaction_export).exit_code == 0
    write_import_map("expense_accounts.csv", "Food,Expense_Living\nGifts,-\n")

    result = _import(test_data_dir, "transactions", str(quicken_transaction_export),
                     "--from-account", "Asset_Checking", "--dry-run", scenario="child")

    assert result.exit_code == 0, result.output
    lines = {line.split()[0]: line.split()[1:] for line in result.output.splitlines()
             if line.strip().startswith("baseline_expense_")}
    assert lines == {
        "baseline_expense_food": ["Expense_Living", "420.00", "420.00", "unchanged"],
        "baseline_expense_gifts": ["Expense_Living", "30.00", "30.00", "kept"],
    }
    assert "baseline_expense_gifts is no longer generated, but child inherits it from " \
        "retire_alice" in result.output
    assert "sim scheduled delete baseline_expense_gifts" in result.output


@pytest.mark.usefixtures("quicken_category_map", "at_a_terminal")
def test_sim_import_transactions_does_not_ask_about_the_blank_category(
    test_data_dir: Path, quicken_transaction_export: Path, write_import_map: MapWriter,
) -> None:
    saved = write_import_map("expense_accounts.csv", "Food,Expense_Living\nGifts,-\n")
    before = saved.read_text(encoding="utf-8")
    _export_with(quicken_transaction_export, '"Florist","Gifts"', '"Florist",""')

    result = _transactions(test_data_dir, quicken_transaction_export, input="Expense_Living\n")

    assert result.exit_code == 0, result.output
    assert "expense account for" not in result.output
    assert "ignoring category ''" in result.output
    assert saved.read_text(encoding="utf-8") == before
    assert list(_generated(test_data_dir)) == ["baseline_expense_food"]


@pytest.mark.usefixtures("quicken_category_map", "at_a_terminal")
def test_sim_import_transactions_asks_before_overriding_an_account_named_like_the_category(
    test_data_dir: Path, quicken_transaction_export: Path, write_import_map: MapWriter,
    test_child_scenario: Path,
) -> None:
    saved = write_import_map("expense_accounts.csv", "Food,Expense_Living\n")
    before = saved.read_text(encoding="utf-8")
    header = (test_data_dir / "scenarios" / "retire_alice" / "accounts.csv").read_text(
        encoding="utf-8").splitlines()[0]
    row = dict.fromkeys(header.split(","), "") | {
        "name": "Gifts", "kind": "expense", "subkind": "living", "owner": "joint"}
    (test_child_scenario / "accounts.csv").write_text(
        header + "\n" + ",".join(row.values()) + "\n", encoding="utf-8")

    # In retire_alice, which has no Gifts account: N keeps the answer to this run.
    result = _transactions(test_data_dir, quicken_transaction_export, input="skip\nn\n")
    assert result.exit_code == 0, result.output
    assert "Another scenario has an account named Gifts" in result.output
    assert saved.read_text(encoding="utf-8") == before

    result = _transactions(test_data_dir, quicken_transaction_export, input="skip\ny\n")
    assert result.exit_code == 0, result.output
    assert saved.read_text(encoding="utf-8") == before + "Gifts,-\n"


def test_a_rename_warns_when_a_mapping_file_does_not_read(
    test_data_dir: Path, write_import_map: MapWriter,
) -> None:
    saved = write_import_map("quicken/accounts.csv", "Old Card,,Skip,,,\nBank,,map,Asset_Checking,,\n")
    before = saved.read_text(encoding="utf-8")

    result = CliRunner().invoke(main, [
        "sim", "account", "rename", "Asset_Checking", "Asset_Bank",
        "--scenario", "retire_alice", "--data-dir", str(test_data_dir)])

    assert result.exit_code == 0, result.output
    assert "warning: the saved import mappings still name Asset_Checking, not Asset_Bank" \
        in result.output
    assert "action must be 'map' or 'skip'" in result.output
    assert saved.read_text(encoding="utf-8") == before
    assert "Asset_Bank" in _lots(test_data_dir)


def test_a_rename_follows_a_map_not_yet_migrated_and_a_category_named_like_the_account(
    test_data_dir: Path, write_import_map: MapWriter, write_old_import_map: MapWriter,
) -> None:
    old = write_old_import_map("retire_alice", "Checking,Asset_Checking,map,\n")
    write_import_map("quicken/categories.csv", "Groceries,Expense_Living\n")
    where = ["--scenario", "retire_alice", "--data-dir", str(test_data_dir)]

    result = CliRunner().invoke(main, ["sim", "account", "rename", "Asset_Checking", "Asset_Bank", *where])
    assert result.exit_code == 0, result.output
    assert old.read_text(encoding="utf-8").splitlines()[1] == "Checking,Asset_Bank,map,"

    # The category found the account by its name; now it needs a row to.
    result = CliRunner().invoke(main, ["sim", "account", "rename", "Expense_Living", "Expense_Life", *where])
    assert result.exit_code == 0, result.output
    assert (test_data_dir / "imports" / "expense_accounts.csv").read_text(encoding="utf-8") == \
        "category,account\nExpense_Living,Expense_Life\n"


# ---------- brokerage files: sources described by a profile ----------

def _all_lots(data: Path, scenario: str = "retire_alice") -> dict[tuple[str, str], dict[str, str]]:
    path = data / "scenarios" / scenario / "account_lots.csv"
    with path.open(newline="", encoding="utf-8") as f:
        return {(r["account_name"], r["lot"]): r for r in csv.DictReader(f)}


def _saved(data: Path, source: str) -> list[str]:
    return (data / "imports" / source / "accounts.csv").read_text(encoding="utf-8").splitlines()[1:]


def test_sim_import_sources_lists_the_profiles_and_knows_their_files(
    fidelity_positions_export: Path, vanguard_holdings_export: Path,
) -> None:
    listed = CliRunner().invoke(main, ["sim", "import", "sources"])
    assert listed.exit_code == 0, listed.output
    rows = {line.split()[0]: line for line in listed.output.splitlines()}
    assert "built in" in rows["fidelity"] and "built in" in rows["vanguard"]

    found = CliRunner().invoke(main, ["sim", "import", "sources", str(fidelity_positions_export),
                                      str(vanguard_holdings_export)])
    assert found.output.splitlines() == [
        f"{fidelity_positions_export}: fidelity holdings",
        f"{vanguard_holdings_export}: vanguard holdings",
    ]


def test_sim_import_holdings_asks_about_a_brokerage_files_lots_once(
    test_data_dir: Path, fidelity_positions_export: Path, at_a_terminal: None,
) -> None:
    # Each outside account, then: a lot for each of the brokerage's holdings
    # (n), the Roth's added together (y), and the offered name for each lot.
    result = _import(test_data_dir, "holdings", str(fidelity_positions_export),
                     input="Asset_Brokerage_Joint\nAsset_Roth_Alice\nn\ny\n\n\n\n")

    assert result.exit_code == 0, result.output
    assert "'111111111' (Joint brokerage)" in result.output
    assert "fidelity snapshot as of 2026-01-05" in result.output
    lots = _all_lots(test_data_dir)
    fund = lots["Asset_Brokerage_Joint", "fxaix"]
    assert (fund["symbol"], fund["quantity"], fund["opening_balance"], fund["opening_cost_basis"]) == (
        "FXAIX", "1000.5", "200100.00", "150000.00")
    assert lots["Asset_Brokerage_Joint", "fskax"]["opening_cost_basis"] == "80000.00"
    cash = lots["Asset_Brokerage_Joint", "spaxx"]
    assert (cash["symbol"], cash["quantity"], cash["opening_balance"], cash["opening_cost_basis"]) == (
        "SPAXX", "", "44900.00", "")
    # The lot the holdings were split out of is emptied, not counted twice.
    old = lots["Asset_Brokerage_Joint", "default"]
    assert (old["opening_balance"], old["opening_cost_basis"]) == ("0.00", "0.00")
    assert "[split into lots]" in result.output
    assert "note: Asset_Brokerage_Joint:default is emptied" in result.output
    # A broker's cost basis isn't a Roth's contributions.
    roth = lots["Asset_Roth_Alice", "default"]
    assert (roth["opening_balance"], roth["opening_cost_basis"]) == ("90000.00", "")
    assert "is a Roth lot with no basis" in result.output
    assert "the plan doesn't know FSKAX, FXAIX, SPAXX yet" in result.output
    assert _saved(test_data_dir, "fidelity") == [
        "111111111,,map,Asset_Brokerage_Joint,,", "222222222,,map,Asset_Roth_Alice,default,"]

    # The next file finds every lot by its symbol, and asks nothing.
    fidelity_positions_export.write_text(
        fidelity_positions_export.read_text(encoding="utf-8").replace('"$75,000.00"', '"$76,500.00"'),
        encoding="utf-8")
    again = _import(test_data_dir, "holdings", str(fidelity_positions_export))
    assert again.exit_code == 0, again.output
    assert "updated 1 lot(s)" in again.output
    assert _all_lots(test_data_dir)["Asset_Brokerage_Joint", "fskax"]["opening_balance"] == "76500.00"


def test_sim_import_holdings_takes_an_existing_lot_or_skip_for_a_holding(
    test_data_dir: Path, fidelity_positions_export: Path, at_a_terminal: None,
    write_import_map: MapWriter,
) -> None:
    write_import_map("fidelity/accounts.csv",
                     "111111111,,map,Asset_Brokerage_Joint,,\n222222222,,skip,,,\n")

    # A lot for each; FXAIX gets one, FSKAX goes into it too, the cash is left out.
    result = _import(test_data_dir, "holdings", str(fidelity_positions_export),
                     input="n\nindex\nindex\nskip\n")

    assert result.exit_code == 0, result.output
    index = _all_lots(test_data_dir)["Asset_Brokerage_Joint", "index"]
    assert (index["symbol"], index["opening_balance"]) == ("FXAIX", "275100.00")
    # Two funds in one lot have no one share count; what they cost adds up.
    assert (index["quantity"], index["opening_cost_basis"]) == ("", "230000.00")
    assert _saved(test_data_dir, "fidelity")[2:] == [
        "111111111,FSKAX,map,Asset_Brokerage_Joint,index,", "111111111,SPAXX,skip,,,"]


def test_sim_import_holdings_new_lots_needs_no_terminal(
    test_data_dir: Path, vanguard_holdings_export: Path, write_import_map: MapWriter,
) -> None:
    write_import_map("vanguard/accounts.csv", "12345678,,map,Asset_Roth_Bill,,\n")

    refused = _import(test_data_dir, "holdings", str(vanguard_holdings_export))
    assert refused.exit_code != 0
    assert "--new-lots" in refused.output and "12345678 / VTSAX" in refused.output

    result = _import(test_data_dir, "holdings", str(vanguard_holdings_export), "--new-lots")
    assert result.exit_code == 0, result.output
    assert "3 new" in result.output
    lots = _all_lots(test_data_dir)
    stock = lots["Asset_Roth_Bill", "vtsax"]
    # The new lot is the kind the account's lot is; a Roth lot's basis isn't the broker's.
    assert (stock["subkind"], stock["symbol"], stock["quantity"], stock["opening_balance"],
            stock["opening_cost_basis"]) == ("roth_ira", "VTSAX", "2500", "400000.00", "")
    assert lots["Asset_Roth_Bill", "vmfxx"]["opening_balance"] == "20000.00"
    assert "lot 'vtsax' of Asset_Roth_Bill is a Roth lot with no basis" in result.output
    # Nothing in the file says whose it is.
    assert "vanguard account '12345678', from the file's name" in result.output


def test_sim_import_holdings_new_lots_finds_a_free_name(
    test_data_dir: Path, vanguard_holdings_export: Path, write_import_map: MapWriter,
    add_account_value: Callable[..., None],
) -> None:
    write_import_map("vanguard/accounts.csv", "12345678,,map,Asset_Roth_Bill,,\n")
    add_account_value("Asset_Roth_Bill", lot="vtsax", subkind="roth_ira", symbol="OTHER",
                      opening_balance="5.00")

    result = _import(test_data_dir, "holdings", str(vanguard_holdings_export), "--new-lots")

    assert result.exit_code == 0, result.output
    assert _all_lots(test_data_dir)["Asset_Roth_Bill", "vtsax-2"]["symbol"] == "VTSAX"


def test_sim_import_holdings_new_lots_makes_one_lot_of_a_symbol_and_asks_of_words(
    test_data_dir: Path, vanguard_two_row_export: Path, write_import_map: MapWriter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_import_map("vanguard/accounts.csv", "12345678,,map,Asset_Roth_Bill,,\n")

    refused = _import(test_data_dir, "holdings", str(vanguard_two_row_export), "--new-lots")
    assert refused.exit_code != 0
    # Only the row no lot can be named for is left to say.
    assert refused.output.count("-> Asset_Roth_Bill") == 1 and "Pending activity" in refused.output

    monkeypatch.setattr(cli, "_interactive", lambda: True)
    result = _import(test_data_dir, "holdings", str(vanguard_two_row_export), "--new-lots", input="skip\n")
    assert result.exit_code == 0, result.output
    lots = _all_lots(test_data_dir)
    # Both rows of the fund, in the one lot made for it.
    assert (lots["Asset_Roth_Bill", "vtsax"]["quantity"], lots["Asset_Roth_Bill", "vtsax"]["opening_balance"]) \
        == ("2600", "416000.00")
    assert ("Asset_Roth_Bill", "vtsax-2") not in lots
    assert "12345678,Pending activity,skip,,," in _saved(test_data_dir, "vanguard")


def test_sim_import_holdings_dry_run_adds_no_lot(
    test_data_dir: Path, vanguard_holdings_export: Path, write_import_map: MapWriter,
) -> None:
    write_import_map("vanguard/accounts.csv", "12345678,,map,Asset_Roth_Bill,,\n")
    before = _all_lots(test_data_dir)

    result = _import(test_data_dir, "holdings", str(vanguard_holdings_export), "--new-lots", "--dry-run")

    assert result.exit_code == 0, result.output
    assert "[new lot]" in result.output and "(dry run)" in result.output
    assert _all_lots(test_data_dir) == before


def test_sim_import_holdings_outside_account_names_a_files_account(
    test_data_dir: Path, vanguard_holdings_export: Path, fidelity_positions_export: Path,
    write_import_map: MapWriter,
) -> None:
    write_import_map("vanguard/accounts.csv", "Alice IRA,,map,Asset_TraditionalIRA_Alice,default,\n")

    result = _import(test_data_dir, "holdings", str(vanguard_holdings_export),
                     "--outside-account", "Alice IRA")
    assert result.exit_code == 0, result.output
    assert _all_lots(test_data_dir)["Asset_TraditionalIRA_Alice", "default"]["opening_balance"] == "520000.00"

    refused = _import(test_data_dir, "holdings", str(fidelity_positions_export),
                      "--outside-account", "Alice IRA")
    assert refused.exit_code != 0
    assert "names its accounts itself" in refused.output


def test_sim_import_holdings_asks_which_account_a_file_without_its_number_is(
    test_data_dir: Path, vanguard_holdings_export: Path, write_import_map: MapWriter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_import_map("vanguard/accounts.csv", "Bill Roth,,map,Asset_Roth_Bill,,\n")
    renamed = vanguard_holdings_export.rename(vanguard_holdings_export.with_name("holdings.csv"))

    refused = _import(test_data_dir, "holdings", str(renamed), "--new-lots")
    assert refused.exit_code != 0
    assert "doesn't say which account it is" in refused.output and "--outside-account" in refused.output

    monkeypatch.setattr(cli, "_interactive", lambda: True)
    result = _import(test_data_dir, "holdings", str(renamed), "--new-lots", input="Bill Roth\n")
    assert result.exit_code == 0, result.output
    # What it holds says which account the file is, and the ones imported before are offered.
    assert "holdings.csv doesn't say which vanguard account it is; it holds VTSAX, VBTLX, VMFXX." \
        in result.output
    assert "One you have imported (Bill Roth)" in result.output
    assert "from the file's name" not in result.output
    assert _all_lots(test_data_dir)["Asset_Roth_Bill", "vtsax"]["opening_balance"] == "400000.00"


def test_sim_import_holdings_offers_a_share_count_from_a_stored_price(
    test_data_dir: Path, fidelity_positions_export: Path, write_import_map: MapWriter,
    add_account_value: Callable[..., None], write_aux: Callable[[str, str], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_import_map("fidelity/accounts.csv",
                     "111111111,,map,Asset_Brokerage_Joint,,\n222222222,,skip,,,\n")
    # The file's money market row has a balance and neither shares nor a price.
    add_account_value("Asset_Brokerage_Joint", lot="spaxx", symbol="SPAXX", quantity="44000",
                      opening_balance="44000.00")
    write_aux("security_price__SPAXX.csv", "effective_date,price\n2026-01-02,1.00\n")

    unasked = _import(test_data_dir, "holdings", str(fidelity_positions_export), "--new-lots", "--dry-run")
    assert unasked.exit_code == 0, unasked.output
    assert "[the file has no share count]" in unasked.output
    assert "Asset_Brokerage_Joint:spaxx could be worked out" in unasked.output

    monkeypatch.setattr(cli, "_interactive", lambda: True)
    result = _import(test_data_dir, "holdings", str(fidelity_positions_export), "--new-lots", input="y\n")
    assert result.exit_code == 0, result.output
    assert "At SPAXX's price of 1 on 2026-01-02, that is 44900.000 shares; the lot has 44000." \
        in result.output
    money = _all_lots(test_data_dir)["Asset_Brokerage_Joint", "spaxx"]
    assert (money["quantity"], money["opening_balance"]) == ("44900", "44900.00")


def test_sim_import_holdings_year_end_adds_no_lots(
    test_data_dir: Path, vanguard_holdings_export: Path, write_import_map: MapWriter,
) -> None:
    write_import_map("vanguard/accounts.csv", "12345678,,map,Asset_Roth_Bill,,\n")

    result = _import(test_data_dir, "holdings", str(vanguard_holdings_export), "--year-end", "--new-lots")

    assert result.exit_code != 0
    assert "import the current holdings first" in result.output


def test_sim_import_holdings_reads_a_households_own_profile(
    test_data_dir: Path, tmp_path: Path, write_import_profile: Callable[[str, str], Path],
    write_import_map: MapWriter,
) -> None:
    profile = write_import_profile("credit_union", (
        "description: Credit union balances\n"
        "holdings:\n"
        "  layout: 'Accounts: Export'\n"
        "  columns: {account: Share, value: Balance, asset: Kind}\n"
        "  blank: [Savings]\n"
    ))
    write_import_map("credit_union/accounts.csv", "S1,,map,Asset_Savings,,\n")
    export = tmp_path / "shares.csv"
    export.write_text("Share,Kind,Balance\nS1,Savings,\"$21,500.00\"\n", encoding="utf-8")

    listed = CliRunner().invoke(main, ["sim", "import", "sources", "--data-dir", str(test_data_dir)])
    assert listed.exit_code == 0, listed.output
    assert any(line.split()[:1] == ["credit_union"] and str(profile.name) in line
               for line in listed.output.splitlines())

    result = _import(test_data_dir, "holdings", str(export))
    assert result.exit_code == 0, result.output
    assert _lots(test_data_dir)["Asset_Savings"]["opening_balance"] == "21500.00"


def test_sim_import_names_a_profile_it_cant_use_and_an_unknown_source(
    test_data_dir: Path, fidelity_positions_export: Path, write_import_profile: Callable[[str, str], Path],
) -> None:
    unknown = _import(test_data_dir, "holdings", str(fidelity_positions_export), "--source", "nowhere")
    assert unknown.exit_code != 0
    assert "unknown source 'nowhere'" in unknown.output and "fidelity" in unknown.output

    profile = write_import_profile("my_bank", "holdings:\n  columns: {asset: Symbol, value: Worth, colour: X}\n")
    # It stops an import that names it, and no other source's.
    result = _import(test_data_dir, "holdings", str(fidelity_positions_export), "--source", "my_bank")
    assert result.exit_code != 0
    assert str(profile) in result.output and "unknown column(s) ['colour']" in result.output
    other = _import(test_data_dir, "holdings", str(fidelity_positions_export), "--dry-run")
    assert "unmapped fidelity account(s)" in other.output
    listed = CliRunner().invoke(main, ["sim", "import", "sources", "--data-dir", str(test_data_dir)])
    assert listed.exit_code == 0 and f"warning: not used: {profile}" in listed.output


def test_sim_import_holdings_prune_empties_the_lot_a_file_no_longer_has(
    test_data_dir: Path, fidelity_positions_export: Path, write_import_map: MapWriter,
) -> None:
    write_import_map("fidelity/accounts.csv",
                     "111111111,,map,Asset_Brokerage_Joint,,\n222222222,,skip,,,\n")

    first = _import(test_data_dir, "holdings", str(fidelity_positions_export), "--new-lots")
    assert first.exit_code == 0, first.output

    # FSKAX is sold: the next file doesn't have it.
    text = fidelity_positions_export.read_text(encoding="utf-8").splitlines(keepends=True)
    fidelity_positions_export.write_text(
        "".join(line for line in text if ",FSKAX," not in line), encoding="utf-8")
    kept = _import(test_data_dir, "holdings", str(fidelity_positions_export), "--dry-run")
    assert kept.exit_code == 0, kept.output
    assert "lot 'fskax' of Asset_Brokerage_Joint isn't in the fidelity file and keeps" in kept.output
    assert "--prune empties such lots" in kept.output

    result = _import(test_data_dir, "holdings", str(fidelity_positions_export), "--prune")
    assert result.exit_code == 0, result.output
    assert "[not in the file]" in result.output and "1 emptied" in result.output
    sold = _all_lots(test_data_dir)["Asset_Brokerage_Joint", "fskax"]
    assert (sold["opening_balance"], sold["quantity"], sold["opening_cost_basis"]) == ("0.00", "0", "0.00")

    refused = _import(test_data_dir, "holdings", str(fidelity_positions_export), "--prune", "--year-end")
    assert refused.exit_code != 0 and "doesn't go with --year-end" in refused.output


def test_sim_import_holdings_warns_of_a_file_dated_far_from_the_start(
    test_data_dir: Path, fidelity_positions_export: Path, write_import_map: MapWriter,
) -> None:
    import datetime as dt

    write_import_map("fidelity/accounts.csv", "111111111,,skip,,,\n222222222,,skip,,,\n")
    with (test_data_dir / "scenarios" / "base" / "scenario.csv").open(encoding="utf-8") as f:
        # The balances are the ones the first month opens with, on its first day.
        start = dt.date.fromisoformat(next(csv.DictReader(f))["start"][:10]).replace(day=1)

    def dated(day: dt.date) -> str:
        text = fidelity_positions_export.read_text(encoding="utf-8")
        was = text[text.index("Date downloaded "):].split(" at ")[0]
        fidelity_positions_export.write_text(
            text.replace(was, f"Date downloaded {day:%b-%d-%Y}"), encoding="utf-8")
        result = _import(test_data_dir, "holdings", str(fidelity_positions_export), "--dry-run")
        assert result.exit_code == 0, result.output
        return result.output

    # A download is rarely made on the day itself.
    assert "warning: the file is as of" not in dated(start + dt.timedelta(days=5))
    # The start month's last day, which scenario.start is pinned to, is not it.
    assert "warning: the file is as of" in dated(start + dt.timedelta(days=29))
    late = start + dt.timedelta(days=40)
    output = dated(late)
    assert f"the file is as of {late}, and its balances are being set as of {start}" in output
    assert "sim scenario roll" in output


# ---------- transactions from a bank's file (issue #617) ----------

_BANK_PAYEES = "corner market,Food\ncity power,Utilities\ntransfer to savings,transfer\ncheck,-\n"


def test_sim_import_transactions_reads_a_banks_file_by_its_payees(
    test_data_dir: Path, bank_checking_export: Path, write_import_map: MapWriter,
) -> None:
    write_import_map("expense_accounts.csv", "Food,Expense_Living\nUtilities,Expense_Housing\n")

    refused = _transactions(test_data_dir, bank_checking_export)
    assert refused.exit_code != 0
    # The most paid first, so the file can be written from the top.
    listed = [line.strip() for line in refused.output.splitlines() if "payment(s)" in line]
    assert [line.split(" (")[1] for line in listed] == [
        "1 payment(s), 1000.00)", "2 payment(s), 600.00)", "2 payment(s), 300.00)",
        "1 payment(s), 75.00)"]
    assert "payees.csv" in refused.output and _generated(test_data_dir) == {}

    write_import_map("wells_fargo/payees.csv", _BANK_PAYEES)
    result = _transactions(test_data_dir, bank_checking_export)
    assert result.exit_code == 0, result.output
    rules = _generated(test_data_dir)
    # The move to savings and the check aren't spending; no category went to Other.
    assert {name: (rule["amount"], rule["to_account"]) for name, rule in rules.items()} == {
        "baseline_expense_food": ("300.00", "Expense_Living"),
        "baseline_expense_utilities": ("150.00", "Expense_Housing"),
    }
    assert "warning" not in result.output


@pytest.mark.usefixtures("at_a_terminal")
def test_sim_import_transactions_summary_asks_nothing(
    test_data_dir: Path, bank_checking_export: Path,
) -> None:
    # No input to read: a question, about a payee or which account the file is, would end the run.
    result = _import(test_data_dir, "transactions", str(bank_checking_export), "--summary")

    assert result.exit_code == 0, result.output
    assert "payee(s) not asked about went to Other" in result.output
    assert "has no category" not in result.output and "not saved" not in result.output
    assert not (test_data_dir / "imports" / "wells_fargo" / "payees.csv").exists()


@pytest.mark.usefixtures("at_a_terminal")
def test_sim_import_transactions_asks_about_payees_and_saves_the_answers(
    test_data_dir: Path, bank_checking_export: Path, write_import_map: MapWriter,
) -> None:
    write_import_map("expense_accounts.csv", "Food,Expense_Living\nUtilities,Expense_Housing\nOther,-\n")
    saved = test_data_dir / "imports" / "wells_fargo" / "payees.csv"

    # The transfer; the market, by two of its words; then the rest unasked.
    # The file's account, then the payees.
    answers = "Joint checking\ntransfer\n\nFood\ncorner market\nrest\n"
    dry = _transactions(test_data_dir, bank_checking_export, "--dry-run", input=answers)
    assert dry.exit_code == 0, dry.output
    assert "'ONLINE TRANSFER TO SAVINGS REF #IB0XYZ12', 1 payment(s), 1000.00 in all" in dry.output
    assert "2 payee(s) not asked about went to Other, 375.00 in all" in dry.output
    assert "the payee categories you gave were not saved" in dry.output and not saved.exists()
    assert "Checking1.csv (wells_fargo) doesn't say which account it is." in dry.output

    result = _transactions(test_data_dir, bank_checking_export, input=answers)
    assert result.exit_code == 0, result.output
    assert saved.read_text(encoding="utf-8").splitlines() == [
        "payee_pattern,category", "online transfer to savings ref,transfer", "corner market,Food"]
    assert _generated(test_data_dir)["baseline_expense_food"]["amount"] == "300.00"

    # Words the payee doesn't have are asked for again.
    fussy = _transactions(test_data_dir, bank_checking_export, "--dry-run",
                          input="\nUtilities\nelectric\ncity pow\ncity power\n-\n\n")
    assert fussy.exit_code == 0, fussy.output
    # Nor is part of a word enough: it would be found in other payees' words.
    assert fussy.output.count("whole words found together in 'city power and water bill pay'") == 2


@pytest.mark.usefixtures("quicken_category_map")
def test_sim_import_transactions_makes_one_set_of_rules_from_several_sources(
    test_data_dir: Path, bank_checking_export: Path, quicken_transaction_export: Path,
    write_import_map: MapWriter,
) -> None:
    write_import_map("expense_accounts.csv", "Food,Expense_Living\nUtilities,Expense_Housing\nGifts,-\n")
    write_import_map("wells_fargo/payees.csv", _BANK_PAYEES)

    result = _import(test_data_dir, "transactions", str(bank_checking_export),
                     str(quicken_transaction_export), "--from-account", "Asset_Checking")

    assert result.exit_code == 0, result.output
    rules = _generated(test_data_dir)
    # Food from both: (300 + 40 + 500) from one and (400 + 200) from the other, over two months.
    assert rules["baseline_expense_food"]["amount"] == "720.00"
    assert json.loads(rules["baseline_expense_food"]["meta_json"])["source"] == "wells_fargo+quicken"


def test_sim_import_transactions_reads_a_cards_categories_and_its_payees_over_them(
    test_data_dir: Path, card_exports: tuple[Path, Path, Path], write_import_map: MapWriter,
) -> None:
    detailed, february, _ = card_exports
    write_import_map("expense_accounts.csv", "Food,Expense_Living\nTreats,Expense_Living\n")
    write_import_map(
        "amex/categories.csv", "Merchandise & Supplies-Groceries,Food\nRestaurant-Restaurant,Food\n")
    write_import_map("amex/payees.csv", "harbor cafe,Treats\n")

    result = _import(test_data_dir, "transactions", str(detailed), str(february),
                     "--from-account", "Asset_Checking")

    assert result.exit_code == 0, result.output
    assert "1 transaction(s) that an earlier file had too" in result.output
    # January 25 once: (80 + 60) over two months; the cafe by its payee, not the card's category.
    assert {name: rule["amount"] for name, rule in _generated(test_data_dir).items()} == {
        "baseline_expense_food": "70.00", "baseline_expense_treats": "20.00"}


def test_sim_import_transactions_finds_a_short_card_files_rows_by_payee_beside_a_detailed_one(
    test_data_dir: Path, card_exports: tuple[Path, Path, Path], write_import_map: MapWriter,
) -> None:
    detailed, _, short = card_exports
    write_import_map("expense_accounts.csv", "Food,Expense_Living\n")
    write_import_map(
        "amex/categories.csv", "Merchandise & Supplies-Groceries,Food\nRestaurant-Restaurant,Food\n")

    # The short download's charge has no category, and isn't quietly left without one.
    refused = _import(test_data_dir, "transactions", str(detailed), str(short),
                      "--from-account", "Asset_Checking")
    assert refused.exit_code != 0
    assert "1 payee(s) with no category" in refused.output and "CORNER MARKET 204" in refused.output

    write_import_map("amex/payees.csv", "corner market,Food\n")
    result = _import(test_data_dir, "transactions", str(detailed), str(short),
                     "--from-account", "Asset_Checking")
    assert result.exit_code == 0, result.output
    # (80 + 40) in January and 60 in February.
    assert {name: rule["amount"] for name, rule in _generated(test_data_dir).items()} == {
        "baseline_expense_food": "90.00"}


# ---------- a budget (issue #617) ----------

_BOTH_ACCOUNTS = ("Food,Expense_Living\nGifts,Expense_Living\nUtilities,Expense_Housing\n"
                  "Property Tax,Expense_Housing\n")


def _with_budget(data: Path, export: Path, budget: Path, *args: str, input: str | None = None):  # noqa: A002
    return _import(data, "transactions", str(export), "--budget", str(budget),
                   "--from-account", "Asset_Checking", *args, input=input)


def _amounts(data: Path) -> dict[str, tuple[str, str]]:
    return {name: (row["amount"], row["cadence"]) for name, row in _generated(data).items()}


@pytest.mark.usefixtures("quicken_category_map")
def test_sim_import_transactions_with_a_budget_makes_each_category_from_the_one_preferred(
    test_data_dir: Path, quicken_transaction_export: Path, quicken_budget_full_export: Path,
    write_import_map: MapWriter,
) -> None:
    write_import_map("expense_accounts.csv", _BOTH_ACCOUNTS)

    # Food is in both, and nothing says which to use.
    undecided = _with_budget(test_data_dir, quicken_transaction_export, quicken_budget_full_export)
    assert undecided.exit_code != 0
    assert "no choice of budget or actual is saved for: Food; give --prefer" in undecided.output
    assert _generated(test_data_dir) == {}

    result = _with_budget(test_data_dir, quicken_transaction_export, quicken_budget_full_export,
                          "--prefer", "budget")

    assert result.exit_code == 0, result.output
    lines = [line.split(maxsplit=4) for line in result.output.splitlines()]
    assert ["Food", "710.00", "420.00", "-290.00", "?"] in lines
    assert ["Gifts", "30.00", "actual", "(the", "only one)"] in lines
    assert "the budget's income is not used here" in result.output
    # The budget's yearly payments come with a category taken from it.
    assert _amounts(test_data_dir) == {
        "baseline_expense_food": ("710.00", "monthly"),
        "baseline_expense_gifts": ("30.00", "monthly"),
        "baseline_expense_property_tax_apr": ("3000.00", "annual"),
        "baseline_expense_property_tax_dec": ("3000.00", "annual"),
        "baseline_expense_utilities": ("165.00", "monthly"),
    }
    rules = _generated(test_data_dir)
    assert json.loads(rules["baseline_expense_food"]["meta_json"])["source"] == "quicken budget"
    assert json.loads(rules["baseline_expense_gifts"]["meta_json"])["source"] == "quicken"
    # A preference is not a choice made: nothing is saved.
    assert not (test_data_dir / "imports" / "budget_choices.csv").exists()

    actual = _with_budget(test_data_dir, quicken_transaction_export, quicken_budget_full_export,
                          "--prefer", "actual")
    assert actual.exit_code == 0, actual.output
    assert _amounts(test_data_dir)["baseline_expense_food"] == ("420.00", "monthly")


@pytest.mark.usefixtures("quicken_category_map", "at_a_terminal")
def test_sim_import_transactions_with_a_budget_asks_once_and_again_when_an_amount_moves(
    test_data_dir: Path, quicken_transaction_export: Path, quicken_budget_full_export: Path,
    write_import_map: MapWriter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_import_map("expense_accounts.csv", _BOTH_ACCOUNTS)
    saved = test_data_dir / "imports" / "budget_choices.csv"

    dry = _with_budget(test_data_dir, quicken_transaction_export, quicken_budget_full_export, "--dry-run",
                       input="actual\n")
    assert dry.exit_code == 0, dry.output
    assert "(dry run) your choices of budget or actual were not saved" in dry.output and not saved.exists()

    asked = _with_budget(test_data_dir, quicken_transaction_export, quicken_budget_full_export,
                         input="actual\n")

    assert asked.exit_code == 0, asked.output
    assert "Food: budget 710.00 or actual 420.00 a month?" in asked.output
    assert saved.read_text(encoding="utf-8") == "category,use,budget,actual\nFood,actual,710.00,420.00\n"
    assert _amounts(test_data_dir)["baseline_expense_food"] == ("420.00", "monthly")

    # The same files again: the saved choice stands, and nothing is asked.
    again = _with_budget(test_data_dir, quicken_transaction_export, quicken_budget_full_export)
    assert again.exit_code == 0, again.output
    assert "a month?" not in again.output
    assert ["Food", "710.00", "420.00", "-290.00", "actual (saved)"] in \
        [line.split(maxsplit=4) for line in again.output.splitlines()]

    # More spent on food, by more than 5%: asked again, the saved choice offered.
    quicken_transaction_export.write_text(
        quicken_transaction_export.read_text(encoding="utf-8").replace('"-500.00"', '"-700.00"'),
        encoding="utf-8")
    moved = _with_budget(test_data_dir, quicken_transaction_export, quicken_budget_full_export,
                         input="all-budget\n")
    assert moved.exit_code == 0, moved.output
    assert "Food: budget 710.00 or actual 520.00 a month?" in moved.output and "[actual]" in moved.output
    assert saved.read_text(encoding="utf-8") == "category,use,budget,actual\nFood,budget,710.00,520.00\n"
    assert _amounts(test_data_dir)["baseline_expense_food"] == ("710.00", "monthly")

    # Away from a terminal a moved amount keeps the saved choice, and says so.
    quicken_transaction_export.write_text(
        quicken_transaction_export.read_text(encoding="utf-8").replace('"-700.00"', '"-900.00"'),
        encoding="utf-8")
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    away = _with_budget(test_data_dir, quicken_transaction_export, quicken_budget_full_export)
    assert away.exit_code == 0, away.output
    assert "note: Food uses the saved choice, budget, though an amount has moved more than 5%" in away.output


@pytest.mark.usefixtures("quicken_category_map")
def test_sim_import_transactions_summary_with_a_budget_shows_the_two_and_asks_nothing(
    test_data_dir: Path, quicken_transaction_export: Path, quicken_budget_full_export: Path,
) -> None:
    result = _import(test_data_dir, "transactions", str(quicken_transaction_export), "--summary",
                     "--budget", str(quicken_budget_full_export))

    assert result.exit_code == 0, result.output
    lines = [line.split(maxsplit=4) for line in result.output.splitlines()]
    assert ["category", "budget", "actual", "difference", "use"] in lines
    assert ["Food", "710.00", "420.00", "-290.00", "?"] in lines
    assert ["Utilities", "165.00", "budget", "(the", "only one)"] in lines
    assert _generated(test_data_dir) == {}

    alone = _import(test_data_dir, "transactions", str(quicken_transaction_export), "--summary",
                    "--prefer", "budget")
    assert alone.exit_code == 2 and "--prefer goes with --budget" in alone.output


def test_sim_import_transactions_with_a_budget_refuses_a_choice_it_cannot_read(
    test_data_dir: Path, quicken_transaction_export: Path, quicken_budget_full_export: Path,
    write_import_map: MapWriter, quicken_category_map: Path,
) -> None:
    write_import_map("expense_accounts.csv", _BOTH_ACCOUNTS)
    write_import_map("budget_choices.csv", "Food,either,710.00,420.00\n")

    result = _with_budget(test_data_dir, quicken_transaction_export, quicken_budget_full_export)

    assert result.exit_code != 0
    assert "budget_choices.csv row 2: use must be budget or actual, not 'either'" in result.output


def _budget(data: Path, export: Path, *args: str, input: str | None = None):  # noqa: A002
    return _import(data, "budget", str(export), "--from-account", "Asset_Checking", *args, input=input)


_BUDGET_ACCOUNTS = "Food,Expense_Living\nUtilities,Expense_Housing\nProperty Tax,Expense_Housing\n"


def test_sim_import_budget_pays_monthly_categories_monthly_and_yearly_ones_in_their_months(
    test_data_dir: Path, quicken_budget_full_export: Path, quicken_budget_summary_export: Path,
    write_import_map: MapWriter,
) -> None:
    write_import_map("expense_accounts.csv", _BUDGET_ACCOUNTS)

    dry = _budget(test_data_dir, quicken_budget_full_export, "--dry-run")
    assert dry.exit_code == 0, dry.output
    assert "would replace" in dry.output and _generated(test_data_dir) == {}

    result = _budget(test_data_dir, quicken_budget_full_export)
    assert result.exit_code == 0, result.output
    rules = _generated(test_data_dir)
    # With no category map, what is under Food is Food's. Utilities at its average month.
    assert {name: (rule["cadence"], rule["month_of_year"], rule["amount"], rule["to_account"])
            for name, rule in rules.items()} == {
        "baseline_expense_food": ("monthly", "", "710.00", "Expense_Living"),
        "baseline_expense_utilities": ("monthly", "", "165.00", "Expense_Housing"),
        "baseline_expense_property_tax_dec": ("annual", "12", "3000.00", "Expense_Housing"),
        "baseline_expense_property_tax_apr": ("annual", "4", "3000.00", "Expense_Housing"),
    }
    assert json.loads(rules["baseline_expense_food"]["meta_json"])["source"] == "quicken budget"
    # Income is offered, never assumed.
    assert "The budget has income: 1 category, 60000.00 over its period. It is left out" in result.output
    assert "copy the budget" in result.output

    # The Summary form has no months, so the tax is spread over the year.
    spread = _budget(test_data_dir, quicken_budget_summary_export)
    assert spread.exit_code == 0, spread.output
    rules = _generated(test_data_dir)
    assert rules["baseline_expense_property_tax"]["amount"] == "500.00"
    assert "baseline_expense_property_tax_dec" not in rules and "removed" in spread.output


def test_sim_import_budget_gathers_categories_by_the_category_map(
    test_data_dir: Path, quicken_budget_full_export: Path, write_import_map: MapWriter,
) -> None:
    write_import_map("expense_accounts.csv", _BUDGET_ACCOUNTS + "Dining,Expense_Living\n")
    # A row covers what is under its category; '-' leaves one out.
    write_import_map("quicken/categories.csv", "Food:Restaurants,Dining\nProperty Tax,-\n")

    result = _budget(test_data_dir, quicken_budget_full_export)

    assert result.exit_code == 0, result.output
    assert {name: rule["amount"] for name, rule in _generated(test_data_dir).items()} == {
        "baseline_expense_food": "510.00", "baseline_expense_dining": "200.00",
        "baseline_expense_utilities": "165.00"}


@pytest.mark.usefixtures("at_a_terminal")
def test_sim_import_budget_asks_about_income_and_adds_an_expense_account(
    test_data_dir: Path, quicken_budget_full_export: Path, write_import_map: MapWriter,
) -> None:
    write_import_map("expense_accounts.csv", "Food,Expense_Living\nUtilities,Expense_Housing\n")
    # Use the income, which is take-home pay; for the tax, an asset and a chain are refused,
    # then a new account of the property tax subkind; the paycheck's account.
    answers = ("y\nn\nAsset_Savings\nasset_checking_liquidity\nExpense_Property_Tax\ny\ntaxes_property\n"
               "Income_Salary_Alice\n")

    result = _budget(test_data_dir, quicken_budget_full_export, input=answers)

    assert result.exit_code == 0, result.output
    assert "added expense account Expense_Property_Tax" in result.output
    assert "Asset_Savings is an account or chain of the scenario, and not an expense account" in result.output
    assert "Add expense account asset_checking_liquidity" not in result.output
    accounts = (test_data_dir / "scenarios" / "retire_alice" / "accounts.csv").read_text(encoding="utf-8")
    assert "Expense_Property_Tax,expense,taxes_property,joint" in accounts
    assert _generated(test_data_dir)["baseline_expense_property_tax_dec"]["to_account"] \
        == "Expense_Property_Tax"
    with (test_data_dir / "scenarios" / "retire_alice" / "scheduled.csv").open(
            newline="", encoding="utf-8") as f:
        income = [row for row in csv.DictReader(f) if "generated:income_baseline" in row["tags"]]
    assert [(row["name"], row["from_account"], row["to_account"], row["amount"], row["tax_character"],
             row["person"]) for row in income] == [
        ("baseline_income_paycheck", "Income_Salary_Alice", "Asset_Checking", "5000.00", "none",
         "alice")]
    saved = test_data_dir / "imports"
    assert "Property Tax,Expense_Property_Tax" in (saved / "expense_accounts.csv").read_text(encoding="utf-8")
    assert (saved / "income_accounts.csv").read_text(encoding="utf-8").splitlines() == [
        "category,account", "Paycheck,Income_Salary_Alice"]

    # The account's rename follows into the saved income accounts.
    renamed = CliRunner().invoke(main, [
        "sim", "account", "rename", "Income_Salary_Alice", "Income_Pay_Alice",
        "--scenario", "retire_alice", "--data-dir", str(test_data_dir)])
    assert renamed.exit_code == 0, renamed.output
    assert "imports/income_accounts.csv" in renamed.output
    assert (saved / "income_accounts.csv").read_text(encoding="utf-8").splitlines()[1] \
        == "Paycheck,Income_Pay_Alice"

    # A rerun that declines the income leaves the income rule alone.
    again = _budget(test_data_dir, quicken_budget_full_export, input="n\n")
    assert again.exit_code == 0, again.output
    assert "baseline_income_paycheck" in (
        test_data_dir / "scenarios" / "retire_alice" / "scheduled.csv").read_text(encoding="utf-8")


def _income_rules(data: Path) -> list[dict[str, str]]:
    with (data / "scenarios" / "retire_alice" / "scheduled.csv").open(newline="", encoding="utf-8") as f:
        return [row for row in csv.DictReader(f) if "generated:income_baseline" in row["tags"]]


def test_sim_import_budget_income_is_take_home_pay_unless_said_to_be_gross(
    test_data_dir: Path, quicken_budget_full_export: Path, write_import_map: MapWriter,
) -> None:
    write_import_map("expense_accounts.csv", _BUDGET_ACCOUNTS)
    write_import_map("income_accounts.csv", "Paycheck,Income_Salary_Alice\n")

    net = _budget(test_data_dir, quicken_budget_full_export, "--income")
    assert net.exit_code == 0, net.output
    assert "written as take-home pay, untaxed" in net.output
    assert [row["tax_character"] for row in _income_rules(test_data_dir)] == ["none"]

    gross = _budget(test_data_dir, quicken_budget_full_export, "--income", "--gross-income")
    assert gross.exit_code == 0, gross.output
    assert [row["tax_character"] for row in _income_rules(test_data_dir)] == ["ordinary"]


def test_sim_import_budget_from_a_chain_needs_an_income_account_only_for_income(
    test_data_dir: Path, quicken_budget_full_export: Path, write_import_map: MapWriter,
) -> None:
    write_import_map("expense_accounts.csv", _BUDGET_ACCOUNTS)
    write_import_map("income_accounts.csv", "Paycheck,Income_Salary_Alice\n")
    chain = ("budget", str(quicken_budget_full_export), "--from-account", "asset_checking_liquidity")

    spending = _import(test_data_dir, *chain)
    assert spending.exit_code == 0, spending.output
    assert _generated(test_data_dir)["baseline_expense_food"]["from_account"] == "asset_checking_liquidity"

    refused = _import(test_data_dir, *chain, "--income")
    assert refused.exit_code != 0
    assert "'asset_checking_liquidity' is a chain; name one with --income-to" in refused.output

    named = _import(test_data_dir, *chain, "--income", "--income-to", "Asset_Checking")
    assert named.exit_code == 0, named.output
    assert [row["to_account"] for row in _income_rules(test_data_dir)] == ["Asset_Checking"]


def test_sim_import_budget_pays_income_into_the_lot_the_spending_comes_from(
    test_data_dir: Path, quicken_budget_full_export: Path, write_import_map: MapWriter,
) -> None:
    write_import_map("expense_accounts.csv", _BUDGET_ACCOUNTS)
    write_import_map("income_accounts.csv", "Paycheck,Income_Salary_Alice\n")

    result = _import(test_data_dir, "budget", str(quicken_budget_full_export), "--income",
                     "--from-account", "Asset_Brokerage_Joint", "--from-lot", "default")

    assert result.exit_code == 0, result.output
    assert [(row["to_account"], row["to_lot"]) for row in _income_rules(test_data_dir)] == [
        ("Asset_Brokerage_Joint", "default")]


def test_a_rename_gives_an_income_category_named_like_the_account_a_row(
    test_data_dir: Path, quicken_budget_full_export: Path, write_import_map: MapWriter,
) -> None:
    write_import_map("expense_accounts.csv", _BUDGET_ACCOUNTS)
    where = ["--scenario", "retire_alice", "--data-dir", str(test_data_dir)]
    # The budget's category is Paycheck: an account of that name needs no row.
    renamed = CliRunner().invoke(main, ["sim", "account", "rename", "Income_Salary_Alice", "Paycheck", *where])
    assert renamed.exit_code == 0, renamed.output
    assert _budget(test_data_dir, quicken_budget_full_export, "--income").exit_code == 0
    assert [row["from_account"] for row in _income_rules(test_data_dir)] == ["Paycheck"]

    result = CliRunner().invoke(main, ["sim", "account", "rename", "Paycheck", "Income_Pay_Alice", *where])

    assert result.exit_code == 0, result.output
    assert (test_data_dir / "imports" / "income_accounts.csv").read_text(encoding="utf-8") \
        == "category,account\nPaycheck,Income_Pay_Alice\n"
