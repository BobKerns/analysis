"""Itemized deductions from the year's spending (#542): state and local
taxes under the SALT cap and its phase-down, mortgage interest under the
debt limit, charity and medical expenses past their AGI floors, and the
hand-typed total on top; the federal limits, a state's own, and the
loader's checks on them."""

from __future__ import annotations

import json
import sqlite3
import sys
import types
from collections import defaultdict
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

# The calculator subclasses below are written for the current API: an
# undeclared household module is taken to predate the itemized lines.
API_VERSION = 5

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
    prior: Mapping[str, int] | None = None,
) -> TaxYear:
    """A year of ``salary`` with the ``spending``: every table in force from
    2020, and the household in California. ``prior`` is last year's
    federal return's lines."""
    prior = prior or {}
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
        _prior=lambda line: prior.get(line, 0) if jurisdiction == "federal" else 0,
        _series_of=lambda kind: sorted(s for k, s in tables if k == kind and s is not None),
        _residences=[("2020-01-31", Residence("CA"))],
        _upstream=lambda name: calculator(name).prepare(_year(
            salary, spending, jurisdiction=name, mortgages=mortgages, taxes_paid=taxes_paid,
            tables=tables, prior=prior)),
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
    # Charity is capped at 60% of AGI, and the 0.5% floor comes off what
    # the cap allows (§170(b)(1)(I)): 6,000 less 50.
    assert _federal(10_000_00, [_paid(GIFTS, 10_000_00)])["itemized_charity"] == 5_950_00


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
    # An MFS row halved from MFJ's but for its floor would double its cap.
    ("2026-01-31,MFJ,5000.00,10000.00,,,,,,", "salt_floor can't be above salt_cap"),
    # Without a cap, a floor or phase-down would be ignored without a word.
    ("2026-01-31,MFJ,,10000.00,,,,,,", "needs a salt_cap"),
    ("2026-01-31,MFJ,,,500000.00,30%,,,,", "needs a salt_cap"),
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


class _OlderOwnDeductions(StateTablesYear):
    """:class:`_OwnDeductions` as written for API 4, before the lines."""

    def deductions(self) -> Mapping[str, int]:
        own = self.ty.spent("tax:property")
        return {"itemized_deduction": own, "deduction": max(12_000_00, own)}


