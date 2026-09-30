"""Smoke + negative tests for the intent loader."""

from __future__ import annotations

import csv
import shutil
import sqlite3
from pathlib import Path

import git
import pytest

from analysis.sim.loader import (
    DEFAULT_SCENARIO_NAME,
    DataDirtyRefused,
    LoadError,
    growth_rule_name,
    load_scenario,
    read_current_scenario,
    resolve_scenario_name,
    write_current_scenario,
)
from analysis.sim.provenance import data_stamps

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
            "ss_benefit", "rmd",
        }

        cal_kinds = {r["kind"] for r in con.execute("SELECT kind FROM calendar_event")}
        assert {"set_rate", "retire_scheduled", "set_amount", "one_off_transfer"} <= cal_kinds

        chain_row = con.execute("""
            SELECT c.id, c.name, cs.draw_from_chain_id
            FROM chain c
            JOIN chain_steps cs ON cs.chain_id = c.id
            WHERE c.name = 'asset checking liquidity'
            ORDER BY cs.step_order
        """).fetchall()
        assert len(chain_row) == 2
        assert {r[1] for r in chain_row} == {"asset checking liquidity"}
        assert chain_row[0][2] == con.execute("SELECT id FROM chain WHERE name = 'Asset_Savings'").fetchone()[0]
        assert chain_row[1][2] == (
            con.execute("SELECT id FROM chain WHERE name = 'Asset_TraditionalIRA_Alice:default'").fetchone()[0]
        )

        rmd_rows = con.execute(
            "SELECT COUNT(*) FROM aux_table WHERE kind = 'rmd_factors'"
        ).fetchone()[0]
        assert rmd_rows >= 6

        tax_priors = con.execute(
            "SELECT name, amount, tax_character, year FROM year_to_date ORDER BY name"
        ).fetchall()
        assert len(tax_priors) == 3
        assert (
            "prior_ltcg_loss_carryforward", -150000, "ltcg", 2025
        ) in [tuple(r) for r in tax_priors]

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


def test_sparse_child_inherits_parent_rows(tmp_path: Path) -> None:
    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)
    parent = repo / "scenarios" / "retire_alice"
    child = repo / "scenarios" / "tight"
    child.mkdir()
    (child / "scenario.csv").write_text(
        "name,description,account_model,start,end,seed,parent_scenario,created_by,meta_json\n"
        "tight,,,,,,retire_alice,,\n",
        encoding="utf-8",
    )
    for filename in (
        "accounts.csv", "account_values.csv", "scheduled.csv",
        "calendar.csv", "year_to_date.csv",
    ):
        header = (parent / filename).read_text(encoding="utf-8").splitlines()[0]
        (child / filename).write_text(header + "\n", encoding="utf-8")

    current = load_scenario(
        repo, "retire_alice", intent_dir=tmp_path / "current", allow_dirty_data=True,
    )
    tight = load_scenario(
        repo, "tight", intent_dir=tmp_path / "tight", allow_dirty_data=True,
    )
    tables = (
        "account", "account_model_member", "scheduled_transaction",
        "calendar_event", "year_to_date",
    )
    with sqlite3.connect(current.intent_path) as current_con, \
            sqlite3.connect(tight.intent_path) as tight_con:
        for table in tables:
            current_count = current_con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            tight_count = tight_con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert tight_count == current_count


def test_current_scenario_file_drives_default_resolution(tmp_path: Path) -> None:
    repo = tmp_path / "data"
    (repo / "scenarios" / DEFAULT_SCENARIO_NAME).mkdir(parents=True)
    (repo / "scenarios" / "tight").mkdir()

    assert read_current_scenario(repo) is None
    assert resolve_scenario_name(repo, None).name == DEFAULT_SCENARIO_NAME
    assert resolve_scenario_name(repo, "tight").name == "tight"

    write_current_scenario(repo, "tight")
    assert read_current_scenario(repo) == "tight"
    assert resolve_scenario_name(repo, None).name == "tight"
    assert resolve_scenario_name(repo, DEFAULT_SCENARIO_NAME).name == DEFAULT_SCENARIO_NAME

    write_current_scenario(repo, "gone")
    with pytest.raises(LoadError) as ei:
        resolve_scenario_name(repo, None)
    assert any("missing scenario" in issue for issue in ei.value.issues)


