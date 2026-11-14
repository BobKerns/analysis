"""An age test on a state's Social Security exclusion (#581): rows of
``state_social_security__<state>.csv`` by ``min_age`` are age tiers, and
each person's share of the federally taxable amount, by their benefits,
takes the highest row they reach by December 31."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from analysis.sim import registries
from analysis.sim.engine import TaxRule, _prepare_tax
from analysis.sim.loader import LoadError, LoadResult
from analysis.sim.tax_api import Return
from analysis.sim.taxes import common

Aux = dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]]

YEAR = 2026
"""The ledger's tax year: these tests build their own ledger and tables."""
DATED = f"{YEAR}-01-31"
STATE = "CO"

CHECKING_A, CHECKING_B, SALARY, SS_ALICE, SS_BILL = 1, 2, 3, 4, 5


@pytest.fixture
def ledger(ledger_con: sqlite3.Connection,
           set_residence: Callable[..., None]) -> sqlite3.Connection:
    """Alice (65 this year) and Bill (60), each with a checking account
    and their own Social Security."""
    set_residence(ledger_con, (DATED, STATE, None))
    ledger_con.executemany(
        "INSERT INTO person(id, display, birthdate, role) VALUES (?, ?, ?, ?)",
        [("alice", "Alice", f"{YEAR - 65}-03-01", "primary"),
         ("bill", "Bill", f"{YEAR - 60}-07-01", "secondary")])
    ledger_con.executemany(
        "INSERT INTO account(id, name, kind, subkind, owner) VALUES (?, ?, ?, ?, ?)",
        [(CHECKING_A, "Asset_Checking_Alice", "asset", "cash", "alice"),
         (CHECKING_B, "Asset_Checking_Bill", "asset", "cash", "bill"),
         (SALARY, "Income_Pension", "income", "pension", None),
         (SS_ALICE, "Income_SS_Alice", "income", "ss_benefit", "alice"),
         (SS_BILL, "Income_SS_Bill", "income", "ss_benefit", "bill")])
    return ledger_con


def _income(con: sqlite3.Connection, post: Callable[..., None], *, other: int,
            alice_ss: int = 30_000_00, bill_ss: int = 20_000_00) -> None:
    post(con, YEAR, SALARY, CHECKING_A, other, "ordinary")
    post(con, YEAR, SS_ALICE, CHECKING_A, alice_ss, "ss_benefit")
    post(con, YEAR, SS_BILL, CHECKING_B, bill_ss, "ss_benefit")


def _ss_row(min_age: str | None, *, cliff: int | None = None,
            status: str = "MFJ") -> tuple[str, dict[str, Any]]:
    return (DATED, {"filing_status": status, "min_age": min_age,
                    "exclusion_micros": 1_000_000,
                    "phaseout_start": cliff, "phaseout_end": cliff})


COLORADO = [_ss_row("55", cliff=95_000_00), _ss_row("65")]
"""Colorado's shape: all of it from 65; from 55 only below an income."""


def _aux(ss_rows: list[tuple[str, dict[str, Any]]], *, separate: bool = False) -> Aux:
    """A federal 10% flat tax and a 5% flat state with no deductions."""
    statuses = ("MFJ", "MFS", "Single")

    def flat(rate: int) -> list[tuple[str, dict[str, Any]]]:
        return [(DATED, {"filing_status": s, "floor": 0, "marginal_rate_micros": rate})
                for s in statuses]

    def amounts(amount: int) -> list[tuple[str, dict[str, Any]]]:
        return [(DATED, {"filing_status": s, "amount": amount}) for s in statuses]

    aux: Aux = {
        ("filing_status", None): [(DATED, {"filing_status": "MFJ"})],
        ("federal_brackets", None): flat(100_000),
        ("standard_deduction", None): amounts(0),
        ("capital_loss_limit", None): amounts(3_000_00),
        ("ss_taxation_thresholds", None): [
            (DATED, {"filing_status": "MFJ", "tier1_floor": 32_000_00,
                     "tier2_floor": 44_000_00})],
        ("state_brackets", STATE): flat(50_000),
        (registries.STATE_STANDARD_DEDUCTION, STATE): amounts(0),
        (registries.STATE_SOCIAL_SECURITY, STATE): ss_rows,
    }
    if separate:
        aux[(registries.STATE_SEPARATE_RETURN, STATE)] = [(DATED, {"allowed": "yes"})]
    return aux


def _returns(con: sqlite3.Connection, aux: Aux) -> tuple[Return, Return]:
    federal_rule = TaxRule("federal", {"scope": "federal"}, 1)
    federal = _prepare_tax(con, {"scope": "federal"}, aux=aux, tax_year=YEAR, tid=1,
                           kind="federal").compute()
    state = _prepare_tax(con, {"scope": "state"}, aux=aux, tax_year=YEAR, tid=2,
                         kind="state_tables", jurisdictions={"federal": federal_rule}).compute()
    return federal, state


def _shares(taxable: int) -> dict[str, int]:
    """The federally taxable amount by benefits: Alice 30,000, Bill 20,000."""
    return common.apportion(taxable, {"alice": 30_000_00, "bill": 20_000_00})


def test_below_the_income_both_ages_leave_it_all_out(
    ledger: sqlite3.Connection, post_transfer: Callable[..., None],
) -> None:
    _income(ledger, post_transfer, other=20_000_00)
    federal, state = _returns(ledger, _aux(COLORADO))
    assert federal.agi < 95_000_00
    assert federal["ss_taxable"] > 0
    assert state["ss_taxable"] == 0
    assert state["ss_difference"] == federal["ss_taxable"]


