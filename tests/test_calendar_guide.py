"""Guided ``sim calendar add`` (issue #32): the question flow, the review
step, and prompting for arguments a subcommand was given without."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.sim import calendar_guide
from analysis.sim.cli import calendar_add_group_command


@pytest.fixture
def calendar_add(test_data_dir: Path) -> Callable[..., Result]:
    """Run ``sim calendar add`` on the private retire_alice copy, typing ``answers``."""
    def run(*args: str, answers: list[str]) -> Result:
        return CliRunner().invoke(
            main,
            ["sim", "calendar", "add", *args,
             "--data-dir", str(test_data_dir), "--scenario", "retire_alice"],
            input="".join(answer + "\n" for answer in answers),
        )
    return run


@pytest.fixture
def calendar_rows(retire_alice_csv: Callable[[str], Path]) -> Callable[[], pd.DataFrame]:
    def read() -> pd.DataFrame:
        return pd.read_csv(retire_alice_csv("calendar.csv"), dtype=str, keep_default_na=False)
    return read


def test_guide_writes_a_varying_row_and_shows_the_equivalent_command(
    calendar_add: Callable[..., Result], calendar_rows: Callable[[], pd.DataFrame],
    test_data_dir: Path,
) -> None:
    result = calendar_add(answers=[
        "set-rate", "2027-02", "", "Asset_Checking", "4.5", "", "",   # aux kind, description, tags blank
        "y", "checking_vary", "amount", "uniform", "4", "5", "", "",  # ramp, process blank
        "n", "",                                                       # no correlation; write
    ])

    assert result.exit_code == 0, result.output
    assert (
        "sim calendar add set-rate 2027-02 Asset_Checking 4.5 --name checking_vary "
        "--vary-target amount --vary-distribution uniform --vary-low 4 --vary-high 5 "
        f"--scenario retire_alice --data-dir {test_data_dir}"
    ) in result.output
    row = calendar_rows().query("name == 'checking_vary'").iloc[0]
    assert (row["kind"], row["date"], row["amount"]) == ("set_rate", "2027-02", "4.5%")
    assert (row["vary_low"], row["vary_high"]) == ("4%", "5%")


def test_guide_asks_only_what_the_distribution_uses(calendar_add: Callable[..., Result]) -> None:
    result = calendar_add(answers=[
        "set-rate", "2027-02", "", "Asset_Checking", "4.5", "", "",
        "y", "checking_vary", "amount", "uniform", "4", "5", "", "", "n", "",
    ])

    assert result.exit_code == 0, result.output
    assert "low:" in result.output and "high:" in result.output
    for unused in ("median", "spread (sd)", "most likely value", "skew", "tail weight"):
        assert f"{unused} (optional)" not in result.output


def test_guide_writes_a_process_row(
    calendar_add: Callable[..., Result], calendar_rows: Callable[[], pd.DataFrame],
) -> None:
    """ADR-006 §4a: a set-rate amount can move year by year; the shock is
    typed in percent, like the rate, and stored in micros."""
    result = calendar_add(answers=[
        "set-rate", "2027-02", "", "Asset_Checking", "7", "", "",
        "y", "stock_path", "amount", "uniform", "5", "9", "",   # ramp months blank
        "ar1", "0.2", "17", "",                                  # process, persistence, shock, start
        "n", "",
    ])

    assert result.exit_code == 0, result.output
    assert "--vary-process ar1 --vary-persistence 0.2 --vary-innovation-sd 17" in result.output
    row = calendar_rows().query("name == 'stock_path'").iloc[0]
    assert (row["vary_process"], row["vary_persistence"], row["vary_innovation_sd"]) == (
        "ar1", "0.2", "17%")
    assert row["vary_process_start"] == ""


def test_guide_writes_a_process_that_starts_at_the_current_rate(
    calendar_add: Callable[..., Result], calendar_rows: Callable[[], pd.DataFrame],
) -> None:
    """#280: a process may start at the rate in effect and revert from it."""
    result = calendar_add(answers=[
        "set-rate", "2027-02", "", "Asset_Checking", "3", "", "",
        "y", "cash_path", "amount", "uniform", "2", "4", "",
        "ar1", "0.6", "1", "current",
        "n", "",
    ])

    assert result.exit_code == 0, result.output
    assert "--vary-process-start current" in result.output
    row = calendar_rows().query("name == 'cash_path'").iloc[0]
    assert row["vary_process_start"] == "current"


