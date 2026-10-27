"""``sim person add|edit`` and ``sim person lifespan set|clear`` (#192)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.sim.staging import EditRejected, staged_edit


@pytest.fixture
def sim(test_data_dir: Path) -> Callable[..., Result]:
    """Run a ``sim`` command against the private data copy; ``input`` answers
    its prompts."""
    def run(*args: str, input: str | None = None) -> Result:  # noqa: A002 -- CliRunner's name
        return CliRunner().invoke(
            main, ["sim", *args, "--data-dir", str(test_data_dir)], input=input,
        )
    return run


def _persons(data_dir: Path) -> dict[str, dict[str, str]]:
    frame = pd.read_csv(data_dir / "persons.csv", dtype=str, keep_default_na=False)
    return {row["id"]: row for row in frame.to_dict("records")}


def _lifespans(scenario_dir: Path) -> dict[str, str]:
    path = scenario_dir / "lifespan.csv"
    if not path.exists():
        return {}
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    return dict(zip(frame["person"], frame["age_at_death"], strict=True))


# ---------- person add ----------


def test_person_add_with_options_writes_the_row(
    sim: Callable[..., Result], test_data_dir: Path,
) -> None:
    result = sim("person", "add", "carol", "--display", "Carol (fake)",
                 "--birthdate", "1995-02-03", "--role", "secondary")

    assert result.exit_code == 0, result.output
    assert "every scenario" in result.output
    assert _persons(test_data_dir)["carol"] == {
        "id": "carol", "display": "Carol (fake)", "birthdate": "1995-02-03", "sex": "",
        "role": "secondary", "meta_json": "{}",
    }


def test_person_add_asks_for_what_is_missing(
    sim: Callable[..., Result], test_data_dir: Path,
) -> None:
    result = sim("person", "add", "carol", input="Carol (fake)\n1995-02-03\nf\n\n\n")

    assert result.exit_code == 0, result.output
    carol = _persons(test_data_dir)["carol"]
    assert (carol["display"], carol["birthdate"], carol["sex"], carol["role"]) == (
        "Carol (fake)", "1995-02-03", "f", "")


@pytest.mark.parametrize(("args", "message"), [
    (("alice", "--display", "A", "--birthdate", "1990-01-01"), "already exists"),
    (("carol", "--display", "C", "--birthdate", "1990-01"), "YYYY-MM-DD"),
    (("carol", "--display", "C", "--birthdate", "1990-01-01", "--meta-json", "{"), "meta_json"),
])
def test_person_add_refuses_a_bad_person(
    sim: Callable[..., Result], test_data_dir: Path, args: tuple[str, ...], message: str,
) -> None:
    before = (test_data_dir / "persons.csv").read_text()

    result = sim("person", "add", *args)

    assert result.exit_code != 0
    assert message in result.output
    assert (test_data_dir / "persons.csv").read_text() == before


# ---------- person edit ----------


def test_person_edit_option_changes_only_that_field(
    sim: Callable[..., Result], test_data_dir: Path,
) -> None:
    before = _persons(test_data_dir)["bill"]

    result = sim("person", "edit", "bill", "--role", "secondary")

    assert result.exit_code == 0, result.output
    assert _persons(test_data_dir)["bill"] == {**before, "role": "secondary"}


def test_person_edit_option_keeps_a_blank_meta_json(
    sim: Callable[..., Result], test_data_dir: Path,
) -> None:
    persons = test_data_dir / "persons.csv"
    persons.write_text(persons.read_text() + "carol,Carol (fake),1995-02-03,,\n")

    result = sim("person", "edit", "carol", "--role", "secondary")

    assert result.exit_code == 0, result.output
    assert _persons(test_data_dir)["carol"]["meta_json"] == ""


def test_person_edit_asks_with_current_values_as_defaults(
    sim: Callable[..., Result], test_data_dir: Path,
) -> None:
    before = _persons(test_data_dir)["bill"]

    # Keep display, sex and role; change the birthdate and meta_json.
    result = sim("person", "edit", "bill", input='\n1967-09-01\n\n\n{"note": 1}\n')

    assert result.exit_code == 0, result.output
    assert _persons(test_data_dir)["bill"] == {
        **before, "birthdate": "1967-09-01", "meta_json": '{"note": 1}'}


def test_person_edit_refuses_when_persons_csv_changes_meanwhile(
    sim: Callable[..., Result], test_data_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A change made while the command prompts isn't undone by writing the
    file as it was read."""
    persons = test_data_dir / "persons.csv"

    def role_prompt(*_args: object, **_kwargs: object) -> str:
        if "carol" not in persons.read_text():
            persons.write_text(persons.read_text() + "carol,Carol (fake),1995-02-03,,,{}\n")
        return ""
    monkeypatch.setattr("analysis.sim.cli._prompt_with_completion", role_prompt)

    result = sim("person", "edit", "bill", input="\n1967-09-01\n{}\n")

    assert result.exit_code != 0
    assert "changed on disk" in result.output
    assert set(_persons(test_data_dir)) == {"alice", "bill", "joint", "carol"}
    assert _persons(test_data_dir)["bill"]["birthdate"] == "1967-08-20"


