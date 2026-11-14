"""Married couples filing separately on one state return (#580): where the
state's ``state_separate_return`` table allows it, each spouse's income runs
through the married-filing-separately brackets and standard deduction
alone, and the couple pays the lower of that and the joint tax."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable, Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from analysis.sim import registries
from analysis.sim.engine import TaxRule, _prepare_tax
from analysis.sim.connection import RunConnection
from analysis.sim.loader import LoadError, LoadResult
from analysis.sim.reports import tax_return
from analysis.sim.tax_api import Extra, Return
from analysis.sim.taxes.state_base import Basis, Income
from analysis.sim.taxes.state_tables import StateTablesYear

Aux = dict[tuple[str, str | None], list[tuple[str, dict[str, Any]]]]

YEAR = 2026
"""The ledger's tax year: these tests build their own ledger and tables,
so no scenario's dates move it."""
DATED = f"{YEAR}-01-31"
STATE = "DE"

CHECKING_A, CHECKING_B, SALARY = 1, 2, 3


@pytest.fixture
def ledger(ledger_con: sqlite3.Connection,
           set_residence: Callable[..., None]) -> sqlite3.Connection:
    """Alice (65 this year) and Bill (60), each with a checking account
    their pay goes into, living in Delaware."""
    set_residence(ledger_con, (DATED, STATE, None))
    ledger_con.executemany(
        "INSERT INTO person(id, display, birthdate, role) VALUES (?, ?, ?, ?)",
        [("alice", "Alice", f"{YEAR - 65}-03-01", "primary"),
         ("bill", "Bill", f"{YEAR - 60}-07-01", "secondary")])
    ledger_con.executemany(
        "INSERT INTO account(id, name, kind, subkind, owner) VALUES (?, ?, ?, ?, ?)",
        [(CHECKING_A, "Asset_Checking_Alice", "asset", "cash", "alice"),
         (CHECKING_B, "Asset_Checking_Bill", "asset", "cash", "bill"),
         (SALARY, "Income_Salary", "income", "salary", None)])
    return ledger_con


def _pay(con: sqlite3.Connection, post: Callable[..., None], alice: int, bill: int) -> None:
    if alice:
        post(con, YEAR, SALARY, CHECKING_A, alice, "ordinary")
    if bill:
        post(con, YEAR, SALARY, CHECKING_B, bill, "ordinary")


def _rates(*bands: tuple[int, int], statuses: tuple[str, ...] = ("MFJ", "MFS", "Single"),
           ) -> list[tuple[str, dict[str, Any]]]:
    return [(DATED, {"filing_status": s, "floor": floor, "marginal_rate_micros": rate})
            for s in statuses for floor, rate in bands]


def _amounts(**by_status: int) -> list[tuple[str, dict[str, Any]]]:
    return [(DATED, {"filing_status": s, "amount": amount}) for s, amount in by_status.items()]


def _aux(*, allowed: str | None = "yes", status: str = "MFJ",
         brackets: list[tuple[str, dict[str, Any]]] | None = None,
         standard: list[tuple[str, dict[str, Any]]] | None = None) -> Aux:
    """A federal 10% flat tax, and a state whose brackets are the same for
    every filing status (2% to $20,000, 6% above, Delaware's shape) and
    whose married-filing-separately deduction is half the joint one."""
    aux: Aux = {
        ("filing_status", None): [(DATED, {"filing_status": status})],
        ("federal_brackets", None): _rates((0, 100_000)),
        ("standard_deduction", None): _amounts(MFJ=10_000_00, MFS=5_000_00, Single=5_000_00),
        ("capital_loss_limit", None): _amounts(MFJ=3_000_00, MFS=1_500_00, Single=3_000_00),
        ("state_brackets", STATE): brackets or _rates((0, 20_000), (20_000_00, 60_000)),
        (registries.STATE_STANDARD_DEDUCTION, STATE): standard or _amounts(
            MFJ=6_500_00, MFS=3_250_00, Single=3_250_00),
    }
    if allowed is not None:
        aux[(registries.STATE_SEPARATE_RETURN, STATE)] = [(DATED, {"allowed": allowed})]
    return aux


