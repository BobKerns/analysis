"""The state bases (#508): a state that starts from the federal return and
one that builds its own income, on tax years built by hand."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from analysis.sim import tax_api
from analysis.sim.tax_api import NO_EXTRA, Extra, TaxError, calculator
from analysis.sim.taxes import common
from analysis.sim.taxes.state_base import (
    Adjustment, Basis, FederalStateYear, Income, StateFromFederal, StateFromIncome, StateYear,
)
from tests.test_tax_api import MICROS, TABLES, _item, _year

SS = _item("ss_benefit", 40_000_00)
SALARY_100K = _item("ordinary", 100_000_00)
SS_TABLES: dict[tuple[str, str | None], list[dict[str, Any]]] = {
    **TABLES,
    ("ss_taxation_thresholds", None): [
        {"filing_status": "MFJ", "tier1_floor": 32_000_00, "tier2_floor": 44_000_00}],
}


class _FlatYear(FederalStateYear):
    """A state at 5% of federal AGI with one addition: a tenth of federal
    AGI, read from the federal return; Social Security left out; no
    deduction; a $100 credit."""

    def setup(self) -> None:
        self.brackets = [(0, MICROS // 20)]

    def deductions(self) -> Mapping[str, int]:
        return {"deduction": 0}

    def additions(self, income: Income, basis: Basis) -> Sequence[Adjustment]:
        return (Adjustment("tenth_of_federal", ordinary=basis.federal.agi // 10),)

    def subtractions(self, income: Income, basis: Basis) -> Sequence[Adjustment]:
        return (Adjustment("no_ss", ss=income.ss),)

    def credits(self, tax: int, income: Income, basis: Basis) -> int:
        return 100_00


class _Flat(StateFromFederal):
    kind = "test_flat"
    jurisdiction = "state"
    year_class = _FlatYear


def _flat(items: Sequence[Any]) -> FederalStateYear:
    year = _year(items, jurisdiction="state", tables=SS_TABLES)
    return _FlatYear(_Flat(), year)


def test_a_state_starts_from_the_federal_return_and_adds_its_lines() -> None:
    state = _flat([SALARY_100K]).compute()
    # Federal AGI 100,000, plus a tenth of it.
    assert state.agi == 110_000_00
    assert state["tenth_of_federal"] == 10_000_00
    assert state.tax == 110_000_00 // 20 - 100_00
    assert state["credits"] == 100_00


def test_a_subtraction_takes_off_the_kind_it_names() -> None:
    year = _flat([SALARY_100K, SS])
    federal = calculator("federal").prepare(_year([SALARY_100K, SS], tables=SS_TABLES)).compute()
    assert federal["ss_taxable"] > 0
    state = year.compute()
    assert state["ss_taxable"] == 0 and state["no_ss"] == federal["ss_taxable"]
    assert state.agi == 110_000_00 + federal["ss_taxable"] // 10


def test_a_what_if_moves_the_federal_return_the_state_reads() -> None:
    year = _flat([SALARY_100K])
    more = year.compute(Extra(10_000_00))
    # The extra income, and a tenth of it again through the federal AGI.
    assert more.agi - year.compute().agi == 11_000_00
    assert more["tenth_of_federal"] == 11_000_00


def test_credits_never_take_the_tax_below_zero() -> None:
    state = _flat([_item("ordinary", 1_000_00)]).compute()
    assert (state.tax, state["credits"]) == (0, 1_100_00 // 20)


def test_a_year_without_its_upstream_is_a_tax_error() -> None:
    # A year the engine built for a calculator that builds on nothing.
    alone = dataclasses.replace(_year([SALARY_100K], jurisdiction="state"),
                                _upstream=tax_api._no_upstream)
    with pytest.raises(TaxError, match="no upstream jurisdiction 'federal'"):
        _FlatYear(_Flat(), alone)


class _OwnIncomeYear(StateYear):
    """A state taxing ordinary income at a flat 3% after a $10,000
    deduction, from the year's own items: no Social Security, no gains."""

    def setup(self) -> None:
        self.brackets = [(0, MICROS * 3 // 100)]

    def start(self, basis: Basis) -> Income:
        ordinary, _ss, _dividends = common.income_buckets(self.ty)
        return Income(ordinary=ordinary + basis.extra.ordinary)

    def deductions(self) -> Mapping[str, int]:
        return {"deduction": 10_000_00}


class _OwnIncome(StateFromIncome):
    kind = "test_own_income"
    jurisdiction = "state"
    year_class = _OwnIncomeYear


def test_a_state_from_its_own_income_reads_no_federal_return() -> None:
    assert _OwnIncome.upstream == ()
    prepared = _OwnIncomeYear(_OwnIncome(), _year([SALARY_100K, SS], jurisdiction="state"))
    state = prepared.compute()
    assert (state.agi, state.taxable_income) == (100_000_00, 90_000_00)
    assert state.tax == 90_000_00 * 3 // 100
    assert prepared.compute(Extra(1_000_00)).tax - state.tax == 30_00
    with pytest.raises(TaxError, match="no federal return"):
        _ = prepared.basis(NO_EXTRA).federal


# -- state_tables on the federal return -------------------------------------

CA_SS: dict[tuple[str, str | None], list[dict[str, Any]]] = {
    **SS_TABLES,
    # Half of taxable Social Security left out, phased out from $100,000 of
    # federal AGI to none at $140,000.
    ("state_social_security", "CA"): [
        {"filing_status": "MFJ", "exclusion_micros": MICROS // 2,
         "phaseout_start": 100_000_00, "phaseout_end": 140_000_00}],
    # Half of a long-term gain left out of the state's income.
    ("state_ltcg_exclusion", "CA"): [{"exclusion_micros": MICROS // 2}],
}


def _state_tables(items: Sequence[Any], tables: Mapping[tuple[str, str | None], list[dict[str, Any]]]):
    year = _year(items, jurisdiction="state", tables=tables)
    federal = calculator("federal").prepare(_year(items, tables=tables)).compute()
    return calculator("state_tables").prepare(year).compute(), federal


def test_state_social_security_phases_out_by_federal_agi() -> None:
    gain = _item("ltcg", 40_000_00)
    state, federal = _state_tables([_item("ordinary", 80_000_00), SS, gain], CA_SS)
    # Federal AGI is 120,000 plus the taxable Social Security; the state's,
    # with half the gain left out, is under 100,000 before it. The phase-out
    # reads federal AGI, as the states' rules say.
    excluded = common.state_ss_exclusion(
        CA_SS[("state_social_security", "CA")][0], federal["ss_taxable"], federal.agi)
    assert federal.agi > 120_000_00
    assert state["ss_taxable"] == federal["ss_taxable"] - excluded
    assert state["ss_difference"] == excluded
    assert state["ltcg_exclusion"] == 20_000_00


def test_a_state_reclassifies_qualified_dividends_without_changing_agi() -> None:
    dividends = _item("qualified_dividend", 5_000_00)
    state, federal = _state_tables([SALARY_100K, dividends], TABLES)
    assert state["dividends_as_ordinary"] == 5_000_00
    assert (federal["ltcg_taxed"], state["ltcg_taxed"]) == (5_000_00, 0)
    assert state.agi == federal.agi


def test_the_federal_memo_records_what_agi_leaves_out() -> None:
    from analysis.sim.tax_api import Account
    roth = Account("Asset_Roth_Alice", "asset", "retirement", "alice")
    home = Account("Asset_House", "asset", "home", "joint")
    federal = calculator("federal").prepare(_year([
        SALARY_100K, SS,
        _item("tax_free", 7_000_00, payer=roth),
        _item("excluded_gain", 250_000_00, payer=home),
    ], tables=SS_TABLES)).compute()
    memo = federal.memo
    assert memo["roth_qualified"] == 7_000_00
    assert memo["excluded_gain"] == 250_000_00
    assert memo["ss_untaxed"] == 40_000_00 - federal["ss_taxable"]
