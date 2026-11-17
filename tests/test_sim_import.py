"""The `sim import` commands (issue #527)."""

from __future__ import annotations

import csv
import dataclasses
import json
import os
import subprocess
import re
import sys
from collections.abc import Callable, Collection, Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.expenses.baseline import Occasional
from analysis.sim import cli
from analysis.sim.imports import holdings as import_holdings
from analysis.sim.imports import ofx as import_ofx
from analysis.sim.imports import sources as import_sources
from analysis.sim.imports import mapping as import_mapping
from analysis.sim.imports import yearly as import_yearly


MapWriter = Callable[[str, str], Path]
Rewrite = Callable[..., Path]


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
    answers = "nope\nAsset_TraditionalIRA_Alice\n\nskip\n"

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
                     input="Asset_Brokerage_Joint\n\nn\n")
    assert result.exit_code == 0, result.output
    assert "is mapped to Asset_Brokerage, which retire_alice doesn't have" in result.output
    assert "saved mappings" not in result.output
    assert quicken_account_map.read_text(encoding="utf-8") == before
    assert _lots(test_data_dir)["Asset_Brokerage_Joint"]["opening_balance"] == "310000.00"

    # Y replaces it for every scenario.
    result = _import(test_data_dir, "holdings", str(quicken_net_worth_export),
                     input="Asset_Brokerage_Joint\n\ny\n")
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
    return _import(data, "transactions", str(export), "--from", "Asset_Checking", *args,
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
                     "--from", "Asset_Checking")

    # It implies --summary: nothing is written but the script.
    assert result.exit_code == 0, result.output
    # Seen in one year only, the premium may be a one-time expense: it waits for a choice.
    assert (f"wrote {script}: 0 payee(s) as yearly rules ready to run, 1 commented out and waiting "
            "for your choice") in result.output and "1 noted" not in result.output
    assert "--yearly-script FILE writes" not in result.output and _generated(test_data_dir) == {}
    # The command to copy is one word for the shell, though the name has a space.
    assert result.output.rstrip().endswith(f"run it with: sh '{script}'")
    text = script.read_text(encoding="utf-8")
    assert "EDIT IT BEFORE RUNNING IT" in text and "EDIT_ME" not in text.split("set -eu")[1]
    # It runs the `sim` that wrote it, whatever the PATH it is run with.
    assert f"SIM=${{SIM:-{Path(sys.executable).parent / 'sim'}}}" in text
    assert "# Mutual Insurance (Insurance): Mar 2026 900.00, Sep 2026 900.00" in text
    assert "# yearly yearly_mutual_insurance_mar Expense_Living 900.00 3 2026-03 " in text
    # Run as written, it does nothing.
    env = {**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"}
    unchosen = subprocess.run(["sh", str(script)], capture_output=True, text=True, env=env, check=False)
    assert unchosen.returncode == 0, unchosen.stdout + unchosen.stderr
    assert "yearly_" not in (test_data_dir / "scenarios" / "retire_alice" / "scheduled.csv").read_text(encoding="utf-8")
    assert not (test_data_dir / "imports" / "quicken" / "excluded_payees.csv").exists()

    # The household chooses: a bill that comes round, so every line of it. Then runs it, with `sim` on the PATH.
    script.write_text("\n".join(line.removeprefix("# ") if line.startswith(("# yearly ", "# leave_out ")) else line
                                for line in text.splitlines()) + "\n", encoding="utf-8")
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
    assert excluded.read_text(encoding="utf-8").splitlines()[-1] == import_yearly.payee_pattern("mutual insurance")
    # Run again, it adds neither the rules nor the row a second time.
    rerun = subprocess.run(["sh", str(script)], capture_output=True, text=True, env=env, check=False)
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert rerun.stdout.splitlines() == [
        "already added: yearly_mutual_insurance_mar", "already added: yearly_mutual_insurance_sep"]
    assert excluded.read_text(encoding="utf-8").count("mutual") == 1
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
    assert "# Not seen to come round again: a bill, or a one-time expense? Choose, and uncomment:" in text
    assert "# yearly yearly_mutual_insurance_sep EDIT_ME 900.00 9 2026-09 " in text

    # The file is one to edit: a second run doesn't replace it unasked.
    script.write_text("# edited\n", encoding="utf-8")
    again = _import(test_data_dir, "transactions", str(quicken_year_export), "--yearly-script", str(script))
    assert again.exit_code != 0
    assert "exists, and may hold your edits; --overwrite replaces it" in again.output
    assert script.read_text(encoding="utf-8") == "# edited\n"
    forced = _import(test_data_dir, "transactions", str(quicken_year_export), "--yearly-script", str(script),
                     "--overwrite")
    assert forced.exit_code == 0 and "EDIT IT BEFORE RUNNING IT" in script.read_text(encoding="utf-8")
    alone = _import(test_data_dir, "transactions", str(quicken_year_export), "--summary", "--overwrite")
    assert alone.exit_code == 2 and "--overwrite goes with --yearly-script" in alone.output

    # An account to pay from is checked as an import's is, though a summary needs none.
    unknown = _import(test_data_dir, "transactions", str(quicken_year_export), "--yearly-script", str(script),
                      "--overwrite", "--from", "Asset_Nowhere")
    assert unknown.exit_code != 0 and "unknown from account or chain: 'Asset_Nowhere'" in unknown.output
    no_lot = _import(test_data_dir, "transactions", str(quicken_year_export), "--yearly-script", str(script),
                     "--overwrite", "--from", "Asset_Brokerage_Joint")
    assert no_lot.exit_code != 0 and "needs its lot named with --from-lot" in no_lot.output
    lot_alone = _import(test_data_dir, "transactions", str(quicken_year_export), "--yearly-script", str(script),
                        "--overwrite", "--from-lot", "default")
    assert lot_alone.exit_code == 2 and "--from-lot goes with --from" in lot_alone.output
    assert "EDIT IT BEFORE RUNNING IT" in script.read_text(encoding="utf-8")

    # Two months of transactions have no candidates, and write no script.
    short = tmp_path / "short.sh"
    none = _import(test_data_dir, "transactions", str(quicken_transaction_export), "--yearly-script", str(short))
    assert none.exit_code == 0, none.output
    assert f"no candidates for yearly rules: {short} was not written" in none.output and not short.exists()


def _seen(
    category: str, payee: str, *paid: tuple[date, int],
    repeats: bool = True, sources: tuple[str, ...] = (), elsewhere: bool = False,
) -> Occasional:
    return Occasional(category, payee, paid, repeats, sources=sources, elsewhere=elsewhere)


def _script(
    *seen: Occasional, expense_accounts: Mapping[str, str] | None = None,
    accounts: Collection[str] = (), taken: Collection[str] = (),
) -> tuple[str, int]:
    """The script for ``seen``, and how many payees have live commands."""
    text, written, _ = import_yearly.script(
        seen, data_dir=Path("/data dir"), scenario="retire_alice", start="2028-01", from_account=None,
        expense_accounts=expense_accounts or {}, accounts=accounts, source="bank", taken=taken)
    return text, written


def test_yearly_script_takes_the_latest_year_and_names_a_rule_past_the_payee_s_numbers() -> None:
    seen = _seen("Taxes", "COUNTY TAX #4411 PMT (2027)",
                 (date(2026, 4, 1), 210_000), (date(2026, 12, 1), 210_000),
                 (date(2027, 4, 1), 220_000), (date(2027, 12, 1), 225_000), sources=("card",))

    assert import_yearly.latest_year(seen) == ((date(2027, 4, 1), 220_000), (date(2027, 12, 1), 225_000))
    text, written = _script(seen)

    assert written == 1
    # A path with a space, and text with a quote's worth of punctuation, stay one word each.
    assert "DATA_DIR='/data dir'" in text
    # Neither the reference nor the year is in the rule's name: the next export's differ.
    assert "yearly yearly_county_tax_pmt_apr EDIT_ME 2200.00 4 2027-04 " \
           "'Yearly: COUNTY TAX #4411 PMT (2027) (Taxes)'" in text
    assert "Not seen to come round again" not in text
    # The row goes to the source the payments came from, not the run's first.
    assert "leave_out card " in text and "leave_out bank " not in text


def test_yearly_script_s_pattern_finds_the_payee_s_own_key_and_no_other() -> None:
    def found(pattern: str, payee: str) -> bool:
        return bool(re.search(pattern, payee, re.IGNORECASE))

    pattern = import_yearly.payee_pattern("COUNTY TAX #4411 PMT (2027)")
    # Its dates and reference numbers change, before, between and after its words.
    assert found(pattern, "County Tax #5522 pmt (2028)") and found(pattern, "11/02 county tax pmt")
    # Any other word makes another payee, with a key of its own.
    assert not found(pattern, "ACH county tax pmt") and not found(pattern, "Twin County Taxes Pmt")
    store = import_yearly.payee_pattern("Corner Store")
    assert found(store, "CORNER STORE #114") and found(store, "corner 22 store")
    for other in ("CORNER STORE DELI", "THE CORNER STORE", "CORNER LIQUOR STORE", "CORNER STOREFRONT"):
        assert not found(store, other)
    # One word does not find every payee that has it, nor part of a word; punctuation is kept.
    art = import_yearly.payee_pattern("ART 123")
    assert found(art, "art 456") and not found(art, "WALMART 456") and not found(art, "ART SUPPLY")
    dotted = import_yearly.payee_pattern("Roofing Co.")
    assert found(dotted, "ROOFING CO. 77") and not found(dotted, "ROOFING COX 77")
    # It finds its own payee where folding case would change the letters.
    assert found(import_yearly.payee_pattern("STRAßE GMBH 123"), "Straße GmbH 456")  # cspell:ignore Straße
    # What it finds is what `payee_key` reads the same.
    assert all(found(import_yearly.payee_pattern(text), text)
               for text in ("A+ (Plumbing) [24h]", "Bill's $ Shop | 7", "x.y*z ^ w?"))


def test_yearly_script_counts_a_bill_that_moved_a_month_once() -> None:
    # Paid in April, and the next year at the end of March: one premium, not two.
    moved = _seen("Insurance", "Mutual Insurance", (date(2025, 4, 1), 88_000), (date(2026, 3, 1), 90_000))
    assert import_yearly.latest_year(moved) == ((date(2026, 3, 1), 90_000),)
    # Two installments, the second a month early this year.
    twice = _seen("Taxes", "County Tax", (date(2025, 12, 1), 200_000), (date(2026, 4, 1), 210_000),
                  (date(2026, 11, 1), 220_000))
    assert import_yearly.latest_year(twice) == ((date(2026, 4, 1), 210_000), (date(2026, 11, 1), 220_000))
    text, _ = _script(moved)
    assert [line.split()[1] for line in text.splitlines() if line.startswith("yearly ")] == ["yearly_mutual_insurance"]


def test_yearly_script_keeps_a_payee_s_line_break_out_of_the_commands() -> None:
    text, _ = _script(_seen("Home\nRepair", "Roofing Co.\ntouch /tmp/x #", (date(2027, 5, 1), 900_000), repeats=False))

    block = text.split("\npay_card() {")[1].split("\n}\n", 1)[1]
    assert all(line.startswith("#") for line in block.splitlines() if line)
    assert "'Yearly: Roofing Co. touch /tmp/x # (Home Repair)'" in block


def test_yearly_script_names_no_rule_as_one_the_scenario_has() -> None:
    text, _ = _script(_seen("Taxes", "County Tax", (date(2027, 4, 1), 220_000)),
                      _seen("Insurance", "Mutual Insurance", (date(2027, 3, 1), 90_000)),
                      taken=["yearly_county_tax", "yearly_county_tax_taxes", "baseline_expense_food"])

    names = [line.split()[1] for line in text.splitlines() if line.startswith("yearly yearly_")]
    assert names == ["yearly_county_tax_2", "yearly_mutual_insurance"]


def test_yearly_script_finds_a_category_s_account_as_the_import_does() -> None:
    premium = _seen("Expense_Insurance", "Mutual Insurance", (date(2027, 3, 1), 90_000))
    dropped = _seen("Gifts", "Big Gift", (date(2027, 6, 1), 80_000))
    mapped = _seen("Taxes", "County Tax", (date(2027, 4, 1), 220_000))

    text, written = _script(premium, dropped, mapped, accounts=["Expense_Insurance", "Expense_Housing"],
                            expense_accounts={"Gifts": "-", "Taxes": "Expense_Housing"})

    # A category with the name of one of the scenario's accounts needs no row.
    assert "yearly yearly_mutual_insurance Expense_Insurance 900.00 3 2027-03 " in text
    assert "yearly yearly_county_tax Expense_Housing 2200.00 4 2027-04 " in text
    assert "EDIT_ME" not in text.split("\nyearly() {")[1]
    # '-' is the household's choice of no rule: nothing spreads it, and nothing is added.
    assert "# Left out: expense_accounts.csv gives Gifts no rule ('-')" in text
    assert "big_gift" not in text and "big\\s" not in text
    assert written == 2

    # The saved rows are every scenario's: an account this one lacks is no account to pay.
    other, _ = _script(mapped, accounts=["Expense_Living"], expense_accounts={"Taxes": "Expense_Housing"})
    assert "# Taxes is saved as Expense_Housing, which retire_alice doesn't have: name one" in other
    assert "yearly yearly_county_tax EDIT_ME 2200.00 4 2027-04 " in other


def test_yearly_script_gives_every_rule_a_name_of_its_own() -> None:
    long = "Northern Regional Mutual Property And Casualty Insurance"
    text, _ = _script(
        _seen("Insurance", "Store", (date(2027, 3, 1), 90_000)),
        _seen("Home", "Store", (date(2027, 5, 1), 90_000)),
        _seen("Insurance", f"{long} Auto", (date(2027, 3, 1), 90_000)),
        _seen("Insurance", f"{long} Home", (date(2027, 3, 1), 90_000)),
        _seen("Insurance", f"{long} Boat", (date(2027, 3, 1), 90_000)))

    names = [line.split()[1] for line in text.splitlines() if line.startswith("yearly yearly_")]
    assert len(names) == len(set(names)) == 5
    cut = "yearly_northern_regional_mutual_propert"  # its first 32 characters; cspell:ignore propert
    assert names == ["yearly_store", "yearly_store_home", cut, f"{cut}_insurance", f"{cut}_2"]


def test_yearly_script_makes_a_payee_not_seen_again_a_choice_and_runs_only_one_that_was() -> None:
    tax = _seen("Taxes", "County Tax", (date(2026, 4, 1), 210_000), (date(2026, 12, 1), 210_000),
                (date(2027, 4, 1), 220_000), (date(2027, 12, 1), 220_000))
    water = _seen("Utilities", "Water District", *((date(2027, month, 1), 30_000 + month) for month in (1, 4, 7, 10)),
                  repeats=False)
    roof = _seen("Home", "Roofing Co.", (date(2027, 5, 1), 900_000), repeats=False)

    text, written, waiting = import_yearly.script(
        (tax, water, roof), data_dir=Path("/data"), scenario="retire_alice", start="2028-01",
        from_account="Asset_Checking", expense_accounts={}, source="bank")

    assert (written, waiting) == (1, 2)
    live = [line.split()[1] for line in text.splitlines() if line.startswith(("yearly ", "leave_out "))]
    assert live == ["yearly_county_tax_apr", "yearly_county_tax_dec", "bank"]
    # A bill the household has rules for already keeps only the row that leaves it out.
    assert "If you already have rules for it, delete its `yearly` lines" in text
    # A quarterly bill is a rule for each of its four months, each for that quarter's amount.
    held = [line.split()[2:6] for line in text.splitlines() if line.startswith("# yearly ")]
    assert held == [
        ["yearly_water_district_jan", "EDIT_ME", "300.01", "1"],
        ["yearly_water_district_apr", "EDIT_ME", "300.04", "4"],
        ["yearly_water_district_jul", "EDIT_ME", "300.07", "7"],
        ["yearly_water_district_oct", "EDIT_ME", "300.10", "10"],
        ["yearly_roofing_co", "EDIT_ME", "9000.00", "5"]]
    assert text.count("# leave_out bank ") == 2
    assert text.count("#   only `leave_out`, for a one-time expense or a bill you already have rules for;") == 2


def test_yearly_script_comments_out_a_payee_that_is_paid_in_other_categories_too() -> None:
    text, written = _script(_seen("Electronics", "Corner Store", (date(2027, 3, 1), 90_000), elsewhere=True),
                            expense_accounts={"Electronics": "Expense_Living"}, accounts=["Expense_Living"])

    assert written == 0
    assert "# Commented out: this payee is paid in other categories too" in text
    assert "# yearly yearly_corner_store Expense_Living 900.00 3 2027-03 " in text
    assert "# leave_out bank " in text
    assert not [line for line in text.splitlines() if line.startswith(("yearly yearly_", "leave_out "))]


def test_sim_import_transactions_needs_the_account_to_pay_from_before_it_reads_a_file(
    test_data_dir: Path, quicken_transaction_export: Path,
) -> None:
    # No category map either, which reading the file would have refused.
    result = _import(test_data_dir, "transactions", str(quicken_transaction_export))

    assert result.exit_code == 2
    assert "Missing option '--from'" in result.output and "category map" not in result.output


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
    mistyped = run("--from", "Asset_Nowhere")
    assert mistyped.exit_code != 0 and "unknown from account or chain: 'Asset_Nowhere'" in mistyped.output
    no_lot = run("--from", "Asset_Brokerage_Joint")
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
    assert "a new name adds the account to the scenario" in result.output
    assert "appended account 'Asset_Savings_New'" in result.output
    assert "set opening balance now?" not in result.output  # the file has the balance
    with retire_alice_csv("accounts.csv").open(newline="", encoding="utf-8") as f:
        added = next(row for row in csv.DictReader(f) if row["name"] == "Asset_Savings_New")
    assert (added["kind"], added["subkind"], added["owner"]) == ("asset", "cash", "joint")
    assert _lots(test_data_dir)["Asset_Savings_New"]["opening_balance"] == "510000.00"
    assert "Asset_TraditionalIRA_Alice,,map,Asset_Savings_New,," in saved.read_text(encoding="utf-8")


@pytest.mark.usefixtures("at_a_terminal")
def test_sim_import_holdings_asks_again_for_a_slip_at_a_new_account_s_questions(
    test_data_dir: Path, quicken_net_worth_export: Path, write_import_map: MapWriter,
    retire_alice_csv: Callable[[str], Path],
) -> None:
    saved = write_import_map(
        "quicken/accounts.csv",
        "Asset_Checking,,map,Asset_Checking,,\nAsset_Brokerage_Joint,,skip,,,\nLiability_Mortgage,,skip,,,\n")
    # An expense account's name can't take the balance or be added; then a
    # new name, with a slip at its subkind and one at its owner.
    answers = "Expense_Living\nAsset_Savings_New\ny\nasset\nnothing\ncash\nnobody\njoint\n"

    result = _import(test_data_dir, "holdings", str(quicken_net_worth_export), input=answers)

    assert result.exit_code == 0, result.output
    assert "Expense_Living is already an account or chain of the scenario" in result.output
    assert "Add account Expense_Living?" not in result.output
    with retire_alice_csv("accounts.csv").open(newline="", encoding="utf-8") as f:
        added = next(row for row in csv.DictReader(f) if row["name"] == "Asset_Savings_New")
    assert (added["kind"], added["subkind"], added["owner"]) == ("asset", "cash", "joint")
    assert "Asset_TraditionalIRA_Alice,,map,Asset_Savings_New,," in saved.read_text(encoding="utf-8")


@pytest.mark.usefixtures("at_a_terminal")
def test_sim_import_holdings_adds_no_account_for_a_slip_in_a_name(
    test_data_dir: Path, quicken_net_worth_export: Path, write_import_map: MapWriter,
    retire_alice_csv: Callable[[str], Path],
) -> None:
    write_import_map(
        "quicken/accounts.csv",
        "Asset_Checking,,map,Asset_Checking,,\nAsset_Brokerage_Joint,,skip,,,\nLiability_Mortgage,,skip,,,\n")
    # A slip and Enter: not added. Then yes by mistake, and back at its kind.
    answers = "Asset_Checks\n\nAsset_Checks\ny\nback\nskip\n"

    result = _import(test_data_dir, "holdings", str(quicken_net_worth_export), input=answers)

    assert result.exit_code == 0, result.output
    assert "not added" not in result.output  # going back is no error
    assert "Asset_Checks" not in retire_alice_csv("accounts.csv").read_text(encoding="utf-8")


@pytest.mark.usefixtures("at_a_terminal")
def test_sim_import_holdings_shows_no_holding_s_type_as_the_account_s(
    test_data_dir: Path, vanguard_holdings_export: Path,
) -> None:
    result = _import(test_data_dir, "holdings", str(vanguard_holdings_export), input="skip\n")

    assert result.exit_code == 0, result.output
    assert "a new name adds the account to the scenario" in result.output
    assert "the file calls this one" not in result.output


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


@pytest.mark.usefixtures("at_a_terminal")
def test_sim_import_map_asks_about_an_account_the_scenario_does_not_have(
    test_data_dir: Path, write_import_map: MapWriter,
) -> None:
    accounts = write_import_map("expense_accounts.csv", "Food,Expense_Living\n")

    # A slip is asked about and, not kept, asked for again; one kept on
    # purpose (another scenario's account) is saved.
    result = _map(test_data_dir, "--scenario", "retire_alice", input=(
        "1\nchange\nExpense_Missing\nn\nExpense_Housing\n"
        "1\nchange\nExpense_Elsewhere\ny\n"
        "done\n"))

    assert result.exit_code == 0, result.output
    assert "Expense_Missing is not one of retire_alice's accounts that fit here. Keep it?" in result.output
    assert "saved" in result.output
    assert accounts.read_text(encoding="utf-8") == "category,account\nFood,Expense_Elsewhere\n"


@pytest.mark.usefixtures("at_a_terminal")
def test_sim_import_map_forgets_a_change_the_file_refused(
    test_data_dir: Path, write_import_map: MapWriter,
) -> None:
    choices = write_import_map("budget_choices.csv", "Food,actual,710.00,420.00\nRent,actual,900.00,950.00\n")

    # Food's use is refused, then its budget (no amount); Rent's change is good.
    result = _map(test_data_dir, "--scenario", "retire_alice", input=(
        "1\nchange\neither\n\n\n"
        "1\nchange\n\ninf\n\n"
        "2\nchange\nbudget\n\n\n"
        "done\n"))

    assert result.exit_code == 0, result.output
    assert result.output.count("not kept: ") == 2
    assert "budget and actual are dollar amounts" in result.output
    # The row is shown as the file has it, not as the refused change left it.
    assert "use: either" not in result.output
    assert choices.read_text(encoding="utf-8") == (
        "category,use,budget,actual\nFood,actual,710.00,420.00\nRent,budget,900.00,950.00\n")


@pytest.mark.usefixtures("at_a_terminal")
def test_sim_import_map_changes_a_row_beside_a_file_that_does_not_read(
    test_data_dir: Path, write_import_map: MapWriter,
) -> None:
    write_import_map("bank/splits.csv", "Food,Checking,\n")  # no target category
    payees = write_import_map("bank/payees.csv", "corner market,Food\n")

    result = _map(test_data_dir, "bank", "--scenario", "retire_alice", input="2\nchange\nHome\ndone\n")

    assert result.exit_code == 0, result.output
    assert "no names to complete with: " in result.output
    assert payees.read_text(encoding="utf-8") == "payee_pattern,category\ncorner market,Home\n"


@pytest.mark.usefixtures("at_a_terminal")
def test_sim_import_map_checks_no_account_of_a_scenario_that_does_not_load(
    test_data_dir: Path, write_import_map: MapWriter, append_scheduled_row: Callable[..., None],
) -> None:
    accounts = write_import_map("expense_accounts.csv", "Food,Expense_Living\n")
    append_scheduled_row(name="broken", from_account="No_Such_Account", to_account="Expense_Living",
                         amount_rule_kind="fixed_amount", amount="1.00", start="2026-01")

    result = _map(test_data_dir, "--scenario", "retire_alice", input="1\nchange\nExpense_Housing\ndone\n")

    assert result.exit_code == 0, result.output
    assert "no scenario to check an account against or complete its name from: " in result.output
    assert "Keep it?" not in result.output
    assert accounts.read_text(encoding="utf-8") == "category,account\nFood,Expense_Housing\n"


@pytest.mark.usefixtures("at_a_terminal")
def test_sim_import_map_writes_over_nothing_written_since_it_listed(
    test_data_dir: Path, write_import_map: MapWriter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    payees = write_import_map("bank/payees.csv", "corner market,Food\n")
    asked = cli._ask_name

    def meanwhile(what: str, *args: object, **kwargs: object) -> str:
        # Another command saves a row while this one waits at its question.
        if what == "choice":
            payees.write_text(payees.read_text(encoding="utf-8") + "city water,Home\n", encoding="utf-8")
        return asked(what, *args, **kwargs)  # ty: ignore[invalid-argument-type]  # passed through as given

    monkeypatch.setattr(cli, "_ask_name", meanwhile)
    result = _map(test_data_dir, "bank", "--scenario", "retire_alice", input="1\nremove\ndone\n")

    assert result.exit_code == 0, result.output
    assert "has changed since it was listed" in result.output
    assert payees.read_text(encoding="utf-8") == "payee_pattern,category\ncorner market,Food\ncity water,Home\n"
    assert "city water" in result.output  # listed again, as it is now


@pytest.mark.usefixtures("at_a_terminal")
def test_sim_import_map_writes_nothing_for_a_change_that_changes_nothing(
    test_data_dir: Path, write_import_map: MapWriter,
) -> None:
    payees = write_import_map("bank/payees.csv", "corner market,Food\n")
    # As a hand might leave it: a blank row, which a rewrite would drop.
    payees.write_text(payees.read_text(encoding="utf-8") + ",\n", encoding="utf-8")
    before = payees.read_bytes()

    # A number int() can't read, then Enter at the row's one question.
    result = _map(test_data_dir, "bank", "--scenario", "retire_alice", input="\u00b2\n1\nchange\n\ndone\n")

    assert result.exit_code == 0, result.output
    assert "a row's number from the list, or done" in result.output
    assert "nothing changed" in result.output and "saved" not in result.output
    assert payees.read_bytes() == before


@pytest.mark.usefixtures("at_a_terminal")
def test_sim_import_map_lists_every_file_again_once_a_row_is_removed(
    test_data_dir: Path, write_import_map: MapWriter,
) -> None:
    write_import_map("expense_accounts.csv", "Food,Expense_Living\nGifts,-\n")
    payees = write_import_map("bank/payees.csv", "corner market,Food\ncity water,Home\n")

    # Row 1 goes, so the payees are 2 and 3 no longer, but 1... and so on:
    # what was shown as 3 is now 2, and the screen must say so before 3 is typed.
    result = _map(test_data_dir, "--scenario", "retire_alice", input="1\nremove\n3\nremove\ndone\n")

    assert result.exit_code == 0, result.output
    relisted = result.output.split("saved ", 2)[1]
    assert ["2", "corner", "market", "Food"] in [line.split() for line in relisted.splitlines()]
    assert payees.read_text(encoding="utf-8") == "payee_pattern,category\ncorner market,Food\n"


@pytest.mark.usefixtures("at_a_terminal")
def test_sim_import_map_offers_a_changed_account_s_own_lots(
    test_data_dir: Path, write_import_map: MapWriter,
) -> None:
    saved = write_import_map("quicken/accounts.csv", "Old IRA,,map,Asset_Roth_Bill,roth,\n")

    # action kept, a new account, Enter at its lot, Enter at the note.
    result = _map(test_data_dir, "quicken", "--scenario", "retire_alice",
                  input="1\nchange\n\nAsset_Checking\n\n\ndone\n")

    assert result.exit_code == 0, result.output
    assert "lots of Asset_Checking: " in result.output
    assert "Old IRA,,map,Asset_Checking,," in saved.read_text(encoding="utf-8")


@pytest.mark.usefixtures("at_a_terminal")
def test_sim_import_map_keeps_nothing_for_a_file_that_has_gone(
    test_data_dir: Path, write_import_map: MapWriter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_import_map("expense_accounts.csv", "Food,Expense_Living\n")
    payees = write_import_map("bank/payees.csv", "corner market,Food\n")
    asked = cli._ask_name

    def meanwhile(what: str, *args: object, **kwargs: object) -> str:
        if what == "choice":
            payees.unlink()
        return asked(what, *args, **kwargs)  # ty: ignore[invalid-argument-type]  # passed through as given

    monkeypatch.setattr(cli, "_ask_name", meanwhile)
    result = _map(test_data_dir, "--scenario", "retire_alice", input="2\nremove\ndone\n")

    assert result.exit_code == 0, result.output
    assert "not kept: " in result.output


def test_import_map_offers_no_rule_only_where_a_file_reads_it(test_data_dir: Path) -> None:
    by_name = {kind.name: kind for kind in import_mapping.MAP_FILES}

    def offered(name: str, column: str) -> list[str]:
        return cli._map_choices(by_name[name], {}, lambda: cli._map_categories(test_data_dir))[column]

    assert import_mapping.NO_RULE in offered(import_mapping.CATEGORIES, "category")
    assert {import_mapping.NO_RULE, import_mapping.TRANSFER} <= set(offered(import_mapping.PAYEES, "category"))
    # A split's target is an ordinary category: '-' there would be one's name.
    assert import_mapping.NO_RULE not in offered(import_mapping.SPLITS, "target_category")


def test_a_mapping_file_is_as_it_was_whatever_stops_its_check(
    write_import_map: MapWriter,
) -> None:
    path = write_import_map("expense_accounts.csv", "Food,Expense_Living\n")
    before = path.read_text(encoding="utf-8")

    def interrupted(_: Path) -> object:
        raise KeyboardInterrupt

    kind = dataclasses.replace(
        next(each for each in import_mapping.MAP_FILES if each.name == import_mapping.EXPENSE_ACCOUNTS),
        check=interrupted)
    with pytest.raises(KeyboardInterrupt):
        import_mapping.write_map_rows(path, kind, [{"category": "Food", "account": "Expense_Housing"}])

    assert path.read_text(encoding="utf-8") == before


@pytest.mark.usefixtures("at_a_terminal")
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
                     "--from", "Asset_Checking", "--dry-run", scenario="child")

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
                     str(quicken_transaction_export), "--from", "Asset_Checking")

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
                     "--from", "Asset_Checking")

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
                      "--from", "Asset_Checking")
    assert refused.exit_code != 0
    assert "1 payee(s) with no category" in refused.output and "CORNER MARKET 204" in refused.output

    write_import_map("amex/payees.csv", "corner market,Food\n")
    result = _import(test_data_dir, "transactions", str(detailed), str(short),
                     "--from", "Asset_Checking")
    assert result.exit_code == 0, result.output
    # (80 + 40) in January and 60 in February.
    assert {name: rule["amount"] for name, rule in _generated(test_data_dir).items()} == {
        "baseline_expense_food": "90.00"}


