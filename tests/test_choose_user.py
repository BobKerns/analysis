"""The data repo user prompt (``analysis.sim.choose_user``) and the template it runs on."""

from __future__ import annotations

import io
from pathlib import Path

from analysis.sim.choose_user import choose, match_person, primary_persons
from analysis.sim.loader import load_scenario

PERSONS = [("alice", "Alice (fake)"), ("bill", "Bill (fake)")]


def test_primary_persons_lists_only_primary_rows(prototype_data: Path) -> None:
    assert primary_persons(prototype_data) == PERSONS


def test_match_person_takes_an_id_or_a_display_name_in_any_case() -> None:
    assert match_person("bill\n", PERSONS) == "bill"
    assert match_person("  alice (FAKE) ", PERSONS) == "alice"
    assert match_person("joint", PERSONS) is None


def test_match_person_prefers_an_exact_id_over_a_display_name() -> None:
    assert match_person("Bob", [("bobby", "Bob"), ("Bob", "Robert")]) == "Bob"


def test_choose_reprompts_until_an_answer_matches(prototype_data: Path) -> None:
    stderr = io.StringIO()
    assert choose(prototype_data, io.StringIO("nobody\nBill (fake)\n"), stderr) == "bill"
    assert "'nobody' is not in the list." in stderr.getvalue()
    assert "Alice (fake)" in stderr.getvalue()


def test_choose_gives_up_at_end_of_input(prototype_data: Path) -> None:
    assert choose(prototype_data, io.StringIO("nobody\n"), io.StringIO()) is None


def test_template_logs_in_as_its_own_primary_person(repo_root: Path) -> None:
    templates = repo_root / "templates"
    user = (templates / ".analysis_user").read_text().strip()
    assert primary_persons(templates) == [(user, "Test Tester")]


def test_template_base_scenario_loads(repo_root: Path, intent_dir: Path) -> None:
    # scripts/link-data.sh --template copies templates/ as the data repo's sim/
    # dir, so it must load as is.
    result = load_scenario(
        repo_root / "templates", "base", intent_dir=intent_dir, compute_stamps=False,
    )
    assert result.scenario_name == "base"
