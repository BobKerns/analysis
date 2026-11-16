"""A run's negative-balance warnings, read back for `sim explain` and the
notebooks (#431)."""

from __future__ import annotations

import html
import json
import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from analysis.sim import shortfalls
from analysis.sim.connection import RunConnection
from analysis.sim.notebook_ui.ensemble_widgets import batch_shortfalls, shortfall_message
from analysis.sim.notebook_ui.pickers import shortfall_banner, shortfall_summary_line


@pytest.fixture
def short_run(append_calendar_row: Callable[..., None],
              run_test_copy: Callable[[], Path]) -> Path:
    """The private retire_alice copy, run with a bill far beyond what
    checking and its refill chain hold: checking goes negative and stays so."""
    append_calendar_row(
        date="2027-03-31", kind="one_off_transfer", name="huge_bill",
        account_1="Asset_Checking", account_2="Expense_Living",
        amount="10000000.00", tax_character="none",
    )
    return run_test_copy()


@pytest.mark.allow_negative_balance
def test_negative_balances_read_the_recorded_warning_and_its_lowest_balance(
    short_run: Path,
) -> None:
    found = shortfalls.run_negative_balances(short_run)
    assert len(found) == 1
    (warning,) = found
    assert warning.month == "2027-03-31"
    assert warning.label == "Asset_Checking"
    assert warning.balance_cents < 0
    # Spending goes on from an empty account, so it only sinks further.
    assert warning.lowest_cents < warning.balance_cents
    assert warning.lowest_month > warning.month
    assert "refill chain" in warning.refill or "no refill chain" in warning.refill
    (line,) = shortfalls.warning_lines(found)
    assert line.startswith("2027-03-31  Asset_Checking  -$")
    assert f"lowest {shortfalls.compact_cents(warning.lowest_cents)} at {warning.lowest_month}" in line


@pytest.mark.allow_negative_balance
def test_each_warning_looks_for_its_lowest_only_until_the_next(
    short_run: Path, tmp_path: Path,
) -> None:
    """An account can go negative more than once: each warning's lowest
    balance is looked for up to its account's next warning."""
    run = shutil.copy(short_run, tmp_path / "twice.sqlite3")
    con = sqlite3.connect(run)
    try:
        (first,) = shortfalls.recorded_events(con)
        months = [m for (m,) in con.execute(
            "SELECT month FROM simulation_step WHERE month > ? ORDER BY month", (first["month"],))]
        second = {**first, "month": months[len(months) // 2]}
        con.execute("UPDATE simulation_run SET run_args_json = json_set(run_args_json, "
                    "'$.negative_balance_warnings', json(?)) WHERE id = 1",
                    (json.dumps([first, second]),))
        con.commit()
        early, late = shortfalls.negative_balances(con)
    finally:
        con.close()
    assert early.month <= early.lowest_month < second["month"] <= late.lowest_month


@pytest.mark.allow_negative_balance
def test_notebook_banner_lists_the_warnings_as_sim_explain_does(short_run: Path) -> None:
    with RunConnection(short_run) as rc:
        banner = shortfall_banner(rc, "retire_alice")
        summary = shortfall_summary_line("retire_alice", rc)
        lines = shortfalls.warning_lines(rc.negative_balances())
    assert banner is not None
    assert "This run went negative once" in banner
    assert html.escape(lines[0]) in banner
    assert summary is not None
    assert summary.startswith("retire_alice: negative once from 2027-03-31; lowest Asset_Checking -$")


def test_a_run_without_shortfalls_shows_no_banner(retire_alice_run_file: Path) -> None:
    with RunConnection(retire_alice_run_file) as rc:
        assert rc.negative_balances() == []
        assert shortfall_banner(rc) is None
        assert shortfall_summary_line("retire_alice", rc) is None
    assert shortfall_banner(None) is None


@pytest.mark.allow_negative_balance
def test_ensemble_counts_members_with_shortfalls_and_the_worst(
    short_run: Path, retire_alice_run_file: Path, tmp_path: Path,
) -> None:
    batch = tmp_path / "batch"
    batch.mkdir()
    shutil.copy(retire_alice_run_file, batch / "member_0000.sqlite3")
    shutil.copy(short_run, batch / "member_0001.sqlite3")
    found = batch_shortfalls(batch, ["member_0000", "member_0001"])
    assert (found.members, found.with_warnings, found.worst_member) == (2, 1, "member_0001")
    assert found.worst is not None
    assert found.worst.lowest_cents == shortfalls.run_negative_balances(short_run)[0].lowest_cents
    message = shortfall_message(found)
    assert message is not None and message.startswith("**1 of 2 members went negative")
    assert shortfall_message(batch_shortfalls(batch, ["member_0000"])) is None


@pytest.mark.parametrize(("subkind", "lot", "shown"), [
    ("cash", "default", "Checking"),
    ("cash", "reserve", "Checking (reserve)"),
    ("brokerage_taxable", "default", "Checking (default)"),
])
def test_account_with_lot_names_a_lot_worth_showing(subkind: str, lot: str, shown: str) -> None:
    assert shortfalls.account_with_lot("Checking", subkind, lot) == shown


@pytest.mark.allow_negative_balance
def test_scenario_picker_shows_the_banner_under_the_picker(
    append_calendar_row: Callable[..., None], test_data_dir: Path, intent_dir: Path,
    runs_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner

    from analysis.cli import main
    from analysis.sim.notebook_ui import pickers

    append_calendar_row(
        date="2027-03-31", kind="one_off_transfer", name="huge_bill",
        account_1="Asset_Checking", account_2="Expense_Living",
        amount="10000000.00", tax_character="none",
    )
    loaded = CliRunner().invoke(main, [
        "sim", "load", "retire_alice", "--data-dir", str(test_data_dir),
        "--intent-dir", str(intent_dir), "--runs-dir", str(runs_dir), "--allow-dirty-data",
    ])
    assert loaded.exit_code == 0, loaded.output
    shown: list[object] = []
    monkeypatch.setattr(pickers, "display", shown.append)
    _widget, container, _dropdown = pickers.scenario_picker(
        test_data_dir, "retire_alice", runs_dir=runs_dir)
    try:
        banners = [item for item in shown if "went negative" in str(getattr(item, "data", ""))]
        assert len(banners) == 1
    finally:
        container.close()


def test_compact_cents_stays_exact_for_huge_balances() -> None:
    assert shortfalls.compact_cents(-(2**60 + 1)) == "-$11,529,215,046,068,469.77"
    assert shortfalls.compact_cents(5) == "$0.05"
