"""Filing status follows the taxpayers' lifespans (no filing_status.csv row needed)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from analysis.sim.engine import EngineError, _active_filing_status, run_scenario
from analysis.sim.loader import LoadResult

TAXPAYERS = {"alice", "bill"}


def _aux(status: str = "MFJ") -> dict:
    return {("filing_status", None): [("2026-01-31", {"filing_status": status})]}


@pytest.mark.parametrize(("tax_year", "expected"), [
    (2035, "MFJ"),
    (2036, "MFJ"),      # the year of death: the survivor may still file jointly
    (2037, "Single"),
    (2040, "Single"),
])
def test_a_taxpayers_death_makes_the_survivor_single_from_the_next_year(
    tax_year: int, expected: str,
) -> None:
    assert _active_filing_status(
        _aux(), "filing_status", tax_year, 1,
        person_lifespan={"alice": "2036-08-01"}, taxpayers=TAXPAYERS,
    ) == expected


def test_only_taxpayers_count_and_only_joint_or_separate_filers_change() -> None:
    lifespan = {"child": "2030-01-01"}
    assert _active_filing_status(
        _aux(), "filing_status", 2035, 1, person_lifespan=lifespan, taxpayers=TAXPAYERS,
    ) == "MFJ"
    for base, expected in (("MFJ", "Single"), ("MFS", "Single"), ("Single", "Single")):
        assert _active_filing_status(
            _aux(base), "filing_status", 2035, 1,
            person_lifespan={"bill": "2030-01-01"}, taxpayers=TAXPAYERS,
        ) == expected


def test_no_filing_status_row_is_an_error_unless_a_taxpayer_died() -> None:
    with pytest.raises(EngineError, match="no filing_status row"):
        _active_filing_status({}, "filing_status", 2030, 1, taxpayers=TAXPAYERS)
    assert _active_filing_status(
        {}, "filing_status", 2035, 1, person_lifespan={"bill": "2030-01-01"}, taxpayers=TAXPAYERS,
    ) == "Single"


def test_the_standard_deduction_switches_after_the_year_of_death(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult],
    runs_dir: Path,
) -> None:
    import pandas as pd

    # Only 2027-2030 are asserted on; retire_alice's household budget isn't
    # sized to survive losing a person's income for the scenario's full
    # declared range (2026-2040) on top of a new annual tax payment.
    scenario_csv = retire_alice_csv("scenario.csv")
    scenario_df = pd.read_csv(scenario_csv, dtype=str, keep_default_na=False)
    scenario_df.loc[0, "end"] = "2031-12-31"
    scenario_df.to_csv(scenario_csv, index=False)

    retire_alice_csv("lifespan.csv").write_text("person,final_month\nalice,2028-06-01\n")
    scheduled = retire_alice_csv("scheduled.csv")

    df = pd.read_csv(scheduled, dtype=str, keep_default_na=False)
    df.loc[len(df)] = {
        **dict.fromkeys(df.columns, ""), "name": "tax_federal_prev_year",
        "from_account": "Asset_Checking", "to_account": "Expense_Taxes_Federal",
        "cadence": "annual", "month_of_year": "4", "amount_rule_kind": "tax_accumulator",
        "tax_character": "none", "start": "2027-04-30", "priority": "400", "meta_json": "{}",
        "params_json": '{"year":"previous","scope":"federal"}',
    }
    df.to_csv(scheduled, index=False)

    run = run_scenario(load_test_copy().intent_path, runs_dir / "r.sqlite3", run_by="T",
                       allow_dirty_data=True, allow_dirty_code=True)
    con = sqlite3.connect(run)
    try:
        deduction = dict(con.execute(
            "SELECT year, amount FROM tax_year_summary "
            "WHERE bucket = 'federal_standard_deduction'"
        ).fetchall())
    finally:
        con.close()
    assert deduction[2027] == deduction[2028] == 32_200_00  # MFJ, including the year of death
    assert deduction[2029] == deduction[2030] == 16_100_00  # Single afterward
