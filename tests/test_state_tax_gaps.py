"""California's own state tax rules (#490): the surcharge on taxable income
over $1 million, and an HSA taxed like a brokerage account."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from analysis.sim import registries
from analysis.sim.engine import TaxRule, _prepare_tax
from analysis.sim.tax_api import Extra, Return
from analysis.sim.loader import LoadError, LoadResult

Aux = dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]]

CHECKING, HSA, DEFERRED_GAINS, SALARY, MEDICAL = 1, 2, 3, 4, 5



def _aux(*, surcharge: bool = False, hsa: str | None = None) -> Aux:
    """A household in California with a 10% flat bracket and a $10,000
    deduction, and a federal tax with the same, for an MFJ household."""
    flat = [("2026-01-31", {"filing_status": "MFJ", "floor": 0, "marginal_rate_micros": 100_000})]
    deduction = [("2026-01-31", {"filing_status": "MFJ", "amount": 10_000_00})]
    aux: Aux = {
        ("filing_status", None): [("2026-01-31", {"filing_status": "MFJ"})],
        ("federal_brackets", None): flat,
        ("standard_deduction", None): deduction,
        ("state_brackets", "CA"): flat,
        (registries.STATE_STANDARD_DEDUCTION, "CA"): deduction,
        ("capital_loss_limit", None): [
            ("2026-01-31", {"filing_status": "MFJ", "amount": 3_000_00})],
    }
    if surcharge:
        aux[(registries.STATE_SURCHARGE, "CA")] = [("2005-01-31", {
            "filing_status": "MFJ", "floor": 1_000_000_00, "marginal_rate_micros": 10_000,
            "floor_series": None})]
    if hsa:
        aux[(registries.STATE_HSA, "CA")] = [("2005-01-31", {"treatment": hsa})]
    return aux


@pytest.fixture
def ledger(ledger_con: sqlite3.Connection,
           set_residence: Callable[..., None]) -> sqlite3.Connection:
    set_residence(ledger_con, ("2026-01-31", "CA", None))
    ledger_con.executemany(
        "INSERT INTO account(id, name, kind, subkind) VALUES (?, ?, ?, ?)",
        [(CHECKING, "Asset_Checking", "asset", "cash"),
         (HSA, "Asset_HSA", "asset", "hsa"),
         (DEFERRED_GAINS, registries.GROWTH_COUNTERPARTY_ACCOUNTS["deferred"].name,
          "income", "capital_gain"),
         (SALARY, "Income_Salary", "income", "salary"),
         (MEDICAL, "Expense_Medical", "expense", "healthcare")],
    )
    return ledger_con


def _post(con: sqlite3.Connection, year: int, frm: int, to: int, amount: int, character: str) -> None:
    con.execute(
        "INSERT INTO transfer_event(step_id, date, from_account, to_account, from_lot_id, "
        "to_lot_id, amount, requested_cents, remainder_cents, tax_character, reason_kind) "
        "VALUES (1, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'rule')",
        (f"{year}-06-30", frm, to, frm, to, amount, amount, character),
    )


def _estimate(con: sqlite3.Connection, aux: Aux, scope: str, year: int = 2026,
              more: int = 0) -> Return:
    params = {"scope": scope}
    kind = "state_tables" if scope == "state" else scope
    return _prepare_tax(con, params, aux=aux, tax_year=year, tid=1, kind=kind,
                        jurisdictions={"federal": TaxRule("federal", {"scope": "federal"}, 1)}).compute(Extra(more))


def test_the_surcharge_adds_its_rate_above_its_floor(ledger: sqlite3.Connection) -> None:
    _post(ledger, 2026, SALARY, CHECKING, 2_010_000_00, "ordinary")
    taxable = 2_000_000_00
    state = _estimate(ledger, _aux(surcharge=True), "state")
    assert state.tax == taxable // 10 + (taxable - 1_000_000_00) // 100
    # Below the floor it adds nothing, and federal tax never reads it.
    assert _estimate(ledger, _aux(), "state").tax == taxable // 10
    assert _estimate(ledger, _aux(surcharge=True), "federal").tax == taxable // 10
    # More income on top pays the surcharge too.
    assert _estimate(ledger, _aux(surcharge=True), "state", more=100_00).tax - state.tax == 11_00


def test_a_state_that_taxes_an_hsa_undoes_its_deduction_and_taxes_its_growth(
    ledger: sqlite3.Connection,
) -> None:
    _post(ledger, 2026, SALARY, CHECKING, 100_000_00, "ordinary")
    _post(ledger, 2026, CHECKING, HSA, 8_000_00, "tax_deferred_in")
    _post(ledger, 2026, DEFERRED_GAINS, HSA, 1_000_00, "deferred")
    _post(ledger, 2026, HSA, MEDICAL, 2_000_00, "tax_deferred_out")

    federal = _estimate(ledger, _aux(hsa="taxable"), "federal")
    assert (federal["ordinary_income"], federal["ltcg_taxed"]) == (94_000_00, 0)
    # No table, or one following the federal rules, leaves the state alone.
    for aux in (_aux(), _aux(hsa="federal")):
        state = _estimate(ledger, aux, "state")
        assert (state["ordinary_income"], state["ltcg_taxed"]) == (94_000_00, 0)
    # The contribution isn't deducted, the withdrawal was taxed already, and
    # the growth is income.
    state = _estimate(ledger, _aux(hsa="taxable"), "state")
    assert (state["ordinary_income"], state["ltcg_taxed"]) == (100_000_00, 1_000_00)
    # The after-tax drawdown prices its shares on top of the year's other
    # income: the HSA's withdrawal and growth aren't what the shares replace.
    assert (state.memo["hsa_withdrawn"], state.memo["hsa_growth"]) == (2_000_00, 1_000_00)
    # The federal memo records the HSA's money in and out (#508), which the
    # state adds back; its drawdown baseline is every traditional dollar out.
    assert (federal.memo["hsa_contributed"], federal.memo["hsa_withdrawn"]) == (8_000_00, 2_000_00)
    assert federal.memo["drawn_ordinary"] == 2_000_00
    assert "hsa_growth" not in federal.memo


def test_an_hsa_that_falls_is_a_capital_loss_for_the_state(ledger: sqlite3.Connection) -> None:
    _post(ledger, 2026, SALARY, CHECKING, 100_000_00, "ordinary")
    _post(ledger, 2026, HSA, DEFERRED_GAINS, 5_000_00, "deferred")
    _post(ledger, 2027, SALARY, CHECKING, 100_000_00, "ordinary")
    _post(ledger, 2027, DEFERRED_GAINS, HSA, 4_000_00, "deferred")

    fell = _estimate(ledger, _aux(hsa="taxable"), "state")
    assert (fell["capital_loss_used"], fell["capital_loss_carryover"]) == (3_000_00, 2_000_00)
    # The drawdown's baseline adds a fall back: it was no sale.
    assert fell.memo["hsa_growth"] == -5_000_00
    # The next year's growth first absorbs the loss carried in.
    rose = _estimate(ledger, _aux(hsa="taxable"), "state", year=2027)
    assert rose["ltcg_taxed"] == 2_000_00
    assert _estimate(ledger, _aux(hsa="taxable"), "federal")["capital_loss_used"] == 0


def test_each_year_reads_the_tables_of_the_state_it_is_filed_in(
    ledger: sqlite3.Connection, set_residence: Callable[..., None],
) -> None:
    """A tax year reads every table keyed by the state where the household
    lives on its December 31 (#509), whenever it is computed: the old
    state's surcharge and HSA rules for the years before the move, the new
    state's, or none, from the year of the move: no surcharge, and the
    federal HSA rules. An HSA year before the move stays the old state's."""
    _post(ledger, 2026, SALARY, CHECKING, 2_010_000_00, "ordinary")
    _post(ledger, 2027, SALARY, CHECKING, 2_010_000_00, "ordinary")
    _post(ledger, 2026, CHECKING, HSA, 8_000_00, "tax_deferred_in")
    _post(ledger, 2029, CHECKING, HSA, 8_000_00, "tax_deferred_in")
    aux = _aux(surcharge=True, hsa="taxable")
    aux[("state_brackets", "WA")] = aux[("state_brackets", "CA")]
    set_residence(ledger, ("2026-01-31", "CA", None), ("2027-03-31", "WA", None))
    taxable = 2_000_000_00
    before = _estimate(ledger, aux, "state", 2026)
    assert before.tax == taxable // 10 + 1_000_000_00 // 100
    assert before["ordinary_income"] == 2_010_000_00
    after = _estimate(ledger, aux, "state", 2027)
    assert after.tax == taxable // 10
    assert _estimate(ledger, aux, "state", 2029)["ordinary_income"] == -8_000_00


def test_a_move_to_a_state_whose_tables_start_later_waits_for_them(
    ledger: sqlite3.Connection, set_residence: Callable[..., None],
) -> None:
    _post(ledger, 2029, CHECKING, HSA, 8_000_00, "tax_deferred_in")
    aux = _aux()
    aux[("state_brackets", "OR")] = aux[("state_brackets", "CA")]
    aux[(registries.STATE_HSA, "OR")] = [("2030-01-31", {"treatment": "taxable"})]
    set_residence(ledger, ("2026-01-31", "CA", None), ("2028-03-31", "OR", None))
    assert _estimate(ledger, aux, "state", 2029)["ordinary_income"] == -8_000_00
    _post(ledger, 2030, CHECKING, HSA, 8_000_00, "tax_deferred_in")
    assert _estimate(ledger, aux, "state", 2030)["ordinary_income"] == 0


def test_a_move_in_the_first_months_of_a_year_files_the_year_before_in_the_old_state(
    ledger: sqlite3.Connection, set_residence: Callable[..., None],
) -> None:
    """#498: a move in February is before the April settlement of the year
    before, which is still filed where the household lived on its December
    31, with that state's surcharge."""
    _post(ledger, 2026, SALARY, CHECKING, 2_010_000_00, "ordinary")
    aux = _aux(surcharge=True)
    aux[("state_brackets", "WA")] = aux[("state_brackets", "CA")]
    set_residence(ledger, ("2026-01-31", "CA", None), ("2027-02-28", "WA", None))
    assert _estimate(ledger, aux, "state", 2026).tax == 2_000_000_00 // 10 + 1_000_000_00 // 100


def test_the_templates_ship_the_california_tables(
    write_aux: Callable[[str, str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    rows = {(kind, series): payload for kind, series, payload, _ in _aux_rows(load_test_copy())
            if kind in (registries.STATE_SURCHARGE, registries.STATE_HSA)}
    assert rows[(registries.STATE_HSA, "CA")]["treatment"] == "taxable"
    assert rows[(registries.STATE_SURCHARGE, "CA")]["floor"] == 1_000_000_00

    nameless = write_aux("state_hsa__.csv", "effective_date,treatment\n2026-01-31,taxable\n")
    with pytest.raises(LoadError, match=r"state_hsa__.csv: name the state"):
        load_test_copy()
    nameless.unlink()
    nameless = write_aux("state_surcharge.csv",
                         "effective_date,filing_status,bracket_index,floor,marginal_rate\n"
                         "2026-01-31,MFJ,0,1000000.00,1%\n")
    with pytest.raises(LoadError, match=r"state_surcharge.csv: name the state"):
        load_test_copy()
    nameless.unlink()
    write_aux("state_hsa__CA.csv", "effective_date,treatment\n2026-01-31,exempt\n")
    with pytest.raises(LoadError, match=r"state_hsa__CA.csv \(2026-01-31\): treatment 'exempt'"):
        load_test_copy()


def _aux_rows(result: LoadResult) -> list[tuple[str, str | None, dict[str, Any], str]]:
    con = sqlite3.connect(result.intent_path)
    try:
        return [(kind, series, json.loads(payload), effective) for kind, series, payload, effective
                in con.execute("SELECT kind, series, payload_json, effective_date FROM aux_table")]
    finally:
        con.close()


def test_the_old_states_later_surcharge_never_applies_after_a_move(
    ledger: sqlite3.Connection, set_residence: Callable[..., None],
) -> None:
    _post(ledger, 2031, SALARY, CHECKING, 110_000_00, "ordinary")
    aux = _aux()
    aux[("state_brackets", "WA")] = aux[("state_brackets", "CA")]
    aux[(registries.STATE_SURCHARGE, "CA")] = [("2031-01-31", {
        "filing_status": "MFJ", "floor": 0, "marginal_rate_micros": 10_000, "floor_series": None})]
    set_residence(ledger, ("2026-01-31", "CA", None), ("2029-03-31", "WA", None))
    assert _estimate(ledger, aux, "state", 2031).tax == 100_000_00 // 10