def test_a_subclass_written_before_the_lines_keeps_its_own_schedule_a(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its module says API 4, from before itemized_lines existed, so its
    deductions() is taken to figure the whole Schedule A."""
    monkeypatch.setattr(sys.modules[__name__], "API_VERSION", 4, raising=False)
    year = _OlderOwnDeductions(calculator("state_tables"), _year(
        200_000_00, [_paid(PROPERTY_TAX, 20_000_00, tags="tax:property")],
        jurisdiction="state", tables={**TABLES, **CA_LIMITS}))
    ret = year.compute()
    assert "itemized_salt" not in ret.lines
    assert ret["deduction"] == 20_000_00
    monkeypatch.setattr(sys.modules[__name__], "API_VERSION", 5)
    assert _OlderOwnDeductions(calculator("state_tables"), year.ty).compute()["itemized_salt"] \
        == 20_000_00


def test_a_household_module_without_an_api_version_keeps_its_own_schedule_a(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API_VERSION is optional, and a module written before the lines had
    no reason to say one."""
    monkeypatch.delattr(sys.modules[__name__], "API_VERSION")
    year = _OlderOwnDeductions(calculator("state_tables"), _year(
        200_000_00, [_paid(PROPERTY_TAX, 20_000_00, tags="tax:property")],
        jurisdiction="state", tables={**TABLES, **CA_LIMITS}))
    assert "itemized_salt" not in year.compute().lines


class _NoStandard(StateTablesYear):
    """A state with a $12,000 exemption and no standard deduction, which
    leaves the itemized lines to the built-in."""

    def deductions(self) -> Mapping[str, int]:
        return {"itemized_deduction": 0, "deduction": 12_000_00}


def test_a_subclass_without_a_standard_deduction_keeps_its_exemption_beside_the_lines() -> None:
    """No standard_deduction line it never figured, and its exemption is
    taken on top of the itemized total, not instead of it."""
    year = _NoStandard(calculator("state_tables"), _year(
        200_000_00, [_paid(PROPERTY_TAX, 20_000_00, tags="tax:property")],
        jurisdiction="state", tables={**TABLES, **CA_LIMITS}))
    ret = year.compute()
    assert "standard_deduction" not in ret.lines
    assert ret["itemized_salt"] == 20_000_00
    assert ret["deduction"] == 32_000_00


class _Capped(StateTablesYear):
    """A state that caps the deduction taken at $1,000."""

    def deductions(self) -> Mapping[str, int]:
        base = super().deductions()
        return {**base, "deduction": min(base["deduction"], 1_000_00)}


def test_a_cap_in_deductions_holds_with_the_lines() -> None:
    """A deductions() that takes less than its own standard or itemized
    total caps the deduction; $50,000 of property tax doesn't lift it."""
    year = _Capped(calculator("state_tables"), _year(
        200_000_00, [_paid(PROPERTY_TAX, 50_000_00, tags="tax:property")],
        jurisdiction="state", tables={**TABLES, **CA_LIMITS}))
    ret = year.compute()
    assert ret["itemized_salt"] == 50_000_00
    assert ret["deduction"] == 1_000_00


def test_a_report_tag_on_a_rule_keeps_the_accounts_itemizing_tag() -> None:
    """A rule tagged tax:itemized for a report still pays charity into an
    account tagged tax:charity; a rule's own itemizing tag does move it."""
    report = _paid(GIFTS, 20_000_00, tags="tax:itemized")
    assert "tax:charity" in report.tags
    assert _federal(200_000_00, [report])["itemized_charity"] == 19_000_00
    moved = _paid(GIFTS, 20_000_00, tags="tax:medical")
    assert "tax:charity" not in moved.tags


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
    """``property_account`` pays the housing rule, untagged, into an
    account of subkind taxes_property (#586); ``report_tag`` tags the
    housing rule ``tax:property`` and the state tax's account and the
    mortgage interest's ``tax:itemized``, a tag for a report."""
    path = retire_alice_csv("scheduled.csv")
    rows = pd.read_csv(path, dtype=str, keep_default_na=False)
    housing = rows["name"] == "housing"
    accounts = pd.read_csv(retire_alice_csv("accounts.csv"), dtype=str, keep_default_na=False)
    if variant == "property_account":
        rows.loc[housing, "to_account"] = "Expense_Taxes_Property"
        accounts.loc[len(accounts)] = {
            **dict.fromkeys(accounts.columns, ""), "name": "Expense_Taxes_Property", "kind": "expense",
            "subkind": "taxes_property", "owner": "joint", "tax_treatment": "none",
            "opened": "2020-01-31", "meta_json": "{}"}
    else:
        rows.loc[housing, "tags"] = "tax:property"
        accounts.loc[accounts["name"].isin(["Expense_Taxes_State", "Expense_Interest_Mortgage"]),
                     "tags"] = "tax:itemized"
    rows.to_csv(path, index=False)
    accounts.to_csv(retire_alice_csv("accounts.csv"), index=False)


@pytest.mark.slow
@pytest.mark.parametrize("variant", ["property_account", "report_tag"])
def test_state_tax_paid_and_property_tax_count_once(
    add_state_tax: Callable[..., None], retire_alice_csv: Callable[[str], Path],
    run_test_copy: Callable[[], Path], variant: str,
) -> None:
    """The state's settlements count though its account has a tax: tag for
    a report, and property tax counts once, whether tagged or paid into a
    taxes_property account with no tag (#586)."""
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
        # A settlement out of the state's account is a refund (#568), for
        # the year before: never netted.
        state_paid = {year: max(0, paid) for year, paid in con.execute(
            "SELECT CAST(substr(t.date, 1, 4) AS INTEGER), "
            "       SUM(CASE WHEN t.from_account <> c.account_id THEN t.amount "
            "                WHEN COALESCE(t.tax_year, CAST(substr(t.date, 1, 4) AS INTEGER)) "
            "                     = CAST(substr(t.date, 1, 4) AS INTEGER) THEN -t.amount "
            "                ELSE 0 END) "
            "FROM transfer_event t JOIN tax_jurisdiction j ON j.id = t.reason_id "
            "JOIN chain c ON c.id = j.destination_chain_id "
            "WHERE t.reason_kind = 'tax' AND j.name = 'state' GROUP BY 1")}
        interest = dict(con.execute(
            "SELECT CAST(substr(t.date, 1, 4) AS INTEGER), SUM(t.amount) FROM transfer_event t "
            "JOIN account a ON a.id = t.to_account WHERE a.name = 'Expense_Interest_Mortgage' "
            "GROUP BY 1"))
    assert state_paid[2028] > 0
    for year in (2028, 2029):
        assert lines["federal_itemized_mortgage_interest"][year] == interest[year] > 0
        assert salt[year] == housing[year] + state_paid.get(year, 0), year


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


@pytest.mark.parametrize("row", ["2025-01-31,30000.00", "2027-01-31,0.00"])
def test_a_hand_typed_total_nothing_adds_to_is_no_warning(
    write_aux: Callable[[str, str], Path], load_test_copy: Callable[[], Any], row: str,
) -> None:
    """A year before the limits' first date (2026) itemizes only its
    hand-typed total, and a zero adds nothing to the lines."""
    write_aux("federal_deductions.csv", f"effective_date,amount\n{row}\n")
    warnings = load_test_copy().warnings
    assert not any("the hand-typed total adds" in w for w in warnings), warnings


@pytest.mark.parametrize(("irmaa_start", "warns"), [("2040-01-31", False), ("2030-01-31", True)])
def test_a_hand_typed_total_warns_only_in_a_year_a_rule_adds_lines(
    retire_alice_csv: Callable[[str], Path], write_aux: Callable[[str, str], Path],
    add_tax: Callable[..., Path], append_scheduled_row: Callable[..., None],
    load_test_copy: Callable[[], Any], irmaa_start: str, warns: bool,
) -> None:
    """An IRMAA surcharge adds to the medical line only from its own start,
    as a mortgage adds interest only while it runs: a 2030 total beside a
    mortgage paid off in 2028 and an irmaa rule from 2040 counts nothing
    twice."""
    path = retire_alice_csv("scheduled.csv")
    scheduled = pd.read_csv(path, dtype=str, keep_default_na=False)
    scheduled.loc[scheduled["name"] == "mortgage_pmt", "end"] = "2028-12-31"
    scheduled.to_csv(path, index=False)
    add_tax()
    append_scheduled_row(name="irmaa_alice", from_account="Asset_Checking",
                         to_account="Expense_Healthcare", amount_rule_kind="irmaa",
                         person="alice", tax_character="none", start=irmaa_start)
    write_aux("federal_deductions.csv", "effective_date,amount\n2030-01-31,30000.00\n")
    warnings = load_test_copy().warnings
    assert any("federal_deductions.csv (2030)" in w for w in warnings) == warns, warnings


def _set_cells(path: Path, name: str, **cells: str) -> None:
    """Set cells of the row ``name`` in one of the copy's CSVs."""
    rows = pd.read_csv(path, dtype=str, keep_default_na=False)
    for column, value in cells.items():
        rows.loc[rows["name"] == name, column] = value
    rows.to_csv(path, index=False)


@pytest.mark.parametrize("change", ["heloc", "retired"])
def test_a_rule_that_itemizes_nothing_then_is_no_warning(
    retire_alice_csv: Callable[[str], Path], write_aux: Callable[[str, str], Path],
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], Any], change: str,
) -> None:
    """A mortgage_payment amortizing a HELOC implies no mortgage interest,
    and one a calendar row retires before 2027 has none in 2027."""
    if change == "heloc":
        _set_cells(retire_alice_csv("accounts.csv"), "Liability_Mortgage", subkind="heloc")
    else:
        append_calendar_row(date="2026-12-31", kind="retire_scheduled", scheduled_name="mortgage_pmt")
    write_aux("federal_deductions.csv", "effective_date,amount\n2027-01-31,30000.00\n")
    warnings = load_test_copy().warnings
    assert not any("the hand-typed total adds" in w for w in warnings), warnings


