"""Multiple inheritance of scenarios (issue #59, ADR-003 §4.5): a
``parent_scenario`` naming a main parent and colon-separated mix-ins, loaded
in ``lineage.load_order``."""

from __future__ import annotations

import io
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import git
import pandas as pd
import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim.lineage import ScenarioCycleError, load_order, parse_parents
from analysis.sim.loader import LoadError, load_scenario
from analysis.sim.provenance import data_stamps
from analysis.sim.staging import _affected_scenarios

PROTOTYPE = Path(__file__).resolve().parents[1] / "prototype_data"
ROOT = "retire_alice"


def _scheduled_override(**amounts: str) -> str:
    """scheduled.csv text repeating the prototype's rows named in ``amounts``
    with only their ``amount`` changed (an override replaces a row whole)."""
    rows = pd.read_csv(
        PROTOTYPE / "scenarios" / ROOT / "scheduled.csv", dtype=str, keep_default_na=False,
    )
    rows = rows[rows["name"].isin(amounts)].copy()
    rows["amount"] = rows["name"].map(amounts)
    out = io.StringIO()
    rows.to_csv(out, index=False)
    return out.getvalue()


def _amounts(intent_path: Path) -> dict[str, int]:
    with sqlite3.connect(intent_path) as con:
        return dict(con.execute("SELECT name, amount_cents FROM scheduled_transaction"))


@pytest.fixture
def load(test_data_dir: Path, tmp_path: Path) -> Callable[[str], Path]:
    def _load(name: str) -> Path:
        return load_scenario(
            test_data_dir, name, intent_dir=tmp_path / "intent" / name, allow_dirty_data=True,
        ).intent_path
    return _load


# -- load order -------------------------------------------------------------


def test_parse_parents_splits_on_colons_and_drops_blanks() -> None:
    assert parse_parents("") == []
    assert parse_parents(None) == []
    assert parse_parents("base") == ["base"]
    assert parse_parents(" base : mix_a::mix_b ") == ["base", "mix_a", "mix_b"]


def test_single_parent_order_is_the_ancestor_chain(
    test_scenarios_dir: Path, derive_scenario: Callable[..., Path],
) -> None:
    derive_scenario("child", ROOT)
    derive_scenario("grandchild", "child")
    assert load_order(test_scenarios_dir, "grandchild") == [ROOT, "child", "grandchild"]


def test_a_mixin_stops_at_the_first_ancestor_already_loaded(
    test_scenarios_dir: Path, derive_scenario: Callable[..., Path],
) -> None:
    derive_scenario("main", ROOT)
    derive_scenario("mid", ROOT)
    derive_scenario("mix", "mid")
    derive_scenario("other_mix", ROOT)
    derive_scenario("combo", "main:mix:other_mix")
    assert load_order(test_scenarios_dir, "combo") == [
        ROOT, "main", "mid", "mix", "other_mix", "combo",
    ]


def test_a_mixin_already_in_the_main_line_adds_nothing(
    test_scenarios_dir: Path, derive_scenario: Callable[..., Path],
) -> None:
    derive_scenario("main", ROOT)
    derive_scenario("combo", f"main:{ROOT}:main")
    assert load_order(test_scenarios_dir, "combo") == [ROOT, "main", "combo"]


def test_a_cycle_through_a_mixin_is_rejected_strictly_and_tolerated_otherwise(
    test_scenarios_dir: Path, derive_scenario: Callable[..., Path],
) -> None:
    derive_scenario("a", f"{ROOT}:b")
    derive_scenario("b", f"{ROOT}:a")
    with pytest.raises(ScenarioCycleError, match="a -> b -> a"):
        load_order(test_scenarios_dir, "a", strict=True)
    assert load_order(test_scenarios_dir, "a") == [ROOT, "b", "a"]


# -- loading ----------------------------------------------------------------


def test_mixins_contribute_their_rows_and_the_later_one_wins(
    derive_scenario: Callable[..., Path], load: Callable[[str], Path],
) -> None:
    derive_scenario("mix_a", ROOT, {
        "scheduled.csv": _scheduled_override(salary_alice="9000.00"),
    })
    derive_scenario("mix_b", ROOT, {
        "scheduled.csv": _scheduled_override(salary_alice="9500.00", salary_bill="7000.00"),
    })
    derive_scenario("combo", f"{ROOT}:mix_a:mix_b")
    derive_scenario("combo_ab", f"{ROOT}:mix_b:mix_a")

    base = _amounts(load(ROOT))
    combo = _amounts(load("combo"))
    assert combo["salary_alice"] == 950000
    assert combo["salary_bill"] == 700000
    assert set(combo) == set(base)
    assert _amounts(load("combo_ab"))["salary_alice"] == 900000


def test_the_scenarios_own_rows_beat_its_mixins(
    derive_scenario: Callable[..., Path], load: Callable[[str], Path],
) -> None:
    derive_scenario("mix_a", ROOT, {
        "scheduled.csv": _scheduled_override(salary_alice="9000.00"),
    })
    derive_scenario("combo", f"{ROOT}:mix_a", {
        "scheduled.csv": _scheduled_override(salary_alice="8500.00"),
    })
    assert _amounts(load("combo"))["salary_alice"] == 850000


