"""Tests for ScenarioConnection / RunConnection."""

from __future__ import annotations

import shutil
from pathlib import Path

import pandas as pd
import pytest

from analysis.frames import index_years
from analysis.sim.connection import RunConnection, ScenarioConnection, find_latest_run, open_run, open_scenario
from analysis.sim.engine import run_scenario
from analysis.sim.loader import ScenarioRef, load_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = REPO_ROOT / "prototype_data"


def _copy_data(tmp_path: Path) -> Path:
    dst = tmp_path / "data"
    shutil.copytree(PROTOTYPE, dst)
    return dst


def test_scenario_connection_tag_add_read_round_trip(tmp_path: Path) -> None:
    data = _copy_data(tmp_path)
    with open_scenario(data, "retire_alice") as sc:
        assert sc.list_tags().empty

        new = sc.add_tags("account", "Asset_Checking", "expense:liquid notebook:nw")
        assert "expense:liquid" in new and "notebook:nw" in new

        df = sc.list_tags()
        roles = set(zip(df["role"], df["name"], strict=True))
        assert ("expense", "liquid") in roles
        assert ("notebook", "nw") in roles

        acc = pd.read_csv(data / "scenarios" / "retire_alice" / "accounts.csv", dtype=str, keep_default_na=False)
        tags = acc.loc[acc["name"] == "Asset_Checking", "tags"].iloc[0]
        assert "expense:liquid" in tags

        hit = sc.items("account", tags="expense:liquid")
        assert "Asset_Checking" in set(hit["name"])


def test_scenario_connection_add_is_idempotent_and_rm_works(tmp_path: Path) -> None:
    data = _copy_data(tmp_path)
    with open_scenario(data, "retire_alice") as sc:
        sc.add_tags("account", "Asset_Checking", "x:y")
        sc.add_tags("account", "Asset_Checking", "x:y")
        df = sc.list_tags(role="x")
        row = df.loc[df["name"] == "y"].iloc[0]
        assert row["n_account"] == 1

        final = sc.remove_tags("account", "Asset_Checking", "x:y")
        assert "x:y" not in final
        assert sc.list_tags(role="x").empty


def test_items_matches_a_whole_tag_role_with_a_wildcard(tmp_path: Path) -> None:
    """`role:*` selects every tag in that role, so review passes can ask for
    e.g. all `estimate:` rows without naming each subject."""
    data = _copy_data(tmp_path)
    with open_scenario(data, "retire_alice") as sc:
        sc.add_tags("account", "Asset_Checking", "estimate:balance")
        sc.add_tags("account", "Asset_Savings", "estimate:rate")
        sc.add_tags("account", "Asset_Brokerage_Joint", "other:thing")

        hit = set(sc.items("account", tags="estimate:*")["name"])
        assert hit == {"Asset_Checking", "Asset_Savings"}

        assert set(sc.items("account", tags=[("estimate", "*")])["name"]) == hit

        # Mixed selectors, and "all" requires the role to be present too.
        both = sc.items("account", tags=["estimate:*", "other:thing"])
        assert len(both) == 3
        assert sc.items(
            "account", tags=["estimate:*", "other:thing"], tags_match="all",
        ).empty
        sc.add_tags("account", "Asset_Checking", "other:thing")
        assert set(
            sc.items(
                "account", tags=["estimate:*", "other:thing"], tags_match="all",
            )["name"]
        ) == {"Asset_Checking"}


def test_scenario_connection_rejects_bad_token(tmp_path: Path) -> None:
    data = _copy_data(tmp_path)
    with open_scenario(data, "retire_alice") as sc:
        with pytest.raises(ValueError):
            sc.add_tags("account", "Asset_Checking", "no_colon")


def test_scenario_connection_unknown_item_raises(tmp_path: Path) -> None:
    data = _copy_data(tmp_path)
    with open_scenario(data, "retire_alice") as sc:
        with pytest.raises(KeyError):
            sc.add_tags("account", "no_such_account", "a:b")


def test_scenario_connection_scenario_property_returns_self(tmp_path: Path) -> None:
    """``Connection.scenario`` lets polymorphic code written against the
    shared base (``conn.scenario.whatever``) work the same whether `conn`
    started as a RunConnection (which builds/caches one) or a
    ScenarioConnection (which already is one)."""
    data = _copy_data(tmp_path)
    with open_scenario(data, "retire_alice") as sc:
        assert sc.scenario is sc