def test_sole_scenario_wins_over_current_default(tmp_path: Path) -> None:
    repo = tmp_path / "data"
    (repo / "scenarios" / "only_one").mkdir(parents=True)
    assert resolve_scenario_name(repo, None).name == "only_one"


def test_scenario_ref_locates_paths_and_git_head(tmp_path: Path) -> None:
    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)
    _git_init_commit(repo)

    ref = resolve_scenario_name(repo, "retire_alice")
    assert ref.path == repo / "scenarios" / "retire_alice"
    assert ref.intent_path == tmp_path / "state" / "intent" / "retire_alice.sqlite3"
    assert ref.runs_dir == tmp_path / "state" / "runs"
    assert ref.run_path is None
    assert not ref.loaded
    assert ref.commit is not None and len(ref.commit) in (40, 64)
    assert ref.branch == "main"
    assert ref.dirty is False
    assert "retire_alice" in ref.describe()
    assert ref.describe() == ref._repr_markdown_()

    load_scenario(repo, "retire_alice", intent_dir=ref.intent_path.parent)
    ref.runs_dir.mkdir(parents=True)
    for name in ["2026-01-01T000000Z_retire_alice_abc1234.sqlite3",
                 "2026-06-01T000000Z_other_abc1234.sqlite3"]:
        (ref.runs_dir / name).touch()

    reloaded = resolve_scenario_name(repo, "retire_alice")
    assert reloaded.loaded
    assert reloaded.run_path is not None
    assert reloaded.run_path.name == "2026-01-01T000000Z_retire_alice_abc1234.sqlite3"


def test_scenario_ref_intent_dir_override(tmp_path: Path) -> None:
    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)
    ref = resolve_scenario_name(
        repo, "retire_alice",
        intent_dir=tmp_path / "elsewhere", runs_dir=tmp_path / "other_runs",
    )
    assert ref.intent_path == tmp_path / "elsewhere" / "retire_alice.sqlite3"
    assert ref.runs_dir == tmp_path / "other_runs"


def test_account_series_synthesizes_growth_rule_and_income_account(
    tmp_path: Path,
) -> None:
    """ADR-003 §2.2: an asset account's `series` becomes a percent_of_balance
    rule funded by the built-in income account for its tax_character."""
    import pandas as pd

    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)
    accounts_path = repo / "scenarios" / "retire_alice" / "accounts.csv"
    df = pd.read_csv(accounts_path, dtype=str, keep_default_na=False)
    ira = df["name"] == "Asset_TraditionalIRA_Alice"
    df.loc[ira, "series"] = "Brokerage Growth"
    df.loc[ira, "tax_character"] = "deferred"
    df.to_csv(accounts_path, index=False)

    result = load_scenario(
        repo, "retire_alice", intent_dir=tmp_path / "out", allow_dirty_data=True,
    )
    con = sqlite3.connect(result.intent_path)
    con.row_factory = sqlite3.Row
    try:
        rule = con.execute(
            "SELECT s.amount_rule_kind, s.series, s.tax_character, s.priority, "
            "       s.cadence, s.start, fa.name AS from_name, ta.name AS to_name "
            "FROM scheduled_transaction s "
            "JOIN account fa ON fa.id = s.from_account "
            "JOIN account ta ON ta.id = s.to_account "
            "WHERE s.name = ?",
            (growth_rule_name("Asset_TraditionalIRA_Alice", "default"),),
        ).fetchone()
        assert rule["amount_rule_kind"] == "percent_of_balance"
        assert rule["series"] == "Brokerage Growth"
        assert rule["tax_character"] == "deferred"
        assert rule["cadence"] == "monthly"
        assert rule["priority"] == 100
        assert rule["to_name"] == "Asset_TraditionalIRA_Alice"
        assert rule["from_name"] == "deferred_gains"

        income = con.execute(
            "SELECT kind, subkind, tax_treatment FROM account WHERE name = 'deferred_gains'"
        ).fetchone()
        assert (income["kind"], income["subkind"], income["tax_treatment"]) == (
            "income", "capital_gain", "tax_deferred",
        )
        opening = con.execute(
            "SELECT m.opening_balance FROM account_model_member m "
            "JOIN account a ON a.id = m.account_id WHERE a.name = 'deferred_gains'"
        ).fetchone()[0]
        assert opening == 0
    finally:
        con.close()