def test_a_mixins_series_and_chain_overrides_are_inherited(
    derive_scenario: Callable[..., Path], load: Callable[[str], Path],
) -> None:
    derive_scenario("mix_rates", ROOT, {
        "series/rate__Asset_Checking.csv": "effective_date,annual_rate_micros\n2026-01-31,10000\n",
        "chains/asset__asset_checking_liquidity.csv": (
            "account_name,lot,maximum,percent,series,cadence\n"
            "Asset_TraditionalIRA_Alice,default,,,,\n"
        ),
    })
    derive_scenario("combo", f"{ROOT}:mix_rates")
    with sqlite3.connect(load("combo")) as con:
        (payload,) = con.execute(
            "SELECT payload_json FROM aux_table WHERE kind = 'rate' AND series = 'Asset_Checking'"
        ).fetchone()
        steps = con.execute(
            "SELECT COUNT(*) FROM chain_steps s JOIN chain c ON c.id = s.chain_id "
            "WHERE c.name = 'asset_checking_liquidity'"
        ).fetchone()[0]
    assert json.loads(payload)["annual_rate_micros"] == 10000
    assert steps == 1


def test_a_mixin_cycle_fails_the_load(
    derive_scenario: Callable[..., Path], test_data_dir: Path, tmp_path: Path,
) -> None:
    derive_scenario("a", f"{ROOT}:b")
    derive_scenario("b", f"{ROOT}:a")
    with pytest.raises(LoadError, match="scenario inheritance cycle"):
        load_scenario(test_data_dir, "a", intent_dir=tmp_path, allow_dirty_data=True)


def test_a_missing_mixin_fails_the_load(
    derive_scenario: Callable[..., Path], test_data_dir: Path, tmp_path: Path,
) -> None:
    derive_scenario("combo", f"{ROOT}:nowhere")
    with pytest.raises(LoadError, match="scenario folder not found"):
        load_scenario(test_data_dir, "combo", intent_dir=tmp_path, allow_dirty_data=True)


# -- provenance and staging -------------------------------------------------


@pytest.fixture
def committed_combo(test_data_dir: Path, derive_scenario: Callable[..., Path]) -> Path:
    """A ``combo`` of ``retire_alice`` and a ``mix`` mix-in, committed to git,
    plus an ``unrelated`` sibling."""
    derive_scenario("mix", ROOT)
    derive_scenario("combo", f"{ROOT}:mix")
    derive_scenario("unrelated", ROOT)
    repo = git.Repo.init(test_data_dir, b="main")
    repo.git.add(A=True)
    with repo.config_writer() as cw:
        cw.set_value("commit", "gpgsign", "false")
    actor = git.Actor("t", "t@t")
    repo.index.commit("init", author=actor, committer=actor)
    return test_data_dir


def test_a_mixin_edit_moves_the_combos_tree_id(committed_combo: Path) -> None:
    before = data_stamps(committed_combo, "combo")
    assert not before.dirty
    (committed_combo / "scenarios" / "mix" / "scheduled.csv").write_text(
        _scheduled_override(salary_alice="9000.00"), encoding="utf-8",
    )
    after = data_stamps(committed_combo, "combo")
    assert after.dirty
    assert after.tree_id != before.tree_id
    assert not data_stamps(committed_combo, "unrelated").dirty


def test_editing_a_mixin_marks_scenarios_using_it_affected(committed_combo: Path) -> None:
    assert _affected_scenarios(committed_combo, {"mix"}) == ["combo", "mix"]


# -- scenario create ----------------------------------------------------------


@pytest.mark.uses_current_scenario_write
def test_scenario_create_records_mixins(
    test_data_dir: Path, derive_scenario: Callable[..., Path],
    load: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    derive_scenario("mix_a", ROOT, {
        "scheduled.csv": _scheduled_override(salary_alice="9000.00"),
    })
    monkeypatch.setenv("ANALYSIS_DATA_DIR", str(test_data_dir))
    result = CliRunner().invoke(main, ["sim", "scenario", "create", "combo", ROOT, "mix_a"])
    assert result.exit_code == 0, result.output
    assert f"inherits: {ROOT} + mix_a" in result.output

    row = pd.read_csv(test_data_dir / "scenarios" / "combo" / "scenario.csv",
                      dtype=str, keep_default_na=False).iloc[0]
    assert row["parent_scenario"] == f"{ROOT}:mix_a"
    assert row["description"] == f"Derived from {ROOT} + mix_a"
    assert _amounts(load("combo"))["salary_alice"] == 900000


def test_scenario_create_rejects_an_unknown_mixin(
    test_data_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANALYSIS_DATA_DIR", str(test_data_dir))
    result = CliRunner().invoke(main, ["sim", "scenario", "create", "combo", ROOT, "nope"])
    assert result.exit_code != 0
    assert "scenario not found" in result.output
    assert not (test_data_dir / "scenarios" / "combo").exists()
