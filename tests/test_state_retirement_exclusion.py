"""State retirement income exclusions (#486): a
``state_retirement_exclusion__<state>.csv`` table takes some of each
person's retirement income off the state's taxable income."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest

from analysis.sim.engine import _ExclusionGrant, _RetirementExclusion, run_scenario
from analysis.sim.loader import LoadError, load_scenario
from analysis.sim.registries import SCHEDULED_CSV_HEADER

HEADER = "effective_date,name,income,min_age,amount,filing_status,income_limit,phaseout,less_ss\n"


def _exclusions(repo: Path, rows: str) -> None:
    (repo / "aux" / "state_retirement_exclusion__CA.csv").write_text(
        HEADER + rows, encoding="utf-8")


def _ira_draw(retire_alice_csv: Callable[[str], Path], amount: str = "30000.00") -> None:
    """Alice (born 1965) takes ``amount`` out of her traditional IRA each
    December from 2026, the year she turns 61."""
    path = retire_alice_csv("scheduled.csv")
    scheduled = pd.read_csv(path, dtype=str, keep_default_na=False)
    scheduled = scheduled.reindex(columns=SCHEDULED_CSV_HEADER, fill_value="")
    scheduled.loc[len(scheduled)] = {
        **dict.fromkeys(SCHEDULED_CSV_HEADER, ""),
        "name": "ira_draw", "from_account": "Asset_TraditionalIRA_Alice",
        "from_lot": "default", "to_account": "Asset_Checking", "cadence": "annual",
        "month_of_year": "12", "amount_rule_kind": "fixed_amount", "amount": amount,
        "tax_character": "tax_deferred_out", "start": "2026-05-31", "priority": "200",
        "meta_json": "{}", "params_json": json.dumps({}),
    }
    scheduled.to_csv(path, index=False)


def _run(repo: Path, out: Path) -> tuple[sqlite3.Connection, list[str]]:
    load = load_scenario(repo, "retire_alice", intent_dir=out / "intent",
                         loaded_by="test", allow_dirty_data=True)
    run_path = out / "r.sqlite3"
    run_scenario(load.intent_path, run_path, run_by="test", allow_dirty_data=True)
    return sqlite3.connect(run_path), load.warnings


def _bucket(con: sqlite3.Connection, year: int, bucket: str) -> int:
    row = con.execute("SELECT amount FROM tax_year_summary WHERE year = ? AND bucket = ?",
                      (year, bucket)).fetchone()
    assert row is not None, bucket
    return row[0]


@pytest.fixture
def state_ira(test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
              add_state_tax: Callable[..., None]) -> Path:
    """retire_alice with a California tax rule and Alice's $30,000 IRA draw."""
    add_state_tax(bracket_series="CA")
    _ira_draw(retire_alice_csv)
    return test_data_dir


def test_exclusion_takes_a_persons_ira_draw_off_state_agi(
    state_ira: Path, tmp_path: Path,
) -> None:
    """Alice reaches 59½ in 2026, so $20,000 of her IRA draw comes off state
    AGI; Bill, 59, gets the age-65 exclusion of nothing."""
    con, _ = _run(state_ira, tmp_path / "without")
    try:
        before = _bucket(con, 2026, "state_adjusted_gross_income")
        assert _bucket(con, 2026, "state_retirement_exclusion") == 0
    finally:
        con.close()
    _exclusions(state_ira, (
        "2025-01,pension,pension;retirement_account,59.5,20000,,,,\n"
        "2025-01,age,all,65,12000,,,,\n"
    ))
    con, _ = _run(state_ira, tmp_path / "with")
    try:
        assert _bucket(con, 2026, "state_retirement_exclusion") == 20_000_00
        assert _bucket(con, 2026, "state_adjusted_gross_income") == before - 20_000_00
    finally:
        con.close()


