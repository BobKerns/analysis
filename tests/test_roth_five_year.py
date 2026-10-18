"""Roth distributions and the five-year clock (#217): basis always comes out
tax-free; earnings are ordinary income until the distribution is qualified
(the owner 59½ and five tax years since their first Roth year). A chain skips
a Roth step that would pay taxed earnings unless no later step can pay."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pandas as pd
import pytest

from analysis.sim import engine
from analysis.sim.engine import RothLot, run_scenario
from analysis.sim.loader import LoadError, LoadResult

ROTH = "Asset_Roth_Alice"  # Alice was born 1965-03-15, so 59½ from 2024-09-15


@pytest.mark.parametrize(("on", "qualified"), [
    (date(2028, 12, 31), False),  # the clock started 2024-01-01 runs out 2029-01-01
    (date(2029, 1, 1), True),
])
def test_the_clock_runs_five_tax_years_from_jan_1(on: date, qualified: bool) -> None:
    roth = RothLot(basis_first=True, clock_start=date(2024, 1, 1), birthdate=date(1950, 1, 1))
    assert engine._roth_qualified(roth, on) is qualified


@pytest.mark.parametrize(("on", "qualified"), [
    (date(2024, 9, 14), False),
    (date(2024, 9, 15), True),
])
def test_a_qualified_distribution_also_needs_59_and_a_half(on: date, qualified: bool) -> None:
    roth = RothLot(basis_first=True, clock_start=None, birthdate=date(1965, 3, 15))
    assert engine._roth_qualified(roth, on) is qualified


@pytest.fixture
def young_roth(
    retire_alice_csv: Callable[[str], Path], update_account_value: Callable[..., None],
) -> Callable[[str], None]:
    """Alice's first Roth year becomes 2024 (so her clock runs out on
    2029-01-01), holding $100,000 of which ``basis`` is basis."""
    def _set(basis: str = "60000.00") -> None:
        path = retire_alice_csv("accounts.csv")
        accounts = pd.read_csv(path, dtype=str, keep_default_na=False)
        accounts.loc[accounts["name"] == ROTH, "opened"] = "2024-01-31"
        accounts.to_csv(path, index=False)
        update_account_value(ROTH, "default", opening_cost_basis=basis)
    return _set


def _run(load: Callable[[], LoadResult], runs_dir: Path) -> Path:
    return run_scenario(load().intent_path, runs_dir / "roth.sqlite3", run_by="T",
                        allow_dirty_data=True, allow_dirty_code=True)


def _legs(run: Path, account: str = ROTH) -> list[tuple[str, str, int, str]]:
    """Every leg out of ``account`` that isn't a growth or tax flow."""
    with sqlite3.connect(run) as con:
        return con.execute(
            "SELECT te.date, ta.name, te.amount, te.tax_character FROM transfer_event te "
            "JOIN account fa ON fa.id = te.from_account JOIN account ta ON ta.id = te.to_account "
            "WHERE fa.name = ? AND ta.kind = 'asset' ORDER BY te.id",
            (account,),
        ).fetchall()


