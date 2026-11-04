"""``sim deduction add|list|edit|delete`` (#370): one itemized total per
tax year in the scenario's own aux/federal_deductions.csv (or
state_deductions.csv), with an itemization of the year's expenses beside it.
An inherited file is copied into the scenario before it changes."""

from __future__ import annotations

import csv
from collections.abc import Sequence
from pathlib import Path

from click.testing import CliRunner, Result

from analysis.cli import main


def _sim(data: Path, scenario: str, *args: str, answers: Sequence[str] = ()) -> Result:
    return CliRunner().invoke(
        main,
        ["sim", "deduction", args[0], "--data-dir", str(data), "--scenario", scenario, *args[1:]],
        input="".join(answer + "\n" for answer in answers),
    )


def _rows(path: Path) -> list[tuple[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return [(row["effective_date"], row["amount"]) for row in csv.DictReader(fh)]


def _items(path: Path) -> list[tuple[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return [(row["Item"], row["Value"]) for row in csv.DictReader(fh)]


def _own(data: Path, scenario: str, kind: str = "federal_deductions") -> Path:
    return data / "scenarios" / scenario / "aux" / f"{kind}.csv"


def test_add_lists_the_items_and_references_them(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "retire_alice", "add", "2028",
                  "--item", "surgery=38000", "--item", "hospital stay=9000")

    assert result.exit_code == 0, result.output
    assert "($47,000.00)" in result.output
    assert _rows(_own(test_data_dir, "retire_alice")) == [("2028-01-01", "@deductions_2028")]
    items = test_data_dir / "scenarios" / "retire_alice" / "itemizations" / "deductions_2028.csv"
    assert _items(items) == [("surgery", "38000.00"), ("hospital stay", "9000.00")]


def test_add_a_total_and_list(test_data_dir: Path) -> None:
    assert _sim(test_data_dir, "retire_alice", "add", "2029", "41000").exit_code == 0
    assert _sim(test_data_dir, "retire_alice", "add", "2027", "--item", "gifts=5000").exit_code == 0

    result = _sim(test_data_dir, "retire_alice", "list")

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert any("2027" in line and "@deductions_2027" in line and "$5,000.00" in line for line in lines)
    assert any("2029" in line and "41000.00" in line and "$41,000.00" in line for line in lines)
    # Written in year order, whatever order they were added.
    assert [d for d, _ in _rows(_own(test_data_dir, "retire_alice"))] == ["2027-01-01", "2029-01-01"]


def test_add_asks_for_the_items(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "retire_alice", "add", "2028",
                  answers=["surgery", "38000", "drugs", "3000", ""])

    assert result.exit_code == 0, result.output
    items = test_data_dir / "scenarios" / "retire_alice" / "itemizations" / "deductions_2028.csv"
    assert _items(items) == [("surgery", "38000.00"), ("drugs", "3000.00")]


def test_add_asks_for_a_total_when_no_items_are_given(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "retire_alice", "add", "2028", answers=["", "12500"])

    assert result.exit_code == 0, result.output
    assert _rows(_own(test_data_dir, "retire_alice")) == [("2028-01-01", "12500.00")]


def test_add_refuses_a_year_already_there(test_data_dir: Path) -> None:
    assert _sim(test_data_dir, "retire_alice", "add", "2028", "100").exit_code == 0

    result = _sim(test_data_dir, "retire_alice", "add", "2028", "200")

    assert result.exit_code != 0
    assert "sim deduction edit 2028" in result.output
    assert _rows(_own(test_data_dir, "retire_alice")) == [("2028-01-01", "100.00")]


def test_state_deductions_go_in_their_own_file(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "retire_alice", "add", "2028", "--state", "--item", "x=10")

    assert result.exit_code == 0, result.output
    assert _rows(_own(test_data_dir, "retire_alice", "state_deductions")) == [
        ("2028-01-01", "@state_deductions_2028")]
    assert not _own(test_data_dir, "retire_alice").exists()


def test_a_child_starts_from_the_rows_it_inherits(test_data_dir: Path) -> None:
    assert _sim(test_data_dir, "retire_alice", "add", "2027", "5000").exit_code == 0
    created = CliRunner().invoke(main, ["sim", "scenario", "create", "child", "retire_alice",
                                        "--data-dir", str(test_data_dir)])
    assert created.exit_code == 0, created.output

    result = _sim(test_data_dir, "child", "add", "2028", "7000")

    assert result.exit_code == 0, result.output
    assert "inherited from retire_alice" in result.output
    assert _rows(_own(test_data_dir, "child")) == [("2027-01-01", "5000.00"), ("2028-01-01", "7000.00")]
    assert _rows(_own(test_data_dir, "retire_alice")) == [("2027-01-01", "5000.00")]


def test_edit_replaces_the_items(test_data_dir: Path) -> None:
    assert _sim(test_data_dir, "retire_alice", "add", "2028", "--item", "a=1").exit_code == 0

    result = _sim(test_data_dir, "retire_alice", "edit", "2028", "--item", "a=2", "--item", "b=3")

    assert result.exit_code == 0, result.output
    items = test_data_dir / "scenarios" / "retire_alice" / "itemizations" / "deductions_2028.csv"
    assert _items(items) == [("a", "2.00"), ("b", "3.00")]


def test_edit_guided_keeps_drops_and_adds_items(test_data_dir: Path) -> None:
    assert _sim(test_data_dir, "retire_alice", "add", "2028",
                "--item", "a=1", "--item", "b=2").exit_code == 0

    # a kept (Enter), b dropped, c added.
    result = _sim(test_data_dir, "retire_alice", "edit", "2028", answers=["", "-", "c", "5", ""])

    assert result.exit_code == 0, result.output
    items = test_data_dir / "scenarios" / "retire_alice" / "itemizations" / "deductions_2028.csv"
    assert _items(items) == [("a", "1.00"), ("c", "5.00")]


def test_edit_a_total(test_data_dir: Path) -> None:
    assert _sim(test_data_dir, "retire_alice", "add", "2028", "100").exit_code == 0

    result = _sim(test_data_dir, "retire_alice", "edit", "2028", "--amount", "250")

    assert result.exit_code == 0, result.output
    assert _rows(_own(test_data_dir, "retire_alice")) == [("2028-01-01", "250.00")]


def test_delete_removes_the_row_and_its_items(test_data_dir: Path) -> None:
    assert _sim(test_data_dir, "retire_alice", "add", "2028", "--item", "a=1").exit_code == 0
    assert _sim(test_data_dir, "retire_alice", "add", "2029", "5").exit_code == 0

    result = _sim(test_data_dir, "retire_alice", "delete", "2028", "--yes")

    assert result.exit_code == 0, result.output
    assert _rows(_own(test_data_dir, "retire_alice")) == [("2029-01-01", "5.00")]
    assert not (test_data_dir / "scenarios" / "retire_alice" / "itemizations"
                / "deductions_2028.csv").exists()


def test_a_bad_amount_is_refused(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "retire_alice", "add", "2028", "lots")

    assert result.exit_code != 0
    assert not _own(test_data_dir, "retire_alice").exists()
