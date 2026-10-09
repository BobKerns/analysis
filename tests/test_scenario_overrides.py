"""Per-scenario overrides for aux/, series/, prices/ and itemizations/
(ADR-003 §4.1d): a scenario's own folder wins over its ancestors', resolved
whole-file and via parent_scenario lineage. aux/, prices/ and itemizations/
also have a shared data repo root default; series/ does not."""

# cspell:ignore NEWFUND

from __future__ import annotations

import csv
import shutil
import sqlite3
from pathlib import Path

import pytest

from analysis.sim.loader import LoadError, load_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = REPO_ROOT / "prototype_data"


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


def _rmd_start_age(intent_path: Path) -> int:
    con = sqlite3.connect(intent_path)
    try:
        (payload,) = con.execute(
            "SELECT payload_json FROM aux_table WHERE kind = 'rmd_start_age'"
        ).fetchone()
    finally:
        con.close()
    import json
    return json.loads(payload)["start_age"]


def _write_rmd_start_age(path: Path, age: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"effective_date,start_age\n2026-01-31,{age}\n", encoding="utf-8")


@pytest.fixture(scope="function")
def repo(tmp_path: Path) -> Path:
    dest = tmp_path / "data"
    shutil.copytree(PROTOTYPE, dest)
    return dest


def test_a_scenario_overrides_one_shared_aux_file(repo: Path, tmp_path: Path) -> None:
    shared_age = _rmd_start_age(
        load_scenario(repo, "retire_alice", intent_dir=tmp_path / "shared",
                     allow_dirty_data=True).intent_path
    )
    _write_rmd_start_age(repo / "scenarios" / "retire_alice" / "aux" / "rmd_start_age.csv", 80)
    overridden = load_scenario(repo, "retire_alice", intent_dir=tmp_path / "override",
                              allow_dirty_data=True)
    assert _rmd_start_age(overridden.intent_path) == 80 != shared_age


def test_a_scenario_can_add_a_series_file_the_shared_dir_does_not_have(
    repo: Path, tmp_path: Path,
) -> None:
    series_dir = repo / "scenarios" / "retire_alice" / "series"
    series_dir.mkdir(exist_ok=True)
    (series_dir / "rate__NEWFUND.csv").write_text(
        "effective_date,annual_rate\n2026-01-31,5\n", encoding="utf-8",
    )
    result = load_scenario(repo, "retire_alice", intent_dir=tmp_path / "out",
                           allow_dirty_data=True)
    con = sqlite3.connect(result.intent_path)
    try:
        rows = con.execute(
            "SELECT series FROM aux_table WHERE kind = 'rate' AND series = 'NEWFUND'"
        ).fetchall()
    finally:
        con.close()
    assert rows == [("NEWFUND",)]


def test_a_shared_series_dir_at_the_data_repo_root_is_not_read(
    repo: Path, tmp_path: Path,
) -> None:
    """series/ has no shared root tier: growth-rate and inflation series are
    per-scenario projections, not tax-law facts (unlike aux/), so a stray
    top-level series/ file is simply never looked at."""
    series_dir = repo / "series"
    series_dir.mkdir(exist_ok=True)
    (series_dir / "rate__STRAY.csv").write_text(
        "effective_date,annual_rate\n2026-01-31,5\n", encoding="utf-8",
    )
    result = load_scenario(repo, "retire_alice", intent_dir=tmp_path / "out",
                           allow_dirty_data=True)
    con = sqlite3.connect(result.intent_path)
    try:
        rows = con.execute(
            "SELECT series FROM aux_table WHERE kind = 'rate' AND series = 'STRAY'"
        ).fetchall()
    finally:
        con.close()
    assert rows == []


def test_prices_are_shared_at_the_data_repo_root(repo: Path, tmp_path: Path) -> None:
    """Unlike series/, prices/ is a shared root tier: security prices are
    global facts, not per-scenario projections."""
    prices_dir = repo / "prices"
    prices_dir.mkdir(exist_ok=True)
    (prices_dir / "security_price__ZZZ.csv").write_text(
        "effective_date,price\n2026-01-31,10.00\n", encoding="utf-8",
    )
    result = load_scenario(repo, "retire_alice", intent_dir=tmp_path / "out",
                           allow_dirty_data=True)
    con = sqlite3.connect(result.intent_path)
    try:
        rows = con.execute(
            "SELECT series FROM aux_table WHERE kind = 'security_price' AND series = 'ZZZ'"
        ).fetchall()
    finally:
        con.close()
    assert rows == [("ZZZ",)]


def test_a_child_inherits_its_parents_aux_override_and_can_override_it_again(
    repo: Path, tmp_path: Path,
) -> None:
    _write_rmd_start_age(repo / "scenarios" / "retire_alice" / "aux" / "rmd_start_age.csv", 80)
    _derive_empty_child(repo, "retire_alice", "child")
    inherited = load_scenario(repo, "child", intent_dir=tmp_path / "inherited",
                              allow_dirty_data=True)
    assert _rmd_start_age(inherited.intent_path) == 80  # picked up via the parent, not just the shared default

    _write_rmd_start_age(repo / "scenarios" / "child" / "aux" / "rmd_start_age.csv", 85)
    overridden = load_scenario(repo, "child", intent_dir=tmp_path / "overridden",
                               allow_dirty_data=True)
    assert _rmd_start_age(overridden.intent_path) == 85  # the child's own wins over its parent's


def _write_home_basis(path: Path, purchase: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["Item", "Value"])
        w.writeheader()
        w.writerow({"Item": "purchase", "Value": purchase})


def _set_home_basis_reference(repo: Path, scenario: str = "retire_alice") -> None:
    av = repo / "scenarios" / scenario / "account_values.csv"
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


def _home_basis(intent_path: Path) -> int:
    con = sqlite3.connect(intent_path)
    try:
        return con.execute(
            "SELECT m.opening_cost_basis FROM account_model_member m "
            "JOIN account a ON a.id = m.account_id WHERE a.name = 'Asset_Home'"
        ).fetchone()[0]
    finally:
        con.close()


def test_a_scenario_overrides_a_shared_itemization(repo: Path, tmp_path: Path) -> None:
    _write_home_basis(repo / "itemizations" / "home_basis.csv", "300000.00")
    _set_home_basis_reference(repo)
    shared = load_scenario(repo, "retire_alice", intent_dir=tmp_path / "shared",
                           allow_dirty_data=True)
    assert _home_basis(shared.intent_path) == 30_000_000

    _write_home_basis(
        repo / "scenarios" / "retire_alice" / "itemizations" / "home_basis.csv", "450000.00",
    )
    overridden = load_scenario(repo, "retire_alice", intent_dir=tmp_path / "override",
                               allow_dirty_data=True)
    assert _home_basis(overridden.intent_path) == 45_000_000


def test_a_missing_itemization_lists_every_place_it_looked(repo: Path, tmp_path: Path) -> None:
    _set_home_basis_reference(repo)
    with pytest.raises(LoadError) as ei:
        load_scenario(repo, "retire_alice", intent_dir=tmp_path / "out", allow_dirty_data=True)
    message = "; ".join(ei.value.issues)
    assert "not found" in message
    assert str(repo / "scenarios" / "retire_alice" / "itemizations") in message
    assert str(repo / "itemizations") in message
