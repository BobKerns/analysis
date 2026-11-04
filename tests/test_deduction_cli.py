"""``sim deduction add|list|edit|delete`` (#370): one itemized total per
tax year in the scenario's own aux/federal_deductions.csv (or
state_deductions.csv), with an itemization of the year's expenses beside it.
An inherited file is copied into the scenario before it changes."""

from __future__ import annotations

import csv
from collections.abc import Sequence
from typing import Any
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.sim import cli


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


def test_an_empty_amount_is_refused(test_data_dir: Path) -> None:
    assert _sim(test_data_dir, "retire_alice", "add", "2028", "100").exit_code == 0

    result = _sim(test_data_dir, "retire_alice", "edit", "2028", "--amount", "")

    assert result.exit_code != 0
    assert _rows(_own(test_data_dir, "retire_alice")) == [("2028-01-01", "100.00")]


def test_guided_edit_keeps_an_items_other_columns(test_data_dir: Path) -> None:
    assert _sim(test_data_dir, "retire_alice", "add", "2028", "--item", "a=1").exit_code == 0
    items = test_data_dir / "scenarios" / "retire_alice" / "itemizations" / "deductions_2028.csv"
    items.write_text("Item,Value,Note\na,1.00,receipt in the box\n", encoding="utf-8")

    result = _sim(test_data_dir, "retire_alice", "edit", "2028", answers=["2", ""])

    assert result.exit_code == 0, result.output
    assert items.read_text(encoding="utf-8") == "Item,Value,Note\na,2.00,receipt in the box\n"


def _itemization(data: Path, name: str, value: str = "500.00") -> None:
    path = data / "scenarios" / "retire_alice" / "itemizations" / f"{name}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"Item,Value\nx,{value}\n", encoding="utf-8")


def test_editing_a_shared_itemization_writes_the_years_own(test_data_dir: Path) -> None:
    _itemization(test_data_dir, "medical")
    assert _sim(test_data_dir, "retire_alice", "add", "2028", "@medical").exit_code == 0

    result = _sim(test_data_dir, "retire_alice", "edit", "2028", answers=["700", ""])

    assert result.exit_code == 0, result.output
    folder = test_data_dir / "scenarios" / "retire_alice" / "itemizations"
    assert _items(folder / "medical.csv") == [("x", "500.00")]
    assert _items(folder / "deductions_2028.csv") == [("x", "700.00")]
    assert _rows(_own(test_data_dir, "retire_alice")) == [("2028-01-01", "@deductions_2028")]


def test_a_name_another_itemization_has_is_refused(test_data_dir: Path) -> None:
    _itemization(test_data_dir, "deductions_2028")

    result = _sim(test_data_dir, "retire_alice", "add", "2028", "--item", "a=1")

    assert result.exit_code != 0
    assert "already exists" in result.output
    assert _items(test_data_dir / "scenarios" / "retire_alice" / "itemizations"
                  / "deductions_2028.csv") == [("x", "500.00")]


def test_delete_keeps_items_something_else_references(test_data_dir: Path) -> None:
    assert _sim(test_data_dir, "retire_alice", "add", "2028", "--item", "a=1").exit_code == 0
    assert _sim(test_data_dir, "retire_alice", "add", "2029", "--state",
                "@deductions_2028").exit_code == 0

    result = _sim(test_data_dir, "retire_alice", "delete", "2028", "--yes")

    assert result.exit_code == 0, result.output
    assert (test_data_dir / "scenarios" / "retire_alice" / "itemizations"
            / "deductions_2028.csv").exists()


def test_guided_edit_keeps_an_unchanged_itemization_reference(test_data_dir: Path) -> None:
    _itemization(test_data_dir, "drugs", "300.00")
    assert _sim(test_data_dir, "retire_alice", "add", "2028",
                "--item", "a=1", "--item", "pharmacy=@drugs").exit_code == 0

    result = _sim(test_data_dir, "retire_alice", "edit", "2028", answers=["", "", ""])

    assert result.exit_code == 0, result.output
    items = test_data_dir / "scenarios" / "retire_alice" / "itemizations" / "deductions_2028.csv"
    assert _items(items) == [("a", "1.00"), ("pharmacy", "@drugs")]
    assert "($301.00)" in result.output


