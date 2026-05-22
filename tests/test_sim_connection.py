"""Tests for ScenarioConnection / RunConnection."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from analysis.sim.connection import RunConnection, open_run, open_scenario
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

        # CSV was rewritten.
        acc = pd.read_csv(data / "accounts.csv", dtype=str, keep_default_na=False)
        tags = acc.loc[acc["name"] == "Asset_Checking", "tags"].iloc[0]
        assert "expense:liquid" in tags

        # items_for_tag picks up the account.
        items = sc.items_for_tag("expense", "liquid")
        assert (items["item_type"] == "account").any()
        assert (items["label"] == "Asset_Checking").any()


def test_scenario_connection_add_is_idempotent_and_rm_works(tmp_path: Path) -> None:
    data = _copy_data(tmp_path)
    with open_scenario(data, "retire_alice") as sc:
        sc.add_tags("account", "Asset_Checking", "x:y")
        sc.add_tags("account", "Asset_Checking", "x:y")  # duplicate
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
    # Tag via scenario, then build an intent file to mimic a run snapshot.
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
        items = rc.items_for_tag("run", "ok", item_type="account")
        assert (items["label"] == "Asset_Checking").any()
