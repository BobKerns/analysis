"""``scenario.meta_json`` inherits key by key down a scenario's lineage
(issue #74, ADR-003 §4.5), so a child of an ensemble recipe keeps the recipe's
``ensemble_size`` even though ``sim scenario create`` writes its own ``{}``."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim.loader import LoadError, load_scenario

ROOT = "retire_alice"


def _meta(intent_path: Path) -> dict[str, object]:
    with sqlite3.connect(intent_path) as con:
        (meta_json,) = con.execute("SELECT meta_json FROM scenario").fetchone()
    return json.loads(meta_json)


def test_an_empty_child_meta_json_inherits_the_parents_keys(
    derive_scenario: Callable[..., Path], load_test_scenario: Callable[[str], Path],
) -> None:
    derive_scenario("recipe", ROOT, meta_json='{"ensemble_size":7,"note":"x"}')
    derive_scenario("what_if", "recipe", meta_json="{}")
    assert _meta(load_test_scenario("what_if")) == {"ensemble_size": 7, "note": "x"}


def test_the_child_key_wins_and_the_rest_are_kept(
    derive_scenario: Callable[..., Path], load_test_scenario: Callable[[str], Path],
) -> None:
    derive_scenario("recipe", ROOT, meta_json='{"ensemble_size":7,"note":"x"}')
    derive_scenario("bigger", "recipe", meta_json='{"ensemble_size":50,"extra":true}')
    assert _meta(load_test_scenario("bigger")) == {
        "ensemble_size": 50, "note": "x", "extra": True,
    }


def test_mixins_merge_their_keys_in_load_order(
    derive_scenario: Callable[..., Path], load_test_scenario: Callable[[str], Path],
) -> None:
    derive_scenario("mix_a", ROOT, meta_json='{"ensemble_size":10,"a":1}')
    derive_scenario("mix_b", ROOT, meta_json='{"ensemble_size":20,"b":2}')
    derive_scenario("combo", f"{ROOT}:mix_a:mix_b")
    assert _meta(load_test_scenario("combo")) == {"ensemble_size": 20, "a": 1, "b": 2}


@pytest.mark.parametrize(
    ("meta_json", "message"),
    [("[1, 2]", "must be a JSON object"), ("{oops", "is not valid JSON")],
)
def test_a_meta_json_that_is_not_an_object_fails_the_load(
    derive_scenario: Callable[..., Path], test_data_dir: Path, tmp_path: Path,
    meta_json: str, message: str,
) -> None:
    derive_scenario("bad", ROOT, meta_json=meta_json)
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "bad", intent_dir=tmp_path, allow_dirty_data=True)
    assert any(issue.startswith("bad: ") and message in issue for issue in ei.value.issues)


def test_sim_run_on_a_child_of_a_recipe_uses_the_recipes_member_count(
    test_data_dir: Path, append_calendar_row: Callable[..., None],
    intent_dir: Path, runs_dir: Path,
) -> None:
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="45000",
        vary_target="amount", vary_distribution="uniform",
        vary_low="40000", vary_high="50000",
    )
    runner = CliRunner()
    data = ["--data-dir", str(test_data_dir)]
    for args in (
        ["sim", "ensemble", "create", "recipe", ROOT, "--size", "2", *data],
        ["sim", "scenario", "create", "what_if", "recipe", *data],
        ["sim", "load", "what_if", *data, "--intent-dir", str(intent_dir),
         "--allow-dirty-data", "--no-run"],
    ):
        result = runner.invoke(main, args)
        assert result.exit_code == 0, result.output

    result = runner.invoke(
        main,
        [
            "sim", "run", "what_if",
            "--intent-dir", str(intent_dir), "--runs-dir", str(runs_dir),
            "--allow-dirty-data",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "2/2 member(s) succeeded" in result.output
