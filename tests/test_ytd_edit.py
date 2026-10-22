"""``sim ytd add``, ``edit`` and ``delete`` (issue #193): year_to_date.csv
rows, through a staged edit. An inherited row is edited by a whole-row
override and deleted by an override with amount zero."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.sim.registries import YEAR_TO_DATE_CSV_HEADER


def _sim(test_data_dir: Path, scenario: str, *args: str, answers: Sequence[str] = ()) -> Result:
    return CliRunner().invoke(
        main,
        # The options go right after the command, so a test can end its line
        # with `-- -250` as a negative amount needs.
        ["sim", "ytd", args[0], "--data-dir", str(test_data_dir), "--scenario", scenario, *args[1:]],
        input="".join(answer + "\n" for answer in answers),
    )


def _read(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=list(YEAR_TO_DATE_CSV_HEADER))
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    return frame.reindex(columns=YEAR_TO_DATE_CSV_HEADER, fill_value="")


def _row(frame: pd.DataFrame, name: str) -> dict[str, str]:
    rows = frame[frame["name"] == name]
    assert len(rows) == 1, f"{name!r}: {len(rows)} rows"
    return {str(k): str(v) for k, v in rows.iloc[0].to_dict().items()}


@pytest.fixture
def ytd(retire_alice_csv: Callable[[str], Path]) -> Callable[[], pd.DataFrame]:
    """The private retire_alice copy's year_to_date.csv, every column present."""
    return lambda: _read(retire_alice_csv("year_to_date.csv"))


# ---------- add ----------


def test_add_writes_a_row(test_data_dir: Path, ytd: Callable[[], pd.DataFrame]) -> None:
    result = _sim(
        test_data_dir, "retire_alice", "add", "prior_interest", "Income_Salary_Alice",
        "Asset_Checking", "120", "--tax-character", "ordinary", "--description", "bank interest",
    )

    assert result.exit_code == 0, result.output
    row = _row(ytd(), "prior_interest")
    assert (row["from_account"], row["to_account"], row["amount"], row["tax_character"],
            row["year"], row["description"]) == (
        "Income_Salary_Alice", "Asset_Checking", "120.00", "ordinary", "", "bank interest")


def test_add_takes_a_negative_amount_and_a_prior_year(
    test_data_dir: Path, ytd: Callable[[], pd.DataFrame],
) -> None:
    result = _sim(
        test_data_dir, "retire_alice", "add", "loss_2024", "Income_CapitalGain",
        "Income_CapitalGain", "--tax-character", "stcg", "--year", "2025", "--", "-250.5",
    )

    assert result.exit_code == 0, result.output
    row = _row(ytd(), "loss_2024")
    assert (row["amount"], row["tax_character"], row["year"]) == ("-250.50", "stcg", "2025")


def test_add_asks_for_what_it_needs(test_data_dir: Path, ytd: Callable[[], pd.DataFrame]) -> None:
    # The brokerage account is an investment account, so its lot is asked too.
    result = _sim(
        test_data_dir, "retire_alice", "add",
        answers=["prior_dividends", "Income_Salary_Alice", "Asset_Brokerage_Joint", "no_such_lot",
                 "default", "300", "nonsense", "ordinary"],
    )

    assert result.exit_code == 0, result.output
    assert "not one of" in result.output
    row = _row(ytd(), "prior_dividends")
    assert (row["to_account"], row["to_lot"], row["amount"], row["tax_character"]) == (
        "Asset_Brokerage_Joint", "default", "300.00", "ordinary")


def test_add_refuses_a_name_already_taken(test_data_dir: Path, ytd: Callable[[], pd.DataFrame]) -> None:
    before = ytd()
    result = _sim(
        test_data_dir, "retire_alice", "add", "prior_ordinary_wages", "Income_Salary_Alice",
        "Asset_Checking", "1", "--tax-character", "ordinary",
    )

    assert result.exit_code != 0
    assert "sim ytd edit prior_ordinary_wages" in result.output
    pd.testing.assert_frame_equal(ytd(), before)