@pytest.mark.parametrize(
    ("column_values", "expected"),
    [
        ({"series": "cpi", "tax_character": "ltcg"}, "requires tax_character in"),
        ({"series": "", "tax_character": "gains"}, "without series"),
    ],
)
def test_account_growth_rejects_bad_columns(
    tmp_path: Path, column_values: dict[str, str], expected: str,
) -> None:
    import pandas as pd

    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)
    accounts_path = repo / "scenarios" / "retire_alice" / "accounts.csv"
    df = pd.read_csv(accounts_path, dtype=str, keep_default_na=False)
    for column, value in column_values.items():
        df.loc[df["name"] == "Asset_Savings", column] = value
    df.to_csv(accounts_path, index=False)

    with pytest.raises(LoadError) as ei:
        load_scenario(
            repo, "retire_alice", intent_dir=tmp_path / "out", allow_dirty_data=True,
        )
    assert any(expected in issue for issue in ei.value.issues)


def test_account_series_rejected_for_non_asset(tmp_path: Path) -> None:
    import pandas as pd

    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)
    accounts_path = repo / "scenarios" / "retire_alice" / "accounts.csv"
    df = pd.read_csv(accounts_path, dtype=str, keep_default_na=False)
    expense = df.index[df["kind"] == "expense"][0]
    df.at[expense, "series"] = "cpi"
    df.at[expense, "tax_character"] = "ordinary"
    df.to_csv(accounts_path, index=False)

    with pytest.raises(LoadError) as ei:
        load_scenario(
            repo, "retire_alice", intent_dir=tmp_path / "out", allow_dirty_data=True,
        )
    assert any("only supported for asset accounts" in issue for issue in ei.value.issues)


def test_invalid_subkind_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "repo"
    shutil.copytree(FIXTURE_DATA / "bad_subkind", bad)
    with pytest.raises(LoadError) as ei:
        load_scenario(bad, "x", intent_dir=tmp_path / "out")
    assert any("bogus_subkind" in issue for issue in ei.value.issues)


def test_unresolvable_refill_chain_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    scenario_dir = repo / "scenarios" / "x"
    scenario_dir.mkdir(parents=True)

    (repo / "persons.csv").write_text(
        "id,display,birthdate,role,meta_json\n"
        "alice,Alice,1980-01-01,,{}\n",
        encoding="utf-8",
    )
    (scenario_dir / "scenario.csv").write_text(
        "name,description,account_model,start,end,seed,parent_scenario,created_by,meta_json\n"
        "x,,m,2026-01-31,2026-12-31,,,test,{}\n",
        encoding="utf-8",
    )
    (scenario_dir / "accounts.csv").write_text(
        "name,kind,subkind,owner,tax_treatment,series,tax_character,opened,closed,"
        "meta_json,tags\n"
        "Asset_Checking,asset,cash,,taxable,,,,,{},\n",
        encoding="utf-8",
    )
    for filename, header in {
        "account_values.csv": (
            "account_name,opening_balance,opening_cost_basis,prior_year_end_balance,"
            "refill_chain\nAsset_Checking,100.00,,,missing"
        ),
        "scheduled.csv": (
            "name,from_account,to_account,cadence,month_of_year,amount_rule_kind,interest_account"
            ",person,series,rate_micros,claim_age,amount,base_date,inflation_cadence,tax_character"
            ",start,end,priority,ytd_amount,ytd_preroll,description,meta_json,params_json,tags"
        ),
        "calendar.csv": (
            "date,kind,scheduled_name,account_1,account_2,series,amount,param_date,tax_character"
            ",payload_json,description,tags"
        ),
        "year_to_date.csv": "name,amount,tax_character,year,from_account,to_account",
    }.items():
        (scenario_dir / filename).write_text(header + "\n", encoding="utf-8")

    with pytest.raises(LoadError) as ei:
        load_scenario(repo, "x", intent_dir=tmp_path / "out")
    assert any("refill_chain 'missing'" in issue for issue in ei.value.issues)


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


