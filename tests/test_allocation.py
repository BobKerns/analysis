"""Portfolio asset allocation from allocations.csv (ADR-003 §4.1b)."""

from __future__ import annotations

from pathlib import Path

import matplotlib
import pytest
from click.testing import CliRunner

matplotlib.use("Agg")

from analysis.cli import main
from analysis.sim.allocation import (
    UNCLASSIFIED, asset_classes_present, lot_classes, portfolio_allocation,
)
from analysis.sim.connection import RunConnection, ScenarioConnection
from analysis.sim.loader import LoadError
from analysis.sim.notebook_ui.allocation_widgets import allocation_view, portfolio_allocation_widget
from analysis.sim.notebook_ui.accounts_plots import account_history_widget
from analysis.sim.notebook_ui.holdings_widgets import account_holdings_widget, symbol_holders_widget

ALLOCATIONS = (
    "symbol,as_of,asset_class,weight\n"
    "AAA,,stock_us,70%\n"
    "AAA,,bond,30%\n"
    "BBB,2026-01-01,stock_intl,100%\n"
    "BBB,2027-01-01,bond,100%\n"
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
    assert a.loc["stock_us", "balance"] == pytest.approx(0.7 * (a.loc["stock_us", "balance"]
                                                                + a.loc["bond", "balance"]))
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
    ("AAA,,stock_us,60%\nAAA,,bond,30%\n", "add up to 90%, not 100%"),
    ("AAA,,gold,100%\n", "unknown asset_class 'gold'"),
    ("AAA,,gold,60%\nAAA,,bond,40%\n", "unknown asset_class 'gold'"),
    ("AAA,,bond,100%\nAAA,,bond,0%\n", "duplicate bond"),
    ("AAA,soon,bond,100%\n", "as_of 'soon' is not YYYY-MM or YYYY-MM-DD"),
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
    selectors, _boxes, _out, _pie = widget.children
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
        "symbol,as_of,asset_class,weight\nAAA,,gold,60%\nAAA,,bond,40%\n")
    with pytest.raises(LoadError) as ei:
        load_test_copy()
    assert not any("add up to" in issue for issue in ei.value.issues)  # 60 + 40 is 100


@pytest.fixture(scope="function")
def unknown_split_csv(test_data_dir: Path) -> Path:
    path = test_data_dir / "allocations.csv"
    path.write_text("symbol,as_of,asset_class,weight\nAAA,,stock,70%\nAAA,,bond,30%\n")
    return path


def test_stock_of_unknown_split_is_its_own_class_between_stock_and_bond(
    unknown_split_csv: Path, two_lot_run: RunConnection,
) -> None:
    table = portfolio_allocation(two_lot_run)
    assert table.loc["stock", "balance"] > 0
    assert list(table.index).index("stock") < list(table.index).index("bond")


def test_excluded_classes_leave_the_table_and_the_totals(
    allocations_csv: Path, two_lot_run: RunConnection,
) -> None:
    everything = portfolio_allocation(two_lot_run)
    liquid = portfolio_allocation(two_lot_run, exclude=["real_estate", UNCLASSIFIED])
    assert "real_estate" not in liquid.index and UNCLASSIFIED not in liquid.index
    assert liquid.loc["Total", "balance"] == pytest.approx(
        everything.loc["Total", "balance"]
        - everything.loc["real_estate", "balance"] - everything.loc[UNCLASSIFIED, "balance"])
    assert liquid.loc["Total", "percent"] == pytest.approx(100.0)  # of what remains
    split = portfolio_allocation(two_lot_run, by="owner", exclude=["real_estate"])
    assert "real_estate" not in split.index
    with pytest.raises(ValueError, match="unknown asset class 'gold'"):
        portfolio_allocation(two_lot_run, exclude=["gold"])


def test_excluding_everything_leaves_an_empty_total(two_lot_run: RunConnection) -> None:
    table = portfolio_allocation(two_lot_run, exclude=[*asset_classes_present(two_lot_run)])
    assert list(table.index) == ["Total"] and table.loc["Total", "balance"] == 0


def test_widget_checkboxes_leave_classes_out(allocations_csv: Path, two_lot_run: RunConnection) -> None:
    widget = portfolio_allocation_widget(two_lot_run, exclude=("real_estate",))
    _selectors, boxes, _out, _pie = widget.children
    checks = {box.description: box for box in boxes.children if hasattr(box, "description") and box.description}
    assert checks["real_estate"].value is False and checks["cash"].value is True
    assert UNCLASSIFIED in checks  # the IRAs and Roths have no symbol, so they are unclassified
    heading, table = allocation_view(two_lot_run, exclude=("real_estate",))
    assert "excluding real_estate" in heading and "real_estate" not in table.index
    checks["cash"].value = False  # must not raise


