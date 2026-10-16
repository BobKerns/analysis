"""Scenario-compare widgets for lots: definition differences, and the account
diffs' lot drill-down with its Measure toggle."""

from __future__ import annotations

from typing import Any

import ipywidgets as widgets
import matplotlib
import pytest

matplotlib.use("Agg")

from analysis.sim.connection import RunConnection
from analysis.sim.notebook_ui.lot_compare import (
    account_definition_differences,
    year_to_date_differences,
)
from analysis.sim.notebook_ui.pickers import AccountPicker

BROKERAGE = "Asset_Brokerage_Joint"
SCENARIOS = ["retire_alice", "variant"]
Runs = tuple[RunConnection, dict[str, RunConnection]]


def _connections(runs: Runs) -> dict[str, RunConnection]:
    base, variants = runs
    return {"retire_alice": base, **variants}


def test_only_changed_definitions_are_listed_lot_by_lot(lot_variant_runs: Runs) -> None:
    diff = account_definition_differences(_connections(lot_variant_runs), SCENARIOS, "retire_alice")
    keys = set(diff.index)
    assert {(BROKERAGE, "b", "quantity"), (BROKERAGE, "b", "opening_balance"),
            (BROKERAGE, "b", "opening_cost_basis")} <= keys
    assert diff.loc[(BROKERAGE, "b", "opening_balance")].to_list() == ["$200,000.00", "$150,000.00"]
    assert diff.loc[(BROKERAGE, "b", "quantity")].to_list() == ["2,000.5", "1,500"]
    # Unchanged fields, unchanged lots and unchanged accounts do not appear.
    assert (BROKERAGE, "b", "symbol") not in keys and (BROKERAGE, "a", "symbol") not in keys
    assert {account for account, _lot, _field in keys} == {BROKERAGE}


def test_a_lot_only_one_scenario_has_shows_as_absent_in_the_other(lot_variant_runs: Runs) -> None:
    diff = account_definition_differences(_connections(lot_variant_runs), SCENARIOS, "retire_alice")
    assert diff.loc[(BROKERAGE, "c", "(exists)")].isna().to_list() == [True, False]
    assert diff.loc[(BROKERAGE, "c", "symbol")].to_list()[1] == "CCC"
    assert diff.loc[(BROKERAGE, "c", "opening_balance")].to_list()[1] == "$25,000.00"
    # Sorted by lot, then by field order with existence first.
    fields_of_c = [field for account, lot, field in diff.index if lot == "c"]
    assert fields_of_c[0] == "(exists)"
    assert fields_of_c.index("symbol") < fields_of_c.index("opening_balance")


def test_identical_scenarios_have_no_definition_differences(lot_variant_runs: Runs) -> None:
    base, _ = lot_variant_runs
    diff = account_definition_differences({"a": base, "b": base}, ["a", "b"], "a")
    assert diff.empty
    assert year_to_date_differences({"a": base, "b": base}, ["a", "b"], "a").empty


def test_year_to_date_rows_are_compared_by_name(lot_variant_runs: Runs) -> None:
    diff = year_to_date_differences(_connections(lot_variant_runs), SCENARIOS, "retire_alice")
    assert list(diff.index) == [("prior_ordinary_wages", "amount")]  # inherited rows are not repeated
    assert diff.iloc[0].to_list() == ["$62,000.00", "$70,000.00"]


def _account_diffs(runs: Runs, monkeypatch: pytest.MonkeyPatch, **kwargs: Any):
    """The account diffs widget, its account picker and Measure toggle, and
    the list its tables are displayed into."""
    from analysis.sim.notebook_ui import compare_plots

    base, variants = runs
    shown: list[Any] = []
    monkeypatch.setattr(compare_plots, "display", lambda *args, **_: shown.extend(args))
    box = compare_plots.show_account_balance_diffs(None, "retire_alice", base, variants, **kwargs)
    picker = next(child for child in box.children if isinstance(child, AccountPicker))
    modes = next(child for child in box.children
                 if isinstance(child, widgets.HBox) and not isinstance(child, AccountPicker))
    measure = modes.children[-1]
    picker.kind_picker.value = "asset"
    picker.account_picker.value = (BROKERAGE,)
    return picker, measure, shown


