"""`sim items`: a run's ledger, filtered as events.ipynb filters it (#189)."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from analysis.cli import main
from analysis.sim.connection import RunConnection


def _items(runfile: Path, *args: str):
    return CliRunner().invoke(main, ["sim", "items", *args, "--runfile", str(runfile)],
                              terminal_width=200)


def test_no_account_shows_the_whole_ledger(two_lot_run_file: Path) -> None:
    result = _items(two_lot_run_file, "--start", "2026-06", "--end", "2026-06")
    assert result.exit_code == 0, result.output
    assert "accounts: all  window: 2026-06-30 through 2026-06-30" in result.output
    assert "From" in result.output and "To" in result.output and "Direction" not in result.output
    with RunConnection(two_lot_run_file) as rc:
        june = rc.items("transfer_event")
        june = june[june["date"].str.startswith("2026-06")]
    assert f"ledger events: {len(june):,}," in result.output


def test_dates_default_to_the_whole_run(two_lot_run_file: Path) -> None:
    result = _items(two_lot_run_file, "--reason", "refill")
    assert result.exit_code == 0, result.output
    assert "window: start through end" in result.output
    with RunConnection(two_lot_run_file) as rc:
        refills = rc.items("transfer_event", reason_kind="refill")
    assert f"ledger events: {len(refills):,}," in result.output
    assert refills["date"].min() in result.output and refills["date"].max() in result.output


def test_a_refill_names_the_debit_that_caused_it(two_lot_run_file: Path) -> None:
    with RunConnection(two_lot_run_file) as rc:
        events = rc.items("transfer_event").set_index("id")
    refill = events[events["reason_kind"] == "refill"].iloc[0]
    month = refill["date"][:7]
    result = _items(two_lot_run_file, "--reason", "refill", "--start", month)
    assert result.exit_code == 0, result.output
    row = next(line for line in result.output.splitlines() if f"{int(refill.name)} " in line.lstrip("│ ")[:12])
    assert str(int(refill["parent_event_id"])) in row.split("│")[-2]


def test_one_account_shows_its_running_balance(two_lot_run_file: Path) -> None:
    result = _items(two_lot_run_file, "--account", "Asset_Checking", "--start", "2026-05")
    assert result.exit_code == 0, result.output
    assert "Balance" in result.output and "Direction" in result.output


def test_filters_by_rule_and_tax_character(two_lot_run_file: Path) -> None:
    result = _items(two_lot_run_file, "--rule", "lot_b_draw", "--start", "2026-05", "--months", "3")
    assert result.exit_code == 0, result.output
    assert "rule: lot_b_draw" in result.output
    character = _items(two_lot_run_file, "--tax-character", "return_of_capital", "--start", "2026-05",
                       "--months", "3")
    assert character.exit_code == 0, character.output
    assert "return_of_capital" in character.output and "ltcg" not in character.output


def test_filters_by_tag(report_run_file: Path) -> None:
    result = _items(report_run_file, "--tag", "budget:fun", "--start", "2026-06")
    assert result.exit_code == 0, result.output
    assert "living" in result.output and "Expense_Healthcare" not in result.output


def test_usage_errors(two_lot_run_file: Path) -> None:
    lot = _items(two_lot_run_file, "--lot", "b")
    assert lot.exit_code != 0 and "--lot narrows one --account" in lot.output
    months = _items(two_lot_run_file, "--months", "2")
    assert months.exit_code != 0 and "--months counts from --start" in months.output
    tag = _items(two_lot_run_file, "--tag", "core")
    assert tag.exit_code != 0 and "role:name" in tag.output
    unknown = _items(two_lot_run_file, "--account", "Nope")
    assert unknown.exit_code != 0 and "unknown account" in unknown.output
