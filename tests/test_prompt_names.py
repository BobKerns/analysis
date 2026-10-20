"""A name left off a command's line, or mistyped at a terminal, is asked for,
with completion (#240)."""
# cspell:ignore Savngs Jiont alcie defalt csh

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import click
import pandas as pd
import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.sim import cli as sim_cli
from analysis.sim import completion, names
from analysis.sim.cli import sim_group_command
from analysis.sim.loader import BATCH_MANIFEST_FILENAME


@pytest.fixture
def sim(test_data_dir: Path) -> Callable[..., Result]:
    """Run a ``sim`` command against the private data copy; ``input`` answers
    its prompts."""
    def run(*args: str, input: str | None = None) -> Result:  # noqa: A002 -- CliRunner's name
        return CliRunner().invoke(
            main, ["sim", *args, "--data-dir", str(test_data_dir)], input=input,
        )
    return run


def _column(path: Path, column: str) -> list[str]:
    return list(pd.read_csv(path, dtype=str, keep_default_na=False)[column])


def test_rename_asks_for_both_names(sim: Callable[..., Result], test_data_dir: Path) -> None:
    result = sim("account", "rename", "--scenario", "retire_alice", input="Asset_Savings\nAsset_Rainy_Day\n")
    assert result.exit_code == 0, result.output
    assert "accounts: " in result.output  # the choices are listed first
    accounts = names.account_names(test_data_dir, "retire_alice")
    assert "Asset_Rainy_Day" in accounts and "Asset_Savings" not in accounts


def test_a_prompted_name_is_asked_again_until_it_is_one(
    sim: Callable[..., Result], test_data_dir: Path,
) -> None:
    result = sim("scheduled", "rename", "--scenario", "retire_alice",
                 input="no_such_rule\nliving\ncost_of_living\n")
    assert result.exit_code == 0, result.output
    assert "one of: " in result.output
    assert "cost_of_living" in names.scheduled_names(test_data_dir, "retire_alice")


def test_lot_rename_asks_account_then_its_lots(sim: Callable[..., Result], test_data_dir: Path) -> None:
    result = sim("account", "lot", "rename", "--scenario", "retire_alice",
                 input="Asset_Brokerage_Joint\ndefault\ncore\n")
    assert result.exit_code == 0, result.output
    assert names.lot_names(test_data_dir, "retire_alice", "Asset_Brokerage_Joint") == ["core"]


def test_tag_add_asks_for_the_account_and_tags(sim: Callable[..., Result], test_data_dir: Path) -> None:
    result = sim("account", "tag", "add", "--scenario", "retire_alice",
                 input="Asset_Checking\nbucket:cash use:spending\n")
    assert result.exit_code == 0, result.output
    assert {"bucket:cash", "use:spending"} <= set(names.tag_tokens(test_data_dir, "retire_alice"))
    listed = sim("tags", "items", "--scenario", "retire_alice", input="bucket:cash\n")
    assert listed.exit_code == 0, listed.output
    assert "Asset_Checking" in listed.output


def test_calendar_tag_add_asks_for_the_tags(sim: Callable[..., Result], test_data_dir: Path) -> None:
    result = sim("calendar", "tag", "add", "salary_alice", "--scenario", "retire_alice",
                 input="event:retire\n")
    assert result.exit_code == 0, result.output
    assert "event:retire" in names.tag_tokens(test_data_dir, "retire_alice")


def test_scheduled_delete_asks_which(sim: Callable[..., Result], test_data_dir: Path) -> None:
    result = sim("scheduled", "delete", "--yes", "--scenario", "retire_alice", input="housing\n")
    assert result.exit_code == 0, result.output
    assert "housing" not in names.scheduled_names(test_data_dir, "retire_alice")


def test_scenario_create_and_delete_ask(sim: Callable[..., Result], test_data_dir: Path) -> None:
    created = sim("scenario", "create", input="kid\nretire_alice\n")
    assert created.exit_code == 0, created.output
    assert _column(test_data_dir / "scenarios" / "kid" / "scenario.csv", "parent_scenario") == ["retire_alice"]
    deleted = sim("scenario", "delete", "--yes", input="kid\n")
    assert deleted.exit_code == 0, deleted.output
    assert not (test_data_dir / "scenarios" / "kid").exists()


def test_ensemble_rename_offers_only_ensembles(
    sim: Callable[..., Result], test_data_dir: Path, test_ensemble_scenario: str,
) -> None:
    result = sim("ensemble", "rename", input=f"{test_ensemble_scenario}\nrecipe_9\n")
    assert result.exit_code == 0, result.output
    assert f"ensembles: {test_ensemble_scenario}\n" in result.output
    assert (test_data_dir / "scenarios" / "recipe_9").is_dir()


