"""The tax calculator API (#507): a calculator computes a return from a
:class:`TaxYear` alone, so a year built by hand, with no run file or
scenario behind it, is enough to test one."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from analysis.sim import tax_api
from analysis.sim.tax_api import (
    Account,
    Extra,
    IncomeItem,
    Return,
    TaxCalculator,
    TaxError,
    TaxYear,
    calculator,
)

YEAR = 2030
SALARY = Account("Income_Salary", "income", "salary", "alice")
CHECKING = Account("Asset_Checking", "asset", "cash", None)
BROKERAGE = Account("Asset_Brokerage", "asset", "brokerage", "alice")

MICROS = 1_000_000
TABLES: dict[tuple[str, str | None], list[dict[str, Any]]] = {
    ("standard_deduction", None): [{"filing_status": "MFJ", "amount": 30_000_00}],
    ("federal_brackets", None): [
        {"filing_status": "MFJ", "floor": 0, "marginal_rate_micros": MICROS // 10},
        {"filing_status": "MFJ", "floor": 20_000_00, "marginal_rate_micros": MICROS // 5},
    ],
    ("ltcg_brackets", None): [
        {"filing_status": "MFJ", "floor": 0, "marginal_rate_micros": 0},
        {"filing_status": "MFJ", "floor": 80_000_00, "marginal_rate_micros": MICROS * 15 // 100},
    ],
    ("state_brackets", "CA"): [
        {"filing_status": "MFJ", "floor": 0, "marginal_rate_micros": MICROS // 20},
    ],
}


def _item(character: str, amount: int, *, year: int = YEAR,
          payer: Account = SALARY, payee: Account = CHECKING) -> IncomeItem:
    return IncomeItem(year, character, payer, payee, amount)


def _year(
    items: Sequence[IncomeItem], *, jurisdiction: str = "federal",
    params: Mapping[str, Any] | None = None,
    tables: Mapping[tuple[str, str | None], list[dict[str, Any]]] = TABLES,
    prior: Mapping[str, int] | None = None,
) -> TaxYear:
    """A tax year from plain data: every table in force from 2020, and
    ``prior`` last year's return lines."""
    def keyed(kind: str, column: str, value: str, _year: int) -> dict[str, Any] | None:
        rows = [r for r in tables.get((kind, None), []) if r.get(column) == value]
        return rows[-1] if rows else None

    return TaxYear(
        year=YEAR, jurisdiction=jurisdiction, params=dict(params or {}), items=list(items),
        losses=[], persons=[],
        _table=lambda kind, series, _year: list(tables.get((kind, series), [])),
        _indexed=lambda kind, series: list(tables.get((kind, series), [])),
        _dated=lambda kind, series: [("2020-01-01", r) for r in tables.get((kind, series), [])],
        _keyed=keyed,
        _filing_status_in=lambda _year: "MFJ",
        _prior=lambda line: (prior or {}).get(line, 0),
        _series_of=lambda kind: sorted(s for k, s in tables if k == kind and s is not None),
    )


def _federal(items: Sequence[IncomeItem], **kw: Any) -> Return:
    return calculator("federal").prepare(_year(items, **kw)).compute()


def test_the_federal_calculator_taxes_wages_at_its_brackets() -> None:
    ret = _federal([_item("ordinary", 100_000_00)])
    assert set(Return.REQUIRED) <= set(ret.lines)
    assert (ret.agi, ret.taxable_income, ret.ordinary_taxable) == (100_000_00, 70_000_00, 70_000_00)
    assert ret["deduction"] == 30_000_00
    assert ret.tax == 20_000_00 * 10 // 100 + 50_000_00 * 20 // 100


def test_a_what_if_adds_income_on_top_of_the_years() -> None:
    prepared = calculator("federal").prepare(_year([_item("ordinary", 100_000_00)]))
    assert prepared.compute(Extra(ordinary=10_000_00)).tax - prepared.compute().tax == 2_000_00
    assert prepared.compute().tax == prepared.compute().tax  # computing again changes nothing
    assert prepared.brackets == [(0, MICROS // 10), (20_000_00, MICROS // 5)]


def test_long_term_gain_is_taxed_on_top_of_ordinary_taxable_income() -> None:
    ret = _federal([_item("ordinary", 50_000_00),
                    _item("ltcg", 100_000_00, payer=BROKERAGE, payee=CHECKING)])
    assert (ret.taxable_income, ret.ordinary_taxable) == (120_000_00, 20_000_00)
    assert ret["ltcg_taxed"] == 100_000_00
    # 20,000 ordinary at 10%; the gain fills 20,000-80,000 at 0%, the rest at 15%.
    assert ret.tax == 2_000_00 + 40_000_00 * 15 // 100


def test_a_carried_in_operating_loss_comes_off_taxable_income() -> None:
    ret = _federal([_item("ordinary", 100_000_00)], prior={"nol_carryover": 10_000_00})
    # 80% of the 70,000 taxable income may be offset, so all 10,000 is used.
    assert (ret["nol_used"], ret.taxable_income, ret["nol_carryover"]) == (10_000_00, 60_000_00, 0)


def test_the_state_calculator_reads_the_states_brackets() -> None:
    year = _year([_item("ordinary", 100_000_00)], jurisdiction="state",
                 params={"bracket_series": "CA"})
    ret = calculator("state_tables").prepare(year).compute()
    # No state_standard_deduction__CA: the federal one stands in.
    assert ret.taxable_income == 70_000_00
    assert ret.tax == 70_000_00 * 5 // 100
    assert "magi" not in ret.lines and "local_tax" in ret.lines


def test_a_missing_table_is_a_tax_error() -> None:
    tables = {k: v for k, v in TABLES.items() if k != ("federal_brackets", None)}
    with pytest.raises(TaxError, match="missing brackets"):
        calculator("federal").prepare(_year([_item("ordinary", 1_00)], tables=tables))


def test_an_unknown_kind_is_a_tax_error() -> None:
    with pytest.raises(TaxError, match="unknown tax calculator kind 'nope'"):
        calculator("nope")


def test_a_kind_registers_once() -> None:
    calculator("federal")  # the built-ins are registered

    class Again(TaxCalculator):
        kind = "federal"
        jurisdiction = "federal"

    with pytest.raises(ValueError, match="registered twice"):
        tax_api.register(Again)