def test_account_diffs_measure_sits_right_of_the_modes(
    lot_variant_runs: Runs, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from analysis.sim.notebook_ui.pickers import DollarModePicker, ValuePlotModePicker

    dollars, values = DollarModePicker(), ValuePlotModePicker()
    _picker, measure, _shown = _account_diffs(lot_variant_runs, monkeypatch,
                                              dollar_mode=dollars, value_mode=values)
    assert measure.description == "Measure:" and measure.value == "balance"
    assert [key for _text, key in measure.options] == ["balance", "cost_basis", "quantity"]


@pytest.mark.parametrize("measure", ["balance", "cost_basis", "quantity"])
def test_account_diffs_show_one_lots_measure_month_by_month(
    lot_variant_runs: Runs, monkeypatch: pytest.MonkeyPatch, measure: str,
) -> None:
    picker, measure_picker, shown = _account_diffs(lot_variant_runs, monkeypatch)
    picker.lot_picker.picker.value = ("b",)
    measure_picker.value = measure
    frame = shown[-1].data
    assert list(frame.columns) == ["baseline", "variant", "Δvariant"]
    assert not frame.empty
    assert (frame["Δvariant"] == frame["variant"] - frame["baseline"]).all()
    first = frame.iloc[0]
    expected_start = {"balance": -50_000.0, "cost_basis": -40_000.0, "quantity": -500.5}[measure]
    # Growth and sales move both runs, so only the size and sign of the gap are stable.
    assert first["Δvariant"] < 0 and abs(first["Δvariant"] - expected_start) < abs(expected_start) * 0.5


def test_a_lot_the_baseline_lacks_counts_as_zero_there(
    lot_variant_runs: Runs, monkeypatch: pytest.MonkeyPatch,
) -> None:
    picker, measure_picker, shown = _account_diffs(lot_variant_runs, monkeypatch)
    picker.lot_picker.picker.value = ("c",)
    measure_picker.value = "cost_basis"
    frame = shown[-1].data
    assert (frame["baseline"] == 0).all()
    assert frame["variant"].iloc[0] == 20_000.0


def test_cpi_dollars_leave_quantities_alone(
    lot_variant_runs: Runs, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from analysis.sim.notebook_ui.pickers import DollarModePicker
    from analysis.sim.types import DollarMode

    dollars = DollarModePicker(DollarMode.NOMINAL)
    picker, measure_picker, shown = _account_diffs(lot_variant_runs, monkeypatch,
                                                   dollar_mode=dollars)
    picker.lot_picker.picker.value = ("b",)
    nominal = shown[-1].data
    dollars.value = DollarMode.CPI_CONSTANT
    real = shown[-1].data
    assert real["baseline"].iloc[-1] < nominal["baseline"].iloc[-1]  # inflation shrinks later dollars
    measure_picker.value = "quantity"
    shares_real = shown[-1].data
    dollars.value = DollarMode.NOMINAL
    assert shares_real["baseline"].equals(shown[-1].data["baseline"])


def test_measure_appears_only_for_a_picked_lot(
    lot_variant_runs: Runs, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Summed over an account's lots, basis and quantity would mislead, so
    Measure shows only once one lot is picked, and All lots is a balance."""
    from analysis.sim.notebook_ui import compare_plots

    picker, measure_picker, shown = _account_diffs(lot_variant_runs, monkeypatch)
    assert measure_picker.layout.display == "none"
    picker.lot_picker.picker.value = ("b",)
    assert measure_picker.layout.display is None
    measure_picker.value = "quantity"
    monkeypatch.setattr(compare_plots, "to_markdown", lambda *lines: "\n".join(lines))
    picker.lot_picker.picker.value = ()
    assert measure_picker.layout.display == "none"
    assert shown[-2] == f"### {BROKERAGE}"  # the account's balance, not a quantity
