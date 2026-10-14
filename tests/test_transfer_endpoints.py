"""Either side of a transfer (#98): an account and its lot, or an interior
chain, picked the same way whether given on the command line or typed at a
prompt, by ``sim scheduled add``, ``sim transfer add``, ``sim calendar add
transfer`` and the guided ``sim calendar add``."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.sim import calendar_guide
from analysis.sim.engine import run_scenario
from analysis.sim.loader import LoadError, LoadResult

CHAIN = "asset_checking_liquidity"  # the prototype's one interior chain, a source
INTO = "savings_first"  # a destination chain, from the ``into_chain`` fixture


@pytest.fixture
def into_chain(retire_alice_csv: Callable[[str], Path]) -> str:
    """A destination chain paying into Asset_Savings, for a transfer's to side."""
    (retire_alice_csv("chains/destination") / f"{INTO}.csv").write_text(
        "account_name,lot,maximum,percent,series,cadence\nAsset_Savings,,,,,\n")
    return INTO


@pytest.fixture
def sim(test_data_dir: Path) -> Callable[..., Result]:
    """Run a ``sim`` command on the private retire_alice copy, typing ``answers``."""
    def run(*args: str, answers: tuple[str, ...] = ()) -> Result:
        return CliRunner().invoke(
            main,
            ["sim", *args, "--data-dir", str(test_data_dir), "--scenario", "retire_alice"],
            input="".join(answer + "\n" for answer in answers),
            env={"COLUMNS": "240"},  # wide enough that a listed table cuts no name short
        )
    return run


@pytest.fixture
def last_row(retire_alice_csv: Callable[[str], Path]) -> Callable[[str], pd.Series]:
    def read(name: str) -> pd.Series:
        return pd.read_csv(retire_alice_csv(name), dtype=str, keep_default_na=False).iloc[-1]
    return read


@pytest.mark.parametrize(("lots", "needed"), [
    (None, False),                              # a chain
    ((False, ["default"]), False),
    ((False, ["default", "travel"]), True),
    ((False, ["emergency"]), True),
    ((True, ["default"]), True),                # investment accounts always name it
])
def test_a_lot_is_needed_unless_default_is_an_ordinary_accounts_only_lot(
    lots: tuple[bool, list[str]] | None, needed: bool,
) -> None:
    assert calendar_guide.lot_needed(lots) is needed


def test_scheduled_add_takes_a_chain_on_the_command_line(
    sim: Callable[..., Result], last_row: Callable[[str], pd.Series],
) -> None:
    result = sim("scheduled", "add", "2030-01", CHAIN, "Asset_Checking", "100.00")

    assert result.exit_code == 0, result.output
    row = last_row("scheduled.csv")
    assert (row["from_account"], row["from_lot"], row["to_account"]) == (CHAIN, "", "Asset_Checking")


def test_scheduled_add_offers_chains_at_the_prompt(
    sim: Callable[..., Result], last_row: Callable[[str], pd.Series],
) -> None:
    result = sim("scheduled", "add",
                 answers=("2030-01", CHAIN, "Asset_Checking", "100.00"))

    assert result.exit_code == 0, result.output
    assert f"chains: {CHAIN}" in result.output
    assert "lot:" not in result.output   # neither side asks for one
    assert last_row("scheduled.csv")["from_account"] == CHAIN


def test_the_account_list_shows_the_lots_an_account_must_name(
    sim: Callable[..., Result], savings_lots: tuple[str, str],
) -> None:
    result = sim("transfer", "add", answers=("2030-06",))   # ends at the account prompt

    assert "Asset_Savings (lots: emergency, travel)" in result.output
    assert "Asset_Checking," in result.output   # no lots shown where none need naming