def test_add_refuses_a_malformed_amount(test_data_dir: Path, ytd: Callable[[], pd.DataFrame]) -> None:
    before = ytd()
    result = _sim(
        test_data_dir, "retire_alice", "add", "bad", "Income_Salary_Alice",
        "Asset_Checking", "12.345", "--tax-character", "ordinary",
    )

    assert result.exit_code != 0
    assert "sub-cent" in result.output
    pd.testing.assert_frame_equal(ytd(), before)


def test_add_writes_nothing_when_the_scenario_would_not_load(
    test_data_dir: Path, ytd: Callable[[], pd.DataFrame],
) -> None:
    before = ytd()
    # business_income must be paid into a business account (ADR-003 §4.6).
    result = _sim(
        test_data_dir, "retire_alice", "add", "biz", "Income_Salary_Alice",
        "Asset_Checking", "10", "--tax-character", "business_income",
    )

    assert result.exit_code != 0
    assert "nothing was written" in result.output
    pd.testing.assert_frame_equal(ytd(), before)


def test_add_takes_an_itemization(test_data_dir: Path, ytd: Callable[[], pd.DataFrame]) -> None:
    items = test_data_dir / "scenarios" / "retire_alice" / "itemizations"
    items.mkdir(exist_ok=True)
    (items / "withheld.csv").write_text("Item,Value\njan,100.00\nfeb,150.00\n")

    result = _sim(
        test_data_dir, "retire_alice", "add", "withheld", "Asset_Checking",
        "Expense_Taxes_Federal", "@withheld", "--tax-character", "none",
    )

    assert result.exit_code == 0, result.output
    assert _row(ytd(), "withheld")["amount"] == "@withheld"


# ---------- edit ----------


def test_edit_options_change_those_fields_and_keep_the_rest(
    test_data_dir: Path, ytd: Callable[[], pd.DataFrame],
) -> None:
    before = _row(ytd(), "prior_federal_withholding")
    result = _sim(test_data_dir, "retire_alice", "edit", "prior_federal_withholding",
                  "--amount", "9400", "--description", "")

    assert result.exit_code == 0, result.output
    after = _row(ytd(), "prior_federal_withholding")
    assert (after["amount"], after["description"]) == ("9400.00", "")
    assert {k: v for k, v in after.items() if k not in ("amount", "description")} == {
        k: v for k, v in before.items() if k not in ("amount", "description")}
    # In its place, not moved to the end.
    assert list(ytd()["name"]).index("prior_federal_withholding") == 1


def test_guided_edit_changes_a_named_field(
    test_data_dir: Path, ytd: Callable[[], pd.DataFrame],
) -> None:
    result = _sim(test_data_dir, "retire_alice", "edit", "prior_ordinary_wages",
                  answers=["amount", "63000", "write"])

    assert result.exit_code == 0, result.output
    assert "updated year-to-date row 'prior_ordinary_wages'" in result.output
    assert _row(ytd(), "prior_ordinary_wages")["amount"] == "63000.00"


def test_edit_asks_which_row(test_data_dir: Path, ytd: Callable[[], pd.DataFrame]) -> None:
    result = _sim(test_data_dir, "retire_alice", "edit",
                  answers=["prior_ordinary_wages", "year", "2025", "write"])

    assert result.exit_code == 0, result.output
    assert "prior_federal_withholding" in result.output  # the choices are listed
    assert _row(ytd(), "prior_ordinary_wages")["year"] == "2025"


