"""Local income taxes (#487): a ``state_local_tax__<state>.csv`` table of
bands on the state's taxable income (a county's flat rate, a city's
brackets), paid with the state's tax and reported apart from it."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from analysis.sim import registries
from analysis.sim.connection import RunConnection
from analysis.sim.engine import _prepare_tax, _swap_aux_table
from analysis.sim.tax_api import Extra, Return
from analysis.sim.loader import LoadError, LoadResult
from analysis.sim.reports import tax_by_year
from analysis.sim.run_charts import tax_figure

Aux = dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]]

CHECKING, SALARY = 1, 2

LOCAL_HEADER = "effective_date,filing_status,bracket_index,floor,marginal_rate\n"


def _band(floor: int, rate_micros: int, effective: str = "2026-01-31"):
    return (effective, {"filing_status": "MFJ", "floor": floor,
                        "marginal_rate_micros": rate_micros, "floor_series": None})


def _aux(*local: tuple[str, dict[str, Any]], surcharge: bool = False) -> Aux:
    """A state with a 5% flat bracket and a $10,000 deduction, and a federal
    one with the same, for an MFJ household."""
    flat = [_band(0, 50_000)]
    deduction = [("2026-01-31", {"filing_status": "MFJ", "amount": 10_000_00})]
    aux: Aux = {
        ("filing_status", None): [("2026-01-31", {"filing_status": "MFJ"})],
        ("federal_brackets", None): flat,
        ("standard_deduction", None): deduction,
        ("state_brackets", "CA"): flat,
        (registries.STATE_STANDARD_DEDUCTION, "CA"): deduction,
    }
    if local:
        aux[(registries.STATE_LOCAL_TAX, "CA")] = list(local)
    if surcharge:
        aux[(registries.STATE_SURCHARGE, "CA")] = [_band(1_000_000_00, 10_000)]
    return aux


@pytest.fixture
def ledger(ledger_con: sqlite3.Connection) -> sqlite3.Connection:
    ledger_con.executemany(
        "INSERT INTO account(id, name, kind, subkind) VALUES (?, ?, ?, ?)",
        [(CHECKING, "Asset_Checking", "asset", "cash"),
         (SALARY, "Income_Salary", "income", "salary")],
    )
    return ledger_con


def _earn(con: sqlite3.Connection, year: int, amount: int) -> None:
    con.execute(
        "INSERT INTO transfer_event(step_id, date, from_account, to_account, from_lot_id, "
        "to_lot_id, amount, requested_cents, remainder_cents, tax_character, reason_kind) "
        "VALUES (1, ?, ?, ?, ?, ?, ?, ?, 0, 'ordinary', 'rule')",
        (f"{year}-06-30", SALARY, CHECKING, SALARY, CHECKING, amount, amount),
    )


def _estimate(con: sqlite3.Connection, aux: Aux, scope: str = "state", year: int = 2026,
              more: int = 0) -> Return:
    params = {"scope": scope, **({"bracket_series": "CA"} if scope == "state" else {})}
    return _prepare_tax(con, params, aux=aux, tax_year=year, tid=1).compute(Extra(more))


def test_a_local_tax_taxes_the_states_taxable_income(ledger: sqlite3.Connection) -> None:
    """A county's flat 3% on the state's taxable income adds to the state's
    tax and is broken out of it; federal tax never reads it."""
    _earn(ledger, 2026, 110_000_00)
    taxable = 100_000_00
    state = _estimate(ledger, _aux(_band(0, 30_000)))
    assert state["local_tax"] == taxable * 3 // 100
    assert state.tax == taxable * 5 // 100 + state["local_tax"]
    assert _estimate(ledger, _aux())["local_tax"] == 0
    federal = _estimate(ledger, _aux(_band(0, 30_000)), "federal")
    assert (federal.lines.get("local_tax", 0), federal.tax) == (0, taxable * 5 // 100)
    # More income pays the local rate too (the drawdown, fill_bracket).
    assert _estimate(ledger, _aux(_band(0, 30_000)), more=100_00).tax - state.tax == 8_00


def test_a_local_tax_with_brackets_and_a_threshold(ledger: sqlite3.Connection) -> None:
    """A city's own brackets, or a tax only on income above a threshold
    (a 0% band below it), as a progressive table of its own, which adds
    to a state surcharge rather than replacing it."""
    _earn(ledger, 2026, 1_510_000_00)
    taxable = 1_500_000_00
    aux = _aux(_band(0, 0), _band(200_000_00, 10_000), _band(400_000_00, 25_000),
               surcharge=True)
    state = _estimate(ledger, aux)
    local = (400_000_00 - 200_000_00) // 100 + (taxable - 400_000_00) * 25 // 1000
    assert state["local_tax"] == local
    surcharge = (taxable - 1_000_000_00) // 100
    assert state.tax == taxable * 5 // 100 + surcharge + local


def test_a_move_takes_the_local_tax_along(ledger: sqlite3.Connection) -> None:
    """A move to another county swaps the local table alone; a move to
    another state takes the new state's, or none, keeping the old one for
    the years before the move."""
    for year in (2026, 2027, 2028):
        _earn(ledger, year, 110_000_00)
    aux = _aux(_band(0, 30_000))
    aux[(registries.STATE_LOCAL_TAX, "other_county")] = [_band(0, 20_000)]
    aux[("state_brackets", "WA")] = aux[("state_brackets", "CA")]
    _swap_aux_table(ledger, 1, "2027-01-31", {
        "aux_kind": registries.STATE_LOCAL_TAX, "series": "CA",
        "source_series": "other_county"}, aux)
    _swap_aux_table(ledger, 2, "2028-01-31", {
        "aux_kind": "state_brackets", "series": "CA", "source_series": "WA"}, aux)
    assert [_estimate(ledger, aux, year=year)["local_tax"] for year in (2026, 2027, 2028)] == [
        3_000_00, 2_000_00, 0]


def test_load_rejects_a_local_tax_without_a_state(
    write_aux: Callable[[str, str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    write_aux("state_local_tax.csv", LOCAL_HEADER + "2026-01-31,MFJ,0,0.00,3%\n")
    with pytest.raises(LoadError, match=r"state_local_tax.csv: name the state"):
        load_test_copy()


def test_load_rejects_a_local_tax_missing_a_filing_status(
    write_aux: Callable[[str, str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    """Brackets for MFJ, Single and MFS, a local tax for MFJ only: the
    survivor's Single years would tax nothing, so it fails to load."""
    write_aux("state_local_tax__CA.csv", LOCAL_HEADER + "2026-01-31,MFJ,0,0.00,3%\n")
    with pytest.raises(LoadError, match=r"state_local_tax__CA.csv \(2026-01-31\): no rows for "
                                        r"filing status MFS, Single"):
        load_test_copy()