def test_person_edit_asks_which_person(sim: Callable[..., Result], test_data_dir: Path) -> None:
    result = sim("person", "edit", "--display", "Alice A.", input="alice\n")
    assert result.exit_code == 0, result.output
    frame = pd.read_csv(test_data_dir / "persons.csv", dtype=str, keep_default_na=False)
    assert frame.set_index("id").at["alice", "display"] == "Alice A."


def test_holdings_asks_for_the_account(sim: Callable[..., Result]) -> None:
    result = sim("account", "holdings", "--opening", "--scenario", "retire_alice",
                 input="Asset_Brokerage_Joint\n")
    assert result.exit_code == 0, result.output
    assert "holdings of Asset_Brokerage_Joint" in result.output


def test_nothing_to_choose_from_says_so(sim: Callable[..., Result]) -> None:
    result = sim("itemization", "show", "--scenario", "retire_alice")
    assert result.exit_code != 0
    assert "there are no itemizations to choose from" in result.output
    result = sim("person", "lifespan", "clear", "--scenario", "retire_alice")
    assert "there are no persons with a lifespan set here to choose from" in result.output


def test_ensemble_summary_with_no_runs_says_so(
    test_data_dir: Path, runs_dir: Path,
) -> None:
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "summary", "--data-dir", str(test_data_dir), "--runs-dir", str(runs_dir),
    ])
    assert result.exit_code != 0
    assert "there are no ensembles with runs to choose from" in result.output


def test_a_name_given_on_the_line_is_not_asked(sim: Callable[..., Result], test_data_dir: Path) -> None:
    result = sim("account", "rename", "Asset_Savings", "Asset_Rainy_Day", "--scenario", "retire_alice")
    assert result.exit_code == 0, result.output
    assert "accounts: " not in result.output


# Required params that aren't names a scenario defines: a date, and the
# inputs of the two importers, which are run from scripts.
_STILL_REQUIRED = {"sim items start", "sim scheduled import-baseline from_account",
                   "sim account import-quicken quicken_file"}


def test_only_non_names_are_required() -> None:
    def required(command: click.Command, path: str) -> set[str]:
        found = {f"{path} {p.name}" for p in command.params if p.required}
        if isinstance(command, click.Group):
            for name, sub in command.commands.items():
                found |= required(sub, f"{path} {'account' if name == 'accounts' else name}")
        return found

    assert required(sim_group_command, "sim") == _STILL_REQUIRED


# ---------- a mistyped name is asked for again, at a terminal ----------