def test_a_federal_total_beside_a_state_income_tax_is_a_warning(
    retire_alice_csv: Callable[[str], Path], write_aux: Callable[[str, str], Path],
    add_state_tax: Callable[..., None], load_test_copy: Callable[[], Any],
) -> None:
    """The federal SALT line adds the state income tax paid, which a total
    written as a whole Schedule A already counted."""
    _set_cells(retire_alice_csv("scheduled.csv"), "mortgage_pmt", end="2027-12-31")
    add_state_tax()
    write_aux("federal_deductions.csv", "effective_date,amount\n2028-01-31,30000.00\n")
    warnings = load_test_copy().warnings
    assert any("federal_deductions.csv (2028): the hand-typed total adds" in w
               and "state and local income tax" in w for w in warnings), warnings


@pytest.mark.parametrize(("state", "warns"), [("ID", True), ("CA", False)])
def test_a_state_without_lines_falling_back_to_the_federal_total_is_a_warning(
    write_aux: Callable[[str, str], Path], add_state_tax: Callable[..., None],
    load_test_copy: Callable[[], Any], state: str, warns: bool,
) -> None:
    """Idaho figures no lines and takes federal_deductions, now only the
    hand-typed rest, so its return loses the mortgage interest the federal
    lines count. California figures its own."""
    add_state_tax(state)
    write_aux("federal_deductions.csv", "effective_date,amount\n2027-01-31,2000.00\n")
    warnings = load_test_copy().warnings
    assert any("figures no itemized lines" in w for w in warnings) == warns, warnings


def test_property_tax_by_its_character_from_a_chain_is_refused(
    append_scheduled_row: Callable[..., None], load_test_copy: Callable[[], Any],
) -> None:
    """A chain's legs carry their source's character, so the rule's
    property_tax would never reach the ledger."""
    append_scheduled_row(name="county_tax", from_account="asset_checking_liquidity",
                         to_account="Expense_Housing", amount_rule_kind="fixed_amount",
                         amount="500.00", tax_character="property_tax", start="2026-05-31")
    with pytest.raises(LoadError, match="property_tax must be paid out of an account named directly"):
        load_test_copy()


def test_a_year_to_date_row_into_two_home_mortgages_interest_is_refused(
    retire_alice_csv: Callable[[str], Path], append_scheduled_row: Callable[..., None],
    load_test_copy: Callable[[], Any],
) -> None:
    """Whose debt the row's interest was on decides the debt limit."""
    accounts = pd.read_csv(retire_alice_csv("accounts.csv"), dtype=str, keep_default_na=False)
    second = accounts[accounts["name"] == "Liability_Mortgage"].iloc[0].to_dict()
    accounts.loc[len(accounts)] = {**second, "name": "Liability_Second"}
    accounts.to_csv(retire_alice_csv("accounts.csv"), index=False)
    append_scheduled_row(name="second_pmt", from_account="Asset_Checking", to_account="Liability_Second",
                         amount_rule_kind="mortgage_payment", interest_account="Expense_Interest_Mortgage",
                         rate="6%", amount="1000.00", tax_character="none", start="2026-05-31")
    with open(retire_alice_csv("year_to_date.csv"), "a") as f:
        f.write("prior_interest,Asset_Checking,,Expense_Interest_Mortgage,,4000.00,none,,\n")
    with pytest.raises(LoadError, match="interest account of several home mortgages"):
        load_test_copy()


