"""Beneficiaries (#274): where an account goes at its owner's death.

An account's ``beneficiary`` (accounts.csv) is a destination chain, a lot,
or an account whose lots match the decedent's. With none, a surviving spouse
takes the account over (its owner changes; no money moves), else the estate
(scenario meta_json ``estate``) takes it. A joint account goes the same way
at the household's last death. Everyone outside the household has an
account of their own, outside net worth, where gifts and bequests land."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner

from zabob.retirement.cli import main
from zabob.retirement.sim import chain_edit, engine, registries, rename, reports
from zabob.retirement.sim.connection import open_run, open_scenario
from zabob.retirement.sim.loader import LoadError, LoadResult

ALICE_DIES = "alice,70,,,,,,"  # born 1965-03-15: final month 2035-03
BILL_DIES = "bill,72,,,,,,"  # born 1967-08-20: final month 2039-08
BILL_DIES_FIRST = "bill,60,,,,,,"  # final month 2027-08
ALICE_FINAL, BILL_FINAL = "2035-03-31", "2039-08-31"
BROKERAGE = "Asset_Brokerage_Joint"


@pytest.fixture(autouse=True)
def _funded(fund_asset_savings: Callable[[str], None]) -> None:
    """retire_alice runs short in 2039 once both die (their income stops while
    joint costs go on); savings carry it to the second death."""
    fund_asset_savings("1000000.00")


@pytest.fixture
def heirs(add_person: Callable[..., None]) -> None:
    """A daughter and a charity, outside the household."""
    add_person("dana")
    add_person("good_cause", kind="charity")


@pytest.fixture
def write_chain(retire_alice_csv: Callable[[str], Path]) -> Callable[..., None]:
    """Write a destination chain of the private copy: ``write_chain("x", "a,", "b,50%")``,
    each step ``account_name,percent``."""
    def _write(name: str, *steps: str) -> None:
        path = retire_alice_csv(f"chains/destination/{name}.csv")
        path.write_text("account_name,lot,percent\n" + "".join(
            f"{account},{lot},{percent}\n"
            for account, _, rest in (step.partition(",") for step in steps)
            for lot, percent in [rest.split(",") if rest.count(",") else ("", rest)]))
    return _write


def _rows(run: Path, sql: str, *args: object) -> list[tuple]:
    with closing(sqlite3.connect(run)) as con:
        return con.execute(sql, args).fetchall()


def _balance(run: Path, account: str, month: str) -> int:
    rows = _rows(run, "SELECT balance FROM balance_by_account WHERE name = ? AND month = ?",
                 account, month)
    if rows:
        return rows[0][0]
    # An expense account isn't on the balance sheet; its lots still have balances.
    return _rows(run, "SELECT SUM(ls.balance) FROM lot_state ls "
                      "JOIN simulation_step s ON s.id = ls.step_id "
                      "JOIN account_lot m ON m.id = ls.lot_id JOIN account a ON a.id = m.account_id "
                      "WHERE a.name = ? AND s.month = ?", account, month)[0][0]


def _legs(run: Path) -> list[tuple[str, str, str, int]]:
    """Every inheritance leg: (date, from account, to account, character)."""
    return _rows(run, "SELECT te.date, f.name, t.name, te.tax_character FROM transfer_event te "
                      "JOIN account f ON f.id = te.from_account JOIN account t ON t.id = te.to_account "
                      "WHERE te.reason_kind = 'inheritance' ORDER BY te.id")


def test_each_outside_party_gets_an_account_of_their_own(
    heirs: None, load_test_copy: Callable[[], LoadResult],
) -> None:
    with closing(sqlite3.connect(load_test_copy().intent_path)) as con:
        rows = con.execute(
            "SELECT a.name, a.kind, a.subkind, a.owner, p.kind FROM account a "
            "JOIN person p ON p.id = a.owner WHERE a.name IN ('dana', 'good_cause') ORDER BY 1"
        ).fetchall()
        joint = con.execute("SELECT 1 FROM account WHERE name = 'joint'").fetchall()
    kind, subkind = registries.OUTSIDE_ACCOUNT_KIND, registries.OUTSIDE_ACCOUNT_SUBKIND
    assert rows == [("dana", kind, subkind, "dana", "person"),
                    ("good_cause", kind, subkind, "good_cause", "charity")]
    assert joint == []  # the household's own pseudo-person is no outside party


def test_an_account_an_outside_party_owns_must_be_outside_net_worth(
    heirs: None, update_account: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    update_account("Asset_Savings", owner="dana")
    with pytest.raises(LoadError, match=r"Asset_Savings.*'dana' is outside the household"):
        load_test_copy()


def test_a_person_kind_is_checked(
    add_person: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    add_person("dana", kind="cousin")
    with pytest.raises(LoadError, match=r"\(dana\): kind must be one of"):
        load_test_copy()


@pytest.mark.parametrize(("beneficiary", "problem"), [
    ("nowhere", "'nowhere' is not a known account or chain"),
    ("Income_Interest", "is a income account"),
    (BROKERAGE, "this account itself"),
])
def test_a_beneficiary_must_be_somewhere_money_can_go(
    beneficiary: str, problem: str, heirs: None,
    update_account: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    update_account(BROKERAGE, beneficiary=beneficiary)
    with pytest.raises(LoadError, match=problem):
        load_test_copy()


def test_an_account_left_to_a_child_leaves_the_household_at_the_death(
    heirs: None, write_lifespans: Callable[..., None], update_account: Callable[..., None],
    run_test_copy: Callable[[], Path],
) -> None:
    write_lifespans(ALICE_DIES, BILL_DIES)
    update_account(BROKERAGE, owner="alice", beneficiary="dana")
    run = run_test_copy()
    before = _balance(run, BROKERAGE, "2035-02-28")
    assert before > 0
    # The whole lot, stepped up first, so nothing is realized: one bequest leg.
    assert _legs(run) == [(ALICE_FINAL, BROKERAGE, "dana", registries.BEQUEST)]
    assert _balance(run, BROKERAGE, ALICE_FINAL) == 0
    assert _balance(run, "dana", ALICE_FINAL) > before
    assert _rows(run, "SELECT decedent, rule, placed > 0 FROM inheritance "
                      "WHERE lot_id IS NOT NULL") == [("alice", "designated", 1)]
    # Net worth leaves it out, as any expense.
    assert _rows(run, "SELECT COUNT(*) FROM balance_by_account WHERE name = 'dana'") == [(0,)]


def test_with_no_beneficiary_a_surviving_spouse_takes_the_account_over(
    write_lifespans: Callable[..., None], run_test_copy: Callable[[], Path],
) -> None:
    write_lifespans(ALICE_DIES, BILL_DIES)
    run = run_test_copy()
    assert _legs(run) == []
    taken = _rows(run, "SELECT a.name, i.owner, i.rule FROM inheritance i "
                       "JOIN account a ON a.id = i.account_id ORDER BY a.name")
    assert taken == [("Asset_Roth_Alice", "bill", "spouse"),
                     ("Asset_TraditionalIRA_Alice", "bill", "spouse")]
    owners = dict(_rows(run, "SELECT month, owner FROM balance_by_owner "
                             "WHERE owner IN ('alice', 'bill') AND month IN (?, ?) AND owner = 'alice'",
                        ALICE_FINAL, "2035-04-30"))
    assert owners == {ALICE_FINAL: "alice"}  # Alice's own accounts are Bill's from the next month


@pytest.mark.parametrize(("bill", "legs"), [
    # Bill outlives Alice: a rollover into his own Roth, untaxed.
    (BILL_DIES, [(ALICE_FINAL, "Asset_Roth_Alice", "Asset_Roth_Bill", "none")]),
    # Bill died first: his step is skipped, and the daughter takes it.
    (BILL_DIES_FIRST, [(ALICE_FINAL, "Asset_Roth_Alice", "dana", registries.BEQUEST)]),
])
def test_a_contingent_beneficiary_takes_it_when_the_first_has_died(
    bill: str, legs: list[tuple[str, str, str, str]], heirs: None,
    write_lifespans: Callable[..., None], update_account: Callable[..., None],
    write_chain: Callable[..., None], run_test_copy: Callable[[], Path],
) -> None:
    write_chain("alice_roth_heirs", "Asset_Roth_Bill,default,", "dana,,")
    update_account("Asset_Roth_Alice", beneficiary="alice_roth_heirs")
    write_lifespans(ALICE_DIES, bill)
    assert _legs(run_test_copy()) == legs


# Rules with no person (the home's costs) run on after the last death, when
# the estate has taken everything they would draw from.
@pytest.mark.allow_negative_balance
def test_the_estate_takes_what_is_left_at_the_last_death_paying_debts_first(
    heirs: None, write_lifespans: Callable[..., None], write_chain: Callable[..., None],
    set_scenario_meta: Callable[..., None], run_test_copy: Callable[[], Path],
) -> None:
    write_chain("estate", "Liability_Mortgage,,", "dana,,50%", "good_cause,,")
    set_scenario_meta(estate="estate")
    write_lifespans(ALICE_DIES, BILL_DIES)
    run = run_test_copy()
    legs = _legs(run)
    # Nothing at Alice's death: Bill survives her and takes her accounts over.
    assert {leg[0] for leg in legs} == {BILL_FINAL}
    assert {leg[2] for leg in legs} == {"Liability_Mortgage", "dana", "good_cause"}
    assert _balance(run, "Liability_Mortgage", BILL_FINAL) == 0
    for account in ("Asset_Checking", "Asset_Savings", BROKERAGE, "Asset_Home",
                    "Asset_TraditionalIRA_Alice", "Asset_Roth_Alice", "Asset_Roth_Bill"):
        assert _balance(run, account, BILL_FINAL) == 0, account


@pytest.mark.parametrize(("heir", "owner"), [("joint", "joint"), ("bill", "bill")])
def test_a_beneficiary_in_the_household_takes_the_account_over(
    heir: str, owner: str, write_lifespans: Callable[..., None],
    update_account: Callable[..., None], run_test_copy: Callable[[], Path],
) -> None:
    update_account("Asset_Savings", owner="alice", beneficiary=heir)
    write_lifespans(ALICE_DIES, BILL_DIES)
    run = run_test_copy()
    assert _legs(run) == []
    assert ("Asset_Savings", owner, "designated") in _rows(
        run, "SELECT a.name, i.owner, i.rule FROM inheritance i JOIN account a ON a.id = i.account_id")


def test_a_retirement_account_passes_only_to_a_person(
    update_account: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    update_account("Asset_Roth_Alice", beneficiary="joint")
    with pytest.raises(LoadError, match="a retirement account passes only to a person"):
        load_test_copy()


def test_a_blank_owner_is_refused(
    update_account: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    update_account("Asset_Savings", owner="")
    with pytest.raises(LoadError, match=r"Asset_Savings.*owner is blank"):
        load_test_copy()


def test_renames_follow_beneficiaries_and_the_estate(
    heirs: None, write_chain: Callable[..., None], update_account: Callable[..., None],
    set_scenario_meta: Callable[..., None], retire_alice_csv: Callable[[str], Path],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    write_chain("estate", "dana,,")
    set_scenario_meta(estate="estate")
    update_account(BROKERAGE, owner="alice", beneficiary="estate")
    update_account("Asset_Roth_Alice", beneficiary="Asset_Roth_Bill")
    scenario = retire_alice_csv("accounts.csv").parent
    assert "scenario.csv" in chain_edit.rename_references(scenario, "estate", "heirs")
    assert "accounts.csv" in rename.rename_account(scenario, "Asset_Roth_Bill", "Asset_Roth_B")
    accounts = pd.read_csv(retire_alice_csv("accounts.csv"), dtype=str, keep_default_na=False)
    assert dict(zip(accounts["name"], accounts["beneficiary"], strict=True))[BROKERAGE] == "heirs"
    assert "Asset_Roth_B" in set(accounts["beneficiary"])
    meta = pd.read_csv(retire_alice_csv("scenario.csv"), dtype=str, keep_default_na=False)["meta_json"][0]
    assert json.loads(meta)["estate"] == "heirs"
    load_test_copy()


def test_account_edit_sets_a_beneficiary_the_scenario_loads_with(
    heirs: None, test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    def edit(beneficiary: str) -> str:
        return CliRunner().invoke(main, [
            "sim", "account", "edit", BROKERAGE, "--beneficiary", beneficiary,
            "--scenario", "retire_alice", "--data-dir", str(test_data_dir)]).output

    edit("dana")
    accounts = pd.read_csv(retire_alice_csv("accounts.csv"), dtype=str, keep_default_na=False)
    assert accounts.set_index("name").at[BROKERAGE, "beneficiary"] == "dana"
    assert "'nowhere' is not a known account or chain" in edit("nowhere")


def test_explain_and_the_income_report_show_the_bequest(
    heirs: None, write_lifespans: Callable[..., None], update_account: Callable[..., None],
    run_test_copy: Callable[[], Path],
) -> None:
    write_lifespans(ALICE_DIES, BILL_DIES)
    update_account(BROKERAGE, owner="alice", beneficiary="dana")
    run = run_test_copy()

    def sim(*args: str) -> str:
        result = CliRunner().invoke(main, ["sim", *args])
        assert result.exit_code == 0, result.output
        return result.output

    explained = sim("explain", str(run))
    assert "accounts passed on at a death (3):" in explained  # the brokerage, and Alice's IRAs to Bill
    assert any(BROKERAGE in line and "-> dana" in line and "left the household" in line
               and "(its beneficiary)" in line for line in explained.splitlines())
    income = sim("report", "income", "--runfile", str(run), "--year", "2035")
    assert "gifts_and_bequests" in income


GRANDMA_DIES = "grandma,92,uniform,88,97,,,"  # born 1940-06-15: final month 2032-06 in the base case
GRANDMA_DIES_LAST = "grandma,100,,,,,,"  # 2040-06, after Alice and Bill


@pytest.fixture
def grandma(
    add_person: Callable[..., None], add_account_value: Callable[..., None],
    retire_alice_csv: Callable[[str], Path],
) -> Callable[[str], None]:
    """An older relative outside the household with $200,000 of her own,
    which goes to her account's ``beneficiary`` at her death."""
    add_person("grandma", birthdate="1940-06-15")
    add_account_value("grandma", opening_balance="200000.00")

    def _leave_to(beneficiary: str) -> None:
        path = retire_alice_csv("accounts.csv")
        accounts = pd.read_csv(path, dtype=str, keep_default_na=False)
        if "beneficiary" not in accounts:  # an optional column the prototype leaves out
            accounts["beneficiary"] = ""
        accounts.loc[len(accounts)] = {
            **dict.fromkeys(accounts.columns, ""), "name": "grandma", "kind": "expense",
            "subkind": "gift", "owner": "grandma", "meta_json": "{}", "beneficiary": beneficiary}
        accounts.to_csv(path, index=False)
    return _leave_to