def _derive_empty_child(repo: Path, parent_name: str, child_name: str) -> Path:
    """A scenario that inherits everything from ``parent_name`` and overrides nothing."""
    parent = repo / "scenarios" / parent_name
    child = repo / "scenarios" / child_name
    child.mkdir()
    (child / "scenario.csv").write_text(
        "name,description,account_model,start,end,seed,parent_scenario,created_by,meta_json\n"
        f"{child_name},,,,,,{parent_name},,\n",
        encoding="utf-8",
    )
    for filename in (
        "accounts.csv", "account_values.csv", "scheduled.csv",
        "calendar.csv", "year_to_date.csv",
    ):
        header = (parent / filename).read_text(encoding="utf-8").splitlines()[0]
        (child / filename).write_text(header + "\n", encoding="utf-8")
    return child


def test_child_provenance_covers_parent_scenario(tmp_path: Path) -> None:
    """A derived scenario's stamps must track its parent's files (ADR-004 §1).

    The child reads the parent's folder, so editing the parent changes what the
    child loaded. If the provenance scope stopped at ``scenarios/<child>/``,
    that edit would leave the child's tree id untouched and its ``data_dirty``
    false -- two different scenarios sharing one determinism key.
    """
    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)
    _derive_empty_child(repo, "retire_alice", "tight")
    _git_init_commit(repo)

    before = data_stamps(repo, "tight")
    assert not before.dirty

    parent_sched = repo / "scenarios" / "retire_alice" / "scheduled.csv"
    parent_sched.write_text(
        parent_sched.read_text(encoding="utf-8") + "# scratch\n", encoding="utf-8",
    )

    after = data_stamps(repo, "tight")
    assert after.dirty, "parent edit must mark the child's data dirty"
    assert any("retire_alice" in p for p in after.dirty_paths)
    assert after.tree_id != before.tree_id, "parent edit must move the child's tree id"


def test_provenance_scope_excludes_unrelated_scenarios(tmp_path: Path) -> None:
    """Scope covers the lineage, not the whole scenarios/ directory."""
    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)
    _derive_empty_child(repo, "retire_alice", "unrelated")
    _git_init_commit(repo)

    before = data_stamps(repo, "retire_alice")
    sibling = repo / "scenarios" / "unrelated" / "scheduled.csv"
    sibling.write_text(sibling.read_text(encoding="utf-8") + "# scratch\n", encoding="utf-8")

    after = data_stamps(repo, "retire_alice")
    assert not after.dirty
    assert after.tree_id == before.tree_id


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


def test_lifespan_csv_round_trip(tmp_path: Path) -> None:
    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)

    lifespan = repo / "scenarios" / "retire_alice" / "lifespan.csv"
    lifespan.write_text("person,final_month\nalice,2033-06-30\n")

    result = load_scenario(
        repo, "retire_alice", intent_dir=tmp_path / "out",
        allow_dirty_data=True,
    )
    con = sqlite3.connect(result.intent_path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT person, final_month FROM person_lifespan"
        ).fetchone()
        assert row["person"] == "alice"
        assert row["final_month"] == "2033-06-30"
    finally:
        con.close()