# ---------- a budget (issue #617) ----------

_BOTH_ACCOUNTS = ("Food,Expense_Living\nGifts,Expense_Living\nUtilities,Expense_Housing\n"
                  "Property Tax,Expense_Housing\n")


def _with_budget(data: Path, export: Path, budget: Path, *args: str, input: str | None = None):  # noqa: A002
    return _import(data, "transactions", str(export), "--budget", str(budget),
                   "--from", "Asset_Checking", *args, input=input)


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
    # A preference answers where a question would be asked: here too, and it is said.
    preferred = _with_budget(test_data_dir, quicken_transaction_export, quicken_budget_full_export,
                             "--prefer", "actual")
    assert preferred.exit_code == 0, preferred.output
    assert "note: Food uses --prefer actual in place of the saved choice, budget" in preferred.output
    assert _amounts(test_data_dir)["baseline_expense_food"] == ("620.00", "monthly")
    assert saved.read_text(encoding="utf-8") == "category,use,budget,actual\nFood,budget,710.00,520.00\n"


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


@pytest.mark.usefixtures("quicken_category_map")
def test_sim_import_transactions_with_a_budget_asks_about_a_category_whose_transactions_were_all_left_out(
    test_data_dir: Path, quicken_transaction_export: Path, quicken_budget_full_export: Path,
    write_import_map: MapWriter,
) -> None:
    write_import_map("expense_accounts.csv", _BOTH_ACCOUNTS)
    # As the yearly script leaves a payee out: every payment for food, here.
    write_import_map("quicken/excluded_payees.csv", "market\ncafe\n")

    shown = _import(test_data_dir, "transactions", str(quicken_transaction_export), "--summary",
                    "--budget", str(quicken_budget_full_export))

    assert shown.exit_code == 0, shown.output
    # Not the budget's alone, to be used unasked: the scenario may pay it already.
    assert ["Food", "710.00", "0.00", "-710.00", "?"] in [line.split(maxsplit=4) for line in shown.output.splitlines()]
    assert "note: every transaction of Food was left out as modeled by the scenario or replaced" in shown.output
    undecided = _with_budget(test_data_dir, quicken_transaction_export, quicken_budget_full_export)
    assert undecided.exit_code != 0 and "no choice of budget or actual is saved for: Food" in undecided.output
    result = _with_budget(test_data_dir, quicken_transaction_export, quicken_budget_full_export,
                          "--prefer", "actual")
    assert result.exit_code == 0, result.output
    assert "baseline_expense_food" not in _generated(test_data_dir)
    assert "baseline_expense_utilities" in _generated(test_data_dir)


