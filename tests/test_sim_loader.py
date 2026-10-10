"""Smoke + negative tests for the intent loader."""

from __future__ import annotations

import csv
import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path

import git
import pandas as pd
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
        assert {"alice", "bill"} <= person_ids

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
            WHERE c.name = 'asset_checking_liquidity'
            ORDER BY cs.step_order
        """).fetchall()
        assert len(chain_row) == 2
        assert {r[1] for r in chain_row} == {"asset_checking_liquidity"}
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


def test_readme_overrides_scenario_csv_description(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path], intent_dir: Path,
) -> None:
    (retire_alice_csv("README.md")).write_text(
        "# Retirement baseline\n\nAlice and Bill's main plan.\n"
    )
    result = load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    con = sqlite3.connect(result.intent_path)
    try:
        description = con.execute("SELECT description FROM scenario").fetchone()[0]
        assert description == "# Retirement baseline\n\nAlice and Bill's main plan."
    finally:
        con.close()


def test_no_readme_keeps_scenario_csv_description(
    test_data_dir: Path, intent_dir: Path,
) -> None:
    result = load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    con = sqlite3.connect(result.intent_path)
    try:
        description = con.execute("SELECT description FROM scenario").fetchone()[0]
    finally:
        con.close()
    df = pd.read_csv(test_data_dir / "scenarios" / "retire_alice" / "scenario.csv",
                     dtype=str, keep_default_na=False)
    assert description == (df.loc[0, "description"] or None)


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


def test_scenario_ref_falls_back_to_ensemble_member_zero(tmp_path: Path) -> None:
    """ADR-006 §8: a caller that only knows how to open one run file per
    scenario (everything resolve_scenario_name serves) still gets a run for
    an ensemble recipe -- its deterministic member 0 -- when there's no flat
    run file to find."""
    repo = tmp_path / "data"
    shutil.copytree(PROTOTYPE, repo)
    ref = resolve_scenario_name(repo, "retire_alice")
    batch_dir = ref.runs_dir / "retire_alice" / "2026-01-01T000000Z"
    batch_dir.mkdir(parents=True)
    (batch_dir / "member_0000.sqlite3").touch()
    (batch_dir / "member_0001.sqlite3").touch()

    reloaded = resolve_scenario_name(repo, "retire_alice")
    assert reloaded.run_path == batch_dir / "member_0000.sqlite3"


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
    df.loc[ira, "series"] = "IRA_Growth"
    df.loc[ira, "tax_character"] = "deferred"
    df.to_csv(accounts_path, index=False)
    # Distinct from the existing brokerage_growth file -- macOS's default
    # case-insensitive filesystem would otherwise collide with it on a name
    # differing only in case.
    (repo / "scenarios" / "retire_alice" / "series" / "rate__IRA_Growth.csv").write_text(
        "effective_date,annual_rate\n2026-01-31,6%\n"
    )

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
        assert rule["series"] == "IRA_Growth"
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


def test_account_series_with_a_space_rejected(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path], intent_dir: Path,
) -> None:
    """A series name becomes a `rate__<name>.csv`-style filename segment
    (ADR-003 §4.1) -- a space there needs shell quoting and reads awkwardly,
    so it's forbidden outright rather than encoded/decoded around."""
    accounts_path = retire_alice_csv("accounts.csv")
    df = pd.read_csv(accounts_path, dtype=str, keep_default_na=False)
    row = df["name"] == "Asset_TraditionalIRA_Alice"
    df.loc[row, "series"] = "no such series"
    df.loc[row, "tax_character"] = "deferred"
    df.to_csv(accounts_path, index=False)
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any(
        "series 'no such series' may not contain a space" in issue
        for issue in ei.value.issues
    )


