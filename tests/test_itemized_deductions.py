"""Itemized deductions from the year's spending (#542): state and local
taxes under the SALT cap and its phase-down, mortgage interest under the
debt limit, charity and medical expenses past their AGI floors, and the
hand-typed total on top; the federal limits, a state's own, and the
loader's checks on them."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from analysis.sim.loader import LoadError
from analysis.sim.tax_api import (
    Account, Extra, IncomeItem, Mortgage, Residence, Return, SpendingItem, TaxPaid, TaxYear, calculator,
)

YEAR = 2030
MICROS = 1_000_000
SALARY = Account("Income_Salary", "income", "salary", "alice")
CHECKING = Account("Asset_Checking", "asset", "cash", None)
HSA = Account("Asset_HSA", "asset", "hsa", "alice")
PROPERTY_TAX = Account("Expense_Property_Tax", "expense", "", None)
INTEREST = Account("Expense_Interest_Mortgage", "expense", "", None)
GIFTS = Account("Expense_Gifts", "expense", "", None, tags=frozenset({"tax:charity"}))
DOCTOR = Account("Expense_Doctor", "expense", "", None, tags=frozenset({"tax:medical"}))

LIMITS = {
    "filing_status": "MFJ", "salt_cap": 40_000_00, "salt_floor": 10_000_00,
    "salt_phaseout_start": 500_000_00, "salt_phaseout_rate_micros": 300_000,
    "mortgage_limit": 750_000_00, "charity_floor_micros": 5_000,
    "charity_limit_micros": 600_000, "medical_floor_micros": 75_000,
}
TABLES: dict[tuple[str, str | None], list[dict[str, Any]]] = {
    ("standard_deduction", None): [{"filing_status": "MFJ", "amount": 30_000_00}],
    ("federal_brackets", None): [{"filing_status": "MFJ", "floor": 0, "marginal_rate_micros": MICROS // 5}],
    ("state_brackets", "CA"): [{"filing_status": "MFJ", "floor": 0, "marginal_rate_micros": MICROS // 20}],
    ("itemized_limits", None): [LIMITS],
}


def _paid(payee: Account, amount: int, *, tags: str = "", payer: Account = CHECKING,
          character: str = "none") -> SpendingItem:
    return SpendingItem(YEAR, character, payer, payee, amount,
                        frozenset(tags.split()) if tags else frozenset())


def _year(
    salary: int, spending: Sequence[SpendingItem] = (), *, jurisdiction: str = "federal",
    mortgages: Sequence[Mortgage] = (), taxes_paid: Sequence[TaxPaid] = (),
    tables: Mapping[tuple[str, str | None], list[dict[str, Any]]] = TABLES,
) -> TaxYear:
    """A year of ``salary`` with the ``spending``: every table in force from
    2020, and the household in California."""
    kind = {"federal": "federal", "state": "state_tables"}[jurisdiction]
    return TaxYear(
        year=YEAR, jurisdiction=jurisdiction, params=type(calculator(kind)).Params(),
        items=[IncomeItem(YEAR, "ordinary", SALARY, CHECKING, salary)], losses=[], persons=[],
        spending=list(spending), mortgages=list(mortgages), taxes_paid=list(taxes_paid),
        _table=lambda kind, series, _year: list(tables.get((kind, series), [])),
        _indexed=lambda kind, series: list(tables.get((kind, series), [])),
        _dated=lambda kind, series=None: [(f"{YEAR}-01-31", r) for r in tables.get((kind, series), [])],
        _keyed=lambda *_a: None,
        _filing_status_in=lambda _year: "MFJ",
        _prior=lambda _line: 0,
        _series_of=lambda kind: sorted(s for k, s in tables if k == kind and s is not None),
        _residences=[("2020-01-31", Residence("CA"))],
        _upstream=lambda name: calculator(name).prepare(_year(
            salary, spending, jurisdiction=name, mortgages=mortgages, taxes_paid=taxes_paid,
            tables=tables)),
    )


def _federal(salary: int, spending: Sequence[SpendingItem] = (), **kw: Any) -> Return:
    return calculator("federal").prepare(_year(salary, spending, **kw)).compute()


def test_without_the_limits_table_only_the_hand_typed_total_counts() -> None:
    tables = {k: v for k, v in TABLES.items() if k != ("itemized_limits", None)}
    tables[("federal_deductions", None)] = [{"amount": 35_000_00}]
    ret = _federal(200_000_00, [_paid(PROPERTY_TAX, 20_000_00, tags="tax:property")], tables=tables)
    assert ret["itemized_deduction"] == ret["deduction"] == 35_000_00
    assert "itemized_salt" not in ret.lines


def test_property_tax_and_state_income_tax_share_the_salt_cap() -> None:
    spending = [_paid(PROPERTY_TAX, 12_000_00, tags="tax:property"),
                # The property_tax character counts without the tag.
                _paid(PROPERTY_TAX, 3_000_00, character="property_tax")]
    ret = _federal(200_000_00, spending, taxes_paid=[
        TaxPaid(YEAR, "state", 20_000_00), TaxPaid(YEAR - 1, "state", 9_000_00)])
    # 35,000 paid this calendar year, under the 40,000 cap.
    assert ret["itemized_salt"] == 35_000_00
    ret = _federal(200_000_00, spending, taxes_paid=[TaxPaid(YEAR, "state", 30_000_00)])
    assert ret["itemized_salt"] == 40_000_00
    assert ret["itemized_deduction"] == ret["deduction"] == 40_000_00
    assert ret.taxable_income == 160_000_00


def test_the_salt_cap_phases_down_with_agi_to_its_floor() -> None:
    spending = [_paid(PROPERTY_TAX, 50_000_00, tags="tax:property")]
    # 30% of the 50,000 over 500,000 comes off the cap.
    assert _federal(550_000_00, spending)["itemized_salt"] == 25_000_00
    assert _federal(700_000_00, spending)["itemized_salt"] == 10_000_00
    # A what-if's income moves it: the limits are figured per compute.
    prepared = calculator("federal").prepare(_year(500_000_00, spending))
    assert prepared.compute()["itemized_salt"] == 40_000_00
    assert prepared.compute(Extra(ordinary=50_000_00))["itemized_salt"] == 25_000_00


def test_a_state_deducts_no_income_tax_and_takes_its_own_limits() -> None:
    spending = [_paid(PROPERTY_TAX, 50_000_00, tags="tax:property")]
    paid = [TaxPaid(YEAR, "state", 10_000_00)]
    # Without a table of its own the state takes the federal limits.
    year = _year(200_000_00, spending, jurisdiction="state", taxes_paid=paid)
    ret = calculator("state_tables").prepare(year).compute()
    assert ret["itemized_salt"] == 40_000_00
    tables = {**TABLES, ("state_itemized_limits", "CA"): [
        {"filing_status": "MFJ", "salt_cap": None, "mortgage_limit": 1_000_000_00,
         "charity_limit_micros": 500_000, "medical_floor_micros": 75_000}]}
    year = _year(200_000_00, spending, jurisdiction="state", taxes_paid=paid, tables=tables)
    ret = calculator("state_tables").prepare(year).compute()
    # No cap, and no state income tax.
    assert ret["itemized_salt"] == 50_000_00
    assert ret["deduction"] == 50_000_00


def test_mortgage_interest_is_limited_by_the_average_debt() -> None:
    spending = [_paid(INTEREST, 60_000_00, tags="tax:mortgage_interest")]
    home = Mortgage(YEAR, "Liability_Mortgage", 60_000_00, 1_000_000_00,
                    frozenset({"tax:mortgage_interest"}))
    ret = _federal(300_000_00, spending, mortgages=[home])
    # 750,000 of a 1,000,000 average balance.
    assert ret["itemized_mortgage_interest"] == 45_000_00
    # Under the limit, all of it.
    small = Mortgage(YEAR, "Liability_Mortgage", 60_000_00, 700_000_00,
                     frozenset({"tax:mortgage_interest"}))
    assert _federal(300_000_00, spending, mortgages=[small])["itemized_mortgage_interest"] == 60_000_00
    # A rental's mortgage carries another tag and counts neither way.
    rental = Mortgage(YEAR, "Liability_Rental", 30_000_00, 500_000_00, frozenset({"tax:rental"}))
    ret = _federal(300_000_00, spending, mortgages=[small, rental])
    assert ret["itemized_mortgage_interest"] == 60_000_00


def test_charity_and_medical_count_past_their_floors() -> None:
    spending = [_paid(GIFTS, 10_000_00), _paid(DOCTOR, 20_000_00),
                # An HSA paid this, untaxed already.
                _paid(DOCTOR, 5_000_00, payer=HSA)]
    ret = _federal(200_000_00, spending)
    # 0.5% of 200,000 is 1,000; 7.5% is 15,000.
    assert ret["itemized_charity"] == 9_000_00
    assert ret["itemized_medical"] == 5_000_00
    # Charity is capped at 60% of AGI.
    assert _federal(10_000_00, [_paid(GIFTS, 10_000_00)])["itemized_charity"] == 6_000_00


def test_the_hand_typed_total_adds_to_the_lines() -> None:
    tables = {**TABLES, ("federal_deductions", None): [{"amount": 5_000_00}]}
    ret = _federal(200_000_00, [_paid(PROPERTY_TAX, 30_000_00, tags="tax:property")], tables=tables)
    assert ret["itemized_other"] == 5_000_00
    assert ret["itemized_deduction"] == 35_000_00 == ret["deduction"]
    # Below the standard deduction the lines still show, and the standard is taken.
    ret = _federal(200_000_00, [_paid(PROPERTY_TAX, 10_000_00, tags="tax:property")])
    assert (ret["itemized_salt"], ret["deduction"]) == (10_000_00, 30_000_00)


def _lines(run: Path, *buckets: str) -> dict[str, dict[int, int]]:
    with closing(sqlite3.connect(run)) as con:
        return {bucket: dict(con.execute(
            "SELECT year, amount FROM tax_year_summary WHERE bucket = ? ORDER BY year", (bucket,)))
            for bucket in buckets}


@pytest.mark.slow
def test_a_run_itemizes_mortgage_interest_property_tax_and_state_tax(
    add_state_tax: Callable[..., None], retire_alice_csv: Callable[[str], Path],
    run_test_copy: Callable[[], Path],
) -> None:
    add_state_tax()
    path = retire_alice_csv("scheduled.csv")
    rows = pd.read_csv(path, dtype=str, keep_default_na=False)
    rows.loc[rows["name"] == "housing", "tags"] = "tax:property"
    rows.to_csv(path, index=False)
    run = run_test_copy()
    lines = _lines(run, "federal_itemized_salt", "federal_itemized_mortgage_interest",
                   "state_itemized_salt", "state_itemized_mortgage_interest")
    with closing(sqlite3.connect(run)) as con:
        interest = dict(con.execute(
            "SELECT CAST(substr(t.date, 1, 4) AS INTEGER), SUM(t.amount) FROM transfer_event t "
            "JOIN account a ON a.id = t.to_account WHERE a.name = 'Expense_Interest_Mortgage' "
            "GROUP BY 1"))
        housing = dict(con.execute(
            "SELECT CAST(substr(t.date, 1, 4) AS INTEGER), SUM(t.amount) FROM transfer_event t "
            "JOIN account a ON a.id = t.to_account WHERE a.name = 'Expense_Housing' GROUP BY 1"))
        state_paid = dict(con.execute(
            "SELECT CAST(substr(t.date, 1, 4) AS INTEGER), SUM(t.amount) FROM transfer_event t "
            "WHERE t.reason_kind = 'tax' AND t.to_account IN ("
            "  SELECT c.account_id FROM tax_jurisdiction j JOIN chain c ON c.id = j.destination_chain_id "
            "  WHERE j.name = 'state') GROUP BY 1"))
    year = 2028
    # The mortgage is well under $750,000: all its interest counts.
    assert lines["federal_itemized_mortgage_interest"][year] == interest[year] > 0
    assert lines["state_itemized_mortgage_interest"][year] == interest[year]
    # Federal SALT is the property tax and the state tax paid that year (cash basis).
    assert lines["federal_itemized_salt"][year] == housing[year] + state_paid.get(year, 0)
    assert state_paid.get(year, 0) > 0
    # California has no SALT cap and deducts no state income tax.
    assert lines["state_itemized_salt"][year] == housing[year]


@pytest.mark.parametrize(("row", "message"), [
    ("2026-01-31,MFJ,-1.00,,,,,,,", "salt_cap must not be negative"),
    ("2026-01-31,MFJ,,,,,,,120%,", "charity_limit must be 0% to 100%"),
    ("2026-01-31,MFJ,40000.00,,500000.00,,,,,", "needs both salt_phaseout_start and salt_phaseout_rate"),
])
def test_the_loader_checks_an_itemized_limits_row(
    write_aux: Callable[[str, str], Path], load_test_copy: Callable[[], object], row: str, message: str,
) -> None:
    header = ("effective_date,filing_status,salt_cap,salt_floor,salt_phaseout_start,salt_phaseout_rate,"
              "mortgage_limit,charity_floor,charity_limit,medical_floor\n")
    write_aux("itemized_limits.csv", header + row + "\n")
    with pytest.raises(LoadError, match=message):
        load_test_copy()


def test_the_federal_limits_are_not_keyed_by_state(
    write_aux: Callable[[str, str], Path], load_test_copy: Callable[[], object],
) -> None:
    write_aux("itemized_limits__CA.csv", "effective_date,filing_status,salt_cap\n2026-01-31,MFJ,1.00\n")
    with pytest.raises(LoadError, match="state_itemized_limits__CA.csv"):
        load_test_copy()