def test_exclusion_waits_for_its_age(state_ira: Path, tmp_path: Path) -> None:
    """Alice is 61 at the end of 2026, short of 62; the tier for 62 starts in
    2027, and the higher tier at 65 in 2030."""
    _exclusions(state_ira, (
        "2025-01,retirement,retirement_account,62,5000,,,,\n"
        "2025-01,retirement,retirement_account,65,25000,,,,\n"
    ))
    con, _ = _run(state_ira, tmp_path)
    try:
        assert _bucket(con, 2026, "state_retirement_exclusion") == 0
        assert _bucket(con, 2027, "state_retirement_exclusion") == 5_000_00
        assert _bucket(con, 2030, "state_retirement_exclusion") == 25_000_00
    finally:
        con.close()


def test_an_unlimited_exclusion_takes_it_all(state_ira: Path, tmp_path: Path) -> None:
    """A blank amount excludes all the income it covers."""
    _exclusions(state_ira, "2025-01,retirement,retirement_account,,,,,,\n")
    con, _ = _run(state_ira, tmp_path)
    try:
        assert _bucket(con, 2026, "state_retirement_exclusion") == 30_000_00
    finally:
        con.close()


def test_an_income_limit_takes_the_exclusion_away(state_ira: Path, tmp_path: Path) -> None:
    """The household's two salaries are far over a $100,000 cliff."""
    _exclusions(state_ira, "2025-01,retirement,retirement_account,,,,100000,,\n")
    con, _ = _run(state_ira, tmp_path)
    try:
        assert _bucket(con, 2026, "state_retirement_exclusion") == 0
    finally:
        con.close()


def test_grant_income_test() -> None:
    """Up to the limit all of it; past it nothing at a cliff, or a straight
    line down to nothing over the phaseout."""
    cliff = _ExclusionGrant(10_000, 0, income_limit=100_000, phaseout=None)
    assert cliff.after_income_test(100_000) == (10_000, 0)
    assert cliff.after_income_test(100_001) == (0, 0)
    phased = _ExclusionGrant(10_000, 4_000, income_limit=100_000, phaseout=20_000)
    assert phased.after_income_test(110_000) == (5_000, 2_000)
    assert phased.after_income_test(130_000) == (0, 0)
    assert _ExclusionGrant(10_000, 0, None, None).after_income_test(10**9) == (10_000, 0)


def test_exclusion_never_exceeds_the_income() -> None:
    """Gains come off the long-term gain first, and the whole never takes
    income below zero."""
    exclusion = _RetirementExclusion((
        _ExclusionGrant(8_000, 6_000, None, None), _ExclusionGrant(5_000, 0, None, None)))
    assert exclusion.excluded(10_000, 4_000) == (9_000, 4_000)
    assert exclusion.excluded(2_000, 1_000) == (2_000, 1_000)
    assert _RetirementExclusion(()).excluded(5, 5) == (0, 0)


@pytest.mark.parametrize(("row", "message"), [
    ("2025-01,,pension,,,,,,\n", "name is blank"),
    ("2025-01,x,pensions,,,,,,\n", "must list one or more of"),
    ("2025-01,x,pension,sixty,,,,,\n", "min_age 'sixty'"),
    ("2025-01,x,pension,,,Married,,,\n", "filing_status 'Married'"),
    ("2025-01,x,pension,,,,,5000,\n", "a phaseout needs the income_limit"),
    ("2025-01,x,pension,,,,,,yes\n", "less_ss needs an amount"),
    ("2025-01,x,pension,65,10,,,,\n2025-01,x,pension,65,20,,,,\n", "two rows for the same"),
])
def test_load_rejects_a_malformed_row(
    test_data_dir: Path, tmp_path: Path, row: str, message: str,
) -> None:
    _exclusions(test_data_dir, row)
    with pytest.raises(LoadError, match=message):
        load_scenario(test_data_dir, "retire_alice", intent_dir=tmp_path / "intent",
                      loaded_by="test", allow_dirty_data=True)


