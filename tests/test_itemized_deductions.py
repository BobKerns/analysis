"""Itemized deductions from the year's spending (#542): state and local
taxes under the SALT cap and its phase-down, mortgage interest under the
debt limit, charity and medical expenses past their AGI floors, and the
hand-typed total on top; the federal limits, a state's own, and the
loader's checks on them."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from analysis.sim.loader import LoadError
from analysis.sim.taxes.state_tables import StateTablesYear
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


CA_LIMITS = {("state_itemized_limits", "CA"): [
    {"filing_status": "MFJ", "salt_cap": None, "mortgage_limit": 1_000_000_00,
     "charity_limit_micros": 500_000, "medical_floor_micros": 75_000}]}


def test_a_state_without_limits_of_its_own_figures_no_lines() -> None:
    """Its law may not match the federal limits (North Carolina caps
    mortgage interest and property tax together), so only the hand-typed
    total counts, unless its params name the federal table."""
    spending = [_paid(PROPERTY_TAX, 50_000_00, tags="tax:property")]
    year = _year(200_000_00, spending, jurisdiction="state")
    ret = calculator("state_tables").prepare(year).compute()
    assert "itemized_salt" not in ret.lines
    assert ret["itemized_deduction"] == 0
    federal = replace(year, params=replace(year.params, itemized_limits_aux="itemized_limits"))
    assert calculator("state_tables").prepare(federal).compute()["itemized_salt"] == 40_000_00


def test_a_state_deducts_no_income_tax_and_takes_its_own_limits() -> None:
    spending = [_paid(PROPERTY_TAX, 50_000_00, tags="tax:property")]
    paid = [TaxPaid(YEAR, "state", 10_000_00)]
    tables = {**TABLES, **CA_LIMITS}
    year = _year(200_000_00, spending, jurisdiction="state", taxes_paid=paid, tables=tables)
    ret = calculator("state_tables").prepare(year).compute()
    # No cap, and no state income tax.
    assert ret["itemized_salt"] == 50_000_00
    assert ret["deduction"] == 50_000_00


HOME = frozenset({"tax:mortgage_interest"})


def _months(name: str, months: range, interest: int, balance: int,
            tags: frozenset[str] = HOME) -> list[Mortgage]:
    return [Mortgage(YEAR, month, name, interest, balance, tags) for month in months]


def test_mortgage_interest_is_limited_by_each_months_debt() -> None:
    spending = [_paid(INTEREST, 60_000_00, tags="tax:mortgage_interest")]
    home = _months("Liability_Mortgage", range(1, 13), 5_000_00, 1_000_000_00)
    # 750,000 of a 1,000,000 balance.
    assert _federal(300_000_00, spending, mortgages=home)["itemized_mortgage_interest"] == 45_000_00
    # Under the limit, all of it.
    small = _months("Liability_Mortgage", range(1, 13), 5_000_00, 700_000_00)
    assert _federal(300_000_00, spending, mortgages=small)["itemized_mortgage_interest"] == 60_000_00
    # A rental's mortgage carries another tag and counts neither way.
    rental = _months("Liability_Rental", range(1, 13), 2_500_00, 500_000_00, frozenset({"tax:rental"}))
    ret = _federal(300_000_00, spending, mortgages=small + rental)
    assert ret["itemized_mortgage_interest"] == 60_000_00


def test_mortgages_held_one_after_another_dont_add_up() -> None:
    """A home sold in June and another bought: $600,000 owed at any time,
    under the limit, though the two debts' balances sum past it."""
    spending = [_paid(INTEREST, 60_000_00, tags="tax:mortgage_interest")]
    mortgages = (_months("Liability_Old", range(1, 7), 5_000_00, 600_000_00)
                 + _months("Liability_New", range(7, 13), 5_000_00, 600_000_00))
    assert _federal(300_000_00, spending, mortgages=mortgages)["itemized_mortgage_interest"] == 60_000_00
    # Held together, they are over it in every month they overlap.
    both = mortgages + _months("Liability_Old", range(7, 8), 5_000_00, 600_000_00)
    spending = [_paid(INTEREST, 65_000_00, tags="tax:mortgage_interest")]
    # July: 10,000 of interest on 1,200,000, of which 750/1200 counts.
    assert _federal(300_000_00, spending, mortgages=both)["itemized_mortgage_interest"] == 65_000_00 - 3_750_00