def test_over_the_income_only_the_younger_spouses_share_is_taxed(
    ledger: sqlite3.Connection, post_transfer: Callable[..., None],
) -> None:
    """Bill, 60, is on the 55 row, which the income takes away; Alice, 65,
    keeps hers."""
    _income(ledger, post_transfer, other=60_000_00)
    federal, state = _returns(ledger, _aux(COLORADO))
    assert federal.agi > 95_000_00
    assert state["ss_taxable"] == _shares(federal["ss_taxable"])["bill"]


def test_someone_under_every_row_has_none_left_out(
    ledger: sqlite3.Connection, post_transfer: Callable[..., None],
) -> None:
    _income(ledger, post_transfer, other=20_000_00)
    federal, state = _returns(ledger, _aux([_ss_row("65")]))
    assert state["ss_taxable"] == _shares(federal["ss_taxable"])["bill"]


def test_a_row_without_min_age_reaches_everyone(
    ledger: sqlite3.Connection, post_transfer: Callable[..., None],
) -> None:
    """A blank min_age among age rows is any age: Bill gets half, Alice all."""
    _income(ledger, post_transfer, other=20_000_00)
    half = (DATED, {"filing_status": "MFJ", "min_age": None, "exclusion_micros": 500_000})
    federal, state = _returns(ledger, _aux([half, _ss_row("65")]))
    bill = _shares(federal["ss_taxable"])["bill"]
    assert state["ss_taxable"] == bill - bill // 2


def test_without_min_age_the_household_is_phased_as_before(
    ledger: sqlite3.Connection, post_transfer: Callable[..., None],
) -> None:
    _income(ledger, post_transfer, other=60_000_00)
    row = (DATED, {"filing_status": "MFJ", "exclusion_micros": 1_000_000,
                   "phaseout_start": 90_000_00, "phaseout_end": 120_000_00})
    federal, state = _returns(ledger, _aux([row]))
    excluded = common.state_ss_exclusion(row[1], federal["ss_taxable"], federal.agi)
    assert 0 < excluded < federal["ss_taxable"]
    assert state["ss_taxable"] == federal["ss_taxable"] - excluded


def test_spouses_filing_separately_each_keep_their_own_taxable_share(
    ledger: sqlite3.Connection, post_transfer: Callable[..., None],
) -> None:
    """With rows by age, Bill's separate return carries all of the state's
    taxable Social Security, Alice's none (#580)."""
    _income(ledger, post_transfer, other=60_000_00)
    federal, state = _returns(ledger, _aux(COLORADO, separate=True))
    bill_ss = _shares(federal["ss_taxable"])["bill"]
    pension = common.apportion(60_000_00, {"alice": 1, "bill": 0})
    assert state["separate_agi_bill"] == pension["bill"] + bill_ss
    assert state["separate_agi_alice"] == pension["alice"]


@pytest.mark.parametrize(("born", "years", "months"), [
    (date(1937, 6, 1), 65, 0),
    (date(1940, 6, 1), 65, 6),
    (date(1950, 6, 1), 66, 0),
    (date(1955, 6, 1), 66, 2),
    (date(1959, 6, 1), 66, 10),
    (date(1960, 1, 1), 66, 10),  # January 1 counts with the year before
    (date(1960, 1, 2), 67, 0),
    (date(1975, 6, 1), 67, 0),
])
def test_full_retirement_age_follows_the_birth_year(born: date, years: int, months: int) -> None:
    assert common.full_retirement_age_months(born) == years * 12 + months


def test_fra_is_each_persons_own() -> None:
    """Born December 1959, full retirement age is 66 and 10 months: not
    reached at the end of 2025 (66 and 0), reached at the end of 2026."""
    rows = [{"min_age": "fra", "exclusion_micros": 1_000_000}]
    born = date(1959, 12, 1)
    assert common.state_ss_row(rows, born, 2025) is None
    assert common.state_ss_row(rows, born, 2026) is rows[0]
    assert common.state_ss_row(rows, None, 2026) is None


def test_fra_rows_tax_rhode_islands_way(
    ledger: sqlite3.Connection, post_transfer: Callable[..., None],
) -> None:
    """Alice, 65 and born in 1961, hasn't reached her full retirement age
    (67), so all of the taxable amount stays in."""
    _income(ledger, post_transfer, other=20_000_00)
    federal, state = _returns(ledger, _aux([_ss_row("fra", cliff=133_500_00)]))
    assert state["ss_taxable"] == federal["ss_taxable"]


SS_HEADER = "effective_date,filing_status,min_age,exclusion_percent,phaseout_start,phaseout_end\n"


@pytest.mark.parametrize(("rows", "message"), [
    ("2020-01-31,MFJ,old,100%,,\n", "min_age 'old' must be an age in years, such as 59.5, or fra"),
    ("2020-01-31,MFJ,65,100%,,\n2020-01-31,MFJ,65,50%,,\n",
     "two rows for the same filing status and min_age"),
    ("2020-01-31,MFJ,fra,100%,,\n2020-01-31,MFJ,FRA,50%,,\n",
     "two rows for the same filing status and min_age"),
    ("2020-01-31,MFJ,,100%,,\n2020-01-31,MFJ,,50%,,\n",
     "two rows for the same filing status and min_age"),
])
def test_min_age_is_checked_at_load(
    write_aux: Callable[[str, str], Path], load_test_copy: Callable[[], LoadResult],
    rows: str, message: str,
) -> None:
    write_aux("state_social_security__CA.csv", SS_HEADER + rows)
    with pytest.raises(LoadError, match=message):
        load_test_copy()


def test_age_rows_load(
    write_aux: Callable[[str, str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    write_aux("state_social_security__CA.csv", SS_HEADER
              + "2020-01-31,MFJ,55,100%,95000,95000\n2020-01-31,MFJ,65,100%,,\n"
              + "2020-01-31,Single,fra,100%,107000,107000\n")
    load_test_copy()
