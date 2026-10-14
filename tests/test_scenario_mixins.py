"""Multiple inheritance of scenarios (issue #59, ADR-003 §4.5): a
``parent_scenario`` naming a main parent and colon-separated mix-ins, loaded
in ``lineage.load_order``."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim.connection import open_scenario
from analysis.sim.lineage import ScenarioCycleError, load_order, parse_parents
from analysis.sim.loader import LoadError, load_scenario
from analysis.sim.provenance import data_stamps
from analysis.sim.readme import description_view, lineage_markdown
from analysis.sim.staging import _affected_scenarios

# The scenario the tests derive from, and its own load order: it inherits
# the prototype's root, `base` (#91).
PARENT = "retire_alice"
PARENT_LINE = ["base", PARENT]


def _amounts(intent_path: Path) -> dict[str, int]:
    with sqlite3.connect(intent_path) as con:
        return dict(con.execute("SELECT name, amount_cents FROM scheduled_transaction"))


# -- load order -------------------------------------------------------------


def test_parse_parents_splits_on_colons_and_drops_blanks() -> None:
    assert parse_parents("") == []
    assert parse_parents(None) == []
    assert parse_parents("base") == ["base"]
    assert parse_parents(" base : mix_a::mix_b ") == ["base", "mix_a", "mix_b"]


def test_single_parent_order_is_the_ancestor_chain(
    test_scenarios_dir: Path, derive_scenario: Callable[..., Path],
) -> None:
    derive_scenario("child", PARENT)
    derive_scenario("grandchild", "child")
    assert load_order(test_scenarios_dir, "grandchild") == [*PARENT_LINE, "child", "grandchild"]


def test_a_mixin_stops_at_the_first_ancestor_already_loaded(
    test_scenarios_dir: Path, derive_scenario: Callable[..., Path],
) -> None:
    derive_scenario("main", PARENT)
    derive_scenario("mid", PARENT)
    derive_scenario("mix", "mid")
    derive_scenario("other_mix", PARENT)
    derive_scenario("combo", "main:mix:other_mix")
    assert load_order(test_scenarios_dir, "combo") == [
        *PARENT_LINE, "main", "mid", "mix", "other_mix", "combo",
    ]


def test_a_mixin_already_in_the_main_line_adds_nothing(
    test_scenarios_dir: Path, derive_scenario: Callable[..., Path],
) -> None:
    derive_scenario("main", PARENT)
    derive_scenario("combo", f"main:{PARENT}:main")
    assert load_order(test_scenarios_dir, "combo") == [*PARENT_LINE, "main", "combo"]


def test_a_cycle_through_a_mixin_is_rejected_strictly_and_tolerated_otherwise(
    test_scenarios_dir: Path, derive_scenario: Callable[..., Path],
) -> None:
    derive_scenario("a", f"{PARENT}:b")
    derive_scenario("b", f"{PARENT}:a")
    with pytest.raises(ScenarioCycleError, match="a -> b -> a"):
        load_order(test_scenarios_dir, "a", strict=True)
    assert load_order(test_scenarios_dir, "a") == [*PARENT_LINE, "b", "a"]


# -- loading ----------------------------------------------------------------


def test_mixins_contribute_their_rows_and_the_later_one_wins(
    scheduled_override: Callable[..., str],
    derive_scenario: Callable[..., Path], load_test_scenario: Callable[[str], Path],
) -> None:
    derive_scenario("mix_a", PARENT, {
        "scheduled.csv": scheduled_override(salary_alice="9000.00"),
    })
    derive_scenario("mix_b", PARENT, {
        "scheduled.csv": scheduled_override(salary_alice="9500.00", salary_bill="7000.00"),
    })
    derive_scenario("combo", f"{PARENT}:mix_a:mix_b")
    derive_scenario("combo_ab", f"{PARENT}:mix_b:mix_a")

    base = _amounts(load_test_scenario(PARENT))
    combo = _amounts(load_test_scenario("combo"))
    assert combo["salary_alice"] == 950000
    assert combo["salary_bill"] == 700000
    assert set(combo) == set(base)
    assert _amounts(load_test_scenario("combo_ab"))["salary_alice"] == 900000


def test_the_scenarios_own_rows_beat_its_mixins(
    scheduled_override: Callable[..., str],
    derive_scenario: Callable[..., Path], load_test_scenario: Callable[[str], Path],
) -> None:
    derive_scenario("mix_a", PARENT, {
        "scheduled.csv": scheduled_override(salary_alice="9000.00"),
    })
    derive_scenario("combo", f"{PARENT}:mix_a", {
        "scheduled.csv": scheduled_override(salary_alice="8500.00"),
    })
    assert _amounts(load_test_scenario("combo"))["salary_alice"] == 850000


def test_a_mixins_series_and_chain_overrides_are_inherited(
    derive_scenario: Callable[..., Path], load_test_scenario: Callable[[str], Path],
) -> None:
    derive_scenario("mix_rates", PARENT, {
        "series/rate__Asset_Checking.csv": "effective_date,annual_rate\n2026-01-31,1%\n",
        "chains/source/asset_checking_liquidity.csv": (
            "account_name,lot,maximum,percent,series,cadence\n"
            "Asset_TraditionalIRA_Alice,default,,,,\n"
        ),
    })
    derive_scenario("combo", f"{PARENT}:mix_rates")
    with sqlite3.connect(load_test_scenario("combo")) as con:
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
    derive_scenario("a", f"{PARENT}:b")
    derive_scenario("b", f"{PARENT}:a")
    with pytest.raises(LoadError, match="scenario inheritance cycle"):
        load_scenario(test_data_dir, "a", intent_dir=tmp_path, allow_dirty_data=True)


def test_a_missing_mixin_fails_the_load(
    derive_scenario: Callable[..., Path], test_data_dir: Path, tmp_path: Path,
) -> None:
    derive_scenario("combo", f"{PARENT}:nowhere")
    with pytest.raises(LoadError, match="scenario folder not found"):
        load_scenario(test_data_dir, "combo", intent_dir=tmp_path, allow_dirty_data=True)


# -- what a scenario says it inherits ----------------------------------------


def test_the_intent_records_parents_and_load_order(
    derive_scenario: Callable[..., Path], load_test_scenario: Callable[[str], Path],
) -> None:
    derive_scenario("main", PARENT)
    derive_scenario("mix", PARENT)
    derive_scenario("combo", "main:mix")
    with sqlite3.connect(load_test_scenario("combo")) as con:
        parents, order = con.execute(
            "SELECT parents_json, load_order_json FROM scenario"
        ).fetchone()
    assert json.loads(parents) == ["main", "mix"]
    assert json.loads(order) == [*PARENT_LINE, "main", "mix", "combo"]


def test_description_view_shows_parents_mixins_and_load_order(
    derive_scenario: Callable[..., Path], test_data_dir: Path,
) -> None:
    derive_scenario("main", PARENT)
    derive_scenario("mix", PARENT)
    derive_scenario("combo", "main:mix")
    with open_scenario(test_data_dir, "combo") as scenario:
        md = description_view(scenario).data
    assert isinstance(md, str)
    assert md.endswith(
        "**Parent:** `main`; **mix-ins:** `mix`  \n"
        f"**Load order:** `base` → `{PARENT}` → `main` → `mix` → `combo`"
    )


def test_a_single_parent_shows_no_mixins() -> None:
    row = {"parents_json": '["base"]', "load_order_json": '["base", "child"]'}
    assert lineage_markdown(row) == (
        "**Parent:** `base`  \n**Load order:** `base` → `child`"
    )


def test_a_file_from_before_lineage_was_recorded_shows_none() -> None:
    assert lineage_markdown({"description": "old"}) == ""


def test_a_mixins_set_rate_warns_alone_and_is_read_by_its_child(
    prototype_data: Path, derive_scenario: Callable[..., Path],
    test_data_dir: Path, intent_dir: Path,
) -> None:
    """A mix-in can set a series only its children read (#92): loaded on its
    own the row has no reader, which is a warning, not a load error."""
    calendar = pd.read_csv(prototype_data / "scenarios" / PARENT / "calendar.csv",
                           dtype=str, keep_default_na=False).iloc[:0]
    calendar.loc[0] = {**dict.fromkeys(calendar.columns, ""),
                       "date": "2027-01-31", "kind": "set_rate", "series": "housing",
                       "amount": "3.5%", "params_json": '{"aux_kind": "inflation"}',
                       "name": "housing_rate"}
    scheduled = pd.read_csv(prototype_data / "scenarios" / PARENT / "scheduled.csv",
                            dtype=str, keep_default_na=False)
    living = scheduled[scheduled["name"] == "living"].assign(series="housing")
    derive_scenario("mix_housing", PARENT, {"calendar.csv": calendar.to_csv(index=False)})
    derive_scenario("combo", f"{PARENT}:mix_housing",
                    {"scheduled.csv": living.to_csv(index=False)})

    def _warnings(name: str) -> list[str]:
        return load_scenario(test_data_dir, name, intent_dir=intent_dir / name,
                             allow_dirty_data=True).warnings

    assert any("'housing'" in w and "is never read" in w for w in _warnings("mix_housing"))
    assert not any("'housing'" in w for w in _warnings("combo"))


# -- provenance and staging -------------------------------------------------


@pytest.fixture
def mixin_scenarios(derive_scenario: Callable[..., Path]) -> None:
    """``combo`` = ``retire_alice`` + a ``mix`` mix-in, plus an ``unrelated`` sibling."""
    derive_scenario("mix", PARENT)
    derive_scenario("combo", f"{PARENT}:mix")
    derive_scenario("unrelated", PARENT)


def test_a_mixin_edit_moves_the_combos_tree_id(
    mixin_scenarios: None, commit_test_data: Callable[[], Path],
    scheduled_override: Callable[..., str],
) -> None:
    committed_combo = commit_test_data()
    before = data_stamps(committed_combo, "combo")
    assert not before.dirty
    (committed_combo / "scenarios" / "mix" / "scheduled.csv").write_text(
        scheduled_override(salary_alice="9000.00"), encoding="utf-8",
    )
    after = data_stamps(committed_combo, "combo")
    assert after.dirty
    assert after.tree_id != before.tree_id
    assert not data_stamps(committed_combo, "unrelated").dirty


def test_editing_a_mixin_marks_scenarios_using_it_affected(
    mixin_scenarios: None, test_data_dir: Path,
) -> None:
    assert _affected_scenarios(test_data_dir, {"mix"}) == ["combo", "mix"]


# -- scenario create ----------------------------------------------------------


@pytest.mark.uses_current_scenario_write
def test_scenario_create_records_mixins(
    scheduled_override: Callable[..., str],
    test_data_dir: Path, derive_scenario: Callable[..., Path],
    load_test_scenario: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    derive_scenario("mix_a", PARENT, {
        "scheduled.csv": scheduled_override(salary_alice="9000.00"),
    })
    monkeypatch.setenv("ANALYSIS_DATA_DIR", str(test_data_dir))
    result = CliRunner().invoke(main, ["sim", "scenario", "create", "combo", PARENT, "mix_a"])
    assert result.exit_code == 0, result.output
    assert f"inherits: {PARENT} + mix_a" in result.output

    row = pd.read_csv(test_data_dir / "scenarios" / "combo" / "scenario.csv",
                      dtype=str, keep_default_na=False).iloc[0]
    assert row["parent_scenario"] == f"{PARENT}:mix_a"
    assert row["description"] == f"Derived from {PARENT} + mix_a"
    assert _amounts(load_test_scenario("combo"))["salary_alice"] == 900000


def test_scenario_create_rejects_an_unknown_mixin(
    test_data_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANALYSIS_DATA_DIR", str(test_data_dir))
    result = CliRunner().invoke(main, ["sim", "scenario", "create", "combo", PARENT, "nope"])
    assert result.exit_code != 0
    assert "scenario not found" in result.output
    assert not (test_data_dir / "scenarios" / "combo").exists()
