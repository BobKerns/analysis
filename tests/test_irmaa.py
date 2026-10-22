"""The ``irmaa`` rule (#253): Medicare's income-related surcharge, charged
each month at the tier the household's MAGI two years earlier lands in, its
tier table and growth series, and its loader checks."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from analysis.sim import registries
from analysis.sim.engine import IrmaaContext, run_scenario
from analysis.sim.loader import LoadError, LoadResult
from analysis.sim.registries import SCHEDULED_CSV_HEADER
from analysis.sim.reports import tax_by_year

Run = Callable[..., Path]

# The prototype's 2026 table (aux/irmaa_tiers.csv), in cents.
MFJ_FLOORS = {1: 218_000_00, 2: 274_000_00, 3: 342_000_00, 4: 410_000_00, 5: 750_000_00}
SINGLE_FLOORS = {1: 109_000_00, 2: 137_000_00, 3: 171_000_00, 4: 205_000_00, 5: 500_000_00}
PART_B = {1: 81_20, 2: 202_90, 3: 324_60, 4: 446_30, 5: 487_00}
PART_D = {1: 14_50, 2: 37_50, 3: 60_40, 4: 83_30, 5: 91_00}
# The prototype base's growth series (series/inflation__irmaa_*.csv).
FLOOR_RATE, PART_B_RATE, PART_D_RATE = 25_000, 55_000, 60_000


def _grown(cents: int, rate_micros: int, year: int) -> int:
    """``cents`` grown monthly at a flat rate from the table's January 2026
    to January of ``year``, as the engine compounds an inflation series."""
    factor = 1_000_000
    for _ in range((year - 2026) * 12):
        factor = factor * (12_000_000 + rate_micros) // 12_000_000
    return cents * factor // 1_000_000


def _floor(cents: int, year: int) -> int:
    return (_grown(cents, FLOOR_RATE, year) + 50_000) // 100_000 * 100_000


def _expected_tier(magi: int, floors: dict[int, int], year: int) -> int:
    """The tier premium ``year`` charges for ``magi``, tiers 1-4 grown (the
    top tier's floor is frozen until 2028, so leave it out of these runs'
    incomes)."""
    return max((t for t, f in floors.items() if t < 5 and magi > _floor(f, year)), default=0)


def _monthly(tier: int, year: int) -> int:
    if tier == 0:
        return 0
    return _grown(PART_B[tier], PART_B_RATE, year) + _grown(PART_D[tier], PART_D_RATE, year)


@pytest.fixture
def irmaa_run(
    retire_alice_csv: Callable[[str], Path], fund_asset_savings: Callable[..., None],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> Run:
    """Run retire_alice with a federal tax rule, an irmaa rule for each of
    Alice and Bill from the start, and the given extra scheduled rows (each
    a dict of scheduled.csv columns). ``magi`` sets meta_json irmaa.magi."""
    fund_asset_savings("1000000.00")
    path = retire_alice_csv("scheduled.csv")
    scenario_path = retire_alice_csv("scenario.csv")
    blank = dict.fromkeys(SCHEDULED_CSV_HEADER, "")
    runs: list[Path] = []

    def _run(*extra: dict[str, str], magi: dict[str, Any] | None = None,
             irmaa: dict[str, str] | None = None) -> Path:
        scheduled = pd.read_csv(path, dtype=str, keep_default_na=False)
        scheduled = scheduled.reindex(columns=SCHEDULED_CSV_HEADER, fill_value="")
        rows = [{
            **blank, "name": "tax_federal", "from_account": "Asset_Checking",
            "to_account": "Expense_Taxes_Federal", "cadence": "annual",
            "month_of_year": "4", "amount_rule_kind": "tax_accumulator",
            "tax_character": "none", "start": "2027-04-30", "meta_json": "{}",
            "params_json": '{"year":"previous","scope":"federal"}',
        }]
        rows += [{
            **blank, "name": f"irmaa_{person}", "from_account": "Asset_Checking",
            "to_account": "Expense_Healthcare", "cadence": "monthly",
            "amount_rule_kind": "irmaa", "person": person, "tax_character": "none",
            "start": "2026-05-31", "meta_json": "{}", **(irmaa or {}),
        } for person in ("alice", "bill")]
        rows += [{**blank, "meta_json": "{}", **row} for row in extra]
        for row in rows:
            scheduled.loc[len(scheduled)] = row
        scheduled.to_csv(path, index=False)
        scenario = pd.read_csv(scenario_path, dtype=str, keep_default_na=False)
        scenario.loc[0, "meta_json"] = json.dumps({"irmaa": {"magi": magi}} if magi else {})
        scenario.to_csv(scenario_path, index=False)
        intent = load_test_copy().intent_path
        runs.append(run_scenario(intent, runs_dir / f"irmaa{len(runs)}.sqlite3", run_by="TEST",
                                 allow_dirty_data=True, allow_dirty_code=True))
        return runs[-1]
    return _run


def _bonus(year: int, amount: str) -> dict[str, str]:
    """A one-off ordinary income in June of ``year``."""
    return {
        "name": f"bonus_{year}", "from_account": "Income_Salary_Alice",
        "to_account": "Asset_Checking", "cadence": "one_off",
        "amount_rule_kind": "fixed_amount", "amount": amount, "tax_character": "ordinary",
        "start": f"{year}-06-30", "end": f"{year}-06-30",
    }


def _charges(run: Path) -> pd.DataFrame:
    """Each month's irmaa charge, by rule name, indexed by month."""
    with sqlite3.connect(run) as con:
        frame = pd.read_sql_query(
            "SELECT te.date AS month, st.name, te.amount FROM transfer_event te "
            "JOIN scheduled_transaction st ON st.id = te.reason_id "
            "WHERE te.reason_kind = 'rule' AND st.amount_rule_kind = 'irmaa'", con)
    return frame.pivot_table(index="month", columns="name", values="amount", aggfunc="sum")