def test_sim_import_transactions_yearly_script_leaves_a_category_made_from_the_budget_to_the_budget(
    test_data_dir: Path, quicken_year_export: Path, quicken_budget_full_export: Path,
    write_import_map: MapWriter, tmp_path: Path,
) -> None:
    # The premium is in Food here, which the budget has too.
    write_import_map("quicken/categories.csv", "Groceries,Food\nGifts,Gifts\nAuto Insurance,Food\n")
    write_import_map("expense_accounts.csv", _BOTH_ACCOUNTS)
    script = tmp_path / "yearly.sh"

    actual = _import(test_data_dir, "transactions", str(quicken_year_export), "--budget",
                     str(quicken_budget_full_export), "--prefer", "actual", "--yearly-script", str(script))
    assert actual.exit_code == 0, actual.output
    assert "# yearly yearly_mutual_insurance_mar " in script.read_text(encoding="utf-8")

    budget = _import(test_data_dir, "transactions", str(quicken_year_export), "--budget",
                     str(quicken_budget_full_export), "--prefer", "budget", "--yearly-script", str(script),
                     "--overwrite")

    assert budget.exit_code == 0, budget.output
    text = script.read_text(encoding="utf-8")
    # The budget's rules have the category's yearly payments: a rule here would pay them twice.
    assert "# Left out: Food is made from the budget, whose rules have its yearly payments already." in text
    assert "yearly_mutual_insurance" not in text and "leave_out quicken" not in text
    assert "0 payee(s) as yearly rules ready to run, 1 noted and not written as rules" in budget.output


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
    return _import(data, "budget", str(export), "--from", "Asset_Checking", *args, input=input)


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
    chain = ("budget", str(quicken_budget_full_export), "--from", "asset_checking_liquidity")

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
                     "--from", "Asset_Brokerage_Joint", "--from-lot", "default")

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


# ---------- a card's charges are paid from the card (issue #641) ----------

_CARD_ROW = "Rewards Card,,map,Liability_Card,,\n"


def _rules(data: Path) -> dict[str, dict[str, str]]:
    path = data / "scenarios" / "retire_alice" / "scheduled.csv"
    with path.open(newline="", encoding="utf-8") as f:
        return {r["name"]: r for r in csv.DictReader(f)}


@pytest.fixture
def card_import(
    test_data_dir: Path, quicken_card_export: Path, credit_card: str, quicken_category_map: Path,
    write_import_map: MapWriter,
) -> Callable[..., Result]:
    """``sim import transactions`` of ``quicken_card_export`` into the
    private copy, which has the ``credit_card``, paid from checking."""
    write_import_map("expense_accounts.csv", "Food,Expense_Living\nGifts,Expense_Living\n")

    def run(*args: str, input: str | None = None) -> Result:  # noqa: A002
        return _import(test_data_dir, "transactions", str(quicken_card_export),
                       *(() if "--summary" in args else ("--from", "Asset_Checking")), *args, input=input)
    return run


def test_sim_import_transactions_charges_a_cards_spending_to_the_card_and_pays_it_in_full(
    test_data_dir: Path, card_import: Callable[..., Result], write_import_map: MapWriter,
    load_test_copy: Callable[[], object],
) -> None:
    write_import_map("quicken/accounts.csv", _CARD_ROW)

    result = card_import()

    assert result.exit_code == 0, result.output
    # Two months: food 400 from checking and 440 on the card; the gift on the card alone.
    assert {name: (rule["from_account"], rule["amount"]) for name, rule in _generated(test_data_dir).items()} == {
        "baseline_expense_food": ("Asset_Checking", "200.00"),
        "baseline_expense_food__liability_card": ("Liability_Card", "220.00"),
        "baseline_expense_gifts__liability_card": ("Liability_Card", "30.00"),
    }
    payment = _rules(test_data_dir)["card_payment_liability_card"]
    assert (payment["from_account"], payment["to_account"], payment["amount"], payment["cadence"],
            payment["tags"]) == ("Asset_Checking", "Liability_Card", "", "monthly", "generated:card_payment")
    assert "card_payment_liability_card: pays Liability_Card in full each month from Asset_Checking" \
        in result.output
    assert "sim scheduled add --kind card_interest" in result.output
    load_test_copy()


def test_sim_import_transactions_writes_a_cards_payment_once_and_leaves_the_households_own(
    test_data_dir: Path, card_import: Callable[..., Result], write_import_map: MapWriter,
    update_scheduled: Callable[..., None],
) -> None:
    write_import_map("quicken/accounts.csv", _CARD_ROW)
    assert card_import().exit_code == 0
    update_scheduled("card_payment_liability_card", amount="150.00")

    again = card_import()

    assert again.exit_code == 0, again.output
    assert "card_payment_liability_card" not in again.output
    assert _rules(test_data_dir)["card_payment_liability_card"]["amount"] == "150.00"
    assert len(_generated(test_data_dir)) == 3


def test_sim_import_transactions_adds_no_payment_to_a_card_a_rule_already_pays(
    test_data_dir: Path, card_import: Callable[..., Result], write_import_map: MapWriter,
    append_scheduled_row: Callable[..., None],
) -> None:
    write_import_map("quicken/accounts.csv", _CARD_ROW)
    append_scheduled_row(name="my_card_bill", from_account="Asset_Savings", to_account="Liability_Card",
                         amount_rule_kind="fixed_amount", amount="500.00", tax_character="none",
                         start="2026-05-31")

    result = card_import()

    assert result.exit_code == 0, result.output
    assert "card_payment_liability_card" not in _rules(test_data_dir)


def test_sim_import_transactions_dry_run_shows_the_card_and_writes_nothing(
    test_data_dir: Path, card_import: Callable[..., Result], write_import_map: MapWriter,
) -> None:
    write_import_map("quicken/accounts.csv", _CARD_ROW)

    result = card_import("--dry-run")

    assert result.exit_code == 0, result.output
    assert "baseline_expense_food__liability_card" in result.output
    assert "card_payment_liability_card: pays Liability_Card in full" in result.output
    assert not _generated(test_data_dir) and "card_payment_liability_card" not in _rules(test_data_dir)


def test_sim_import_transactions_notes_a_card_with_no_account_away_from_a_terminal(
    test_data_dir: Path, card_import: Callable[..., Result],
) -> None:
    result = card_import()

    assert result.exit_code == 0, result.output
    assert ("note: Liability_Card has no outside account mapped to it, and Checking (quicken), "
            "Rewards Card (quicken) is mapped to nothing") in result.output
    assert {name: rule["from_account"] for name, rule in _generated(test_data_dir).items()} == {
        "baseline_expense_food": "Asset_Checking", "baseline_expense_gifts": "Asset_Checking"}