def test_run_connection_reads_tags_from_snapshot(test_retire_alice_scenario: ScenarioConnection,
                                                 test_retire_alice_scenario_ref: ScenarioRef,
                                                 retire_alice_run_path: Path,
                                                 test_data_dir: Path) -> None:
    """
    Test that the run connection correctly reads tags from the snapshot
    created by the scenario connection.

    This uses per-test (function) fixtures for the scenario connection and its reference,
    because we modify the scenario connection within the test.
    """
    # Add a tag to the scenario connection before creating the run connection.
    test_retire_alice_scenario.add_tags("account", "Asset_Checking", "run:ok")
    ref = test_retire_alice_scenario_ref

    # Load the modified scenario
    result = load_scenario(
        test_data_dir, "retire_alice",
        scenarios_dir=ref.scenarios_dir,
        intent_dir=ref.intent_dir,
        allow_dirty_data=True,
        compute_stamps=False,
    )

    # Run the modified scenario
    run = run_scenario(
        result.intent_path, retire_alice_run_path,
        allow_dirty_data=True,
        run_by="TEST",
    )

    # Validate the modified scenario including our new tag.
    with open_run(run,
                  scenario="retire_alice",
                  data_dir=test_data_dir,
                  scenarios_dir=ref.scenarios_dir) as rc:
        assert isinstance(rc, RunConnection)
        df = rc.list_tags(role="run")
        assert (df["name"] == "ok").any()
        hit = rc.items("account", tags="run:ok")
        assert "Asset_Checking" in set(hit["name"])


def test_items_filters_by_columns(tmp_path: Path) -> None:
    data = _copy_data(tmp_path)
    with open_scenario(data, "retire_alice") as sc:
        all_acc = sc.items("account")
        assert len(all_acc) > 0
        assert {"name", "kind", "subkind", "tags"} <= set(all_acc.columns)

        assets = sc.items("account", kind="asset")
        assert (assets["kind"] == "asset").all()
        assert len(assets) < len(all_acc)

        kinds = sc.items("account", kind=["asset", "expense"])
        assert set(kinds["kind"]) <= {"asset", "expense"}


