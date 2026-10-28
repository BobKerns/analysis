"""What a scenario builds on (issue #259): templates as parents, parents
asked one at a time, and mix-in only scenarios with no parent."""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim import scenario as sc
from analysis.sim.cli import _parents_cell
from analysis.sim.lineage import MIXIN_BASE, MIXIN_ONLY_DESCRIPTION, is_mixin_only, load_order, read_parents
from analysis.sim.loader import load_scenario
from analysis.sim.scenario_guide import walk_parents


def _sim(data_dir: Path, *args: str, input: str | None = None):
    return CliRunner().invoke(main, ["sim", *args, "--data-dir", str(data_dir)],
                              input=input, terminal_width=200)


def _row(folder: Path) -> dict[str, str]:
    with (folder / "scenario.csv").open(newline="", encoding="utf-8") as fh:
        return next(csv.DictReader(fh))


def _lines(*answers: str) -> str:
    return "".join(f"{a}\n" for a in answers)


# ---------- the walk ----------

CHOICES = ["base", "retire_alice", "t_a", "t_b", "t_c"]


def _walk(current: list[str], *answers: str, itself: str | None = "kid") -> tuple[list[str], list[str]]:
    """``walk_parents`` given ``answers`` in turn (Enter takes the default);
    returns the parents and what it echoed."""
    pending = list(answers)
    said: list[str] = []

    def ask(text: str, completions: list[str], default: str) -> str:
        said.append(text)
        answer = pending.pop(0)
        return answer or default

    result = walk_parents(current, CHOICES, ask, said.append, itself=itself)
    assert not pending, f"left unasked: {pending}"
    return result, said


def test_walk_keeps_everything_on_enter() -> None:
    assert _walk(["base", "t_a"], "", "", "")[0] == ["base", "t_a"]


def test_walk_removes_inserts_and_replaces() -> None:
    parents, said = _walk(["base", "t_a", "t_b"], "", "+t_c", "", "-", "")
    assert parents == ["base", "t_c", "t_a"]
    # After an insert the same entry is asked again, now one place later.
    assert [s.strip() for s in said if "mix-in" in s][:3] == ["mix-in 1", "mix-in 2", "mix-in 3"]
    assert _walk(["base", "t_a"], "retire_alice", "", "")[0] == ["retire_alice", "t_a"]


def test_walk_adds_mix_ins_at_the_end() -> None:
    assert _walk(["base"], "", "t_a", "+t_b", "")[0] == ["base", "t_a", "t_b"]


def test_removing_the_parent_makes_the_first_mix_in_the_parent() -> None:
    parents, said = _walk(["base", "t_a"], "-", "", "")
    assert parents == ["t_a"]
    assert said[1].strip() == "parent"


def test_walk_asks_again_for_a_bad_name() -> None:
    parents, said = _walk(["base"], "nobody", "kid", "", "base", "t_a", "")
    assert parents == ["base", "t_a"]
    assert "  not a scenario: 'nobody'" in said
    assert "  kid can't build on itself" in said
    assert "  base is already in the list" in said


def test_walk_may_end_with_nothing() -> None:
    assert _walk(["base"], "-", "")[0] == []
    assert _walk([], "", itself=None)[0] == []


# ---------- templates as parents ----------

def test_edit_parents_takes_a_template(test_data_dir: Path, test_child_scenario: Path,
                                       intent_dir: Path) -> None:
    result = _sim(test_data_dir, "scenario", "edit", "child", "--parents", "retire_alice:t_great_inflation")
    assert result.exit_code == 0, result.output
    assert read_parents(test_child_scenario) == ["retire_alice", "t_great_inflation"]
    load_scenario(test_data_dir, "child", intent_dir=intent_dir, allow_dirty_data=True)


def test_guided_edit_walks_the_parents(test_data_dir: Path, test_child_scenario: Path) -> None:
    result = _sim(test_data_dir, "scenario", "edit", "child",
                  input=_lines("parents", "", "t_great_inflation", "", "write"))
    assert result.exit_code == 0, result.output
    assert "+NAME inserts" in result.output
    assert read_parents(test_child_scenario) == ["retire_alice", "t_great_inflation"]


