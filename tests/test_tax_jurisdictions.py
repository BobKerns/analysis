"""Tax jurisdiction files (#507, ADR-003 §4.15): ``taxes/federal.csv`` and
``taxes/state.csv``, each one row, in place of the retired
``tax_accumulator`` rule; the loader's checks, and where the settlement falls
in its month."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest

from analysis.sim.engine import run_scenario
from analysis.sim.loader import LoadError, LoadResult
from analysis.sim.registries import SCHEDULED_CSV_HEADER


@pytest.mark.parametrize(("name", "cells", "message"), [
    ("federal", {"kind": "state_tables"},
     "kind 'state_tables' computes the state tax; the federal tax takes kind federal"),
    ("state", {"kind": "nope"}, "no tax calculator kind 'nope'; the state tax takes kind state_tables"),
    ("federal", {"params_json": '{"bracket_aux": 3}'}, "parameter 'bracket_aux': 3 must be a string"),
    ("federal", {"params_json": '{"brackets": "x"}'}, "unknown parameter 'brackets'"),
    ("federal", {"upstream": "federal"}, "upstream names the jurisdiction itself"),
    ("federal", {"upstream": "state"}, "upstream must be blank or empty: the federal calculator builds on no"),
    ("federal", {"source_chain": ""}, "source_chain names the chain or account the tax is paid from"),
    ("federal", {"source_chain": "Nowhere"}, "source_chain"),
    ("federal", {"destination": ""}, "destination names the expense account"),
    ("federal", {"destination": "Asset_Savings"}, "is a asset account; the tax is paid into an expense"),
    ("federal", {"schedule": "quarterly"}, "schedule 'quarterly' must be one of april"),
    ("federal", {"start": "2030-04", "end": "2029-04"}, "end 2029-04 is before start 2030-04"),
    ("federal", {"params_json": "[]"}, "params_json is not a JSON object"),
    ("federal", {"params_json": '{"year": "current"}'}, "params_json takes no 'year'"),
    ("federal", {"params_json": '{"scope": "state"}'}, "params_json takes no 'scope'"),
    ("county", {}, "a tax jurisdiction is one of federal, state, local"),
])
def test_the_loader_checks_a_tax_file(
    add_tax: Callable[..., Path], load_test_copy: Callable[[], LoadResult],
    name: str, cells: dict[str, str], message: str,
) -> None:
    add_tax(name, **({"kind": "federal"} if name == "county" else {}), **cells)
    with pytest.raises(LoadError, match=message):
        load_test_copy()


def test_a_state_needs_the_federal_return_it_starts_from(
    add_state_tax: Callable[..., None], retire_alice_csv: Callable[[str], Path],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    add_state_tax()
    (retire_alice_csv("") / "taxes" / "federal.csv").unlink()
    with pytest.raises(LoadError, match=r"taxes/state\.csv line 2: the state_tables calculator "
                                        r"builds on the federal tax, .*`sim tax add federal`"):
        load_test_copy()


def test_a_states_upstream_column_may_only_spell_out_federal(
    add_state_tax: Callable[..., None], add_tax: Callable[..., Path],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    add_state_tax()
    add_tax("state", upstream="federal")
    load_test_copy()
    add_tax("state", upstream="state")
    with pytest.raises(LoadError, match="upstream must be blank or federal"):
        load_test_copy()


def test_a_tax_file_holds_one_row(
    retire_alice_csv: Callable[[str], Path], add_tax: Callable[..., Path],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    path = add_tax()
    path.write_text(path.read_text() + path.read_text().splitlines()[1] + "\n")
    with pytest.raises(LoadError, match="a tax file holds one row"):
        load_test_copy()


def test_a_template_tax_file_is_skipped(
    add_tax: Callable[..., Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    add_tax("_example", kind="nothing")
    load_test_copy()


def test_the_retired_rule_kind_names_the_jurisdiction_files(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    path = retire_alice_csv("scheduled.csv")
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    frame = frame.reindex(columns=SCHEDULED_CSV_HEADER, fill_value="")
    frame.loc[len(frame)] = {
        **dict.fromkeys(SCHEDULED_CSV_HEADER, ""), "name": "tax_federal",
        "from_account": "Asset_Checking", "to_account": "Expense_Taxes_Federal",
        "cadence": "annual", "month_of_year": "4", "amount_rule_kind": "tax_accumulator",
        "tax_character": "none", "start": "2027-04-30", "meta_json": "{}"}
    frame.to_csv(path, index=False)
    with pytest.raises(LoadError, match="income tax is a jurisdiction file now"):
        load_test_copy()


def test_a_child_scenarios_tax_file_replaces_its_parents_whole(
    add_tax: Callable[..., Path], write_tax: Callable[..., Path], test_child_scenario: Path,
    load_test_scenario: Callable[[str], Path], runs_dir: Path,
) -> None:
    add_tax()
    write_tax(test_child_scenario, start="2030-04")
    run = run_scenario(load_test_scenario("child"), runs_dir / "child.sqlite3", run_by="T",
                       allow_dirty_data=True, allow_dirty_code=True)
    with closing(sqlite3.connect(run)) as con:
        [(start,)] = con.execute("SELECT start FROM tax_jurisdiction WHERE name = 'federal'")
        paid = [d for (d,) in con.execute(
            "SELECT DISTINCT date FROM transfer_event WHERE reason_kind = 'tax' ORDER BY date")]
    assert start == "2030-04-30"
    assert paid and paid[0] >= "2030-04-30"
    assert all(d[5:7] == "04" for d in paid)


def test_each_tax_is_paid_into_an_account_of_its_own_kind(
    add_tax: Callable[..., Path], add_state_tax: Callable[..., None],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    """#586: the federal tax pays into a taxes_federal account, the state's
    into taxes_state, and no two taxes share one."""
    add_state_tax()
    add_tax("state", destination="Expense_Taxes_Federal")
    with pytest.raises(LoadError) as raised:
        load_test_copy()
    issues = "\n".join(raised.value.issues)
    assert ("taxes/state.csv line 2: destination 'Expense_Taxes_Federal' has subkind taxes_federal; "
            "the state tax is paid into an expense account of subkind taxes_state") in issues
    assert "the federal and state taxes are both paid into 'Expense_Taxes_Federal'" in issues


def test_a_local_tax_needs_its_own_destination(
    add_tax: Callable[..., Path], add_state_tax: Callable[..., None],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    """#586: a blank destination for the local tax no longer takes the
    state's."""
    add_state_tax()
    add_tax("local", destination="")
    with pytest.raises(LoadError, match="taxes/local.csv line 2: destination names the expense account"):
        load_test_copy()