def test_calendar_series_with_a_space_rejected(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    append_calendar_row(
        date="2027-01-31", kind="set_rate", name="typo_series",
        series="some new series", amount="4.5%",
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any(
        "series 'some new series' may not contain a space" in issue
        for issue in ei.value.issues
    )


def test_chain_name_with_a_space_rejected(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path], intent_dir: Path,
) -> None:
    (retire_alice_csv("chains") / "asset__roth first.csv").write_text(
        "account_name,lot,maximum,percent,series,cadence\nAsset_Roth_Alice,,,,,\n"
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any(
        "chain name 'roth first' may not contain a space" in issue
        for issue in ei.value.issues
    )


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
            ",person,series,rate,claim_age,amount,base_date,inflation_cadence,tax_character"
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


@pytest.mark.parametrize(
    ("chain_files", "column", "chain", "path"),
    [
        ({}, "refill_chain", "Asset_Checking", "Asset_Checking"),
        (
            {"asset__asset_checking_liquidity.csv": "Asset_Savings,,,,,\nAsset_Checking,,,,,\n"},
            "refill_chain", "asset_checking_liquidity",
            "asset_checking_liquidity -> Asset_Checking",
        ),
        (
            {
                "asset__asset_checking_liquidity.csv": "Asset_Savings,,,,,\ninner,,,,,\n",
                "asset__inner.csv": "Asset_Checking,,,,,\n",
            },
            "refill_chain", "asset_checking_liquidity",
            "asset_checking_liquidity -> inner -> Asset_Checking",
        ),
        (
            {"asset__spill.csv": "Asset_Savings,,,,,\nAsset_Checking,,,,,\n"},
            "overflow_chain", "spill", "spill -> Asset_Checking",
        ),
    ],
    ids=["names-itself", "step", "nested-step", "overflow"],
)
def test_policy_chain_leading_back_to_its_own_lot_rejected(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path], intent_dir: Path,
    chain_files: dict[str, str], column: str, chain: str, path: str,
) -> None:
    """Issue #101: the engine skips a chain step naming the lot being refilled
    (or swept), so a refill chain reaching its own lot silently supplied
    nothing. The leaf step adds no edge for the cycle check, so it needs
    its own check, at any depth."""
    import pandas as pd

    for filename, rows in chain_files.items():
        (retire_alice_csv("chains") / filename).write_text(
            "account_name,lot,maximum,percent,series,cadence\n" + rows
        )
    values_path = retire_alice_csv("account_values.csv")
    df = pd.read_csv(values_path, dtype=str, keep_default_na=False)
    checking = df["account_name"] == "Asset_Checking"
    if column == "overflow_chain":
        df.loc[checking, "maximum_balance"] = "10000.00"
    df.loc[checking, column] = chain
    df.to_csv(values_path, index=False)

    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any(
        f"(Asset_Checking): {column} leads back to the lot itself: {path}" in issue
        for issue in ei.value.issues
    ), ei.value.issues


def test_policy_loop_through_another_lot_loads(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path], intent_dir: Path,
) -> None:
    """Only a chain leading back to its own lot is rejected: checking that
    refills from (and overflows into) savings, while savings refills from
    checking, is a legitimate loop through another lot's policy."""
    import pandas as pd

    (retire_alice_csv("chains") / "asset__spill.csv").write_text(
        "account_name,lot,maximum,percent,series,cadence\nAsset_Savings,,,,,\n"
    )
    values_path = retire_alice_csv("account_values.csv")
    df = pd.read_csv(values_path, dtype=str, keep_default_na=False)
    checking = df["account_name"] == "Asset_Checking"
    df.loc[checking, "maximum_balance"] = "10000.00"
    df.loc[checking, "overflow_chain"] = "spill"
    df.loc[df["account_name"] == "Asset_Savings", "refill_chain"] = "Asset_Checking"
    df.to_csv(values_path, index=False)

    load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)


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


def test_itemization_error_names_its_file_and_line(
    test_data_dir: Path, update_account_value: Callable[..., None], intent_dir: Path,
) -> None:
    """#136: an itemization's missing Value is reported at its physical
    line (a blank line before it counted), by its path in the data repo."""
    itemizations = test_data_dir / "itemizations"
    itemizations.mkdir(exist_ok=True)
    (itemizations / "home_basis.csv").write_text(
        "Item,Value\npurchase,300000.00\n\nimprovements,\n", encoding="utf-8",
    )
    update_account_value("Asset_Home", "", opening_cost_basis="@home_basis")
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any(
        "itemizations/home_basis.csv line 4: missing Value for '@home_basis'" in issue
        for issue in ei.value.issues
    )