def test_the_tags_a_rule_kind_implies_are_the_scenarios() -> None:
    """A calculator's Tag param may name tax:mortgage_interest when only a
    home mortgage's rule carries it, and tax:medical when only IRMAA does."""
    from analysis.sim.loader import _scenario_tags
    accounts = [{"name": "Liability_Mortgage", "subkind": "mortgage"},
                {"name": "Liability_Car", "subkind": "other_liability"}]
    rules = [{"amount_rule_kind": "mortgage_payment", "to_account": "Liability_Mortgage"},
             {"amount_rule_kind": "irmaa", "to_account": "Expense_Healthcare"}]
    assert _scenario_tags(accounts, rules, []) == {"tax:mortgage_interest", "tax:medical"}
    car = [{"amount_rule_kind": "mortgage_payment", "to_account": "Liability_Car"}]
    assert _scenario_tags(accounts, car, []) == frozenset()


@pytest.mark.parametrize(("state", "warns"), [("CA", True), ("WA", False)])
def test_a_state_total_warns_only_where_the_state_figures_lines(
    write_aux: Callable[[str, str], Path], add_state_tax: Callable[..., None],
    load_test_copy: Callable[[], Any], state: str, warns: bool,
) -> None:
    """California has limits of its own (the template's), so its lines
    are figured beside a hand-typed state total; a state without them
    itemizes only the hand-typed total, whatever California's table."""
    add_state_tax(state)
    write_aux("state_deductions.csv", "effective_date,amount\n2027-01-31,30000.00\n")
    warnings = load_test_copy().warnings
    assert any("state_deductions.csv (2027)" in w for w in warnings) == warns, warnings


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


def test_business_expenses_and_rental_spending_are_never_itemized() -> None:
    """Schedule C deducts a business expense (the business_expense
    character) and Schedule E a rental's (tax:rental), whatever the
    expense account's tag. The household's own spending paid from
    business checking is still the household's."""
    business = Account("Asset_Business_Alice", "asset", "business", "alice")
    spending = [
        _paid(PROPERTY_TAX, 10_000_00, tags="tax:property", payer=business,
              character="business_expense"),
        _paid(PROPERTY_TAX, 10_000_00, tags="tax:property tax:rental"),
        _paid(GIFTS, 10_000_00, payer=business, character="business_expense"),
        _paid(PROPERTY_TAX, 3_000_00, tags="tax:property"),
    ]
    ret = _federal(200_000_00, spending)
    assert ret["itemized_salt"] == 3_000_00
    assert ret["itemized_charity"] == 0
    own = [_paid(PROPERTY_TAX, 4_000_00, tags="tax:property", payer=business),
           _paid(GIFTS, 12_000_00, payer=business)]
    ret = _federal(200_000_00, [*spending, *own])
    assert ret["itemized_salt"] == 7_000_00
    # Over 0.5% of AGI.
    assert ret["itemized_charity"] == 12_000_00 - 1_000_00


@pytest.mark.slow
@pytest.mark.parametrize(("start", "ytd", "first"), [("2026-01-31", "7400.00", 1), ("2026-03-31", "3700.00", 3)])
def test_a_year_to_date_catch_up_is_not_read_as_a_larger_debt(
    add_state_tax: Callable[..., None], retire_alice_csv: Callable[[str], Path],
    run_test_copy: Callable[[], Path], start: str, ytd: str, first: int,
) -> None:
    """The months before the start come as one catch-up transfer; its
    interest is on the debt of the months it replays, from the loan's
    start that year, not on several times it, so a mortgage well under
    the limit keeps all its interest."""
    add_state_tax()
    path = retire_alice_csv("scheduled.csv")
    rows = pd.read_csv(path, dtype=str, keep_default_na=False)
    mortgage = rows["amount_rule_kind"] == "mortgage_payment"
    rows.loc[mortgage, "ytd_amount"] = ytd
    rows.loc[mortgage, "start"] = start
    rows.to_csv(path, index=False)
    run = run_test_copy()
    with closing(sqlite3.connect(run)) as con:
        catchup, interest = con.execute(
            "SELECT SUM(CASE WHEN t.reason_kind = 'ytd_rule' THEN t.amount ELSE 0 END), SUM(t.amount) "
            "FROM transfer_event t JOIN account a ON a.id = t.to_account "
            "WHERE a.name = 'Expense_Interest_Mortgage' AND substr(t.date, 1, 4) = '2026'").fetchone()
        # Each month's balance is recorded as the interest was figured: the
        # catch-up's months replayed from the loan's start, then each payment.
        balances = con.execute(
            "SELECT month, balance FROM mortgage_balance WHERE month LIKE '2026-%' ORDER BY month").fetchall()
    assert catchup > 0
    assert [month for month, _ in balances] == [f"2026-{m:02d}" for m in range(first, 13)]
    assert all(a[1] > b[1] for a, b in zip(balances, balances[1:], strict=False))
    assert _lines(run, "federal_itemized_mortgage_interest")["federal_itemized_mortgage_interest"][2026] \
        == interest