@pytest.mark.usefixtures("at_a_terminal")
def test_sim_import_transactions_asks_which_account_is_a_card_until_each_card_has_one(
    test_data_dir: Path, card_import: Callable[..., Result],
) -> None:
    # Checking isn't one (blank); the card is, and after it nothing is left to ask.
    result = card_import(input="\nLiability_Card\n")

    assert result.exit_code == 0, result.output
    assert "'Checking' (quicken) has no row" in result.output
    assert _generated(test_data_dir)["baseline_expense_gifts__liability_card"]["from_account"] == "Liability_Card"
    saved = test_data_dir / "imports" / "quicken" / "accounts.csv"
    assert saved.read_text(encoding="utf-8").splitlines()[1:] == ["Rewards Card,,map,Liability_Card,,"]

    again = card_import()
    assert again.exit_code == 0 and "has no row" not in again.output


@pytest.mark.usefixtures("at_a_terminal")
def test_sim_import_transactions_asks_once_about_an_account_named_as_no_card(
    test_data_dir: Path, card_import: Callable[..., Result], credit_card: str,
    retire_alice_csv: Callable[[str], Path],
) -> None:
    # A second card that no file has: without it named, checking would be asked about at every import.
    accounts = retire_alice_csv("accounts.csv")
    lines = accounts.read_text(encoding="utf-8").splitlines()
    accounts.write_text("\n".join([*lines, next(
        line for line in lines if line.startswith(f"{credit_card},")).replace(credit_card, "Liability_Card_Two")])
        + "\n", encoding="utf-8")

    result = card_import(input="Asset_Checking\nLiability_Card\n")

    assert result.exit_code == 0, result.output
    saved = test_data_dir / "imports" / "quicken" / "accounts.csv"
    assert saved.read_text(encoding="utf-8").splitlines()[1:] == [
        "Checking,,map,Asset_Checking,,", "Rewards Card,,map,Liability_Card,,"]
    again = card_import()
    assert again.exit_code == 0 and "has no row" not in again.output


def test_card_payments_see_a_payment_through_a_chain_and_a_card_only_yearly_rules_charge(
    test_data_dir: Path, credit_card: str, append_scheduled_row: Callable[..., None],
) -> None:
    scenario = test_data_dir / "scenarios" / "retire_alice"
    cards = {("quicken", "Rewards Card"): (credit_card, "")}

    def payments(chains: Mapping[str, Collection[str]]) -> list[str]:
        return [row["name"] for row in cli._card_payments(
            scenario, [], cards, "Asset_Checking", "", "2026-05-31", chains)]

    # Nothing charges the card: nothing to pay.
    assert payments({}) == []
    # A yearly rule the script added charges it, though this run's monthly rules don't.
    append_scheduled_row(name="yearly_mutual_insurance", from_account=credit_card, to_account="Expense_Living",
                         amount_rule_kind="fixed_amount", amount="900.00", tax_character="none",
                         start="2026-05-31", cadence="annual", month_of_year="3")
    assert payments({}) == ["card_payment_liability_card"]
    # The chains are the scenario's own, each with the accounts it reaches.
    assert cli._chain_accounts(test_data_dir, "retire_alice")["asset_checking_liquidity"] >= {
        "Asset_Savings", "Asset_TraditionalIRA_Alice"}
    # The household pays it through a chain that reaches it: its own payment stays the only one.
    append_scheduled_row(name="my_card_bill", from_account="Asset_Checking", to_account="pay_card",
                         amount_rule_kind="fixed_amount", amount="300.00", tax_character="none",
                         start="2026-05-31")
    assert payments({"pay_card": {credit_card}}) == []


@pytest.mark.slow
def test_yearly_script_run_charges_the_card_and_adds_its_payment(
    test_data_dir: Path, credit_card: str, tmp_path: Path, load_test_copy: Callable[[], object],
) -> None:
    charged = dataclasses.replace(
        _seen("Insurance", "Mutual Insurance", (date(2026, 3, 1), 90_000)), accounts=(("card", "Rewards Card"),))
    text, _, _ = import_yearly.script(
        [charged], data_dir=test_data_dir, scenario="retire_alice", start="2026-05",
        from_account="Asset_Checking", expense_accounts={"Insurance": "Expense_Living"},
        accounts=["Expense_Living"], source="card", cards={("card", "Rewards Card"): (credit_card, "")},
        sim=str(Path(sys.executable).parent / "sim"))
    script = tmp_path / "yearly.sh"
    script.write_text(text, encoding="utf-8")

    ran = subprocess.run(["sh", str(script)], capture_output=True, text=True, check=False)

    assert ran.returncode == 0, ran.stdout + ran.stderr
    rules = _rules(test_data_dir)
    assert rules["yearly_mutual_insurance"]["from_account"] == credit_card
    payment = rules["card_payment_liability_card"]
    assert (payment["from_account"], payment["to_account"], payment["amount"], payment["tags"]) == (
        "Asset_Checking", credit_card, "", "generated:card_payment")
    load_test_copy()
    # Run again, it adds neither a second time.
    rerun = subprocess.run(["sh", str(script)], capture_output=True, text=True, check=False)
    assert rerun.stdout.splitlines() == [
        "already added: yearly_mutual_insurance", "already added: card_payment_liability_card"]


def test_sim_import_transactions_summary_shows_what_each_card_was_charged(
    card_import: Callable[..., Result], write_import_map: MapWriter,
) -> None:
    write_import_map("quicken/accounts.csv", _CARD_ROW)

    result = card_import("--summary")

    assert result.exit_code == 0, result.output
    assert ("charged to Liability_Card, and paid from it: 250.00 a month (Food 220.00, Gifts 30.00)"
            in result.output)


def test_sim_import_transactions_pays_a_budgets_category_as_most_of_it_was_paid(
    test_data_dir: Path, card_import: Callable[..., Result], write_import_map: MapWriter,
    quicken_budget_full_export: Path,
) -> None:
    write_import_map("quicken/accounts.csv", _CARD_ROW)
    write_import_map("expense_accounts.csv", _BOTH_ACCOUNTS)

    result = card_import("--budget", str(quicken_budget_full_export), "--prefer", "budget")

    assert result.exit_code == 0, result.output
    # The card paid 440 of food's 840, so the budget's food is the card's.
    assert _generated(test_data_dir)["baseline_expense_food"]["from_account"] == "Liability_Card"
    assert _generated(test_data_dir)["baseline_expense_utilities"]["from_account"] == "Asset_Checking"
    assert _rules(test_data_dir)["card_payment_liability_card"]["amount"] == ""


def test_yearly_script_pays_a_payee_from_the_card_all_of_it_was_charged_to() -> None:
    cards = {("card", "Rewards Card"): ("Liability_Card", "")}
    charged = dataclasses.replace(
        _seen("Insurance", "Mutual Insurance", (date(2027, 3, 1), 90_000)),
        accounts=(("card", "Rewards Card"),))
    partly = dataclasses.replace(
        _seen("Taxes", "County Tax", (date(2027, 4, 1), 210_000)),
        accounts=(("card", "Rewards Card"), ("bank", "Checking")))
    text, _, _ = import_yearly.script(
        [charged, partly], data_dir=Path("/data"), scenario="retire_alice", start="2028-01",
        from_account="Asset_Checking", expense_accounts={}, accounts=["Insurance", "Taxes"],
        source="bank", cards=cards)

    assert "yearly yearly_mutual_insurance Insurance 900.00 3 2027-03 " \
           "'Yearly: Mutual Insurance (Insurance)' Liability_Card\n" in text
    assert "# Charged to a card only in part: the rules below pay from FROM.\n" in text
    assert "yearly yearly_county_tax Taxes 2100.00 4 2027-04 'Yearly: County Tax (Taxes)'\n" in text
    assert "if [ $# -ge 7 ]; then from=$7; lot=${8:-}; else from=$FROM; lot=$FROM_LOT; fi" in text
    # The card these rules charge is paid: the import writes a payment only for a card its
    # monthly rules charge.
    assert text.rstrip().endswith("\npay_card card_payment_liability_card Liability_Card")
    # Not when the scenario has the rule, and commented out with the rules that wait.
    again, _, _ = import_yearly.script(
        [charged], data_dir=Path("/data"), scenario="retire_alice", start="2028-01",
        from_account="Asset_Checking", expense_accounts={}, accounts=["Insurance"],
        source="bank", cards=cards, taken=["card_payment_liability_card"])
    assert "\npay_card " not in again and "# pay_card " not in again
    waiting, _, _ = import_yearly.script(
        [dataclasses.replace(charged, repeats=False)], data_dir=Path("/data"), scenario="retire_alice",
        start="2028-01", from_account="Asset_Checking", expense_accounts={}, accounts=["Insurance"],
        source="bank", cards=cards)
    assert waiting.rstrip().endswith("\n# pay_card card_payment_liability_card Liability_Card")


# ---------- OFX and QFX (#618) ----------

_OFX_PAYEES = ("corner market,Food\ncity power,Utilities\ntransfer to savings,transfer\n"
               "online payment,transfer\n")


def test_sim_import_sources_recognizes_an_ofx_file_by_its_content(
    test_data_dir: Path, ofx_bank_download: Path, ofx_card_download: Path, rewrite_download: Rewrite,
) -> None:
    renamed = rewrite_download(ofx_card_download, lambda raw: raw, name="download.txt")

    result = CliRunner().invoke(main, ["sim", "import", "sources", str(ofx_bank_download), str(renamed),
                                       "--data-dir", str(test_data_dir)])

    assert result.exit_code == 0, result.output
    assert result.output.count("ofx holdings, ofx transactions") == 2


def test_ofx_reads_both_versions_and_keeps_the_institution_s_own_day(
    ofx_bank_download: Path, ofx_card_download: Path,
) -> None:
    bank = import_ofx.read_transactions(ofx_bank_download).transactions
    card = import_ofx.read_transactions(ofx_card_download).transactions

    # The account's whole number stays out: the institution, a bank account's type and the
    # last four digits.
    assert set(bank["account"]) == {"First Example Bank checking ...4321"}
    assert set(card["account"]) == {"Example Card card ...1007"}
    # 9 p.m. Pacific on the 2nd is the 3rd in UTC; the bank's day is kept.
    assert [f"{day:%Y-%m-%d}" for day in bank["date"]][:2] == ["2026-01-02", "2026-01-15"]
    assert list(bank["amount_cents"]) == [-15000, 500000, -40000, -15000, -100000, -20000]
    assert list(bank["reference"])[0] == "B0001" and not bank["category"].any()
    assert list(card["payee"]) == ["CORNER MARKET #204 OAKLAND CA", "ONLINE PAYMENT - THANK YOU"]


def test_ofx_refuses_a_file_it_cannot_read(ofx_bank_download: Path, rewrite_download: Rewrite) -> None:
    bad = rewrite_download(ofx_bank_download, lambda raw: raw.replace(b"20260102210000.000[-8:PST]", b"soon"))
    with pytest.raises(ValueError, match="DTPOSTED 'soon' is not a date"):
        import_ofx.read_transactions(bad)

    # A header and no account's statement: refused, with the file named.
    bad = rewrite_download(ofx_bank_download, lambda _: b"OFXHEADER:100\nDATA:OFXSGML\n\n<OFX>\n</OFX>\n",
                           name="bad.qfx")
    assert import_ofx.is_ofx(bad)
    with pytest.raises(ValueError, match=r"bad\.qfx: not an OFX file that can be read"):
        import_ofx.read_holdings(bad)
    # And one whose markup the library refuses, with its header or without.
    bad = rewrite_download(ofx_bank_download, lambda raw: raw.replace(b"</STMTRS>", b"</STMTRS>stray"))
    with pytest.raises(ValueError, match="not an OFX file that can be read: text follows a closing tag, at"):
        import_ofx.read_transactions(bad)
    # A statement whose own group is left unclosed gives no account number: every such
    # account would have one name.
    bad = rewrite_download(ofx_bank_download, lambda raw: raw.replace(b"</BANKACCTFROM>\n", b""))
    for read in (import_ofx.read_transactions, import_ofx.read_holdings):
        with pytest.raises(ValueError, match="a statement doesn't say which account it is"):
            read(bad)
    # A download cut off, in a transaction or after the last one, with its header or without.
    for cut in (b"<FITID>B0004", b"</BANKTRANLIST>"):
        for start in (b"OFXHEADER", b"<OFX>"):
            short = rewrite_download(ofx_bank_download, lambda raw, cut=cut, start=start:
                                     raw[raw.index(start):raw.index(cut)])
            for read in (import_ofx.read_transactions, import_ofx.read_holdings):
                with pytest.raises(ValueError, match="it stops before its end"):
                    read(short)


def test_ofx_keeps_a_refused_file_s_text_out_of_its_message(
    ofx_card_download: Path, rewrite_download: Rewrite,
) -> None:
    """The library's own message for text after a closing tag has the whole file in it."""
    bad = rewrite_download(ofx_card_download, lambda raw: raw.replace(
        b"<NAME>CORNER MARKET #204 OAKLAND CA</NAME>", b"<NAME>CORNER MARKET</NAME>#204 OAKLAND CA"))

    with pytest.raises(ValueError, match="text follows a closing tag") as refused:
        import_ofx.read_transactions(bad)

    assert "370000000001007" not in str(refused.value) and "OAKLAND" not in str(refused.value)
    assert len(str(refused.value)) < len(str(bad)) + 100


