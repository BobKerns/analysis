"""``sim ensemble create`` takes mix-ins like ``sim scenario create`` (#168),
and under an ensemble parent inherits its seed and member count rather than
drawing new ones (#169); ``sim scenario create`` sets no member count, so
it inherits its parents' (#369)."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim import registries
from analysis.sim.scenario import ScenarioValue, ensemble_settings


def _create(data_dir: Path, group: str, *args: str) -> str:
    result = CliRunner().invoke(
        main, ["sim", group, "create", *args, "--data-dir", str(data_dir)],
    )
    assert result.exit_code == 0, result.output
    return result.output


def _own(data_dir: Path, name: str) -> tuple[str, dict[str, object]]:
    """``name``'s own scenario.csv ``seed`` and ``meta_json``."""
    df = pd.read_csv(data_dir / "scenarios" / name / "scenario.csv",
                     dtype=str, keep_default_na=False)
    return df.loc[0, "seed"], json.loads(df.loc[0, "meta_json"])


def test_ensemble_create_applies_mixins_after_the_parent(test_data_dir: Path) -> None:
    _create(test_data_dir, "scenario", "mix", "base")
    output = _create(test_data_dir, "ensemble", "recipe", "retire_alice", "mix", "--seed", "3")
    assert "inherits: retire_alice + mix" in output
    df = pd.read_csv(test_data_dir / "scenarios" / "recipe" / "scenario.csv",
                     dtype=str, keep_default_na=False)
    assert df.loc[0, "parent_scenario"] == "retire_alice:mix"
    assert df.loc[0, "description"] == "Derived from retire_alice + mix"


def test_ensemble_create_from_a_plain_scenario_gets_its_own_seed_and_size(
    test_data_dir: Path,
) -> None:
    # retire_alice has a seed (42) but no ensemble_size: not an ensemble.
    _create(test_data_dir, "ensemble", "recipe", "retire_alice")
    seed, meta = _own(test_data_dir, "recipe")
    assert seed
    assert meta == {"ensemble_size": registries.DEFAULT_ENSEMBLE_SIZE}


def test_ensemble_create_from_an_ensemble_inherits_its_seed_and_size(
    test_data_dir: Path,
) -> None:
    _create(test_data_dir, "ensemble", "recipe", "retire_alice", "--seed", "7", "--size", "5")
    output = _create(test_data_dir, "ensemble", "variant", "recipe")
    assert "seed: 7 (inherited)" in output
    assert "ensemble_size: 5 (inherited)" in output
    # Inherited by leaving them out, so a later change to the parent carries.
    assert _own(test_data_dir, "variant") == ("", {})
    assert ensemble_settings(test_data_dir, "variant") == (
        ScenarioValue(7, inherited=True), ScenarioValue(5, inherited=True),
    )


def test_an_ensemble_mixin_is_enough_to_inherit(test_data_dir: Path) -> None:
    _create(test_data_dir, "ensemble", "recipe", "base", "--seed", "7", "--size", "5")
    _create(test_data_dir, "ensemble", "variant", "retire_alice", "recipe")
    assert ensemble_settings(test_data_dir, "variant") == (
        ScenarioValue(7, inherited=True), ScenarioValue(5, inherited=True),
    )


def test_ensemble_create_overrides_an_inherited_seed_or_size(test_data_dir: Path) -> None:
    _create(test_data_dir, "ensemble", "recipe", "retire_alice", "--seed", "7", "--size", "5")
    _create(test_data_dir, "ensemble", "bigger", "recipe", "--size", "9")
    assert _own(test_data_dir, "bigger") == ("", {"ensemble_size": 9})
    _create(test_data_dir, "ensemble", "fresh", "recipe", "--new-seed")
    seed, meta = _own(test_data_dir, "fresh")
    assert seed and meta == {}
    _create(test_data_dir, "ensemble", "chosen", "recipe", "--seed", "11")
    assert _own(test_data_dir, "chosen") == ("11", {})


def test_seed_and_new_seed_are_exclusive(test_data_dir: Path) -> None:
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "create", "recipe", "retire_alice",
        "--seed", "7", "--new-seed", "--data-dir", str(test_data_dir),
    ])
    assert result.exit_code != 0
    assert "exclusive" in result.output
    assert not (test_data_dir / "scenarios" / "recipe").exists()


def test_scenario_create_inherits_its_parents_ensemble(test_data_dir: Path) -> None:
    _create(test_data_dir, "ensemble", "recipe", "retire_alice", "--seed", "7", "--size", "5")
    _create(test_data_dir, "scenario", "what_if", "recipe")
    assert _own(test_data_dir, "what_if") == ("", {})
    assert ensemble_settings(test_data_dir, "what_if") == (
        ScenarioValue(7, inherited=True), ScenarioValue(5, inherited=True))


def test_a_plain_mixin_keeps_an_ensemble_in_either_order(test_data_dir: Path) -> None:
    """#369: a what-if made by ``sim scenario create`` and mixed into an
    ensemble, after it or before it, leaves an ensemble that pairs with it."""
    _create(test_data_dir, "ensemble", "recipe", "retire_alice", "--seed", "7", "--size", "5")
    _create(test_data_dir, "scenario", "what_if", "retire_alice")
    for name, parents in (("after", ("recipe", "what_if")), ("before", ("what_if", "recipe"))):
        _create(test_data_dir, "ensemble", name, *parents)
        assert _own(test_data_dir, name) == ("", {})
        assert ensemble_settings(test_data_dir, name) == (
            ScenarioValue(7, inherited=True), ScenarioValue(5, inherited=True))