def test_a_rentals_property_tax_stays_out_of_salt() -> None:
    spending = [_paid(PROPERTY_TAX, 8_000_00, character="property_tax"),
                _paid(PROPERTY_TAX, 5_000_00, character="property_tax", tags="tax:rental")]
    assert _federal(200_000_00, spending)["itemized_salt"] == 8_000_00


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


def test_a_state_that_taxes_an_hsa_counts_the_medical_it_paid() -> None:
    spending = [_paid(DOCTOR, 20_000_00), _paid(DOCTOR, 10_000_00, payer=HSA)]
    taxed = {**TABLES, **CA_LIMITS, ("state_hsa", "CA"): [{"treatment": "taxable"}]}
    federal = _federal(100_000_00, spending, tables=taxed)
    state = calculator("state_tables").prepare(
        _year(100_000_00, spending, jurisdiction="state", tables=taxed)).compute()
    # 7.5% of 100,000 is 7,500.
    assert federal["itemized_medical"] == 12_500_00
    assert state["itemized_medical"] == 22_500_00


def test_without_the_federal_table_the_state_figures_no_lines_either() -> None:
    """The documented off switch, a header-only itemized_limits.csv, turns
    off a state that has a table of its own too."""
    tables = {k: v for k, v in TABLES.items() if k != ("itemized_limits", None)}
    tables[("state_itemized_limits", "CA")] = [{"filing_status": "MFJ", "salt_cap": None}]
    tables[("federal_deductions", None)] = [{"amount": 35_000_00}]
    spending = [_paid(PROPERTY_TAX, 20_000_00, tags="tax:property")]
    state = calculator("state_tables").prepare(
        _year(200_000_00, spending, jurisdiction="state", tables=tables)).compute()
    assert state["itemized_deduction"] == state["deduction"] == 35_000_00
    assert "itemized_salt" not in state.lines


class _OwnDeductions(StateTablesYear):
    """A household's state that figures its own deduction, Schedule A
    included (a state with an exemption and no standard deduction)."""

    def deductions(self) -> Mapping[str, int]:
        own = self.ty.spent("tax:property")
        return {"itemized_deduction": own, "deduction": max(12_000_00, own)}

    def itemized_lines(self) -> None:
        return None


class _MoreStandard(StateTablesYear):
    """A household's state that only adds to the standard deduction."""

    def deductions(self) -> Mapping[str, int]:
        base = super().deductions()
        standard = base["standard_deduction"] + 1_000_00
        return {**base, "standard_deduction": standard, "deduction": max(standard, base["deduction"])}


def test_a_subclass_figuring_its_own_schedule_a_turns_the_lines_off() -> None:
    """No lines are added to it, so the property tax it already counted
    doesn't count twice, and it needn't return a standard deduction."""
    calc = calculator("state_tables")
    year = _OwnDeductions(calc, _year(
        200_000_00, [_paid(PROPERTY_TAX, 20_000_00, tags="tax:property")],
        jurisdiction="state", tables={**TABLES, **CA_LIMITS}))
    ret = year.compute()
    assert "itemized_salt" not in ret.lines
    assert ret["deduction"] == 20_000_00


def test_a_subclass_extending_deductions_keeps_the_lines() -> None:
    calc = calculator("state_tables")
    year = _MoreStandard(calc, _year(
        200_000_00, [_paid(PROPERTY_TAX, 50_000_00, tags="tax:property")],
        jurisdiction="state", tables={**TABLES, **CA_LIMITS}))
    ret = year.compute()
    assert ret["itemized_salt"] == 50_000_00
    assert ret["standard_deduction"] == 31_000_00
    assert ret["deduction"] == 50_000_00


def test_a_year_before_the_limits_take_effect_itemizes_the_hand_typed_total() -> None:
    tables = {**TABLES, ("federal_deductions", None): [{"amount": 35_000_00}]}

    year = _year(200_000_00, [_paid(PROPERTY_TAX, 20_000_00, tags="tax:property")], tables=tables)
    # No row in force yet: the table's first date is after the year.
    early = replace(year, _indexed=lambda kind, series: (
        [] if kind == "itemized_limits" else list(tables.get((kind, series), []))))
    ret = calculator("federal").prepare(early).compute()
    assert ret["itemized_deduction"] == 35_000_00
    assert "itemized_salt" not in ret.lines