def test_each_owner_has_one_clock_from_their_earliest_roth_year(
    young_roth: Callable[..., None], add_account_value: Callable[..., None],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    young_roth()
    # A conversion lot of 2026 doesn't restart the clock that began in 2024.
    add_account_value(ROTH, lot="conv_2026", acquired="2026-03-01", opening_balance="1000.00")
    with sqlite3.connect(load_test_copy().intent_path) as con:
        lots = engine._roth_lots(con, {"alice": date(1965, 3, 15), "bill": date(1967, 8, 20)})
        names = dict(con.execute(
            "SELECT m.id, a.name || ':' || m.lot FROM account_lot m JOIN account a ON a.id = m.account_id"
        ).fetchall())
    clocks = {names[lot]: roth.clock_start for lot, roth in lots.items()}
    assert clocks == {
        f"{ROTH}:default": date(2024, 1, 1), f"{ROTH}:conv_2026": date(2024, 1, 1),
        "Asset_Roth_Bill:default": date(2015, 1, 1),
    }


@pytest.mark.parametrize(("when", "legs"), [
    # Before the clock runs out: basis first, tax-free, then the rest as earnings.
    ("2027-01-31", [(6_000_000, "tax_free"), (1_000_000, "roth_nonqualified")]),
    ("2029-01-31", [(7_000_000, "tax_free")]),  # qualified: all of it tax-free
])
def test_a_roth_distribution_is_taxed_on_earnings_until_qualified(
    when: str, legs: list[tuple[int, str]], young_roth: Callable[..., None],
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    runs_dir: Path,
) -> None:
    young_roth()
    append_calendar_row(date=when, kind="one_off_transfer", account_1=ROTH, lot_1="default",
                        account_2="Asset_Checking", amount="70000.00")
    assert [(amount, character) for _d, _to, amount, character in _legs(_run(load_test_copy, runs_dir))] == legs


def test_nonqualified_earnings_are_ordinary_income(
    young_roth: Callable[..., None], append_calendar_row: Callable[..., None],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    young_roth()
    append_calendar_row(date="2027-01-31", kind="one_off_transfer", account_1=ROTH, lot_1="default",
                        account_2="Asset_Checking", amount="70000.00")
    run = _run(load_test_copy, runs_dir)
    with sqlite3.connect(run) as con:
        ordinary, _ss = engine._tax_year_income_buckets(con, tax_year=2027)
        con.execute("DELETE FROM transfer_event WHERE tax_character = 'roth_nonqualified'")
        without, _ss = engine._tax_year_income_buckets(con, tax_year=2027)
    assert ordinary - without == 1_000_000


def _roth_chain(retire_alice_csv: Callable[[str], Path], *steps: str) -> None:
    (retire_alice_csv("chains/source") / "spend.csv").write_text(
        "account_name,lot,maximum,percent,series,cadence\n" + "".join(f"{s}\n" for s in steps))


def test_a_roth_step_that_would_pay_taxed_earnings_is_skipped(
    young_roth: Callable[..., None], retire_alice_csv: Callable[[str], Path],
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    runs_dir: Path,
) -> None:
    young_roth("0.00")  # all earnings, so any draw before 2029 is taxed
    _roth_chain(retire_alice_csv, f"{ROTH},default,,,,", "Asset_Savings,,,,,")
    append_calendar_row(date="2027-01-31", kind="one_off_transfer", account_1="spend",
                        account_2="Asset_Checking", amount="15000.00")
    run = _run(load_test_copy, runs_dir)
    assert _legs(run) == []
    assert ("2027-01-31", "Asset_Checking", 1_500_000, "none") in _legs(run, "Asset_Savings")


def test_a_roth_step_pays_tax_free_basis_even_before_the_clock_runs(
    young_roth: Callable[..., None], retire_alice_csv: Callable[[str], Path],
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    runs_dir: Path,
) -> None:
    young_roth("60000.00")
    _roth_chain(retire_alice_csv, f"{ROTH},default,,,,", "Asset_Savings,,,,,")
    append_calendar_row(date="2027-01-31", kind="one_off_transfer", account_1="spend",
                        account_2="Asset_Checking", amount="15000.00")
    assert _legs(_run(load_test_copy, runs_dir)) == [("2027-01-31", "Asset_Checking", 1_500_000, "tax_free")]


def test_a_roth_last_step_pays_and_its_earnings_are_taxed(
    young_roth: Callable[..., None], retire_alice_csv: Callable[[str], Path],
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    runs_dir: Path,
) -> None:
    young_roth("0.00")
    _roth_chain(retire_alice_csv, "Asset_Savings,,5000.00,,,", f"{ROTH},default,,,,")
    append_calendar_row(date="2027-01-31", kind="one_off_transfer", account_1="spend",
                        account_2="Asset_Checking", amount="15000.00")
    assert _legs(_run(load_test_copy, runs_dir)) == [
        ("2027-01-31", "Asset_Checking", 1_000_000, "roth_nonqualified"),
    ]


def test_a_roth_rollover_is_not_a_distribution(
    young_roth: Callable[..., None], add_account_value: Callable[..., None],
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    runs_dir: Path,
) -> None:
    young_roth("0.00")
    add_account_value(ROTH, lot="b", opening_balance="0.00")
    append_calendar_row(date="2027-01-31", kind="one_off_transfer", account_1=ROTH, lot_1="default",
                        account_2=ROTH, lot_2="b", amount="15000.00")
    assert _legs(_run(load_test_copy, runs_dir)) == [("2027-01-31", ROTH, 1_500_000, "none")]


def test_a_roth_distribution_through_a_chain_is_taxed_on_earnings(
    young_roth: Callable[..., None], retire_alice_csv: Callable[[str], Path],
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    runs_dir: Path,
) -> None:
    young_roth("0.00")
    (retire_alice_csv("chains/destination") / "out.csv").write_text(
        "account_name,lot,maximum,percent,series,cadence\nAsset_Checking,,,,,\n")
    append_calendar_row(date="2027-01-31", kind="one_off_transfer", account_1=ROTH, lot_1="default",
                        account_2="out", amount="10000.00")
    assert _legs(_run(load_test_copy, runs_dir)) == [
        ("2027-01-31", "Asset_Checking", 1_000_000, "roth_nonqualified"),
    ]


def test_a_roth_step_inside_a_chain_with_later_steps_above_it_is_skipped(
    young_roth: Callable[..., None], retire_alice_csv: Callable[[str], Path],
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    runs_dir: Path,
) -> None:
    """The Roth step is its own chain's last, but the chain above still has
    a step that can pay, so the taxed draw is skipped."""
    young_roth("0.00")
    (retire_alice_csv("chains/source") / "roth_only.csv").write_text(
        f"account_name,lot,maximum,percent,series,cadence\n{ROTH},default,,,,\n")
    _roth_chain(retire_alice_csv, "roth_only,,,,,", "Asset_Savings,,,,,")
    append_calendar_row(date="2027-01-31", kind="one_off_transfer", account_1="spend",
                        account_2="Asset_Checking", amount="15000.00")
    run = _run(load_test_copy, runs_dir)
    assert _legs(run) == []
    assert ("2027-01-31", "Asset_Checking", 1_500_000, "none") in _legs(run, "Asset_Savings")


def test_a_roth_rollover_through_a_chain_is_not_a_distribution(
    young_roth: Callable[..., None], add_account_value: Callable[..., None],
    retire_alice_csv: Callable[[str], Path], append_calendar_row: Callable[..., None],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    young_roth("0.00")
    add_account_value(ROTH, lot="b", opening_balance="0.00")
    (retire_alice_csv("chains/destination") / "into_b.csv").write_text(
        f"account_name,lot,maximum,percent,series,cadence\n{ROTH},b,,,,\n")
    append_calendar_row(date="2027-01-31", kind="one_off_transfer", account_1=ROTH, lot_1="default",
                        account_2="into_b", amount="15000.00")
    assert _legs(_run(load_test_copy, runs_dir)) == [("2027-01-31", ROTH, 1_500_000, "none")]


def _roth_ctx(basis_first: bool, basis: int) -> engine.RefillContext:
    roth = RothLot(basis_first=basis_first, clock_start=date(2024, 1, 1), birthdate=None)
    return cast(engine.RefillContext, SimpleNamespace(roth={1: roth}, cost_bases={1: basis}))


@pytest.mark.parametrize(("basis_first", "basis", "balance", "basis_out"), [
    (True, 6_000_000, 10_000_000, 2_000_000),   # Roth IRA: basis first
    (False, 6_000_000, 10_000_000, 1_200_000),  # Roth 401k: pro-rata
    (False, 6_000_000, 4_000_000, 2_000_000),   # worth less than its basis: all basis
])
def test_the_basis_in_a_roth_draw_never_exceeds_the_draw(
    basis_first: bool, basis: int, balance: int, basis_out: int,
) -> None:
    ctx = _roth_ctx(basis_first, basis)
    assert engine._roth_basis_out(ctx, {1: balance}, 1, 2_000_000) == basis_out
    assert engine._roth_taxed(ctx, {1: balance}, 1, 2_000_000, date(2027, 1, 31)) == 2_000_000 - basis_out


def _bases(run: Path, month: str) -> dict[str, int]:
    """Each Roth lot's basis at ``month``'s end, by lot name."""
    with sqlite3.connect(run) as con:
        return dict(con.execute(
            "SELECT m.lot, ls.cost_basis FROM lot_state ls JOIN simulation_step s ON s.id = ls.step_id "
            "JOIN account_lot m ON m.id = ls.lot_id JOIN account a ON a.id = m.account_id "
            "WHERE a.name = ? AND substr(s.month, 1, 7) = ?", (ROTH, month),
        ).fetchall())


@pytest.mark.parametrize(("when", "moved_basis"), [
    ("2027-01-31", 1_500_000),  # basis first, so the $15,000 is all basis
    ("2029-01-31", 1_500_000),  # qualified: all of it arrives as basis
])
def test_a_roth_rollover_moves_basis_without_counting_it_twice(
    when: str, moved_basis: int, young_roth: Callable[..., None],
    add_account_value: Callable[..., None], append_calendar_row: Callable[..., None],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    young_roth("60000.00")
    add_account_value(ROTH, lot="b", opening_balance="0.00", opening_cost_basis="0.00")
    append_calendar_row(date=when, kind="one_off_transfer", account_1=ROTH, lot_1="default",
                        account_2=ROTH, lot_2="b", amount="15000.00")
    bases = _bases(_run(load_test_copy, runs_dir), when[:7])
    assert bases == {"default": 6_000_000 - 1_500_000, "b": moved_basis}


def test_a_nonqualified_roth_rollover_carries_only_its_basis(
    young_roth: Callable[..., None], add_account_value: Callable[..., None],
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    runs_dir: Path,
) -> None:
    young_roth("5000.00")  # $5,000 of basis in $100,000
    add_account_value(ROTH, lot="b", opening_balance="0.00", opening_cost_basis="0.00")
    append_calendar_row(date="2027-01-31", kind="one_off_transfer", account_1=ROTH, lot_1="default",
                        account_2=ROTH, lot_2="b", amount="15000.00")
    assert _bases(_run(load_test_copy, runs_dir), "2027-01") == {"default": 0, "b": 500_000}


@pytest.mark.parametrize("owner", ["", "nobody", "joint"])  # joint: a pseudo-person, no role
def test_a_retirement_account_needs_a_person_as_its_owner(
    owner: str, retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    path = retire_alice_csv("accounts.csv")
    accounts = pd.read_csv(path, dtype=str, keep_default_na=False)
    accounts.loc[accounts["name"] == ROTH, "owner"] = owner
    accounts.to_csv(path, index=False)
    with pytest.raises(LoadError, match=f"{ROTH}.*owner must be a person"):
        load_test_copy()


@pytest.mark.parametrize("role", ["primary", "secondary"])
def test_a_person_needs_a_birthdate(
    role: str, test_data_dir: Path, load_test_copy: Callable[[], LoadResult],
) -> None:
    persons = test_data_dir / "persons.csv"
    persons.write_text(persons.read_text() + f"carol,Carol (fake),,{role},{{}}\n")
    with pytest.raises(LoadError, match=f"carol.*{role} person needs a birthdate"):
        load_test_copy()


def test_a_basis_step_may_not_name_a_retirement_account(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    """A Roth lot's basis follows its own rules, so a sale's basis step would
    spend it twice."""
    (retire_alice_csv("chains/destination") / "roth_sale.csv").write_text(
        "account_name,lot,maximum,percent,series,cadence,tax_character,basis_of,basis_of_lot\n"
        f"Asset_Checking,,,,,,return_of_capital,{ROTH},default\n"
        "Asset_Checking,,,,,,ltcg,,\n")
    with pytest.raises(LoadError, match=f"basis_of '{ROTH}' is a retirement account"):
        load_test_copy()