def test_guided_edit_confirms_removing_every_parent(test_data_dir: Path, test_child_scenario: Path) -> None:
    kept = _sim(test_data_dir, "scenario", "edit", "child", input=_lines("parents", "-", "", "n", "write"))
    assert kept.exit_code == 0, kept.output
    assert "can only be mixed into others" in kept.output and "parents unchanged" in kept.output
    assert read_parents(test_child_scenario) == ["retire_alice"]
    gone = _sim(test_data_dir, "scenario", "edit", "child", input=_lines("parents", "-", "", "y", "write"))
    assert gone.exit_code == 0, gone.output
    assert read_parents(test_child_scenario) == []
    assert is_mixin_only(test_child_scenario.parent, "child")


def test_create_asks_for_the_parents_one_at_a_time(test_data_dir: Path, test_scenarios_dir: Path) -> None:
    result = _sim(test_data_dir, "scenario", "create", "kid", input=_lines("retire_alice", "t_great_inflation", ""))
    assert result.exit_code == 0, result.output
    assert read_parents(test_scenarios_dir / "kid") == ["retire_alice", "t_great_inflation"]
    assert "inherits: retire_alice + t_great_inflation" in result.output


# ---------- no parent ----------

def test_create_no_parent_makes_a_mix_in_only_scenario(test_data_dir: Path, test_scenarios_dir: Path,
                                                       intent_dir: Path) -> None:
    result = _sim(test_data_dir, "scenario", "create", "extra", "--no-parent")
    assert result.exit_code == 0, result.output
    assert "inherits: nothing (a mix-in only scenario)" in result.output
    folder = test_scenarios_dir / "extra"
    row = _row(folder)
    assert (row["parent_scenario"], row["start"], row["end"], row["seed"]) == ("", "", "", "")
    assert row["description"] == MIXIN_ONLY_DESCRIPTION
    assert "Builds on no other scenario." in (folder / "README.md").read_text()
    assert is_mixin_only(test_scenarios_dir, "extra")
    # Checked by itself on the empty template; mixed in, it adds only itself.
    assert load_order(test_scenarios_dir, "extra") == [MIXIN_BASE, "extra"]
    assert _sim(test_data_dir, "check", "extra").exit_code == 0
    kid = _sim(test_data_dir, "scenario", "create", "kid", "retire_alice", "extra")
    assert kid.exit_code == 0, kid.output
    assert load_order(test_scenarios_dir, "kid")[-3:] == ["retire_alice", "extra", "kid"]
    loaded = load_scenario(test_data_dir, "kid", intent_dir=intent_dir, allow_dirty_data=True)
    assert loaded.warnings is not None  # it loads, with retire_alice's span


def test_a_mix_in_only_scenario_does_not_run(test_data_dir: Path) -> None:
    assert _sim(test_data_dir, "scenario", "create", "extra", "--no-parent").exit_code == 0
    assert _sim(test_data_dir, "scenario", "create", "kid", "retire_alice", "extra").exit_code == 0
    result = _sim(test_data_dir, "run", "extra")
    assert result.exit_code != 0
    assert "mix-in only" in result.output and "run kid" in result.output


def test_a_mix_in_only_intent_does_not_run_without_the_data_repo(test_data_dir: Path, intent_dir: Path,
                                                                runs_dir: Path) -> None:
    assert _sim(test_data_dir, "scenario", "create", "extra", "--no-parent").exit_code == 0
    loaded = _sim(test_data_dir, "load", "extra", "--intent-dir", str(intent_dir),
                  "--allow-dirty-data", "--no-run")
    assert loaded.exit_code == 0, loaded.output
    result = CliRunner().invoke(
        main, ["sim", "run", "extra", "--intent-dir", str(intent_dir), "--runs-dir", str(runs_dir),
               "--allow-dirty-data"],
        env={"ANALYSIS_DATA_DIR": None},
    )
    assert result.exit_code != 0
    assert "mix-in only" in result.output, result.output


def test_list_marks_a_mix_in_only_scenario(test_data_dir: Path) -> None:
    assert _sim(test_data_dir, "scenario", "create", "extra", "--no-parent").exit_code == 0
    [summary] = [s for s in sc.scenario_summaries(test_data_dir) if s.name == "extra"]
    assert summary.mixin_only and summary.start is None and summary.seed is None
    result = _sim(test_data_dir, "scenario", "list", "-v")
    assert result.exit_code == 0, result.output
    assert "(mix-" in result.output  # rich cuts the cell to fit


