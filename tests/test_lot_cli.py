"""``sim account lot add|edit|delete`` (issue #124): a lot is one
account_values.csv row, added, changed or deleted with the references to it."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner, Result

from analysis.cli import main

BROKERAGE = "Asset_Brokerage_Joint"


@pytest.fixture
def lot(test_data_dir: Path) -> Callable[..., Result]:
    """Run ``sim account lot <args>`` on the private data copy, typing ``answers``."""
    def run(*args: str, scenario: str = "retire_alice", answers: list[str] | None = None) -> Result:
        return CliRunner().invoke(
            main,
            ["sim", "account", "lot", *args,
             "--data-dir", str(test_data_dir), "--scenario", scenario],
            input="".join(answer + "\n" for answer in answers or []),
        )
    return run


@pytest.fixture
def values(test_scenarios_dir: Path) -> Callable[[str], pd.DataFrame]:
    """A scenario's own account_values.csv."""
    def read(scenario: str = "retire_alice") -> pd.DataFrame:
        path = test_scenarios_dir / scenario / "account_values.csv"
        return pd.read_csv(path, dtype=str, keep_default_na=False)
    return read


def _lot_row(frame: pd.DataFrame, account: str, lot: str) -> dict[str, str]:
    rows = frame[(frame["account_name"] == account) & (frame["lot"] == lot)]
    assert len(rows) == 1, frame
    return {str(k): str(v) for k, v in rows.iloc[0].items()}


@pytest.fixture
def check(test_data_dir: Path) -> Callable[[str], None]:
    """Assert a scenario of the private copy still loads."""
    def run(scenario: str = "retire_alice") -> None:
        result = CliRunner().invoke(main, ["sim", "check", scenario, "--data-dir", str(test_data_dir)])
        assert result.exit_code == 0, result.output
    return run


def test_add_writes_the_lot_from_its_options_with_the_symbol_upper_cased(
    lot: Callable[..., Result], values: Callable[..., pd.DataFrame], check: Callable[[str], None],
) -> None:
    result = lot("add", BROKERAGE, "growth", "--symbol", "vti", "--acquired", "2024-02-01",
                 "--opening-balance", "$1,500", "--opening-cost-basis", "1200")

    assert result.exit_code == 0, result.output
    row = _lot_row(values(), BROKERAGE, "growth")
    assert (row["symbol"], row["acquired"], row["opening_balance"], row["opening_cost_basis"]) == (
        "VTI", "2024-02-01", "1500.00", "1200.00")
    check("retire_alice")


@pytest.mark.parametrize(("account", "label", "message"), [
    (BROKERAGE, "default", "already has a lot 'default'"),
    (BROKERAGE, "a:b", "may not contain ':'"),
    ("Income_Salary_Alice", "x", "unknown account"),
])
def test_add_refuses_a_lot_it_cannot_add(
    lot: Callable[..., Result], values: Callable[..., pd.DataFrame],
    account: str, label: str, message: str,
) -> None:
    before = values()

    result = lot("add", account, label, "--opening-balance", "1")

    assert result.exit_code != 0
    assert message in result.output
    pd.testing.assert_frame_equal(values(), before)


def test_guided_add_asks_only_what_applies(
    lot: Callable[..., Result], values: Callable[..., pd.DataFrame],
) -> None:
    # account, lot, symbol, acquired, quantity, series, balance, manage?, write
    result = lot("add", answers=["Asset_Roth_Bill", "bonds", "BND", "", "", "", "2500", "n", "write"])

    assert result.exit_code == 0, result.output
    # A Roth account isn't taxable, so no basis is asked for.
    assert "cost basis" not in result.output
    assert "sim account lot add Asset_Roth_Bill bonds --symbol BND --opening-balance 2500" in result.output
    assert _lot_row(values(), "Asset_Roth_Bill", "bonds")["opening_balance"] == "2500.00"


def test_edit_sets_a_field_and_keeps_the_rest_as_authored(
    lot: Callable[..., Result], values: Callable[..., pd.DataFrame],
) -> None:
    before = values()

    result = lot("edit", BROKERAGE, "default", "--opening-cost-basis", "250000")

    assert result.exit_code == 0, result.output
    after = values()
    changed = after.ne(before)
    assert changed.sum().sum() == 1
    assert _lot_row(after, BROKERAGE, "default")["opening_cost_basis"] == "250000.00"


def test_guided_edit_changes_a_named_field(
    lot: Callable[..., Result], values: Callable[..., pd.DataFrame],
) -> None:
    # The account has one lot, so it is chosen without asking.
    result = lot("edit", "Asset_Savings", answers=["opening-balance", "21000", "write"])

    assert result.exit_code == 0, result.output
    assert _lot_row(values(), "Asset_Savings", "")["opening_balance"] == "21000.00"


