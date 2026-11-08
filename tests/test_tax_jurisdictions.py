"""Tax jurisdiction files (#507, ADR-003 §4.15): ``taxes/federal.csv`` and
``taxes/state.csv``, each one row, in place of the retired
``tax_accumulator`` rule; the loader's checks, and where the settlement falls
in its month."""

from __future__ import annotations

import sqlite3
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
    with sqlite3.connect(run) as con:
        [(start,)] = con.execute("SELECT start FROM tax_jurisdiction WHERE name = 'federal'")
        paid = [d for (d,) in con.execute(
            "SELECT DISTINCT date FROM transfer_event WHERE reason_kind = 'tax' ORDER BY date")]
    assert start == "2030-04-30"
    assert paid and paid[0] >= "2030-04-30"
    assert all(d[5:7] == "04" for d in paid)