@pytest.mark.parametrize("command", [("scheduled", "add"), ("transfer", "add")])
def test_an_ordinary_account_without_a_default_lot_is_asked_for_one(
    command: tuple[str, ...], sim: Callable[..., Result],
    last_row: Callable[[str], pd.Series], savings_lots: tuple[str, str],
) -> None:
    """The issue's case: the lot was left blank, read as ``default`` at load,
    and failed there. Now it is asked, and a wrong answer asked again."""
    result = sim(*command, "2030-01", "Asset_Checking", "Asset_Savings", "50.00",
                 answers=("nope", "travel"))

    assert result.exit_code == 0, result.output
    assert "lots of Asset_Savings: emergency, travel" in result.output
    assert "not one of: emergency, travel" in result.output
    file, lot_column = (("scheduled.csv", "to_lot") if command[0] == "scheduled"
                        else ("calendar.csv", "lot_2"))
    assert last_row(file)[lot_column] == "travel"


@pytest.mark.parametrize("command", [("scheduled", "add"), ("transfer", "add")])
def test_a_lot_on_the_command_line_is_checked_against_the_same_lots(
    command: tuple[str, ...], sim: Callable[..., Result], savings_lots: tuple[str, str],
) -> None:
    result = sim(*command, "2030-01", "Asset_Checking", "Asset_Savings", "50.00",
                 "--to-lot", "nope")

    assert result.exit_code != 0
    assert "'Asset_Savings' has no lot 'nope'; its lots: emergency, travel" in result.output


def test_a_prompt_asks_again_for_an_unknown_account(
    sim: Callable[..., Result], last_row: Callable[[str], pd.Series],
) -> None:
    result = sim("transfer", "add",
                 answers=("2030-06", "Nope", "Asset_Checking", "Asset_Savings", "42.00"))

    assert result.exit_code == 0, result.output
    assert "unknown from account or chain: 'Nope'" in result.output
    assert last_row("calendar.csv")["account_1"] == "Asset_Checking"


def test_an_unknown_account_on_the_command_line_fails(sim: Callable[..., Result]) -> None:
    result = sim("scheduled", "add", "2030-01", "Asset_Checking", "Nope", "1.00")

    assert result.exit_code != 0
    assert "unknown to account or chain: 'Nope'" in result.output


def test_a_chain_takes_no_lot(sim: Callable[..., Result]) -> None:
    result = sim("transfer", "add", "2030-06", CHAIN, "Asset_Checking", "1.00",
                 "--from-lot", "default")

    assert result.exit_code != 0
    assert f"'{CHAIN}' is a chain, so --from-lot must be blank" in result.output


def test_a_transfer_cannot_run_from_one_chain_into_another(sim: Callable[..., Result]) -> None:
    given = sim("scheduled", "add", "2030-01", CHAIN, CHAIN, "1.00")
    assert given.exit_code != 0
    assert "can't run from one chain into another" in given.output

    typed = sim("scheduled", "add",
                answers=("2030-01", CHAIN, CHAIN, "Asset_Checking", "1.00"))
    assert typed.exit_code == 0, typed.output
    assert "can't run from one chain into another" in typed.output


def test_each_side_takes_only_chains_of_its_role(sim: Callable[..., Result], into_chain: str) -> None:
    """A from side names a source chain, a to side a destination (#143)."""
    backwards = sim("scheduled", "add", "2030-01", into_chain, "Asset_Checking", "1.00")
    assert backwards.exit_code != 0
    assert f"'{into_chain}' is a destination chain; only a source chain or an account fits here" in backwards.output
    typed = sim("scheduled", "add", answers=("2030-01", "Asset_Checking", CHAIN, into_chain, "1.00"))
    assert typed.exit_code == 0, typed.output
    assert f"'{CHAIN}' is a source chain; only a destination chain or an account fits here" in typed.output