def test_dates_make_it_an_ordinary_root(test_data_dir: Path, test_scenarios_dir: Path) -> None:
    assert _sim(test_data_dir, "scenario", "create", "extra", "--no-parent").exit_code == 0
    result = _sim(test_data_dir, "scenario", "edit", "extra", "--start", "2027-01", "--end", "2040-12")
    assert result.exit_code == 0, result.output
    assert not is_mixin_only(test_scenarios_dir, "extra")
    assert load_order(test_scenarios_dir, "extra") == ["extra"]


def test_no_parent_with_a_parent_is_refused(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "scenario", "create", "extra", "retire_alice", "--no-parent")
    assert result.exit_code != 0 and "--no-parent" in result.output, result.output


def test_a_blank_parent_must_be_confirmed(test_data_dir: Path, test_scenarios_dir: Path) -> None:
    result = _sim(test_data_dir, "scenario", "create", "kid", input=_lines("", "n", "retire_alice", ""))
    assert result.exit_code == 0, result.output
    assert "Create it with no parent?" in result.output
    assert read_parents(test_scenarios_dir / "kid") == ["retire_alice"]
    result = _sim(test_data_dir, "scenario", "create", "extra", input=_lines("", "y"))
    assert result.exit_code == 0, result.output
    assert is_mixin_only(test_scenarios_dir, "extra")


def test_ensemble_create_no_parent(test_data_dir: Path, test_scenarios_dir: Path) -> None:
    result = _sim(test_data_dir, "ensemble", "create", "many", "--no-parent", "--size", "5")
    assert result.exit_code == 0, result.output
    assert is_mixin_only(test_scenarios_dir, "many")
    assert '"ensemble_size":5' in _row(test_scenarios_dir / "many")["meta_json"]


@pytest.mark.parametrize("parent", ["retire_alice", None])
def test_create_scenario_api(test_data_dir: Path, parent: str | None) -> None:
    folder = sc.create_scenario(test_data_dir, "api", parent)
    assert read_parents(folder) == ([parent] if parent else [])


def test_mix_ins_without_a_parent_are_refused(test_data_dir: Path) -> None:
    from analysis.sim.loader import LoadError
    with pytest.raises(LoadError, match="need a parent"):
        sc.create_scenario(test_data_dir, "api", None, mixins=["base"])


# ---------- a mix-in built on mix-ins (#324) ----------

def _mixin_on_mixin(test_data_dir: Path) -> None:
    assert _sim(test_data_dir, "scenario", "create", "extra", "--no-parent").exit_code == 0
    result = _sim(test_data_dir, "scenario", "create", "more", "extra")
    assert result.exit_code == 0, result.output
    assert "a mix-in only scenario" in result.output


def test_a_mix_in_built_on_a_mix_in_is_mix_in_only(test_data_dir: Path, test_scenarios_dir: Path) -> None:
    _mixin_on_mixin(test_data_dir)
    assert is_mixin_only(test_scenarios_dir, "more")
    # Checked by itself on the empty template; mixed in, it adds its own line.
    assert load_order(test_scenarios_dir, "more") == [MIXIN_BASE, "extra", "more"]
    assert _sim(test_data_dir, "check", "more").exit_code == 0
    assert _sim(test_data_dir, "scenario", "create", "kid", "retire_alice", "more").exit_code == 0
    assert load_order(test_scenarios_dir, "kid")[-4:] == ["retire_alice", "extra", "more", "kid"]


def test_a_mix_in_built_on_a_mix_in_does_not_run(test_data_dir: Path) -> None:
    _mixin_on_mixin(test_data_dir)
    result = _sim(test_data_dir, "run", "more")
    assert result.exit_code != 0
    assert "mix-in only" in result.output, result.output


