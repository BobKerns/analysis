"""Tax jurisdiction files (#507, ADR-003 §4.15): ``taxes/federal.csv`` and
``taxes/state.csv``, each one row, in place of the retired
``tax_accumulator`` rule; the loader's checks, where the settlement falls
in its month, and the one-time migration of a data repo."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest

from analysis.sim.engine import run_scenario
from analysis.sim.loader import LoadError, LoadResult
from analysis.sim.registries import SCHEDULED_CSV_HEADER


@pytest.mark.parametrize(("name", "cells", "message"), [
    ("federal", {"kind": "state_tables"}, "kind 'state_tables' doesn't compute the federal tax"),
    ("federal", {"upstream": "federal"}, "upstream names the jurisdiction itself"),
    ("federal", {"upstream": "state"}, "upstream 'state' is not a jurisdiction of this scenario"),
    ("federal", {"source_chain": ""}, "source_chain names the chain or account the tax is paid from"),
    ("federal", {"source_chain": "Nowhere"}, "source_chain"),
    ("federal", {"destination": ""}, "destination names the expense account"),
    ("federal", {"destination": "Asset_Savings"}, "is a asset account; the tax is paid into an expense"),
    ("federal", {"schedule": "quarterly"}, "schedule 'quarterly' must be one of april"),
    ("federal", {"start": "2030-04", "end": "2029-04"}, "end 2029-04 is before start 2030-04"),
    ("federal", {"params_json": "[]"}, "params_json is not a JSON object"),
    ("federal", {"params_json": '{"year": "current"}'}, "params_json takes no 'year'"),
    ("federal", {"params_json": '{"scope": "state"}'}, "params_json takes no 'scope'"),
    ("local", {}, "a tax jurisdiction is one of federal, state"),
])
def test_the_loader_checks_a_tax_file(
    add_tax: Callable[..., Path], load_test_copy: Callable[[], LoadResult],
    name: str, cells: dict[str, str], message: str,
) -> None:
    add_tax(name, **({"kind": "federal"} if name == "local" else {}), **cells)
    with pytest.raises(LoadError, match=message):
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


def test_the_retired_rule_kind_names_the_migration(
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
    with pytest.raises(LoadError, match="scripts/migrate_507_taxes.py"):
        load_test_copy()


def test_a_child_scenarios_tax_file_replaces_its_parents_whole(
    add_tax: Callable[..., Path], write_tax: Callable[..., Path], test_child_scenario: Path,
    load_test_scenario: Callable[[str], Path], runs_dir: Path,
) -> None:
    add_tax()
    write_tax(test_child_scenario, start="2030-04")
    run = run_scenario(load_test_scenario("child"), runs_dir / "child.sqlite3", run_by="T",
                       allow_dirty_data=True, allow_dirty_code=True)
    with sqlite3.connect(run) as con:
        [(start,)] = con.execute("SELECT start FROM tax_jurisdiction WHERE name = 'federal'")
        paid = [d for (d,) in con.execute(
            "SELECT DISTINCT date FROM transfer_event WHERE reason_kind = 'tax' ORDER BY date")]
    assert start == "2030-04-30"
    assert paid and paid[0] >= "2030-04-30"
    assert all(d[5:7] == "04" for d in paid)


def _migration():
    path = Path(__file__).parents[1] / "scripts" / "migrate_507_taxes.py"
    spec = importlib.util.spec_from_file_location("migrate_507_taxes", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclass looks its module up there
    spec.loader.exec_module(module)
    return module


_HEADER = ("name,from_account,to_account,cadence,month_of_year,amount_rule_kind,priority,"
           "start,end,params_json,description")


_FED = ('fed,Asset_Checking,Expense_Taxes_Federal,annual,4,tax_accumulator,400,2027-04-30,,'
        '"{""year"":""previous"",""scope"":""federal""}",federal tax')


def test_the_migration_moves_each_tax_rule_to_its_file(
    new_scenario_file: Callable[[str, str, str], Path],
) -> None:
    scheduled = new_scenario_file("base", "scheduled.csv", (
        f"{_HEADER}\n"
        "living,Asset_Checking,Expense_Living,monthly,,fixed_amount,200,2026-01-31,,,\n"
        f"{_FED}\n"
        'st,Asset_Checking,Expense_Taxes_State,annual,4,tax_accumulator,,2027-04-30,2040-04-30,'
        '"{""scope"":""state"",""bracket_series"":""CA""}",state tax\n'))
    folder, data_dir = scheduled.parent, scheduled.parents[2]
    migration = _migration()

    planned = migration.plan(data_dir)
    assert planned.problems == []
    assert not (folder / "taxes").exists()  # a dry run writes nothing
    migration.apply(planned)

    assert scheduled.read_text().splitlines() == [
        _HEADER, "living,Asset_Checking,Expense_Living,monthly,,fixed_amount,200,2026-01-31,,,"]
    assert (folder / "taxes" / "federal.csv").read_text().splitlines() == [
        "kind,upstream,source_chain,destination,schedule,start,end,params_json,description",
        "federal,,Asset_Checking,Expense_Taxes_Federal,april,2027-04-30,,,federal tax",
    ]
    assert (folder / "taxes" / "state.csv").read_text().splitlines()[1] == (
        'state_tables,,Asset_Checking,Expense_Taxes_State,april,2027-04-30,2040-04-30,'
        '"{""bracket_series"":""CA""}",state tax')
    assert migration.plan(data_dir).problems == [
        f"{data_dir}: no tax_accumulator rules left; already migrated?"]


def test_the_migration_refuses_a_rule_that_settles_differently(
    new_scenario_file: Callable[[str, str, str], Path],
) -> None:
    scheduled = new_scenario_file("base", "scheduled.csv", (
        f"{_HEADER}\n"
        'fed,Asset_Checking,Expense_Taxes_Federal,annual,12,tax_accumulator,300,2027-12-31,,'
        '"{""year"":""current""}",\n'))
    problems = _migration().plan(scheduled.parents[2]).problems
    where = f"{scheduled} (fed)"
    assert problems == [
        f"{where}: year 'current': a jurisdiction settles the year before, each April",
        f"{where}: not annual in April: a jurisdiction settles each April",
        f"{where}: priority 300: a settlement takes the default rule's place (400)",
    ]


def test_the_migration_refuses_what_a_child_scenario_says_about_its_parents_rule(
    new_scenario_file: Callable[[str, str, str], Path],
) -> None:
    base = new_scenario_file("base", "scheduled.csv", f"{_HEADER}\n{_FED}\n")
    new_scenario_file("child", "scenario.csv", "name,parent_scenario\nchild,base\n")
    override = new_scenario_file("child", "scheduled.csv", "name,end\nfed,2035-04\n")
    calendar = new_scenario_file(
        "child", "calendar.csv", "name,event_kind,date,scheduled_name\n,retire_scheduled,2030-01,fed\n")
    assert _migration().plan(base.parents[2]).problems == [
        f"{calendar}: a row names tax rule 'fed', which becomes a jurisdiction file; "
        "change that row by hand",
        f"{override} (fed): overrides the tax rule 'fed' a parent defines; "
        "merge it into that rule's taxes/ file by hand",
    ]


def test_the_migration_drops_fill_brackets_tax_rule(
    new_scenario_file: Callable[[str, str, str], Path],
) -> None:
    scheduled = new_scenario_file("base", "scheduled.csv", (
        f"{_HEADER}\n{_FED}\n"
        'roth,Asset_IRA,Asset_Roth,annual,12,fill_bracket,,2027-12-31,,'
        '"{""scope"":""federal"",""tax_rule"":""fed"",""rate"":""22%""}",\n'))
    migration = _migration()
    planned = migration.plan(scheduled.parents[2])
    assert planned.problems == []
    migration.apply(planned)
    assert scheduled.read_text().splitlines()[1:] == [
        'roth,Asset_IRA,Asset_Roth,annual,12,fill_bracket,,2027-12-31,,'
        '"{""scope"":""federal"",""rate"":""22%""}",']
