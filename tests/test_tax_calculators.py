"""A household's own tax calculators (#510, ADR-011): ``taxes/*.py`` in the
data repo, imported as strategies are, checked at load, snapshotted into
the intent and run; and what they read, the year's spending and the tags
on it."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest

from analysis.sim import provenance, tax_api, tax_code
from analysis.sim.connection import open_run
from analysis.sim.loader import LoadError, LoadResult
from analysis.sim.tax_api import Account, IncomeItem, SpendingItem

HOUSING = Account("Expense_Housing", "expense", "housing", "joint", tags=frozenset({"tax:rent"}))
CHECKING = Account("Asset_Checking", "asset", "cash", "joint")
PENSION = Account("Income_Pension", "income", "pension", "alice", tags=frozenset({"tax:pension:public"}))


def test_a_rules_tag_refines_its_accounts_in_the_same_role() -> None:
    rent = SpendingItem(2030, "none", CHECKING, HOUSING, 1_00)
    repair = SpendingItem(2030, "none", CHECKING, HOUSING, 1_00, rule_tags=frozenset({"tax:repair"}))
    flexible = SpendingItem(2030, "none", CHECKING, HOUSING, 1_00, rule_tags=frozenset({"flex:optional"}))
    assert rent.tags == {"tax:rent"}
    assert repair.tags == {"tax:repair"}
    assert flexible.tags == {"tax:rent", "flex:optional"}
    # Income counts against its income account, whichever way it flows.
    assert IncomeItem(2030, "ordinary", PENSION, CHECKING, 1_00).tags == {"tax:pension:public"}
    assert IncomeItem(2030, "ordinary", CHECKING, PENSION, -1_00).tags == {"tax:pension:public"}


def _tag_rule(retire_alice_csv: Callable[[str], Path], rule: str, tags: str) -> None:
    path = retire_alice_csv("scheduled.csv")
    rows = pd.read_csv(path, dtype=str, keep_default_na=False)
    rows.loc[rows["name"] == rule, "tags"] = tags
    rows.to_csv(path, index=False)


@pytest.fixture
def renters(add_state_tax: Callable[..., None], add_tax: Callable[..., Path],
            tax_module: Callable[..., Path]) -> Callable[..., Path]:
    """The private retire_alice copy, taxed by California through the
    ``state_renters`` example calculator; ``renters(**params)`` sets its
    params."""
    add_state_tax()
    tax_module("renters")
    return lambda **params: add_tax("state", kind="state_renters", params_json=params)


def _summary(run: Path, bucket: str) -> dict[int, int]:
    con = sqlite3.connect(run)
    try:
        return dict(con.execute(
            "SELECT year, amount FROM tax_year_summary WHERE bucket = ? ORDER BY year", (bucket,)))
    finally:
        con.close()


@pytest.mark.slow
def test_a_household_calculator_takes_its_deduction_from_tagged_spending(
    renters: Callable[..., Path], retire_alice_csv: Callable[[str], Path],
    run_test_copy: Callable[[], Path], load_test_copy: Callable[[], LoadResult], test_data_dir: Path,
) -> None:
    renters()
    _tag_rule(retire_alice_csv, "housing", "tax:rent")
    run = run_test_copy()
    # $800 a month, growing with inflation: half a full year's is past the
    # $4,000 cap. Once Alice and Bill retire, it can't exceed their ordinary
    # income, which is small.
    deduction = _summary(run, "state_renter_deduction")
    assert deduction[2027] == 4000_00
    assert all(0 < amount <= 4000_00 for amount in deduction.values())
    # The calculator's own line shows in the state's return.
    shown = _cli(test_data_dir, "report", "tax", "--return", "state", "--runfile", str(run),
                 "--dollars", "nominal", "--start", "2027", "--end", "2028")
    assert shown.exit_code == 0, shown.output
    [line] = [row for row in shown.output.splitlines() if "renter_deduction" in row]
    assert line.count("$4,000") == 2
    con = sqlite3.connect(load_test_copy().intent_path)
    try:
        assert [m for (m,) in con.execute("SELECT module FROM tax_source")] == ["renters"]
    finally:
        con.close()


def test_a_tag_the_calculator_reads_must_be_carried(
    renters: Callable[..., Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    renters()
    with pytest.raises(LoadError, match=r"the state_renters calculator reads tag 'tax:rent' \(its 'rent'\), "
                                        "and no account, scheduled rule or calendar row carries it"):
        load_test_copy()
    renters(rent="tax:lease")
    with pytest.raises(LoadError, match="parameter 'rent': no account, scheduled rule or calendar row "
                                        "carries tag 'tax:lease'"):
        load_test_copy()


def test_a_household_calculators_params_are_checked_in_their_units(
    renters: Callable[..., Path], retire_alice_csv: Callable[[str], Path],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    _tag_rule(retire_alice_csv, "housing", "tax:rent")
    renters(rent_share=50)  # a percent carries its %
    with pytest.raises(LoadError, match="parameter 'rent_share'"):
        load_test_copy()
    renters(rent_cap="3000.00", rent_share="40%")
    con = sqlite3.connect(load_test_copy().intent_path)
    try:
        (params,) = con.execute("SELECT params_json FROM tax_jurisdiction WHERE name = 'state'").fetchone()
    finally:
        con.close()
    assert params == '{"rent_cap":300000,"rent_share":400000}'


def test_a_built_in_scenario_keeps_no_calculator_source(
    add_state_tax: Callable[..., None], tax_module: Callable[..., Path],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    add_state_tax()
    tax_module("renters")
    con = sqlite3.connect(load_test_copy().intent_path)
    try:
        assert con.execute("SELECT COUNT(*) FROM tax_source").fetchone() == (0,)
    finally:
        con.close()


def test_a_broken_module_fails_only_the_loads_that_name_a_household_kind(
    add_state_tax: Callable[..., None], add_tax: Callable[..., Path],
    tax_module: Callable[..., Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    add_state_tax()
    tax_module("broken", "raise RuntimeError('half written')\n")
    result = load_test_copy()
    assert any("taxes/broken.py failed to import" in u for u in result.unused)
    add_tax("state", kind="mine")
    with pytest.raises(LoadError) as caught:
        load_test_copy()
    assert any("taxes/broken.py failed to import" in i for i in caught.value.issues)
    assert any("no tax calculator kind 'mine'" in i for i in caught.value.issues)


def test_a_household_kind_cant_take_a_built_in_name_or_compute_another_tax(
    add_state_tax: Callable[..., None], add_tax: Callable[..., Path],
    tax_module: Callable[..., Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    kinds = tax_code.TaxKinds({"again": (
        "from analysis.sim.tax_api import TaxCalculator, register\n"
        "@register\nclass Again(TaxCalculator):\n    kind = 'federal'\n    jurisdiction = 'federal'\n")})
    assert kinds.problems == [
        "taxes/again.py: kind 'federal' is already defined (built-in); rename it or promote it"]
    add_state_tax()
    tax_module("renters")
    add_tax("federal", kind="state_renters")
    with pytest.raises(LoadError, match="kind 'state_renters' computes the state tax; "
                                        "the federal tax takes kind federal"):
        load_test_copy()


def test_a_newer_api_version_is_refused() -> None:
    kinds = tax_code.TaxKinds({"future": "API_VERSION = 99\n"})
    assert kinds.problems == [
        f"taxes/future.py is written for tax calculator API 99; this code provides {tax_api.API_VERSION}"]


def test_the_taxes_folder_is_in_the_provenance_scope(
    test_data_dir: Path, tax_module: Callable[..., Path],
) -> None:
    tax_module("renters")
    assert "taxes" in provenance._scoped_paths(test_data_dir, "retire_alice")


def _cli(data_dir: Path, *args: str):
    from click.testing import CliRunner

    from analysis.cli import main

    return CliRunner().invoke(main, ["sim", *args, "--data-dir", str(data_dir)], terminal_width=200)


def test_sim_tax_kinds_lists_the_households_kinds_and_their_params(
    test_data_dir: Path, tax_module: Callable[..., Path],
) -> None:
    tax_module("renters")
    result = _cli(test_data_dir, "tax", "kinds")
    assert result.exit_code == 0, result.output
    assert "federal  (built-in; the federal tax)" in result.output
    assert "state_renters  (taxes/renters.py; the state tax, on federal)" in result.output
    result = _cli(test_data_dir, "tax", "kinds", "state_renters")
    assert "rent_cap" in result.output and "(dollars, default 4000.00)" in result.output
    # The parameters it inherits from state_tables, with their docs.
    assert "100 by default" in result.output


_HASHED = '''
from dataclasses import dataclass

from analysis.sim.tax_api import register
from analysis.sim.taxes.state_base import Adjustment
from analysis.sim.taxes.state_tables import StateTables, StateTablesYear


class OddYear(StateTablesYear):
    def subtractions(self, income, basis):
        # Set order of strings changes with the process's hash seed.
        first = next(iter(set("abcdefgh")))
        return (*super().subtractions(income, basis), Adjustment("odd", ordinary=ord(first)))


@register
class Odd(StateTables):
    kind = "state_odd"
    jurisdiction = "state"
    year_class = OddYear
'''


@pytest.mark.slow
def test_sim_tax_check_runs_twice_and_finds_a_calculator_that_depends_on_hash_order(
    renters: Callable[..., Path], retire_alice_csv: Callable[[str], Path],
    add_tax: Callable[..., Path], tax_module: Callable[..., Path], test_data_dir: Path,
) -> None:
    renters()
    _tag_rule(retire_alice_csv, "housing", "tax:rent")
    result = _cli(test_data_dir, "tax", "check", "retire_alice")
    assert result.exit_code == 0, result.output
    assert "the same in both runs" in result.output
    tax_module("odd", _HASHED)
    add_tax("state", kind="state_odd")
    result = _cli(test_data_dir, "tax", "check", "retire_alice")
    assert result.exit_code != 0
    assert "2026's state_agi came out differently on a second run" in result.output


def test_sim_tax_edit_chooses_the_calculator(
    add_state_tax: Callable[..., None], tax_module: Callable[..., Path],
    retire_alice_csv: Callable[[str], Path], test_data_dir: Path,
) -> None:
    add_state_tax()
    tax_module("renters")
    _tag_rule(retire_alice_csv, "housing", "tax:rent")

    def kind() -> str:
        return pd.read_csv(retire_alice_csv("taxes/state.csv"), dtype=str, keep_default_na=False)["kind"][0]

    result = _cli(test_data_dir, "tax", "edit", "state", "--kind", "state_renters", "--scenario", "retire_alice")
    assert result.exit_code == 0, result.output
    assert kind() == "state_renters"
    result = _cli(test_data_dir, "tax", "edit", "state", "--kind", "", "--scenario", "retire_alice")
    assert result.exit_code == 0, result.output
    assert kind() == "state_tables"


def test_the_guides_and_videos_example_is_the_tested_calculator() -> None:
    """docs/guides/tax-calculators.md shows property_credit.py whole, and
    video 13 shows (and its setup copies) its own copy, so the code both
    show is the code these tests run."""
    root = Path(__file__).parent.parent
    guide = (root / "docs" / "guides" / "tax-calculators.md").read_text(encoding="utf-8")
    module = (root / "tests" / "data" / "sim" / "tax_calculators" / "taxes" / "property_credit.py").read_text(
        encoding="utf-8")
    assert f"```python\n{module}```" in guide
    video = root / "docs" / "videos" / "13-tax-plugins" / "property_credit.py"
    assert video.read_text(encoding="utf-8") == module


@pytest.fixture
def property_credit(add_state_tax: Callable[..., None], add_tax: Callable[..., Path],
                    tax_module: Callable[..., Path],
                    retire_alice_csv: Callable[[str], Path]) -> Callable[..., Path]:
    """The private retire_alice copy, living in DC and taxed through the
    ``property_credit`` example calculator (#568), its housing tagged as
    property tax; ``property_credit(**params)`` sets its params."""
    add_state_tax("DC")
    tax_module("property_credit")
    _tag_rule(retire_alice_csv, "housing", "tax:property")
    return lambda **params: add_tax("state", kind="property_credit", params_json=params)


@pytest.mark.slow
def test_a_refundable_credit_takes_the_tax_below_zero(
    property_credit: Callable[..., Path], run_test_copy: Callable[[], Path],
    test_data_dir: Path, test_scenarios_dir: Path,
) -> None:
    property_credit()
    run = run_test_copy()
    credit, tax = _summary(run, "state_property_credit"), _summary(run, "state_tax")
    # Working, Alice and Bill earn too much; once retired, their $800 a
    # month of housing is far past the cap, which grows with inflation.
    assert 2030 not in credit
    assert 1425_00 < credit[2031] < credit[2032] < 1425_00 * 2
    # They owe DC nothing then, so DC pays the whole credit back in April.
    refund = _summary(run, "state_tax_overpaid")
    assert all(tax[year] == -amount and refund[year] == amount for year, amount in credit.items())
    # And the refund is money: each April, out of the state tax account back
    # into checking, which pays the tax.
    con = sqlite3.connect(run)
    try:
        paid_back = dict(con.execute(
            "SELECT e.tax_year, e.amount FROM transfer_event e "
            "JOIN account a ON a.id = e.from_account JOIN account b ON b.id = e.to_account "
            "WHERE a.name = 'Expense_Taxes_State' AND b.name = 'Asset_Checking' "
            "AND e.reason_kind = 'tax'"))
    finally:
        con.close()
    assert paid_back == {year: amount for year, amount in credit.items() if year < max(credit)}
    # Until it's paid back, the refund is money DC owes them: after-tax net
    # worth counts it, less the federal tax due, from December 31 to April,
    # and the run's last year's, never paid, to the end.
    federal = _summary(run, "federal_tax_due")
    first, last = min(credit), max(credit)
    with open_run(run, "retire_alice", data_dir=test_data_dir,
                  scenarios_dir=test_scenarios_dir) as rc:
        unpaid = rc.after_tax_net_worth_frame()["unpaid_tax"]
        # The reports net the refund against the tax paid into the account.
        state = rc.expense_events()
        state = state[(state["to_name"] == "Expense_Taxes_State")
                      & (state["date"].str[:4] == str(first + 1))]
        into = rc.items("transfer_event", to_name="Expense_Taxes_State")
        into = into[into["date"].str[:4] == str(first + 1)]
    assert unpaid[f"{first}-12"].iloc[0] == pytest.approx((federal.get(first, 0) - credit[first]) / 100)
    assert unpaid[f"{first + 1}-04"].iloc[0] == 0
    assert unpaid.iloc[-1] == pytest.approx((federal.get(last, 0) - credit[last]) / 100)
    assert state["amount"].sum() == into["amount"].sum() - credit[first]


def test_a_renter_names_no_property_tax(
    property_credit: Callable[..., Path], retire_alice_csv: Callable[[str], Path],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    """A renter counts a fifth of the rent, with no property tax tag to carry."""
    _tag_rule(retire_alice_csv, "housing", "tax:rent")
    property_credit(property_tax=None, rent="tax:rent")
    load_test_copy()
    property_credit(rent="tax:rent")  # the default property tax tag, carried by nothing
    with pytest.raises(LoadError, match="reads tag 'tax:property'"):
        load_test_copy()


def test_a_taxes_property_account_carries_the_property_tag_for_a_calculator(
    add_state_tax: Callable[..., None], add_tax: Callable[..., Path], tax_module: Callable[..., Path],
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], object],
) -> None:
    """#586: housing paid, untagged, into a taxes_property account is
    property tax, so a calculator's Tag param naming tax:property finds a
    carrier and the scenario loads."""
    add_state_tax("DC")
    tax_module("property_credit")
    accounts = pd.read_csv(retire_alice_csv("accounts.csv"), dtype=str, keep_default_na=False)
    accounts.loc[len(accounts)] = {
        **dict.fromkeys(accounts.columns, ""), "name": "Expense_Taxes_Property", "kind": "expense",
        "subkind": "taxes_property", "owner": "joint", "tax_treatment": "none",
        "opened": "2020-01-31", "meta_json": "{}"}
    accounts.to_csv(retire_alice_csv("accounts.csv"), index=False)
    path = retire_alice_csv("scheduled.csv")
    rows = pd.read_csv(path, dtype=str, keep_default_na=False)
    rows.loc[rows["name"] == "housing", "to_account"] = "Expense_Taxes_Property"
    rows.to_csv(path, index=False)
    add_tax("state", kind="property_credit")
    load_test_copy()
