"""Guided ``sim calendar edit`` (issue #56): pick one row, change its fields,
and write it back in place without duplicating another row."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click
import pandas as pd
import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.sim import cli as cli_module
from analysis.sim.cli import (
    _CALENDAR_REPLACE_ROW,
    _read_calendar_rows,
    _write_calendar_row,
    calendar_edit as calendar_edit_command,
)
from analysis.sim.loader import calendar_row_key
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
    assert after.loc[2, "amount"] == "1500.00"
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
        amount="4.25", vary_target="amount", vary_distribution="truncated_normal",
        vary_median="4.25", vary_sd="1", vary_skew="0.5", vary_ramp_months="6",
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
        "date", "2030-12",                      # row 5's date, spelled as a bare month
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


@pytest.mark.usefixtures("without_prototype_defaults")
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


@pytest.mark.usefixtures("without_prototype_defaults")
def test_edit_keeps_what_the_guide_has_no_field_for(
    calendar_edit: Callable[..., Result], calendar_rows: Callable[[], pd.DataFrame],
    append_calendar_row: Callable[..., None],
) -> None:
    append_calendar_row(
        date="2031-05", kind="one_off_transfer", account_1="Asset_Savings",
        account_2="Asset_Checking", amount="100.00", tax_character="none",
        tags="expense:discretionary", params_json=json.dumps({"note": "hand-authored"}),
    )

    result = calendar_edit("6", answers=["amount", "250.00", ""])

    assert result.exit_code == 0, result.output
    row = calendar_rows().iloc[-1]
    assert row["amount"] == "250.00"
    assert row["tags"] == "expense:discretionary"
    assert json.loads(row["params_json"])["note"] == "hand-authored"


@pytest.mark.usefixtures("without_prototype_defaults")
def test_edit_keeps_a_hand_authored_key_another_kinds_command_would_own(
    calendar_edit: Callable[..., Result], calendar_rows: Callable[[], pd.DataFrame],
    append_calendar_row: Callable[..., None],
) -> None:
    # aux_kind is set-rate's to write, and description is transfer's; on the
    # other kind each is just a key someone put there.
    append_calendar_row(
        date="2031-05", kind="one_off_transfer", account_1="Asset_Savings",
        account_2="Asset_Checking", amount="100.00", tax_character="none",
        params_json=json.dumps({"aux_kind": "by hand"}),
    )
    append_calendar_row(
        date="2031-06", kind="set_rate", series="Asset_Checking", amount="3",
        params_json=json.dumps({"description": "by hand"}),
    )

    transfer = calendar_edit("6", answers=["amount", "250.00", ""])
    rate = calendar_edit("7", answers=["rate-percent", "3.5", ""])

    assert transfer.exit_code == 0, transfer.output
    assert rate.exit_code == 0, rate.output
    rows = calendar_rows()
    assert json.loads(rows.loc[5, "params_json"]) == {"aux_kind": "by hand"}
    assert json.loads(rows.loc[6, "params_json"]) == {"description": "by hand"}
    assert rows.loc[6, "description"] == ""    # not promoted into the row's own column


def test_edit_compares_another_rows_values_without_their_stray_spaces(
    calendar_edit: Callable[..., Result], calendar_rows: Callable[[], pd.DataFrame],
    append_calendar_row: Callable[..., None],
) -> None:
    append_calendar_row(date="2028-01", kind="set_rate", series=" Asset_Checking ", amount="3")
    before = calendar_rows()

    # Row 1 is Asset_Checking's rate; moved to 2028-01 at 3% it would be row 6 again.
    result = calendar_edit("1", answers=["date", "2028-01", "rate-percent", "3", "", "cancel"])

    assert result.exit_code == 1
    assert "that would duplicate another row" in result.output
    pd.testing.assert_frame_equal(calendar_rows(), before)


def test_edit_returns_to_the_review_when_the_scenario_would_not_load(
    calendar_edit: Callable[..., Result], calendar_rows: Callable[[], pd.DataFrame],
) -> None:
    before = calendar_rows()

    result = calendar_edit("1", answers=["series", "no such series", "", "cancel"])

    assert result.exit_code == 1
    assert "Not written:" in result.output
    pd.testing.assert_frame_equal(calendar_rows(), before)


@pytest.mark.usefixtures("without_prototype_defaults")
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


def test_a_row_key_reads_dates_and_spacing_as_the_loader_does() -> None:
    row = {"kind": "set_rate", "date": "2030-12-31", "series": "cpi", "amount": "3"}

    assert calendar_row_key({**row, "date": "2030-12"}) == calendar_row_key(row)
    assert calendar_row_key({**row, "series": " cpi "}) == calendar_row_key(row)
    assert calendar_row_key({**row, "date": "2030-11"}) != calendar_row_key(row)


@pytest.mark.usefixtures("without_prototype_defaults")
def test_edit_ignores_a_lot_the_changed_account_no_longer_has(
    calendar_edit: Callable[..., Result], calendar_rows: Callable[[], pd.DataFrame],
    append_calendar_row: Callable[..., None],
) -> None:
    transfer = {
        "date": "2031-05", "kind": "one_off_transfer", "account_2": "Asset_Checking",
        "amount": "100.00", "tax_character": "none",
    }
    append_calendar_row(**transfer, account_1="Asset_Savings")                           # row 6
    append_calendar_row(**transfer, account_1="Asset_Brokerage_Joint", lot_1="default")  # row 7
    before = calendar_rows()

    # Row 7 from savings would be row 6 over again, once its lot is gone.
    result = calendar_edit("7", answers=["from-account", "Asset_Savings", "", "cancel"])

    assert result.exit_code == 1
    assert "that would duplicate another row" in result.output
    offered = result.output.split("from account or chain [")[0].rsplit("choices", 1)[-1]
    assert "Asset_Savings" not in offered
    pd.testing.assert_frame_equal(calendar_rows(), before)


def test_edit_refuses_to_write_when_the_file_changed_under_it(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    path = retire_alice_csv("calendar.csv")
    chosen = _read_calendar_rows(path)[2]
    # Someone removes row 1 while the prompts are open: index 2 is now another row.
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    frame.drop(index=0).to_csv(path, index=False)
    changed = path.read_text()

    with click.Context(calendar_edit_command) as context:
        context.meta[_CALENDAR_REPLACE_ROW] = (2, chosen)
        with pytest.raises(click.ClickException, match="changed while this row was being edited"):
            _write_calendar_row(
                path, {**chosen, "amount": "1500.00"},
                data_dir=test_data_dir, scenario_name="retire_alice",
            )

    assert path.read_text() == changed


def test_edit_says_which_scenario_before_asking_anything(
    calendar_edit: Callable[..., Result], test_data_dir: Path,
    retire_alice_csv: Callable[[str], Path],
) -> None:
    named = calendar_edit(answers=["3", "cancel"])

    assert named.output.startswith(f"Scenario: retire_alice\n  {retire_alice_csv('calendar.csv')}\n")

    # With no --scenario, it is whichever scenario is the default: say that too.
    defaulted = CliRunner().invoke(
        main, ["sim", "calendar", "edit", "--data-dir", str(test_data_dir)], input="3\ncancel\n",
    )

    assert defaulted.output.startswith(
        "Scenario: retire_alice (the default; use --scenario for another)\n"
    )


@pytest.mark.usefixtures("without_prototype_defaults")
def test_edit_compares_names_without_their_stray_spaces(
    calendar_edit: Callable[..., Result], append_calendar_row: Callable[..., None],
) -> None:
    for name, low in ((" first_vary ", "2029-01"), ("second_vary", "2029-02")):
        append_calendar_row(
            date=low, kind="retire_scheduled", name=name, scheduled_name="salary_alice",
            vary_target="date", vary_distribution="uniform", vary_low=low, vary_high="2029-12",
        )

    renamed = calendar_edit("second_vary", answers=["name", "first_vary", "second_vary", "cancel"])
    own = calendar_edit("6", answers=["correlate", "y", "second_vary", "0.5", "cancel"])

    assert "another row is already named 'first_vary'" in renamed.output
    offered = own.output.split("correlate with:")[0].rsplit("choices:", 1)[-1]
    assert "second_vary" in offered and "first_vary" not in offered


@pytest.mark.parametrize("command", ["edit", "add"])
def test_a_defaulted_scenario_is_resolved_once_and_then_named(
    test_data_dir: Path, monkeypatch: pytest.MonkeyPatch, command: str,
) -> None:
    """The default could change while the prompts are open; what was
    announced is what must be written."""
    asked_for: list[str | None] = []
    resolve = cli_module._resolve_scenario

    def recording(data_dir: Path, scenario: str | None, **kwargs: Any) -> Any:
        asked_for.append(scenario)
        return resolve(data_dir, scenario, **kwargs)

    monkeypatch.setattr(cli_module, "_resolve_scenario", recording)
    answers = {
        "edit": ["3", "amount", "1500.00", ""],
        "add": ["latent-factor", "economy", "", "n", "", ""],  # no tail dependence
    }[command]

    result = CliRunner().invoke(
        main, ["sim", "calendar", command, "--data-dir", str(test_data_dir)],
        input="".join(answer + "\n" for answer in answers),
    )

    assert result.exit_code == 0, result.output
    assert asked_for[0] is None
    assert set(asked_for[1:]) == {"retire_alice"}


@pytest.mark.usefixtures("without_prototype_defaults")
def test_edit_refuses_to_clear_a_field_when_that_would_copy_another_row(
    calendar_edit: Callable[..., Result], calendar_rows: Callable[[], pd.DataFrame],
    append_calendar_row: Callable[..., None],
) -> None:
    transfer = {
        "date": "2031-05", "kind": "one_off_transfer", "account_1": "Asset_Savings",
        "account_2": "Asset_Checking", "amount": "100.00", "tax_character": "none",
    }
    append_calendar_row(**transfer)                 # row 6
    append_calendar_row(**transfer, series="cpi")   # row 7: differs only in its series
    before = calendar_rows()

    result = calendar_edit("7", answers=["series", "-", "", "cancel"])

    assert result.exit_code == 1
    assert "left blank, that would duplicate another row" in result.output
    pd.testing.assert_frame_equal(calendar_rows(), before)