def _summary(run: Path) -> pd.DataFrame:
    with sqlite3.connect(run) as con:
        frame = pd.read_sql_query("SELECT year, bucket, amount FROM tax_year_summary", con)
    return frame.pivot_table(index="year", columns="bucket", values="amount", aggfunc="sum")


def _year_charge(charges: pd.DataFrame, year: int, name: str) -> set[int]:
    """The distinct monthly charges of ``name`` in ``year`` (0 for none)."""
    months = [m for m in charges.index if m.startswith(str(year))]
    column = charges[name] if name in charges.columns else pd.Series(dtype=float)
    return {int(column.get(m, 0) or 0) for m in months} or {0}


def test_a_premium_year_charges_the_tier_of_magi_two_years_earlier(irmaa_run: Run) -> None:
    run = irmaa_run(_bonus(2029, "400000.00"))
    charges, summary = _charges(run), _summary(run)
    magi = int(summary.loc[2029, "federal_magi"])
    assert MFJ_FLOORS[4] < magi < MFJ_FLOORS[5]
    assert _expected_tier(magi, MFJ_FLOORS, 2031) == 4
    for person in ("irmaa_alice", "irmaa_bill"):
        assert _year_charge(charges, 2031, person) == {_monthly(4, 2031)}
    # Every year the run computed two years back charges its own tier.
    for year in range(2028, 2041):
        tier = _expected_tier(int(summary.loc[year - 2, "federal_magi"]), MFJ_FLOORS, year)
        assert _year_charge(charges, year, "irmaa_alice") == {_monthly(tier, year)}, year
        assert summary.loc[year - 2, "federal_irmaa_tier"] == tier, year


def test_magi_is_agi_while_tax_exempt_interest_is_not_modeled(irmaa_run: Run) -> None:
    summary = _summary(irmaa_run())
    assert (summary["federal_magi"] == summary["federal_adjusted_gross_income"]).all()


def test_years_before_the_run_read_magi_from_meta_json(irmaa_run: Run) -> None:
    run = irmaa_run(magi={"2024": "300000.00", "2025": 150000})
    charges = _charges(run)
    assert _year_charge(charges, 2026, "irmaa_bill") == {_monthly(2, 2026)}
    assert _year_charge(charges, 2027, "irmaa_bill") == {0}
    # The run's own years record the tier; years before it are the author's.
    assert 2024 not in _summary(run).index


def test_parts_b_charges_part_b_alone(irmaa_run: Run) -> None:
    run = irmaa_run(magi={"2024": "300000.00"}, irmaa={"params_json": '{"parts": "B"}'})
    assert _year_charge(_charges(run), 2026, "irmaa_alice") == {PART_B[2]}