def test_a_mix_in_on_a_mix_in_intent_does_not_run_without_the_data_repo(
        test_data_dir: Path, intent_dir: Path, runs_dir: Path) -> None:
    _mixin_on_mixin(test_data_dir)
    loaded = _sim(test_data_dir, "load", "more", "--intent-dir", str(intent_dir),
                  "--allow-dirty-data", "--no-run")
    assert loaded.exit_code == 0, loaded.output
    result = CliRunner().invoke(
        main, ["sim", "run", "more", "--intent-dir", str(intent_dir), "--runs-dir", str(runs_dir),
               "--allow-dirty-data"],
        env={"ANALYSIS_DATA_DIR": None},
    )
    assert result.exit_code != 0
    assert "mix-in only" in result.output, result.output


def test_list_shows_a_mix_in_on_a_mix_in_with_its_parents_and_own_values(test_data_dir: Path) -> None:
    _mixin_on_mixin(test_data_dir)
    [summary] = [s for s in sc.scenario_summaries(test_data_dir) if s.name == "more"]
    # Not t_empty's dates and seed, which it is only checked on.
    assert summary.mixin_only and summary.parents == ["extra"]
    assert summary.start is None and summary.end is None and summary.seed is None
    assert _parents_cell(summary) == "extra (mix-in only)"
    result = _sim(test_data_dir, "scenario", "list", "-v")
    assert result.exit_code == 0, result.output
    assert "extra" in result.output and "(mix-" in result.output  # rich cuts the cell to fit


def test_a_mix_in_on_a_template_is_not_mix_in_only(test_data_dir: Path, test_scenarios_dir: Path) -> None:
    # A template sits on t_empty's dates, so this one has a span and runs.
    assert _sim(test_data_dir, "scenario", "create", "extra", "--no-parent").exit_code == 0
    assert _sim(test_data_dir, "scenario", "create", "kid", "t_great_inflation", "extra").exit_code == 0
    assert not is_mixin_only(test_scenarios_dir, "kid")


def test_one_date_in_the_lineage_is_still_a_load_error(test_data_dir: Path) -> None:
    _mixin_on_mixin(test_data_dir)
    result = _sim(test_data_dir, "scenario", "edit", "more", "--start", "2027-01")
    assert result.exit_code != 0
    assert "end is blank here and in every scenario it builds on" in result.output, result.output


def test_edit_may_make_a_dated_scenario_mix_in_only(test_data_dir: Path, test_scenarios_dir: Path) -> None:
    # Taking a template out from under a mix-in, say, leaving only mix-ins.
    _mixin_on_mixin(test_data_dir)
    assert _sim(test_data_dir, "scenario", "create", "kid", "t_empty", "extra").exit_code == 0
    assert not is_mixin_only(test_scenarios_dir, "kid")
    result = _sim(test_data_dir, "scenario", "edit", "kid", "--parents", "extra")
    assert result.exit_code == 0, result.output
    assert "now mix-in only" in result.output and "kid" in result.output, result.output
    assert is_mixin_only(test_scenarios_dir, "kid")


def test_a_missing_parent_is_not_a_mix_in(test_data_dir: Path, test_scenarios_dir: Path) -> None:
    # A typo in a parent's name is the loader's to report, not a mix-in.
    _mixin_on_mixin(test_data_dir)
    assert _sim(test_data_dir, "scenario", "create", "kid", "extra").exit_code == 0
    csv_path = test_scenarios_dir / "kid" / "scenario.csv"
    csv_path.write_text(csv_path.read_text().replace("extra", "nosuch"), encoding="utf-8")
    assert not is_mixin_only(test_scenarios_dir, "kid")


def test_a_pre_schema_50_root_mix_in_intent_still_does_not_run(test_data_dir: Path, intent_dir: Path,
                                                               runs_dir: Path) -> None:
    assert _sim(test_data_dir, "scenario", "create", "extra", "--no-parent").exit_code == 0
    assert _sim(test_data_dir, "load", "extra", "--intent-dir", str(intent_dir),
                "--allow-dirty-data", "--no-run").exit_code == 0
    con = sqlite3.connect(intent_dir / "extra.sqlite3")
    con.execute("ALTER TABLE scenario DROP COLUMN mixin_only")
    con.commit()
    con.close()
    result = CliRunner().invoke(
        main, ["sim", "run", "extra", "--intent-dir", str(intent_dir), "--runs-dir", str(runs_dir),
               "--allow-dirty-data"],
        env={"ANALYSIS_DATA_DIR": None},
    )
    assert result.exit_code != 0
    assert "mix-in only" in result.output, result.output