def test_a_total_in_place_of_the_list_removes_it(test_data_dir: Path) -> None:
    assert _sim(test_data_dir, "retire_alice", "add", "2028", "--item", "a=1").exit_code == 0
    items = test_data_dir / "scenarios" / "retire_alice" / "itemizations" / "deductions_2028.csv"

    result = _sim(test_data_dir, "retire_alice", "edit", "2028", "--amount", "900")

    assert result.exit_code == 0, result.output
    assert not items.exists()
    # So the year can be listed again.
    assert _sim(test_data_dir, "retire_alice", "edit", "2028", "--item", "b=2").exit_code == 0
    assert _items(items) == [("b", "2.00")]


def test_another_years_row_counts_as_a_reference(test_data_dir: Path) -> None:
    assert _sim(test_data_dir, "retire_alice", "add", "2028", "--item", "a=1").exit_code == 0
    assert _sim(test_data_dir, "retire_alice", "add", "2029", "@deductions_2028").exit_code == 0

    result = _sim(test_data_dir, "retire_alice", "edit", "2028", "--amount", "5")

    assert result.exit_code == 0, result.output
    assert (test_data_dir / "scenarios" / "retire_alice" / "itemizations"
            / "deductions_2028.csv").exists()


def test_a_rows_other_columns_are_kept(test_data_dir: Path) -> None:
    own = _own(test_data_dir, "retire_alice")
    own.parent.mkdir(parents=True, exist_ok=True)
    own.write_text("effective_date,amount,note\n2027-01-01,500.00,gifts\n", encoding="utf-8")

    result = _sim(test_data_dir, "retire_alice", "add", "2028", "700")

    assert result.exit_code == 0, result.output
    assert own.read_text(encoding="utf-8") == (
        "effective_date,amount,note\n2027-01-01,500.00,gifts\n2028-01-01,700.00,\n")


def test_no_state_rows_says_the_federal_ones_apply(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "retire_alice", "list", "--state")

    assert result.exit_code == 0, result.output
    assert "the state uses the federal ones" in result.output