def test_ofx_reads_files_whose_wrapping_is_not_as_the_specification_has_it(
    ofx_bank_download: Path, ofx_card_download: Path, rewrite_download: Rewrite,
) -> None:
    expected = list(import_ofx.read_transactions(ofx_bank_download).transactions["amount_cents"])

    # No header at all: recognized by its <OFX> tag, and read from it.
    bare = rewrite_download(ofx_bank_download, lambda raw: raw[raw.index(b"<OFX>"):])
    assert import_ofx.is_ofx(bare)
    assert list(import_ofx.read_transactions(bare).transactions["amount_cents"]) == expected

    # An OFX 2 file that starts with a byte-order mark.
    marked = rewrite_download(ofx_card_download, lambda raw: b"\xef\xbb\xbf" + raw)
    assert import_ofx.is_ofx(marked)
    assert len(import_ofx.read_transactions(marked).transactions) == 2

    # A payee in the bank's own code page under a header that doesn't say so.
    accented = rewrite_download(ofx_bank_download, lambda raw: raw.replace(b"CHARSET:1252", b"CHARSET:NONE")
                                .replace(b"CORNER MARKET", b"CAF\xc9 MARKET"))
    assert list(import_ofx.read_transactions(accented).transactions["payee"])[2] == "CAFÉ MARKET #204 OAKLAND CA"

    # The other way round: UTF-8 under a header that names a code page, in a payee and in
    # the institution's name, which names the account.
    utf8 = rewrite_download(ofx_bank_download, lambda raw: raw.replace(b"CORNER MARKET", "CAFÉ MARKET".encode())
                            .replace(b"First Example Bank", "Café Bank".encode()))
    frame = import_ofx.read_transactions(utf8).transactions
    assert list(frame["payee"])[2] == "CAFÉ MARKET #204 OAKLAND CA"
    assert set(frame["account"]) == {"Café Bank checking ...4321"}

    # The institution's group left unclosed: its name is beside it, and still the account's.
    unclosed = rewrite_download(ofx_bank_download, lambda raw: raw.replace(b"</FI>\n", b""))
    assert set(import_ofx.read_transactions(unclosed).transactions["account"]) == {
        "First Example Bank checking ...4321"}

    # An OFX 2 body on one line with its payees as CDATA: each value ends at its own end.
    def one_line(raw: bytes) -> bytes:
        raw = raw.replace(b"<NAME>", b"<NAME><![CDATA[").replace(b"</NAME>", b"]]></NAME>")
        return raw[:raw.index(b"<OFX>")] + raw[raw.index(b"<OFX>"):].replace(b"\n", b"")
    lined = import_ofx.read_transactions(rewrite_download(ofx_card_download, one_line)).transactions
    assert list(lined["payee"]) == ["CORNER MARKET #204 OAKLAND CA", "ONLINE PAYMENT - THANK YOU"]

    # A space inside a tag, as XML allows: the same tag, which its closing tag closes.
    spaced = rewrite_download(ofx_card_download, lambda raw: raw.replace(b"<STMTTRN>", b"<STMTTRN >")
                              .replace(b"</OFX>", b"</OFX >"))
    assert len(import_ofx.read_transactions(spaced).transactions) == 2

    # An empty NAME before the payee's own group: the name that says something.
    grouped = rewrite_download(ofx_card_download, lambda raw: raw.replace(
        b"<NAME>CORNER MARKET #204 OAKLAND CA</NAME>",
        b"<NAME></NAME><PAYEE><NAME>CORNER MARKET</NAME><CITY>OAKLAND</CITY></PAYEE>"))
    assert list(import_ofx.read_transactions(grouped).transactions["payee"])[0] == "CORNER MARKET"

    # &amp; is the character it stands for, in a payee and in the institution's name.
    escaped = rewrite_download(ofx_bank_download, lambda raw: raw.replace(b"CORNER MARKET", b"A &amp; B MARKET")
                               .replace(b"First Example Bank", b"Alice &amp; Bill&apos;s Bank"))
    frame = import_ofx.read_transactions(escaped).transactions
    assert list(frame["payee"])[2] == "A & B MARKET #204 OAKLAND CA"
    assert set(frame["account"]) == {"Alice & Bill's Bank checking ...4321"}
    # However it is escaped: by number too, so one payees.csv row matches every bank's.
    numbered = rewrite_download(ofx_bank_download,
                                lambda raw: raw.replace(b"CORNER MARKET", b"A &#38; B&#x26;C MARKET"))
    assert list(import_ofx.read_transactions(numbered).transactions["payee"])[2] == "A & B&C MARKET #204 OAKLAND CA"
    # A bare & is itself, though HTML's old references need no ';' (&COPY, &LT, &REG).
    bare = rewrite_download(ofx_bank_download, lambda raw: raw.replace(b"CORNER MARKET", b"QUICK&COPY S&LT B&REGAL"))
    assert list(import_ofx.read_transactions(bare).transactions["payee"])[2] == (
        "QUICK&COPY S&LT B&REGAL #204 OAKLAND CA")


def test_ofx_reads_each_transaction_s_own_fields_after_a_value_left_empty(
    ofx_bank_download: Path, ofx_card_download: Path, ofx_bank_like_accounts_download: Path,
    ofx_card_then_bank_download: Path, rewrite_download: Rewrite,
) -> None:
    """The library reads an empty value as the start of a group, so the next transaction,
    and after enough of them the next statement, lands inside the one before it."""
    # Two accounts' statements, each transaction ending with an empty value: each account
    # has its own six and no more, and its own balance.
    both = rewrite_download(ofx_bank_like_accounts_download,
                            lambda raw: raw.replace(b"\n</STMTTRN>", b"\n<MEMO>\n</STMTTRN>"))
    counted = import_ofx.read_transactions(both).transactions["account"].value_counts().to_dict()
    assert counted == {"First Example Bank checking ...4321": 6, "First Example Bank savings ...4321": 6}
    assert len(import_ofx.read_holdings(both).holdings) == 2
    # A card's statement before a bank's keeps its own number and transactions.
    mixed = rewrite_download(ofx_card_then_bank_download, lambda raw: raw.replace(
        b"</NAME>\n</STMTTRN>", b"</NAME><MEMO/><SIC/></STMTTRN>"))
    counted = import_ofx.read_transactions(mixed).transactions["account"].value_counts().to_dict()
    assert counted == {"Example Card card ...1007": 2, "Example Card checking ...4321": 6}
    assert [(each.account, each.hints) for each in import_ofx.read_holdings(mixed).holdings] == [
        ("Example Card card ...1007", {"type": "creditcard"}), ("Example Card checking ...4321", {"type": "checking"})]

    bank = import_ofx.read_transactions(ofx_bank_download).transactions
    # OFX 1: an empty <MEMO> ends every transaction, and one is a check with no payee.
    empty = rewrite_download(
        ofx_bank_download, lambda raw: raw.replace(b"\n</STMTTRN>", b"\n<MEMO>\n</STMTTRN>")
        .replace(b"<NAME>CORNER MARKET #204 OAKLAND CA", b"<CHECKNUM>1001", 1))
    frame = import_ofx.read_transactions(empty).transactions
    assert list(frame["payee"]) == [*bank["payee"][:2], "CHECK 1001", *bank["payee"][3:]]
    assert list(frame["amount_cents"]) == list(bank["amount_cents"])
    assert list(frame["reference"]) == list(bank["reference"])

    # OFX 2: a self-closing tag, before a deletion that names an earlier download's transaction.
    closed = rewrite_download(
        ofx_card_download, lambda raw: raw.replace(b"</NAME>\n</STMTTRN>", b"</NAME><MEMO/></STMTTRN>")
        .replace(b"<FITID>C0002</FITID>",
                 b"<FITID>C0002</FITID><CORRECTFITID>OLD</CORRECTFITID><CORRECTACTION>DELETE</CORRECTACTION>"))
    flows = import_sources.read_flows([closed]).transactions
    assert list(zip(flows["payee"], flows["amount_cents"], strict=True)) == [
        ("CORNER MARKET #204 OAKLAND CA", -8000)]


def test_ofx_applies_a_correction_to_the_transaction_it_names(
    ofx_bank_download: Path, ofx_bank_later_download: Path, rewrite_download: Rewrite,
) -> None:
    def left(*paths: Path) -> list[tuple[str, int]]:
        frame = import_sources.read_flows(list(paths)).transactions
        assert "corrects" not in frame.columns and "removes" not in frame.columns
        return list(zip(frame["reference"], frame["amount_cents"], strict=True))

    def correcting(source: Path, *pairs: tuple[str, str, str]) -> Path:
        def change(raw: bytes) -> bytes:
            for by, named, action in pairs:
                raw = raw.replace(f"<FITID>{by}".encode(),
                                  f"<FITID>{by}\n<CORRECTFITID>{named}\n<CORRECTACTION>{action}".encode())
            return raw
        return rewrite_download(source, change)

    # The last purchase is a correction that replaces January's, and the move to savings
    # is one that deletes the first utility payment: a deletion is no transaction itself.
    assert left(correcting(ofx_bank_download, ("B0006", "B0003", "REPLACE"), ("B0005", "B0001", "DELETE"))) == [
        ("B0002", 500000), ("B0004", -15000), ("B0006", -20000)]

    # A correction that a later one corrects goes too: only the last word is counted.
    assert left(correcting(ofx_bank_download, ("B0002", "B0001", "REPLACE"), ("B0003", "B0002", "REPLACE")))[:2] == [
        ("B0003", -40000), ("B0004", -15000)]
    assert left(correcting(ofx_bank_download, ("B0002", "B0001", "REPLACE"), ("B0003", "B0002", "DELETE")))[0] == (
        "B0004", -15000)
    # One that names its own reference (the id reused) is not its own original.
    assert left(correcting(ofx_bank_download, ("B0001", "B0001", "REPLACE")))[0] == ("B0001", -15000)

    # What it corrects may be in an earlier download: March's purchase replaces January's.
    assert left(ofx_bank_download, correcting(ofx_bank_later_download, ("B0007", "B0003", "REPLACE"))) == [
        ("B0001", -15000), ("B0002", 500000), ("B0004", -15000), ("B0005", -100000), ("B0006", -20000),
        ("B0007", -9000)]

    # A correction that reuses its original's reference, in a later download: it is not
    # that download's copy of the original.
    alone = rewrite_download(ofx_bank_later_download, lambda raw: raw.replace(b"B0007", b"B0003"))
    assert ("B0003", -40000) not in left(ofx_bank_download, correcting(alone, ("B0003", "B0003", "DELETE")))
    replaced = left(ofx_bank_download, correcting(alone, ("B0003", "B0003", "REPLACE")))
    assert [amount for reference, amount in replaced if reference == "B0003"] == [-9000]
    # And the same correction in two downloads is counted once.
    twice = correcting(alone, ("B0003", "B0003", "REPLACE"))
    assert left(ofx_bank_download, twice, rewrite_download(twice, lambda raw: raw)) == replaced


def test_ofx_tells_apart_accounts_that_end_alike(
    ofx_bank_download: Path, ofx_bank_like_accounts_download: Path, ofx_bank_twin_accounts_download: Path,
    ofx_card_then_bank_download: Path, rewrite_download: Rewrite,
) -> None:
    # A savings account ending as the checking account does, with the bank's same short ids.
    frame = import_ofx.read_transactions(ofx_bank_like_accounts_download).transactions
    assert list(dict.fromkeys(frame["account"])) == [
        "First Example Bank checking ...4321", "First Example Bank savings ...4321"]
    # Numbered through the whole file, not from 1 again for each account.
    assert list(frame["source_row"]) == list(range(1, 13))
    assert [each.account for each in import_ofx.read_holdings(ofx_bank_like_accounts_download).holdings] == [
        "First Example Bank checking ...4321", "First Example Bank savings ...4321"]

    # In the file's order, whichever kind of statement comes first.
    mixed = import_ofx.read_transactions(ofx_card_then_bank_download).transactions
    assert list(dict.fromkeys(mixed["account"])) == ["Example Card card ...1007", "Example Card checking ...4321"]
    assert list(mixed["source_row"]) == list(range(1, 9))

    # Many banks' files don't name the bank: its routing number keeps two banks' apart.
    unnamed = rewrite_download(
        ofx_bank_download, lambda raw: raw.replace(b"<FI>\n<ORG>First Example Bank\n<FID>1234\n</FI>\n", b""))
    assert set(import_ofx.read_transactions(unnamed).transactions["account"]) == {"121000000 checking ...4321"}

    # Two that nothing but the hidden digits tells apart are not read as one account,
    # and one account's two statements are not counted twice.
    twice = rewrite_download(ofx_bank_like_accounts_download,
                             lambda raw: raw.replace(b"000555554321", b"000987654321").replace(b"SAVINGS", b"CHECKING"))
    for read in (import_ofx.read_transactions, import_ofx.read_holdings):
        with pytest.raises(ValueError, match="two of its accounts would both be 'First Example Bank checking"):
            read(ofx_bank_twin_accounts_download)
        with pytest.raises(ValueError, match="more than one statement of 'First Example Bank checking"):
            read(twice)


def test_sim_import_transactions_notes_one_ofx_account_under_two_names(
    test_data_dir: Path, ofx_bank_download: Path, ofx_bank_later_download: Path, rewrite_download: Rewrite,
    write_import_map: MapWriter,
) -> None:
    """A bank's other kind of download doesn't name the bank, so the account gets another
    name, and February, in both, is neither refused nor counted once."""
    write_import_map("expense_accounts.csv", "Food,Expense_Living\nUtilities,Expense_Housing\n")
    write_import_map("ofx/payees.csv", _OFX_PAYEES)
    unnamed = rewrite_download(
        ofx_bank_later_download,
        lambda raw: raw.replace(b"<FI>\n<ORG>First Example Bank\n<FID>1234\n</FI>\n", b""))

    result = _transactions(test_data_dir, ofx_bank_download, str(unnamed))

    assert result.exit_code == 0, result.output
    assert ("note: 3 transaction(s) of '121000000 checking ...4321' and of 'First Example Bank checking ...4321' "
            "(ofx) have the same reference, day and amount") in result.output
    # The same account under one name has nothing to note.
    assert not import_sources.read_flows([ofx_bank_download, ofx_bank_later_download]).notes


