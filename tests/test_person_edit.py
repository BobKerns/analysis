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
    return dict(zip(frame["person"], frame["final_month"], strict=True))


# ---------- person add ----------


def test_person_add_with_options_writes_the_row(
    sim: Callable[..., Result], test_data_dir: Path,
) -> None:
    result = sim("person", "add", "carol", "--display", "Carol (fake)",
                 "--birthdate", "1995-02-03", "--role", "secondary")

    assert result.exit_code == 0, result.output
    assert "every scenario" in result.output
    assert _persons(test_data_dir)["carol"] == {
        "id": "carol", "display": "Carol (fake)", "birthdate": "1995-02-03",
        "role": "secondary", "meta_json": "{}",
    }


def test_person_add_asks_for_what_is_missing(
    sim: Callable[..., Result], test_data_dir: Path,
) -> None:
    result = sim("person", "add", "carol", input="Carol (fake)\n1995-02-03\n\n\n")

    assert result.exit_code == 0, result.output
    carol = _persons(test_data_dir)["carol"]
    assert (carol["display"], carol["birthdate"], carol["role"]) == ("Carol (fake)", "1995-02-03", "")


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


def test_person_edit_asks_with_current_values_as_defaults(
    sim: Callable[..., Result], test_data_dir: Path,
) -> None:
    before = _persons(test_data_dir)["bill"]

    # Keep display and role; change the birthdate and meta_json.
    result = sim("person", "edit", "bill", input='\n1967-09-01\n\n{"note": 1}\n')

    assert result.exit_code == 0, result.output
    assert _persons(test_data_dir)["bill"] == {
        **before, "birthdate": "1967-09-01", "meta_json": '{"note": 1}'}


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
    derive_scenario("other", "base", files={"lifespan.csv": "person,final_month\ncarol,2060-01\n"})
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

    first = sim("person", "lifespan", "set", "bill", "2055-06", "--scenario", "retire_alice")
    second = sim("person", "lifespan", "set", "bill", "2056-01", "--scenario", "retire_alice")

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert _lifespans(folder) == {"bill": "2056-01"}


def test_lifespan_set_overrides_an_inherited_one(
    sim: Callable[..., Result], derive_scenario: Callable[..., Path],
) -> None:
    parent = derive_scenario("parent", "retire_alice",
                             files={"lifespan.csv": "person,final_month\nbill,2055-06\n"})
    child = derive_scenario("child", "parent")

    result = sim("person", "lifespan", "set", "bill", "2050-12", "--scenario", "child")

    assert result.exit_code == 0, result.output
    assert "overriding 2055-06 from 'parent'" in result.output
    assert _lifespans(child) == {"bill": "2050-12"}
    assert _lifespans(parent) == {"bill": "2055-06"}


@pytest.mark.parametrize(("args", "message"), [
    (("nobody", "2055-06"), "unknown person"),
    (("bill", "June 2055"), "date must be"),
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
        "lifespan.csv": "person,final_month\nalice,2060-01\nbill,2055-06\n"})

    result = sim("person", "lifespan", "clear", "bill", "--scenario", "child")
    last = sim("person", "lifespan", "clear", "alice", "--scenario", "child")

    assert result.exit_code == 0, result.output
    assert last.exit_code == 0, last.output
    assert not (child / "lifespan.csv").exists()


def test_lifespan_clear_of_an_override_says_what_is_inherited(
    sim: Callable[..., Result], derive_scenario: Callable[..., Path],
) -> None:
    derive_scenario("parent", "retire_alice",
                    files={"lifespan.csv": "person,final_month\nbill,2055-06\n"})
    derive_scenario("child", "parent", files={"lifespan.csv": "person,final_month\nbill,2050-12\n"})

    result = sim("person", "lifespan", "clear", "bill", "--scenario", "child")

    assert result.exit_code == 0, result.output
    assert "now inherits 2055-06 from 'parent'" in result.output


def test_lifespan_clear_refuses_an_inherited_lifespan(
    sim: Callable[..., Result], derive_scenario: Callable[..., Path],
) -> None:
    parent = derive_scenario("parent", "retire_alice",
                             files={"lifespan.csv": "person,final_month\nbill,2055-06\n"})
    derive_scenario("child", "parent")

    inherited = sim("person", "lifespan", "clear", "bill", "--scenario", "child")
    absent = sim("person", "lifespan", "clear", "alice", "--scenario", "child")

    assert inherited.exit_code != 0
    assert "inherited from 'parent'" in inherited.output
    assert absent.exit_code != 0
    assert "no lifespan" in absent.output
    assert _lifespans(parent) == {"bill": "2055-06"}