def test_lifespan_unknown_person_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)

    lifespan = repo / "scenarios" / "retire_alice" / "lifespan.csv"
    lifespan.write_text("person,final_month\ncarol,2033-06-30\n")

    with pytest.raises(LoadError, match="not in persons.csv"):
        load_scenario(
            repo, "retire_alice", intent_dir=tmp_path / "out",
            allow_dirty_data=True,
        )


def test_year_to_date_csv_round_trip(tmp_path: Path) -> None:
    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)

    ytd = repo / "scenarios" / "retire_alice" / "year_to_date.csv"
    ytd.write_text(
        "name,from_account,to_account,to_lot,amount,tax_character,description\n"
        "prior_brokerage_growth,Income_CapitalGain,Asset_Brokerage_Joint,default,"
        "1234.56,ltcg,pre-simulation brokerage appreciation\n"
    )

    result = load_scenario(
        repo, "retire_alice", intent_dir=tmp_path / "out",
        allow_dirty_data=True,
    )
    con = sqlite3.connect(result.intent_path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT y.name, y.amount, y.tax_character, fa.name AS from_name, "
            "ta.name AS to_name FROM year_to_date y "
            "JOIN account fa ON fa.id = y.from_account "
            "JOIN account ta ON ta.id = y.to_account"
        ).fetchone()
        assert row["name"] == "prior_brokerage_growth"
        assert row["amount"] == 123456
        assert row["tax_character"] == "ltcg"
        assert row["from_name"] == "Income_CapitalGain"
        assert row["to_name"] == "Asset_Brokerage_Joint"
    finally:
        con.close()


def test_year_to_date_unknown_account_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)

    ytd = repo / "scenarios" / "retire_alice" / "year_to_date.csv"
    ytd.write_text(
        "name,from_account,to_account,amount,tax_character,description\n"
        "bogus,Nonexistent_Account,Asset_Checking,100.00,none,\n"
    )

    with pytest.raises(LoadError, match="not a known account"):
        load_scenario(
            repo, "retire_alice", intent_dir=tmp_path / "out",
            allow_dirty_data=True,
        )


def test_scheduled_ytd_preroll_rejected_for_non_fixed_amount(tmp_path: Path) -> None:
    import pandas as pd

    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)

    sched = repo / "scenarios" / "retire_alice" / "scheduled.csv"
    df = pd.read_csv(sched, dtype=str, keep_default_na=False)
    # mortgage_pmt is amount_rule_kind='mortgage_payment', not fixed_amount+monthly.
    df.loc[df["name"] == "mortgage_pmt", "ytd_preroll"] = "1"
    df.to_csv(sched, index=False)

    with pytest.raises(LoadError, match="ytd_preroll only valid"):
        load_scenario(
            repo, "retire_alice", intent_dir=tmp_path / "out",
            allow_dirty_data=True,
        )


def test_scheduled_ytd_preroll_allowed_for_annual_fixed_amount(tmp_path: Path) -> None:
    import pandas as pd

    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)

    sched = repo / "scenarios" / "retire_alice" / "scheduled.csv"
    df = pd.read_csv(sched, dtype=str, keep_default_na=False)
    mask = df["name"] == "living"
    df.loc[mask, "cadence"] = "annual"
    df.loc[mask, "month_of_year"] = "4"
    df.loc[mask, "ytd_preroll"] = "1"
    df.to_csv(sched, index=False)

    result = load_scenario(
        repo, "retire_alice", intent_dir=tmp_path / "out",
        allow_dirty_data=True,
    )

    con = sqlite3.connect(result.intent_path)
    try:
        row = con.execute(
            "SELECT cadence, month_of_year, ytd_preroll "
            "FROM scheduled_transaction WHERE name='living'"
        ).fetchone()
        assert row == ("annual", 4, 1)
    finally:
        con.close()