def test_a_file_put_in_force_during_the_prompts_aborts(
    test_data_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _sim(test_data_dir, "retire_alice", "add", "2028", "100").exit_code == 0
    assert CliRunner().invoke(main, ["sim", "scenario", "create", "child", "retire_alice",
                                     "--data-dir", str(test_data_dir)]).exit_code == 0
    child_own = _own(test_data_dir, "child")
    asked = cli._prompt_with_completion

    def meanwhile(*args: Any, **kwargs: Any) -> str:
        # Another command gives the child its own file, same rows, while
        # this one waits for an answer.
        child_own.parent.mkdir(parents=True, exist_ok=True)
        child_own.write_text("effective_date,amount\n2028-01-01,100.00\n", encoding="utf-8")
        return asked(*args, **kwargs)

    monkeypatch.setattr(cli, "_prompt_with_completion", meanwhile)
    result = _sim(test_data_dir, "child", "edit", "2028", answers=["250"])

    assert result.exit_code != 0
    assert "changed while this was being edited" in result.output
    assert _rows(child_own) == [("2028-01-01", "100.00")]


def test_an_itemization_changed_during_the_prompts_aborts(
    test_data_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _itemization(test_data_dir, "medical")
    assert _sim(test_data_dir, "retire_alice", "add", "2028", "@medical").exit_code == 0
    path = test_data_dir / "scenarios" / "retire_alice" / "itemizations" / "medical.csv"
    asked = cli._prompt_with_completion

    def meanwhile(*args: Any, **kwargs: Any) -> str:
        path.write_text("Item,Value\nx,500.00\ny,9.00\n", encoding="utf-8")
        return asked(*args, **kwargs)

    monkeypatch.setattr(cli, "_prompt_with_completion", meanwhile)
    result = _sim(test_data_dir, "retire_alice", "edit", "2028", answers=["", ""])

    assert result.exit_code != 0
    assert "changed while this was being edited" in result.output
    assert not (path.parent / "deductions_2028.csv").exists()


def test_the_years_own_row_is_found_past_a_blank_line(test_data_dir: Path) -> None:
    assert _sim(test_data_dir, "retire_alice", "add", "2028", "--item", "a=1").exit_code == 0
    own = _own(test_data_dir, "retire_alice")
    own.write_text("effective_date,amount\n\n2028-01-01,@deductions_2028\n", encoding="utf-8")

    result = _sim(test_data_dir, "retire_alice", "edit", "2028", "--item", "a=2")

    assert result.exit_code == 0, result.output
    assert _items(test_data_dir / "scenarios" / "retire_alice" / "itemizations"
                  / "deductions_2028.csv") == [("a", "2.00")]


def test_an_itemization_named_like_a_deduction_file_counts(test_data_dir: Path) -> None:
    assert _sim(test_data_dir, "retire_alice", "add", "2028", "--item", "a=1").exit_code == 0
    # An itemization that happens to be named federal_deductions, using the
    # year's list: not the aux table, so its reference counts.
    _itemization(test_data_dir, "federal_deductions", "@deductions_2028")

    result = _sim(test_data_dir, "retire_alice", "edit", "2028", "--item", "a=2")

    assert result.exit_code != 0
    assert "also referenced by" in result.output


def test_an_amount_naming_the_years_own_list_keeps_it(test_data_dir: Path) -> None:
    assert _sim(test_data_dir, "retire_alice", "add", "2028", "--item", "a=1").exit_code == 0

    result = _sim(test_data_dir, "retire_alice", "edit", "2028", "--amount", "@deductions_2028")

    assert result.exit_code == 0, result.output
    assert (test_data_dir / "scenarios" / "retire_alice" / "itemizations"
            / "deductions_2028.csv").exists()


def test_a_comma_only_row_is_no_item(test_data_dir: Path) -> None:
    _itemization(test_data_dir, "medical")
    path = test_data_dir / "scenarios" / "retire_alice" / "itemizations" / "medical.csv"
    path.write_text("Item,Value\nx,500.00\n,\n", encoding="utf-8")
    assert _sim(test_data_dir, "retire_alice", "add", "2028", "@medical").exit_code == 0

    result = _sim(test_data_dir, "retire_alice", "edit", "2028", answers=["", ""])

    assert result.exit_code == 0, result.output
    assert _items(path.parent / "deductions_2028.csv") == [("x", "500.00")]


def test_a_nearer_itemization_made_during_the_prompts_aborts(
    test_data_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _itemization(test_data_dir, "medical")
    assert _sim(test_data_dir, "retire_alice", "add", "2028", "@medical").exit_code == 0
    assert CliRunner().invoke(main, ["sim", "scenario", "create", "child", "retire_alice",
                                     "--data-dir", str(test_data_dir)]).exit_code == 0
    assert _sim(test_data_dir, "child", "add", "2029", "5").exit_code == 0  # its own aux file
    child_items = test_data_dir / "scenarios" / "child" / "itemizations" / "medical.csv"
    asked = cli._prompt_with_completion

    def meanwhile(*args: Any, **kwargs: Any) -> str:
        child_items.parent.mkdir(parents=True, exist_ok=True)
        child_items.write_text("Item,Value\nx,500.00\n", encoding="utf-8")
        return asked(*args, **kwargs)

    monkeypatch.setattr(cli, "_prompt_with_completion", meanwhile)
    result = _sim(test_data_dir, "child", "edit", "2028", answers=["", ""])

    assert result.exit_code != 0
    assert "changed while this was being edited" in result.output
