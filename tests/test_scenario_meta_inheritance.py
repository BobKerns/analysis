"""``scenario.meta_json`` inherits key by key down a scenario's lineage
(issue #74, ADR-003 §4.5), so a child of an ensemble recipe keeps the recipe's
``ensemble_size`` unless it sets its own -- as ``sim scenario create``'s 0
does, making the child a plain scenario (#169)."""

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


def _create_child_and_run(
    test_data_dir: Path, append_calendar_row: Callable[..., None],
    intent_dir: Path, runs_dir: Path, group: str,
) -> str:
    """Derive ``what_if`` with ``sim <group> create`` from a two-member recipe
    with one varying row, then ``sim run`` it; returns the run's output."""
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_rate_vary",
        series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="uniform",
        vary_low="4%", vary_high="5%",
    )
    runner = CliRunner()
    data = ["--data-dir", str(test_data_dir)]
    for args in (
        ["sim", "ensemble", "create", "recipe", ROOT, "--size", "2", *data],
        ["sim", group, "create", "what_if", "recipe", *data],
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
    return result.output


def test_sim_run_on_an_ensemble_child_of_a_recipe_uses_the_recipes_member_count(
    test_data_dir: Path, append_calendar_row: Callable[..., None],
    intent_dir: Path, runs_dir: Path,
) -> None:
    output = _create_child_and_run(
        test_data_dir, append_calendar_row, intent_dir, runs_dir, "ensemble",
    )
    assert "2/2 member(s) succeeded" in output


def test_sim_run_on_a_scenario_child_of_a_recipe_runs_its_base_case(
    test_data_dir: Path, append_calendar_row: Callable[..., None],
    intent_dir: Path, runs_dir: Path,
) -> None:
    """#169: ``sim scenario create`` never makes an ensemble, so its child of
    a recipe runs once, at member 0's central values, to an ordinary run file."""
    output = _create_child_and_run(
        test_data_dir, append_calendar_row, intent_dir, runs_dir, "scenario",
    )
    assert "what_if is not an ensemble: running its base case" in output
    run_files = list(runs_dir.glob("*_what_if_*.sqlite3"))
    assert len(run_files) == 1
    assert not (runs_dir / "what_if").exists()
    query = ("SELECT amount, vary_distribution FROM calendar_event "
             "WHERE name = 'checking_rate_vary'")
    with sqlite3.connect(intent_dir / "what_if.sqlite3") as con:
        (authored, vary) = con.execute(query).fetchone()
    assert vary == "uniform"  # the load's own intent keeps the varying row
    # Resolved to the uniform's midpoint, 4.5%: the authored amount.
    with sqlite3.connect(run_files[0]) as con:
        assert con.execute(query).fetchone() == (authored, None)


@pytest.mark.parametrize("reserve", ["250000", '"250000.00"'])
def test_a_reserve_in_whole_dollars_or_a_cents_string_loads(
    derive_scenario: Callable[..., Path], load_test_scenario: Callable[[str], Path],
    reserve: str,
) -> None:
    derive_scenario("cushioned", ROOT, meta_json=f'{{"reserve":{reserve}}}')
    assert "reserve" in _meta(load_test_scenario("cushioned"))


@pytest.mark.parametrize("reserve", ["250000.5", '"lots"', "true"])
def test_a_reserve_that_is_not_dollars_fails_the_load(
    derive_scenario: Callable[..., Path], test_data_dir: Path, tmp_path: Path, reserve: str,
) -> None:
    """#163: a float would leak binary fractions into money, so a JSON
    reserve is whole dollars or a dollar string, like a CSV dollar cell."""
    derive_scenario("bad", ROOT, meta_json=f'{{"reserve":{reserve}}}')
    with pytest.raises(LoadError) as ei:
        load_scenario(test_data_dir, "bad", intent_dir=tmp_path, allow_dirty_data=True)
    assert any(issue.startswith("bad: ") and "reserve" in issue for issue in ei.value.issues)
