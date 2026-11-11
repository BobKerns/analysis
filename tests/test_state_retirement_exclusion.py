"""State retirement income exclusions (#486): a
``state_retirement_exclusion__<state>.csv`` table takes some of each
person's retirement income off the state's taxable income."""

from __future__ import annotations

import json
from contextlib import closing
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest

from analysis.sim.engine import run_scenario
from analysis.sim.loader import LoadError, load_scenario
from analysis.sim.registries import (
    GROWTH_COUNTERPARTY_ACCOUNTS,
    SCHEDULED_CSV_HEADER,
    STATE_EXCLUSION_INCOME,
)
from analysis.sim.tax_api import TaxYear
from analysis.sim.taxes.common import (
    ExclusionGrant,
    RetirementExclusion,
    person_income,
    taxable_social_security,
)

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
    add_state_tax("CA")
    _ira_draw(retire_alice_csv)
    return test_data_dir


def test_exclusion_takes_a_persons_ira_draw_off_state_agi(
    state_ira: Path, tmp_path: Path,
) -> None:
    """Alice reaches 59½ in 2026, so $20,000 of her IRA draw comes off state
    AGI; Bill, 59, gets the age-65 exclusion of nothing."""
    con, _ = _run(state_ira, tmp_path / "without")
    try:
        before = _bucket(con, 2026, "state_agi")
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
        assert _bucket(con, 2026, "state_agi") == before - 20_000_00
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


def _pools(**by_kind: int) -> dict[str, dict[str, int]]:
    """Alice's income by kind, the rest zero."""
    return {"alice": {kind: by_kind.get(kind, 0) for kind in STATE_EXCLUSION_INCOME}}


def _grant(*income: str, cap: int | None = None, limit: int | None = None,
           phaseout: int | None = None) -> ExclusionGrant:
    return ExclusionGrant("alice", income, cap, limit, phaseout)


def test_grant_income_test() -> None:
    """Up to the limit all of it; past it nothing at a cliff, or a straight
    line down to nothing over the phaseout."""
    cliff = _grant(limit=100_000)
    assert cliff.after_income_test(10_000, 100_000) == 10_000
    assert cliff.after_income_test(10_000, 100_001) == 0
    phased = _grant(limit=100_000, phaseout=20_000)
    assert phased.after_income_test(10_000, 110_000) == 5_000
    assert phased.after_income_test(10_000, 130_000) == 0
    assert _grant().after_income_test(10_000, 10**9) == 10_000


def test_exclusion_never_exceeds_the_income() -> None:
    """Gains come off the long-term gain first, and the whole never takes
    income below zero."""
    exclusion = RetirementExclusion(
        _pools(capital_gain=6_000, pension=10_000),
        (_grant("capital_gain", "pension", cap=8_000), _grant("pension", cap=5_000)),
        net_gain=6_000)
    assert exclusion.excluded(10_000, 4_000) == (9_000, 4_000)
    assert exclusion.excluded(2_000, 1_000) == (2_000, 1_000)
    assert RetirementExclusion().excluded(5, 5) == (0, 0)


def test_a_gain_a_loss_offset_is_not_excluded() -> None:
    """$10,000 of gain legs that a carried loss wiped out leave nothing to
    exclude, and nothing comes off ordinary income in their place."""
    exclusion = RetirementExclusion(
        _pools(capital_gain=10_000), (_grant("capital_gain"),), net_gain=0)
    assert exclusion.excluded(50_000, 0) == (0, 0)


def test_a_denied_exclusion_leaves_its_income_for_the_next() -> None:
    """A retirement exclusion over its $20,000 cliff takes nothing, so the
    age exclusion after it still covers $10,000 of the same IRA income."""
    exclusion = RetirementExclusion(
        _pools(retirement_account=30_000),
        (_grant("retirement_account", limit=20_000), _grant(*STATE_EXCLUSION_INCOME, cap=10_000)))
    assert exclusion.excluded(30_000, 0) == (10_000, 0)
    # Below the cliff the first takes all of it, leaving the second nothing.
    assert exclusion.excluded(15_000, 0) == (15_000, 0)