def test_scheduled_ytd_amount_allowed_for_mortgage_payment(tmp_path: Path) -> None:
    import pandas as pd

    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)

    sched = repo / "scenarios" / "retire_alice" / "scheduled.csv"
    df = pd.read_csv(sched, dtype=str, keep_default_na=False)
    df.loc[df["name"] == "mortgage_pmt", "ytd_amount"] = "13956.64"
    df.to_csv(sched, index=False)

    result = load_scenario(
        repo, "retire_alice", intent_dir=tmp_path / "out",
        allow_dirty_data=True,
    )

    con = sqlite3.connect(result.intent_path)
    try:
        row = con.execute(
            "SELECT ytd_amount FROM scheduled_transaction WHERE name='mortgage_pmt'"
        ).fetchone()
        assert row == (1_395_664,)
    finally:
        con.close()


def test_scheduled_ytd_amount_and_preroll_mutually_exclusive(tmp_path: Path) -> None:
    import pandas as pd

    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)

    sched = repo / "scenarios" / "retire_alice" / "scheduled.csv"
    df = pd.read_csv(sched, dtype=str, keep_default_na=False)
    df.loc[df["name"] == "salary_alice", "ytd_amount"] = "3000.00"
    df.loc[df["name"] == "salary_alice", "ytd_preroll"] = "1"
    df.to_csv(sched, index=False)

    with pytest.raises(LoadError, match="mutually exclusive"):
        load_scenario(
            repo, "retire_alice", intent_dir=tmp_path / "out",
            allow_dirty_data=True,
        )


def test_on_death_requires_tax_free(tmp_path: Path) -> None:
    import pandas as pd

    from analysis.sim.registries import SCHEDULED_CSV_HEADER

    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)

    sched = repo / "scenarios" / "retire_alice" / "scheduled.csv"
    df = pd.read_csv(sched, dtype=str, keep_default_na=False)
    df = df.reindex(columns=SCHEDULED_CSV_HEADER, fill_value="")
    row = {c: "" for c in SCHEDULED_CSV_HEADER}
    row.update(
        name="sell_car", from_account="Asset_Savings", to_account="Asset_Checking",
        cadence="monthly", amount_rule_kind="on_death", person="alice",
        tax_character="none", start="2026-01-31",
    )
    df.loc[len(df)] = [row[c] for c in SCHEDULED_CSV_HEADER]
    df.to_csv(sched, index=False)

    with pytest.raises(LoadError, match="on_death requires tax_character='tax_free'"):
        load_scenario(
            repo, "retire_alice", intent_dir=tmp_path / "out",
            allow_dirty_data=True,
        )


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


def test_account_values_unknown_account_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)

    av = repo / "scenarios" / "retire_alice" / "account_values.csv"
    with av.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
        fields = list(rows[0].keys())
    rows.append(
        {
            "account_name": "NotAnAccount",
            "opening_balance": "100.00",
            "opening_cost_basis": "",
            "prior_year_end_balance": "",
        }
    )
    with av.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    with pytest.raises(LoadError) as ei:
        load_scenario(
            repo, "retire_alice", intent_dir=tmp_path / "out",
            allow_dirty_data=True,
        )
    assert any("account_name not in accounts.csv" in issue for issue in ei.value.issues)


