"""Guided ``sim calendar edit`` (issue #56): pick one row, change its fields,
and write it back in place without duplicating another row."""

from __future__ import annotations

import json
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


@pytest.fixture
def calendar_edit(test_data_dir: Path) -> Callable[..., Result]:
    """Run ``sim calendar edit`` on the private retire_alice copy, typing ``answers``."""
    def run(*args: str, answers: list[str]) -> Result:
        return CliRunner().invoke(
            main,
            ["sim", "calendar", "edit", *args,
             "--data-dir", str(test_data_dir), "--scenario", "retire_alice"],
            input="".join(answer + "\n" for answer in answers),
        )
    return run


@pytest.fixture
def calendar_rows(retire_alice_csv: Callable[[str], Path]) -> Callable[[], pd.DataFrame]:
    """The private copy's calendar.csv, every column present."""
    def read() -> pd.DataFrame:
        frame = pd.read_csv(retire_alice_csv("calendar.csv"), dtype=str, keep_default_na=False)
        return frame.reindex(columns=CALENDAR_HEADER, fill_value="")
    return read


def test_edit_changes_one_field_and_leaves_the_row_in_place(
    calendar_edit: Callable[..., Result], calendar_rows: Callable[[], pd.DataFrame],
) -> None:
    before = calendar_rows()

    result = calendar_edit("3", answers=["amount", "1500.00", ""])

    assert result.exit_code == 0, result.output
    assert "updated set_amount 'healthcare'" in result.output
    after = calendar_rows()
    assert after.loc[2, "amount"] == "150000"
    changed = after.ne(before)
    assert changed.sum().sum() == 1 and changed.loc[2, "amount"]


@pytest.mark.parametrize("row", ["1", "2", "3", "4", "5"])
def test_writing_a_row_unchanged_leaves_the_file_as_it_was(
    calendar_edit: Callable[..., Result], calendar_rows: Callable[[], pd.DataFrame], row: str,
) -> None:
    before = calendar_rows()

    result = calendar_edit(row, answers=[""])

    assert result.exit_code == 0, result.output
    pd.testing.assert_frame_equal(calendar_rows(), before)


def test_writing_a_varying_row_unchanged_keeps_its_bounds(
    calendar_edit: Callable[..., Result], calendar_rows: Callable[[], pd.DataFrame],
    append_calendar_row: Callable[..., None],
) -> None:
    append_calendar_row(
        date="2027-02", kind="set_rate", name="checking_vary", series="Asset_Checking",
        amount="42500", vary_target="amount", vary_distribution="truncated_normal",
        vary_median="42500", vary_sd="10000", vary_skew="0.5", vary_ramp_months="6",
    )
    before = calendar_rows()

    result = calendar_edit("checking_vary", answers=[""])

    assert result.exit_code == 0, result.output
    pd.testing.assert_frame_equal(calendar_rows(), before)


def test_edit_narrows_the_list_until_one_row_is_left(
    calendar_edit: Callable[..., Result], calendar_rows: Callable[[], pd.DataFrame],
) -> None:
    result = calendar_edit(answers=[
        "nothing like this", "retire", "bill",   # no match; two rows; one row
        "date", "2031-01", "",
    ])

    assert result.exit_code == 0, result.output
    assert "no row matches 'nothing like this'" in result.output
    assert "Editing row 5:" in result.output
    assert calendar_rows().loc[4, "date"] == "2031-01"


def test_edit_refuses_a_change_that_would_copy_another_row(
    calendar_edit: Callable[..., Result], calendar_rows: Callable[[], pd.DataFrame],
) -> None:
    before = calendar_rows()

    result = calendar_edit("2", answers=[
        "date", "2030-12-31",                   # still Alice's salary: no clash yet
        "scheduled-name", "salary_bill",        # now a copy of row 5: refused
        "salary_alice",
        "cancel",
    ])

    assert result.exit_code == 1
    assert "that would duplicate another row" in result.output
    # ... and the rule that would clash is not offered as a completion.
    offered = result.output.split("scheduled rule")[0].rsplit("choices:", 1)[-1]
    assert "salary_alice" in offered and "salary_bill" not in offered
    pd.testing.assert_frame_equal(calendar_rows(), before)


def test_edit_refuses_another_rows_name_and_does_not_offer_its_own_to_correlate_with(
    calendar_edit: Callable[..., Result], calendar_rows: Callable[[], pd.DataFrame],
    append_calendar_row: Callable[..., None],
) -> None:
    for name, low in (("first_vary", "2029-01"), ("second_vary", "2029-02")):
        append_calendar_row(
            date=low, kind="retire_scheduled", name=name, scheduled_name="salary_alice",
            vary_target="date", vary_distribution="uniform", vary_low=low, vary_high="2029-12",
        )

    result = calendar_edit("second_vary", answers=[
        "name", "first_vary", "renamed_vary",   # taken, then free
        "correlate", "y", "first_vary", "0.5",
        "",
    ])

    assert result.exit_code == 0, result.output
    assert "another row is already named 'first_vary'" in result.output
    offered = result.output.split("correlate with:")[0].rsplit("choices:", 1)[-1]
    assert "first_vary" in offered and "second_vary" not in offered
    row = calendar_rows().iloc[-1]
    assert (row["name"], row["vary_correlate_with"], row["vary_correlation"]) == (
        "renamed_vary", "first_vary", "0.5",
    )


def test_edit_keeps_what_the_guide_has_no_field_for(
    calendar_edit: Callable[..., Result], calendar_rows: Callable[[], pd.DataFrame],
    append_calendar_row: Callable[..., None],
) -> None:
    append_calendar_row(
        date="2031-05", kind="one_off_transfer", account_1="Asset_Savings",
        account_2="Asset_Checking", amount="10000", tax_character="none",
        tags="expense:discretionary", params_json=json.dumps({"note": "hand-authored"}),
    )

    result = calendar_edit("6", answers=["amount", "250.00", ""])

    assert result.exit_code == 0, result.output
    row = calendar_rows().iloc[-1]
    assert row["amount"] == "25000"
    assert row["tags"] == "expense:discretionary"
    assert json.loads(row["params_json"])["note"] == "hand-authored"


def test_edit_returns_to_the_review_when_the_scenario_would_not_load(
    calendar_edit: Callable[..., Result], calendar_rows: Callable[[], pd.DataFrame],
) -> None:
    before = calendar_rows()

    result = calendar_edit("1", answers=["series", "no_such_series", "", "cancel"])

    assert result.exit_code == 1
    assert "Not written:" in result.output
    pd.testing.assert_frame_equal(calendar_rows(), before)


def test_edit_says_so_when_a_row_is_of_a_kind_it_cannot_edit(
    calendar_edit: Callable[..., Result], append_calendar_row: Callable[..., None],
) -> None:
    append_calendar_row(date="2031-05", kind="some_future_kind", description="by hand")

    result = calendar_edit("6", answers=[])

    assert result.exit_code != 0
    assert "'some_future_kind' row, which this command cannot edit" in result.output


def test_edit_says_so_when_the_scenario_has_no_rows_of_its_own(
    calendar_edit: Callable[..., Result], retire_alice_csv: Callable[[str], Path],
) -> None:
    retire_alice_csv("calendar.csv").write_text(",".join(CALENDAR_HEADER) + "\n")

    result = calendar_edit(answers=[])

    assert result.exit_code != 0
    assert "has no calendar rows of its own to edit" in result.output