@pytest.mark.parametrize("file", ["scheduled.csv", "calendar.csv"])
def test_the_loader_refuses_a_transfer_between_two_chains(
    file: str, retire_alice_csv: Callable[[str], Path],
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    if file == "calendar.csv":
        append_calendar_row(date="2030-06", kind="one_off_transfer", account_1=CHAIN,
                            account_2=CHAIN, amount="1.00", tax_character="none")
    else:
        scheduled = pd.read_csv(retire_alice_csv(file), dtype=str, keep_default_na=False)
        added = pd.DataFrame([{
            "name": "chain_to_chain", "from_account": CHAIN, "to_account": CHAIN,
            "cadence": "monthly", "amount_rule_kind": "fixed_amount", "amount": "1.00",
            "tax_character": "none", "start": "2030-01", "priority": "200", "meta_json": "{}",
        }])
        pd.concat([scheduled, added], ignore_index=True).fillna("").to_csv(
            retire_alice_csv(file), index=False)

    with pytest.raises(LoadError) as error:
        load_test_copy()
    assert any("may not both be chains" in issue for issue in error.value.issues)


@pytest.mark.parametrize(("source", "destination", "leg"), [
    # Out of a source chain is a draw down its steps, Asset_Savings first.
    (CHAIN, "Asset_Checking", ("Asset_Savings", "Asset_Checking")),
    # Into a destination chain is a sweep into its steps.
    ("Asset_Checking", INTO, ("Asset_Checking", "Asset_Savings")),
])
def test_a_one_off_transfer_through_a_chain_is_traced_to_its_rule(
    source: str, destination: str, leg: tuple[str, str],
    sim: Callable[..., Result], load_test_copy: Callable[[], LoadResult], runs_dir: Path,
    into_chain: str,  # noqa: ARG001
) -> None:
    """Calendar transfers take chains now (#98), and run like a fixed_amount
    rule's, every leg traced to the synthesized rule (a sweep's legs too, not
    left as month-end overflow)."""
    added = sim("calendar", "add", "transfer", "2027-03", source, destination, "1234.00")
    assert added.exit_code == 0, added.output

    run = run_scenario(load_test_copy().intent_path, runs_dir / "chain.sqlite3", run_by="TEST",
                       allow_dirty_data=True, allow_dirty_code=True)
    with sqlite3.connect(run) as con:
        legs = con.execute(
            "SELECT fa.name, ta.name, te.amount FROM transfer_event te "
            "JOIN scheduled_transaction s ON s.id = te.reason_id AND te.reason_kind = 'rule' "
            "JOIN account fa ON fa.id = te.from_account "
            "JOIN account ta ON ta.id = te.to_account "
            "WHERE s.created_by_event_id IS NOT NULL AND s.start LIKE '2027-03%'"
        ).fetchall()
    assert legs == [(*leg, 123400)]


def test_guide_offers_chains_and_asks_no_lot_for_one(
    sim: Callable[..., Result], last_row: Callable[[str], pd.Series],
) -> None:
    result = sim("calendar", "add", answers=(
        "transfer", "2030-06", CHAIN, "Asset_Checking",
        "42.00", "", "", "", "n", "",          # tax character, description, series blank; write
    ))

    assert result.exit_code == 0, result.output
    assert "from lot" not in result.output and "to lot" not in result.output
    row = last_row("calendar.csv")
    assert (row["account_1"], row["lot_1"], row["account_2"]) == (CHAIN, "", "Asset_Checking")


def test_guide_asks_for_the_lot_an_ordinary_account_must_name(
    sim: Callable[..., Result], last_row: Callable[[str], pd.Series],
    savings_lots: tuple[str, str],
) -> None:
    result = sim("calendar", "add", answers=(
        "transfer", "2030-06", "Asset_Savings", "nope", "travel", "Asset_Checking",
        "42.00", "", "", "", "n", "",
    ))

    assert result.exit_code == 0, result.output
    assert "not one of: emergency, travel" in result.output
    row = last_row("calendar.csv")
    assert (row["account_1"], row["lot_1"]) == ("Asset_Savings", "travel")


def test_guide_offers_no_chain_to_a_transfer_out_of_one(
    sim: Callable[..., Result],
) -> None:
    result = sim("calendar", "add", answers=(
        "transfer", "2030-06", CHAIN, CHAIN, "Asset_Checking",
        "42.00", "", "", "", "n", "",
    ))

    assert result.exit_code == 0, result.output
    assert "not one of:" in result.output


def test_transfer_list_names_a_chain(sim: Callable[..., Result]) -> None:
    added = sim("calendar", "add", "transfer", "2027-03", CHAIN, "Asset_Checking", "1234.00")
    assert added.exit_code == 0, added.output

    listed = sim("transfer", "list")
    assert listed.exit_code == 0, listed.output
    assert CHAIN in listed.output
