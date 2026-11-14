"""A state's own deductions and credits (#511, #488): the
``state_adjustments__<state>`` table, each row a named line of the state
return figured from tagged spending or income, the filers, or the federal
return, and the loader's checks on it. A credit may be refundable, and an
income row's cap reduced by Social Security (#579)."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest

from analysis.sim.loader import LoadError, LoadResult
from analysis.sim.tax_api import Account, IncomeItem, Person, TaxYear
from analysis.sim.taxes.common import StateAdjustment, state_adjustments

HEADER = ("effective_date,name,step,base,tag,rate,amount,per_person,min_age,filing_status,"
          "income_limit,phaseout,phaseout_rate,less_ss,refundable\n")


def _tag_rule(retire_alice_csv: Callable[[str], Path], rule: str, tags: str) -> None:
    path = retire_alice_csv("scheduled.csv")
    rows = pd.read_csv(path, dtype=str, keep_default_na=False)
    rows.loc[rows["name"] == rule, "tags"] = tags
    rows.to_csv(path, index=False)


def _lines(run: Path, *buckets: str) -> dict[str, dict[int, int]]:
    with closing(sqlite3.connect(run)) as con:
        return {bucket: dict(con.execute(
            "SELECT year, amount FROM tax_year_summary WHERE bucket = ? ORDER BY year", (bucket,)))
            for bucket in buckets}


def _adjustment(*, income_limit: int | None = None, phaseout: int | None = None,
                phaseout_rate_micros: int | None = None, units: int = 1) -> StateAdjustment:
    return StateAdjustment("line", "credit", "filers", 100_00, None, None, income_limit,
                           phaseout, phaseout_rate_micros, units=units)


def test_the_income_test_ends_a_line_at_once_in_a_line_or_by_a_rate() -> None:
    assert _adjustment().after_income_test(100_00, 10**9) == 100_00
    cliff = _adjustment(income_limit=50_000_00)
    assert cliff.after_income_test(100_00, 50_000_00) == 100_00
    assert cliff.after_income_test(100_00, 50_000_01) == 0
    line = _adjustment(income_limit=50_000_00, phaseout=10_000_00)
    assert line.after_income_test(100_00, 55_000_00) == 50_00
    assert line.after_income_test(100_00, 70_000_00) == 0
    # California's exemption credits: $6 of each credit per $2,500 over.
    rate = _adjustment(income_limit=50_000_00, phaseout_rate_micros=2_400, units=2)
    assert rate.after_income_test(300_00, 52_500_00) == 288_00
    assert rate.after_income_test(300_00, 200_000_00) == 0


@pytest.mark.slow
def test_each_row_is_a_line_of_the_state_return(
    add_state_tax: Callable[..., None], write_aux: Callable[[str, str], Path],
    retire_alice_csv: Callable[[str], Path], run_test_copy: Callable[[], Path],
) -> None:
    add_state_tax()
    _tag_rule(retire_alice_csv, "housing", "tax:rent")
    _tag_rule(retire_alice_csv, "salary_bill", "tax:military")
    write_aux("state_adjustments__CA.csv", HEADER + "\n".join([
        # Massachusetts' shape: half the rent, at most $4,000.
        "2026-01-31,rent_deduction,deduction,spending,tax:rent,50%,4000.00,,,,,,",
        "2026-01-31,federal_tax_deduction,deduction,federal_tax,,,1000.00,,,,,,",
        "2026-01-31,military_subtraction,subtraction,income,tax:military,,5000.00,,,,,,",
        "2026-01-31,senior_credit,credit,filers,,,100.00,,62,,,,",
        "2026-01-31,low_income_credit,credit,filers,,,50.00,,,,1000.00,,",
    ]) + "\n")
    run = run_test_copy()
    lines = _lines(run, "state_rent_deduction", "state_federal_tax_deduction",
                   "state_military_subtraction", "state_senior_credit", "state_low_income_credit",
                   "state_credits", "state_tax", "state_deduction", "state_taxable_income", "state_agi")
    # $800 a month: half a year's is past the cap.
    assert lines["state_rent_deduction"][2027] == 4000_00
    assert lines["state_federal_tax_deduction"][2027] == 1000_00
    assert lines["state_military_subtraction"][2027] == 5000_00
    # Alice is 62 in 2027, Bill in 2029, both still working and taxed.
    senior = lines["state_senior_credit"]
    assert senior[2026] == 0 and senior[2027] == senior[2028] == 100_00 and senior[2029] == 200_00
    # Only below $1,000 of state AGI, which they reach only once retired,
    # when California taxes none of their Social Security; and a credit
    # takes no more than the tax left.
    low, agi = lines["state_low_income_credit"], lines["state_agi"]
    assert low[2027] == 0
    assert all(low[y] == 0 for y in low if agi[y] > 1000_00)
    assert all(low[y] + senior[y] == lines["state_credits"][y] for y in low)
    # The deductions come off after AGI, with the deduction taken.
    year = 2027
    assert lines["state_taxable_income"][year] == (
        lines["state_agi"][year] - lines["state_deduction"][year] - 5000_00)
    assert lines["state_credits"][2027] == 100_00


@pytest.mark.slow
def test_the_shipped_california_table_gives_exemption_credits(
    add_state_tax: Callable[..., None], run_test_copy: Callable[[], Path],
) -> None:
    """templates/aux/state_adjustments__CA.csv (2025's figures): $153 for
    each filer, and $153 more for each from 65."""
    add_state_tax()
    run = run_test_copy()
    lines = _lines(run, "state_personal_exemption_credit",
                   "state_senior_exemption_credit", "state_renters_credit")
    personal, senior = lines["state_personal_exemption_credit"], lines["state_senior_exemption_credit"]
    assert personal[2026] == 306_00
    assert personal[2027] > 306_00  # indexed by cpi, to the dollar
    assert personal[2027] % 100 == 0
    assert senior[2029] == 0  # Alice is 65 in 2030
    # Nothing carries tax:rent: no renter's credit.
    assert set(lines["state_renters_credit"].values()) == {0}
    # The lines show what each took, so they add up to the credits taken,
    # never past the tax.
    credits = _lines(run, "state_credits")["state_credits"]
    assert all(personal[y] + senior[y] + lines["state_renters_credit"][y] == credits[y]
               for y in credits)


def test_a_tag_nothing_carries_is_noted_not_refused(
    add_state_tax: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    add_state_tax()
    result = load_test_copy()
    assert any("state_adjustments__CA.csv" in note and "tax:rent" in note for note in result.unused)


@pytest.mark.parametrize(("row", "message"), [
    ("x,nope,spending,tax:rent,,,,,,,,", "step 'nope' must be one of subtraction"),
    ("x,credit,rent,tax:rent,,,,,,,,", "base 'rent' must be one of spending"),
    ("x,credit,spending,,,,,,,,,", "base spending needs the tag it reads"),
    ("x,credit,spending,rent,,,,,,,,", "tag 'rent' must be one role:name"),
    ("x,credit,filers,tax:rent,,100.00,,,,,,", "base filers reads no tag"),
    ("x,credit,filers,,,,,,,,,", "base filers needs the amount each filer gets"),
    ("x,credit,filers,,50%,100.00,,,,,,", "base filers takes no rate"),
    ("tax,credit,filers,,,100.00,,,,,,", "name 'tax' is a line the state return already has"),
    ("Rent Credit,credit,filers,,,100.00,,,,,,", "name must be lower case"),
    ("x,credit,filers,,,100.00,,,,1000.00,500.00,1%", "give phaseout or phaseout_rate, not both"),
    ("x,credit,filers,,,100.00,,,,,1000.00,", "a phase-out needs the income_limit"),
    ("x,credit,filers,,,100.00,,old,,,,", "min_age 'old' must be an age"),
    ("x,credit,filers,,,100.00,,,,1000.00,,0%", "phaseout_rate must be more than 0%"),
    ("tax_due,credit,filers,,,100.00,,,,,,", "name 'tax_due' is a line the state return already has"),
    ("x,credit,filers,,,100.00,,,HOH,,,", "filing_status 'HOH' must be blank"),
    ("x,deduction,filers,,,100.00,,,,,,,,yes", "refundable is for a credit; step is 'deduction'"),
    ("x,credit,filers,,,100.00,,,,,,,,maybe", "refundable 'maybe' must be yes or blank"),
    ("x,subtraction,spending,tax:rent,,100.00,,,,,,,yes,", "less_ss is for base income"),
    ("x,subtraction,income,tax:pension,,,,,,,,,yes,", "less_ss needs an amount"),
])
def test_the_loader_checks_a_state_adjustments_row(
    add_state_tax: Callable[..., None], write_aux: Callable[[str, str], Path],
    load_test_copy: Callable[[], LoadResult], row: str, message: str,
) -> None:
    add_state_tax()
    write_aux("state_adjustments__CA.csv", HEADER + f"2026-01-31,{row}\n")
    with pytest.raises(LoadError, match=message):
        load_test_copy()


def test_two_rows_for_one_line_and_status_are_refused(
    add_state_tax: Callable[..., None], write_aux: Callable[[str, str], Path],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    add_state_tax()
    write_aux("state_adjustments__CA.csv", HEADER + "2026-01-31,x,credit,filers,,,1.00,,,MFJ,,,\n" * 2)
    with pytest.raises(LoadError, match="two rows for the same line and filing status"):
        load_test_copy()


# -- Refundable credits and the Social Security offset (#579) ----------------

PENSION = Account("Income_Pension_Alice", "income", "pension", "alice", tags=frozenset({"tax:pension"}))
BILLS_PENSION = Account("Income_Pension_Bill", "income", "pension", "bill",
                        tags=frozenset({"tax:pension"}))
JOINT_PENSION = Account("Income_Pension_Joint", "income", "pension", "joint",
                        tags=frozenset({"tax:pension"}))
CHECKING = Account("Asset_Checking", "asset", "cash", "joint")
SS_ALICE = Account("Income_SS_Alice", "income", "ss_benefit", "alice")
SS_BILL = Account("Income_SS_Bill", "income", "ss_benefit", "bill")


def _year(rows: list[dict], items: list[IncomeItem], status: str = "MFJ") -> TaxYear:
    """A 2030 tax year with only what ``state_adjustments`` reads: the
    rows (in stored units), the items, and Alice (born 1964) and Bill
    (born 1967)."""
    def unused(*_args: object) -> list:
        return []

    return TaxYear(
        year=2030, jurisdiction="state", params=None, items=items, losses=[],
        persons=[Person("alice", "1964-03-15", "primary"), Person("bill", "1967-08-20", "primary")],
        _table=unused, _indexed=lambda _kind, _series: rows, _dated=unused,
        _keyed=lambda *_a: None, _filing_status_in=lambda _y: status, _prior=lambda _line: 0,
        _series_of=unused,
    )


def _pension_row(**columns: object) -> dict:
    return {"name": "pension_exclusion", "step": "subtraction", "base": "income",
            "tag": "tax:pension", "amount": 40_000_00, "less_ss": "yes", **columns}


def _income(payer: Account, amount: int, character: str = "ordinary") -> IncomeItem:
    return IncomeItem(2030, character, payer, CHECKING, amount)


def test_less_ss_per_person_takes_each_filers_own_benefits_off_their_own_cap() -> None:
    """Maryland's shape: each filer's pension, up to the cap less their own
    Social Security; one spouse's room can't cover the other's pension."""
    items = [_income(PENSION, 50_000_00), _income(BILLS_PENSION, 10_000_00),
             _income(SS_ALICE, 30_000_00, "ss_benefit"), _income(SS_BILL, 5_000_00, "ss_benefit")]
    (line,) = state_adjustments(_year([_pension_row(per_person="yes")], items), "x", "MD")
    # Alice: $40,000 - $30,000; Bill: all $10,000 (his cap is $35,000).
    assert line.figure == 10_000_00 + 10_000_00
    # A joint pension is split evenly between them.
    joint = [_income(JOINT_PENSION, 20_000_00), _income(SS_ALICE, 35_000_00, "ss_benefit")]
    (line,) = state_adjustments(_year([_pension_row(per_person="yes")], joint), "x", "MD")
    assert line.figure == 5_000_00 + 10_000_00


def test_less_ss_per_person_counts_only_filers_of_age() -> None:
    items = [_income(PENSION, 50_000_00), _income(BILLS_PENSION, 10_000_00)]
    # Alice is 66 at the end of 2030, Bill 63.
    (line,) = state_adjustments(_year([_pension_row(per_person="yes", min_age=65)], items), "x", "MD")
    assert line.figure == 40_000_00


def test_less_ss_without_per_person_takes_the_households_benefits_off_one_cap() -> None:
    """Idaho's shape: one cap for the return, less both spouses' benefits."""
    items = [_income(PENSION, 50_000_00), _income(BILLS_PENSION, 10_000_00),
             _income(SS_ALICE, 30_000_00, "ss_benefit"), _income(SS_BILL, 5_000_00, "ss_benefit")]
    (line,) = state_adjustments(_year([_pension_row(amount=60_000_00)], items), "x", "ID")
    assert line.figure == 60_000_00 - 35_000_00
    # Benefits past the cap leave nothing.
    (line,) = state_adjustments(_year([_pension_row(amount=30_000_00)], items), "x", "ID")
    assert line.figure == 0


def test_a_credit_row_says_whether_it_is_refundable() -> None:
    rows = [{"name": "grocery_credit", "step": "credit", "base": "filers", "amount": 155_00,
             "refundable": "yes"},
            {"name": "other_credit", "step": "credit", "base": "filers", "amount": 10_00}]
    grocery, other = state_adjustments(_year(rows, []), "x", "ID")
    assert grocery.refundable and grocery.figure == 310_00
    assert not other.refundable


@pytest.mark.slow
def test_a_refundable_credit_is_paid_past_the_tax(
    add_state_tax: Callable[..., None], write_aux: Callable[[str, str], Path],
    run_test_copy: Callable[[], Path],
) -> None:
    """Idaho's grocery credit, say: paid in full, so a year with little
    state tax ends with the state paying out; a nonrefundable credit of the
    same size takes only the tax left."""
    add_state_tax()
    write_aux("state_adjustments__CA.csv", HEADER + "\n".join([
        "2026-01-31,big_credit,credit,filers,,,50000.00,,,,,,,,yes",
        "2026-01-31,small_credit,credit,filers,,,50000.00,,,,,,,,",
    ]) + "\n")
    run = run_test_copy()
    lines = _lines(run, "state_big_credit", "state_small_credit", "state_credits", "state_tax")
    big, small, credits, tax = (lines["state_big_credit"], lines["state_small_credit"],
                                lines["state_credits"], lines["state_tax"])
    for year in tax:
        assert big[year] == 100_000_00
        # The nonrefundable credit took all the tax, the refundable one
        # comes off after it and isn't among the credits taken.
        assert small[year] == credits[year]
        assert tax[year] == -100_000_00