def test_the_loader_wants_every_filing_status_at_each_date(
    write_aux: Callable[[str, str], Path], load_test_copy: Callable[[], object],
) -> None:
    header = ("effective_date,filing_status,salt_cap,salt_floor,salt_phaseout_start,salt_phaseout_rate,"
              "mortgage_limit,charity_floor,charity_limit,medical_floor\n")
    write_aux("itemized_limits.csv", header + "2026-01-31,MFJ,40000.00,,,,,,,\n")
    with pytest.raises(LoadError, match=r"itemized_limits.csv \(2026-01\): no rows for filing status MFS, Single"):
        load_test_copy()


@pytest.mark.slow
def test_a_business_mortgage_is_not_itemized(
    add_state_tax: Callable[..., None], retire_alice_csv: Callable[[str], Path],
    run_test_copy: Callable[[], Path],
) -> None:
    """Schedule C deducts a business's mortgage interest, so it isn't
    itemized too."""
    add_state_tax()
    accounts = pd.read_csv(retire_alice_csv("accounts.csv"), dtype=str, keep_default_na=False)
    accounts.loc[len(accounts)] = {
        **dict.fromkeys(accounts.columns, ""), "name": "Asset_Business_Alice", "kind": "asset",
        "subkind": "business", "owner": "alice", "tax_treatment": "taxable",
        "opened": "2020-01-31", "meta_json": "{}"}
    accounts.to_csv(retire_alice_csv("accounts.csv"), index=False)
    lots = pd.read_csv(retire_alice_csv("account_lots.csv"), dtype=str, keep_default_na=False)
    lots.loc[len(lots)] = {
        **dict.fromkeys(lots.columns, ""), "account_name": "Asset_Business_Alice",
        "opening_balance": "0.00", "minimum_balance": "500.00", "refill_chain": "Asset_Checking"}
    lots.to_csv(retire_alice_csv("account_lots.csv"), index=False)
    path = retire_alice_csv("scheduled.csv")
    rows = pd.read_csv(path, dtype=str, keep_default_na=False)
    mortgage = rows["amount_rule_kind"] == "mortgage_payment"
    assert mortgage.any()
    rows.loc[mortgage, "from_account"] = "Asset_Business_Alice"
    rows.loc[mortgage, "tax_character"] = "business_expense"
    rows.to_csv(path, index=False)
    lines = _lines(run_test_copy(), "federal_itemized_mortgage_interest", "federal_business_net")
    assert lines["federal_itemized_mortgage_interest"]
    assert set(lines["federal_itemized_mortgage_interest"].values()) == {0}
    assert lines["federal_business_net"][2028] < 0

def _route(retire_alice_csv: Callable[[str], Path], variant: str) -> None:
    """Tag the housing rule ``tax:property``; ``shared`` pays the state tax
    into the federal tax's account, ``routed`` pays the housing rule into
    the state tax's, and ``report_tag`` tags the state tax's account and
    the mortgage interest's ``tax:itemized``, a tag for a report."""
    path = retire_alice_csv("scheduled.csv")
    rows = pd.read_csv(path, dtype=str, keep_default_na=False)
    housing = rows["name"] == "housing"
    rows.loc[housing, "tags"] = "tax:property"
    if variant == "routed":
        rows.loc[housing, "to_account"] = "Expense_Taxes_State"
    rows.to_csv(path, index=False)
    if variant == "report_tag":
        accounts = pd.read_csv(retire_alice_csv("accounts.csv"), dtype=str, keep_default_na=False)
        accounts.loc[accounts["name"].isin(["Expense_Taxes_State", "Expense_Interest_Mortgage"]),
                     "tags"] = "tax:itemized"
        accounts.to_csv(retire_alice_csv("accounts.csv"), index=False)
    if variant == "shared":
        state = retire_alice_csv("") / "taxes" / "state.csv"
        rows = pd.read_csv(state, dtype=str, keep_default_na=False)
        rows["destination"] = "Expense_Taxes_Federal"
        rows.to_csv(state, index=False)