def test_a_survivor_pays_on_the_death_years_joint_return_then_files_single(
    irmaa_run: Run, write_lifespans: Callable[..., None],
) -> None:
    write_lifespans("bill,66,,,,,,")  # Bill's final month is in 2033
    run = irmaa_run(_bonus(2033, "320000.00"), _bonus(2034, "230000.00"))
    charges, summary = _charges(run), _summary(run)
    joint, single = (int(summary.loc[y, "federal_magi"]) for y in (2033, 2034))
    # The death year's return is joint: its floors, the survivor alone charged.
    assert _expected_tier(joint, MFJ_FLOORS, 2035) != _expected_tier(joint, SINGLE_FLOORS, 2035)
    assert _year_charge(charges, 2035, "irmaa_alice") == {
        _monthly(_expected_tier(joint, MFJ_FLOORS, 2035), 2035)}
    assert _year_charge(charges, 2035, "irmaa_bill") == {0}
    # The next return is Single.
    assert _expected_tier(single, MFJ_FLOORS, 2036) != _expected_tier(single, SINGLE_FLOORS, 2036)
    assert _year_charge(charges, 2036, "irmaa_alice") == {
        _monthly(_expected_tier(single, SINGLE_FLOORS, 2036), 2036)}


def test_the_tax_report_shows_magi_and_the_tier_it_sets(irmaa_run: Run) -> None:
    from analysis.sim.connection import RunConnection

    run = irmaa_run(_bonus(2029, "400000.00"))
    with RunConnection(run) as rc:
        frame = tax_by_year(rc)
    columns = list(frame.columns)
    assert columns[columns.index("fed_agi") + 1:columns.index("fed_agi") + 3] == [
        "fed_magi", "irmaa_tier"]
    assert frame.loc[2029, "irmaa_tier"] == 4
    assert pd.isna(frame.loc[2040, "irmaa_tier"])  # charged after the run ends


# IrmaaContext alone, on a hand-built aux table.

def _tiers(*rows: tuple[str, int, int, str | None]) -> list[tuple[str, dict[str, Any]]]:
    return [("2026-01-31", {
        "filing_status": status, "tier": tier, "magi_floor": floor, "part_b": 100_00 * tier,
        "part_d": 10_00 * tier, "floor_series": series, "part_b_series": None,
        "part_d_series": None,
    }) for status, tier, floor, series in rows]


def _context(aux: dict, **kwargs: Any) -> IrmaaContext:
    aux = {("filing_status", None): [("2026-01-31", {"filing_status": "MFJ"})], **aux}
    settings: dict[str, Any] = {
        "start_year": 2026, "prior_magi": {}, "person_lifespan": {}, "taxpayers": (), **kwargs}
    return IrmaaContext(aux, {}, **settings)


def _con_with_magi(**by_year: int) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE tax_year_summary (year INTEGER, bucket TEXT, amount INTEGER, "
                "computed_at TEXT, PRIMARY KEY (year, bucket))")
    for year, amount in by_year.items():
        con.execute("INSERT INTO tax_year_summary VALUES (?, 'federal_magi', ?, '')",
                    (int(year[1:]), amount))
    return con


def test_a_floor_is_more_than_its_magi_floor() -> None:
    aux = {(registries.IRMAA_TIERS, None): _tiers(("MFJ", 1, 200_000_00, None))}
    at, above = _con_with_magi(y2026=200_000_00), _con_with_magi(y2026=200_000_01)
    assert _context(aux).charge(at, 2028, {}, 1) == 0
    assert _context(aux).charge(above, 2028, {}, 1) == 110_00


def test_a_blank_series_holds_a_figure_flat_and_a_named_one_grows_and_rounds() -> None:
    aux = {
        (registries.IRMAA_TIERS, None): _tiers(
            ("MFJ", 1, 200_000_00, None), ("MFJ", 2, 300_000_00, "fast")),
        ("inflation", "fast"): [("2026-01-31", {"annual_rate_micros": 100_000})],
    }
    tiers = _context(aux).tiers(2036, "MFJ", 1)
    assert tiers[0].floor == 200_000_00
    grown = _grown(300_000_00, 100_000, 2036)
    assert grown % 100_000 != 0
    assert tiers[1].floor == (grown + 50_000) // 100_000 * 100_000