def test_a_business_mortgage_stays_out_of_the_debt_limit() -> None:
    """An office's mortgage paid from a business account is Schedule C's,
    even when its interest goes into the home's tagged account: its debt
    doesn't shrink the home's deductible interest."""
    business = Account("Asset_Business_Alice", "asset", "business", "alice")
    spending = [_paid(INTEREST, 36_000_00, tags="tax:mortgage_interest"),
                _paid(INTEREST, 54_000_00, tags="tax:mortgage_interest", payer=business,
                      character="business_expense")]
    mortgages = [*_months("Liability_Mortgage", range(1, 13), 3_000_00, 600_000_00),
                 *[replace(m, character="business_expense") for m in
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


def test_a_state_refund_never_comes_off_the_state_tax_paid(ledger_con: sqlite3.Connection) -> None:
    """A refund (#568) posts from the state's account under its id, as a
    settlement does, the April after the year it's for. Under the
    tax-benefit rule it is income of the year received, to the extent the
    earlier deduction saved tax, which isn't modeled; it isn't less paid,
    nor a payment. What went into the state's own account that year (#586)
    is what was paid; the federal account's payments are the federal
    tax's."""
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
    post("2028-04-30", checking, state, 700_00, "tax", 2, 2027)
    post("2028-04-30", state, checking, 1_000_00, "tax", 2, 2027)
    post("2028-06-30", checking, federal, 9_000_00, "rule", None)
    post("2029-01-15", checking, state, 400_00, "rule", None, 2028)
    paid = _taxes_paid(con, 2028)
    assert [(p.jurisdiction, p.amount) for p in paid] == [("state", 5_700_00)]


class _InheritedOwnDeductions(_OlderOwnDeductions):
    """A subclass in a household module written for API 5 that only
    inherits an API-4 module's Schedule A."""


_InheritedOwnDeductions.__module__ = "household_later"


def test_an_inherited_older_schedule_a_keeps_its_api_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """The subclass's own module says API 5, but its deductions() is the
    API-4 module's, which decides."""
    later = types.ModuleType("household_later")
    vars(later)["API_VERSION"] = 5
    monkeypatch.setitem(sys.modules, "household_later", later)
    monkeypatch.setattr(sys.modules[__name__], "API_VERSION", 4, raising=False)
    year = _InheritedOwnDeductions(calculator("state_tables"), _year(
        200_000_00, [_paid(PROPERTY_TAX, 20_000_00, tags="tax:property")],
        jurisdiction="state", tables={**TABLES, **CA_LIMITS}))
    assert "itemized_salt" not in year.compute().lines


def _mortgage_ledger(con: sqlite3.Connection, *, preroll: bool = False, ytd: int | None = None) -> int:
    """A $10,000-a-month mortgage at 6% from checking (1) to its liability
    (2), interest into account 3, in ``ledger_con``; returns the interest
    account's id."""
    checking, liability, interest = 1, 2, 3
    con.executemany("INSERT INTO account(id, name, kind, subkind) VALUES (?, ?, ?, ?)", [
        (checking, "Asset_Checking", "asset", "cash"),
        (liability, "Liability_Mortgage", "liability", "mortgage"),
        (interest, "Expense_Interest_Mortgage", "expense", "interest_expense")])
    con.executemany("INSERT INTO chain(id, name, account_id) VALUES (?, ?, ?)", [
        (checking, "Asset_Checking", checking), (liability, "Liability_Mortgage", liability)])
    con.execute(
        "INSERT INTO scheduled_transaction(id, name, from_account, to_account, cadence, "
        "amount_rule_kind, tax_character, start, rate_micros, amount_cents, ytd_amount, ytd_preroll) "
        "VALUES (1, 'mortgage', ?, ?, 'monthly', 'mortgage_payment', 'none', '2025-01-31', 60000, "
        "1000000, ?, ?)", (checking, liability, ytd, int(preroll)))
    return interest


def _post_interest(con: sqlite3.Connection, date: str, amount: int, kind: str,
                   character: str = "none", *, rule: int = 1) -> None:
    con.execute(
        "INSERT INTO transfer_event(step_id, date, from_account, to_account, from_lot_id, "
        "to_lot_id, amount, requested_cents, remainder_cents, tax_character, reason_kind, "
        "reason_id) VALUES (1, ?, 1, 3, 1, 3, ?, ?, 0, ?, ?, ?)",
        (date, amount, amount, character, kind, rule))


def _record_balance(con: sqlite3.Connection, posted: str, months: Sequence[int], balance: int,
                    interest: int, *, rule: int = 1) -> None:
    """The balance the engine figured each of ``months``' interest on, as
    posted on ``posted`` (mortgage_balance)."""
    con.executemany(
        "INSERT INTO mortgage_balance(step_id, rule_id, posted, month, balance, interest) "
        "VALUES (1, ?, ?, ?, ?, ?)",
        [(rule, posted, f"{posted[:4]}-{month:02d}", balance, interest) for month in months])


def _months_of(con: sqlite3.Connection, interest: int) -> list[Mortgage]:
    from analysis.sim.engine import _implied_rules, _mortgage_months
    return _mortgage_months(con, 2026, {}, lambda aid: Account(f"#{aid}", "expense", "", None),
                            [interest], *_implied_rules(con))


@pytest.mark.parametrize(("kind", "payments"), [
    ("ytd_amount", 5), ("ytd_amount", 2), ("ytd_preroll", 5), ("year_to_date", 5)])
def test_a_catch_up_with_no_regular_month_still_meets_the_debt_limit(
    ledger_con: sqlite3.Connection, kind: str, payments: int,
) -> None:
    """A $2M interest-only mortgage paid off before the run starts has only
    its catch-up that year. A ``ytd_amount`` posts its payments' interest
    at once in May, but each month it replays is tested on its own balance;
    a ``ytd_preroll`` posts each month on its own. A year_to_date.csv row,
    which no rule figured, is spread evenly over the months to its own."""
    con = ledger_con
    interest = _mortgage_ledger(con, preroll=kind == "ytd_preroll",
                                ytd=payments * 10_000_00 if kind == "ytd_amount" else None)
    if kind == "ytd_preroll":
        for month in range(1, payments + 1):
            _post_interest(con, f"2026-{month:02d}-28", 10_000_00, "ytd_rule")
            _record_balance(con, f"2026-{month:02d}-28", [month], 2_000_000_00, 10_000_00)
    elif kind == "ytd_amount":
        _post_interest(con, "2026-05-31", payments * 10_000_00, "ytd_rule")
        _record_balance(con, "2026-05-31", range(1, payments + 1), 2_000_000_00, 10_000_00)
    else:
        con.execute("UPDATE scheduled_transaction SET interest_account = 'Expense_Interest_Mortgage'")
        _post_interest(con, f"2026-{payments:02d}-31", payments * 10_000_00, "ytd")
    months = _months_of(con, interest)
    assert {m.balance for m in months} == {2_000_000_00}
    assert sum(m.interest for m in months) == payments * 10_000_00
    assert len(months) == payments


def test_a_catch_up_and_a_preroll_are_one_debt_in_each_month(ledger_con: sqlite3.Connection) -> None:
    """Two $600,000 mortgages at 6% from a June start: one's January to May
    interest posts as one ``ytd_amount`` in May, the other's month by month.
    Each month's debt is $1.2M, not one loan's $600,000 until May."""
    con = ledger_con
    interest = _mortgage_ledger(con, ytd=15_000_00)
    con.execute("INSERT INTO account(id, name, kind, subkind) "
                "VALUES (4, 'Liability_Second', 'liability', 'mortgage')")
    con.execute("INSERT INTO chain(id, name, account_id) VALUES (4, 'Liability_Second', 4)")
    con.execute(
        "INSERT INTO scheduled_transaction(id, name, from_account, to_account, cadence, "
        "amount_rule_kind, tax_character, start, rate_micros, amount_cents, ytd_preroll) "
        "VALUES (2, 'second', 1, 4, 'monthly', 'mortgage_payment', 'none', '2025-01-31', 60000, "
        "500000, 1)")
    _post_interest(con, "2026-05-31", 15_000_00, "ytd_rule")
    _record_balance(con, "2026-05-31", range(1, 6), 600_000_00, 3_000_00)
    for month in range(1, 6):
        _post_interest(con, f"2026-{month:02d}-28", 3_000_00, "ytd_rule", rule=2)
        _record_balance(con, f"2026-{month:02d}-28", [month], 600_000_00, 3_000_00, rule=2)
    debt: dict[int, int] = defaultdict(int)
    for m in _months_of(con, interest):
        debt[m.month] += m.balance
    assert debt == dict.fromkeys(range(1, 6), 1_200_000_00)


@pytest.mark.parametrize("rows", [[15_000_00], [4_000_00, 11_000_00], [11_000_00, 4_000_00]])
def test_a_year_to_date_row_is_spread_and_summed_beside_a_preroll(
    ledger_con: sqlite3.Connection, rows: list[int],
) -> None:
    """Two $600,000 mortgages at 6% from a June start: one's January to May
    interest is a year_to_date.csv row (or two, Alice's and Bill's), the
    other's posts month by month. The rows are spread over January to May
    and summed before their debt is read, so each month is $1.2M however
    the rows are split."""
    con = ledger_con
    interest = _mortgage_ledger(con)
    con.execute("UPDATE scheduled_transaction SET interest_account = 'Expense_Interest_Mortgage'")
    con.execute("INSERT INTO account(id, name, kind, subkind) "
                "VALUES (4, 'Liability_Second', 'liability', 'mortgage')")
    con.execute("INSERT INTO chain(id, name, account_id) VALUES (4, 'Liability_Second', 4)")
    con.execute(
        "INSERT INTO scheduled_transaction(id, name, from_account, to_account, cadence, "
        "amount_rule_kind, tax_character, start, rate_micros, amount_cents, ytd_preroll) "
        "VALUES (2, 'second', 1, 4, 'monthly', 'mortgage_payment', 'none', '2025-01-31', 60000, "
        "500000, 1)")
    for amount in rows:
        _post_interest(con, "2026-05-31", amount, "ytd")
    for month in range(1, 6):
        _post_interest(con, f"2026-{month:02d}-28", 3_000_00, "ytd_rule", rule=2)
        _record_balance(con, f"2026-{month:02d}-28", [month], 600_000_00, 3_000_00, rule=2)
    debt: dict[int, int] = defaultdict(int)
    for m in _months_of(con, interest):
        debt[m.month] += m.balance
    assert debt == dict.fromkeys(range(1, 6), 1_200_000_00)


def test_a_year_to_date_row_carries_its_mortgage_rules_tags(ledger_con: sqlite3.Connection) -> None:
    """A rental's pre-start interest, a year_to_date.csv row into its
    rule's interest account, is the rental's as the rule's own interest
    is: tagged tax:rental, never home mortgage interest."""
    from analysis.sim.engine import _tax_ledger
    con = ledger_con
    _mortgage_ledger(con)
    con.execute("UPDATE scheduled_transaction SET interest_account = 'Expense_Interest_Mortgage'")
    con.execute("INSERT INTO tag(id, role, name) VALUES (1, 'tax', 'rental')")
    con.execute("INSERT INTO scheduled_transaction_tag VALUES (1, 1)")
    _post_interest(con, "2026-04-30", 4_000_00, "ytd")
    ledger = _tax_ledger(con, 2026)
    (item,) = [s for s in ledger.spending if s.payee.name == "Expense_Interest_Mortgage"]
    assert item.tags == {"tax:rental"}
    assert {m.character for m in ledger.mortgages} == {"none"}
    assert all("tax:rental" in m.tags for m in ledger.mortgages)


def test_one_payment_drawn_as_two_characters_is_one_debt(ledger_con: sqlite3.Connection) -> None:
    """A $3,000 interest payment on $600,000 at 6%, drawn as two legs of
    different characters, is still on $600,000, shared by its legs."""
    con = ledger_con
    interest = _mortgage_ledger(con)
    _post_interest(con, "2026-03-31", 4_000_00, "rule", "return_of_capital")
    _post_interest(con, "2026-03-31", -1_000_00, "rule", "ltcg")
    _record_balance(con, "2026-03-31", [3], 600_000_00, 3_000_00)
    # One debt-month, not a leg each with a share (one of them negative).
    assert [(m.interest, m.balance) for m in _months_of(con, interest)] == [(3_000_00, 600_000_00)]


@pytest.mark.parametrize(("tags", "warns"), [("", True), ("tax:rental", False)])
def test_a_property_tax_account_beside_a_hand_typed_total_is_a_warning(
    write_aux: Callable[[str, str], Path], retire_alice_csv: Callable[[str], Path],
    load_test_copy: Callable[[], Any], tags: str, warns: bool,
) -> None:
    """#586: a taxes_property account is figured as property tax, so a
    hand-typed total would count it twice; a rental's (tax:rental) isn't
    itemized, so it's no warning. No mortgage here, which warns alone."""
    path = retire_alice_csv("scheduled.csv")
    rows = pd.read_csv(path, dtype=str, keep_default_na=False)
    rows[rows["amount_rule_kind"] != "mortgage_payment"].to_csv(path, index=False)
    accounts = pd.read_csv(retire_alice_csv("accounts.csv"), dtype=str, keep_default_na=False)
    accounts.loc[len(accounts)] = {
        **dict.fromkeys(accounts.columns, ""), "name": "Expense_Taxes_Property", "kind": "expense",
        "subkind": "taxes_property", "owner": "joint", "tax_treatment": "none",
        "opened": "2020-01-31", "meta_json": "{}", "tags": tags}
    accounts.to_csv(retire_alice_csv("accounts.csv"), index=False)
    write_aux("federal_deductions.csv", "effective_date,amount\n2027-01-31,30000.00\n")
    warnings = load_test_copy().warnings
    assert any("the hand-typed total adds" in w for w in warnings) == warns, warnings


# Last year itemized 45,000 against a 30,000 standard deduction, with
# 25,000 of SALT, under the cap: 24,000 of it state tax withheld.
ITEMIZED_LAST_YEAR = {"standard_deduction": 30_000_00, "itemized_deduction": 45_000_00,
                      "deduction": 45_000_00, "itemized_salt": 25_000_00, "salt_paid": 25_000_00}
WITHHELD_LAST_YEAR = TaxPaid(YEAR - 1, "state", 24_000_00)


def _refunded(refund: int, prior: Mapping[str, int] = ITEMIZED_LAST_YEAR,
              paid: Sequence[TaxPaid] = (WITHHELD_LAST_YEAR,), **kw: Any) -> Return:
    return _federal(200_000_00, taxes_paid=[*paid, TaxPaid(YEAR, "state", 0, refund)],
                    prior=prior, **kw)


def test_a_state_refund_is_income_up_to_what_itemizing_gained() -> None:
    """Under the tax-benefit rule (#593) a refund is income the year it
    comes back, but no more than last year's deduction saved: here what
    itemizing gained over the standard deduction."""
    ret = _refunded(19_000_00)
    assert ret["state_refunds"] == 19_000_00
    assert ret["state_refund_income"] == 15_000_00
    assert ret["agi"] == 215_000_00
    assert _refunded(4_000_00)["state_refund_income"] == 4_000_00
    # Without a refund the lines aren't there.
    assert "state_refunds" not in _federal(200_000_00, prior=ITEMIZED_LAST_YEAR).lines


def test_a_refund_after_a_capped_year_recaptures_only_what_the_cap_let_through() -> None:
    """With 50,000 of SALT under a 40,000 cap, a 19,000 refund would have
    left 31,000, so the deduction lost only 9,000."""
    prior = {**ITEMIZED_LAST_YEAR, "itemized_deduction": 60_000_00, "deduction": 60_000_00,
             "itemized_salt": 40_000_00, "salt_paid": 50_000_00}
    assert _refunded(19_000_00, prior)["state_refund_income"] == 9_000_00
    assert _refunded(5_000_00, prior)["state_refund_income"] == 0


def test_a_refund_after_a_standard_deduction_year_is_no_income() -> None:
    prior = {**ITEMIZED_LAST_YEAR, "itemized_deduction": 25_000_00, "deduction": 30_000_00}
    ret = _refunded(19_000_00, prior)
    assert ret["state_refunds"] == 19_000_00
    assert ret["state_refund_income"] == 0
    assert ret["agi"] == 200_000_00
    # Nor after a year with only the hand-typed total, which itemized no SALT.
    prior = {**ITEMIZED_LAST_YEAR, "itemized_salt": 0, "salt_paid": 0}
    assert _refunded(19_000_00, prior)["state_refund_income"] == 0


def test_a_refund_past_the_tax_paid_was_never_deducted() -> None:
    """A refundable credit (#568) can pay back more than was paid; only
    what was paid was in the SALT line."""
    assert _refunded(8_000_00, paid=[TaxPaid(YEAR - 1, "state", 5_000_00)])[
        "state_refund_income"] == 5_000_00


def test_a_deduction_past_agi_saved_nothing() -> None:
    """Pub. 525: when last year's taxable income was below zero, that much
    of the deduction saved no tax, so the recovery is that much less."""
    prior = {**ITEMIZED_LAST_YEAR, "deduction_unused": 4_000_00}
    assert _refunded(19_000_00, prior)["state_refund_income"] == 11_000_00
    # And a year whose deduction is past its income says how far.
    ret = _federal(20_000_00, [_paid(PROPERTY_TAX, 25_000_00, tags="tax:property")])
    assert ret["deduction_unused"] == 10_000_00
    assert ret["salt_paid"] == ret["itemized_salt"] == 25_000_00
    assert "deduction_unused" not in _federal(200_000_00).lines


def test_a_local_refund_is_pooled_with_the_states() -> None:
    """Schedule A pools state and local income taxes under one cap, and so
    do their refunds."""
    paid = [WITHHELD_LAST_YEAR, TaxPaid(YEAR - 1, "local", 1_000_00),
            TaxPaid(YEAR, "local", 0, 2_000_00)]
    ret = _refunded(10_000_00, paid=paid)
    assert ret["state_refunds"] == ret["state_refund_income"] == 12_000_00


def test_a_state_starting_from_federal_agi_leaves_out_its_refund() -> None:
    def state(refund: int) -> Return:
        year = _year(200_000_00, jurisdiction="state", prior=ITEMIZED_LAST_YEAR,
                     taxes_paid=[WITHHELD_LAST_YEAR, TaxPaid(YEAR, "state", 0, refund)])
        return calculator("state_tables").prepare(year).compute()
    assert _refunded(19_000_00)["state_refund_income"] == 15_000_00
    assert state(19_000_00)["agi"] == state(0)["agi"] == 200_000_00


@pytest.mark.slow
def test_a_run_recaptures_an_over_withheld_state_refund(
    add_state_tax: Callable[..., None], append_scheduled_row: Callable[..., None],
    run_test_copy: Callable[[], Path],
) -> None:
    """$3,000 a month withheld for California is far past its tax: the
    refund each April is federal income as far as last year's deduction
    saved tax (#593), and the next year's SALT is what was paid in."""
    add_state_tax()
    append_scheduled_row(
        name="state_withholding", from_account="Asset_Checking", to_account="Expense_Taxes_State",
        amount_rule_kind="fixed_amount", amount="3000.00", tax_character="none",
        start="2026-05-31", priority="200")
    run = run_test_copy()
    lines = _lines(run, *(f"federal_{line}" for line in (
        "state_refunds", "state_refund_income", "standard_deduction", "itemized_deduction",
        "itemized_salt", "salt_paid", "deduction_unused", "ordinary_income")),
        "state_ordinary_income", "state_tax_overpaid")
    refunds, income = lines["federal_state_refunds"], lines["federal_state_refund_income"]
    with closing(sqlite3.connect(run)) as con:
        paid = dict(con.execute(
            "SELECT CAST(substr(t.date, 1, 4) AS INTEGER), SUM(t.amount) FROM transfer_event t "
            "JOIN account a ON a.id = t.to_account WHERE a.name = 'Expense_Taxes_State' GROUP BY 1"))
    assert refunds, "the withholding should leave a refund"
    for year, refund in refunds.items():
        # Each refund is the year before's overpayment.
        assert refund == lines["state_tax_overpaid"][year - 1]

        def last(line: str, year: int = year) -> int:
            return lines[f"federal_{line}"].get(year - 1, 0)
        gained = last("itemized_deduction") - last("standard_deduction")
        lost = min(last("itemized_salt"),
                   last("itemized_salt") - last("salt_paid") + min(refund, paid.get(year - 1, 0)))
        expected = (0 if gained <= 0 or last("itemized_salt") <= 0
                    else max(0, min(refund, gained, lost) - last("deduction_unused")))
        assert income[year] == expected, year
    assert any(income.values())
    # California doesn't tax its own refund: its ordinary income is the
    # federal one less the recapture.
    for year, recaptured in income.items():
        if year in lines["state_ordinary_income"]:
            assert (lines["federal_ordinary_income"][year] - recaptured
                    == lines["state_ordinary_income"][year]), year