@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run as if stdin were a terminal, where a mistyped name is re-asked."""
    monkeypatch.setattr(sim_cli, "_interactive", lambda: True)


@pytest.mark.usefixtures("terminal")
def test_a_mistyped_name_is_asked_again(sim: Callable[..., Result], test_data_dir: Path) -> None:
    result = sim("account", "rename", "Asset_Savngs", "Asset_Rainy_Day", "--scenario", "retire_alice",
                 input="Asset_Savings\n")
    assert result.exit_code == 0, result.output
    assert "unknown account: 'Asset_Savngs'" in result.output
    assert "Asset_Rainy_Day" in names.account_names(test_data_dir, "retire_alice")


@pytest.mark.usefixtures("terminal")
def test_the_scenario_is_asked_again_first(sim: Callable[..., Result]) -> None:
    # The account can only be checked once the scenario is one.
    result = sim("account", "holdings", "Asset_Brokerage_Jiont", "--opening", "--scenario", "retire_alcie",
                 input="retire_alice\nAsset_Brokerage_Joint\n")
    assert result.exit_code == 0, result.output
    assert result.output.index("unknown scenario") < result.output.index("unknown account")
    assert "holdings of Asset_Brokerage_Joint" in result.output


@pytest.mark.usefixtures("terminal")
def test_each_mistyped_value_of_a_list_is_asked_again(
    sim: Callable[..., Result], test_data_dir: Path,
) -> None:
    assert sim("account", "tag", "add", "Asset_Checking", "bucket:cash", "use:spending",
               "--scenario", "retire_alice").exit_code == 0
    result = sim("account", "tag", "rm", "Asset_Checking", "bucket:csh", "use:spending",
                 "--scenario", "retire_alice", input="bucket:cash\n")
    assert result.exit_code == 0, result.output
    assert not {"bucket:cash", "use:spending"} & set(names.tag_tokens(test_data_dir, "retire_alice"))


@pytest.mark.usefixtures("terminal")
def test_a_lot_is_checked_against_its_account(sim: Callable[..., Result], test_data_dir: Path) -> None:
    result = sim("account", "lot", "rename", "Asset_Brokerage_Joint", "defalt", "core",
                 "--scenario", "retire_alice", input="default\n")
    assert result.exit_code == 0, result.output
    assert "unknown lot: 'defalt'" in result.output
    assert names.lot_names(test_data_dir, "retire_alice", "Asset_Brokerage_Joint") == ["core"]


@pytest.mark.usefixtures("terminal")
def test_a_new_tag_is_not_a_mistyped_one(sim: Callable[..., Result]) -> None:
    result = sim("account", "tag", "add", "Asset_Checking", "brand:new", "--scenario", "retire_alice")
    assert result.exit_code == 0, result.output
    assert "unknown" not in result.output


def test_off_a_terminal_a_mistyped_name_fails_as_before(sim: Callable[..., Result]) -> None:
    result = sim("account", "rename", "Asset_Savngs", "Asset_Rainy_Day", "--scenario", "retire_alice",
                 input="Asset_Savings\n")
    assert result.exit_code != 0
    assert "no account 'Asset_Savngs' in retire_alice" in result.output  # the command's own error
    assert "unknown account" not in result.output


@pytest.mark.usefixtures("terminal")
def test_a_tag_filter_may_match_nothing(sim: Callable[..., Result]) -> None:
    assert sim("account", "tag", "add", "Asset_Checking", "bucket:cash", "--scenario", "retire_alice").exit_code == 0
    result = sim("tags", "items", "unused:*", "--scenario", "retire_alice")
    assert result.exit_code == 0, result.output
    assert "unknown" not in result.output


@pytest.mark.usefixtures("terminal")
def test_a_tag_being_removed_is_not_a_pattern(sim: Callable[..., Result], test_data_dir: Path) -> None:
    assert sim("account", "tag", "add", "Asset_Checking", "bucket:cash", "--scenario", "retire_alice").exit_code == 0
    result = sim("account", "tag", "rm", "Asset_Checking", "bucket:*", "--scenario", "retire_alice",
                 input="bucket:cash\n")
    assert result.exit_code == 0, result.output
    assert "unknown tag: 'bucket:*'" in result.output
    assert "bucket:cash" not in names.tag_tokens(test_data_dir, "retire_alice")


@pytest.mark.usefixtures("terminal")
def test_fixed_is_a_scheduled_rules_series(sim: Callable[..., Result]) -> None:
    result = sim("scheduled", "edit", "living", "--series", "fixed", "--scenario", "retire_alice")
    assert result.exit_code == 0, result.output
    assert "unknown" not in result.output


@pytest.mark.usefixtures("terminal")
def test_a_run_files_names_are_not_checked_against_the_scenario(
    sim: Callable[..., Result], tmp_path: Path,
) -> None:
    runfile = tmp_path / "other.sqlite3"
    runfile.write_bytes(b"")
    result = sim("account", "history", "Asset_Elsewhere", "--runfile", str(runfile))
    assert "unknown account" not in result.output


def test_a_compared_variants_names_count(test_data_dir: Path) -> None:
    ctx = click.Context(sim_group_command.commands["ensemble"].commands["compare"])  # ty: ignore[unresolved-attribute] -- a Group
    ctx.params = {"baseline": "base", "variant": "retire_alice", "data_dir": str(test_data_dir)}
    assert "Asset_Checking" in completion.ACCOUNT.choices(ctx)  # base has no accounts


@pytest.mark.usefixtures("terminal")
def test_joint_is_an_owner(sim: Callable[..., Result]) -> None:
    result = sim("account", "edit", "Asset_Checking", "--owner", "joint", "--scenario", "retire_alice")
    assert result.exit_code == 0, result.output
    assert "unknown" not in result.output


def test_tags_given_without_the_name_ask_for_the_name(
    sim: Callable[..., Result], test_data_dir: Path,
) -> None:
    # click hands the first word to NAME; a tag there means NAME was left out.
    result = sim("account", "tag", "add", "bucket:cash", "--scenario", "retire_alice", input="Asset_Checking\n")
    assert result.exit_code == 0, result.output
    assert "accounts: " in result.output
    listed = sim("tags", "items", "bucket:cash", "--scenario", "retire_alice")
    assert "Asset_Checking" in listed.output
    result = sim("calendar", "tag", "add", "event:retire", "--scenario", "retire_alice", input="salary_alice\n")
    assert result.exit_code == 0, result.output
    assert "event:retire" in names.tag_tokens(test_data_dir, "retire_alice")


def test_tags_items_asks_for_one_tag(sim: Callable[..., Result]) -> None:
    assert sim("account", "tag", "add", "Asset_Checking", "bucket:cash", "--scenario", "retire_alice").exit_code == 0
    result = sim("tags", "items", "--scenario", "retire_alice", input="bucket:cash use:spending\nbucket:cash\n")
    assert result.exit_code == 0, result.output
    assert "  one tag: " in result.output
    assert "Asset_Checking" in result.output


def test_holdings_offers_only_accounts_that_hold_lots(sim: Callable[..., Result]) -> None:
    result = sim("account", "holdings", "--opening", "--scenario", "retire_alice", input="Asset_Checking\n")
    assert result.exit_code == 0, result.output
    offered = next(line for line in result.output.splitlines() if line.startswith("accounts: "))
    assert "Asset_Checking" in offered
    assert "Income_Salary_Alice" not in offered and "Expense" not in offered


def test_an_ensemble_report_with_a_batch_offers_ensembles_that_have_it(
    test_data_dir: Path, runs_dir: Path,
) -> None:
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "summary", "--batch", "no_such_batch",
        "--data-dir", str(test_data_dir), "--runs-dir", str(runs_dir),
    ])
    assert result.exit_code != 0
    assert "there are no ensembles with a batch no_such_batch to choose from" in result.output


def test_a_name_with_a_colon_is_not_taken_for_a_tag(
    sim: Callable[..., Result], test_data_dir: Path,
) -> None:
    renamed = sim("scheduled", "rename", "housing", "home:rent", "--scenario", "retire_alice")
    assert renamed.exit_code == 0, renamed.output
    result = sim("scheduled", "tag", "add", "home:rent", "bucket:fixed", "--scenario", "retire_alice")
    assert result.exit_code == 0, result.output
    assert "scheduled rules: " not in result.output  # nothing was asked
    assert "bucket:fixed" in names.tag_tokens(test_data_dir, "retire_alice")



@pytest.mark.usefixtures("terminal")
def test_at_a_terminal_a_tag_first_asks_for_the_name_not_again(
    sim: Callable[..., Result], test_data_dir: Path,
) -> None:
    # The leading tag isn't reported as a mistyped account: it's moved to the tags.
    result = sim("account", "tag", "add", "bucket:cash", "--scenario", "retire_alice", input="Asset_Checking\n")
    assert result.exit_code == 0, result.output
    assert "unknown" not in result.output
    assert "bucket:cash" in names.tag_tokens(test_data_dir, "retire_alice")


def test_series_rename_offers_only_series_it_can_rename(sim: Callable[..., Result]) -> None:
    result = sim("series", "rename", "--scenario", "retire_alice", input="brokerage_growth\nbroker_growth\n")
    assert result.exit_code == 0, result.output
    offered = next(line for line in result.output.splitlines() if line.startswith("series: "))
    choices = offered.removeprefix("series: ").split(", ")
    assert "brokerage_growth" in choices
    assert "cpi" not in choices  # in the shared aux/ folder
    assert "stock" not in choices  # an asset class


def test_lot_names_can_leave_out_the_implicit_default(data_dir: Path) -> None:
    assert names.lot_names(data_dir, "retire_alice", "Income_Salary_Alice") == ["default"]
    assert names.lot_names(data_dir, "retire_alice", "Income_Salary_Alice", implicit=False) == []


def test_a_named_batch_is_offered_only_when_it_holds_runs_of_the_mode(tmp_path: Path) -> None:
    for recipe, mode in (("mc_recipe", "monte_carlo"), ("oat_recipe", "sensitivity")):
        batch = tmp_path / recipe / "b1"
        batch.mkdir(parents=True)
        (batch / BATCH_MANIFEST_FILENAME).write_text(json.dumps({"mode": mode}))
    (tmp_path / "stale_recipe" / "b1").mkdir(parents=True)  # no manifest, no members
    result = CliRunner().invoke(click.Command("ask", callback=lambda: click.echo(
        sim_cli._ask_ensemble_run(tmp_path, "monte_carlo", batch="b1"))), input="mc_recipe\n")
    assert result.exit_code == 0, result.output
    assert "ensembles with a batch b1: mc_recipe\n" in result.output