def test_a_year_before_the_filing_status_table_takes_its_first_row() -> None:
    aux = {(registries.IRMAA_TIERS, None): _tiers(("MFJ", 1, 1, None))}
    assert _context(aux).filing_status(2024, "filing_status", 1) == "MFJ"


def test_a_death_after_a_return_before_the_table_leaves_that_return_joint() -> None:
    # The 2024 return is joint though the table starts in 2026 and bill died
    # in 2025: the death is judged against 2024, not the table's first year.
    aux = {(registries.IRMAA_TIERS, None): _tiers(("MFJ", 1, 1, None))}
    context = _context(aux, person_lifespan={"bill": "2025-06-30"}, taxpayers=("alice", "bill"))
    assert context.filing_status(2024, "filing_status", 1) == "MFJ"
    assert context.filing_status(2026, "filing_status", 1) == "Single"


def test_a_missing_federal_magi_is_an_error() -> None:
    from analysis.sim.engine import EngineError

    aux = {(registries.IRMAA_TIERS, None): _tiers(("MFJ", 1, 1, None))}
    with pytest.raises(EngineError, match="no federal MAGI for 2027"):
        _context(aux).charge(_con_with_magi(), 2029, {}, 1)


def test_the_first_premium_years_without_prior_magi_charge_nothing(irmaa_run: Run) -> None:
    charges = _charges(irmaa_run())
    assert _year_charge(charges, 2026, "irmaa_alice") == {0}
    assert _year_charge(charges, 2027, "irmaa_alice") == {0}


# Loader checks.

@pytest.fixture
def load_with(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult],
) -> Callable[..., LoadResult]:
    """Load retire_alice with an irmaa rule for Alice (cells over the
    defaults) and, unless ``tax`` is false, a federal tax rule."""
    def _load(*, tax: bool = True, meta: object = None, **cells: str) -> LoadResult:
        path = retire_alice_csv("scheduled.csv")
        scheduled = pd.read_csv(path, dtype=str, keep_default_na=False)
        scheduled = scheduled.reindex(columns=SCHEDULED_CSV_HEADER, fill_value="")
        blank = dict.fromkeys(SCHEDULED_CSV_HEADER, "")
        if tax:
            scheduled.loc[len(scheduled)] = {
                **blank, "name": "tax_federal", "from_account": "Asset_Checking",
                "to_account": "Expense_Taxes_Federal", "cadence": "annual",
                "month_of_year": "4", "amount_rule_kind": "tax_accumulator",
                "tax_character": "none", "start": "2027-04-30", "meta_json": "{}",
                "params_json": '{"year":"previous"}',
            }
        scheduled.loc[len(scheduled)] = {
            **blank, "name": "irmaa_alice", "from_account": "Asset_Checking",
            "to_account": "Expense_Healthcare", "cadence": "monthly",
            "amount_rule_kind": "irmaa", "person": "alice", "tax_character": "none",
            "start": "2030-03-31", "meta_json": "{}", **cells,
        }
        scheduled.to_csv(path, index=False)
        if meta is not None:
            scenario_path = retire_alice_csv("scenario.csv")
            scenario = pd.read_csv(scenario_path, dtype=str, keep_default_na=False)
            scenario.loc[0, "meta_json"] = json.dumps({"irmaa": meta})
            scenario.to_csv(scenario_path, index=False)
        return load_test_copy()
    return _load


@pytest.mark.parametrize(("cells", "tax", "message"), [
    ({"person": ""}, True, "irmaa needs person"),
    ({"cadence": "annual", "month_of_year": "1"}, True, "irmaa needs cadence monthly"),
    ({"amount": "10.00"}, True, "irmaa takes no amount"),
    ({"params_json": '{"parts": "A"}'}, True, "parts must be one of"),
    ({"params_json": '{"filing_status_aux": "nope"}'}, True, "filing_status_aux 'nope' has no aux table"),
    ({"params_json": '{"tiers": 1}'}, True, "unknown params_json key"),
    ({}, False, "there is none"),
])
def test_the_loader_checks_an_irmaa_rule(
    load_with: Callable[..., LoadResult], cells: dict[str, str], tax: bool, message: str,
) -> None:
    with pytest.raises(LoadError, match=message):
        load_with(tax=tax, **cells)