def test_find_latest_run_picks_lexicographically_last_file(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    for name in ["2026-01-01T000000Z_a.sqlite3", "2026-06-01T000000Z_b.sqlite3"]:
        (runs_dir / name).write_bytes(b"x")

    assert find_latest_run(runs_dir).name == "2026-06-01T000000Z_b.sqlite3"


def test_find_latest_run_raises_when_empty(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    with pytest.raises(FileNotFoundError):
        find_latest_run(runs_dir)


def test_find_latest_run_filters_by_scenario(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    for name in [
        "2026-01-01T000000Z_current_abc1234.sqlite3",
        "2026-06-01T000000Z_tight_abc1234.sqlite3",
    ]:
        (runs_dir / name).write_bytes(b"x")

    latest = find_latest_run(runs_dir, "current")
    assert latest.name == "2026-01-01T000000Z_current_abc1234.sqlite3"
    with pytest.raises(FileNotFoundError):
        find_latest_run(runs_dir, "nope")


def test_run_connection_account_balance_item_type(retire_alice_run: RunConnection) -> None:
    df = retire_alice_run.items("account_balance")
    assert {"month", "account", "kind", "balance"} <= set(df.columns)
    assert "Asset_Checking" in set(df["account"])


def test_run_connection_tax_year_summary_item_type(retire_alice_run: RunConnection) -> None:
    df = retire_alice_run.items("tax_year_summary")
    assert {"year", "bucket", "amount"} <= set(df.columns)


def test_items_unknown_column_raises(retire_alice_scenario: ScenarioConnection) -> None:
    with pytest.raises(KeyError):
        retire_alice_scenario.items("account", not_a_column="x")


def test_items_tag_filter_any_and_all(retire_alice_scenario: ScenarioConnection) -> None:
    sc = retire_alice_scenario
    sc.add_tags("account", "Asset_Checking", "expense:liquid notebook:nw")
    sc.add_tags("account", "Asset_Brokerage_Joint", "notebook:nw")

    nw = sc.items("account", tags="notebook:nw")
    assert set(nw["name"]) == {"Asset_Checking", "Asset_Brokerage_Joint"}

    any_two = sc.items("account", tags=["expense:liquid", "notebook:nw"])
    assert set(any_two["name"]) == {"Asset_Checking", "Asset_Brokerage_Joint"}

    all_two = sc.items(
        "account",
        tags={"expense:liquid", "notebook:nw"},
        tags_match="all",
    )
    assert list(all_two["name"]) == ["Asset_Checking"]

    pairs = sc.items("account", tags=[("expense", "liquid")])
    assert list(pairs["name"]) == ["Asset_Checking"]

    assert sc.items("account", tags="missing:tag").empty

    combo = sc.items("account", tags="notebook:nw", kind="asset")
    assert (combo["kind"] == "asset").all()
    assert "Asset_Checking" in set(combo["name"])


def test_items_cache_invalidated_on_write(retire_alice_scenario: ScenarioConnection) -> None:
    sc = retire_alice_scenario
    before = sc.items("account", tags="x:y")
    assert before.empty

    sc.add_tags("account", "Asset_Checking", "x:y")

    after = sc.items("account", tags="x:y")
    assert "Asset_Checking" in set(after["name"])


def test_items_unknown_type(retire_alice_scenario: ScenarioConnection) -> None:
    sc = retire_alice_scenario
    with pytest.raises(ValueError):
        sc.items("bogus")  # ty: ignore[invalid-argument-type]  # deliberately not an ItemType


def test_net_worth_frame_has_nominal_and_cpi_constant(retire_alice_run: RunConnection) -> None:
    rc = retire_alice_run
    nw = rc.net_worth_frame()
    assert {"nominal", "cpi_constant", "cumulative_index_micros"} <= set(nw.columns)
    assert nw.index.name == "month"
    # Cached: same object on a second call.
    assert rc.net_worth_frame() is nw


def test_to_cpi_constant_matches_manual_conversion(retire_alice_run: RunConnection) -> None:
    rc = retire_alice_run
    nw = rc.net_worth_frame()
    converted = rc.to_cpi_constant(nw[["nominal"]])
    factor = 1_000_000 / nw["cumulative_index_micros"]
    expected = nw["nominal"] * factor
    pd.testing.assert_series_equal(
        converted["nominal"], expected, check_names=False,
    )


def test_balance_pivot_filters_by_kind_and_is_cents_to_dollars(retire_alice_run: RunConnection) -> None:
    rc = retire_alice_run
    full = rc.balance_pivot()
    assert "Asset_Checking" in full.columns

    assets = rc.balance_pivot("asset")
    kinds = rc.account_kinds()
    assert all(kinds[c] == "asset" for c in assets.columns)
    assert "Asset_Checking" in assets.columns

    raw = rc.items("account_balance")
    raw_checking = raw[raw["account"] == "Asset_Checking"].iloc[0]
    month = pd.to_datetime(raw_checking["month"])
    assert full.loc[month, "Asset_Checking"] == raw_checking["balance"] / 100


def test_income_balance_pivot_displays_credits_as_positive_inflows(retire_alice_run: RunConnection) -> None:
    rc = retire_alice_run
    raw = rc.items("account_balance")
    raw_income = raw[raw["account"] == "Income_Salary_Alice"].iloc[0]
    month = pd.to_datetime(raw_income["month"])

    income = rc.balance_pivot("income")

    assert income.loc[month, "Income_Salary_Alice"] == -raw_income["balance"] / 100


def test_income_and_expenses_by_category_have_total_column(retire_alice_run: RunConnection) -> None:
    rc = retire_alice_run
    income = rc.income_by_category()
    assert "total" in income.columns
    expenses = rc.expenses_by_category()
    assert "total" in expenses.columns
    assert "other" in expenses.columns  # unlike income, "other" is kept


def test_gains_by_category_covers_expenses_with_distribution_coverage(
    retire_alice_run: RunConnection,
) -> None:
    rc = retire_alice_run
    gains = rc.gains_by_category()
    assert {
        "taxable", "non-taxable", "unrealized",
        "realized_total", "expenses", "distribution_coverage",
    } <= set(gains.columns)
    assert (
        gains["distribution_coverage"]
        == gains["realized_total"] - gains["expenses"]
    ).all()


def test_tax_helpers_return_year_indexed_frames(retire_alice_run: RunConnection) -> None:
    rc = retire_alice_run
    liability = rc.tax_liability_by_year()
    assert isinstance(liability, pd.Series)
    reconciliation = rc.tax_reconciliation_by_year()
    assert "reconciliation_year" in reconciliation.columns
    outstanding = rc.outstanding_tax_by_year()
    assert isinstance(outstanding, pd.Series)
    summary = rc.tax_summary_by_year()
    assert "total tax" in summary.columns


def test_ss_gross_by_month_only_includes_ss_benefit_character(retire_alice_run: RunConnection) -> None:
    rc = retire_alice_run
    ss = rc.ss_gross_by_month()
    te = rc.items("transfer_event")
    expected_total = te.loc[te["tax_character"] == "ss_benefit", "amount"].sum() / 100
    assert ss.sum() == pytest.approx(expected_total)


def test_year_end_values_and_yearly_totals(retire_alice_run: RunConnection) -> None:
    from analysis.sim.connection import year_end_values, yearly_totals

    rc = retire_alice_run
    nw = rc.net_worth_frame()
    year_end = year_end_values(nw[["nominal"]])
    assert (year_end.index == sorted(year_end.index)).all()
    assert set(index_years(nw)) >= set(year_end.index)

    income_yearly = yearly_totals(rc.income_by_category())
    assert income_yearly.index.name == "year"
    assert set(income_yearly.index) == set(index_years(rc.income_by_category()))


@pytest.mark.parametrize(("thread_name", "warns"), [
    ("Thread-unexplained", True),
    # JupyterLab sends widget messages to an ipykernel subshell (#375): expected, no warning.
    ("subshell-0f3c", False),
])
def test_a_query_from_a_different_thread_falls_back_to_a_temporary_connection(
    retire_alice_run, thread_name: str, warns: bool,
) -> None:
    import threading
    import warnings

    results: list = []
    errors: list = []

    def query_from_another_thread() -> None:
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                frame = retire_alice_run.items("person")
                results.append((frame, caught))
        except Exception as exc:  # pragma: no cover - surfaced via errors below
            errors.append(exc)

    thread = threading.Thread(target=query_from_another_thread, name=thread_name)
    thread.start()
    thread.join()

    assert not errors, errors
    (frame, caught), = results
    assert not frame.empty
    assert any("not the thread that opened" in str(w.message) for w in caught) == warns
