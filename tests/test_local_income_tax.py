"""Local income taxes (#487): a ``state_local_tax__<county>.csv`` table of
bands on the state's taxable income (a county's flat rate, a city's
brackets), read by the local jurisdiction (``taxes/local.csv``, kind
``local_bands``, #508), which builds on the state's return and is paid with
the state's tax by default. The county is where the household lives
(``residence.csv``, #509); without one, the state's own table."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from analysis.sim import registries
from analysis.sim.connection import RunConnection
from analysis.sim.engine import EngineError, TaxRule, _prepare_tax
from analysis.sim.tax_api import Extra, Return
from analysis.sim.loader import LoadError, LoadResult
from analysis.sim.reports import tax_by_year, tax_reconciliation
from analysis.sim.run_charts import tax_figure

Aux = dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]]

CHECKING, SALARY = 1, 2

LOCAL_HEADER = "effective_date,filing_status,bracket_index,floor,marginal_rate\n"


def _band(floor: int, rate_micros: int, effective: str = "2026-01-31"):
    return (effective, {"filing_status": "MFJ", "floor": floor,
                        "marginal_rate_micros": rate_micros, "floor_series": None})



def _aux(*local: tuple[str, dict[str, Any]], surcharge: bool = False) -> Aux:
    """A household in California with a 5% flat bracket and a $10,000
    deduction, and a federal tax with the same, for an MFJ household."""
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
def ledger(ledger_con: sqlite3.Connection,
           set_residence: Callable[..., None]) -> sqlite3.Connection:
    set_residence(ledger_con, ("2026-01-31", "CA", None))
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


JURISDICTIONS = {
    "federal": TaxRule("federal", {"scope": "federal"}, 1),
    "state": TaxRule("state_tables", {"scope": "state"}, 2),
}


def _estimate(con: sqlite3.Connection, aux: Aux, scope: str = "local", year: int = 2026,
              more: int = 0) -> Return:
    rule = JURISDICTIONS.get(scope, TaxRule("local_bands", {"scope": "local"}, 3))
    return _prepare_tax(con, rule.params, aux=aux, tax_year=year, tid=rule.id, kind=rule.kind,
                        jurisdictions=JURISDICTIONS).compute(Extra(more))


def test_a_local_tax_taxes_the_states_taxable_income(ledger: sqlite3.Connection) -> None:
    """A county's flat 3% on the state's taxable income is the local
    jurisdiction's tax; the state's and the federal tax never read it."""
    _earn(ledger, 2026, 110_000_00)
    taxable = 100_000_00
    local = _estimate(ledger, _aux(_band(0, 30_000)))
    assert (local.taxable_income, local.tax) == (taxable, taxable * 3 // 100)
    assert _estimate(ledger, _aux()).tax == 0
    for scope in ("state", "federal"):
        ret = _estimate(ledger, _aux(_band(0, 30_000)), scope)
        assert ("local_tax" not in ret.lines, ret.tax) == (True, taxable * 5 // 100)
    # More income on the state's return pays the local rate too (the
    # drawdown, fill_bracket).
    assert _estimate(ledger, _aux(_band(0, 30_000)), more=100_00).tax - local.tax == 3_00


def test_a_local_tax_with_brackets_and_a_threshold(ledger: sqlite3.Connection) -> None:
    """A city's own brackets, or a tax only on income above a threshold
    (a 0% band below it), as a progressive table of its own, apart from a
    state surcharge."""
    _earn(ledger, 2026, 1_510_000_00)
    taxable = 1_500_000_00
    aux = _aux(_band(0, 0), _band(200_000_00, 10_000), _band(400_000_00, 25_000),
               surcharge=True)
    local = (400_000_00 - 200_000_00) // 100 + (taxable - 400_000_00) * 25 // 1000
    assert _estimate(ledger, aux).tax == local
    surcharge = (taxable - 1_000_000_00) // 100
    assert _estimate(ledger, aux, "state").tax == taxable * 5 // 100 + surcharge


def test_a_county_names_its_own_bands(ledger: sqlite3.Connection, set_residence: Callable[..., None]) -> None:
    """A county where the household lives reads its own table; with none
    named, the state's."""
    _earn(ledger, 2026, 110_000_00)
    aux = _aux(_band(0, 30_000))
    aux[(registries.STATE_LOCAL_TAX, "CA-city")] = [_band(0, 10_000)]
    set_residence(ledger, ("2026-01-31", "CA", "CA-city"))
    assert _estimate(ledger, aux).tax == 100_000_00 // 100


def test_a_move_takes_the_local_tax_along(ledger: sqlite3.Connection, set_residence: Callable[..., None]) -> None:
    """A move to another county reads its table; a move to another state
    its, or none. Each year is taxed where the household lives on its
    December 31, whenever it is computed."""
    for year in (2026, 2027, 2028):
        _earn(ledger, year, 110_000_00)
    aux = _aux(_band(0, 30_000))
    aux[(registries.STATE_LOCAL_TAX, "other_county")] = [_band(0, 20_000)]
    aux[("state_brackets", "WA")] = aux[("state_brackets", "CA")]
    set_residence(ledger, 
        ("2026-01-31", "CA", None), ("2027-12-31", "CA", "other_county"), ("2028-01-31", "WA", None))
    assert [_estimate(ledger, aux, year=year).tax for year in (2026, 2027, 2028)] == [
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
    write_aux: Callable[[str, str], Path], add_residence: Callable[..., Path],
    load_test_copy: Callable[[], LoadResult], add_state_tax: Callable[..., None],
    add_tax: Callable[..., Path],
) -> None:
    """A household moving from a county with no local tax into one with a
    local tax names the county; the state has no table of its own. A
    county with no table is refused."""
    add_state_tax()
    add_tax("local")
    write_aux("state_local_tax__CA-county.csv", _local_rows("3%"))
    add_residence("2028-01", "CA", county="CA-county")
    load_test_copy()
    add_residence("2029-01", "CA", county="CA-misspelt")
    with pytest.raises(LoadError, match=r"county 'CA-misspelt' has no "
                                        r"aux/state_local_tax__CA-misspelt\.csv"):
        load_test_copy()