def test_writing_a_lot_unchanged_writes_nothing(
    lot: Callable[..., Result], values: Callable[..., pd.DataFrame],
) -> None:
    before = values()

    result = lot("edit", "Asset_Savings", answers=["write"])

    assert result.exit_code == 0, result.output
    assert "no change" in result.output
    pd.testing.assert_frame_equal(values(), before)


def test_renaming_a_lot_renames_its_references(
    lot: Callable[..., Result], values: Callable[..., pd.DataFrame],
    retire_alice_csv: Callable[[str], Path], check: Callable[[str], None],
) -> None:
    frame = values()
    frame.loc[frame["account_name"] == "Asset_Savings", ["refill_chain", "refill_lot"]] = [BROKERAGE, "default"]
    frame.to_csv(retire_alice_csv("account_values.csv"), index=False)

    result = lot("edit", BROKERAGE, "default", "--new-lot", "core")

    assert result.exit_code == 0, result.output
    after = values()
    assert set(after.loc[after["account_name"] == BROKERAGE, "lot"]) == {"core"}
    assert _lot_row(after, "Asset_Savings", "")["refill_lot"] == "core"
    calendar = pd.read_csv(retire_alice_csv("calendar.csv"), dtype=str, keep_default_na=False)
    assert set(calendar.loc[calendar["account_2"] == BROKERAGE, "lot_2"]) == {"core"}
    check("retire_alice")


def test_delete_lists_references_and_removes_them_once_confirmed(
    lot: Callable[..., Result], values: Callable[..., pd.DataFrame],
    retire_alice_csv: Callable[[str], Path], check: Callable[[str], None],
) -> None:
    frame = values()
    frame.loc[frame["account_name"] == "Asset_Savings",
              ["minimum_balance", "refill_chain", "refill_lot", "refill_amount"]] = [
        "1000.00", BROKERAGE, "default", "500.00"]
    frame.to_csv(retire_alice_csv("account_values.csv"), index=False)
    calendar_before = pd.read_csv(retire_alice_csv("calendar.csv"), dtype=str, keep_default_na=False)

    declined = lot("delete", BROKERAGE, "default", answers=["n"])
    assert declined.exit_code == 0, declined.output
    assert "aborted" in declined.output
    pd.testing.assert_frame_equal(values(), frame)

    result = lot("delete", BROKERAGE, "default", answers=["y"])

    assert result.exit_code == 0, result.output
    assert "account_values.csv row 3: refill_chain/refill_lot" in result.output
    assert "calendar.csv row 5: account_2/lot_2" in result.output
    after = values()
    assert BROKERAGE not in set(after["account_name"])
    savings = _lot_row(after, "Asset_Savings", "")
    # The refill amount means nothing without its chain, so it goes too; the
    # minimum stays.
    assert (savings["refill_chain"], savings["refill_amount"], savings["minimum_balance"]) == (
        "", "", "1000.00")
    calendar = pd.read_csv(retire_alice_csv("calendar.csv"), dtype=str, keep_default_na=False)
    assert len(calendar) == len(calendar_before) - 1
    assert BROKERAGE not in set(calendar["account_2"])
    check("retire_alice")


@pytest.mark.usefixtures("test_child_scenario")
def test_editing_an_inherited_lot_writes_an_override(
    lot: Callable[..., Result], values: Callable[..., pd.DataFrame], check: Callable[[str], None],
) -> None:
    parent = values("retire_alice")

    result = lot("edit", BROKERAGE, "default", "--opening-balance", "350000", scenario="child")

    assert result.exit_code == 0, result.output
    assert "wrote an override" in result.output
    override = _lot_row(values("child"), BROKERAGE, "default")
    assert override == {**_lot_row(parent, BROKERAGE, "default"), "opening_balance": "350000.00"}
    pd.testing.assert_frame_equal(values("retire_alice"), parent)
    check("child")


@pytest.mark.usefixtures("test_child_scenario")
def test_renaming_an_inherited_lot_is_refused(
    lot: Callable[..., Result], values: Callable[..., pd.DataFrame],
) -> None:
    result = lot("edit", BROKERAGE, "default", "--new-lot", "core", scenario="child")

    assert result.exit_code != 0
    assert "rename it there" in result.output
    assert values("child").empty