@pytest.mark.parametrize(("row", "message"), [
    ("2025-01,,pension,,,,,,\n", "name is blank"),
    ("2025-01,x,pensions,,,,,,\n", "must list one or more of"),
    ("2025-01,x,pension,sixty,,,,,\n", "min_age 'sixty'"),
    ("2025-01,x,pension,-0.01,,,,,\n", "min_age '-0.01'"),
    ("2025-01,x,pension,1e309,,,,,\n", "min_age '1e309'"),
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
    state_ira: Path, tmp_path: Path, add_residence: Callable[..., Path],
) -> None:
    """A year reads the exclusions of the state it is filed in; a state
    with none excludes nothing."""
    _exclusions(state_ira, "2025-01,retirement,retirement_account,,,,,,\n")
    for state in ("WA", "NY"):
        (state_ira / "aux" / f"state_brackets__{state}.csv").write_text(
            "effective_date,filing_status,bracket_index,floor,marginal_rate\n"
            "2026-01-31,MFJ,0,0.00,1%\n", encoding="utf-8")
    (state_ira / "aux" / "state_retirement_exclusion__NY.csv").write_text(
        HEADER + "2025-01,pension,retirement_account,59.5,20000,,,,\n", encoding="utf-8")
    for date, state in (("2028-03", "WA"), ("2030-03", "NY")):
        add_residence(date, state)
    con, _ = _run(state_ira, tmp_path)
    try:
        # Each year is filed where the household lives on its December 31,
        # though the April settling it comes after the next move (#498).
        assert _bucket(con, 2026, "state_retirement_exclusion") == 30_000_00
        assert _bucket(con, 2027, "state_retirement_exclusion") == 30_000_00
        assert _bucket(con, 2028, "state_retirement_exclusion") == 0
        assert _bucket(con, 2029, "state_retirement_exclusion") == 0
        assert _bucket(con, 2030, "state_retirement_exclusion") == 20_000_00
    finally:
        con.close()


def test_tax_report_shows_the_exclusion(state_ira: Path, tmp_path: Path) -> None:
    """``sim report tax`` adds a state_exclusion column before state_agi,
    only when some year has one."""
    from analysis.sim.reports import tax_by_year
    from analysis.sim.connection import RunConnection

    con, _ = _run(state_ira, tmp_path / "without")
    con.close()
    with RunConnection(tmp_path / "without" / "r.sqlite3") as rc:
        assert "state_exclusion" not in tax_by_year(rc)
    _exclusions(state_ira, "2025-01,retirement,retirement_account,,,,,,\n")
    con, _ = _run(state_ira, tmp_path / "with")
    con.close()
    with RunConnection(tmp_path / "with" / "r.sqlite3") as rc:
        frame = tax_by_year(rc)
    columns = list(frame.columns)
    assert columns.index("state_exclusion") == columns.index("state_agi") - 1
    assert frame.loc[2026, "state_exclusion"] == 30_000


def test_hobby_receipts_stay_income_an_exclusion_covers(
    business_scenarios: Path, test_data_dir: Path, tmp_path: Path,
    tax_year_of: Callable[..., TaxYear],
) -> None:
    """A hobby's expenses aren't deducted (#231), so they don't cancel its
    receipts in the income an exclusion of ``other`` income covers; a real
    business's do."""
    other = {}
    for scenario in ("alice_business", "alice_business_hobby"):
        load = load_scenario(test_data_dir, scenario, intent_dir=tmp_path / scenario,
                             loaded_by="test", allow_dirty_data=True)
        run_path = tmp_path / f"{scenario}.sqlite3"
        run_scenario(load.intent_path, run_path, run_by="test", allow_dirty_data=True)
        with closing(sqlite3.connect(run_path)) as con:
            receipts = con.execute(
                "SELECT SUM(amount) FROM transfer_event WHERE tax_character = 'business_income' "
                "AND substr(date, 1, 4) = '2032'").fetchone()[0]
            pools, _ = person_income(tax_year_of(con, 2032), ["alice", "bill"])
        other[scenario] = pools["alice"]["other"]
    assert receipts > 0
    assert other["alice_business_hobby"] >= receipts > other["alice_business"]


def test_an_offset_gain_does_not_use_up_a_cap() -> None:
    """$10,000 of gain a loss wiped out leaves the whole $10,000 cap of a
    gains-and-pension exclusion for the pension."""
    exclusion = RetirementExclusion(
        _pools(capital_gain=10_000, pension=10_000),
        (_grant("capital_gain", "pension", cap=10_000),), net_gain=0)
    assert exclusion.excluded(10_000, 0) == (10_000, 0)


def test_a_move_to_a_state_whose_exclusions_start_later(
    state_ira: Path, tmp_path: Path, add_residence: Callable[..., Path],
) -> None:
    """A new state's exclusion table dated after the move takes over on its
    own date, and the move doesn't fail for it."""
    _exclusions(state_ira, "2025-01,retirement,retirement_account,,,,,,\n")
    (state_ira / "aux" / "state_brackets__NY.csv").write_text(
        "effective_date,filing_status,bracket_index,floor,marginal_rate\n"
        "2026-01-31,MFJ,0,0.00,1%\n", encoding="utf-8")
    (state_ira / "aux" / "state_retirement_exclusion__NY.csv").write_text(
        HEADER + "2030-01,pension,retirement_account,,5000,,,,\n", encoding="utf-8")
    add_residence("2028-03", "NY")
    con, _ = _run(state_ira, tmp_path)
    try:
        assert _bucket(con, 2028, "state_retirement_exclusion") == 0
        assert _bucket(con, 2030, "state_retirement_exclusion") == 5_000_00
    finally:
        con.close()


def test_a_secondary_persons_income_is_theirs(state_ira: Path, tmp_path: Path) -> None:
    """A ``secondary`` person is one of the household's people too, so
    Alice's IRA draw stays hers when she is marked secondary, not split
    with Bill, who is short of 59½."""
    persons = state_ira / "persons.csv"
    persons.write_text(persons.read_text(encoding="utf-8").replace(
        "alice,Alice (fake),1965-03-15,f,primary", "alice,Alice (fake),1965-03-15,f,secondary"),
        encoding="utf-8")
    assert "secondary" in persons.read_text(encoding="utf-8")
    _exclusions(state_ira, "2025-01,pension,retirement_account,59.5,20000,,,,\n")
    con, _ = _run(state_ira, tmp_path)
    try:
        assert _bucket(con, 2026, "state_retirement_exclusion") == 20_000_00
    finally:
        con.close()


def test_an_excluded_short_term_gain_leaves_the_short_term_rates() -> None:
    """Gain left out past the long-term gain is short-term gain, so a
    state's short-term rates (#485) don't reach the wages left behind."""
    exclusion = RetirementExclusion(
        _pools(capital_gain=20_000), (_grant("capital_gain"),), net_gain=20_000)
    assert exclusion.split(120_000, 0) == (20_000, 0, 20_000)
    # Long-term gain goes first; only the rest is short-term.
    assert exclusion.split(115_000, 5_000) == (15_000, 5_000, 15_000)


def test_gain_an_estimate_takes_away_is_not_excluded() -> None:
    """The drawdown estimate replaces posted gain: with the $20,000 gain
    taken out, the exclusion leaves the wages alone."""
    exclusion = RetirementExclusion(
        _pools(capital_gain=20_000), (_grant("capital_gain"),), net_gain=20_000)
    assert exclusion.split(100_000, 0, net_gain=0) == (0, 0, 0)
    assert exclusion.split(100_000, 10_000, net_gain=10_000) == (0, 10_000, 0)


def test_a_loss_below_the_gain_leaves_no_negative_exclusion() -> None:
    """A $10,000 business loss and $20,000 of gain: $10,000 of income, all
    of it gain, comes off the gain and nothing off ordinary income."""
    exclusion = RetirementExclusion(
        _pools(capital_gain=20_000), (_grant("capital_gain"),), net_gain=20_000)
    assert exclusion.split(-10_000, 20_000) == (0, 10_000, 0)


def test_state_taxable_social_security_reads_income_before_the_exclusion(
    state_ira: Path, tmp_path: Path,
) -> None:
    """A state that taxes Social Security (#484) starts from the federally
    taxable amount, which the state's own retirement exclusion doesn't
    change: the IRA draw it leaves out still counts toward it."""
    (state_ira / "aux" / "state_social_security__CA.csv").write_text(
        "effective_date,filing_status,exclusion_percent,phaseout_start,phaseout_end,"
        "phaseout_rate\n2020-01-31,MFJ,0%,,,\n2020-01-31,Single,0%,,,\n", encoding="utf-8")
    _exclusions(state_ira, "2025-01,retirement,retirement_account,,,,,,\n")
    con, _ = _run(state_ira, tmp_path)
    try:
        year = 2033
        excluded = _bucket(con, year, "state_retirement_exclusion")
        assert excluded == 30_000_00
        ss = _bucket(con, year, "state_ss_benefits")
        ordinary = _bucket(con, year, "state_ordinary_income")
        ltcg = _bucket(con, year, "state_ltcg_taxed")
        tiers = {"tier1": 32_000_00, "tier2": 44_000_00}  # MFJ, prototype aux
        expected = taxable_social_security(
            ordinary_income=ordinary + excluded, ltcg_income=ltcg, ss_income=ss,
            tier1=tiers["tier1"], tier2=tiers["tier2"])
        after = taxable_social_security(
            ordinary_income=ordinary, ltcg_income=ltcg, ss_income=ss,
            tier1=tiers["tier1"], tier2=tiers["tier2"])
        assert expected != after  # the year shows the difference
        assert _bucket(con, year, "state_ss_taxable") == expected
    finally:
        con.close()


def test_hsa_flows_are_not_retirement_income(
    ledger_con: sqlite3.Connection, tax_year_of: Callable[..., TaxYear],
) -> None:
    """An HSA withdrawal is ``other`` income, not a retirement account's;
    where the state taxes the HSA like a brokerage account (#490) it left
    the HSA's flows out of income, so they aren't income to exclude."""
    ledger_con.executemany(
        "INSERT INTO account(id, name, kind, subkind) VALUES (?, ?, ?, ?)",
        [(1, "Asset_Checking", "asset", "cash"), (2, "Asset_HSA", "asset", "hsa"),
         (3, "Expense_Medical", "expense", "healthcare"),
         (4, "Asset_TraditionalIRA", "asset", "retirement")])
    for frm, to, amount, character in ((1, 2, 8_000_00, "tax_deferred_in"),
                                       (2, 3, 12_000_00, "tax_deferred_out"),
                                       (4, 1, 5_000_00, "tax_deferred_out")):
        ledger_con.execute(
            "INSERT INTO transfer_event(step_id, date, from_account, to_account, from_lot_id, "
            "to_lot_id, amount, requested_cents, remainder_cents, tax_character, reason_kind) "
            "VALUES (1, '2026-06-30', ?, ?, ?, ?, ?, ?, 0, ?, 'rule')",
            (frm, to, frm, to, amount, amount, character))
    pools, _ = person_income(tax_year_of(ledger_con, 2026), ["alice"])
    assert (pools["alice"]["retirement_account"], pools["alice"]["other"]) == (5_000_00, 4_000_00)
    pools, _ = person_income(tax_year_of(ledger_con, 2026), ["alice"], hsa_taxed=True)
    assert (pools["alice"]["retirement_account"], pools["alice"]["other"]) == (5_000_00, 0)


def test_load_rejects_an_exclusion_table_without_a_state(
    test_data_dir: Path, tmp_path: Path,
) -> None:
    """An empty suffix names no state, so no rule would read its rows."""
    (test_data_dir / "aux" / "state_retirement_exclusion__.csv").write_text(
        HEADER + "2025-01,x,pension,,,,,,\n", encoding="utf-8")
    with pytest.raises(LoadError, match="name the state"):
        load_scenario(test_data_dir, "retire_alice", intent_dir=tmp_path / "intent",
                      loaded_by="test", allow_dirty_data=True)


def test_taxed_hsa_growth_is_its_owners_gain(
    ledger_con: sqlite3.Connection, tax_year_of: Callable[..., TaxYear],
) -> None:
    """Where the state taxes an HSA's growth as gain (#490), an exclusion
    covering gains covers it: Alice's HSA grew $1,000 and Bill's fell $300."""
    growth = GROWTH_COUNTERPARTY_ACCOUNTS["deferred"].name
    ledger_con.executemany(
        "INSERT INTO account(id, name, kind, subkind, owner) VALUES (?, ?, ?, ?, ?)",
        [(1, "Asset_HSA_Alice", "asset", "hsa", "alice"),
         (2, "Asset_HSA_Bill", "asset", "hsa", "bill"),
         (3, growth, "income", "capital_gain", None)])
    for frm, to, amount in ((3, 1, 1_000_00), (2, 3, 300_00)):
        ledger_con.execute(
            "INSERT INTO transfer_event(step_id, date, from_account, to_account, from_lot_id, "
            "to_lot_id, amount, requested_cents, remainder_cents, tax_character, reason_kind) "
            "VALUES (1, '2026-06-30', ?, ?, ?, ?, ?, ?, 0, 'deferred', 'rule')",
            (frm, to, frm, to, amount, amount))
    pools, _ = person_income(tax_year_of(ledger_con, 2026), ["alice", "bill"], hsa_taxed=True)
    assert (pools["alice"]["capital_gain"], pools["bill"]["capital_gain"]) == (1_000_00, 0)
    pools, _ = person_income(tax_year_of(ledger_con, 2026), ["alice", "bill"])
    assert pools["alice"]["capital_gain"] == 0
