"""``sim calendar delete`` and ``sim calendar tag add|rm`` (issue #184): pick
a row as ``calendar edit`` does; an inherited row is refused by delete and
overridden by tag (#202)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.sim.registries import CALENDAR_HEADER

# The prototype calendar's own rows, by number:
#   1 set_rate Asset_Checking   2 retire_scheduled salary_alice   3 set_amount healthcare
#   4 one_off_transfer          5 retire_scheduled salary_bill


def _run(test_data_dir: Path, scenario: str, *args: str, answers: tuple[str, ...] | list[str] = ()) -> Result:
    command, rest = args[0], args[1:]
    words = command.split()
    return CliRunner().invoke(
        main,
        ["sim", "calendar", *words, *rest, "--data-dir", str(test_data_dir), "--scenario", scenario],
        input="".join(answer + "\n" for answer in answers),
    )


def _read(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    return frame.reindex(columns=CALENDAR_HEADER, fill_value="")


@pytest.fixture
def calendar_rows(retire_alice_csv: Callable[[str], Path]) -> Callable[[], pd.DataFrame]:
    return lambda: _read(retire_alice_csv("calendar.csv"))


def test_delete_removes_the_chosen_row_once_confirmed(
    test_data_dir: Path, calendar_rows: Callable[[], pd.DataFrame],
) -> None:
    before = calendar_rows()

    result = _run(test_data_dir, "retire_alice", "delete", "healthcare", answers=["y"])

    assert result.exit_code == 0, result.output
    assert "deleted row 3" in result.output
    pd.testing.assert_frame_equal(calendar_rows(), before.drop(index=2).reset_index(drop=True))


def test_delete_writes_nothing_unless_confirmed(
    test_data_dir: Path, calendar_rows: Callable[[], pd.DataFrame],
) -> None:
    before = calendar_rows()

    result = _run(test_data_dir, "retire_alice", "delete", "3", answers=["n"])

    assert result.exit_code == 0, result.output
    assert "aborted" in result.output
    pd.testing.assert_frame_equal(calendar_rows(), before)


def test_delete_refuses_a_row_others_correlate_with(
    test_data_dir: Path, append_calendar_row: Callable[..., None],
    calendar_rows: Callable[[], pd.DataFrame],
) -> None:
    append_calendar_row(kind="latent_factor", name="economy")
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="rate_a", series="Asset_Checking", amount="4.5%",
        vary_target="amount", vary_distribution="uniform", vary_low="4%", vary_high="5%",
        vary_correlate_with="economy", vary_correlation="0.5",
    )
    before = calendar_rows()

    result = _run(test_data_dir, "retire_alice", "delete", "economy", "--yes")

    assert result.exit_code != 0
    assert "row 'economy' is correlated with by" in result.output
    assert "[rate_a]" in result.output
    pd.testing.assert_frame_equal(calendar_rows(), before)


def test_delete_refuses_an_inherited_row_and_names_where_it_comes_from(
    test_data_dir: Path, test_child_scenario: Path,
) -> None:
    result = _run(test_data_dir, "child", "delete", "healthcare", "--yes")

    assert result.exit_code != 0
    assert "comes from scenario 'retire_alice'" in result.output
    assert _read(test_child_scenario / "calendar.csv").empty


def test_deleting_an_override_brings_back_the_inherited_row(
    test_data_dir: Path, test_child_scenario: Path,
) -> None:
    tagged = _run(test_data_dir, "child", "tag add", "healthcare", "cost:medical")
    assert tagged.exit_code == 0, tagged.output
    assert len(_read(test_child_scenario / "calendar.csv")) == 1

    result = _run(test_data_dir, "child", "delete", "healthcare", "--yes")

    assert result.exit_code == 0, result.output
    assert "inherited from 'retire_alice' again" in result.output
    assert _read(test_child_scenario / "calendar.csv").empty


def test_tag_add_and_rm_change_the_rows_own_tags_in_place(
    test_data_dir: Path, calendar_rows: Callable[[], pd.DataFrame],
) -> None:
    before = calendar_rows()

    added = _run(test_data_dir, "retire_alice", "tag add", "4", "event:inheritance", "kind:windfall")

    assert added.exit_code == 0, added.output
    after = calendar_rows()
    assert after.loc[3, "tags"] == "event:inheritance kind:windfall"
    assert after.drop(columns="tags").equals(before.drop(columns="tags"))

    removed = _run(test_data_dir, "retire_alice", "tag rm", "4", "kind:windfall")

    assert removed.exit_code == 0, removed.output
    assert calendar_rows().loc[3, "tags"] == "event:inheritance"


def test_tag_on_an_inherited_row_writes_a_whole_row_override(
    test_data_dir: Path, test_child_scenario: Path, calendar_rows: Callable[[], pd.DataFrame],
) -> None:
    parent = calendar_rows()

    result = _run(test_data_dir, "child", "tag add", "healthcare", "cost:medical")

    assert result.exit_code == 0, result.output
    assert "wrote an override" in result.output
    override = _read(test_child_scenario / "calendar.csv")
    assert override.iloc[0].to_dict() == {**parent.iloc[2].to_dict(), "tags": "cost:medical"}
    pd.testing.assert_frame_equal(calendar_rows(), parent)

    again = _run(test_data_dir, "child", "tag rm", "healthcare", "cost:medical")

    assert again.exit_code == 0, again.output
    override = _read(test_child_scenario / "calendar.csv")
    assert len(override) == 1 and override.loc[0, "tags"] == ""


def test_tag_that_changes_nothing_writes_nothing(
    test_data_dir: Path, test_child_scenario: Path,
) -> None:
    result = _run(test_data_dir, "child", "tag rm", "healthcare", "cost:medical")

    assert result.exit_code == 0, result.output
    assert "tags unchanged" in result.output
    assert _read(test_child_scenario / "calendar.csv").empty


def test_tag_refuses_a_malformed_token(test_data_dir: Path) -> None:
    result = _run(test_data_dir, "retire_alice", "tag add", "4", "untagged")

    assert result.exit_code != 0
    assert "invalid tag 'untagged'" in result.output