@pytest.mark.usefixtures("test_child_scenario")
def test_deleting_an_inherited_lot_zeroes_it_and_adding_it_again_replaces_the_zero(
    lot: Callable[..., Result], values: Callable[..., pd.DataFrame], check: Callable[[str], None],
) -> None:
    result = lot("delete", BROKERAGE, "default", scenario="child")

    assert result.exit_code == 0, result.output
    assert "zeroed" in result.output
    # The inheritance's calendar transfer still names the lot; it is listed to check.
    assert "retire_alice: calendar.csv row 5: account_2/lot_2" in result.output
    zeroed = _lot_row(values("child"), BROKERAGE, "default")
    assert (zeroed["opening_balance"], zeroed["opening_cost_basis"]) == ("0.00", "0.00")
    check("child")

    again = lot("delete", BROKERAGE, "default", scenario="child")
    assert "already deleted" in again.output

    added_again = lot("add", BROKERAGE, "default", "--opening-balance", "10", "--opening-cost-basis", "10",
                  scenario="child")

    assert added_again.exit_code == 0, added_again.output
    assert "re-added" in added_again.output
    child = values("child")
    assert len(child) == 1
    assert _lot_row(child, BROKERAGE, "default")["opening_balance"] == "10.00"
    check("child")


@pytest.mark.usefixtures("test_child_scenario")
def test_list_shows_an_accounts_lots_and_where_each_comes_from(
    lot: Callable[..., Result], set_brokerage_lots: Callable[[list[dict[str, str]]], None],
) -> None:
    set_brokerage_lots([
        {"lot": "default", "opening_balance": "100.00", "opening_cost_basis": "90.00"},
        {"lot": "b", "opening_balance": "200.00", "opening_cost_basis": "150.00"},
        {"lot": "c", "opening_balance": "300.00", "opening_cost_basis": "250.00"},
    ])
    edited = lot("edit", BROKERAGE, "default", "--opening-balance", "110", scenario="child")
    assert edited.exit_code == 0, edited.output
    deleted = lot("delete", BROKERAGE, "b", scenario="child")
    assert deleted.exit_code == 0, deleted.output

    result = lot("list", BROKERAGE, scenario="child")

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].split() == ["account", "lot", "opening_balance", "opening_cost_basis", "from"]
    assert lines[1].split()[1:3] == ["default", "110.00"] and lines[1].endswith("overrides retire_alice")
    assert lines[2].split()[1:3] == ["b", "0.00"] and "deleted" in lines[2]
    assert lines[3].split()[1:3] == ["c", "300.00"] and lines[3].endswith("inherited from retire_alice")
    assert len(lines) == 4


def test_list_refuses_an_account_that_holds_no_lots(lot: Callable[..., Result]) -> None:
    result = lot("list", "Income_Salary_Alice")

    assert result.exit_code != 0
    assert "no account 'Income_Salary_Alice' that holds lots" in result.output


def test_list_shows_every_column_a_lot_uses(
    lot: Callable[..., Result], set_brokerage_lots: Callable[[list[dict[str, str]]], None],
) -> None:
    set_brokerage_lots([{"lot": "default", "series": "stock", "opening_balance": "100.00",
                         "opening_cost_basis": "90.00", "maximum_balance": "500.00",
                         "overflow_chain": "Asset_Savings"}])

    result = lot("list", BROKERAGE)

    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0].split() == [
        "account", "lot", "series", "opening_balance", "opening_cost_basis", "maximum_balance",
        "overflow_chain", "from"]


def test_deleting_an_accounts_last_lot_says_it_loads_with_an_empty_one(
    lot: Callable[..., Result], check: Callable[[str], None],
) -> None:
    result = lot("delete", "Asset_Roth_Bill", "default")

    assert result.exit_code == 0, result.output
    assert "loads with an empty 'default' lot" in result.output
    listed = lot("list", "Asset_Roth_Bill")
    assert listed.exit_code == 0, listed.output
    assert "loads with an empty 'default' lot" in listed.output
    check("retire_alice")


def test_deleting_a_chains_only_step_is_refused(
    lot: Callable[..., Result], values: Callable[..., pd.DataFrame],
    retire_alice_csv: Callable[[str], Path],
) -> None:
    (retire_alice_csv("chains/source") / "roth_only.csv").write_text(
        "account_name,lot,maximum,percent,series,cadence\nAsset_Roth_Bill,default,,,,\n")
    before = values()

    result = lot("delete", "Asset_Roth_Bill", "default", "--yes")

    assert result.exit_code != 0
    assert "only step of retire_alice/chains/source/roth_only.csv" in result.output
    pd.testing.assert_frame_equal(values(), before)


@pytest.mark.parametrize("new_lot", ["reserve", None])
def test_a_chain_without_a_lot_column_names_the_default_lot(
    lot: Callable[..., Result], retire_alice_csv: Callable[[str], Path],
    check: Callable[[str], None], new_lot: str | None,
) -> None:
    chain = retire_alice_csv("chains/source") / "cash_only.csv"
    chain.write_text("account_name,maximum,percent,series,cadence\nAsset_Savings,,,,\nAsset_Checking,,,,\n")

    if new_lot is None:
        result = lot("delete", "Asset_Savings", "default", "--yes")
    else:
        result = lot("edit", "Asset_Savings", "default", "--new-lot", new_lot)

    assert result.exit_code == 0, result.output
    steps = pd.read_csv(chain, dtype=str, keep_default_na=False)
    if new_lot is None:
        assert list(steps["account_name"]) == ["Asset_Checking"]
    else:
        assert list(zip(steps["account_name"], steps["lot"], strict=True)) == [
            ("Asset_Savings", new_lot), ("Asset_Checking", "")]
    check("retire_alice")


