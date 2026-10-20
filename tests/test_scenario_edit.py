"""`sim scenario edit|delete`, and the README.md `scenario create` writes (issue #190)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim import scenario as sc
from analysis.sim.lineage import read_parents
from analysis.sim.loader import load_scenario
from analysis.sim.readme import parse_readme_summary


def _sim(data_dir: Path, *args: str, input: str | None = None):
    return CliRunner().invoke(main, ["sim", *args, "--data-dir", str(data_dir)],
                              input=input, terminal_width=200)


def _row(folder: Path) -> dict[str, str]:
    with (folder / "scenario.csv").open(newline="", encoding="utf-8") as fh:
        return next(csv.DictReader(fh))


# ---------- README.md on create ----------

def test_create_writes_a_readme_listings_show(test_data_dir: Path, test_scenarios_dir: Path,
                                              intent_dir: Path) -> None:
    result = _sim(test_data_dir, "scenario", "create", "kid", "retire_alice", "base")
    assert result.exit_code == 0, result.output
    assert "next: edit" in result.output and "README.md" in result.output
    text = (test_scenarios_dir / "kid" / "README.md").read_text()
    summary = parse_readme_summary(text)
    assert summary.title == "kid"
    assert summary.paragraph == "Derived from `retire_alice` + `base`."
    assert "Builds on `retire_alice`, with mix-in `base`." in text
    assert "## Purpose" in text
    assert [s.description for s in sc.scenario_summaries(test_data_dir) if s.name == "kid"] == [
        "Derived from `retire_alice` + `base`."]
    load_scenario(test_data_dir, "kid", intent_dir=intent_dir, allow_dirty_data=True)


def test_create_readme_carries_a_given_description(test_child_scenario: Path) -> None:
    folder = sc.create_scenario(test_child_scenario.parent.parent, "other", "child",
                                description="What if Bill retires early?")
    assert parse_readme_summary((folder / "README.md").read_text()).paragraph == "What if Bill retires early?"


def test_ensemble_create_suggests_editing_the_readme(test_data_dir: Path, test_scenarios_dir: Path) -> None:
    result = _sim(test_data_dir, "ensemble", "create", "many", "retire_alice")
    assert result.exit_code == 0, result.output
    assert "next: edit" in result.output
    assert (test_scenarios_dir / "many" / "README.md").is_file()


# ---------- edit ----------

def test_edit_sets_and_clears_fields(test_data_dir: Path, test_child_scenario: Path) -> None:
    result = _sim(test_data_dir, "scenario", "edit", "child", "--start", "2027-01", "--seed", "7",
                  "--size", "40", "--reserve", "250000")
    assert result.exit_code == 0, result.output
    row = _row(test_child_scenario)
    assert (row["start"], row["seed"]) == ("2027-01", "7")
    assert json.loads(row["meta_json"]) == {"ensemble_size": 40, "reserve": 250000}
    assert "start: (inherited) -> 2027-01" in result.output
    assert "meta_json ensemble_size: (inherited) -> 40" in result.output
    assert "meta_json reserve: (inherited) -> 250000" in result.output

    result = _sim(test_data_dir, "scenario", "edit", "child", "--start", "", "--seed", "",
                  "--size", "", "--reserve", "")
    assert result.exit_code == 0, result.output
    row = _row(test_child_scenario)
    assert (row["start"], row["seed"], json.loads(row["meta_json"])) == ("", "", {})


def test_edit_new_seed_and_size_zero(test_data_dir: Path, test_child_scenario: Path) -> None:
    result = _sim(test_data_dir, "ensemble", "edit", "child", "--new-seed", "--size", "0")
    assert result.exit_code == 0, result.output
    row = _row(test_child_scenario)
    assert row["seed"].isdigit()
    assert json.loads(row["meta_json"]) == {"ensemble_size": 0}


def test_edit_reserve_keeps_cents_as_a_string(test_data_dir: Path, test_child_scenario: Path) -> None:
    result = _sim(test_data_dir, "scenario", "edit", "child", "--reserve", "1000.50")
    assert result.exit_code == 0, result.output
    assert json.loads(_row(test_child_scenario)["meta_json"])["reserve"] == "1000.50"


def test_edit_description_follows_into_a_generated_readme(test_data_dir: Path, test_child_scenario: Path) -> None:
    result = _sim(test_data_dir, "scenario", "edit", "child", "--description", "Bill works two more years")
    assert result.exit_code == 0, result.output
    assert "README.md: updated to match" in result.output
    readme = (test_child_scenario / "README.md").read_text()
    assert parse_readme_summary(readme).paragraph == "Bill works two more years"
    assert _row(test_child_scenario)["description"] == "Bill works two more years"


def test_edit_description_warns_about_a_hand_written_readme(test_data_dir: Path,
                                                            test_child_scenario: Path) -> None:
    hand = "# Child\n\nMy own words.\n"
    (test_child_scenario / "README.md").write_text(hand)
    result = _sim(test_data_dir, "scenario", "edit", "child", "--description", "New words")
    assert result.exit_code == 0, result.output
    assert "edited by hand" in result.output
    assert (test_child_scenario / "README.md").read_text() == hand


def test_edit_parents_moves_the_default_description_and_readme(
    test_data_dir: Path, test_scenarios_dir: Path, test_child_scenario: Path, derive_scenario,
) -> None:
    derive_scenario("mix", "base")
    result = _sim(test_data_dir, "scenario", "edit", "child", "--parents", "retire_alice:mix")
    assert result.exit_code == 0, result.output
    assert read_parents(test_child_scenario) == ["retire_alice", "mix"]
    assert _row(test_child_scenario)["description"] == "Derived from retire_alice + mix"
    readme = (test_child_scenario / "README.md").read_text()
    assert "Derived from `retire_alice` + `mix`." in readme
    assert "Builds on `retire_alice`, with mix-in `mix`." in readme


def test_edit_leaves_a_partly_hand_written_readme_alone(test_data_dir: Path, test_child_scenario: Path,
                                                        derive_scenario) -> None:
    derive_scenario("mix", "base")
    readme = test_child_scenario / "README.md"
    edited = readme.read_text().replace("Builds on `retire_alice`.", "Builds on Alice's plan.")
    readme.write_text(edited)
    result = _sim(test_data_dir, "scenario", "edit", "child", "--parents", "retire_alice:mix")
    assert result.exit_code == 0, result.output
    assert "edited by hand" in result.output
    assert readme.read_text() == edited


def test_root_parents_keep_the_description(test_child_scenario: Path) -> None:
    changes = sc.edit_scenario_row(test_child_scenario, {"parent_scenario": ""}, {})
    assert _row(test_child_scenario)["description"] == "Derived from retire_alice"
    assert [c.field for c in changes] == ["parent_scenario"]
    assert sc.follow_readme(test_child_scenario, {"description": "Derived from retire_alice",
                                                  "parent_scenario": "retire_alice"},
                            _row(test_child_scenario)) is True
    assert "Builds on no other scenario." in (test_child_scenario / "README.md").read_text()


def test_blank_description_restores_the_default(test_data_dir: Path, test_child_scenario: Path) -> None:
    assert _sim(test_data_dir, "scenario", "edit", "child", "--description", "Mine").exit_code == 0
    result = _sim(test_data_dir, "scenario", "edit", "child", "--description", "")
    assert result.exit_code == 0, result.output
    assert _row(test_child_scenario)["description"] == "Derived from retire_alice"
    assert parse_readme_summary((test_child_scenario / "README.md").read_text()).paragraph == (
        "Derived from `retire_alice`.")


def test_blank_description_on_a_root_scenario_is_refused(test_data_dir: Path, test_scenarios_dir: Path) -> None:
    result = _sim(test_data_dir, "scenario", "edit", "base", "--description", "")
    assert result.exit_code != 0 and "no default description" in result.output, result.output


def test_edit_writes_a_readme_a_scenario_lacks(test_data_dir: Path, test_scenarios_dir: Path,
                                               derive_scenario) -> None:
    folder = derive_scenario("old", "retire_alice", description="Made before #190")
    result = _sim(test_data_dir, "scenario", "edit", "old", "--seed", "3")
    assert result.exit_code == 0, result.output
    assert "README.md: written" in result.output
    summary = parse_readme_summary((folder / "README.md").read_text())
    assert (summary.title, summary.paragraph) == ("old", "Made before #190")


def _guided(data_dir: Path, *args: str, answers: list[str]):
    return _sim(data_dir, "scenario", "edit", *args, input="".join(f"{a}\n" for a in answers))


def test_guided_edit_changes_the_fields_named(test_data_dir: Path, test_child_scenario: Path) -> None:
    result = _guided(test_data_dir, "child", answers=["start", "2027-01", "seed", "new", "reserve", "1000", "write"])
    assert result.exit_code == 0, result.output
    row = _row(test_child_scenario)
    assert row["start"] == "2027-01" and row["seed"].isdigit()
    assert json.loads(row["meta_json"]) == {"reserve": 1000}
    assert "edited scenario child" in result.output


def test_guided_edit_asks_again_for_a_bad_answer(test_data_dir: Path, test_child_scenario: Path) -> None:
    result = _guided(test_data_dir, "child", answers=["size", "-3", "40", "write"])
    assert result.exit_code == 0, result.output
    assert "negative" in result.output
    assert json.loads(_row(test_child_scenario)["meta_json"]) == {"ensemble_size": 40}


def test_guided_edit_asks_for_the_scenario(test_data_dir: Path, test_child_scenario: Path) -> None:
    result = _sim(test_data_dir, "ensemble", "edit", input="nobody\nchild\nend\n2039-12\nwrite\n")
    assert result.exit_code == 0, result.output
    assert "not a scenario: 'nobody'" in result.output
    assert _row(test_child_scenario)["end"] == "2039-12"


def test_guided_edit_without_changes_or_cancelled_writes_nothing(test_data_dir: Path,
                                                                 test_child_scenario: Path) -> None:
    before = (test_child_scenario / "scenario.csv").read_text()
    assert "no change" in _guided(test_data_dir, "child", answers=["write"]).output
    assert "Cancelled" in _guided(test_data_dir, "child", answers=["seed", "5", "cancel"]).output
    assert (test_child_scenario / "scenario.csv").read_text() == before


def test_edit_readme_for_a_blank_description_uses_the_default(test_data_dir: Path, derive_scenario) -> None:
    folder = derive_scenario("old", "retire_alice")
    result = _sim(test_data_dir, "scenario", "edit", "old", "--seed", "3")
    assert result.exit_code == 0, result.output
    assert parse_readme_summary((folder / "README.md").read_text()).paragraph == "Derived from `retire_alice`."


def test_rename_follows_a_generated_readme(test_data_dir: Path, test_scenarios_dir: Path,
                                           test_child_scenario: Path) -> None:
    result = _sim(test_data_dir, "scenario", "rename", "child", "kid")
    assert result.exit_code == 0, result.output
    summary = parse_readme_summary((test_scenarios_dir / "kid" / "README.md").read_text())
    assert summary.title == "kid"
    assert [s.description for s in sc.scenario_summaries(test_data_dir) if s.name == "kid"] == [
        "Derived from `retire_alice`."]


@pytest.mark.parametrize(("args", "message"), [
    (("--start", "May 2027"), "date must be"),
    (("--seed", "x"), "not a whole number"),
    (("--seed", "1", "--new-seed"), "exclusive"),
    (("--size", "-1"), "negative"),
    (("--reserve", "1.5%"), "percent"),
    (("--parents", "nobody"), "scenario not found"),
    (("--parents", "child"), "can't build on itself"),
])
def test_edit_refuses(test_data_dir: Path, test_child_scenario: Path, args: tuple[str, ...], message: str) -> None:
    before = (test_child_scenario / "scenario.csv").read_text()
    result = _sim(test_data_dir, "scenario", "edit", "child", *args)
    assert result.exit_code != 0 and message in result.output, result.output
    assert (test_child_scenario / "scenario.csv").read_text() == before


def test_edit_refuses_a_cycle_through_a_descendant(test_data_dir: Path, test_child_scenario: Path,
                                                   test_grandchild_scenario: Path) -> None:
    before = (test_child_scenario / "scenario.csv").read_text()
    result = _sim(test_data_dir, "scenario", "edit", "child", "--parents", "grandchild")
    assert result.exit_code != 0 and "nothing was written" in result.output, result.output
    assert (test_child_scenario / "scenario.csv").read_text() == before


def test_edit_refuses_what_breaks_a_descendant(test_data_dir: Path, test_child_scenario: Path,
                                               test_grandchild_scenario: Path) -> None:
    # A root scenario must have its own start and end; child inherits them.
    result = _sim(test_data_dir, "scenario", "edit", "child", "--parents", "")
    assert result.exit_code != 0 and "nothing was written" in result.output, result.output
    assert "start is blank here and in every scenario it builds on" in result.output
    assert read_parents(test_child_scenario) == ["retire_alice"]


def test_edit_unknown_scenario(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "scenario", "edit", "../scenarios/base", "--seed", "1")
    assert result.exit_code != 0 and "scenario not found" in result.output


# ---------- delete ----------

def test_delete_removes_the_folder_and_clears_current(test_data_dir: Path, test_child_scenario: Path) -> None:
    (test_data_dir / ".current_scenario").write_text("child\n")
    result = _sim(test_data_dir, "scenario", "delete", "child", "--yes")
    assert result.exit_code == 0, result.output
    assert not test_child_scenario.exists()
    assert not (test_data_dir / ".current_scenario").exists()
    assert "No scenario is current now (was child)" in result.stderr


def test_delete_asks_first(test_data_dir: Path, test_child_scenario: Path) -> None:
    result = _sim(test_data_dir, "scenario", "delete", "child", input="n\n")
    assert result.exit_code != 0 and "nothing was deleted" in result.output
    assert test_child_scenario.is_dir()
    result = _sim(test_data_dir, "ensemble", "delete", "child", input="y\n")
    assert result.exit_code == 0, result.output
    assert not test_child_scenario.exists()


def test_delete_refuses_a_parent_or_mix_in(test_data_dir: Path, test_scenarios_dir: Path,
                                           test_child_scenario: Path, derive_scenario) -> None:
    derive_scenario("mixed", "base:child")
    result = _sim(test_data_dir, "scenario", "delete", "retire_alice", "--yes")
    assert result.exit_code != 0 and "parent or a mix-in of child" in result.output, result.output
    result = _sim(test_data_dir, "scenario", "delete", "child", "--yes")
    assert result.exit_code != 0 and "mixed" in result.output, result.output
    assert test_child_scenario.is_dir()


def test_delete_lists_markdown_still_mentioning_it(test_data_dir: Path, test_child_scenario: Path) -> None:
    (test_data_dir / "README.md").write_text("See [child](scenarios/child/README.md).\n")
    result = _sim(test_data_dir, "scenario", "delete", "child", "--yes")
    assert result.exit_code == 0, result.output
    assert "README.md still mentions it" in result.output


def test_delete_takes_a_scenario_that_fails_to_load(test_data_dir: Path, test_child_scenario: Path) -> None:
    (test_child_scenario / "accounts.csv").write_text("not,a,valid\nheader,at,all\n")
    result = _sim(test_data_dir, "scenario", "delete", "child", "--yes")
    assert result.exit_code == 0, result.output
    assert not test_child_scenario.exists()


def test_delete_unknown_scenario(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "scenario", "delete", "../scenarios/base", "--yes")
    assert result.exit_code != 0 and "scenario not found" in result.output
