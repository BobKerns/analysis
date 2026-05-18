"""Smoke + negative tests for the intent loader."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from analysis.sim.loader import LoadError, load_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = REPO_ROOT / "prototype_data"


def test_load_retire_alice(tmp_path: Path) -> None:
    result = load_scenario(
        PROTOTYPE, "retire_alice", intent_dir=tmp_path, loaded_by="test",
    )
    assert result.intent_path.exists()

    con = sqlite3.connect(result.intent_path)
    con.row_factory = sqlite3.Row
    try:
        person_ids = {r["id"] for r in con.execute("SELECT id FROM person")}
        assert {"alice", "bob"} <= person_ids

        account_names = {r["name"] for r in con.execute("SELECT name FROM account")}
        assert "Asset_Checking" in account_names
        assert "Expense_Interest_Mortgage" in account_names

        kinds = {
            r["amount_rule_kind"]
            for r in con.execute("SELECT amount_rule_kind FROM scheduled_transaction")
        }
        assert kinds == {
            "fixed_amount", "percent_of_balance", "mortgage_payment",
            "ss_benefit", "rmd", "waterfall_fill",
        }

        cal_kinds = {r["kind"] for r in con.execute("SELECT kind FROM calendar_event")}
        assert {"set_rate", "retire_scheduled", "set_amount", "one_off_transfer"} <= cal_kinds

        first_source_id, from_account_id = con.execute("""
            SELECT s.account_id, st.from_account
            FROM scheduled_transaction st
            JOIN scheduled_transaction_source s
                ON s.scheduled_transaction_id = st.id
            WHERE st.name = 'waterfall_topup' AND s.position = 1
        """).fetchone()
        assert first_source_id == from_account_id

        rmd_rows = con.execute(
            "SELECT COUNT(*) FROM aux_table WHERE kind = 'rmd_factors'"
        ).fetchone()[0]
        assert rmd_rows >= 6

        priors = con.execute(
            "SELECT COUNT(*) FROM prior_account_fact"
        ).fetchone()[0]
        assert priors == 2

        manifest = con.execute("SELECT * FROM intent_manifest").fetchone()
        assert manifest["scenario_name"] == "retire_alice"
        assert manifest["loaded_by"] == "test"
    finally:
        con.close()


def test_invalid_subkind_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "repo"
    (bad / "scenarios" / "x").mkdir(parents=True)
    (bad / "aux").mkdir()
    (bad / "account_models").mkdir()
    (bad / "persons.csv").write_text(
        "id,display,birthdate,meta_json\np1,P1,1970-01-01,{}\n"
    )
    (bad / "accounts.csv").write_text(
        "name,kind,subkind,owner,tax_treatment,opened,closed,meta_json\n"
        "A,asset,bogus_subkind,,taxable,,,{}\n"
    )
    (bad / "account_models" / "m.csv").write_text(
        "account_name,opening_balance,opening_cost_basis\n"
    )
    (bad / "scenarios" / "x" / "scenario.csv").write_text(
        "name,description,account_model,start,end,seed,"
        "parent_scenario,created_by,meta_json\n"
        "x,,m,2026-01-31,2026-12-31,,,test,{}\n"
    )
    (bad / "scenarios" / "x" / "scheduled.csv").write_text(
        "name,from_account,to_account,cadence,month_of_year,amount_rule_kind,"
        "amount_rule_params_json,base_amount,base_date,inflation_series,"
        "tax_character,start,end,priority,description,meta_json\n"
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(bad, "x", intent_dir=tmp_path / "out")
    assert any("bogus_subkind" in issue for issue in ei.value.issues)


def test_waterfall_from_account_mismatch_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "repo"
    (bad / "scenarios" / "x").mkdir(parents=True)
    (bad / "aux").mkdir()
    (bad / "account_models").mkdir()
    (bad / "persons.csv").write_text(
        "id,display,birthdate,meta_json\np1,P1,1970-01-01,{}\n"
    )
    (bad / "accounts.csv").write_text(
        "name,kind,subkind,owner,tax_treatment,opened,closed,meta_json\n"
        "Asset_Checking,asset,checking,,taxable,,,{}\n"
        "Asset_Savings,asset,savings,,taxable,,,{}\n"
        "Asset_Brokerage,asset,brokerage_taxable,,taxable,,,{}\n"
    )
    (bad / "account_models" / "m.csv").write_text(
        "account_name,opening_balance,opening_cost_basis\n"
        "Asset_Checking,100000,\nAsset_Savings,500000,\nAsset_Brokerage,1000000,\n"
    )
    (bad / "scenarios" / "x" / "scenario.csv").write_text(
        "name,description,account_model,start,end,seed,"
        "parent_scenario,created_by,meta_json\n"
        "x,,m,2026-01-31,2026-12-31,,,test,{}\n"
    )
    (bad / "scenarios" / "x" / "scheduled.csv").write_text(
        "name,from_account,to_account,cadence,month_of_year,amount_rule_kind,"
        "amount_rule_params_json,base_amount,base_date,inflation_series,"
        "tax_character,start,end,priority,description,meta_json\n"
        "wf,Asset_Brokerage,Asset_Checking,monthly,,waterfall_fill,{},,,,none,"
        "2026-01-31,,900,,{}\n"
    )
    (bad / "scenarios" / "x" / "waterfall_sources.csv").write_text(
        "scheduled_name,position,account_name\n"
        "wf,1,Asset_Savings\nwf,2,Asset_Brokerage\n"
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(bad, "x", intent_dir=tmp_path / "out")
    assert any("ADR-003 §2.4" in issue for issue in ei.value.issues)
