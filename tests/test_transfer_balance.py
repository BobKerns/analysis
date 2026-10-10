"""``transfer_balance`` calendar events (#134): move a lot's whole balance on
a date -- a 401(k) rolled into an IRA, say -- with the amount the simulated
balance at that point, not one written in advance."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.sim.engine import run_scenario
from analysis.sim.loader import LoadError, LoadResult

ROLLOVER = {
    "date": "2030-01-31", "kind": "transfer_balance",
    "account_1": "Asset_401k_Alice", "lot_1": "default",
    "account_2": "Asset_TraditionalIRA_Alice", "lot_2": "default",
    "tax_character": "none", "description": "roll the 401(k) into the IRA",
}


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


def _lot_balances(run: Path, account: str) -> dict[str, int]:
    with sqlite3.connect(run) as con:
        return dict(con.execute(
            "SELECT s.month, ls.balance FROM lot_state ls "
            "JOIN simulation_step s ON s.id = ls.step_id "
            "JOIN account_model_member m ON m.id = ls.lot_id "
            "JOIN account a ON a.id = m.account_id "
            "WHERE a.name = ? AND m.lot = 'default'",
            (account,),
        ).fetchall())


def _balance_events(run: Path) -> list[tuple[str, str, str, int, str, str | None]]:
    """Every transfer a transfer_balance event's synthesized rule posted."""
    with sqlite3.connect(run) as con:
        return con.execute(
            "SELECT te.date, fa.name, ta.name, te.amount, s.amount_rule_kind, s.description "
            "FROM transfer_event te "
            "JOIN scheduled_transaction s ON s.id = te.reason_id AND te.reason_kind = 'rule' "
            "JOIN calendar_event ce ON ce.id = s.created_by_event_id "
            "JOIN account fa ON fa.id = te.from_account "
            "JOIN account ta ON ta.id = te.to_account "
            "WHERE ce.kind = 'transfer_balance'"
        ).fetchall()