@pytest.mark.slow
@pytest.mark.parametrize("variant", ["shared", "routed", "report_tag"])
def test_state_tax_paid_counts_once_wherever_it_is_paid(
    add_state_tax: Callable[..., None], retire_alice_csv: Callable[[str], Path],
    run_test_copy: Callable[[], Path], variant: str,
) -> None:
    """Its settlements count though the federal tax is paid into the same
    account or the account has a tax: tag for a report, and property tax
    paid into the state's account counts once."""
    add_state_tax()
    _route(retire_alice_csv, variant)
    run = run_test_copy()
    lines = _lines(run, "federal_itemized_salt", "federal_itemized_mortgage_interest")
    salt = lines["federal_itemized_salt"]
    with closing(sqlite3.connect(run)) as con:
        housing = dict(con.execute(
            "SELECT CAST(substr(date, 1, 4) AS INTEGER), SUM(amount) FROM transfer_event "
            "WHERE reason_kind = 'rule' AND reason_id = "
            "  (SELECT id FROM scheduled_transaction WHERE name = 'housing') GROUP BY 1"))
        # A settlement out of the state's account is a refund (#568): when
        # property tax is routed there, the state counts it as paid and
        # gives the excess back.
        state_paid = {year: max(0, paid) for year, paid in con.execute(
            "SELECT CAST(substr(t.date, 1, 4) AS INTEGER), "
            "       SUM(CASE WHEN t.from_account = c.account_id THEN -t.amount ELSE t.amount END) "
            "FROM transfer_event t JOIN tax_jurisdiction j ON j.id = t.reason_id "
            "JOIN chain c ON c.id = j.destination_chain_id "
            "WHERE t.reason_kind = 'tax' AND j.name = 'state' GROUP BY 1")}
        interest = dict(con.execute(
            "SELECT CAST(substr(t.date, 1, 4) AS INTEGER), SUM(t.amount) FROM transfer_event t "
            "JOIN account a ON a.id = t.to_account WHERE a.name = 'Expense_Interest_Mortgage' "
            "GROUP BY 1"))
    if variant != "routed":
        assert state_paid[2028] > 0
    for year in (2028, 2029):
        assert lines["federal_itemized_mortgage_interest"][year] == interest[year] > 0
        assert salt[year] == housing[year] + state_paid.get(year, 0), year


def test_a_rule_paying_into_a_shared_tax_account_is_a_warning(
    add_state_tax: Callable[..., None], retire_alice_csv: Callable[[str], Path],
    load_test_copy: Callable[[], Any],
) -> None:
    add_state_tax()
    _route(retire_alice_csv, "shared")
    path = retire_alice_csv("scheduled.csv")
    rows = pd.read_csv(path, dtype=str, keep_default_na=False)
    rows.loc[rows["name"] == "housing", "to_account"] = "Expense_Taxes_Federal"
    rows.to_csv(path, index=False)
    warnings = load_test_copy().warnings
    assert any("housing pays into it as state tax" in w for w in warnings), warnings


LIMITS_HEADER = ("effective_date,filing_status,salt_cap,salt_floor,salt_phaseout_start,salt_phaseout_rate,"
                 "mortgage_limit,charity_floor,charity_limit,medical_floor\n")


@pytest.mark.parametrize(("cap", "message"), [("40400.00", None), ("-1.00", "salt_cap must not be negative")])
def test_a_swap_can_read_another_series_of_the_federal_limits(
    write_aux: Callable[[str, str], Path], append_calendar_row: Callable[..., None],
    load_test_copy: Callable[[], Any], cap: str, message: str | None,
) -> None:
    """What if the SALT cap is extended past 2029: a series that isn't a
    state's is a swap's source, and its rows are checked like the table's."""
    write_aux("itemized_limits__extended.csv", LIMITS_HEADER + "".join(
        f"2026-01-31,{status},{cap},10000.00,,,750000.00,,,\n" for status in ("MFJ", "Single", "MFS")))
    append_calendar_row(date="2030-01", kind="swap_aux_table", params_json=json.dumps(
        {"aux_kind": "itemized_limits", "source_series": "extended"}))
    if message is None:
        load_test_copy()
    else:
        with pytest.raises(LoadError, match=message):
            load_test_copy()