def test_an_irmaa_rule_needs_exactly_one_federal_tax_rule(
    load_with: Callable[..., LoadResult], retire_alice_csv: Callable[[str], Path],
) -> None:
    path = retire_alice_csv("scheduled.csv")
    scheduled = pd.read_csv(path, dtype=str, keep_default_na=False)
    scheduled = scheduled.reindex(columns=SCHEDULED_CSV_HEADER, fill_value="")
    scheduled.loc[len(scheduled)] = {
        **dict.fromkeys(SCHEDULED_CSV_HEADER, ""), "name": "tax_federal_too",
        "from_account": "Asset_Checking", "to_account": "Expense_Taxes_Federal",
        "cadence": "annual", "month_of_year": "12", "amount_rule_kind": "tax_accumulator",
        "tax_character": "none", "start": "2027-12-31", "meta_json": "{}",
        "params_json": '{"year":"current"}'}
    scheduled.to_csv(path, index=False)
    with pytest.raises(LoadError, match="and there are 2; it needs exactly one"):
        load_with()


def test_a_rule_starting_after_the_first_two_years_needs_no_prior_magi(
    load_with: Callable[..., LoadResult],
) -> None:
    assert not [w for w in load_with().warnings if "irmaa" in w]


def test_a_missing_prior_magi_year_is_a_warning(load_with: Callable[..., LoadResult]) -> None:
    warnings = load_with(start="2026-05-31", meta={"magi": {"2025": "1.00"}}).warnings
    assert [w for w in warnings if "irmaa" in w] == [
        "scenario.csv meta_json has no irmaa.magi for 2024, before the run starts, "
        "so 2026 charges no IRMAA surcharge"]


@pytest.mark.parametrize(("meta", "message"), [
    ({"magi": {"24": "1.00"}}, "is not a four-digit year"),
    ({"magi": {"2024": 1.5}}, "invalid dollar value"),
    ({"prior": {}}, "unknown key 'prior'"),
    ([2024], "must be a JSON object"),
    ({"magi": []}, "magi must map years to dollars"),
])
def test_the_loader_checks_irmaa_meta(
    load_with: Callable[..., LoadResult], meta: object, message: str,
) -> None:
    with pytest.raises(LoadError, match=message):
        load_with(meta=meta)


def test_a_tier_tables_series_must_exist(
    load_with: Callable[..., LoadResult], write_aux: Callable[[str, str], Path],
) -> None:
    write_aux(f"{registries.IRMAA_TIERS}.csv", (
        "effective_date,filing_status,tier,magi_floor,part_b,part_d,"
        "floor_series,part_b_series,part_d_series\n"
        "2026-01-31,MFJ,1,218000.00,81.20,14.50,no_such_series,,\n"))
    with pytest.raises(LoadError, match="floor_series 'no_such_series' doesn't exist"):
        load_with()


def test_floors_must_rise_with_tier(
    load_with: Callable[..., LoadResult], write_aux: Callable[[str, str], Path],
) -> None:
    write_aux(f"{registries.IRMAA_TIERS}.csv", (
        "effective_date,filing_status,tier,magi_floor,part_b,part_d,"
        "floor_series,part_b_series,part_d_series\n"
        "2026-01-31,MFJ,1,218000.00,81.20,14.50,,,\n"
        "2026-01-31,MFJ,2,200000.00,202.90,37.50,,,\n"))
    with pytest.raises(LoadError, match="magi_floor must rise with tier"):
        load_with()


# fill_bracket (#252) with IRMAA: a tier to stay within, and the cliff priced
# into max_marginal_rate.

def _conversion(year: int, **params: Any) -> dict[str, str]:
    """A fill_bracket from Alice's traditional IRA to her Roth, to the top of
    the 32% bracket (high enough that IRMAA is what stops it)."""
    return {
        "name": "convert", "from_account": "Asset_TraditionalIRA_Alice", "from_lot": "default",
        "to_account": "Asset_Roth_Alice", "to_lot": "default", "cadence": "annual",
        "month_of_year": "12", "amount_rule_kind": "fill_bracket", "rate": "32%",
        "tax_character": "tax_deferred_out", "start": f"{year}-01-31", "end": f"{year}-12-31",
        "params_json": json.dumps(params),
    }


