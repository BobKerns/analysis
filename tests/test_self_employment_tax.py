"""Self-employment tax on a business's net profit (#623): Schedule SE for
each business owner and Form 8959 Part II, worked out with the federal
return from a tax year built by hand.

Net earnings are 92.35% of a person's net profit (none under $400); 12.4%
of them up to the wage base less the person's own wages, 2.9% of all of
them, and 0.9% of the household's over the threshold less its wages. Half of
the first two comes off income."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from analysis.sim.tax_api import (
    Account, Extra, IncomeItem, Person, Residence, Return, TaxError, TaxYear, calculator,
)
from analysis.sim.taxes import common

YEAR = 2030
MICROS = 1_000_000
ALICE = [Person("alice", "1960-01-31", "primary")]
COUPLE = [*ALICE, Person("bill", "1961-01-31", "primary")]

CHECKING = Account("Asset_Checking", "asset", "cash", None)
SALES = Account("Income_Sales", "income", "other_income", None)
COSTS = Account("Expense_Business", "expense", "business", None)


def _business(owner: str | None, *, hobby: bool = False) -> Account:
    return Account(f"Asset_Business_{owner}", "asset", "business", owner, hobby=hobby)


def _salary(owner: str | None) -> Account:
    return Account(f"Income_Salary_{owner}", "income", "salary", owner)


def _sales(owner: str | None, amount: int, *, hobby: bool = False) -> IncomeItem:
    return IncomeItem(YEAR, "business_income", SALES, _business(owner, hobby=hobby), amount)


def _costs(owner: str | None, amount: int) -> IncomeItem:
    return IncomeItem(YEAR, "business_expense", _business(owner), COSTS, amount)


def _wages(owner: str | None, amount: int) -> IncomeItem:
    return IncomeItem(YEAR, "ordinary", _salary(owner), CHECKING, amount)


def _se_row(status: str, threshold: int) -> dict[str, Any]:
    return {
        "filing_status": status, "wage_base": 160_000_00, "ss_rate_micros": 124_000,
        "medicare_rate_micros": 29_000, "earnings_factor_micros": 923_500, "minimum": 400_00,
        "additional_medicare_rate_micros": 9_000, "additional_medicare_threshold": threshold,
    }


TABLES: dict[tuple[str, str | None], list[dict[str, Any]]] = {
    ("standard_deduction", None): [{"filing_status": "MFJ", "amount": 30_000_00}],
    ("federal_brackets", None): [
        {"filing_status": "MFJ", "floor": 0, "marginal_rate_micros": MICROS // 10},
        {"filing_status": "MFJ", "floor": 20_000_00, "marginal_rate_micros": MICROS // 5},
    ],
    ("state_brackets", "CA"): [
        {"filing_status": "MFJ", "floor": 0, "marginal_rate_micros": MICROS // 20},
    ],
    ("self_employment_tax", None): [_se_row("MFJ", 250_000_00), _se_row("Single", 200_000_00)],
}


def _year(
    items: Sequence[IncomeItem], *, persons: Sequence[Person] = COUPLE,
    jurisdiction: str = "federal",
    tables: Mapping[tuple[str, str | None], list[dict[str, Any]]] = TABLES,
    prior: Mapping[str, int] | None = None,
) -> TaxYear:
    kind = {"federal": "federal", "state": "state_tables"}[jurisdiction]
    return TaxYear(
        year=YEAR, jurisdiction=jurisdiction, params=type(calculator(kind)).Params(),
        items=list(items), losses=[], persons=list(persons),
        _table=lambda kind, series, _year: list(tables.get((kind, series), [])),
        _indexed=lambda kind, series: list(tables.get((kind, series), [])),
        _dated=lambda kind, series: [("2020-01-01", r) for r in tables.get((kind, series), [])],
        _keyed=lambda *_: None,
        _filing_status_in=lambda _year: "MFJ",
        _prior=lambda line: (prior or {}).get(line, 0),
        _series_of=lambda kind: sorted(s for k, s in tables if k == kind and s is not None),
        _residences=[("2020-01-31", Residence("CA"))],
        _upstream=lambda name: calculator(name).prepare(
            _year(items, persons=persons, jurisdiction=name, tables=tables, prior=prior)),
    )


def _federal(items: Sequence[IncomeItem], **kw: Any) -> Return:
    return calculator("federal").prepare(_year(items, **kw)).compute()


def _se(earnings: int, ss_base: int | None = None) -> int:
    """Social Security and Medicare on ``earnings``, the first on no more
    than ``ss_base`` of them."""
    ss = min(earnings, earnings if ss_base is None else ss_base)
    return ss * 124_000 // MICROS + earnings * 29_000 // MICROS


def test_a_profit_pays_social_security_and_medicare_on_net_earnings() -> None:
    ret = _federal([_sales("alice", 100_000_00)])
    earned = 100_000_00 * 923_500 // MICROS
    se_tax = _se(earned)
    assert (ret["se_earnings"], ret["se_tax"]) == (earned, se_tax)
    assert ret["se_tax_deduction"] == se_tax // 2
    # Half of it comes off income, so AGI is the profit less it.
    assert ret.agi == ret["ordinary_income"] == 100_000_00 - se_tax // 2
    income_tax = 20_000_00 // 10 + (ret.taxable_income - 20_000_00) // 5
    assert ret.tax == income_tax + se_tax
    assert "additional_medicare_tax" not in ret.lines
    assert ret.memo["se_tax_deduction"] == se_tax // 2


def test_net_earnings_under_the_minimum_pay_nothing() -> None:
    ret = _federal([_sales("alice", 430_00)])  # 92.35% of it is under $400
    assert "se_tax" not in ret.lines
    assert ret.agi == 430_00


def test_a_loss_or_a_hobby_pays_nothing_and_needs_no_table() -> None:
    no_table = {k: v for k, v in TABLES.items() if k[0] != "self_employment_tax"}
    for items in ([_sales("alice", 1_000_00), _costs("alice", 5_000_00)],
                  [_sales("alice", 50_000_00, hobby=True)]):
        ret = _federal(items, tables=no_table)
        assert "se_tax" not in ret.lines


def test_a_profit_without_the_table_is_an_error() -> None:
    no_table = {k: v for k, v in TABLES.items() if k[0] != "self_employment_tax"}
    with pytest.raises(TaxError, match="needs aux/self_employment_tax.csv"):
        _federal([_sales("alice", 50_000_00)], tables=no_table)


def test_only_the_owners_own_wages_use_up_their_wage_base() -> None:
    earned = 100_000_00 * 923_500 // MICROS
    own = _federal([_sales("alice", 100_000_00), _wages("alice", 150_000_00)])
    assert own["se_tax"] == _se(earned, ss_base=10_000_00)
    spouses = _federal([_sales("alice", 100_000_00), _wages("bill", 150_000_00)])
    assert spouses["se_tax"] == _se(earned)
    capped = _federal([_sales("alice", 100_000_00), _wages("alice", 200_000_00)])
    assert capped["se_tax"] == _se(earned, ss_base=0)  # Medicare only


def test_one_spouses_loss_does_not_offset_the_others_profit() -> None:
    ret = _federal([_sales("alice", 100_000_00), _costs("bill", 40_000_00)])
    assert ret["business_net"] == 60_000_00  # income tax nets them
    assert ret["se_earnings"] == 100_000_00 * 923_500 // MICROS


def test_one_persons_businesses_net_together() -> None:
    other = Account("Asset_Business_alice_2", "asset", "business", "alice")
    ret = _federal([_sales("alice", 100_000_00),
                    IncomeItem(YEAR, "business_expense", other, COSTS, 40_000_00)])
    assert ret["se_earnings"] == 60_000_00 * 923_500 // MICROS


def test_a_joint_business_splits_between_the_spouses() -> None:
    joint = _federal([_sales("joint", 100_000_00)])
    half = 50_000_00 * 923_500 // MICROS
    assert joint["se_earnings"] == 2 * half
    # Each spouse's half has their own wage base: Alice's wages use up hers.
    capped = _federal([_sales("joint", 100_000_00), _wages("alice", 200_000_00)])
    assert capped["se_tax"] == _se(half, ss_base=0) + _se(half)
    # A household of one has no one to split it with.
    alone = _federal([_sales("joint", 100_000_00)], persons=ALICE)
    assert alone["se_earnings"] == 100_000_00 * 923_500 // MICROS


def test_additional_medicare_tax_is_on_earnings_over_the_threshold_less_wages() -> None:
    earned = 100_000_00 * 923_500 // MICROS
    under = _federal([_sales("alice", 100_000_00), _wages("bill", 100_000_00)])
    assert "additional_medicare_tax" not in under.lines
    over = _federal([_sales("alice", 100_000_00), _wages("bill", 200_000_00)])
    extra = (earned - 50_000_00) * 9_000 // MICROS
    assert over["additional_medicare_tax"] == extra
    assert over["se_tax_deduction"] == over["se_tax"] // 2  # none of the 0.9% in it
    income_tax = 20_000_00 // 10 + (over.taxable_income - 20_000_00) // 5
    assert over.tax == income_tax + over["se_tax"] + extra


def test_a_carried_in_loss_does_not_reduce_self_employment_earnings() -> None:
    ret = _federal([_sales("alice", 100_000_00)], prior={"nol_carryover": 50_000_00})
    assert ret["nol_used"] > 0
    assert ret["se_earnings"] == 100_000_00 * 923_500 // MICROS


def test_a_what_if_leaves_the_self_employment_tax_alone() -> None:
    prepared = calculator("federal").prepare(_year([_sales("alice", 100_000_00)]))
    base, more = prepared.compute(), prepared.compute(Extra(ordinary=10_000_00))
    assert more["se_tax"] == base["se_tax"]
    assert more.tax - base.tax == 10_000_00 // 5  # only the income tax moves


def test_the_state_starts_from_the_lower_agi() -> None:
    items = [_sales("alice", 100_000_00)]
    federal = _federal(items)
    state = calculator("state_tables").prepare(_year(items, jurisdiction="state")).compute()
    assert state["ordinary_income"] == federal["ordinary_income"] == 100_000_00 - federal[
        "se_tax_deduction"]
    assert "se_tax" not in state.lines


def test_wages_are_ordinary_income_out_of_a_salary_account() -> None:
    pension = Account("Income_Pension", "income", "pension", "alice")
    year = _year([_wages("alice", 10_00), _wages("joint", 30_01),
                  IncomeItem(YEAR, "ordinary", pension, CHECKING, 99_00),
                  IncomeItem(YEAR, "none", _salary("bill"), CHECKING, 99_00)])
    assert common.wages(year) == {"alice": 10_00 + 15_01, "bill": 15_00}