def _lot_basis(run: Path, account: str, month: str) -> int:
    return _rows(run, "SELECT SUM(ls.cost_basis) FROM lot_state ls "
                      "JOIN simulation_step s ON s.id = ls.step_id "
                      "JOIN account_lot m ON m.id = ls.lot_id JOIN account a ON a.id = m.account_id "
                      "WHERE a.name = ? AND s.month = ?", account, month)[0][0]


def test_a_relatives_bequest_comes_into_the_household_at_her_death(
    grandma: Callable[[str], None], write_lifespans: Callable[..., None], run_test_copy: Callable[[], Path],
) -> None:
    grandma(BROKERAGE)
    write_lifespans(GRANDMA_DIES)  # her date varies in an ensemble; the base case is 92
    run = run_test_copy()
    # Untaxed, and the brokerage's basis rises by all of it: what she left is stepped up.
    assert _legs(run) == [("2032-06-30", "grandma", BROKERAGE, "none")]
    assert _balance(run, "grandma", "2032-06-30") == 0
    before, after = (_lot_basis(run, BROKERAGE, m) for m in ("2032-05-31", "2032-06-30"))
    assert after - before == 20_000_000
    assert _rows(run, "SELECT decedent, rule, placed FROM inheritance") == [
        ("grandma", "designated", 20_000_000)]