def test_the_whole_balance_moves_and_the_destination_grows_on_it(
    add_alice_401k: Callable[[str], None], append_calendar_row: Callable[..., None],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    add_alice_401k("100000.00")
    append_calendar_row(**ROLLOVER)
    run = run_scenario(load_test_copy().intent_path, runs_dir / "roll.sqlite3", run_by="TEST",
                       allow_dirty_data=True, allow_dirty_code=True)

    k401 = _lot_balances(run, "Asset_401k_Alice")
    ira = _lot_balances(run, "Asset_TraditionalIRA_Alice")
    moved = k401["2029-12-31"]
    assert moved > 10_000_000  # it grew from $100,000 before it moved
    assert _balance_events(run) == [(
        "2030-01-31", "Asset_401k_Alice", "Asset_TraditionalIRA_Alice", moved,
        "fixed_amount", "roll the 401(k) into the IRA",
    )]
    # Moved at the start of January, so January's growth accrues in the IRA,
    # and the emptied 401(k) earns nothing after.
    assert all(balance == 0 for month, balance in k401.items() if month >= "2030-01-31")
    assert ira["2030-01-31"] - ira["2029-12-31"] == moved


def test_an_empty_lot_moves_nothing(
    add_alice_401k: Callable[[str], None], append_calendar_row: Callable[..., None],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    add_alice_401k("0")
    append_calendar_row(**ROLLOVER)
    run = run_scenario(load_test_copy().intent_path, runs_dir / "empty.sqlite3", run_by="TEST",
                       allow_dirty_data=True, allow_dirty_code=True)
    assert _balance_events(run) == []
    with sqlite3.connect(run) as con:
        applied = con.execute(
            "SELECT COUNT(*) FROM applied_calendar_event ace "
            "JOIN calendar_event ce ON ce.id = ace.calendar_event_id "
            "WHERE ce.kind = 'transfer_balance'"
        ).fetchone()[0]
    assert applied == 1  # applied, as a no-op -- not skipped as unsupported


@pytest.mark.parametrize(("change", "message"), [
    ({"account_1": "asset_checking_liquidity", "lot_1": ""}, "is a chain; transfer_balance"),
    ({"account_1": "Liability_Mortgage", "lot_1": ""}, "is not an asset account"),
    ({"lot_1": ""}, "is an investment account and needs a lot_1"),
    ({"amount": "100.00"}, "transfer_balance takes no amount"),
    ({"name": "roll", "vary_target": "amount", "vary_distribution": "uniform",
      "vary_low": "1.00", "vary_high": "2.00"}, "vary_target='amount' is only valid"),
])
def test_rows_the_loader_refuses(
    change: dict[str, str], message: str,
    add_alice_401k: Callable[[str], None], append_calendar_row: Callable[..., None],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    add_alice_401k("100000.00")
    append_calendar_row(**{**ROLLOVER, **change})
    with pytest.raises(LoadError, match=message):
        load_test_copy()


def test_the_rollover_date_may_vary(
    add_alice_401k: Callable[[str], None], append_calendar_row: Callable[..., None],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    add_alice_401k("100000.00")
    append_calendar_row(**ROLLOVER, name="rollover", vary_target="date",
                        vary_distribution="uniform", vary_low="2029-01-31", vary_high="2031-12-31")
    with sqlite3.connect(load_test_copy().intent_path) as con:
        assert con.execute(
            "SELECT vary_target FROM calendar_event WHERE name = 'rollover'"
        ).fetchone() == ("date",)


def test_calendar_add_writes_the_row_and_transfer_list_shows_it(
    sim: Callable[..., Result], add_alice_401k: Callable[[str], None],
    retire_alice_csv: Callable[[str], Path],
) -> None:
    add_alice_401k("100000.00")
    added = sim(
        "calendar", "add", "transfer-balance", "2030-01", "Asset_401k_Alice",
        "Asset_TraditionalIRA_Alice", "--from-lot", "default", "--to-lot", "default",
        "--description", "rollover",
    )
    assert added.exit_code == 0, added.output
    row = pd.read_csv(retire_alice_csv("calendar.csv"), dtype=str, keep_default_na=False).iloc[-1]
    assert (row["kind"], row["account_1"], row["lot_1"], row["account_2"], row["amount"],
            row["tax_character"]) == (
        "transfer_balance", "Asset_401k_Alice", "default", "Asset_TraditionalIRA_Alice", "",
        "none",
    )

    listed = sim("transfer", "list")
    assert listed.exit_code == 0, listed.output
    line = next(line for line in listed.output.splitlines() if "rollover" in line)
    assert "Asset_401k_Alice (default)" in line and "balance" in line


def test_calendar_add_refuses_a_source_that_is_not_an_asset(
    sim: Callable[..., Result],
) -> None:
    result = sim("calendar", "add", "transfer-balance", "2030-01", "Income_Salary_Alice",
                 "Asset_Checking")
    assert result.exit_code != 0
    assert "unknown from account" in result.output


def test_calendar_add_refuses_an_amount_variation(sim: Callable[..., Result]) -> None:
    result = sim("calendar", "add", "transfer-balance", "2030-01", "Asset_Savings",
                 "Asset_Checking", "--vary-target", "amount")
    assert result.exit_code != 0
    assert "no amount to vary" in result.output


def test_calendar_edit_rewrites_the_row_in_place(
    sim: Callable[..., Result], add_alice_401k: Callable[[str], None],
    append_calendar_row: Callable[..., None], retire_alice_csv: Callable[[str], Path],
) -> None:
    add_alice_401k("100000.00")
    append_calendar_row(**ROLLOVER, name="rollover", tags="")

    def read() -> pd.DataFrame:
        return pd.read_csv(retire_alice_csv("calendar.csv"), dtype=str, keep_default_na=False)

    before = read()
    unchanged = sim("calendar", "edit", "rollover", answers=("",))
    assert unchanged.exit_code == 0, unchanged.output
    pd.testing.assert_frame_equal(read(), before)

    moved = sim("calendar", "edit", "rollover", answers=("date", "2031-06", ""))
    assert moved.exit_code == 0, moved.output
    after = read()
    assert after.iloc[-1]["date"] == "2031-06"
    assert after.drop(columns="date").equals(before.drop(columns="date"))
