"""Retirement accounts whose lots say Roth or traditional (#217)."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim import registries
from analysis.sim.engine import run_scenario
from analysis.sim.loader import LoadError, LoadResult

IRA = "Asset_TraditionalIRA_Alice"
K401 = "Asset_401k_Alice"


def _run(load: Callable[[], LoadResult], runs_dir: Path, name: str = "run.sqlite3") -> Path:
    return run_scenario(load().intent_path, runs_dir / name, run_by="T",
                        allow_dirty_data=True, allow_dirty_code=True)


def _issues(load: Callable[[], LoadResult]) -> str:
    with pytest.raises(LoadError) as error:
        load()
    return "\n".join(error.value.issues)


def _set_account(path: Path, name: str, **columns: str) -> None:
    accounts = pd.read_csv(path, dtype=str, keep_default_na=False)
    row = accounts["name"] == name
    for column, value in columns.items():
        accounts.loc[row, column] = value
    accounts.to_csv(path, index=False)


def test_a_lots_subkind_sets_its_tax_treatment(load_test_copy: Callable[[], LoadResult]) -> None:
    with sqlite3.connect(load_test_copy().intent_path) as con:
        lots = dict(con.execute(
            "SELECT a.name, m.subkind || ' ' || m.tax_treatment FROM account_lot m "
            "JOIN account a ON a.id = m.account_id WHERE a.subkind = 'retirement'"
        ).fetchall())
        brokerage = con.execute(
            "SELECT m.subkind, m.tax_treatment FROM account_lot m "
            "JOIN account a ON a.id = m.account_id WHERE a.name = 'Asset_Brokerage_Joint'"
        ).fetchone()
    assert lots == {
        IRA: "traditional_ira tax_deferred",
        "Asset_Roth_Alice": "roth_ira tax_free",
        "Asset_Roth_Bill": "roth_ira tax_free",
    }
    assert brokerage == (None, "taxable")  # anything else keeps its account's


def test_a_retirement_lot_needs_a_subkind_and_only_it_may_have_one(
    update_account_value: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    update_account_value("Asset_Roth_Bill", "default", subkind="")
    update_account_value("Asset_Brokerage_Joint", "default", subkind="roth_ira")
    update_account_value("Asset_Roth_Alice", "default", subkind="roth")
    issues = _issues(load_test_copy)
    assert "(Asset_Roth_Bill / default): a retirement account's lot needs a subkind" in issues
    assert ("(Asset_Brokerage_Joint / default): subkind 'roth_ira' is only for the lots "
            "of a retirement account") in issues
    assert "(Asset_Roth_Alice / default): unknown lot subkind 'roth'" in issues


def test_a_retirement_account_leaves_its_tax_treatment_to_its_lots(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    _set_account(retire_alice_csv("accounts.csv"), "Asset_Roth_Bill", tax_treatment="tax_free")
    assert "(Asset_Roth_Bill): a retirement account's tax_treatment" in _issues(load_test_copy)


def test_an_old_subkind_or_lots_file_names_the_migration(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    _set_account(retire_alice_csv("accounts.csv"), "Asset_Roth_Bill", subkind="roth_ira")
    lots = retire_alice_csv("account_lots.csv")
    (lots.parent / "account_values.csv").write_text(lots.read_text())
    issues = _issues(load_test_copy)
    assert "subkind 'roth_ira' is now account subkind 'retirement'" in issues
    assert "account_values.csv: renamed to account_lots.csv (#217)" in issues
    assert issues.count("migrate_217.py") == 2


@pytest.mark.parametrize(("source", "destination", "character"), [
    ("tax_deferred", "tax_deferred", "none"),  # a rollover
    ("tax_deferred", "tax_free", "tax_deferred_out"),  # a conversion
    ("tax_deferred", "none", "tax_deferred_out"),  # a distribution
    ("tax_free", "tax_free", "tax_free"),
])
def test_a_retirement_withdrawals_character_depends_on_where_it_goes(
    source: str, destination: str, character: str,
) -> None:
    assert registries.withdrawal_tax_character(source, "retirement", destination) == character


def _calendar_legs(run: Path, account: str) -> list[tuple[str, str, str, str]]:
    """The legs calendar events moved out of ``account``."""
    with sqlite3.connect(run) as con:
        return con.execute(
            "SELECT ce.kind, fm.lot, tm.lot, te.tax_character FROM transfer_event te "
            "JOIN scheduled_transaction s ON s.id = te.reason_id AND te.reason_kind = 'rule' "
            "JOIN calendar_event ce ON ce.id = s.created_by_event_id "
            "JOIN account_lot fm ON fm.id = te.from_lot_id "
            "JOIN account_lot tm ON tm.id = te.to_lot_id "
            "JOIN account a ON a.id = fm.account_id WHERE a.name = ? ORDER BY te.id",
            (account,),
        ).fetchall()


def test_a_calendar_conversion_is_taxed_and_a_rollover_is_not(
    add_alice_401k: Callable[[str], None], add_account_value: Callable[..., None],
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    runs_dir: Path,
) -> None:
    """A blank tax_character: traditional into a Roth lot of the same 401k is
    an in-plan conversion, traditional into a traditional IRA a rollover."""
    add_alice_401k("100000.00")
    add_account_value(K401, lot="roth", subkind="roth_401k", opening_balance="0.00")
    append_calendar_row(date="2029-01-31", kind="one_off_transfer", account_1=K401,
                        lot_1="default", account_2=K401, lot_2="roth", amount="20000.00")
    append_calendar_row(date="2030-01-31", kind="transfer_balance", account_1=K401,
                        lot_1="default", account_2=IRA, lot_2="default")
    assert _calendar_legs(_run(load_test_copy, runs_dir), K401) == [
        ("one_off_transfer", "default", "roth", "tax_deferred_out"),
        ("transfer_balance", "default", "default", "none"),
    ]


def test_blank_traditional_money_is_taxed_by_where_each_leg_lands(
    retire_alice_csv: Callable[[str], Path], append_calendar_row: Callable[..., None],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    """A blank tax_character out of a traditional lot is a distribution, into
    checking or through a chain into a Roth lot alike (#217)."""
    (retire_alice_csv("chains/destination") / "convert.csv").write_text(
        "account_name,lot,maximum,percent,series,cadence\n"
        "Asset_Roth_Alice,default,5000.00,,,\nAsset_Checking,,,,,\n")
    append_calendar_row(date="2029-01-31", kind="one_off_transfer", account_1=IRA,
                        lot_1="default", account_2="convert", amount="20000.00")
    append_calendar_row(date="2029-02-28", kind="one_off_transfer", account_1=IRA,
                        lot_1="default", account_2="Asset_Checking", amount="1000.00")
    with sqlite3.connect(_run(load_test_copy, runs_dir)) as con:
        legs = con.execute(
            "SELECT ta.name, te.amount, te.tax_character FROM transfer_event te "
            "JOIN scheduled_transaction s ON s.id = te.reason_id AND te.reason_kind = 'rule' "
            "JOIN account ta ON ta.id = te.to_account "
            "WHERE s.created_by_event_id IS NOT NULL AND te.from_account = "
            "(SELECT id FROM account WHERE name = ?) ORDER BY te.id",
            (IRA,),
        ).fetchall()
        rules = {character for (character,) in con.execute(
            "SELECT tax_character FROM scheduled_transaction WHERE created_by_event_id IS NOT NULL")}
    assert legs == [
        ("Asset_Roth_Alice", 500_000, "tax_deferred_out"),
        ("Asset_Checking", 1_500_000, "tax_deferred_out"),
        ("Asset_Checking", 100_000, "tax_deferred_out"),
    ]
    assert "return_of_capital" not in rules  # each rule records what its legs are


def test_an_rmd_counts_and_draws_only_traditional_lots(
    retire_alice_csv: Callable[[str], Path], add_account_value: Callable[..., None],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    """A Roth lot beside the IRA's traditional one owes no RMD and doesn't
    count toward one: the RMDs are what the traditional lot alone gives."""
    add_account_value(IRA, lot="roth", subkind="roth_ira", opening_balance="900000.00",
                      prior_year_end_balance="900000.00")
    scheduled_path = retire_alice_csv("scheduled.csv")
    scheduled = pd.read_csv(scheduled_path, dtype=str, keep_default_na=False)
    row = scheduled["name"] == "rmd_alice"
    scheduled.loc[row, ["from_account", "from_lot"]] = ["ira_rmd", ""]
    scheduled.loc[row, "params_json"] = json.dumps({"subject_account": IRA})
    scheduled.to_csv(scheduled_path, index=False)
    (retire_alice_csv("chains/source") / "ira_rmd.csv").write_text(
        f"account_name,lot,maximum,percent,series,cadence\n{IRA},default,,,,\n")
    with sqlite3.connect(_run(load_test_copy, runs_dir)) as con:
        amounts = [amount for (amount,) in con.execute(
            "SELECT te.amount FROM transfer_event te "
            "JOIN scheduled_transaction st ON st.id = te.reason_id AND te.reason_kind = 'rule' "
            "WHERE st.name = 'rmd_alice' ORDER BY te.id"
        )]
    assert amounts == [681_542, 558_190, 454_889]  # as with no Roth lot (test_rmd_chain)

    (retire_alice_csv("chains/source") / "ira_rmd.csv").write_text(
        f"account_name,lot,maximum,percent,series,cadence\n{IRA},default,,,,\n{IRA},roth,,,,\n")
    assert "an rmd may not draw from a Roth lot" in _issues(load_test_copy)


def test_account_add_gives_a_retirement_account_its_first_lot(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    result = CliRunner().invoke(main, [
        "sim", "account", "add", "Asset_Roth401k_Bill", "asset", "retirement",
        "--owner", "bill", "--lot-subkind", "roth_401k", "--opening-balance", "",
        "--scenario", "retire_alice", "--data-dir", str(test_data_dir),
    ])
    assert result.exit_code == 0, result.output
    accounts = pd.read_csv(retire_alice_csv("accounts.csv"), dtype=str, keep_default_na=False)
    assert accounts.loc[accounts["name"] == "Asset_Roth401k_Bill", "tax_treatment"].item() == ""
    lots = pd.read_csv(retire_alice_csv("account_lots.csv"), dtype=str, keep_default_na=False)
    lot = lots[lots["account_name"] == "Asset_Roth401k_Bill"].iloc[0]
    assert (lot["lot"], lot["subkind"], lot["opening_balance"]) == ("default", "roth_401k", "0.00")


def _migration():
    path = Path(__file__).parents[1] / "scripts" / "migrate_217.py"
    spec = importlib.util.spec_from_file_location("migrate_217", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclass looks its module up there
    spec.loader.exec_module(module)
    return module


def test_the_migration_moves_old_subkinds_onto_the_lots(tmp_path: Path) -> None:
    base, child = tmp_path / "scenarios" / "base", tmp_path / "scenarios" / "child"
    base.mkdir(parents=True)
    child.mkdir()
    (base / "accounts.csv").write_text(
        "name,kind,subkind,owner,tax_treatment\n"
        "IRA,asset,traditional_ira,alice,tax_deferred\n"
        "Roth,asset,roth_ira,alice,tax_free\n"
        "Cash,asset,cash,joint,taxable\n"
    )
    (base / "account_values.csv").write_text(
        "account_name,lot,symbol,opening_balance\nIRA,default,,100.00\nCash,,,5.00\n")
    (child / "account_values.csv").write_text(
        "account_name,lot,symbol,opening_balance\nIRA,b,VTI,7.00\n")
    migration = _migration()

    planned = migration.plan(tmp_path)
    assert planned.problems == []
    assert (base / "account_values.csv").exists()  # a dry run writes nothing
    migration.apply(planned)

    assert not (base / "account_values.csv").exists()
    assert (base / "accounts.csv").read_text().splitlines()[1:] == [
        "IRA,asset,retirement,alice,", "Roth,asset,retirement,alice,", "Cash,asset,cash,joint,taxable",
    ]
    assert (base / "account_lots.csv").read_text().splitlines() == [
        "account_name,lot,subkind,symbol,opening_balance",
        "IRA,default,traditional_ira,,100.00",
        "Cash,,,,5.00",
        "Roth,default,roth_ira,,0.00",  # it had no lot to carry its subkind
    ]
    assert (child / "account_lots.csv").read_text().splitlines()[1:] == ["IRA,b,traditional_ira,VTI,7.00"]
    assert migration.plan(tmp_path).problems == [f"{tmp_path}: no account_values.csv left; already migrated?"]


def test_the_migration_refuses_when_account_lots_already_exists(tmp_path: Path) -> None:
    folder = tmp_path / "scenarios" / "base"
    folder.mkdir(parents=True)
    (folder / "account_values.csv").write_text("account_name,lot,opening_balance\nCash,,5.00\n")
    (folder / "account_lots.csv").write_text("account_name,lot,subkind,opening_balance\nCash,,,9.00\n")
    assert _migration().plan(tmp_path).problems == [
        f"{folder / 'account_values.csv'}: account_lots.csv already exists beside it; merge them by hand",
    ]


def test_account_sync_leaves_a_retirement_account_to_lot_add(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    path = retire_alice_csv("accounts.csv")
    accounts = pd.read_csv(path, dtype=str, keep_default_na=False)
    added = {column: "" for column in accounts.columns}
    added |= {"name": "Asset_Roth401k_Bill", "kind": "asset", "subkind": "retirement", "owner": "bill"}
    pd.concat([accounts, pd.DataFrame([added])], ignore_index=True).to_csv(path, index=False)
    before = retire_alice_csv("account_lots.csv").read_text()

    result = CliRunner().invoke(main, [
        "sim", "account", "sync", "--scenario", "retire_alice", "--data-dir", str(test_data_dir),
    ])
    # The scenario fails to load until the lot is added, so nothing else is
    # written either; the message says what to run.
    assert "skipped Asset_Roth401k_Bill: a retirement account's lot needs a subkind" in result.output
    assert retire_alice_csv("account_lots.csv").read_text() == before


def test_the_migration_seeds_a_lot_where_the_account_is_defined(tmp_path: Path) -> None:
    """A lot only in a derived scenario doesn't stand in for one where the
    account is defined, and a folder with no lots file gets one."""
    base, child, other = (tmp_path / "scenarios" / name for name in ("base", "child", "other"))
    for folder in (base, child, other):
        folder.mkdir(parents=True)
    (base / "account_values.csv").write_text("account_name,lot,opening_balance\nCash,,5.00\n")
    (child / "accounts.csv").write_text("name,kind,subkind,owner,tax_treatment\nIRA,asset,traditional_ira,alice,\n")
    (other / "accounts.csv").write_text("name,kind,subkind,owner,tax_treatment\nRoth,asset,roth_ira,bill,\n")
    (base / "accounts.csv").write_text("name,kind,subkind,owner,tax_treatment\nIRA2,asset,roth_ira,bill,\n")
    (child / "account_values.csv").write_text(
        "account_name,lot,opening_balance\nIRA,default,7.00\nIRA2,b,1.00\n")
    migration = _migration()

    planned = migration.plan(tmp_path)
    assert planned.problems == []
    migration.apply(planned)
    assert (base / "account_lots.csv").read_text().splitlines()[1:] == [
        "Cash,,,5.00", "IRA2,default,roth_ira,0.00",
    ]
    assert (child / "account_lots.csv").read_text().splitlines()[1:] == [
        "IRA,default,traditional_ira,7.00", "IRA2,b,roth_ira,1.00",
    ]
    assert (other / "account_lots.csv").read_text().splitlines() == [
        "account_name,lot,subkind,opening_balance", "Roth,default,roth_ira,0.00",
    ]
