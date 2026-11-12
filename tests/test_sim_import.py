"""The `sim import` commands (issue #527)."""

from __future__ import annotations

import csv
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim import cli


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

    result = _import(test_data_dir, "holdings", str(quicken_net_worth_export), input=answers)
    assert result.exit_code == 0, result.output
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