def test_person_edit_of_an_unknown_person_fails(sim: Callable[..., Result]) -> None:
    result = sim("person", "edit", "nobody", "--role", "primary")

    assert result.exit_code != 0
    assert "unknown person" in result.output


def test_a_persons_change_must_leave_every_scenario_loading(
    test_data_dir: Path, derive_scenario: Callable[..., Path],
) -> None:
    """persons.csv is shared, so a scenario outside the preflighted one's
    lineage is checked too."""
    persons = test_data_dir / "persons.csv"
    persons.write_text(persons.read_text() + "carol,Carol (fake),1995-02-03,,{}\n")
    derive_scenario("other", "base", files={"lifespan.csv": "person,age_at_death\ncarol,65\n"})
    frame = pd.read_csv(persons, dtype=str, keep_default_na=False)
    before = persons.read_text()

    with pytest.raises(EditRejected, match="scenario other: .*carol"), \
            staged_edit(test_data_dir, "retire_alice") as edit:
        frame[frame["id"] != "carol"].to_csv(edit.path(persons), index=False)

    assert persons.read_text() == before


# ---------- lifespan set|clear ----------


def test_lifespan_set_writes_and_then_replaces_the_scenarios_row(
    sim: Callable[..., Result], test_scenarios_dir: Path,
) -> None:
    folder = test_scenarios_dir / "retire_alice"

    first = sim("person", "lifespan", "set", "bill", "88", "--scenario", "retire_alice")
    second = sim("person", "lifespan", "set", "bill", "89.5", "--scenario", "retire_alice")

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert _lifespans(folder) == {"bill": "89.5"}


def test_lifespan_set_overrides_an_inherited_one(
    sim: Callable[..., Result], derive_scenario: Callable[..., Path],
) -> None:
    parent = derive_scenario("parent", "retire_alice",
                             files={"lifespan.csv": "person,age_at_death\nbill,88\n"})
    child = derive_scenario("child", "parent")

    result = sim("person", "lifespan", "set", "bill", "83", "--scenario", "child")

    assert result.exit_code == 0, result.output
    assert "overriding age 88 from 'parent'" in result.output
    assert _lifespans(child) == {"bill": "83"}
    assert _lifespans(parent) == {"bill": "88"}


@pytest.mark.parametrize(("args", "message"), [
    (("nobody", "88"), "unknown person"),
    (("bill", "eighty"), "must be an age"),
    (("bill", "88", "--vary", "triangular", "--low", "90", "--high", "95"), "must lie within"),
    (("bill", "88", "--vary", "lognormal"), "--vary must be"),
])
def test_lifespan_set_refuses_a_bad_row(
    sim: Callable[..., Result], test_scenarios_dir: Path, args: tuple[str, ...], message: str,
) -> None:
    result = sim("person", "lifespan", "set", *args, "--scenario", "retire_alice")

    assert result.exit_code != 0
    assert message in result.output
    assert _lifespans(test_scenarios_dir / "retire_alice") == {}


def test_lifespan_clear_removes_the_scenarios_row(
    sim: Callable[..., Result], derive_scenario: Callable[..., Path],
) -> None:
    child = derive_scenario("child", "retire_alice", files={
        "lifespan.csv": "person,age_at_death\nalice,95\nbill,88\n"})

    result = sim("person", "lifespan", "clear", "bill", "--scenario", "child")
    last = sim("person", "lifespan", "clear", "alice", "--scenario", "child")

    assert result.exit_code == 0, result.output
    assert last.exit_code == 0, last.output
    assert not (child / "lifespan.csv").exists()


def test_lifespan_clear_of_an_override_says_what_is_inherited(
    sim: Callable[..., Result], derive_scenario: Callable[..., Path],
) -> None:
    derive_scenario("parent", "retire_alice",
                    files={"lifespan.csv": "person,age_at_death\nbill,88\n"})
    derive_scenario("child", "parent", files={"lifespan.csv": "person,age_at_death\nbill,83\n"})

    result = sim("person", "lifespan", "clear", "bill", "--scenario", "child")

    assert result.exit_code == 0, result.output
    assert "now inherits age 88 from 'parent'" in result.output