def _prepared(con: sqlite3.Connection, aux: Aux) -> Any:
    return _prepare_tax(
        con, {"scope": "state"}, aux=aux, tax_year=YEAR, tid=1, kind="state_tables",
        jurisdictions={"federal": TaxRule("federal", {"scope": "federal"}, 1)})


def _state(con: sqlite3.Connection, aux: Aux, more: int = 0) -> Return:
    return _prepared(con, aux).compute(Extra(more))


def _tax(taxable: int) -> int:
    """The test state's tax on ``taxable`` cents."""
    return min(taxable, 20_000_00) * 2 // 100 + max(0, taxable - 20_000_00) * 6 // 100


def test_two_incomes_pay_less_filing_separately(
    ledger: sqlite3.Connection, post_transfer: Callable[..., None],
) -> None:
    """Each spouse runs through the 2% band, which a joint return gets once."""
    _pay(ledger, post_transfer, 60_000_00, 40_000_00)
    joint = _tax(100_000_00 - 6_500_00)
    alice, bill = _tax(60_000_00 - 3_250_00), _tax(40_000_00 - 3_250_00)
    state = _state(ledger, _aux())
    assert state["joint_tax"] == joint
    assert (state["separate_tax_alice"], state["separate_tax_bill"]) == (alice, bill)
    assert (state["separate_taxable_income_alice"], state["separate_agi_bill"]) == (
        56_750_00, 40_000_00)
    assert state["separate_tax"] == alice + bill == joint - 800_00
    assert state["separate_return"] == 1
    assert state.tax == alice + bill
    # The joint return's own lines stay as they were.
    assert state["taxable_income"] == 93_500_00


def test_without_the_table_a_couple_files_jointly(
    ledger: sqlite3.Connection, post_transfer: Callable[..., None],
) -> None:
    _pay(ledger, post_transfer, 60_000_00, 40_000_00)
    for aux in (_aux(allowed=None), _aux(allowed="no")):
        state = _state(ledger, aux)
        assert state.tax == _tax(93_500_00)
        assert "separate_tax" not in state.lines


def test_one_income_files_jointly_when_that_costs_less(
    ledger: sqlite3.Connection, post_transfer: Callable[..., None],
) -> None:
    """All the income is Alice's: alone she'd take only half the deduction."""
    _pay(ledger, post_transfer, 100_000_00, 0)
    state = _state(ledger, _aux())
    assert state["separate_tax"] == _tax(100_000_00 - 3_250_00)
    assert state["separate_return"] == 0
    assert state.tax == state["joint_tax"] == _tax(100_000_00 - 6_500_00)


def test_a_deduction_not_doubled_for_a_joint_return_is_taken_twice(
    ledger: sqlite3.Connection, post_transfer: Callable[..., None],
) -> None:
    """Kentucky's shape: a flat 4%, and one $3,270 deduction per return."""
    _pay(ledger, post_transfer, 30_000_00, 20_000_00)
    aux = _aux(brackets=_rates((0, 40_000)),
               standard=_amounts(MFJ=3_270_00, MFS=3_270_00, Single=3_270_00))
    state = _state(ledger, aux)
    assert state["joint_tax"] - state.tax == 3_270_00 * 4 // 100


def test_a_single_year_never_splits(
    ledger: sqlite3.Connection, post_transfer: Callable[..., None],
) -> None:
    _pay(ledger, post_transfer, 60_000_00, 40_000_00)
    state = _state(ledger, _aux(status="Single"))
    assert "separate_tax" not in state.lines
    assert state.tax == _tax(100_000_00 - 3_250_00)


def test_more_income_moves_the_separate_tax(
    ledger: sqlite3.Connection, post_transfer: Callable[..., None],
) -> None:
    """A what-if's extra income divides as the ordinary income does, so
    the drawdown and fill_bracket price the return the couple would file."""
    _pay(ledger, post_transfer, 60_000_00, 40_000_00)
    before, after = _state(ledger, _aux()), _state(ledger, _aux(), more=10_000_00)
    assert (after["separate_agi_alice"] - before["separate_agi_alice"],
            after["separate_agi_bill"] - before["separate_agi_bill"]) == (6_000_00, 4_000_00)
    assert after.tax - before.tax == 600_00