def test_guide_asks_again_after_a_bad_answer(
    calendar_add: Callable[..., Result], calendar_rows: Callable[[], pd.DataFrame],
) -> None:
    result = calendar_add(answers=[
        "nonsense", "retire-scheduled",       # kind
        "2029-6-30", "2029-06-30",            # date
        "no_such_rule", "salary_bill",        # scheduled rule
        "", "", "n", "",
    ])

    assert result.exit_code == 0, result.output
    assert "date must be" in result.output
    assert "not one of:" in result.output
    assert "retire_scheduled" in calendar_rows()["kind"].tolist()


def test_guide_lets_a_field_be_changed_at_the_review(
    calendar_add: Callable[..., Result], calendar_rows: Callable[[], pd.DataFrame],
) -> None:
    result = calendar_add(answers=[
        "set-amount", "2028-01", "salary_bill", "100.00", "", "", "", "n",
        "amount", "250.00",                   # change the amount, then write
        "",
    ])

    assert result.exit_code == 0, result.output
    row = calendar_rows().query("kind == 'set_amount'").iloc[-1]
    assert row["amount"] == "250.00"


def test_guide_turning_variation_on_at_the_review_asks_its_questions(
    calendar_add: Callable[..., Result], calendar_rows: Callable[[], pd.DataFrame],
) -> None:
    result = calendar_add(answers=[
        "retire-scheduled", "2029-06-30", "salary_bill", "", "", "n",
        "vary", "y",                          # change the answer at the review
        "retire_vary", "", "uniform", "2029-01", "2029-12", "n",
        "",
    ])

    assert result.exit_code == 0, result.output
    row = calendar_rows().query("name == 'retire_vary'").iloc[0]
    assert (row["vary_target"], row["vary_low"], row["vary_high"]) == ("date", "2029-01", "2029-12")


def test_guide_returns_to_the_review_when_the_row_is_rejected(
    calendar_add: Callable[..., Result], calendar_rows: Callable[[], pd.DataFrame],
) -> None:
    before = len(calendar_rows())

    result = calendar_add(answers=[
        "set-rate", "2028-01", "", "no such series", "3.5", "", "", "n",
        "",                                   # write: rejected by the validating load
        "series", "Asset_Checking",           # fix it
        "",                                   # write again
    ])

    assert result.exit_code == 0, result.output
    assert "Not written:" in result.output
    rows = calendar_rows()
    assert len(rows) == before + 1
    assert rows.iloc[-1]["series"] == "Asset_Checking"


def test_guide_cancel_writes_nothing(
    calendar_add: Callable[..., Result], calendar_rows: Callable[[], pd.DataFrame],
) -> None:
    before = len(calendar_rows())

    result = calendar_add(answers=[
        "latent-factor", "economy", "the inflation regime", "n", "", "cancel",
    ])

    assert result.exit_code == 1
    assert "nothing was written" in result.output
    assert len(calendar_rows()) == before


def test_guide_asks_for_a_lot_only_where_a_blank_one_would_not_do(
    calendar_add: Callable[..., Result], calendar_rows: Callable[[], pd.DataFrame],
) -> None:
    result = calendar_add(answers=[
        "transfer", "2030-06", "Asset_Checking", "Asset_Brokerage_Joint", "default",
        "42.00", "", "", "", "n", "",         # tax character, description, series tag blank
    ])

    assert result.exit_code == 0, result.output
    assert "to lot" in result.output and "from lot" not in result.output
    row = calendar_rows().query("kind == 'one_off_transfer'").iloc[-1]
    assert (row["account_2"], row["lot_2"], row["amount"]) == ("Asset_Brokerage_Joint", "default", "42.00")


def test_a_subcommand_asks_for_the_arguments_it_was_not_given(
    calendar_add: Callable[..., Result], calendar_rows: Callable[[], pd.DataFrame],
) -> None:
    result = calendar_add("set-amount", "2028-01", answers=["salary_bill", "100.00"])

    assert result.exit_code == 0, result.output
    assert "vary this row" not in result.output   # only the arguments, not the whole guide
    row = calendar_rows().query("kind == 'set_amount'").iloc[-1]
    assert (row["scheduled_name"], row["amount"]) == ("salary_bill", "100.00")


