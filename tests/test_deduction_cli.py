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
from analysis.sim import cli, setup_listings


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


def test_the_shared_money_formatter_keeps_its_columns() -> None:
    # sim deduction once redefined it, changing other commands' tables.
    assert cli._fmt_cents(None) == "-"
    assert cli._fmt_cents(800000) == f" ${'8,000':>14}.00"


def test_a_reference_in_an_overridden_itemization_is_ignored(test_data_dir: Path) -> None:
    _itemization(test_data_dir, "medical", "@deductions_2028")
    assert CliRunner().invoke(main, ["sim", "scenario", "create", "child", "retire_alice",
                                     "--data-dir", str(test_data_dir)]).exit_code == 0
    own = test_data_dir / "scenarios" / "child" / "itemizations" / "medical.csv"
    own.parent.mkdir(parents=True, exist_ok=True)
    own.write_text("Item,Value\nx,10.00\n", encoding="utf-8")

    result = _sim(test_data_dir, "child", "add", "2028", "--item", "a=1")

    assert result.exit_code == 0, result.output


def _child(data: Path) -> None:
    created = CliRunner().invoke(main, ["sim", "scenario", "create", "child", "retire_alice",
                                        "--data-dir", str(data)])
    assert created.exit_code == 0, created.output


def test_a_scheduled_amount_the_child_overrides_is_ignored(test_data_dir: Path) -> None:
    assert _sim(test_data_dir, "retire_alice", "add", "2028", "--item", "a=1").exit_code == 0
    parent = test_data_dir / "scenarios" / "retire_alice" / "scheduled.csv"
    header, first = parent.read_text(encoding="utf-8").splitlines()[:2]
    columns = header.split(",")
    cells = next(csv.reader([first]))
    # The parent's first rule takes the year's list as its amount ...
    shared = list(cells)
    shared[columns.index("amount")] = "@deductions_2028"
    lines = parent.read_text(encoding="utf-8").splitlines()
    lines[1] = ",".join(shared)
    parent.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _child(test_data_dir)
    child = test_data_dir / "scenarios" / "child" / "scheduled.csv"
    own = list(cells)  # ... and the child's override of it a literal.
    own[columns.index("amount")] = "100.00"
    child.write_text(header + "\n" + ",".join(own) + "\n", encoding="utf-8")

    refs = setup_listings.itemization_references(
        test_data_dir, test_data_dir / "scenarios" / "child", effective=True)

    assert not any(ref.path == parent for ref in refs.get("deductions_2028", []))


def test_a_whole_file_the_child_replaces_is_ignored(test_data_dir: Path) -> None:
    assert _sim(test_data_dir, "retire_alice", "add", "2028", "--item", "a=1").exit_code == 0
    # The parent's state deductions use the year's list; the child's own don't.
    assert _sim(test_data_dir, "retire_alice", "add", "2028", "--state",
                "@deductions_2028").exit_code == 0
    _child(test_data_dir)
    assert _sim(test_data_dir, "child", "edit", "2028", "--state", "--amount", "5").exit_code == 0

    result = _sim(test_data_dir, "child", "edit", "2028", "--item", "a=2")

    assert result.exit_code == 0, result.output


def test_a_blank_lot_is_the_default_lot_when_overriding(test_data_dir: Path) -> None:
    _child(test_data_dir)  # first: the files below are only read, never loaded
    parent = test_data_dir / "scenarios" / "retire_alice"
    (parent / "account_lots.csv").write_text(
        "account_name,lot,opening_balance\nAsset_Checking,,@deductions_2028\n", encoding="utf-8")
    (test_data_dir / "scenarios" / "child" / "account_lots.csv").write_text(
        "account_name,lot,opening_balance\nAsset_Checking,default,100.00\n", encoding="utf-8")

    refs = setup_listings.itemization_references(
        test_data_dir, test_data_dir / "scenarios" / "child", effective=True)

    assert "deductions_2028" not in refs


def _effective(data: Path) -> dict[str, list[setup_listings.ItemizationReference]]:
    return setup_listings.itemization_references(
        data, data / "scenarios" / "child", effective=True)


def test_a_chain_the_child_replaces_in_the_other_role_is_ignored(test_data_dir: Path) -> None:
    _child(test_data_dir)  # first: the files below are only read, never loaded
    parent = test_data_dir / "scenarios" / "retire_alice" / "chains" / "source" / "budget.csv"
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text("step,account,maximum\n1,Asset_Checking,@deductions_2028\n", encoding="utf-8")
    own = test_data_dir / "scenarios" / "child" / "chains" / "destination" / "budget.csv"
    own.parent.mkdir(parents=True, exist_ok=True)
    own.write_text("step,account,maximum\n1,Asset_Checking,\n", encoding="utf-8")

    assert "deductions_2028" not in _effective(test_data_dir)


def test_an_itemization_named_like_a_merged_file_is_replaced_whole(test_data_dir: Path) -> None:
    _child(test_data_dir)
    _itemization(test_data_dir, "accounts", "@deductions_2028")
    own = test_data_dir / "scenarios" / "child" / "itemizations" / "accounts.csv"
    own.parent.mkdir(parents=True, exist_ok=True)
    own.write_text("Item,Value\n", encoding="utf-8")

    assert "deductions_2028" not in _effective(test_data_dir)


def test_a_chain_template_the_loader_skips_is_ignored(test_data_dir: Path) -> None:
    _child(test_data_dir)
    draft = test_data_dir / "scenarios" / "child" / "chains" / "source" / "_draft.csv"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("step,account,maximum\n1,Asset_Checking,@deductions_2028\n", encoding="utf-8")

    assert "deductions_2028" not in _effective(test_data_dir)