def test_a_credit_for_each_filer_goes_to_the_one_old_enough(
    ledger: sqlite3.Connection, post_transfer: Callable[..., None],
) -> None:
    """A $100 credit from 65 is Alice's alone."""
    _pay(ledger, post_transfer, 60_000_00, 40_000_00)
    aux = _aux()
    aux[(registries.STATE_ADJUSTMENTS, STATE)] = [(DATED, {
        "name": "senior_credit", "step": "credit", "base": "filers", "amount": 100_00,
        "min_age": 65})]
    state = _state(ledger, aux)
    assert state["senior_credit"] == 100_00
    assert state["separate_tax_alice"] == _tax(56_750_00) - 100_00
    assert state["separate_tax_bill"] == _tax(36_750_00)
    assert state.tax == state["separate_tax"] == state["joint_tax"] - 800_00


def test_a_retirement_exclusion_comes_off_its_own_spouse(
    ledger: sqlite3.Connection, post_transfer: Callable[..., None],
) -> None:
    """Alice's $10,000 exclusion from 65 lowers her AGI, not Bill's."""
    _pay(ledger, post_transfer, 60_000_00, 40_000_00)
    aux = _aux()
    aux[(registries.STATE_RETIREMENT_EXCLUSION, STATE)] = [(DATED, {
        "name": "age", "income": "all", "min_age": 65, "amount": 10_000_00})]
    state = _state(ledger, aux)
    assert state["retirement_exclusion"] == 10_000_00
    assert (state["separate_agi_alice"], state["separate_agi_bill"]) == (50_000_00, 40_000_00)


def test_the_table_is_read_where_the_household_lives(
    ledger: sqlite3.Connection, post_transfer: Callable[..., None],
    set_residence: Callable[..., None],
) -> None:
    """Another state's table doesn't let a Delaware couple split."""
    _pay(ledger, post_transfer, 60_000_00, 40_000_00)
    aux = _aux(allowed=None)
    aux[(registries.STATE_SEPARATE_RETURN, "KY")] = [(DATED, {"allowed": "yes"})]
    assert "separate_tax" not in _state(ledger, aux).lines


class _OwnTax(StateTablesYear):
    """A household's state year with its own tax step."""

    def tax(self, taxable: int, income: Income) -> int:
        return super().tax(taxable, income)