@pytest.mark.usefixtures("without_prototype_defaults")
def test_a_subcommand_given_all_its_arguments_asks_nothing(
    calendar_add: Callable[..., Result],
) -> None:
    result = calendar_add("retire-scheduled", "2029-06-30", "salary_bill", answers=[])

    assert result.exit_code == 0, result.output
    assert result.output.startswith("appended retire_scheduled")


def test_group_options_before_a_kind_are_refused(test_data_dir: Path) -> None:
    result = CliRunner().invoke(main, [
        "sim", "calendar", "add", "--data-dir", str(test_data_dir),
        "retire-scheduled", "2029-06-30", "salary_bill",
    ])

    assert result.exit_code != 0
    assert "go after the kind" in result.output


@pytest.mark.parametrize("spec", calendar_guide.KINDS, ids=lambda spec: spec.command)
def test_every_guide_field_is_a_parameter_of_its_command(spec: calendar_guide.KindSpec) -> None:
    """The field definitions are data another front end can reuse, so they
    must not drift from the commands they describe."""
    command = calendar_add_group_command.commands[spec.command]
    assert command.callback is not None
    callback_params = set(inspect.signature(command.callback).parameters)
    options = {opt for param in command.params for opt in param.opts if opt.startswith("--")}
    arguments = [param.name for param in command.params if param.param_type_name == "argument"]

    passed_on = [f for f in spec.fields if not f.is_question_only]
    assert {f.param for f in passed_on} <= callback_params
    assert [f.param for f in passed_on if f.is_argument] == arguments
    # Both ways: the guide offers every option the command has, bar the two
    # that say which scenario (the guide itself is started with those) and
    # the ramp, which every kind accepts but only set-rate can use.
    not_offered = {"--scenario", "--data-dir"}
    if spec.command != "set-rate":
        not_offered.add("--vary-ramp-months")
    assert {f.option for f in passed_on if f.option} == options - not_offered


def test_guide_does_not_offer_a_distribution_that_cannot_draw_a_date(
    calendar_add: Callable[..., Result], calendar_rows: Callable[[], pd.DataFrame],
) -> None:
    result = calendar_add(answers=[
        "set-amount", "2028-01", "salary_bill", "100.00", "", "", "",
        "y", "raise_vary", "date",
        "truncated_normal", "uniform",        # refused, then a usable one
        "2028-01", "2028-06", "n", "",
    ])

    assert result.exit_code == 0, result.output
    assert "choices: discrete, triangular, uniform" in result.output
    assert calendar_rows().query("name == 'raise_vary'").iloc[0]["vary_distribution"] == "uniform"


def test_guide_asks_again_for_an_answer_a_later_change_rules_out(
    calendar_add: Callable[..., Result], calendar_rows: Callable[[], pd.DataFrame],
) -> None:
    result = calendar_add(answers=[
        "set-amount", "2028-01", "salary_bill", "100.00", "", "", "",
        "y", "raise_vary", "amount", "truncated_normal", "100", "", "90", "110", "", "", "n",
        "vary-target", "date",                # at the review: now the distribution is impossible
        "uniform", "2028-01", "2028-06",
        "",
    ])

    assert result.exit_code == 0, result.output
    assert "vary-distribution can no longer be 'truncated_normal'" in result.output
    row = calendar_rows().query("name == 'raise_vary'").iloc[0]
    assert (row["vary_target"], row["vary_distribution"]) == ("date", "uniform")
    assert (row["vary_low"], row["vary_high"]) == ("2028-01", "2028-06")


def test_guide_says_which_scenario_before_asking_anything(
    calendar_add: Callable[..., Result], test_data_dir: Path,
) -> None:
    named = calendar_add(answers=["latent-factor", "economy", "", "n", "", "cancel"])

    assert named.output.startswith("Scenario: retire_alice\n")

    defaulted = CliRunner().invoke(
        main, ["sim", "calendar", "add", "--data-dir", str(test_data_dir)],
        input="latent-factor\neconomy\n\nn\n\ncancel\n",
    )

    assert defaulted.output.startswith(
        "Using scenario retire_alice\n  "
    )