def _converted(run: Path) -> int:
    with sqlite3.connect(run) as con:
        return con.execute(
            "SELECT COALESCE(SUM(te.amount), 0) FROM transfer_event te "
            "JOIN scheduled_transaction st ON st.id = te.reason_id "
            "WHERE te.reason_kind = 'rule' AND st.amount_rule_kind = 'fill_bracket'",
        ).fetchone()[0]


def test_irmaa_tier_keeps_a_conversions_magi_within_the_tier(irmaa_run: Run) -> None:
    unlimited = _summary(irmaa_run(_conversion(2031)))
    floor = _floor(MFJ_FLOORS[1], 2031)
    assert unlimited.loc[2031, "federal_magi"] > floor
    run = irmaa_run(_conversion(2031, irmaa_tier=0))
    magi = _summary(run).loc[2031, "federal_magi"]
    # As close under the next tier's floor as a cent allows (more Social
    # Security turns taxable with each dollar, so the last cent may overshoot).
    assert floor - 100 <= magi <= floor
    assert _converted(run) > 0
    assert _summary(run).loc[2031, "federal_irmaa_tier"] == 0


def test_irmaa_tier_needs_a_tier_table_and_a_whole_number(
    load_with: Callable[..., LoadResult], retire_alice_csv: Callable[[str], Path],
    test_data_dir: Path,
) -> None:
    path = retire_alice_csv("scheduled.csv")
    scheduled = pd.read_csv(path, dtype=str, keep_default_na=False)
    scheduled = scheduled.reindex(columns=SCHEDULED_CSV_HEADER, fill_value="")
    scheduled.loc[len(scheduled)] = {
        **dict.fromkeys(SCHEDULED_CSV_HEADER, ""), "meta_json": "{}",
        **_conversion(2031, irmaa_tier=-1)}
    scheduled.to_csv(path, index=False)
    (test_data_dir / "aux" / f"{registries.IRMAA_TIERS}.csv").unlink()
    with pytest.raises(LoadError) as error:
        load_with()
    issues = "\n".join(error.value.issues)
    assert "irmaa_tier must be a whole number, 0 or more" in issues
    assert "irmaa_tier needs aux/irmaa_tiers.csv" in issues


def test_irmaa_tier_at_the_top_tier_caps_nothing_and_is_refused(
    load_with: Callable[..., LoadResult], retire_alice_csv: Callable[[str], Path],
) -> None:
    path = retire_alice_csv("scheduled.csv")
    scheduled = pd.read_csv(path, dtype=str, keep_default_na=False)
    scheduled = scheduled.reindex(columns=SCHEDULED_CSV_HEADER, fill_value="")
    scheduled.loc[len(scheduled)] = {
        **dict.fromkeys(SCHEDULED_CSV_HEADER, ""), "meta_json": "{}",
        **_conversion(2031, irmaa_tier=5)}
    scheduled.to_csv(path, index=False)
    with pytest.raises(LoadError) as error:
        load_with()
    assert "irmaa_tier 5 is the top tier or above" in "\n".join(error.value.issues)


def test_a_state_fill_bracket_watching_irmaa_needs_one_federal_rule(
    load_with: Callable[..., LoadResult], retire_alice_csv: Callable[[str], Path],
) -> None:
    path = retire_alice_csv("scheduled.csv")
    scheduled = pd.read_csv(path, dtype=str, keep_default_na=False)
    scheduled = scheduled.reindex(columns=SCHEDULED_CSV_HEADER, fill_value="")
    blank = {**dict.fromkeys(SCHEDULED_CSV_HEADER, ""), "meta_json": "{}"}
    for name in ("federal_a", "federal_b"):
        scheduled.loc[len(scheduled)] = {
            **blank, "name": name, "from_account": "Asset_Checking",
            "to_account": "Expense_Living", "cadence": "annual", "month_of_year": "12",
            "amount_rule_kind": "tax_accumulator", "tax_character": "none",
            "start": "2027-01-31", "params_json": '{"year":"current","scope":"federal"}'}
    scheduled.loc[len(scheduled)] = {
        **blank, **_conversion(2031, scope="state", irmaa_tier=0)}
    scheduled.to_csv(path, index=False)
    with pytest.raises(LoadError) as error:
        load_with()
    assert "measures MAGI with the federal tax_accumulator rule, and there is more than one" in (
        "\n".join(error.value.issues))