def test_ofx_does_not_note_two_accounts_of_one_file_as_one(
    ofx_bank_like_accounts_download: Path, rewrite_download: Rewrite,
) -> None:
    """One file's accounts are known to differ, though this bank numbers each account's
    transactions alike; a third name is compared with both."""
    assert not import_sources.read_flows([ofx_bank_like_accounts_download]).notes

    other = rewrite_download(ofx_bank_like_accounts_download, lambda raw: raw.replace(
        b"<FI>\n<ORG>First Example Bank\n<FID>1234\n</FI>\n", b"").replace(b"SAVINGS", b"MONEYMRKT"))
    notes = import_sources.read_flows([ofx_bank_like_accounts_download, other]).notes
    # Each of the first file's two names with each of the second's, and neither file's own pair.
    assert len(notes) == 4 and all("121000000" in note and "First Example Bank" in note for note in notes)


def test_ofx_references_that_do_not_tell_duplicates_are_noted(
    ofx_bank_download: Path, ofx_bank_later_download: Path, rewrite_download: Rewrite,
) -> None:
    # A bank that numbers each download's transactions from the start again: the later
    # download's B0004 to B0006 are other transactions than the earlier one's.
    def renumber(raw: bytes) -> bytes:
        for old, new in ((b"B0004", b"B0001"), (b"B0005", b"B0002"), (b"B0006", b"B0003"), (b"B0007", b"B0004")):
            raw = raw.replace(old, new)
        return raw
    afresh = rewrite_download(ofx_bank_later_download, renumber)
    flows = import_sources.read_flows([ofx_bank_download, afresh])
    # A reference is taken at its word, so they are left out, and it is said.
    assert (flows.duplicates, len(flows.transactions)) == (4, 6)
    assert flows.notes == (
        f"4 transaction(s) of 'First Example Bank checking ...4321' in {afresh} have a reference that an earlier "
        "file gave a transaction of another day or amount; they are left out as that transaction, at the "
        "earlier file's day and amount. If this institution numbers each download's transactions afresh, they "
        "are other transactions and are missing: import one download that has the whole period",)

    # A bank that gives a transaction a new id in each download: February is counted twice.
    renamed = rewrite_download(ofx_bank_later_download, lambda raw: raw.replace(b"<FITID>B", b"<FITID>N"))
    flows = import_sources.read_flows([ofx_bank_download, renamed])
    assert (flows.duplicates, len(flows.transactions)) == (0, 10)
    assert flows.notes == (
        f"3 transaction(s) of 'First Example Bank checking ...4321' in {renamed} have the day and amount of ones "
        "in an earlier file, under other references; if this institution gives a transaction a new reference "
        "in each download, they are counted twice: import downloads that don't overlap",)


def test_ofx_keeps_the_copies_of_a_reference_that_an_earlier_download_did_not_have(
    ofx_bank_download: Path, rewrite_download: Rewrite,
) -> None:
    """Some institutions make a reference from the day, amount and payee, so two like
    purchases of one day share it."""
    twice = rewrite_download(
        ofx_bank_download, lambda raw: raw.replace(b"B0006", b"B0003").replace(b"20260220", b"20260120")
        .replace(b"-200.00", b"-400.00"), name="twice.qfx")
    plain = import_sources.read_flows([ofx_bank_download, twice])
    # The first download had one B0003 and the second has two: one is the same, one is new.
    assert (plain.duplicates, list(plain.transactions["reference"]).count("B0003")) == (5, 2)
    # Both had two: nothing is new, in either order.
    both = import_sources.read_flows([twice, rewrite_download(twice, lambda raw: raw)])
    assert (both.duplicates, len(both.transactions)) == (6, 6)


def test_sim_import_holdings_keeps_an_ofx_brokerage_s_cash_in_its_cash_lot(
    test_data_dir: Path, ofx_brokerage_download: Path, rewrite_download: Rewrite, write_import_map: MapWriter,
) -> None:
    write_import_map("ofx/accounts.csv", "Example Brokerage investment ...5678,,map,Asset_Brokerage_Joint,,\n")

    def cash_of(amount: str, *args: str, positions: bool = True) -> dict[tuple[str, str], dict[str, str]]:
        def change(raw: bytes) -> bytes:
            raw = raw.replace(b"<AVAILCASH>20000.00", f"<AVAILCASH>{amount}".encode())
            if not positions:
                raw = raw[:raw.index(b"<INVPOSLIST>") + 12] + raw[raw.index(b"</INVPOSLIST>"):]
            return raw
        result = _import(test_data_dir, "holdings", str(rewrite_download(ofx_brokerage_download, change)), *args)
        assert result.exit_code == 0, result.output
        return _all_lots(test_data_dir)

    # The account's one lot is split into a lot for each fund, and one for the cash: the
    # cash doesn't land in the lot being emptied, which would keep the account's whole
    # prior-year balance and basis on a lot of 750.
    lots = cash_of("20750.00", "--new-lots")
    cash = lots["Asset_Brokerage_Joint", "cash"]
    assert (cash["symbol"], cash["opening_balance"], cash["opening_cost_basis"]) == ("CASH", "750.00", "750.00")
    # The file has no basis for the funds, so the account's stays where it was to be shared out.
    old = lots["Asset_Brokerage_Joint", "default"]
    assert (old["opening_balance"], old["opening_cost_basis"]) == ("0.00", "200000.00")
    assert lots["Asset_Brokerage_Joint", "vtsax"]["opening_cost_basis"] == ""

    # The next download's cash goes to the cash lot, by its symbol, and not to a new one.
    lots = cash_of("20500.00")
    assert lots["Asset_Brokerage_Joint", "cash"]["opening_balance"] == "500.00"
    assert ("Asset_Brokerage_Joint", "cash-2") not in lots
    # Spent: the lot is set to nothing, and not left with what it had.
    assert cash_of("20000.00")["Asset_Brokerage_Joint", "cash"]["opening_balance"] == "0.00"

    # The funds sold, and all of it cash: the file lists the account in full though it
    # has no position, so with --prune their lots are emptied.
    lots = cash_of("420750.00", "--prune", positions=False)
    assert [lots["Asset_Brokerage_Joint", lot]["opening_balance"] for lot in ("vtsax", "vmfxx", "cash")] == [
        "0.00", "0.00", "420750.00"]


def test_references_leave_out_the_copies_an_earlier_file_had_wherever_they_are() -> None:
    def rows(*each: tuple[str, str, int] | tuple[str, str, int, str]) -> pd.DataFrame:
        return pd.DataFrame({
            "account": "Card", "reference": [row[0] for row in each],
            "date": pd.to_datetime([row[1] for row in each]), "amount_cents": [row[2] for row in each],
            "corrects": [row[3] if len(row) > 3 else "" for row in each]})

    # An institution whose reference is the day: the later download lists another
    # purchase of the day first. The one the earlier file had is left out, and not the first.
    references = import_sources._References()
    assert references.sift("bank", Path("d1"), rows(("0115", "2026-01-15", -1000))) == ([True], [])
    assert references.sift("bank", Path("d2"), rows(("0115", "2026-01-15", -5000), ("0115", "2026-01-15", -1000))) == (
        [True, False], [])

    # A replacement that a later download lists as the transaction now is, with nothing
    # to say it was a correction: counted once.
    references = import_sources._References()
    assert references.sift("bank", Path("feb1"), rows(("A1", "2026-02-01", -40000)))[0] == [True]
    assert references.sift("bank", Path("feb2"), rows(
        ("A1", "2026-02-01", -40000), ("B1", "2026-02-01", -4000, "A1")))[0] == [False, True]
    assert references.sift("bank", Path("feb3"), rows(("B1", "2026-02-01", -4000))) == ([False], [])

    # A reference whose day moved, and a new purchase on the old day: the purchase is not
    # taken for the earlier file's row, which this file has (under its new day).
    references = import_sources._References()
    assert references.sift("bank", Path("jan"), rows(("B3", "2026-01-20", -40000)))[0] == [True]
    fresh, notes = references.sift(
        "bank", Path("feb"), rows(("B3", "2026-01-21", -40000), ("B99", "2026-01-20", -40000)))
    assert fresh == [False, True]
    assert len(notes) == 1 and "another day or amount" in notes[0]


def test_references_leave_out_only_what_an_earlier_file_kept() -> None:
    def rows(*each: tuple[str, str, int]) -> pd.DataFrame:
        return pd.DataFrame({
            "account": "Checking", "reference": [reference for reference, _, _ in each],
            "date": pd.to_datetime([day for _, day, _ in each]), "amount_cents": [cents for _, _, cents in each]})

    references = import_sources._References()
    first = Path("first.csv")
    assert references.sift("bank", first, rows(("A", "2026-01-10", -500), ("", "2026-01-11", -700))) == (
        [True, True], [])
    # Two of A where the first file had one, a row with no reference, and B on A's day
    # and amount: the extra A is kept, and B is not taken for the first file's A.
    assert references.sift("bank", first, rows(
        ("A", "2026-01-10", -500), ("A", "2026-01-10", -500), ("", "2026-01-11", -700),
        ("B", "2026-01-10", -500))) == ([False, True, True, True], [])
    # Now both of A are known, in another source they are not, and a moved day is said.
    assert references.sift("bank", first, rows(("A", "2026-01-10", -500), ("A", "2026-01-12", -500)))[0] == [
        False, False]
    assert references.sift("card", first, rows(("A", "2026-01-10", -500)))[0] == [True]
    fresh, notes = references.sift("bank", Path("later.csv"), rows(("B", "2026-01-13", -500), ("C", "2026-01-11", -1)))
    assert fresh == [False, True] and len(notes) == 1 and "1 transaction(s) of 'Checking' in later.csv" in notes[0]


def test_ofx_notes_a_payee_that_may_name_no_one(ofx_bank_download: Path, rewrite_download: Rewrite) -> None:
    """Some banks write the kind of payment as the payee and the merchant in the memo."""
    def generic(raw: bytes) -> bytes:
        for name in (b"CITY POWER AND WATER BILL PAY 260102", b"ALICE EMPLOYER PAYROLL 260115",
                     b"CITY POWER AND WATER BILL PAY 260201", b"ONLINE TRANSFER TO SAVINGS REF #IB0XYZ12"):
            raw = raw.replace(b"<NAME>" + name, b"<NAME>POS PURCHASE\n<MEMO>" + name)
        return raw.replace(b"<NAME>CORNER MARKET #204 OAKLAND CA", b"<NAME>POS PURCHASE\n<MEMO>CORNER MARKET", 1) \
            .replace(b"<NAME>CORNER MARKET #204 OAKLAND CA", b"<NAME>POS PURCHASE\n<MEMO>HARDWARE STORE #7")

    assert not import_ofx.read_transactions(ofx_bank_download).notes
    assert import_ofx.read_transactions(rewrite_download(ofx_bank_download, generic)).notes == (
        "First Example Bank checking ...4321: 6 transactions have the payee 'pos purchase', with 5 different "
        "memos; if the memo is where this institution names who was paid, a payee rule gives them all one "
        "category",)


def test_ofx_notes_what_is_not_in_dollars(
    ofx_bank_download: Path, ofx_brokerage_download: Path, rewrite_download: Rewrite,
) -> None:
    assert not import_ofx.read_transactions(ofx_bank_download).notes
    assert not import_ofx.read_holdings(ofx_bank_download).notes

    elsewhere = rewrite_download(ofx_bank_download, lambda raw: raw.replace(b"<CURDEF>USD", b"<CURDEF>CAD"))
    said = ("First Example Bank checking ...4321 is in CAD; its amounts are read as they are, not converted",)
    assert import_ofx.read_transactions(elsewhere).notes == said
    assert import_ofx.read_holdings(elsewhere).notes == said

    abroad = rewrite_download(ofx_bank_download, lambda raw: raw.replace(
        b"<FITID>B0003", b"<CURRENCY>\n<CURRATE>1.1\n<CURSYM>EUR\n</CURRENCY>\n<FITID>B0003"))
    assert import_sources.read_flows([abroad]).notes == (
        "First Example Bank checking ...4321 has amounts in EUR, read as they are and not converted",)
    euros = rewrite_download(ofx_brokerage_download, lambda raw: raw.replace(
        b"<UNITS>2500<", b"<CURRENCY><CURRATE>1.1</CURRATE><CURSYM>EUR</CURSYM></CURRENCY><UNITS>2500<"))
    assert import_ofx.read_holdings(euros).notes == (
        "Example Brokerage investment ...5678 has amounts in EUR, read as they are and not converted",)