def test_edit_with_nothing_changed_writes_nothing(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    path = retire_alice_csv("year_to_date.csv")
    before = path.read_bytes()
    result = _sim(test_data_dir, "retire_alice", "edit", "prior_ordinary_wages", answers=["write"])

    assert result.exit_code == 0, result.output
    assert "no change" in result.output
    assert path.read_bytes() == before


def test_edit_moving_a_side_to_another_account_drops_its_lot(
    test_data_dir: Path, ytd: Callable[[], pd.DataFrame],
) -> None:
    added = _sim(test_data_dir, "retire_alice", "add", "divs", "Income_Salary_Alice",
                 "Asset_Brokerage_Joint", "50", "--to-lot", "default", "--tax-character", "ordinary")
    assert added.exit_code == 0, added.output

    result = _sim(test_data_dir, "retire_alice", "edit", "divs", "--to-account", "Asset_Checking")

    assert result.exit_code == 0, result.output
    row = _row(ytd(), "divs")
    assert (row["to_account"], row["to_lot"]) == ("Asset_Checking", "")


def test_edit_of_an_unknown_row_says_so(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "retire_alice", "edit", "no_such_row", "--amount", "1")

    assert result.exit_code != 0
    assert "no year-to-date row 'no_such_row'" in result.output


def test_editing_an_inherited_row_writes_a_whole_row_override(
    test_data_dir: Path, test_child_scenario: Path, ytd: Callable[[], pd.DataFrame],
) -> None:
    parent = _row(ytd(), "prior_ordinary_wages")
    result = _sim(test_data_dir, "child", "edit", "prior_ordinary_wages", "--amount", "50000")

    assert result.exit_code == 0, result.output
    assert "inherited from 'retire_alice'" in result.output
    override = _row(_read(test_child_scenario / "year_to_date.csv"), "prior_ordinary_wages")
    assert override == {**parent, "amount": "50000.00"}
    assert _row(ytd(), "prior_ordinary_wages") == parent  # the parent keeps its own


# ---------- delete ----------


def test_delete_removes_a_row(test_data_dir: Path, ytd: Callable[[], pd.DataFrame]) -> None:
    result = _sim(test_data_dir, "retire_alice", "delete", "prior_federal_withholding", "--yes")

    assert result.exit_code == 0, result.output
    assert "prior_federal_withholding" not in set(ytd()["name"])


def test_delete_writes_nothing_unless_confirmed(
    test_data_dir: Path, ytd: Callable[[], pd.DataFrame],
) -> None:
    result = _sim(test_data_dir, "retire_alice", "delete", "prior_federal_withholding", answers=["n"])

    assert result.exit_code == 0, result.output
    assert "aborted" in result.output
    assert "prior_federal_withholding" in set(ytd()["name"])


def test_deleting_an_inherited_row_zeroes_it_with_an_override(
    test_data_dir: Path, test_child_scenario: Path, ytd: Callable[[], pd.DataFrame],
) -> None:
    parent = _row(ytd(), "prior_ordinary_wages")
    result = _sim(test_data_dir, "child", "delete", "prior_ordinary_wages", "--yes")

    assert result.exit_code == 0, result.output
    assert "can't remove what it inherits" in result.output
    override = _row(_read(test_child_scenario / "year_to_date.csv"), "prior_ordinary_wages")
    assert override == {**parent, "amount": "0.00"}

    again = _sim(test_data_dir, "child", "delete", "prior_ordinary_wages", "--yes")
    assert again.exit_code == 0, again.output
    assert "inherited from 'retire_alice' again" in again.output
    assert _read(test_child_scenario / "year_to_date.csv").empty


def test_deleting_an_inherited_row_already_zero_writes_nothing(
    test_data_dir: Path, test_child_scenario: Path,
) -> None:
    zeroed = _sim(test_data_dir, "retire_alice", "edit", "prior_ordinary_wages", "--amount", "0")
    assert zeroed.exit_code == 0, zeroed.output

    result = _sim(test_data_dir, "child", "delete", "prior_ordinary_wages", "--yes")

    assert result.exit_code == 0, result.output
    assert "already zero" in result.output
    assert _read(test_child_scenario / "year_to_date.csv").empty
