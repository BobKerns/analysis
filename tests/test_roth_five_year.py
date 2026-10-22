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


@pytest.mark.parametrize(("born", "on", "qualified"), [
    (date(1965, 3, 15), date(2024, 9, 14), False),
    (date(1965, 3, 15), date(2024, 9, 15), True),
    (date(1965, 8, 31), date(2025, 2, 27), False),
    (date(1965, 8, 31), date(2025, 2, 28), True),  # no Feb 31: the month's last day
])
def test_a_qualified_distribution_also_needs_59_and_a_half(born: date, on: date, qualified: bool) -> None:
    roth = RothLot(basis_first=True, clock_start=None, birthdate=born)
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
    persons.write_text(persons.read_text() + f"carol,Carol (fake),,,{role},{{}}\n")
    with pytest.raises(LoadError, match=f"carol.*{role} person needs a birthdate"):
        load_test_copy()


def test_a_pseudo_person_without_a_birthdate_still_runs(
    test_data_dir: Path, load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    persons = test_data_dir / "persons.csv"
    rows = pd.read_csv(persons, dtype=str, keep_default_na=False)
    rows.loc[rows["id"] == "joint", "birthdate"] = ""
    rows.to_csv(persons, index=False)
    assert _run(load_test_copy, runs_dir).exists()


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


# A death (#256). Alice's lifespan of 61.5 ends 2026-09-30; Bill's of 59 ends
# 2026-08-31 and of 60, 2027-08-31. Bill turns 59½ on 2027-02-20. Both Roth
# IRAs' clocks start 2015 (run) unless ``young_roth`` moves Alice's to 2024.
BILL_ROTH = "Asset_Roth_Bill"


def test_a_lot_follows_its_later_rules_from_their_date() -> None:
    after = RothLot(basis_first=True, clock_start=None, birthdate=None)
    roth = RothLot(basis_first=True, clock_start=date(2024, 1, 1), birthdate=date(1950, 1, 1),
                   then=(date(2026, 1, 1), after))
    assert not engine._roth_qualified(roth, date(2025, 12, 31))
    assert engine._roth_qualified(roth, date(2026, 1, 1))


@pytest.fixture
def roth_survivor(retire_alice_csv: Callable[[str], Path]) -> Callable[[str], None]:
    """Set retire_alice's ``roth_survivor`` election in scenario.csv meta_json."""
    def _set(election: str) -> None:
        path = retire_alice_csv("scenario.csv")
        scenario = pd.read_csv(path, dtype=str, keep_default_na=False)
        scenario["meta_json"] = f'{{"roth_survivor": "{election}"}}'
        scenario.to_csv(path, index=False)
    return _set


def _qualified(load: Callable[[], LoadResult], account: str, on: str) -> bool:
    """Whether a distribution from ``account``'s default lot on ``on`` is qualified."""
    with sqlite3.connect(load().intent_path) as con:
        birthdates = {p: date.fromisoformat(b) for p, b in con.execute(
            "SELECT id, birthdate FROM person WHERE birthdate != ''")}
        (lot,) = con.execute(
            "SELECT m.id FROM account_lot m JOIN account a ON a.id = m.account_id "
            "WHERE a.name = ? AND m.lot = 'default'", (account,)).fetchone()
        return engine._roth_qualified(engine._roth_lots(con, birthdates)[lot], date.fromisoformat(on))


@pytest.mark.parametrize(("election", "on", "qualified"), [
    ("own", "2027-06-30", False),          # Alice's own clock, from 2024, hasn't run
    ("own", "2027-09-30", True),           # from Bill's death: the earlier of the two, 2015
    ("beneficiary", "2027-09-30", False),  # her own clock is untouched
])
def test_a_survivor_who_treats_the_roth_as_their_own_takes_the_earlier_clock(
    election: str, on: str, qualified: bool, young_roth: Callable[..., None],
    roth_survivor: Callable[[str], None], write_lifespans: Callable[..., None],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    young_roth()
    roth_survivor(election)
    write_lifespans("bill,60,,,,,,")
    assert _qualified(load_test_copy, ROTH, on) is qualified


@pytest.mark.parametrize(("election", "on", "qualified"), [
    ("own", "2026-09-30", True),            # Alice alive and past 59½
    ("own", "2026-12-31", False),           # now Bill's, and he isn't 59½ yet
    ("own", "2027-02-28", True),            # ... until 2027-02-20
    ("beneficiary", "2026-12-31", True),    # Alice's clock has run; no age test
])
def test_the_decedents_roth_follows_the_survivors_election(
    election: str, on: str, qualified: bool, roth_survivor: Callable[[str], None],
    write_lifespans: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    roth_survivor(election)
    write_lifespans("alice,61.5,,,,,,")
    assert _qualified(load_test_copy, ROTH, on) is qualified


@pytest.mark.parametrize("election", ["own", "beneficiary"])
def test_a_decedent_under_59_and_a_half_leaves_a_qualified_roth(
    election: str, roth_survivor: Callable[[str], None],
    write_lifespans: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    """Bill dies at 59 with a clock that has run: before his death his
    distributions wait for 59½, after it they don't (Alice is past it)."""
    roth_survivor(election)
    write_lifespans("bill,59,,,,,,")
    assert not _qualified(load_test_copy, BILL_ROTH, "2026-08-31")
    assert _qualified(load_test_copy, BILL_ROTH, "2026-09-30")


def test_after_both_deaths_no_age_test_remains(
    write_lifespans: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    write_lifespans("alice,61.5,,,,,,", "bill,60,,,,,,")
    assert not _qualified(load_test_copy, ROTH, "2026-12-31")  # Bill's, under 59½
    assert _qualified(load_test_copy, ROTH, "2027-09-30")


def test_without_two_primary_persons_a_death_leaves_no_spouse(
    test_data_dir: Path, write_lifespans: Callable[..., None],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    """Bill made secondary: Alice's Roth passes to a beneficiary, so the
    under-59½ Bill doesn't hold it back."""
    persons = test_data_dir / "persons.csv"
    rows = pd.read_csv(persons, dtype=str, keep_default_na=False)
    rows.loc[rows["id"] == "bill", "role"] = "secondary"
    rows.to_csv(persons, index=False)
    write_lifespans("alice,61.5,,,,,,")
    assert _qualified(load_test_copy, ROTH, "2026-12-31")


def test_a_survivors_earnings_come_out_tax_free_on_the_decedents_clock(
    young_roth: Callable[..., None], write_lifespans: Callable[..., None],
    retire_alice_csv: Callable[[str], Path], append_calendar_row: Callable[..., None],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    """The run itself: Alice's own earnings, taxed before Bill's death, come
    out tax-free once his older clock is hers."""
    young_roth("0.00")  # all earnings
    # Ends before the household, short of Bill's income, runs out of cash.
    path = retire_alice_csv("scenario.csv")
    scenario = pd.read_csv(path, dtype=str, keep_default_na=False)
    scenario["end"] = "2028-12-31"
    scenario.to_csv(path, index=False)
    write_lifespans("bill,60,,,,,,")
    for when in ("2027-06-30", "2027-09-30"):
        append_calendar_row(date=when, kind="one_off_transfer", account_1=ROTH, lot_1="default",
                            account_2="Asset_Checking", amount="35000.00")
    legs = [(d, character) for d, _to, _amount, character in _legs(_run(load_test_copy, runs_dir))]
    assert ("2027-06-30", "roth_nonqualified") in legs
    assert [character for d, character in legs if d == "2027-09-30"] == ["tax_free"]


def test_roth_survivor_must_be_an_election(
    roth_survivor: Callable[[str], None], load_test_copy: Callable[[], LoadResult],
) -> None:
    roth_survivor("spouse")
    with pytest.raises(LoadError, match="roth_survivor must be one of own, beneficiary"):
        load_test_copy()