def test_a_hand_typed_total_beside_a_mortgage_is_a_warning(
    write_aux: Callable[[str, str], Path], load_test_copy: Callable[[], Any],
) -> None:
    """retire_alice's mortgage interest is itemized by itself; a Schedule A
    typed in before #542 would count it again."""
    write_aux("federal_deductions.csv", "effective_date,amount\n2027-01-31,30000.00\n")
    warnings = load_test_copy().warnings
    assert any("federal_deductions.csv (2027): the hand-typed total adds" in w for w in warnings), warnings


@pytest.mark.parametrize(("state_tax", "credit"), [
    # 20,000 of property tax and 15,000 of state tax, under the cap: the
    # deduction is 35,000, of which 15,000 is state tax.
    (15_000_00, (35_000_00 - 15_000_00) * 6 // 100),
    # 30,000 of state tax: 50,000 capped to 40,000, three fifths of it state tax.
    (30_000_00, (40_000_00 - 24_000_00) * 6 // 100),
])
def test_a_federal_deduction_base_leaves_out_the_state_tax_deducted(state_tax: int, credit: int) -> None:
    """Utah's taxpayer credit is figured on the federal deduction less the
    state income tax in it, or a state's own tax would grow its credit."""
    tables = {**TABLES, ("state_adjustments", "CA"): [
        {"name": "taxpayer_credit", "step": "credit", "base": "federal_deduction", "rate_micros": 60_000}]}
    spending = [_paid(PROPERTY_TAX, 20_000_00, tags="tax:property")]
    paid = [TaxPaid(YEAR, "state", state_tax)]
    federal = _federal(200_000_00, spending, taxes_paid=paid, tables=tables)
    assert federal.memo["income_tax_deducted"] == state_tax * federal["itemized_salt"] // (
        20_000_00 + state_tax)
    state = calculator("state_tables").prepare(
        _year(200_000_00, spending, jurisdiction="state", taxes_paid=paid, tables=tables)).compute()
    assert state["taxpayer_credit"] == credit


def test_business_and_rental_spending_is_never_itemized() -> None:
    """Schedule C deducts a business's (the business_expense character, or
    paid from a business account) and Schedule E a rental's (tax:rental),
    whatever the expense account's tag."""
    business = Account("Asset_Business_Alice", "asset", "business", "alice")
    spending = [
        _paid(PROPERTY_TAX, 10_000_00, tags="tax:property", payer=business),
        _paid(PROPERTY_TAX, 10_000_00, tags="tax:property", character="business_expense"),
        _paid(PROPERTY_TAX, 10_000_00, tags="tax:property tax:rental"),
        _paid(GIFTS, 10_000_00, payer=business),
        _paid(PROPERTY_TAX, 3_000_00, tags="tax:property"),
    ]
    ret = _federal(200_000_00, spending)
    assert ret["itemized_salt"] == 3_000_00
    assert ret["itemized_charity"] == 0


@pytest.mark.slow
def test_a_year_to_date_catch_up_is_not_read_as_a_larger_debt(
    add_state_tax: Callable[..., None], retire_alice_csv: Callable[[str], Path],
    run_test_copy: Callable[[], Path],
) -> None:
    """The months before the start come as one catch-up transfer; its
    interest is on the debt of the months after, not on several times it,
    so a mortgage well under the limit keeps all its interest."""
    add_state_tax()
    path = retire_alice_csv("scheduled.csv")
    rows = pd.read_csv(path, dtype=str, keep_default_na=False)
    mortgage = rows["amount_rule_kind"] == "mortgage_payment"
    rows.loc[mortgage, "ytd_amount"] = "7400.00"
    rows.loc[mortgage, "start"] = "2026-01-31"
    rows.to_csv(path, index=False)
    run = run_test_copy()
    with closing(sqlite3.connect(run)) as con:
        catchup, interest = con.execute(
            "SELECT SUM(CASE WHEN t.reason_kind = 'ytd_rule' THEN t.amount ELSE 0 END), SUM(t.amount) "
            "FROM transfer_event t JOIN account a ON a.id = t.to_account "
            "WHERE a.name = 'Expense_Interest_Mortgage' AND substr(t.date, 1, 4) = '2026'").fetchone()
    assert catchup > 0
    assert _lines(run, "federal_itemized_mortgage_interest")["federal_itemized_mortgage_interest"][2026] \
        == interest


def test_a_business_mortgage_stays_out_of_the_debt_limit() -> None:
    """An office's mortgage paid from a business account is Schedule C's,
    even when its interest goes into the home's tagged account: its debt
    doesn't shrink the home's deductible interest."""
    business = Account("Asset_Business_Alice", "asset", "business", "alice")
    spending = [_paid(INTEREST, 36_000_00, tags="tax:mortgage_interest"),
                _paid(INTEREST, 54_000_00, tags="tax:mortgage_interest", payer=business)]
    mortgages = [*_months("Liability_Mortgage", range(1, 13), 3_000_00, 600_000_00),
                 *[replace(m, payer=business) for m in
                   _months("Liability_Office", range(1, 13), 4_500_00, 900_000_00)]]
    ret = _federal(300_000_00, spending, mortgages=mortgages)
    assert ret["itemized_mortgage_interest"] == 36_000_00


def test_an_implied_tag_keeps_the_accounts_own_tax_tag() -> None:
    """An IRMAA surcharge into an account tagged ``tax:premiums`` (for a
    state's premium adjustment) is both a premium and a medical expense."""
    premiums = Account("Expense_Medicare", "expense", "", None, tags=frozenset({"tax:premiums"}))
    item = SpendingItem(YEAR, "none", CHECKING, premiums, 1_00,
                        implied_tags=frozenset({"tax:medical"}))
    assert item.tags == {"tax:premiums", "tax:medical"}


def test_a_state_refund_comes_off_the_state_tax_paid(ledger_con: sqlite3.Connection) -> None:
    """A refund (#568) posts from the state's account under its id, as a
    settlement does; it takes back what was paid rather than adding to it."""
    from analysis.sim.engine import _taxes_paid
    checking, state, federal = 1, 2, 3
    con = ledger_con
    con.executemany("INSERT INTO account(id, name, kind, subkind) VALUES (?, ?, ?, ?)", [
        (checking, "Asset_Checking", "asset", "cash"),
        (state, "Expense_Tax_State", "expense", "taxes_state"),
        (federal, "Expense_Tax_Federal", "expense", "taxes_federal")])
    con.executemany("INSERT INTO chain(id, name, account_id) VALUES (?, ?, ?)", [
        (checking, "Asset_Checking", checking), (state, "Expense_Tax_State", state),
        (federal, "Expense_Tax_Federal", federal)])
    con.executemany(
        "INSERT INTO tax_jurisdiction(id, name, kind, source_chain_id, destination_chain_id, "
        "schedule, start) VALUES (?, ?, ?, ?, ?, 'april', '2026-01')",
        [(1, "federal", "federal", checking, federal), (2, "state", "state_tables", checking, state)])

    def post(date: str, frm: int, to: int, amount: int, kind: str, reason: int | None,
             tax_year: int | None = None) -> None:
        con.execute(
            "INSERT INTO transfer_event(step_id, date, from_account, to_account, from_lot_id, "
            "to_lot_id, amount, requested_cents, remainder_cents, tax_character, reason_kind, "
            "reason_id, tax_year) VALUES (1, ?, ?, ?, ?, ?, ?, ?, 0, 'none', ?, ?, ?)",
            (date, frm, to, frm, to, amount, amount, kind, reason, tax_year))

    post("2028-06-30", checking, state, 5_000_00, "rule", None)
    post("2028-04-30", state, checking, 1_000_00, "tax", 2, 2027)
    paid = _taxes_paid(con, 2028, {}, lambda aid: Account(f"#{aid}", "", "", None))
    assert [(p.jurisdiction, p.amount) for p in paid] == [("state", 4_000_00)]
    # A refund larger than the year's payments deducts nothing.
    post("2028-05-31", state, checking, 9_000_00, "tax", 2, 2027)
    paid = _taxes_paid(con, 2028, {}, lambda aid: Account(f"#{aid}", "", "", None))
    assert [(p.jurisdiction, p.amount) for p in paid] == [("state", 0)]
