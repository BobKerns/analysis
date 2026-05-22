"""Smoke + negative tests for the intent loader."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import git

import pytest

from analysis.sim.loader import DataDirtyRefused, LoadError, load_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = REPO_ROOT / "prototype_data"
FIXTURE_DATA = Path(__file__).resolve().parent / "data" / "sim"


def test_load_retire_alice(tmp_path: Path) -> None:
    result = load_scenario(
        PROTOTYPE, "retire_alice", intent_dir=tmp_path, loaded_by="test",
        allow_dirty_data=True,
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

        tax_priors = con.execute(
            "SELECT year, bucket, amount FROM prior_tax_activity ORDER BY year, bucket"
        ).fetchall()
        assert len(tax_priors) == 3
        assert (2025, "ltcg_loss_carryforward", -150000) in [tuple(r) for r in tax_priors]

        # Partial-first-year RMD seed lives on account_model_member now.
        ira_seed = con.execute("""
            SELECT m.prior_year_end_balance
            FROM account_model_member m
            JOIN account a ON a.id = m.account_id
            WHERE a.name = 'Asset_TraditionalIRA_Alice'
        """).fetchone()[0]
        assert ira_seed == 48000000

        manifest = con.execute("SELECT * FROM intent_manifest").fetchone()
        assert manifest["scenario_name"] == "retire_alice"
        assert manifest["loaded_by"] == "test"
    finally:
        con.close()


def test_invalid_subkind_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "repo"
    shutil.copytree(FIXTURE_DATA / "bad_subkind", bad)
    with pytest.raises(LoadError) as ei:
        load_scenario(bad, "x", intent_dir=tmp_path / "out")
    assert any("bogus_subkind" in issue for issue in ei.value.issues)


def test_waterfall_from_account_mismatch_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "repo"
    shutil.copytree(FIXTURE_DATA / "bad_waterfall", bad)
    with pytest.raises(LoadError) as ei:
        load_scenario(bad, "x", intent_dir=tmp_path / "out")
    assert any("ADR-003 §2.4" in issue for issue in ei.value.issues)


# ---- ADR-004 git provenance ----


def _git_init_commit(repo: Path) -> None:
    r = git.Repo.init(repo, b="main")
    r.git.add(A=True)
    with r.config_writer() as cw:
        cw.set_value("commit", "gpgsign", "false")
    actor = git.Actor("t", "t@t")
    r.index.commit("init", author=actor, committer=actor)


def test_clean_git_repo_stamps_manifest(tmp_path: Path) -> None:
    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)
    _git_init_commit(repo)

    result = load_scenario(repo, "retire_alice", intent_dir=tmp_path / "out", loaded_by="test")
    con = sqlite3.connect(result.intent_path)
    con.row_factory = sqlite3.Row
    try:
        m = con.execute("SELECT * FROM intent_manifest").fetchone()
        assert m["data_dirty"] == 0
        assert len(m["data_version"]) in (40, 64)
        assert len(m["scenario_tree_id"]) in (40, 64)
        assert m["code_version"]
    finally:
        con.close()


def test_dirty_data_refused(tmp_path: Path) -> None:
    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)
    _git_init_commit(repo)
    persons = repo / "persons.csv"
    persons.write_text(persons.read_text() + "# scratch\n")

    with pytest.raises(DataDirtyRefused) as ei:
        load_scenario(repo, "retire_alice", intent_dir=tmp_path / "out")
    assert any("persons.csv" in p for p in ei.value.dirty_paths)


def test_dirty_data_allowed_records_dirty_flag(tmp_path: Path) -> None:
    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)
    _git_init_commit(repo)
    persons = repo / "persons.csv"
    persons.write_text(persons.read_text() + "# scratch\n")

    result = load_scenario(
        repo, "retire_alice", intent_dir=tmp_path / "out",
        allow_dirty_data=True,
    )
    con = sqlite3.connect(result.intent_path)
    con.row_factory = sqlite3.Row
    try:
        m = con.execute("SELECT * FROM intent_manifest").fetchone()
        assert m["data_dirty"] == 1
        assert len(m["scenario_tree_id"]) in (40, 64)
    finally:
        con.close()


def test_tags_round_trip(tmp_path: Path) -> None:
    """Tags on accounts/scheduled CSVs land in the normalized join tables."""
    import pandas as pd

    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)

    acc = repo / "scenarios" / "retire_alice" / "accounts.csv"
    df = pd.read_csv(acc, dtype=str, keep_default_na=False)
    df.loc[df["name"] == "Asset_Checking", "tags"] = "expense:liquid notebook:net_worth"
    df.to_csv(acc, index=False)

    sched = repo / "scenarios" / "retire_alice" / "scheduled.csv"
    df = pd.read_csv(sched, dtype=str, keep_default_na=False)
    df.loc[df.index[0], "tags"] = "demo:first"
    df.to_csv(sched, index=False)

    result = load_scenario(
        repo, "retire_alice", intent_dir=tmp_path / "out",
        allow_dirty_data=True,
    )
    con = sqlite3.connect(result.intent_path)
    con.row_factory = sqlite3.Row
    try:
        tags = {(r["role"], r["name"]) for r in con.execute("SELECT role, name FROM tag")}
        assert ("expense", "liquid") in tags
        assert ("notebook", "net_worth") in tags
        assert ("demo", "first") in tags

        n_acc_tags = con.execute(
            "SELECT COUNT(*) FROM account_tag t "
            "JOIN account a ON a.id = t.account_id "
            "WHERE a.name = 'Asset_Checking'"
        ).fetchone()[0]
        assert n_acc_tags == 2

        n_sched_tags = con.execute(
            "SELECT COUNT(*) FROM scheduled_transaction_tag"
        ).fetchone()[0]
        assert n_sched_tags == 1
    finally:
        con.close()


def test_invalid_tag_rejected(tmp_path: Path) -> None:
    import pandas as pd

    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)

    acc = repo / "scenarios" / "retire_alice" / "accounts.csv"
    df = pd.read_csv(acc, dtype=str, keep_default_na=False)
    df.loc[df["name"] == "Asset_Checking", "tags"] = "no_colon_here"
    df.to_csv(acc, index=False)

    with pytest.raises(LoadError) as ei:
        load_scenario(
            repo, "retire_alice", intent_dir=tmp_path / "out",
            allow_dirty_data=True,
        )
    assert any("no_colon_here" in issue for issue in ei.value.issues)