def test_load_rejects_a_later_local_tax_update_missing_a_filing_status(
    write_aux: Callable[[str, str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    """Only the latest date's rows are in force, so an update that leaves a
    status out drops it even though an earlier date had it."""
    write_aux("state_local_tax__CA.csv",
              _local_rows("3%") + "2028-01-31,MFJ,0,0.00,3.2%\n")
    with pytest.raises(LoadError, match=r"state_local_tax__CA.csv \(2028-01-31\): no rows for "
                                        r"filing status MFS, Single"):
        load_test_copy()


def _local_rows(rate: str) -> str:
    return LOCAL_HEADER + "".join(
        f"2020-01-31,{status},0,0.00,{rate}\n" for status in ("Single", "MFJ", "MFS"))


def test_a_county_move_needs_no_local_tax_before_it(
    write_aux: Callable[[str, str], Path], append_calendar_row: Callable[..., None],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    """A household moving from a county with no local tax into one with a
    local tax swaps it in alone; the state had no table before."""
    write_aux("state_local_tax__CA-county.csv", _local_rows("3%"))
    append_calendar_row(date="2028-01", kind="swap_aux_table", series="CA",
                        params_json=json.dumps({"aux_kind": registries.STATE_LOCAL_TAX,
                                                "source_series": "CA-county"}))
    load_test_copy()
    append_calendar_row(date="2029-01", kind="swap_aux_table", series="CA",
                        params_json=json.dumps({"aux_kind": registries.STATE_LOCAL_TAX,
                                                "source_series": "CA-misspelt"}))
    with pytest.raises(LoadError, match=r"source_series names state_local_tax__CA-misspelt.csv"):
        load_test_copy()


def test_a_county_move_into_no_state_is_rejected(
    write_aux: Callable[[str, str], Path], append_calendar_row: Callable[..., None],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    """Swapped into a series no state's brackets name, the local tax would
    never be read, so the move would change nothing."""
    write_aux("state_local_tax__CA-county.csv", _local_rows("3%"))
    append_calendar_row(date="2028-01", kind="swap_aux_table", series="CAA",
                        params_json=json.dumps({"aux_kind": registries.STATE_LOCAL_TAX,
                                                "source_series": "CA-county"}))
    with pytest.raises(LoadError, match=r"series CAA names no state with brackets.*check the series' spelling"):
        load_test_copy()


def test_tax_report_shows_local_tax_apart_from_the_states(
    add_state_tax: Callable[..., None], write_aux: Callable[[str, str], Path],
    run_test_copy: Callable[[], Path],
) -> None:
    """``sim report tax`` adds local_tax after state_tax only when some year
    has one; state_tax then leaves it out, and total_tax counts it once."""
    add_state_tax(bracket_series="CA")
    first = run_test_copy()
    with RunConnection(first) as rc:
        without = tax_by_year(rc)
    assert "local_tax" not in without.columns
    first.unlink()  # run_test_copy writes one run file
    write_aux("state_local_tax__CA.csv", _local_rows("3%"))
    with RunConnection(run_test_copy()) as rc:
        frame = tax_by_year(rc)
    columns = list(frame.columns)
    assert columns.index("local_tax") == columns.index("state_tax") + 1
    year = int(frame.index[frame["local_tax"] > 0][0])
    assert frame.loc[year, "local_tax"] == pytest.approx(frame.loc[year, "state_income"] * 0.03,
                                                         abs=0.01)
    assert frame.loc[year, "state_tax"] == pytest.approx(without.loc[year, "state_tax"])
    assert frame.loc[year, "total_tax"] == pytest.approx(
        frame.loc[year, ["fed_tax", "state_tax", "local_tax"]].sum())
    # The chart stacks it on the state's.
    legend = tax_figure(frame, "tax").axes[0].get_legend_handles_labels()[1]
    assert legend[:3] == ["federal", "state", "local"]