def test_ofx_reads_amounts_as_institutions_write_them(
    ofx_bank_download: Path, ofx_brokerage_download: Path, rewrite_download: Rewrite,
) -> None:
    def amounts(*pairs: tuple[bytes, bytes]) -> list[int]:
        def change(raw: bytes) -> bytes:
            for old, new in pairs:
                raw = raw.replace(old, new)
            return raw
        return list(import_ofx.read_transactions(rewrite_download(ofx_bank_download, change))
                    .transactions["amount_cents"])

    # A thousands separator (which the specification forbids), a decimal comma (which it
    # allows), both the other way round, and no cents at all.
    assert amounts((b"5000.00", b"5,000.00"), (b"-400.00", b"-400,00"), (b"-1000.00", b"-1.000,00"),
                   (b"-200.00", b"-200")) == [-15000, 500000, -40000, -15000, -100000, -20000]
    # Whole dollars with a thousands separator are thousands, not dollars and a thousandth.
    assert amounts((b"5000.00", b"5,000"), (b"-1000.00", b"-1,500"), (b"-400.00", b"-4,5"),
                   (b"-200.00", b"-1,234,567")) == [-15000, 500000, -450, -15000, -150000, -123456700]
    # No digit before the separator, a sign, and points for thousands.
    assert amounts((b"5000.00", b"+5.000.000"), (b"-1000.00", b"-,50"), (b"-400.00", b"-.5"),
                   (b"-200.00", b"-200.")) == [-15000, 500000000, -50, -15000, -50, -20000]

    # A fraction of a cent is not rounded away.
    for amount, why in (("-200.005", "has a fraction of a cent"), ("-1,2345", "has a fraction of a cent"),
                        # A lone point is a decimal point, and so is a comma after a leading zero.
                        ("-1.234", "has a fraction of a cent"), ("-0,125", "has a fraction of a cent"),
                        # Separators that are not groups of three are no number.
                        ("-1,000,00", "is not an amount"), ("-12,34,567", "is not an amount"),
                        ("-1.000.00", "is not an amount"), ("-", "is not an amount"), (".", "is not an amount"),
                        ("two hundred", "is not an amount"), ("NaN", "is not an amount"),
                        # Full-width digits are no digits of a number here.
                        ("-１,２３４", "is not an amount")):
        with pytest.raises(ValueError, match=f"TRNAMT '{amount}' {why}"):
            amounts((b"-200.00", amount.encode()))

    def brokerage(old: bytes, new: bytes) -> Path:
        return rewrite_download(ofx_brokerage_download, lambda raw: raw.replace(old, new))

    # A position's value is shares times a price, which a brokerage may not round.
    assert import_ofx.read_holdings(brokerage(b"400000.00", b"400000.005")).holdings[0].value_cents == 40000001
    # A value with thousands separators is thousands. One that reads two ways is refused,
    # as shares are: an unrounded value may have three decimals.
    assert import_ofx.read_holdings(brokerage(b"<MKTVAL>400000.00", b"<MKTVAL>400,000.00")
                                    ).holdings[0].value_cents == 40000000
    for tag in ("MKTVAL", "AVAILCASH"):
        with pytest.raises(ValueError, match=f"{tag} '234,567' could be thousands or a decimal"):
            import_ofx.read_holdings(brokerage(f"<{tag}>".encode(), f"<{tag}>234,567</{tag}><{tag}>".encode()))
    with pytest.raises(ValueError, match="UNITS '2,500' could be thousands or a decimal"):
        import_ofx.read_holdings(brokerage(b"<UNITS>2500<", b"<UNITS>2,500<"))
    assert import_ofx.read_holdings(brokerage(b"<UNITS>2500<", b"<UNITS>2.500,25<")
                                    ).holdings[0].quantity == Decimal("2500.25")
    # No transactions, and the columns still have their types.
    empty = import_ofx.read_transactions(ofx_brokerage_download).transactions
    assert (empty["source_row"].dtype, empty["amount_cents"].dtype) == ("int64", "int64")


def test_ofx_reads_a_brokerage_s_cash_that_no_position_holds(
    ofx_brokerage_download: Path, rewrite_download: Rewrite,
) -> None:
    def beside_the_funds(*pairs: tuple[bytes, bytes]) -> tuple[list[tuple[str, int]], tuple[str, ...]]:
        def change(raw: bytes) -> bytes:
            for old, new in pairs:
                raw = raw.replace(old, new)
            return raw
        read = import_ofx.read_holdings(rewrite_download(ofx_brokerage_download, change))
        assert [each.asset for each in read.holdings[:2]] == ["VTSAX", "VMFXX"]
        # Each is named apart from the positions, so the mapping can place it apart.
        assert all(each.asset == f"({each.hints['name']})" for each in read.holdings[2:])
        return [(each.hints["name"], each.value_cents) for each in read.holdings[2:]], read.notes

    # All of the cash is in the money market fund, which is a position: not counted twice.
    # The cash beside it and the loan are holdings though they are nothing: an earlier
    # import's cash lot is then set to nothing, and not left as it was.
    nothing = [("cash", 0), ("margin", 0)]
    assert beside_the_funds() == (nothing, ())
    # 750 more than the fund holds: the account's core cash, with no position of its own.
    # Which positions are the cash is a guess, and said.
    assert beside_the_funds((b"<AVAILCASH>20000.00", b"<AVAILCASH>20750.00")) == ([("cash", 75000), ("margin", 0)], (
        "Example Brokerage investment ...5678: 750.00 of its cash of 20750.00 is added, taking the rest to be its "
        "position(s) priced at a dollar a share (20000.00); check the account's total",))
    # Less cash than the fund holds: the fund may be an investment and the cash apart from it.
    added, notes = beside_the_funds((b"<AVAILCASH>20000.00", b"<AVAILCASH>2000.00"))
    assert added == nothing
    assert notes[0].startswith("Example Brokerage investment ...5678: its cash of 2000.00 is not added")
    # The fund by its price of a dollar, though its shares and its rounded value differ.
    assert beside_the_funds((b"<UNITS>20000<", b"<UNITS>20000.567<"), (b"<MKTVAL>20000.00", b"<MKTVAL>20000.57"),
                            (b"<AVAILCASH>20000.00", b"<AVAILCASH>20000.567")) == (nothing, ())
    # A CD at par has as many units as dollars and is no sweep fund: priced in percent.
    assert beside_the_funds((b"<UNITPRICE>1.00", b"<UNITPRICE>100.00"),
                            (b"<AVAILCASH>20000.00", b"<AVAILCASH>5000.00")) == ([("cash", 500000), ("margin", 0)], ())
    # Borrowed on margin: less than no cash, or a margin balance the cash doesn't show.
    assert beside_the_funds((b"<UNITPRICE>1.00", b"<UNITPRICE>100.00"),
                            (b"<AVAILCASH>20000.00", b"<AVAILCASH>-2000.00"),
                            (b"<MARGINBALANCE>0", b"<MARGINBALANCE>-2000.00")) == (
        [("cash", 0), ("margin", -200000)], ())
    # Cash below zero is all the cash too: the fund priced at a dollar is not counted beside it.
    assert beside_the_funds((b"<AVAILCASH>20000.00", b"<AVAILCASH>-500.00")) == ([("cash", 0), ("margin", -2050000)], (
        "Example Brokerage investment ...5678: -20500.00 is added as borrowed, taking its cash of -500.00 to "
        "include its position(s) priced at a dollar a share (20000.00); check the account's total",))
    assert beside_the_funds((b"<AVAILCASH>20000.00", b"<AVAILCASH>20750.00"),
                            (b"<MARGINBALANCE>0", b"<MARGINBALANCE>-1500"))[0] == [("cash", 75000), ("margin", -150000)]
    # A loan the file states is taken as stated, whatever the cash below zero would come to.
    assert beside_the_funds((b"<AVAILCASH>20000.00", b"<AVAILCASH>-500.00"),
                            (b"<MARGINBALANCE>0", b"<MARGINBALANCE>-500.00")) == ([("cash", 0), ("margin", -50000)], ())
    # Balances left unclosed are read as they are when closed.
    assert beside_the_funds((b"<AVAILCASH>20000.00", b"<AVAILCASH>20750.00"), (b"</INVBAL>", b""))[0] == [
        ("cash", 75000), ("margin", 0)]

    def changed(old: bytes, new: bytes) -> Path:
        return rewrite_download(ofx_brokerage_download, lambda raw: raw.replace(old, new))

    # Cash's basis is what it is worth: a taxable cash lot needs no other.
    core = import_ofx.read_holdings(changed(b"<AVAILCASH>20000.00", b"<AVAILCASH>20750.00"))
    assert [each.cost_basis_cents for each in core.holdings] == [None, None, 75000, 0]
    # The price says which position is the cash, so one that can't be read is refused, and
    # one left out is said: taken silently as some other price, the cash would be added twice.
    for price, why in (("1,000", "could be thousands or a decimal"), ("N/A", "is not a number")):
        with pytest.raises(ValueError, match=f"UNITPRICE '{price}' {why}"):
            import_ofx.read_holdings(changed(b"<UNITPRICE>1.00", f"<UNITPRICE>{price}".encode()))
    unpriced = import_ofx.read_holdings(changed(b"<UNITPRICE>1.00</UNITPRICE>", b""))
    assert unpriced.holdings[2].value_cents == 2000000
    assert unpriced.notes == (
        "Example Brokerage investment ...5678: its position VMFXX has no price, so it is taken not to be the "
        "account's cash; if it is (a sweep or money market fund), that cash is counted twice",)

    # A list of positions is all the account holds, even an empty one; a download that
    # leaves the list out says nothing of them, and its cash may be a lot's.
    assert core.complete == {"Example Brokerage investment ...5678"}

    def listless(raw: bytes) -> bytes:
        return raw[:raw.index(b"<INVPOSLIST>")] + raw[raw.index(b"</INVPOSLIST>") + len(b"</INVPOSLIST>"):]
    without = import_ofx.read_holdings(rewrite_download(ofx_brokerage_download, listless))
    assert not without.complete and [each.value_cents for each in without.holdings] == [2000000, 0]
    assert without.notes == (
        "Example Brokerage investment ...5678: the file leaves out its positions, so all its cash of 20000.00 "
        "is added; if a lot of the account holds some of it (a sweep or money market fund), that is counted "
        "twice: download the account with its positions",)


def test_sim_import_holdings_stores_what_a_card_owes_whichever_sign_its_file_has(
    test_data_dir: Path, ofx_card_download: Path, rewrite_download: Rewrite, write_import_map: MapWriter,
) -> None:
    write_import_map("ofx/accounts.csv", "Example Card card ...1007,,map,Liability_Mortgage,,\n")

    owed = _import(test_data_dir, "holdings", str(ofx_card_download))
    assert owed.exit_code == 0, owed.output
    assert "credit balance" not in owed.output
    assert _lots(test_data_dir)["Liability_Mortgage"]["opening_balance"] == "250.00"

    # Some issuers write what is owed as a positive amount: the same debt, and said.
    positive = rewrite_download(ofx_card_download, lambda raw: raw.replace(b"-250.00", b"310.00"))
    result = _import(test_data_dir, "holdings", str(positive))
    assert result.exit_code == 0, result.output
    assert "unusual credit balance in ofx" in result.output
    assert _lots(test_data_dir)["Liability_Mortgage"]["opening_balance"] == "310.00"


def test_sim_import_transactions_reads_ofx_files_by_payee_and_drops_what_two_downloads_share(
    test_data_dir: Path, ofx_bank_download: Path, ofx_bank_later_download: Path, ofx_card_download: Path,
    write_import_map: MapWriter,
) -> None:
    write_import_map("expense_accounts.csv", "Food,Expense_Living\nUtilities,Expense_Housing\n")

    # No payees.csv yet: the payees are listed, as for a bank's CSV.
    refused = _transactions(test_data_dir, ofx_bank_download)
    assert refused.exit_code != 0 and "payees.csv" in refused.output

    # One map for every institution's OFX file.
    write_import_map("ofx/payees.csv", _OFX_PAYEES)
    result = _transactions(test_data_dir, ofx_bank_download, str(ofx_bank_later_download),
                           str(ofx_card_download))

    assert result.exit_code == 0, result.output
    # February is in both of the bank's downloads, and counted once.
    assert "3 transaction(s) that an earlier file had too, by their reference, were left out" in result.output
    rules = _generated(test_data_dir)
    # Over three months: food is 400 + 200 + 90 from the bank and 80 on the card, and the
    # utility's two payments (March's isn't in yet).
    assert {name: (rule["amount"], rule["to_account"]) for name, rule in rules.items()} == {
        "baseline_expense_food": ("256.67", "Expense_Living"),
        "baseline_expense_utilities": ("100.00", "Expense_Housing"),
    }
    assert json.loads(rules["baseline_expense_food"]["meta_json"])["source"] == "ofx"


def test_sim_import_holdings_sets_a_balance_from_an_ofx_statement(
    test_data_dir: Path, ofx_bank_download: Path, ofx_card_download: Path, write_import_map: MapWriter,
    rewrite_download: Rewrite,
) -> None:
    write_import_map("ofx/accounts.csv", "First Example Bank checking ...4321,,map,Asset_Checking,,\n")

    result = _import(test_data_dir, "holdings", str(ofx_bank_download))

    assert result.exit_code == 0, result.output
    assert "ofx snapshot as of 2026-02-28" in result.output
    assert _lots(test_data_dir)["Asset_Checking"]["opening_balance"] == "4321.09"

    # A statement with no balance says so, and not nothing.
    bare = rewrite_download(ofx_bank_download, lambda raw: raw[:raw.index(b"<LEDGERBAL>")]
                            + raw[raw.index(b"</LEDGERBAL>\n") + len(b"</LEDGERBAL>\n"):])
    assert import_ofx.read_holdings(bare).notes == (
        "First Example Bank checking ...4321: the file has no balance for it (no LEDGERBAL), so its balance is "
        "left as it is",)

    # The card's file is another outside account, which nothing maps yet.
    card = _import(test_data_dir, "holdings", str(ofx_card_download))
    assert card.exit_code != 0 and "Example Card card ...1007" in card.output