def test_vary_target_required_with_distribution(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_distribution="uniform", vary_low="4%", vary_high="5%",
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any("vary_target" in issue for issue in ei.value.issues)


def test_vary_distribution_unknown_rejected(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="lognormal",
        vary_low="4%", vary_high="5%",
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any("unknown vary_distribution" in issue for issue in ei.value.issues)


def test_vary_uniform_requires_bounds(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="uniform", vary_low="4%",
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any("uniform requires vary_high" in issue for issue in ei.value.issues)


def test_vary_name_required_with_distribution(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="set_rate",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="uniform",
        vary_low="4%", vary_high="5%",
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any("name is required" in issue for issue in ei.value.issues)


def test_vary_name_duplicate_rejected(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    for month_end in ("2027-02-28", "2027-03-31"):
        append_calendar_row(
            date=month_end, kind="set_rate", name="checking_rate_vary",
            series="Asset_Checking", amount="4.5%",
            vary_target="amount", vary_distribution="uniform",
            vary_low="4%", vary_high="5%",
        )
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any("duplicate name" in issue for issue in ei.value.issues)


def test_vary_rows_sharing_every_merge_key_field_but_name_is_ambiguous(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    """Two named rows identical in every scenario-inheritance merge-key
    column (date/kind/series/amount/...) look like the same physical event
    authored twice under different names -- not two distinct events -- so
    this is reported as a conflict rather than silently collapsed one way or
    the other (name is deliberately not part of the merge key; see
    ``_merge_calendar_rows``, which exists specifically so a child can still
    override an inherited *unnamed* row by repeating these same fields)."""
    for name in ("rate_vary_a", "rate_vary_b"):
        append_calendar_row(
            date="2027-02-28", kind="set_rate", name=name,
            series="Asset_Checking", amount="4.5%",
            vary_target="amount", vary_distribution="uniform",
            vary_low="4%", vary_high="5%",
        )
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any(
        "rate_vary_a" in issue and "rate_vary_b" in issue for issue in ei.value.issues
    )


def test_vary_row_overrides_inherited_unnamed_calendar_row(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path], intent_dir: Path,
) -> None:
    """A child re-declaring an inherited *unnamed* row's same key fields plus
    a new name and vary_* columns must replace that one row, not add a
    second, doubled event -- the regression a too-broad merge-key fix would
    reintroduce for e.g. varying an inherited one_off_transfer."""
    calendar_path = retire_alice_csv("calendar.csv")
    df = pd.read_csv(calendar_path, dtype=str, keep_default_na=False)
    inherited = df[df["kind"] == "one_off_transfer"].iloc[0]

    child = pd.DataFrame([{
        "date": inherited["date"], "kind": "one_off_transfer",
        "account_1": inherited["account_1"], "lot_1": inherited["lot_1"],
        "account_2": inherited["account_2"], "lot_2": inherited["lot_2"],
        "series": inherited["series"], "amount": inherited["amount"],
        "tax_character": inherited["tax_character"], "name": "inheritance_vary",
        "vary_target": "amount", "vary_distribution": "uniform",
        "vary_low": inherited["amount"], "vary_high": f"{float(inherited['amount']) * 2:.2f}",
    }])
    pd.concat([df, child], ignore_index=True).fillna("").to_csv(calendar_path, index=False)

    result = load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    con = sqlite3.connect(result.intent_path)
    try:
        rows = con.execute(
            "SELECT name, vary_distribution FROM calendar_event WHERE kind = 'one_off_transfer' "
            "AND date = ?",
            (inherited["date"],),
        ).fetchall()
        # replaced, not doubled: exactly one row for this event, now named/varying.
        assert rows == [("inheritance_vary", "uniform")]
    finally:
        con.close()


_CHILD_CALENDAR_HEADER = (
    "date,kind,name,series,amount,vary_target,vary_distribution,vary_low,vary_high\n"
)


def test_child_overrides_named_calendar_row_with_a_new_amount(
    append_calendar_row: Callable[..., None], derive_scenario: Callable[..., Path],
    load_test_scenario: Callable[[str], Path],
) -> None:
    """#136: a child row naming an inherited named row overrides it even when
    its amount (a merge-key field) differs -- not a second row with the same
    name beside it."""
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="uniform",
        vary_low="4%", vary_high="5%",
    )
    derive_scenario("child", "retire_alice", files={"calendar.csv": (
        _CHILD_CALENDAR_HEADER
        + "2027-02-28,set_rate,checking_rate_vary,Asset_Checking,-5%,amount,uniform,-20%,5%\n"
    )})
    con = sqlite3.connect(load_test_scenario("child"))
    try:
        rows = con.execute(
            "SELECT amount, vary_low, vary_high FROM calendar_event WHERE name = ?",
            ("checking_rate_vary",),
        ).fetchall()
    finally:
        con.close()
    assert rows == [(-50_000, "-200000", "50000")]


def test_calendar_errors_name_the_scenario_file_and_line(
    test_data_dir: Path, append_calendar_row: Callable[..., None],
    derive_scenario: Callable[..., Path], intent_dir: Path,
) -> None:
    """#136: a message about a merged calendar row names the file it was
    authored in and its physical line -- blank lines counted -- not its
    position in the parent-plus-child calendar."""
    append_calendar_row(date="2027-03-31", kind="no_such_kind")
    derive_scenario("child", "retire_alice", files={"calendar.csv": (
        _CHILD_CALENDAR_HEADER
        + "2027-02-28,set_rate,twice,Asset_Checking,4%,,,,\n"
        + "\n"
        + "2027-04-30,set_rate,twice,Asset_Checking,5%,,,,\n"
    )})
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "child", intent_dir=intent_dir, allow_dirty_data=True)
    issues = ei.value.issues
    assert (
        "scenarios/child/calendar.csv line 4: duplicate name 'twice' "
        "(first seen on scenarios/child/calendar.csv line 2)"
    ) in issues
    assert "scenarios/retire_alice/calendar.csv line 7: unknown event kind 'no_such_kind'" in issues


def test_vary_rows_identical_including_name_rejected_as_duplicate(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    """Two fully-identical rows (same name and every other field) would
    otherwise merge into one before the merged-view duplicate-name check
    ever runs; a dedicated per-file check must catch this case too."""
    for _ in range(2):
        append_calendar_row(
            date="2027-02-28", kind="set_rate", name="checking_rate_vary",
            series="Asset_Checking", amount="4.5%",
            vary_target="amount", vary_distribution="uniform",
            vary_low="4%", vary_high="5%",
        )
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any("duplicate name" in issue for issue in ei.value.issues)


def test_vary_ramp_months_requires_amount_target_on_set_rate(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="set_amount", name="healthcare_vary",
        scheduled_name="healthcare", amount="1200.00",
        vary_target="amount", vary_distribution="uniform",
        vary_low="1000.00", vary_high="1400.00", vary_ramp_months="12",
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any("vary_ramp_months is only valid" in issue for issue in ei.value.issues)


def test_vary_amount_target_rejected_on_kind_without_amount_column(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="retire_scheduled", name="retire_bill_vary",
        scheduled_name="salary_bill",
        vary_target="amount", vary_distribution="uniform",
        vary_low="1", vary_high="2",
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any(
        "vary_target='amount' is only valid on kind in" in issue for issue in ei.value.issues
    )


def test_vary_truncated_normal_rejected_with_date_target(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="retire_scheduled", name="retire_bill_vary",
        scheduled_name="salary_bill",
        vary_target="date", vary_distribution="truncated_normal",
        vary_median="2029-06-30", vary_sd="30",
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any(
        "truncated_normal is not valid with vary_target='date'" in issue
        for issue in ei.value.issues
    )


def test_vary_uniform_rejects_columns_it_does_not_use(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="uniform",
        vary_low="4%", vary_high="5%", vary_median="4.5%",
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any(
        "vary_median is not used by uniform" in issue for issue in ei.value.issues
    )


def test_vary_uniform_rejects_high_below_low(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="uniform",
        vary_low="5%", vary_high="4%",
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any("vary_low must be <= vary_high" in issue for issue in ei.value.issues)


def test_vary_uniform_rejects_reversed_date_bounds(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="retire_scheduled", name="retire_bill_vary",
        scheduled_name="salary_bill",
        vary_target="date", vary_distribution="uniform",
        vary_low="2029-12-31", vary_high="2029-01-01",
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any("vary_low must be <= vary_high" in issue for issue in ei.value.issues)


def test_vary_truncated_normal_rejects_nonpositive_sd(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="truncated_normal",
        vary_median="4.5%", vary_sd="0%",
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any("vary_sd must be positive" in issue for issue in ei.value.issues)


def test_vary_truncated_normal_low_equal_to_median_without_sd_is_degenerate(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    """With vary_sd blank, vary_low = vary_median leaves nothing to infer a
    nonzero spread from (median - low = 0), so it's rejected rather than
    silently drawing a single point (and a median on a bound is invalid
    anyway, #31)."""
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="truncated_normal",
        vary_median="4.5%", vary_low="4.5%",
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any("vary_sd must be positive" in issue for issue in ei.value.issues)


def test_vary_truncated_normal_median_on_a_bound_is_rejected(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    """#31: "vary_low = the center" used to give a one-sided draw. With the
    center now the truncated median, a median can't sit on a bound (half
    the mass would have to be on it) -- the error points at vary_skew."""
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="truncated_normal",
        vary_median="4.5%", vary_sd="0.5%", vary_low="4.5%",
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any("strictly between" in issue and "vary_skew" in issue for issue in ei.value.issues)


def test_vary_truncated_normal_skewed_one_sided_spread_loads(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    """The replacement for the one-sided trick: a right skew, with the low
    bound just under the median."""
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="truncated_normal",
        vary_median="4.5%", vary_sd="0.5%", vary_low="4.4%", vary_skew="1.0",
    )
    load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)


def test_vary_truncated_normal_rejects_reversed_low_high(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="truncated_normal",
        vary_median="4.5%", vary_sd="0.5%",
        vary_low="5%", vary_high="4%",
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any("vary_low must be below vary_high" in issue for issue in ei.value.issues)


def test_vary_one_off_transfer_uniform_requires_positive_low(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    append_calendar_row(
        date="2030-06-30", kind="one_off_transfer", name="transfer_vary",
        account_1="Asset_Checking", account_2="Asset_Brokerage_Joint", lot_2="default",
        amount="1000.00", tax_character="none",
        vary_target="amount", vary_distribution="uniform",
        vary_low="-500.00", vary_high="500.00",
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any(
        "one_off_transfer requires vary_low > 0" in issue for issue in ei.value.issues
    )


def test_vary_one_off_transfer_uniform_rejects_zero_low(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    append_calendar_row(
        date="2030-06-30", kind="one_off_transfer", name="transfer_vary",
        account_1="Asset_Checking", account_2="Asset_Brokerage_Joint", lot_2="default",
        amount="1000.00", tax_character="none",
        vary_target="amount", vary_distribution="uniform",
        vary_low="0.00", vary_high="500.00",
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any(
        "one_off_transfer requires vary_low > 0" in issue for issue in ei.value.issues
    )


def test_vary_one_off_transfer_truncated_normal_requires_positive_effective_low(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    """mean=50000, sd=20000, no explicit low: the inferred low
    (mean - 3*sd = -10000) is negative, so this must still be rejected even
    though neither vary_low nor vary_clip_low was written directly."""
    append_calendar_row(
        date="2030-06-30", kind="one_off_transfer", name="transfer_vary",
        account_1="Asset_Checking", account_2="Asset_Brokerage_Joint", lot_2="default",
        amount="1000.00", tax_character="none",
        vary_target="amount", vary_distribution="truncated_normal",
        vary_median="500.00", vary_sd="200.00",
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any(
        "one_off_transfer requires vary_low > 0" in issue for issue in ei.value.issues
    )


def test_vary_one_off_transfer_truncated_normal_with_positive_low_loads(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    append_calendar_row(
        date="2030-06-30", kind="one_off_transfer", name="transfer_vary",
        account_1="Asset_Checking", account_2="Asset_Brokerage_Joint", lot_2="default",
        amount="1000.00", tax_character="none",
        vary_target="amount", vary_distribution="truncated_normal",
        vary_median="1000.00", vary_sd="200.00", vary_low="0.01",
    )
    load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)


def test_vary_date_target_bounds_must_be_iso_dates(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="retire_scheduled", name="retire_bill_vary",
        scheduled_name="salary_bill",
        vary_target="date", vary_distribution="uniform",
        vary_low="2029-01-01", vary_high="not-a-date",
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any("not a valid YYYY-MM or YYYY-MM-DD" in issue for issue in ei.value.issues)


def test_vary_columns_present_without_distribution_rejected(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="set_rate",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount",
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any(
        "vary_* columns set without vary_distribution" in issue for issue in ei.value.issues
    )


def test_ordinary_calendar_unaffected_by_vary_columns(
    test_data_dir: Path, intent_dir: Path,
) -> None:
    """ADR-006 Phase 0 exit criterion: no behavior change for a scenario
    without vary_* columns filled in."""
    result = load_scenario(
        test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True,
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


def test_valid_vary_row_loads_cleanly(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    """A well-formed vary_distribution row is accepted -- resolving it into a
    concrete value is Phase 2's job, not the loader's."""
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="uniform",
        vary_low="4%", vary_high="5%",
    )
    result = load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    con = sqlite3.connect(result.intent_path)
    try:
        row = con.execute(
            "SELECT vary_target, vary_distribution, vary_low, vary_high "
            "FROM calendar_event WHERE name = 'checking_rate_vary'"
        ).fetchone()
        assert row == ("amount", "uniform", "40000", "50000")
    finally:
        con.close()


def test_calendar_set_rate_series_nothing_reads_warns(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    """A set_rate row's (series, aux_kind) pair that no scheduled.csv rule
    (or account growth series) ever reads is a silent no-op at run time --
    its drawn/literal value lands in aux_table but nothing looks it up. It's
    a warning, not an error (#92): a mix-in may set a series only the
    scenarios inheriting from it read."""
    append_calendar_row(
        date="2027-01-31", kind="set_rate", name="typo_series",
        series="mutual_fund", amount="3%",
        params_json='{"aux_kind": "inflation"}',
    )
    result = load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir,
                           allow_dirty_data=True)
    assert any(
        "set_rate series 'mutual_fund' under aux_kind 'inflation' is never read"
        in warning
        for warning in result.warnings
    )


def test_scheduled_percent_of_balance_series_with_no_supplier_rejected(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path], intent_dir: Path,
) -> None:
    """A percent_of_balance rule (including one synthesized from accounts.csv's
    own `series` column) whose series nothing supplies would otherwise only
    fail once run, with an opaque 'no aux row' EngineError."""
    accounts_path = retire_alice_csv("accounts.csv")
    df = pd.read_csv(accounts_path, dtype=str, keep_default_na=False)
    row = df["name"] == "Asset_TraditionalIRA_Alice"
    df.loc[row, "series"] = "no such series"
    df.loc[row, "tax_character"] = "deferred"
    df.to_csv(accounts_path, index=False)
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any(
        "references series 'no such series' under aux_kind 'rate', but "
        "nothing supplies it" in issue
        for issue in ei.value.issues
    )


def test_chain_step_series_with_no_supplier_rejected(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path], intent_dir: Path,
) -> None:
    """A chain step's series inflates its maximum cap over time
    (engine._chain_step_allocation) -- always aux_kind='inflation', and
    only actually read when maximum is also set. Nothing supplying it would
    otherwise only fail once run, with an opaque 'no aux row' EngineError."""
    chain_path = retire_alice_csv("chains") / "asset__asset_checking_liquidity.csv"
    df = pd.read_csv(chain_path, dtype=str, keep_default_na=False)
    df.loc[0, "maximum"] = "1000.00"
    df.loc[0, "series"] = "no_such_inflation_series"
    df.to_csv(chain_path, index=False)
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any(
        "references series 'no_such_inflation_series' under aux_kind "
        "'inflation', but nothing supplies it" in issue
        for issue in ei.value.issues
    )


def test_chain_step_series_with_a_calendar_supplier_loads_cleanly(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
    append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    chain_path = retire_alice_csv("chains") / "asset__asset_checking_liquidity.csv"
    df = pd.read_csv(chain_path, dtype=str, keep_default_na=False)
    df.loc[0, "maximum"] = "1000.00"
    df.loc[0, "series"] = "cpi"
    df.to_csv(chain_path, index=False)
    append_calendar_row(
        date="2026-01-31", kind="set_rate", name="cpi_for_chain",
        series="cpi", amount="3%", params_json='{"aux_kind": "inflation"}',
    )
    result = load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert result.intent_path.exists()


# --- #31: vary_median rename, shape, triangular ------------------------------------


def test_vary_mean_column_is_rejected_as_renamed(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path], intent_dir: Path,
) -> None:
    """A calendar.csv still carrying vary_mean must fail loudly: its meaning
    changed (it's now the truncated median), and the CSV reader would
    otherwise ignore the unknown column silently."""
    path = retire_alice_csv("calendar.csv")
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    for col in ("name", "vary_target", "vary_distribution", "vary_mean", "vary_sd"):
        if col not in df.columns:
            df[col] = ""
    df.loc[len(df)] = dict.fromkeys(df.columns, "") | {
        "date": "2027-02-28", "kind": "set_rate", "series": "Asset_Checking", "amount": "4.5%",
        "name": "checking_rate_vary", "vary_target": "amount",
        "vary_distribution": "truncated_normal", "vary_mean": "45000", "vary_sd": "0.5%",
    }
    df.to_csv(path, index=False)
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any("vary_mean was renamed to vary_median" in issue for issue in ei.value.issues)


def test_vary_truncated_normal_shape_loads_and_is_stored(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="truncated_normal",
        vary_median="4.5%", vary_sd="0.5%", vary_skew="0.5", vary_tailweight="0.8",
    )
    result = load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    con = sqlite3.connect(result.intent_path)
    try:
        stored = con.execute(
            "SELECT vary_median, vary_skew, vary_tailweight FROM calendar_event "
            "WHERE name = 'checking_rate_vary'"
        ).fetchone()
    finally:
        con.close()
    assert stored == ("45000", 0.5, 0.8)


@pytest.mark.parametrize(("extra", "message"), [
    ({"vary_skew": "0.5"}, "vary_sd is required"),               # shape needs an explicit sd
    ({"vary_sd": "0.5%", "vary_tailweight": "0"}, "tailweight must be positive"),
    ({"vary_sd": "0.5%", "vary_skew": "lots"}, "is not a number"),
])
def test_vary_truncated_normal_rejects_bad_shape(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
    extra: dict[str, str], message: str,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="truncated_normal",
        vary_median="4.5%", vary_low="3%", vary_high="6%", **extra,
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any(message in issue for issue in ei.value.issues), ei.value.issues


def test_vary_shape_rejected_on_uniform(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="uniform",
        vary_low="4%", vary_high="5%", vary_skew="0.5",
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any("vary_skew is not used by uniform" in issue for issue in ei.value.issues)


def test_vary_triangular_date_target_loads(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    """The three-point estimate for a date: most likely 2030-12-31, no
    earlier than mid-2030, could slip to the end of 2031."""
    append_calendar_row(
        date="2030-12-31", kind="retire_scheduled", name="retire_bill_vary",
        scheduled_name="salary_bill",
        vary_target="date", vary_distribution="triangular",
        vary_low="2030-06-30", vary_mode="2030-12-31", vary_high="2031-12-31",
    )
    load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)


@pytest.mark.parametrize(("extra", "message"), [
    ({"vary_low": "4%", "vary_high": "5%", "vary_mode": "6%"}, "vary_mode must lie within"),
    ({"vary_low": "4%"}, "triangular requires vary_high"),
    ({"vary_low": "4%", "vary_high": "5%", "vary_sd": "1%"}, "vary_sd is not used by triangular"),
])
def test_vary_triangular_rejects_bad_parameters(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
    extra: dict[str, str], message: str,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="triangular", **extra,
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any(message in issue for issue in ei.value.issues), ei.value.issues


def test_month_columns_accept_year_month_and_land_on_month_end(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
    append_calendar_row: Callable[..., None], write_aux, intent_dir: Path,
) -> None:
    """#34: YYYY-MM works wherever a date names a month, and a full date
    there is snapped to its month's end."""
    def edit(name: str, **cells: str) -> None:
        path = retire_alice_csv(name)
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
        for col, value in cells.items():
            df.loc[0, col] = value
        df.to_csv(path, index=False)

    edit("scenario.csv", start="2026-05", end="2040-12")
    edit("scheduled.csv", start="2026-05", base_date="2026-05-15")
    append_calendar_row(date="2029-03", kind="retire_scheduled", scheduled_name="salary_bill")
    write_aux("inflation__cpi.csv", "effective_date,annual_rate\n2021-01,1.4%\n")
    result = load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir,
                           allow_dirty_data=True)
    con = sqlite3.connect(result.intent_path)
    assert con.execute('SELECT start, "end" FROM scenario').fetchone() == ("2026-05-31", "2040-12-31")
    assert con.execute(
        "SELECT start, base_date FROM scheduled_transaction WHERE name = 'salary_alice'"
    ).fetchone() == ("2026-05-31", "2026-05-31")
    assert ("2029-03-31",) in con.execute(
        "SELECT date FROM calendar_event WHERE scheduled_name = 'salary_bill'"
    ).fetchall()
    assert con.execute(
        "SELECT effective_date FROM aux_table WHERE kind = 'inflation' AND series = 'cpi'"
    ).fetchall() == [("2021-01-31",)]


def test_month_column_that_is_not_a_month_is_reported(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    append_calendar_row(date="2029-3", kind="retire_scheduled", scheduled_name="salary_bill")
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any("date '2029-3' is not YYYY-MM or YYYY-MM-DD" in issue for issue in ei.value.issues)


def test_vary_date_bounds_bare_month_spans_the_month_full_date_is_exact(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    """#34: a bare month as a lower bound is its first day, as an upper
    bound its last, as a mode mid-month; a full date stays exact."""
    append_calendar_row(
        date="2029-06", kind="retire_scheduled", name="retire_bill_vary",
        scheduled_name="salary_bill",
        vary_target="date", vary_distribution="triangular",
        vary_low="2029-01", vary_mode="2029-06", vary_high="2029-12-10",
    )
    result = load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir,
                           allow_dirty_data=True)
    con = sqlite3.connect(result.intent_path)
    assert con.execute(
        "SELECT date, vary_low, vary_mode, vary_high FROM calendar_event "
        "WHERE name = 'retire_bill_vary'"
    ).fetchone() == ("2029-06-30", "2029-01-01", "2029-06-15", "2029-12-10")


def test_malformed_month_is_a_load_error_not_a_crash(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path], intent_dir: Path,
) -> None:
    """A reported bad month value must not reach later loader stages (e.g.
    the opening-balance derivation's ``int(start[:4])``) as a raw string."""
    path = retire_alice_csv("scenario.csv")
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df.loc[0, "start"] = "soon"
    df.to_csv(path, index=False)
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any("start 'soon' is not YYYY-MM or YYYY-MM-DD" in issue for issue in ei.value.issues)


def _set_scheduled(retire_alice_csv: Callable[[str], Path], name: str, **cells: str) -> None:
    path = retire_alice_csv("scheduled.csv")
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    for col, value in cells.items():
        df.loc[df["name"] == name, col] = value
    df.to_csv(path, index=False)


def test_biweekly_dates_keep_their_day(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path], intent_dir: Path,
) -> None:
    """A biweekly rule's start is its 14-day anchor and its end clips it, so
    a full date keeps its day; a bare-month end is still the month's end."""
    _set_scheduled(retire_alice_csv, "salary_alice",
                   cadence="biweekly", start="2026-05-15", end="2030-06")
    _set_scheduled(retire_alice_csv, "salary_bill",
                   cadence="biweekly", start="2026-05-08", end="2030-06-12")
    result = load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir,
                           allow_dirty_data=True)
    con = sqlite3.connect(result.intent_path)
    assert dict(
        (name, (start, end)) for name, start, end in con.execute(
            'SELECT name, start, "end" FROM scheduled_transaction '
            "WHERE name IN ('salary_alice', 'salary_bill')"
        )
    ) == {
        "salary_alice": ("2026-05-15", "2030-06-30"),
        "salary_bill": ("2026-05-08", "2030-06-12"),
    }


def test_biweekly_start_needs_a_full_date(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path], intent_dir: Path,
) -> None:
    _set_scheduled(retire_alice_csv, "salary_alice", cadence="biweekly", start="2026-05")
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any("start '2026-05' needs a full YYYY-MM-DD" in issue for issue in ei.value.issues)


def test_retire_scheduled_date_keeps_its_day(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    """It becomes the rule's end, where the day matters (ADR-006 §2)."""
    append_calendar_row(date="2029-03-12", kind="retire_scheduled", scheduled_name="salary_bill")
    result = load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir,
                           allow_dirty_data=True)
    con = sqlite3.connect(result.intent_path)
    assert ("2029-03-12",) in con.execute(
        "SELECT date FROM calendar_event WHERE kind = 'retire_scheduled'"
    ).fetchall()


def test_vary_date_bound_rejects_iso_week_date(
    test_data_dir: Path, append_calendar_row: Callable[..., None], intent_dir: Path,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="retire_scheduled", name="retire_bill_vary",
        scheduled_name="salary_bill",
        vary_target="date", vary_distribution="uniform",
        vary_low="2029-W01-1", vary_high="2029-12-31",
    )
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any("vary_low '2029-W01-1' is not a valid" in issue for issue in ei.value.issues)


@pytest.mark.parametrize("bad", ["2021-W01-1", "20210104", "1960-03"])
def test_day_precise_dates_are_strict(
    bad: str, test_data_dir: Path, retire_alice_csv: Callable[[str], Path], intent_dir: Path,
) -> None:
    """birthdate and a lot's acquired keep their day, so they must be a
    strict YYYY-MM-DD (not a week date, basic format, or bare month)."""
    persons = pd.read_csv(test_data_dir / "persons.csv", dtype=str, keep_default_na=False)
    persons.loc[0, "birthdate"] = bad
    persons.to_csv(test_data_dir / "persons.csv", index=False)
    values = pd.read_csv(retire_alice_csv("account_values.csv"), dtype=str, keep_default_na=False)
    values.loc[0, "acquired"] = bad
    values.to_csv(retire_alice_csv("account_values.csv"), index=False)
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "retire_alice", intent_dir=intent_dir, allow_dirty_data=True)
    assert any(f"birthdate '{bad}' is not YYYY-MM-DD" in issue for issue in ei.value.issues)
    assert any(f"acquired '{bad}' is not YYYY-MM-DD" in issue for issue in ei.value.issues)
