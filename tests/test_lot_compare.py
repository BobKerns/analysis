"""Scenario-compare widgets for lots: definition differences and lot balance diffs."""

from __future__ import annotations

import matplotlib
import pytest

matplotlib.use("Agg")

from analysis.sim.connection import RunConnection
from analysis.sim.notebook_ui.lot_compare import (
    account_definition_differences,
    differing_lots,
    lot_frame,
    show_lot_balance_diffs,
    year_to_date_differences,
)

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


def test_lots_worth_comparing_come_from_accounts_with_several_lots(lot_variant_runs: Runs) -> None:
    base, variants = lot_variant_runs
    labels, added, deleted = differing_lots(base, variants)
    assert labels == [f"{BROKERAGE}/b", f"{BROKERAGE}/c"]  # a is unchanged; single-lot accounts are out
    assert added == {"variant": [f"{BROKERAGE}/c"]} and deleted == {"variant": []}


@pytest.mark.parametrize("measure", ["balance", "cost_basis", "quantity"])
def test_lot_frame_shows_the_month_by_month_difference(lot_variant_runs: Runs, measure: str) -> None:
    base, variants = lot_variant_runs
    frame = lot_frame(base, variants, f"{BROKERAGE}/b", measure)
    assert list(frame.columns) == ["baseline", "variant", "Δvariant"]
    assert not frame.empty
    assert (frame["Δvariant"] == frame["variant"] - frame["baseline"]).all()
    first = frame.iloc[0]
    expected_start = {"balance": -50_000.0, "cost_basis": -40_000.0, "quantity": -500.5}[measure]
    # Growth and sales move both runs, so only the size and sign of the gap are stable.
    assert first["Δvariant"] < 0 and abs(first["Δvariant"] - expected_start) < abs(expected_start) * 0.5


def test_a_lot_the_baseline_lacks_counts_as_zero_there(lot_variant_runs: Runs) -> None:
    base, variants = lot_variant_runs
    frame = lot_frame(base, variants, f"{BROKERAGE}/c", "cost_basis")
    assert (frame["baseline"] == 0).all()
    assert frame["variant"].iloc[0] == 20_000.0


def test_cpi_dollars_and_monthly_changes(lot_variant_runs: Runs) -> None:
    base, variants = lot_variant_runs
    nominal = lot_frame(base, variants, f"{BROKERAGE}/b", "balance")
    real = lot_frame(base, variants, f"{BROKERAGE}/b", "balance", cpi=True)
    assert real["baseline"].iloc[-1] < nominal["baseline"].iloc[-1]  # inflation shrinks later dollars
    shares = lot_frame(base, variants, f"{BROKERAGE}/b", "quantity", cpi=True)
    assert shares["baseline"].iloc[0] == lot_frame(base, variants, f"{BROKERAGE}/b", "quantity")["baseline"].iloc[0]
    monthly = lot_frame(base, variants, f"{BROKERAGE}/c", "balance", monthly=True)
    assert monthly["variant"].abs().max() < 25_000.0  # changes, not levels


def test_the_widget_offers_the_differing_lots_and_measures(lot_variant_runs: Runs) -> None:
    base, variants = lot_variant_runs
    box = show_lot_balance_diffs(None, baseline="retire_alice", baseline_rc=base, variant_rcs=variants)
    lot_picker, measure_picker = box.children[1].children
    assert lot_picker.description == "Lot:"
    assert list(lot_picker.options) == [f"{BROKERAGE}/b", f"{BROKERAGE}/c"]
    assert [key for _text, key in measure_picker.options] == ["balance", "cost_basis", "quantity"]
    for label in lot_picker.options:
        lot_picker.value = label
        for _text, key in measure_picker.options:
            measure_picker.value = key  # must not raise


def test_the_widget_says_so_when_nothing_differs(lot_variant_runs: Runs) -> None:
    base, _ = lot_variant_runs
    box = show_lot_balance_diffs(None, baseline="a", baseline_rc=base, variant_rcs={"b": base})
    assert len(box.children) == 3  # mode toggles, heading, and the message: no pickers


def test_the_definitions_widget_shows_both_tables(lot_variant_runs: Runs, capsys) -> None:
    from types import SimpleNamespace

    from analysis.sim.notebook_ui.lot_compare import account_definitions_widget

    account_definitions_widget(
        SimpleNamespace(ok=True), _connections(lot_variant_runs), SCENARIOS, ["variant"],
        baseline="retire_alice",
    )
    shown = capsys.readouterr().out
    assert "opening_balance" in shown and "prior_ordinary_wages" in shown