def test_a_household_tax_step_written_before_the_step_files_jointly(
    ledger: sqlite3.Connection, post_transfer: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A module written for API 5 or before, or saying none, overriding
    tax(): separate returns read the tables its tax() may replace."""
    _pay(ledger, post_transfer, 60_000_00, 40_000_00)
    prepared = _prepared(ledger, _aux())
    monkeypatch.setattr(sys.modules[__name__], "API_VERSION", 5, raising=False)
    assert "separate_tax" not in _OwnTax(prepared.calc, prepared.ty).compute().lines
    monkeypatch.delattr(sys.modules[__name__], "API_VERSION")
    assert "separate_tax" not in _OwnTax(prepared.calc, prepared.ty).compute().lines
    monkeypatch.setattr(sys.modules[__name__], "API_VERSION", 6, raising=False)
    assert _OwnTax(prepared.calc, prepared.ty).compute()["separate_return"] == 1


def test_the_loader_refuses_an_allowed_other_than_yes_or_no(
    add_state_tax: Callable[..., None], write_aux: Callable[[str, str], Path],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    add_state_tax()
    write_aux("state_separate_return__CA.csv", "effective_date,allowed\n2026-01-31,maybe\n")
    with pytest.raises(LoadError, match="allowed 'maybe' must be yes or no"):
        load_test_copy()


def test_the_district_ships_with_separate_returns_allowed(
    add_state_tax: Callable[..., None], write_aux: Callable[[str, str], Path],
    run_test_copy: Callable[[], Path],
) -> None:
    """retire_alice's two salaries in the District: the years with both pay
    less filing separately; a data table saying no turns it off. The years
    are the run's own, not literals."""
    add_state_tax("DC")

    def lines(run: Path, *buckets: str) -> list[dict[int, int]]:
        with closing(sqlite3.connect(run)) as con:
            return [dict(con.execute(
                "SELECT year, amount FROM tax_year_summary WHERE bucket = ?", (bucket,)))
                for bucket in buckets]

    first = run_test_copy()
    chosen, joint, separate = lines(
        first, "state_separate_return", "state_joint_tax", "state_separate_tax")
    assert chosen and any(chosen.values())
    for year, split in chosen.items():
        assert (separate[year] < joint[year]) == bool(split)
    # The report shows the choice as a flag, not dollars.
    with RunConnection(first) as rc:
        assert set(tax_return(rc, "state")["separate_return"]) == set(chosen.values())
    first.unlink()
    write_aux("state_separate_return__DC.csv", "effective_date,allowed\n2000-01-31,no\n")
    assert lines(run_test_copy(), "state_separate_return") == [{}]


def _kentucky() -> Aux:
    """Kentucky's shape: a flat 4%, one $3,270 deduction per return."""
    return _aux(brackets=_rates((0, 40_000)),
                standard=_amounts(MFJ=3_270_00, MFS=3_270_00, Single=3_270_00))


def test_a_carried_in_nol_offsets_each_spouse_in_turn(
    ledger: sqlite3.Connection, post_transfer: Callable[..., None],
) -> None:
    """$20,000 of NOL: Alice's small income uses what it can, Bill's the
    rest, so none is lost to the split and none carries on."""
    _pay(ledger, post_transfer, 5_000_00, 60_000_00)
    ledger.execute("INSERT INTO tax_year_summary(year, bucket, amount, computed_at) "
                   "VALUES (?, 'state_nol_carryover', ?, 'test')", (YEAR - 1, 20_000_00))
    state = _state(ledger, _kentucky())
    assert state["separate_return"] == 1
    assert state["separate_taxable_income_alice"] == 0
    assert state["separate_taxable_income_bill"] == 60_000_00 - 3_270_00 - 18_270_00
    assert state["nol_used"] == 20_000_00
    assert state.lines.get("nol_carryover", 0) == 0


def test_a_per_person_income_line_comes_off_its_own_spouse(
    ledger: sqlite3.Connection, post_transfer: Callable[..., None],
) -> None:
    """A deduction of each filer's own tagged income from 65 (#579) is
    Alice's pension, not half of it Bill's salary."""
    _pay(ledger, post_transfer, 0, 60_000_00)
    ledger.execute("INSERT INTO account(id, name, kind, subkind, owner) VALUES "
                   "(9, 'Income_Pension_Alice', 'income', 'pension', 'alice')")
    ledger.execute("INSERT INTO tag(id, role, name) VALUES (1, 'tax', 'pension')")
    ledger.execute("INSERT INTO account_tag(account_id, tag_id) VALUES (9, 1)")
    post_transfer(ledger, YEAR, 9, CHECKING_A, 20_000_00, "ordinary")
    aux = _aux()
    aux[(registries.STATE_ADJUSTMENTS, STATE)] = [(DATED, {
        "name": "pension_deduction", "step": "deduction", "base": "income",
        "tag": "tax:pension", "per_person": "yes", "min_age": 65})]
    state = _state(ledger, aux)
    assert state["pension_deduction"] == 20_000_00
    assert state["separate_taxable_income_alice"] == 0
    assert state["separate_taxable_income_bill"] == 60_000_00 - 3_250_00


def test_a_subtraction_for_each_filer_comes_off_the_one_old_enough(
    ledger: sqlite3.Connection, post_transfer: Callable[..., None],
) -> None:
    """A $5,000 subtraction from 65 is Alice's, though Bill earns more."""
    _pay(ledger, post_transfer, 20_000_00, 80_000_00)
    aux = _aux()
    aux[(registries.STATE_ADJUSTMENTS, STATE)] = [(DATED, {
        "name": "age_subtraction", "step": "subtraction", "base": "filers",
        "amount": 5_000_00, "min_age": 65})]
    state = _state(ledger, aux)
    assert (state["separate_agi_alice"], state["separate_agi_bill"]) == (15_000_00, 80_000_00)


def test_an_exclusion_of_gain_comes_off_the_gain_it_covered(
    ledger: sqlite3.Connection, post_transfer: Callable[..., None],
) -> None:
    """Alice's grant covers her $10,000 gain and Bill's his salary: her
    gain is excluded, not moved onto his other income."""
    ledger.execute("INSERT INTO account(id, name, kind, subkind, owner) VALUES "
                   "(8, 'Asset_Brokerage_Alice', 'asset', 'brokerage_taxable', 'alice')")
    ledger.execute("INSERT INTO account(id, name, kind, subkind) VALUES "
                   "(7, 'Income_Gains', 'income', 'capital_gain')")
    _pay(ledger, post_transfer, 30_000_00, 40_000_00)
    post_transfer(ledger, YEAR, 8, CHECKING_A, 10_000_00, "ltcg")
    aux = _aux()
    aux[(registries.STATE_RETIREMENT_EXCLUSION, STATE)] = [
        (DATED, {"name": "gains", "income": "capital_gain", "min_age": 65, "amount": 10_000_00}),
        (DATED, {"name": "pay", "income": "other", "min_age": 60, "amount": 10_000_00}),
    ]
    state = _state(ledger, aux)
    assert state["retirement_exclusion"] == 30_000_00
    # Alice: $30,000 pay less $10,000 and her $10,000 gain wholly left out;
    # Bill: $40,000 less $10,000.
    assert (state["separate_agi_alice"], state["separate_agi_bill"]) == (20_000_00, 30_000_00)


class _ExemptionsOnly(StateTablesYear):
    """A household's state year with exemptions and no standard deduction,
    a minimum tax of $100 a return, and a credit of 5% of the tax."""

    def deductions(self) -> Mapping[str, int]:
        return {"standard_deduction": 0, "itemized_deduction": 0, "deduction": 2_000_00}

    def tax(self, taxable: int, income: Income) -> int:
        return max(100_00, super().tax(taxable, income))

    def credits(self, tax: int, income: Income, basis: Basis) -> int:
        return tax * 5 // 100


def test_a_household_years_own_steps_apply_to_each_spouse(
    ledger: sqlite3.Connection, post_transfer: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Written for API 6: no standard deduction for either spouse, the
    minimum tax on each return, and the credit as the joint return figured
    it, at its own tax."""
    monkeypatch.setattr(sys.modules[__name__], "API_VERSION", 6, raising=False)
    _pay(ledger, post_transfer, 60_000_00, 2_000_00)
    prepared = _prepared(ledger, _aux())
    state = _ExemptionsOnly(prepared.calc, prepared.ty).compute()
    assert state["separate_deduction_alice"] == state["separate_deduction_bill"] == 1_000_00
    assert state["separate_taxable_income_bill"] == 1_000_00
    joint = _tax(62_000_00 - 2_000_00)
    credit = joint * 5 // 100
    assert state["separate_tax_bill"] == 100_00 - credit // 2
    assert state["separate_tax_alice"] == _tax(59_000_00) - (credit - credit // 2)


def test_the_loader_wants_mfs_rows_where_spouses_may_file_separately(
    add_state_tax: Callable[..., None], write_aux: Callable[[str, str], Path],
    test_data_dir: Path, load_test_copy: Callable[[], LoadResult],
) -> None:
    add_state_tax()
    write_aux("state_separate_return__CA.csv", "effective_date,allowed\n2026-01-31,yes\n")
    brackets = test_data_dir / "aux" / "state_brackets__CA.csv"
    rows = brackets.read_text().splitlines(keepends=True)
    brackets.write_text("".join(r for r in rows if ",MFS," not in r))
    with pytest.raises(LoadError, match=r"aux/state_brackets__CA.csv \(2026-01-31\): no MFS rows"):
        load_test_copy()
