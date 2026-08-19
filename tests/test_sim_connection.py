"""Tests for ScenarioConnection / RunConnection."""

from __future__ import annotations

import shutil
from pathlib import Path

import pandas as pd
import pytest

from analysis.sim.connection import RunConnection, find_latest_run, open_run, open_scenario
from analysis.sim.engine import run_scenario
from analysis.sim.loader import load_scenario

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


def test_run_connection_reads_tags_from_snapshot(tmp_path: Path) -> None:
    data = _copy_data(tmp_path)
    with open_scenario(data, "retire_alice") as sc:
        sc.add_tags("account", "Asset_Checking", "run:ok")

    intent_dir = tmp_path / "intent"
    result = load_scenario(
        data, "retire_alice", intent_dir=intent_dir,
        allow_dirty_data=True, compute_stamps=False,
    )

    with open_run(result.intent_path) as rc:
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
        (runs_dir / name).touch()

    assert find_latest_run(runs_dir).name == "2026-06-01T000000Z_b.sqlite3"


def test_find_latest_run_raises_when_empty(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    with pytest.raises(FileNotFoundError):
        find_latest_run(runs_dir)


def test_run_connection_account_balance_item_type(tmp_path: Path) -> None:
    data = _copy_data(tmp_path)
    intent_path = load_scenario(
        data, "retire_alice",
        intent_dir=tmp_path / "intent", loaded_by="test",
        allow_dirty_data=True,
    ).intent_path
    run_path = run_scenario(
        intent_path, tmp_path / "runs" / "r.sqlite3",
        run_by="test", data_mode="prototype", allow_dirty_data=True,
    )

    with open_run(run_path) as rc:
        df = rc.items("account_balance")
        assert {"month", "account", "kind", "balance"} <= set(df.columns)
        assert "Asset_Checking" in set(df["account"])


def test_items_unknown_column_raises(tmp_path: Path) -> None:
    data = _copy_data(tmp_path)
    with open_scenario(data, "retire_alice") as sc:
        with pytest.raises(KeyError):
            sc.items("account", not_a_column="x")


def test_items_tag_filter_any_and_all(tmp_path: Path) -> None:
    data = _copy_data(tmp_path)
    with open_scenario(data, "retire_alice") as sc:
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


def test_items_cache_invalidated_on_write(tmp_path: Path) -> None:
    data = _copy_data(tmp_path)
    with open_scenario(data, "retire_alice") as sc:
        before = sc.items("account", tags="x:y")
        assert before.empty

        sc.add_tags("account", "Asset_Checking", "x:y")

        after = sc.items("account", tags="x:y")
        assert "Asset_Checking" in set(after["name"])


def test_items_unknown_type(tmp_path: Path) -> None:
    data = _copy_data(tmp_path)
    with open_scenario(data, "retire_alice") as sc:
        with pytest.raises(ValueError):
            sc.items("bogus")  # type: ignore[arg-type]