def _flat_tax(rate: float) -> Callable[[int, int], int]:
    return lambda extra, _ltcg: int(extra * rate)


def _cliff_at(floor: int, jump: int) -> Callable[[int], int]:
    return lambda x: jump if x > floor else 0


@pytest.mark.parametrize(("amount", "expected"), [
    # 5,000 past the floor: (1,000 tax + 2,000 surcharge) / 5,000 is 60%.
    (15_000_00, 10_000_00),
    # 50,000 past it: (10,000 + 2,000) / 50,000 is 24%, within 30%.
    (60_000_00, 60_000_00),
])
def test_a_cliff_is_crossed_only_when_the_span_past_it_pays_for_it(
    amount: int, expected: int,
) -> None:
    from analysis.sim.engine import _within_marginal_rate

    tax, cliff = _flat_tax(0.2), _cliff_at(10_000_00, 2_000_00)
    assert _within_marginal_rate(tax, cliff, amount, 300_000) == expected
    assert _within_marginal_rate(tax, None, amount, 300_000) == amount


def test_the_tax_rate_alone_still_stops_a_crossing_span() -> None:
    from analysis.sim.engine import _within_marginal_rate

    # 20% up to 30,000, then 40%: past the floor at 10,000 stepping stops at
    # 30,000, and that span, (4,000 + 1,000) / 20,000 = 25%, pays.
    def tax(extra: int, _ltcg: int) -> int:
        return int(min(extra, 30_000_00) * 0.2 + max(0, extra - 30_000_00) * 0.4)
    assert _within_marginal_rate(tax, _cliff_at(10_000_00, 1_000_00), 60_000_00, 300_000) \
        == 30_000_00


def test_a_cliff_costs_a_years_surcharge_for_each_person_enrolled_two_years_on() -> None:
    aux = {(registries.IRMAA_TIERS, None): _tiers(("MFJ", 1, 200_000_00, None))}
    rule = [None] * 23
    both, part_b = list(rule), list(rule)
    for row, ext in ((both, None), (part_b, '{"parts": "B"}')):
        row[8], row[13], row[16], row[17] = "alice", ext, "2030-01-31", None
    late = list(both)
    late[8], late[16] = "bill", "2034-01-31"  # not enrolled until after 2033
    gone = list(both)
    gone[8] = "carol"  # dies before the conversion is decided
    context = _context(aux, rules=[both, part_b, late, gone],
                       person_lifespan={"carol": "2030-06-30"})
    assert context.annual_cost(2031, 200_000_00, "2031-12-31", 1) == 0
    assert context.annual_cost(2031, 200_000_01, "2031-12-31", 1) == 12 * (110_00 + 100_00)


def test_annual_cost_prices_each_rule_with_its_own_filing_status_table() -> None:
    aux = {
        (registries.IRMAA_TIERS, None): _tiers(
            ("MFJ", 1, 200_000_00, None), ("Single", 1, 100_000_00, None)),
        ("single_status", None): [("2026-01-31", {"filing_status": "Single"})],
    }
    rule = [None] * 23
    joint, single = list(rule), list(rule)
    for row, ext in ((joint, None), (single, '{"filing_status_aux": "single_status"}')):
        row[8], row[13], row[16], row[17] = "alice", ext, "2030-01-31", None
    context = _context(aux, rules=[joint, single])
    # Over the single floor only: just the rule reading the Single table pays.
    assert context.annual_cost(2031, 150_000_00, "2031-12-31", 1) == 12 * 110_00


def test_annual_cost_counts_only_the_months_a_rule_charges() -> None:
    aux = {(registries.IRMAA_TIERS, None): _tiers(("MFJ", 1, 200_000_00, None))}
    rule = [None] * 23
    late, early = list(rule), list(rule)
    late[8], late[16], late[17] = "alice", "2033-12-31", None  # enrolls in December
    early[8], early[16], early[17] = "bill", "2030-01-31", "2033-06-30"  # ends in June
    context = _context(aux, rules=[late, early])
    assert context.annual_cost(2031, 200_000_01, "2031-12-31", 1) == (1 + 6) * 110_00