def test_the_state_and_local_files_take_no_bracket_series(
    add_state_tax: Callable[..., None], add_tax: Callable[..., Path],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    """Where the household lives is residence.csv now (#509), so the
    params that named a state or a county's table are refused, naming the
    migration."""
    add_state_tax()
    add_tax("local", params_json={"bracket_series": "CA-city"})
    with pytest.raises(LoadError, match=r"local.csv.*takes no 'bracket_series'.*migrate_509"):
        load_test_copy()


def test_a_swap_of_a_state_table_is_refused(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    """A move is a residence row now; a swap of a table keyed by the state
    names the command and the migration."""
    append_calendar_row(date="2028-01", kind="swap_aux_table", series="CA",
                        params_json=json.dumps({"aux_kind": registries.STATE_LOCAL_TAX,
                                                "source_series": "CA-county"}))
    with pytest.raises(LoadError, match=r"swap_aux_table of state_local_tax: where the household "
                                        r"lives is residence.csv now.*`sim tax move`"):
        load_test_copy()


def test_tax_report_shows_local_tax_apart_from_the_states(
    add_state_tax: Callable[..., None], add_tax: Callable[..., Path],
    write_aux: Callable[[str, str], Path], run_test_copy: Callable[[], Path],
) -> None:
    """``sim report tax`` adds local_tax after state_tax only when some year
    has one, and total_tax counts it once."""
    add_state_tax("CA")
    first = run_test_copy()
    with RunConnection(first) as rc:
        without = tax_by_year(rc)
    assert "local_tax" not in without.columns
    first.unlink()  # run_test_copy writes one run file
    write_aux("state_local_tax__CA.csv", _local_rows("3%"))
    add_tax("local")
    run = run_test_copy()
    with RunConnection(run) as rc:
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
    # Each paid in full into its own account (#586): the state's payment
    # doesn't count toward the local tax.
    con = sqlite3.connect(run)
    try:
        summary = dict(con.execute(
            "SELECT bucket, amount FROM tax_year_summary WHERE year = ?", (year,)).fetchall())
        paid = dict(con.execute(
            "SELECT j.name, SUM(t.amount) FROM transfer_event t "
            "JOIN tax_jurisdiction j ON j.id = t.reason_id "
            "WHERE t.reason_kind = 'tax' AND t.tax_year = ? GROUP BY j.name", (year,)).fetchall())
    finally:
        con.close()
    assert summary["local_tax_paid"] == 0 and summary["local_tax_due"] == summary["local_tax"]
    assert paid["local"] == summary["local_tax"] and paid["state"] == summary["state_tax_due"]
    assert "local_drawdown_tax" in summary and "state_local_tax" not in summary
    # The year's liability, what's still owed at its end, and the April
    # reconciliation count the local tax, no longer inside the state's.
    with RunConnection(run) as rc:
        liability = rc.tax_liability_by_year()[year]
        outstanding = rc.outstanding_tax_by_year()[year]
        reconciliation = tax_reconciliation(rc)
    scopes = ("federal", "state", "local")
    assert liability == pytest.approx(sum(summary[f"{s}_tax"] for s in scopes) / 100)
    assert outstanding == pytest.approx(sum(summary[f"{s}_tax_due"] for s in scopes) / 100)
    assert reconciliation.loc[year, "local_liability"] == pytest.approx(summary["local_tax"] / 100)
    assert reconciliation.loc[year, "local_due"] == pytest.approx(summary["local_tax_due"] / 100)


def test_local_bands_without_a_local_file_name_the_command(
    add_state_tax: Callable[..., None], write_aux: Callable[[str, str], Path],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    add_state_tax()
    write_aux("state_local_tax__CA.csv", _local_rows("3%"))
    with pytest.raises(LoadError, match=r"aux/state_local_tax__CA\.csv: a local income tax is its "
                                        r"own jurisdiction now.*`sim tax add local`"):
        load_test_copy()


def test_a_local_tax_needs_the_state_it_builds_on(
    add_tax: Callable[..., Path], write_aux: Callable[[str, str], Path],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    add_tax()
    add_tax("local")
    write_aux("state_local_tax__CA.csv", _local_rows("3%"))
    with pytest.raises(LoadError, match=r"local_bands calculator builds on the state tax"):
        load_test_copy()


def test_an_upstreams_error_names_its_own_jurisdiction(ledger: sqlite3.Connection) -> None:
    """Prepared for the local tax (id 3), through the state's (2), the
    federal year names its own jurisdiction (1) in its errors."""
    _earn(ledger, 2026, 110_000_00)
    aux = _aux(_band(0, 30_000))
    del aux[("filing_status", None)]
    with pytest.raises(EngineError, match=r"tax jurisdiction id=1: no filing_status row"):
        _estimate(ledger, aux)