def test_money_reference_in_account_values_resolves_itemization_sum(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)

    itemizations = repo / "itemizations"
    itemizations.mkdir(parents=True, exist_ok=True)
    with (itemizations / "home_basis.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["Item", "Value", "Note"])
        w.writeheader()
        w.writerow({"Item": "purchase", "Value": "300000.00", "Note": ""})
        w.writerow({"Item": "improvements", "Value": "100000.00", "Note": ""})

    av = repo / "scenarios" / "retire_alice" / "account_values.csv"
    with av.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
        fields = list(rows[0].keys())
    for row in rows:
        if row.get("account_name") == "Asset_Home":
            row["opening_cost_basis"] = "@home_basis"
    with av.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    result = load_scenario(
        repo, "retire_alice", intent_dir=tmp_path / "out",
        allow_dirty_data=True,
    )
    con = sqlite3.connect(result.intent_path)
    try:
        got = con.execute(
            "SELECT m.opening_cost_basis "
            "FROM account_model_member m "
            "JOIN account a ON a.id = m.account_id "
            "WHERE a.name = 'Asset_Home'"
        ).fetchone()[0]
        assert got == 40_000_000
    finally:
        con.close()


def test_money_reference_rejects_itemization_missing_value_column(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)

    itemizations = repo / "itemizations"
    itemizations.mkdir(parents=True, exist_ok=True)
    with (itemizations / "home_basis.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["Item", "Amount"])
        w.writeheader()
        w.writerow({"Item": "purchase", "Amount": "300000.00"})

    av = repo / "scenarios" / "retire_alice" / "account_values.csv"
    with av.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
        fields = list(rows[0].keys())
    for row in rows:
        if row.get("account_name") == "Asset_Home":
            row["opening_cost_basis"] = "@home_basis"
    with av.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    with pytest.raises(LoadError) as ei:
        load_scenario(
            repo, "retire_alice", intent_dir=tmp_path / "out",
            allow_dirty_data=True,
        )
    assert any("expected a 'Value' column" in issue for issue in ei.value.issues)


def _append_calendar_vary_row(repo: Path, **fields: str) -> None:
    """ADR-006: append one calendar.csv row, addressing columns by name."""
    import pandas as pd

    from analysis.sim.registries import CALENDAR_HEADER

    cal = repo / "scenarios" / "retire_alice" / "calendar.csv"
    df = pd.read_csv(cal, dtype=str, keep_default_na=False)
    df = df.reindex(columns=CALENDAR_HEADER, fill_value="")
    unknown = set(fields) - set(CALENDAR_HEADER)
    assert not unknown, f"unknown calendar.csv columns: {sorted(unknown)}"
    df.loc[len(df)] = [fields.get(column, "") for column in CALENDAR_HEADER]
    df.to_csv(cal, index=False)


def test_vary_target_required_with_distribution(tmp_path: Path) -> None:
    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)
    _append_calendar_vary_row(
        repo, date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset Checking", amount="45000",
        vary_distribution="uniform", vary_low="40000", vary_high="50000",
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(
            repo, "retire_alice", intent_dir=tmp_path / "out",
            allow_dirty_data=True,
        )
    assert any("vary_target" in issue for issue in ei.value.issues)


def test_vary_distribution_unknown_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)
    _append_calendar_vary_row(
        repo, date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset Checking", amount="45000",
        vary_target="amount", vary_distribution="lognormal",
        vary_low="40000", vary_high="50000",
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(
            repo, "retire_alice", intent_dir=tmp_path / "out",
            allow_dirty_data=True,
        )
    assert any("unknown vary_distribution" in issue for issue in ei.value.issues)


def test_vary_uniform_requires_bounds(tmp_path: Path) -> None:
    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)
    _append_calendar_vary_row(
        repo, date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset Checking", amount="45000",
        vary_target="amount", vary_distribution="uniform", vary_low="40000",
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(
            repo, "retire_alice", intent_dir=tmp_path / "out",
            allow_dirty_data=True,
        )
    assert any("uniform requires vary_high" in issue for issue in ei.value.issues)


def test_vary_name_required_with_distribution(tmp_path: Path) -> None:
    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)
    _append_calendar_vary_row(
        repo, date="2027-02-28", kind="set_rate",
        series="Asset Checking", amount="45000",
        vary_target="amount", vary_distribution="uniform",
        vary_low="40000", vary_high="50000",
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(
            repo, "retire_alice", intent_dir=tmp_path / "out",
            allow_dirty_data=True,
        )
    assert any("name is required" in issue for issue in ei.value.issues)


def test_vary_name_duplicate_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)
    for month_end in ("2027-02-28", "2027-03-31"):
        _append_calendar_vary_row(
            repo, date=month_end, kind="set_rate", name="checking_rate_vary",
            series="Asset Checking", amount="45000",
            vary_target="amount", vary_distribution="uniform",
            vary_low="40000", vary_high="50000",
        )
    with pytest.raises(LoadError) as ei:
        load_scenario(
            repo, "retire_alice", intent_dir=tmp_path / "out",
            allow_dirty_data=True,
        )
    assert any("duplicate name" in issue for issue in ei.value.issues)


def test_vary_ramp_months_requires_amount_target_on_set_rate(tmp_path: Path) -> None:
    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)
    _append_calendar_vary_row(
        repo, date="2027-02-28", kind="set_amount", name="healthcare_vary",
        scheduled_name="healthcare", amount="120000",
        vary_target="amount", vary_distribution="uniform",
        vary_low="100000", vary_high="140000", vary_ramp_months="12",
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(
            repo, "retire_alice", intent_dir=tmp_path / "out",
            allow_dirty_data=True,
        )
    assert any("vary_ramp_months is only valid" in issue for issue in ei.value.issues)


def test_vary_date_target_bounds_must_be_iso_dates(tmp_path: Path) -> None:
    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)
    _append_calendar_vary_row(
        repo, date="2027-02-28", kind="retire_scheduled", name="retire_bob_vary",
        scheduled_name="salary_bob",
        vary_target="date", vary_distribution="uniform",
        vary_low="2029-01-01", vary_high="not-a-date",
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(
            repo, "retire_alice", intent_dir=tmp_path / "out",
            allow_dirty_data=True,
        )
    assert any("not an ISO date" in issue for issue in ei.value.issues)


def test_vary_columns_present_without_distribution_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)
    _append_calendar_vary_row(
        repo, date="2027-02-28", kind="set_rate",
        series="Asset Checking", amount="45000",
        vary_target="amount",
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(
            repo, "retire_alice", intent_dir=tmp_path / "out",
            allow_dirty_data=True,
        )
    assert any(
        "vary_* columns set without vary_distribution" in issue for issue in ei.value.issues
    )


def test_ordinary_calendar_unaffected_by_vary_columns(tmp_path: Path) -> None:
    """ADR-006 Phase 0 exit criterion: no behavior change for a scenario
    without vary_* columns filled in."""
    result = load_scenario(
        PROTOTYPE, "retire_alice", intent_dir=tmp_path, loaded_by="test",
        allow_dirty_data=True,
    )
    con = sqlite3.connect(result.intent_path)
    try:
        rows = con.execute(
            "SELECT vary_target, vary_distribution FROM calendar_event"
        ).fetchall()
        assert rows
        assert all(r == (None, None) for r in rows)
    finally:
        con.close()


def test_valid_vary_row_loads_cleanly(tmp_path: Path) -> None:
    """A well-formed vary_distribution row is accepted -- resolving it into a
    concrete value is Phase 2's job, not the loader's."""
    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)
    _append_calendar_vary_row(
        repo, date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset Checking", amount="45000",
        vary_target="amount", vary_distribution="uniform",
        vary_low="40000", vary_high="50000",
    )
    result = load_scenario(
        repo, "retire_alice", intent_dir=tmp_path / "out", loaded_by="test",
        allow_dirty_data=True,
    )
    con = sqlite3.connect(result.intent_path)
    try:
        row = con.execute(
            "SELECT vary_target, vary_distribution, vary_low, vary_high "
            "FROM calendar_event WHERE name = 'checking_rate_vary'"
        ).fetchone()
        assert row == ("amount", "uniform", "40000", "50000")
    finally:
        con.close()