def test_a_bequest_coming_in_keeps_the_income_report_adding_up(
    grandma: Callable[[str], None], write_lifespans: Callable[..., None],
    run_test_copy: Callable[[], Path], test_data_dir: Path,
) -> None:
    grandma(BROKERAGE)
    write_lifespans(GRANDMA_DIES)
    run = run_test_copy()
    with open_scenario(test_data_dir, "retire_alice") as scenario, \
            open_run(run, scenario, scenarios_dir=test_data_dir / "scenarios") as rc:
        story = reports.flows_by_year(rc, "income", after_tax=False)
    year = story.loc[2032]
    # Money back out of a gift account is a negative gift: net worth rose by it.
    assert year[reports.GIFTS_AND_BEQUESTS] == pytest.approx(-200_000)
    assert year[reports.NET_WORTH_CHANGE] == pytest.approx(
        year[reports.ECONOMIC_INCOME] - year["tax_paid"] - year["spending"]
        - year[reports.GIFTS_AND_BEQUESTS], abs=0.01)


def test_a_relatives_bequest_skips_an_heir_who_died_first(
    heirs: None, grandma: Callable[[str], None], update_account: Callable[..., None],
    write_chain: Callable[..., None], write_lifespans: Callable[..., None],
    run_test_copy: Callable[[], Path],
) -> None:
    write_chain("grandma_will", "Asset_Savings,,", "dana,,")
    update_account("Asset_Savings", owner="alice")
    grandma("grandma_will")
    write_lifespans(ALICE_DIES, BILL_DIES, GRANDMA_DIES_LAST)
    legs = [leg for leg in _legs(run_test_copy()) if leg[1] == "grandma"]
    # Alice's savings passed to Bill, who died in 2039 too, so the household's
    # step is skipped and the money goes on to Dana.
    assert legs == [("2040-06-30", "grandma", "dana", registries.BEQUEST)]