def test_allocation_command_can_exclude_classes(
    allocations_csv: Path, two_lot_run_file: Path, test_data_dir: Path,
) -> None:
    result = CliRunner().invoke(main, [
        "sim", "allocation", "--exclude", "real_estate", "--exclude", "unclassified",
        "--scenario", "retire_alice", "--data-dir", str(test_data_dir),
        "--runfile", str(two_lot_run_file),
    ], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    title = " ".join(result.output.split("┏")[0].split())  # rich wraps it to the table's width
    assert title.endswith("excluding real_estate, unclassified")
    assert "real_estate " not in result.output.split("┏")[1]


def test_cd_symbol_is_its_own_class_without_any_rows(
    test_data_dir: Path, update_account_value, load_test_copy,
) -> None:
    update_account_value("Asset_Savings", "", symbol="CD")
    with ScenarioConnection(test_data_dir, "retire_alice") as scenario:
        frame = lot_classes(scenario)
        assert list(frame[frame["account"] == "Asset_Savings"]["asset_class"]) == ["cd"]
        table = portfolio_allocation(scenario)
        assert table.loc["cd", "balance"] == pytest.approx(20_000.0)
        assert list(table.index).index("cd") < list(table.index).index("cash")


def test_split_by_security_uses_the_symbol_or_a_placeholder(
    allocations_csv: Path, two_lot_run: RunConnection,
) -> None:
    table = portfolio_allocation(two_lot_run, by="security")
    assert {"AAA", "BBB", "(no symbol)"} <= set(table.columns)
    assert table.loc["bond", "AAA"] > 0 and table.loc["stock_us", "BBB"] == 0
    assert table.loc["Total", "Total"] == pytest.approx(portfolio_allocation(two_lot_run).loc["Total", "balance"])


@pytest.mark.parametrize("build", [
    portfolio_allocation_widget, account_holdings_widget, symbol_holders_widget,
    lambda rc: account_history_widget(rc, "asset", "Balances"),
])
def test_widgets_return_their_box_without_displaying_it(
    build, allocations_csv: Path, two_lot_run: RunConnection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Connections.show`` displays what a widget function returns, so displaying it
    as well puts the widget on the page twice."""
    from ipywidgets import widgets
    from analysis.sim.notebook_ui import accounts_plots, allocation_widgets, holdings_widgets

    shown: list[object] = []
    for module in (accounts_plots, allocation_widgets, holdings_widgets):
        monkeypatch.setattr(module, "display", lambda *args, **kwargs: shown.extend(args))
    box = build(two_lot_run)
    assert isinstance(box, widgets.VBox)
    assert not any(isinstance(item, widgets.Widget) for item in shown)


def test_the_pie_figure_is_closed_after_it_is_drawn(
    allocations_csv: Path, two_lot_run: RunConnection,
) -> None:
    import matplotlib.pyplot as plt

    plt.close("all")
    portfolio_allocation_widget(two_lot_run)
    assert plt.get_fignums() == []  # closed, so the inline backend does not show it again


def test_the_account_history_figure_is_displayed_and_closed(
    two_lot_run: RunConnection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import matplotlib.pyplot as plt
    from matplotlib.figure import Figure
    from analysis.sim.notebook_ui import accounts_plots

    shown: list[object] = []
    monkeypatch.setattr(accounts_plots, "display", lambda *args, **kwargs: shown.extend(args))
    plt.close("all")
    account_history_widget(two_lot_run, "asset", "Balances")
    assert any(isinstance(item, Figure) for item in shown)
    assert plt.get_fignums() == []  # closed, so the inline backend does not show it again


def test_allocation_command_labels_a_run_month_as_nominal_dollars(
    allocations_csv: Path, two_lot_run_file: Path, test_data_dir: Path,
) -> None:
    result = CliRunner().invoke(main, [
        "sim", "allocation", "--scenario", "retire_alice", "--data-dir", str(test_data_dir),
        "--runfile", str(two_lot_run_file), "--month", "2027-01",
    ], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "2027-01-31, nominal dollars" in " ".join(result.output.split())


def test_allocation_command_on_a_scenario_without_lots_prints_an_empty_table(
    test_data_dir: Path,
) -> None:
    result = CliRunner().invoke(main, [
        "sim", "allocation", "--scenario", "base", "--data-dir", str(test_data_dir), "--opening",
    ], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "no lots" in result.output and "Total" in result.output


def test_a_wide_split_keeps_every_amount_in_a_narrow_terminal(
    allocations_csv: Path, two_lot_run_file: Path, test_data_dir: Path,
) -> None:
    result = CliRunner().invoke(main, [
        "sim", "allocation", "--by", "account", "--scenario", "retire_alice",
        "--data-dir", str(test_data_dir), "--runfile", str(two_lot_run_file),
    ], env={"COLUMNS": "60"})
    assert result.exit_code == 0, result.output
    assert "…" not in result.output
    total_row = next(line for line in result.output.splitlines() if line.startswith("│ Total"))
    assert total_row.rstrip().endswith("100.0% │")  # not cropped at the terminal's edge


ADD_SYMBOL_HEADING = "### 4.11 Add a stock or fund"


@pytest.mark.parametrize("command", [["allocation"], ["prices"], ["prices", "fetch"], ["symbol", "add"]])
def test_symbol_commands_point_to_the_add_a_symbol_guide(command: list[str]) -> None:
    """Issue #127: adding a symbol is documented, not a command, so the help says where."""
    guide = Path(__file__).parents[1] / "docs" / "user-guide.md"
    assert ADD_SYMBOL_HEADING in guide.read_text()  # the section the help names still exists
    result = CliRunner().invoke(main, ["sim", *command, "--help"], terminal_width=200)
    assert result.exit_code == 0, result.output
    assert "Part 4.11" in result.output
    assert "Add a stock or fund" in " ".join(result.output.split())