def test_less_ss_reduces_the_exclusion_by_the_persons_social_security(
    state_ira: Path, tmp_path: Path,
) -> None:
    """Alice claims Social Security at 67, in 2032; from then her $40,000
    exclusion is less her benefits, as Maine's and Maryland's are."""
    _exclusions(state_ira, "2025-01,pension,retirement_account,,40000,,,,yes\n")
    con, _ = _run(state_ira, tmp_path)
    try:
        assert _bucket(con, 2031, "state_retirement_exclusion") == 30_000_00
        ss = con.execute(
            "SELECT SUM(te.amount) FROM transfer_event te JOIN account a ON a.id = te.from_account "
            "WHERE a.name = 'Income_SS_Alice' AND substr(te.date, 1, 4) = '2033'").fetchone()[0]
        assert 0 < ss
        assert _bucket(con, 2033, "state_retirement_exclusion") == max(0, 40_000_00 - ss)
    finally:
        con.close()


def test_joint_income_is_split_between_the_spouses(
    state_ira: Path, tmp_path: Path,
) -> None:
    """The joint checking account's interest is half Alice's and half Bill's,
    so a $1 exclusion each takes $2 of it."""
    _exclusions(state_ira, "2025-01,interest,interest,,1,,,,\n")
    con, _ = _run(state_ira, tmp_path)
    try:
        assert _bucket(con, 2027, "state_retirement_exclusion") == 2_00
    finally:
        con.close()


def test_a_move_takes_the_new_states_exclusions(
    state_ira: Path, tmp_path: Path, append_calendar_row: Callable[..., None],
) -> None:
    """A swap of state_brackets swaps the exclusions with it; a state with
    none excludes nothing."""
    _exclusions(state_ira, "2025-01,retirement,retirement_account,,,,,,\n")
    for state in ("WA", "NY"):
        (state_ira / "aux" / f"state_brackets__{state}.csv").write_text(
            "effective_date,filing_status,bracket_index,floor,marginal_rate\n"
            "2026-01-31,MFJ,0,0.00,1%\n", encoding="utf-8")
    (state_ira / "aux" / "state_retirement_exclusion__NY.csv").write_text(
        HEADER + "2025-01,pension,retirement_account,59.5,20000,,,,\n", encoding="utf-8")
    for date, state in (("2028-03", "WA"), ("2030-03", "NY")):
        append_calendar_row(
            date=date, kind="swap_aux_table", series="CA",
            params_json=json.dumps({"aux_kind": "state_brackets", "source_series": state}))
    con, _ = _run(state_ira, tmp_path)
    try:
        # Each year is figured the April after it, with the tables then.
        assert _bucket(con, 2026, "state_retirement_exclusion") == 30_000_00
        assert _bucket(con, 2028, "state_retirement_exclusion") == 0
        assert _bucket(con, 2029, "state_retirement_exclusion") == 20_000_00
    finally:
        con.close()


def test_tax_report_shows_the_exclusion(state_ira: Path, tmp_path: Path) -> None:
    """``sim report tax`` adds a state_exclusion column before state_agi,
    only when some year has one."""
    from analysis.sim.reports import tax_by_year
    from analysis.sim.connection import RunConnection

    con, _ = _run(state_ira, tmp_path / "without")
    con.close()
    assert "state_exclusion" not in tax_by_year(RunConnection(tmp_path / "without" / "r.sqlite3"))
    _exclusions(state_ira, "2025-01,retirement,retirement_account,,,,,,\n")
    con, _ = _run(state_ira, tmp_path / "with")
    con.close()
    frame = tax_by_year(RunConnection(tmp_path / "with" / "r.sqlite3"))
    columns = list(frame.columns)
    assert columns.index("state_exclusion") == columns.index("state_agi") - 1
    assert frame.loc[2026, "state_exclusion"] == 30_000