def test_a_relatives_account_cant_be_taken_over_by_the_household(
    grandma: Callable[[str], None], load_test_copy: Callable[[], LoadResult],
) -> None:
    grandma("alice")
    with pytest.raises(LoadError, match="can't take over an account outside the household"):
        load_test_copy()


def test_a_dead_heir_outside_the_household_is_skipped_too(
    heirs: None, add_person: Callable[..., None], write_lifespans: Callable[..., None],
    update_account: Callable[..., None], write_chain: Callable[..., None],
    run_test_copy: Callable[[], Path],
) -> None:
    add_person("eve", birthdate="1990-01-01")
    write_chain("brokerage_heirs", "eve,,", "dana,,")
    update_account(BROKERAGE, owner="alice", beneficiary="brokerage_heirs")
    write_lifespans(ALICE_DIES, BILL_DIES, "eve,40,,,,,,")  # Eve dies in 2030
    assert _legs(run_test_copy()) == [(ALICE_FINAL, BROKERAGE, "dana", registries.BEQUEST)]


def test_a_contingent_spouse_reached_by_no_money_does_not_take_the_roth_as_their_own(
    heirs: None, write_lifespans: Callable[..., None], update_account: Callable[..., None],
    write_chain: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    def roth_heirs(*steps: str) -> dict[str, frozenset[str]]:
        write_chain("alice_roth_heirs", *steps)
        update_account("Asset_Roth_Alice", beneficiary="alice_roth_heirs")
        write_lifespans(ALICE_DIES, BILL_DIES)
        with closing(sqlite3.connect(load_test_copy().intent_path)) as con:
            return engine._inheritance_plan(
                con, engine._household(con), engine._load_chain_graph(con)).roth_heirs

    # Dana takes all of it, so Bill's Roth, a later step, receives nothing.
    assert roth_heirs("dana,,", "Asset_Roth_Bill,default,") == {}
    assert roth_heirs("Asset_Roth_Bill,default,", "dana,,") == {"alice": frozenset({"bill"})}


@pytest.mark.parametrize(("estate", "problem"), [
    ("Asset_Savings", "reaches no one outside the household"),
    ("dana", None),
])
def test_an_estate_must_reach_someone_outside_the_household(
    estate: str, problem: str | None, heirs: None, set_scenario_meta: Callable[..., None],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    set_scenario_meta(estate=estate)
    if problem is None:
        load_test_copy()
        return
    with pytest.raises(LoadError, match=problem):
        load_test_copy()


def test_an_outside_party_has_one_account_named_by_their_id(
    heirs: None, retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    path = retire_alice_csv("accounts.csv")
    accounts = pd.read_csv(path, dtype=str, keep_default_na=False)
    accounts.loc[len(accounts)] = {
        **dict.fromkeys(accounts.columns, ""), "name": "dana_college", "kind": "expense",
        "subkind": "gift", "owner": "dana", "meta_json": "{}"}
    accounts.to_csv(path, index=False)
    with pytest.raises(LoadError, match=r"dana_college.*one account is named by their id"):
        load_test_copy()


def test_a_stand_in_with_no_kind_stays_in_the_household(
    add_person: Callable[..., None], update_account: Callable[..., None],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    # A custodial account's owner, say: no role and no kind, so not an heir.
    add_person("kids", kind="")
    update_account("Asset_Savings", owner="kids")
    with closing(sqlite3.connect(load_test_copy().intent_path)) as con:
        assert con.execute("SELECT 1 FROM account WHERE name = 'kids'").fetchall() == []