@pytest.mark.usefixtures("test_child_scenario")
def test_renaming_or_deleting_a_lot_follows_it_into_inheriting_scenarios(
    lot: Callable[..., Result], values: Callable[..., pd.DataFrame],
    test_scenarios_dir: Path, check: Callable[[str], None],
) -> None:
    child = test_scenarios_dir / "child"
    calendar = (test_scenarios_dir / "retire_alice" / "calendar.csv").read_text().splitlines()[0]
    (child / "calendar.csv").write_text(
        calendar + "\n2030-01-31,one_off_transfer,,Asset_Checking,,Asset_Brokerage_Joint,default,,"
        + "100000,,none" + "," * (calendar.count(",") - 10) + "\n")
    assert lot("edit", BROKERAGE, "default", "--opening-balance", "1", scenario="child").exit_code == 0

    renamed = lot("edit", BROKERAGE, "default", "--new-lot", "core")

    assert renamed.exit_code == 0, renamed.output
    child_calendar = pd.read_csv(child / "calendar.csv", dtype=str, keep_default_na=False)
    assert list(child_calendar["lot_2"]) == ["core"]
    assert list(values("child")["lot"]) == ["core"]
    check("child")

    deleted = lot("delete", BROKERAGE, "core", "--yes")

    assert deleted.exit_code == 0, deleted.output
    assert "child: calendar.csv row 2: account_2/lot_2" in deleted.output
    assert "child: account_values.csv row 2: an override of the lot" in deleted.output
    assert pd.read_csv(child / "calendar.csv", dtype=str, keep_default_na=False).empty
    assert values("child").empty
    check("child")


@pytest.mark.usefixtures("test_child_scenario")
def test_a_zero_opening_override_with_a_basis_is_not_deleted(
    lot: Callable[..., Result],
) -> None:
    edited = lot("edit", BROKERAGE, "default", "--opening-balance", "0", scenario="child")
    assert edited.exit_code == 0, edited.output

    result = lot("list", BROKERAGE, scenario="child")

    assert "overrides retire_alice" in result.output
    assert "deleted" not in result.output


@pytest.mark.usefixtures("test_grandchild_scenario")
def test_a_lot_deleted_in_a_parent_is_deleted_below_it_and_can_be_added_back(
    lot: Callable[..., Result], values: Callable[..., pd.DataFrame], check: Callable[[str], None],
) -> None:
    assert lot("delete", BROKERAGE, "default", scenario="child").exit_code == 0

    listed = lot("list", BROKERAGE, scenario="grandchild")
    assert "deleted (zeroed in child" in listed.output
    again = lot("delete", BROKERAGE, "default", scenario="grandchild")
    assert "already deleted (zeroed in child)" in again.output

    added = lot("add", BROKERAGE, "default", "--opening-balance", "10", "--opening-cost-basis", "10",
                scenario="grandchild")

    assert added.exit_code == 0, added.output
    assert "re-added" in added.output
    assert _lot_row(values("grandchild"), BROKERAGE, "default")["opening_balance"] == "10.00"
    check("grandchild")


@pytest.mark.parametrize("quantity", ["NaN", "Infinity", "-1"])
def test_a_quantity_must_be_a_finite_count_of_shares(
    lot: Callable[..., Result], values: Callable[..., pd.DataFrame], quantity: str,
) -> None:
    before = values()

    result = lot("add", BROKERAGE, "x", "--quantity", quantity, "--opening-balance", "1")

    assert result.exit_code != 0
    assert "quantity must be a number of shares" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    pd.testing.assert_frame_equal(values(), before)


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-Infinity"])
def test_an_amount_must_be_finite(
    lot: Callable[..., Result], values: Callable[..., pd.DataFrame], amount: str,
) -> None:
    before = values()

    result = lot("add", BROKERAGE, "x", "--opening-balance", amount)

    assert result.exit_code != 0
    assert "invalid amount" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    pd.testing.assert_frame_equal(values(), before)


def test_overflow_takes_a_destination_chain_not_a_source(lot: Callable[..., Result]) -> None:
    """Issue #143: a lot overflows into a destination chain, and the
    prototype's one chain is a source."""
    result = lot("edit", "Asset_Checking", "", "--maximum-balance", "30000",
                 "--overflow-chain", "asset_checking_liquidity")
    assert result.exit_code != 0
    assert "overflow_chain 'asset_checking_liquidity' is a source chain" in result.output
