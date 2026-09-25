"""Portfolio asset allocation from allocations.csv (ADR-003 §4.1b)."""

from __future__ import annotations

from pathlib import Path

import matplotlib
import pytest
from click.testing import CliRunner

matplotlib.use("Agg")

from analysis.cli import main
from analysis.sim.allocation import UNCLASSIFIED, lot_classes, portfolio_allocation
from analysis.sim.connection import RunConnection, ScenarioConnection
from analysis.sim.loader import LoadError
from analysis.sim.notebook_ui.allocation_widgets import allocation_view, portfolio_allocation_widget

ALLOCATIONS = (
    "symbol,as_of,asset_class,weight\n"
    "AAA,,stock_us,70\n"
    "AAA,,bond,30\n"
    "BBB,2026-01-01,stock_intl,100\n"
    "BBB,2027-01-01,bond,100\n"
)


@pytest.fixture(scope="function")
def allocations_csv(test_data_dir: Path) -> Path:
    path = test_data_dir / "allocations.csv"
    path.write_text(ALLOCATIONS)
    return path


def test_lots_are_split_by_their_symbols_weights(
    allocations_csv: Path, two_lot_run: RunConnection,
) -> None:
    frame = lot_classes(two_lot_run)
    a = frame[(frame["lot"] == "a")].set_index("asset_class")
    assert set(a.index) == {"stock_us", "bond"}
    assert a.loc["stock_us", "balance"] == pytest.approx(0.7 * (a.loc["stock_us", "balance"] + a.loc["bond", "balance"]))
    assert frame["balance"].sum() == pytest.approx(
        lot_total(two_lot_run))  # nothing is lost or invented by the split


def lot_total(source) -> float:
    from analysis.sim.holdings import lots_frame
    lots = lots_frame(source)
    return lots[lots["kind"] == "asset"]["balance"].sum()


def test_the_weight_set_in_force_at_the_date_is_used(
    allocations_csv: Path, two_lot_run: RunConnection,
) -> None:
    def classes_of_b(month: str) -> set[str]:
        frame = lot_classes(two_lot_run, month)
        return set(frame[frame["lot"] == "b"]["asset_class"])

    assert classes_of_b("2026-06") == {"stock_intl"}  # the 2026-01-01 set
    assert classes_of_b("2027-03") == {"bond"}  # replaced by the 2027-01-01 set


def test_unclassified_lots_are_shown_not_dropped(two_lot_run: RunConnection) -> None:
    table = portfolio_allocation(two_lot_run)  # no allocations.csv: AAA and BBB are unknown
    assert UNCLASSIFIED in table.index
    assert table.loc["Total", "balance"] == pytest.approx(lot_total(two_lot_run))
    assert table.loc["cash", "balance"] > 0 and table.loc["real_estate", "balance"] > 0


def test_totals_and_percentages_add_up_and_can_be_split(
    allocations_csv: Path, two_lot_run: RunConnection,
) -> None:
    plain = portfolio_allocation(two_lot_run)
    assert plain.loc["Total", "percent"] == pytest.approx(100.0)
    split = portfolio_allocation(two_lot_run, by="tax_treatment")
    assert split.loc["Total", "Total"] == pytest.approx(plain.loc["Total", "balance"])
    assert split.loc["stock_us", "taxable"] > 0
    with pytest.raises(ValueError, match="cannot group by"):
        portfolio_allocation(two_lot_run, by="colour")


@pytest.mark.parametrize(("rows", "message"), [
    ("AAA,,stock_us,60\nAAA,,bond,30\n", "add up to 90%, not 100%"),
    ("AAA,,gold,100\n", "unknown asset_class 'gold'"),
    ("AAA,,gold,60\nAAA,,bond,40\n", "unknown asset_class 'gold'"),
    ("AAA,,bond,100\nAAA,,bond,0\n", "duplicate bond"),
    ("AAA,soon,bond,100\n", "as_of 'soon' is not an ISO date"),
    (",,bond,100\n", "missing symbol"),
    ("AAA,,bond,many\n", "weight"),
])
def test_bad_allocation_rows_are_rejected(
    rows: str, message: str, test_data_dir: Path, load_test_copy,
) -> None:
    (test_data_dir / "allocations.csv").write_text("symbol,as_of,asset_class,weight\n" + rows)
    with pytest.raises(LoadError, match=message):
        load_test_copy()


def test_allocation_command_prints_the_portfolio(
    allocations_csv: Path, two_lot_run_file: Path, test_data_dir: Path,
) -> None:
    result = CliRunner().invoke(main, [
        "sim", "allocation", "--by", "owner", "--scenario", "retire_alice",
        "--data-dir", str(test_data_dir), "--runfile", str(two_lot_run_file),
    ], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "asset allocation" in result.output
    assert "stock_us" in result.output and "Total" in result.output


def test_widget_view_and_widget(allocations_csv: Path, two_lot_run: RunConnection) -> None:
    heading, table = allocation_view(two_lot_run, "account")
    assert heading.startswith("**Asset allocation** as of ")
    assert "Asset_Brokerage_Joint" in table.columns
    widget = portfolio_allocation_widget(two_lot_run)
    selectors, _out = widget.children
    selectors.children[0].value = "owner"  # must not raise


def test_a_scenario_shows_opening_values_with_the_weights_at_its_start(
    allocations_csv: Path, test_data_dir: Path, two_lot_run_file: Path,
) -> None:
    with ScenarioConnection(test_data_dir, "retire_alice") as scenario:
        frame = lot_classes(scenario)
        assert set(frame["as_of"]) == {"opening"}
        b = frame[frame["lot"] == "b"]
        assert list(b["asset_class"]) == ["stock_intl"]  # the set in force at the 2026-05-31 start
        assert portfolio_allocation(scenario).loc["stock_us", "balance"] == pytest.approx(70000.0)
        portfolio_allocation_widget(scenario)  # a scenario has no month picker


def test_a_bad_class_still_counts_toward_the_weight_total(test_data_dir: Path, load_test_copy) -> None:
    (test_data_dir / "allocations.csv").write_text(
        "symbol,as_of,asset_class,weight\nAAA,,gold,60\nAAA,,bond,40\n")
    with pytest.raises(LoadError) as ei:
        load_test_copy()
    assert not any("add up to" in issue for issue in ei.value.issues)  # 60 + 40 is 100


@pytest.fixture(scope="function")
def unknown_split_csv(test_data_dir: Path) -> Path:
    path = test_data_dir / "allocations.csv"
    path.write_text("symbol,as_of,asset_class,weight\nAAA,,stock,70\nAAA,,bond,30\n")
    return path


def test_stock_of_unknown_split_is_its_own_class_between_stock_and_bond(
    unknown_split_csv: Path, two_lot_run: RunConnection,
) -> None:
    table = portfolio_allocation(two_lot_run)
    assert table.loc["stock", "balance"] > 0
    assert list(table.index).index("stock") < list(table.index).index("bond")