@pytest.mark.usefixtures("at_a_terminal")
def test_sim_import_holdings_asks_which_lot_a_bank_balance_goes_into_and_adds_a_new_one(
    test_data_dir: Path, ofx_bank_download: Path, ofx_bank_like_accounts_download: Path,
) -> None:
    # Enter takes the account's one lot, and the row names none, as before there was a question.
    one = _import(test_data_dir, "holdings", str(ofx_bank_download), "--dry-run", input="Asset_Checking\n\n")
    assert one.exit_code == 0, one.output
    assert "lots: default; a new name adds a lot to Asset_Checking" in one.output
    assert "lot of Asset_Checking [default]:" in one.output

    # A bank's checking and savings as two lots of one account: each new
    # name is asked about (a slip isn't a lot), then added with the balance.
    result = _import(test_data_dir, "holdings", str(ofx_bank_like_accounts_download),
                     input="Asset_Checking\nchecking\ny\nAsset_Checking\nsaving\nn\nsavings\ny\n")
    assert result.exit_code == 0, result.output
    assert "Add lot saving to Asset_Checking? [y/N]" in result.output
    with (test_data_dir / "scenarios" / "retire_alice" / "account_lots.csv").open(encoding="utf-8") as f:
        lots = {row["lot"]: row["opening_balance"] for row in csv.DictReader(f)
                if row["account_name"] == "Asset_Checking"}
    assert lots["checking"] == lots["savings"] == "4321.09" and "saving" not in lots
    assert _saved(test_data_dir, "ofx") == [
        "First Example Bank checking ...4321,,map,Asset_Checking,checking,",
        "First Example Bank savings ...4321,,map,Asset_Checking,savings,"]


def _downloaded_in_june(raw: bytes) -> bytes:
    """``ofx_bank_download`` as made on June 15 with everything since
    January 1: the move to savings in May, a purchase in June, and a bill
    paid after the balance's date."""
    return (raw.replace(b"<DTEND>20260228", b"<DTEND>20260630")
            .replace(b"<DTASOF>20260228", b"<DTASOF>20260615")
            .replace(b"<DTPOSTED>20260205", b"<DTPOSTED>20260510")
            .replace(b"<DTPOSTED>20260220", b"<DTPOSTED>20260605")
            .replace(b"<DTPOSTED>20260201", b"<DTPOSTED>20260620"))


def test_sim_import_holdings_works_an_ofx_balance_back_to_the_scenario_s_start(
    test_data_dir: Path, ofx_bank_download: Path, write_import_map: MapWriter, rewrite_download: Rewrite,
) -> None:
    write_import_map("ofx/accounts.csv", "First Example Bank checking ...4321,,map,Asset_Checking,,\n")
    # The scenario starts in May: 1000.00 and 200.00 have left the account since.
    result = _import(test_data_dir, "holdings", str(rewrite_download(ofx_bank_download, _downloaded_in_june)))

    assert result.exit_code == 0, result.output
    assert "ofx snapshot as of 2026-06-15" in result.output
    assert ("note: First Example Bank checking ...4321: its balance of 4321.09 as of 2026-06-15 is worked "
            "back to 2026-05-01: what was posted from that day comes to -1200.00, so it opened the day with "
            "5521.09") in result.output
    assert "warning: the file is as of" not in result.output
    assert _lots(test_data_dir)["Asset_Checking"]["opening_balance"] == "5521.09"

    # A download that starts after the scenario does can't say what it opened with.
    short = rewrite_download(ofx_bank_download, lambda raw: _downloaded_in_june(raw).replace(
        b"<DTSTART>20260101", b"<DTSTART>20260515"), name="short.qfx")
    result = _import(test_data_dir, "holdings", str(short))
    assert result.exit_code == 0, result.output
    assert ("note: First Example Bank checking ...4321: the file's transactions are those of 2026-05-15 to "
            "2026-06-30, so its balance can't be worked back to 2026-05-01; a download of every day from "
            "2026-05-01 to the day it is made can be") in result.output
    assert "warning: the file is as of 2026-06-15, and its balances are being set as of 2026-05-01" in result.output
    assert _lots(test_data_dir)["Asset_Checking"]["opening_balance"] == "4321.09"


def test_holdings_are_worked_back_only_where_the_file_accounts_for_the_days_between(
    ofx_bank_download: Path, ofx_card_download: Path, ofx_brokerage_download: Path, rewrite_download: Rewrite,
) -> None:
    june = import_ofx.read_holdings(rewrite_download(ofx_bank_download, _downloaded_in_june))
    account = "First Example Bank checking ...4321"

    # Last year's end is the balance before January 1: everything up to the
    # balance's date comes off, and what was posted after it doesn't.
    year_end, back, _ = import_holdings.at(june, date(2026, 1, 1))
    assert [each.value_cents for each in year_end.holdings] == [432109 - 325000]
    assert back == {account} and year_end.as_of == date(2026, 1, 1)

    # A file older than the day wanted, or one that ends before its balance's date, says nothing of it.
    assert import_holdings.at(june, date(2026, 7, 1)) == (june, frozenset(), ())
    ended = import_ofx.read_holdings(rewrite_download(ofx_bank_download, lambda raw: _downloaded_in_june(raw).replace(
        b"<DTEND>20260630", b"<DTEND>20260601"), name="ended.qfx"))
    unchanged, back, notes = import_holdings.at(ended, date(2026, 5, 1))
    assert unchanged == ended and not back and "2026-01-01 to 2026-06-01" in notes[0]

    # A correction may be of a transaction in another file, so the amounts don't add up to the change.
    corrected = rewrite_download(ofx_bank_download, lambda raw: raw.replace(
        b"<FITID>B0006", b"<FITID>B0007\n<CORRECTFITID>B0006\n<CORRECTACTION>REPLACE"), name="corrected.qfx")
    refused = import_ofx.read_holdings(corrected)
    assert not refused.activity
    # Never silently: a balance wanted as of an earlier day says why it is the file's day's.
    assert import_holdings.at(refused, date(2026, 1, 1)) == (refused, frozenset(), (
        f"{account}: its balance can't be worked back to 2026-01-01: the file has a correction of a "
        "transaction, so its amounts don't add up to the change",))
    undated = import_ofx.read_holdings(rewrite_download(ofx_bank_download, lambda raw: raw.replace(
        b"<DTSTART>20260101\n<DTEND>20260228\n", b""), name="undated.qfx"))
    assert "no DTSTART and DTEND" in import_holdings.at(undated, date(2026, 1, 1))[2][0]
    # What a card owes written above zero has the other sign from its transactions.
    assert import_ofx.read_holdings(ofx_card_download).activity
    positive = rewrite_download(ofx_card_download, lambda raw: raw.replace(b"-250.00", b"310.00"))
    owed = import_ofx.read_holdings(positive)
    assert not owed.activity and "above zero" in import_holdings.at(owed, date(2026, 2, 1))[2][0]
    # Positions on an earlier day can't be had from a brokerage's file.
    brokerage = import_ofx.read_holdings(ofx_brokerage_download)
    assert not brokerage.activity

    # With a holding that isn't worked back, the snapshot is still of the file's day.
    mixed = dataclasses.replace(june, holdings=(*june.holdings, *brokerage.holdings))
    partly, back, _ = import_holdings.at(mixed, date(2026, 5, 1))
    assert back == {account} and partly.as_of == date(2026, 6, 15)
    assert partly.holdings[0].value_cents == 552109 and partly.holdings[1:] == brokerage.holdings


def test_sim_import_holdings_reads_an_ofx_brokerage_s_positions_by_symbol(
    test_data_dir: Path, ofx_brokerage_download: Path, rewrite_download: Rewrite, write_import_map: MapWriter,
) -> None:
    write_import_map("ofx/accounts.csv", "Example Brokerage investment ...5678,,map,Asset_Roth_Bill,,\n")
    # What the reader guessed about the account's cash is said before anything is written.
    core = rewrite_download(ofx_brokerage_download,
                            lambda raw: raw.replace(b"<AVAILCASH>20000.00", b"<AVAILCASH>20750.00"))
    looked = _import(test_data_dir, "holdings", str(core), "--new-lots", "--dry-run")
    assert "note: Example Brokerage investment ...5678: 750.00 of its cash of 20750.00 is added" in looked.output

    result = _import(test_data_dir, "holdings", str(ofx_brokerage_download), "--new-lots")

    assert result.exit_code == 0, result.output
    assert "2 new" in result.output and "ofx snapshot as of 2026-01-05" in result.output
    lots = _all_lots(test_data_dir)
    stock = lots["Asset_Roth_Bill", "vtsax"]
    assert (stock["symbol"], stock["quantity"], stock["opening_balance"]) == ("VTSAX", "2500", "400000.00")
    assert lots["Asset_Roth_Bill", "vmfxx"]["opening_balance"] == "20000.00"


def test_ofx_reads_a_brokerage_s_banking_and_not_its_trades(
    ofx_brokerage_download: Path, rewrite_download: Rewrite,
) -> None:
    # The purchase in the file is investment activity, not spending.
    assert import_ofx.read_transactions(ofx_brokerage_download).transactions.empty
    # A check written on the brokerage account is spending, as a bank account's is.
    banking = rewrite_download(ofx_brokerage_download, lambda raw: raw.replace(
        b"</INVTRANLIST>",
        b"<INVBANKTRAN><STMTTRN><TRNTYPE>CHECK</TRNTYPE><DTPOSTED>20260103</DTPOSTED><TRNAMT>-125.00</TRNAMT>"
        b"<FITID>K1</FITID><CHECKNUM>1001</CHECKNUM><NAME>CORNER MARKET</NAME></STMTTRN>"
        b"<SUBACCTFUND>CASH</SUBACCTFUND></INVBANKTRAN></INVTRANLIST>"))
    spent = import_ofx.read_transactions(banking).transactions
    assert list(zip(spent["account"], spent["payee"], spent["amount_cents"], spent["reference"], strict=True)) == [
        ("Example Brokerage investment ...5678", "CORNER MARKET", -12500, "K1")]


def test_sim_import_holdings_asks_no_lot_for_a_brokerage_with_cash_beside_its_positions(
    test_data_dir: Path, ofx_brokerage_download: Path, rewrite_download: Rewrite, write_import_map: MapWriter,
    at_a_terminal: None,
) -> None:
    """The cash has no symbol; an answer naming one lot would put every position in it."""
    write_import_map("ofx/accounts.csv", "Example Brokerage investment ...5678,,map,Asset_Roth_Bill,,\n")
    first = _import(test_data_dir, "holdings", str(ofx_brokerage_download), "--new-lots", input="\n\n\n\n")
    assert first.exit_code == 0, first.output
    write_import_map("ofx/accounts.csv", "")
    core = rewrite_download(ofx_brokerage_download,
                            lambda raw: raw.replace(b"<AVAILCASH>20000.00", b"<AVAILCASH>20750.00"))

    result = _import(test_data_dir, "holdings", str(core), input="Asset_Roth_Bill\n")

    assert result.exit_code == 0, result.output
    assert "lot of" not in result.output
    assert _saved(test_data_dir, "ofx") == ["Example Brokerage investment ...5678,,map,Asset_Roth_Bill,,"]
    lots = _all_lots(test_data_dir)
    assert lots["Asset_Roth_Bill", "vtsax"]["opening_balance"] == "400000.00"
    assert lots["Asset_Roth_Bill", "vmfxx"]["opening_balance"] == "20000.00"
    assert lots["Asset_Roth_Bill", "default"]["opening_balance"] == "750.00"


def test_sim_import_holdings_gives_a_new_account_s_cash_a_lot_of_its_own(
    test_data_dir: Path, ofx_brokerage_download: Path, rewrite_download: Rewrite, at_a_terminal: None,
) -> None:
    """A new account has no lot yet: the cash is asked about once, as a lot, and not as an
    account the scenario lacks, which would move the positions with it."""
    core = rewrite_download(ofx_brokerage_download, lambda raw: raw.replace(
        b"<AVAILCASH>20000.00", b"<AVAILCASH>20750.00").replace(b"<MARGINBALANCE>0", b"<MARGINBALANCE>-100.00"))

    result = _import(test_data_dir, "holdings", str(core), "--new-lots",
                     input="Asset_New_Brokerage\ny\nasset\nbrokerage_taxable\njoint\n")

    assert result.exit_code == 0, result.output
    assert "doesn't have" not in result.output
    lots = _all_lots(test_data_dir)
    assert lots["Asset_New_Brokerage", "vtsax"]["opening_balance"] == "400000.00"
    assert lots["Asset_New_Brokerage", "vmfxx"]["opening_balance"] == "20000.00"
    # The cash and what is borrowed, in one cash lot, whose basis is what it holds.
    cash = lots["Asset_New_Brokerage", "cash"]
    assert (cash["symbol"], cash["opening_balance"], cash["opening_cost_basis"]) == ("CASH", "650.00", "650.00")
    # The plan knows what a cash lot holds; the funds have no basis in the file, which is said.
    assert "doesn't know VMFXX, VTSAX yet" in result.output
    assert ("note: the ofx file has no cost basis for Asset_New_Brokerage's new lot(s) vtsax, vmfxx. A run stops "
            "at the first withdrawal from a taxable lot with none") in result.output
    assert _saved(test_data_dir, "ofx") == ["Example Brokerage investment ...5678,,map,Asset_New_Brokerage,,"]