def test_lifespan_clear_refuses_an_inherited_lifespan(
    sim: Callable[..., Result], derive_scenario: Callable[..., Path],
) -> None:
    parent = derive_scenario("parent", "retire_alice",
                             files={"lifespan.csv": "person,age_at_death\nbill,88\n"})
    derive_scenario("child", "parent")

    inherited = sim("person", "lifespan", "clear", "bill", "--scenario", "child")
    absent = sim("person", "lifespan", "clear", "alice", "--scenario", "child")

    assert inherited.exit_code != 0
    assert "inherited from 'parent'" in inherited.output
    assert absent.exit_code != 0
    assert "no lifespan" in absent.output
    assert _lifespans(parent) == {"bill": "88"}


def _lifespan_row(scenario_dir: Path, person: str) -> dict[str, str]:
    frame = pd.read_csv(scenario_dir / "lifespan.csv", dtype=str, keep_default_na=False)
    return frame[frame["person"] == person].iloc[0].to_dict()


def test_lifespan_set_with_vary_options_writes_the_distribution(
    sim: Callable[..., Result], test_scenarios_dir: Path,
) -> None:
    folder = test_scenarios_dir / "retire_alice"

    result = sim("person", "lifespan", "set", "bill", "86", "--vary", "triangular",
                 "--low", "78", "--high", "97", "--scenario", "retire_alice")

    assert result.exit_code == 0, result.output
    assert "age 86 (triangular 78..97)" in result.output
    row = _lifespan_row(folder, "bill")
    assert (row["vary_distribution"], row["vary_low"], row["vary_high"], row["vary_mode"]) == (
        "triangular", "78", "97", "")


def test_lifespan_set_changes_only_the_options_given(
    sim: Callable[..., Result], test_scenarios_dir: Path,
) -> None:
    folder = test_scenarios_dir / "retire_alice"
    sim("person", "lifespan", "set", "bill", "86", "--vary", "uniform",
        "--low", "78", "--high", "97", "--scenario", "retire_alice")

    moved = sim("person", "lifespan", "set", "bill", "--high", "95", "--scenario", "retire_alice")
    row = _lifespan_row(folder, "bill")
    dropped = sim("person", "lifespan", "set", "bill", "--vary", "none",
                  "--scenario", "retire_alice")

    assert moved.exit_code == 0, moved.output
    assert (row["age_at_death"], row["vary_low"], row["vary_high"]) == ("86", "78", "95")
    assert dropped.exit_code == 0, dropped.output
    after = _lifespan_row(folder, "bill")
    assert (after["age_at_death"], after["vary_distribution"], after["vary_low"]) == ("86", "", "")


def test_lifespan_set_with_no_options_asks_for_each_field(
    sim: Callable[..., Result], test_scenarios_dir: Path,
) -> None:
    folder = test_scenarios_dir / "retire_alice"
    # Age, distribution, bounds, mode, correlation, and the default life table.
    answers = "\n".join(["84", "triangular", "75", "95", "", "", ""]) + "\n"

    result = sim("person", "lifespan", "set", "alice", "--scenario", "retire_alice",
                 input=answers)

    assert result.exit_code == 0, result.output
    row = _lifespan_row(folder, "alice")
    assert (row["age_at_death"], row["vary_distribution"], row["vary_low"],
            row["vary_high"], row["vary_correlate_with"]) == ("84", "triangular", "75", "95", "")


def test_lifespan_set_correlates_two_lifespans(
    sim: Callable[..., Result], test_scenarios_dir: Path,
) -> None:
    folder = test_scenarios_dir / "retire_alice"
    sim("person", "lifespan", "set", "alice", "84", "--vary", "triangular",
        "--low", "75", "--high", "95", "--scenario", "retire_alice")

    result = sim("person", "lifespan", "set", "bill", "86", "--vary", "uniform",
                 "--low", "78", "--high", "97", "--correlate-with", "lifespan:alice",
                 "--correlation", "0.5", "--scenario", "retire_alice")
    missing = sim("person", "lifespan", "set", "bill", "--correlate-with", "lifespan:carol",
                  "--scenario", "retire_alice")

    assert result.exit_code == 0, result.output
    row = _lifespan_row(folder, "bill")
    assert (row["vary_correlate_with"], row["vary_correlation"]) == ("lifespan:alice", "0.5")
    assert missing.exit_code != 0
    assert "lifespan:carol" in missing.output