@pytest.mark.parametrize(("cells", "why"), [
    ({"tax_character": "property_tax"}, "tax_character property_tax; pay it into a taxes_property account"),
    ({"tags": "tax:property"}, "tag tax:property; pay it into a taxes_property account"),
    ({"tags": "tax:charity"}, "tag tax:charity; pay it into another expense account"),
    ({"tax_character": "business_expense"}, "tax_character business_expense"),
])
def test_only_income_tax_payments_go_into_an_income_tax_account(
    add_tax: Callable[..., Path], append_scheduled_row: Callable[..., None],
    load_test_copy: Callable[[], LoadResult], cells: dict[str, str], why: str,
) -> None:
    """#586: a rule paying into an income-tax account must be a tax payment:
    property tax, an itemized deduction's spending or a business expense is
    refused there."""
    add_tax()
    append_scheduled_row(name="not_tax", from_account="Asset_Checking",
                         to_account="Expense_Taxes_Federal", amount_rule_kind="fixed_amount",
                         amount="100.00", start="2027-01-31", **{"tax_character": "none", **cells})
    with pytest.raises(LoadError, match="Expense_Taxes_Federal is a taxes_federal account, which takes "
                       "only income tax payments") as raised:
        load_test_copy()
    assert why in "\n".join(raised.value.issues)


def test_withholding_into_an_income_tax_account_loads(
    add_tax: Callable[..., Path], append_scheduled_row: Callable[..., None],
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    """Withholding and an estimated payment are tax payments (#586)."""
    add_tax()
    append_scheduled_row(name="withholding", from_account="Asset_Checking",
                         to_account="Expense_Taxes_Federal", amount_rule_kind="fixed_amount",
                         amount="100.00", start="2027-01-31", tax_character="none")
    append_calendar_row(date="2027-06-30", kind="one_off_transfer", name="estimate",
                        account_1="Asset_Checking", account_2="Expense_Taxes_Federal",
                        amount="500.00")
    load_test_copy()


def test_a_calendar_row_of_property_tax_into_an_income_tax_account_is_refused(
    add_tax: Callable[..., Path], append_calendar_row: Callable[..., None],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    add_tax()
    append_calendar_row(date="2027-06-30", kind="one_off_transfer", name="county",
                        account_1="Asset_Checking", account_2="Expense_Taxes_Federal",
                        amount="500.00", tax_character="property_tax")
    with pytest.raises(LoadError, match="calendar.csv.*Expense_Taxes_Federal is a taxes_federal account"):
        load_test_copy()
