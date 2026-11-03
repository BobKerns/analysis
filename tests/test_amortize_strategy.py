"""Amortization spending (#459, #326 stage 4, ADR-010 §6.4): the built-in
``amortize`` kind (VPW, the RMD method, a survival horizon) and the context's
``survival_years``, ``life_expectancy`` and ``rmd_divisor``.

``alice_spending`` (from the ``strategy_scenarios`` fixture) owns travel and
gifts ($30,000 a year, tagged ``flex:optional``) over the
``portfolio:invest`` accounts ($800,000, USSTK and BONDX). Its strategy is
switched to ``amortize`` here. Alice was born in March 1965 and Bill in
August 1967, so in May 2026 Bill, the younger, is 58."""

from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from analysis.sim.ensemble import resolve_member
from analysis.sim.loader import LoadError, load_scenario
from analysis.sim.strategies.spending import annuity_rate, parse_horizon
from analysis.sim.strategy_runtime import _TableMortality

SCENARIO = "alice_spending"
FLEX = "strategies/flex.csv"


def _amortize(strategy_csv: Callable[[str, str], Path], **params: object) -> None:
    path = strategy_csv(SCENARIO, FLEX)
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    rows[0].update(kind="amortize", params_json=json.dumps({"portfolio": "portfolio:invest", **params}))
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _query(path: Path, sql: str) -> list[tuple]:
    con = sqlite3.connect(path)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _year_starts(run: Path) -> list[tuple[str, str, dict]]:
    return [(month, note, json.loads(detail)) for month, note, detail in _query(run, (
        "SELECT s.month, d.note, d.detail_json FROM strategy_decision d "
        "JOIN simulation_step s ON s.id = d.step_id JOIN strategy st ON st.id = d.strategy_id "
        "WHERE st.name = 'flex' AND d.hook = 'year_start' ORDER BY d.id"))]


def _cpi(run: Path) -> dict[str, int]:
    return dict(_query(run, (
        "SELECT s.month, i.cumulative_index_micros FROM inflation_index_state i "
        "JOIN simulation_step s ON s.id = i.step_id WHERE i.series = 'cpi'")))


def _load_errors(data_dir: Path, intent_dir: Path) -> str:
    with pytest.raises(LoadError) as info:
        load_scenario(data_dir, SCENARIO, intent_dir=intent_dir, allow_dirty_data=True,
                      compute_stamps=False)
    return str(info.value)


# ---------- the formula ----------

def test_annuity_rate() -> None:
    assert annuity_rate(0, 25) == pytest.approx(1 / 25)
    assert annuity_rate(0.05, 1) == pytest.approx(1.0)  # the last year spends it all
    # An annuity-due: 30 payments, the first today, at 5%.
    assert annuity_rate(0.05, 30) == pytest.approx(0.061954, abs=1e-6)
    # The persona example in the design: 60/40 at VPW's returns, 41 years.
    assert annuity_rate(0.6 * 0.05 + 0.4 * 0.019, 41) == pytest.approx(0.0465, abs=5e-5)


@pytest.mark.parametrize(("text", "parsed"), [
    ("age:100", ("age", 100.0)),
    ("survival:10%", ("survival", 0.1)),
    ("rmd", ("rmd", 0.0)),
    ("age:abc", "age:N needs a whole age"),
    ("age:0", "an age from 1 to 130"),
    ("survival:10", "survival:P needs a percent"),
    ("survival:100%", "above 0% and below 100%"),
    ("vpw", "age:N, survival:P% or rmd"),
    ("rmd:1", "age:N, survival:P% or rmd"),
])
def test_parse_horizon(text: str, parsed: object) -> None:
    found = parse_horizon(text)
    if isinstance(parsed, tuple):
        assert found == parsed
    else:
        assert isinstance(found, str) and str(parsed) in found


# ---------- runs ----------

def test_vpw_starts_on_plan_then_moves_dollar_for_dollar(
    strategy_csv: Callable[[str, str], Path], run_strategy_scenario: Callable[[str], Path],
) -> None:
    _amortize(strategy_csv)
    run = run_strategy_scenario(SCENARIO)
    starts = _year_starts(run)
    month0, note0, first = starts[0]
    assert month0 == "2026-05-31"
    assert first["years"] == 42  # until Bill, 58, is 100
    assert first["spend"] == first["budget"] == 3_000_000 and first["factor"] == 1.0
    assert first["payment"] == round(first["rate"] * first["portfolio"])
    assert "on plan" in note0 and "(age:100, 42 years at " in note0
    # A 60/40-ish blend of VPW's 5.0% and 1.9%.
    assert 0.019 < first["real_return"] < 0.05
    cpi = _cpi(run)
    for (_m0, _n0, d0), (m1, _n1, d1) in zip(starts, starts[1:], strict=False):
        assert d1["years"] < d0["years"]  # a fixed horizon shortens each year
        # Owned spending is the plan plus the change in the payment since the
        # first year, in today's dollars.
        since_first = first["first_payment"] * cpi[m1] / cpi[month0]
        expected = max(0, d1["budget"] + d1["payment"] - since_first)
        assert d1["spend"] == pytest.approx(expected, abs=5), m1  # CPI in millionths
    assert {round(d["factor"], 6) for _m, _n, d in starts[1:]} != {1.0}


@pytest.mark.allow_negative_balance  # the crash runs checking short in the last year
def test_a_down_year_cuts_the_owned_spending_by_the_payments_drop(
    strategy_csv: Callable[[str, str], Path], run_strategy_scenario: Callable[[str], Path],
) -> None:
    header = ("date,kind,scheduled_name,account_1,lot_1,account_2,lot_2,series,amount,param_date,"
              "tax_character,params_json,description,tags,name")
    strategy_csv(SCENARIO, "calendar.csv").write_text(
        f"{header}\n2030-01-31,set_rate,,,,,,stock_us,-40%,,,,a crash,,crash\n")
    _amortize(strategy_csv)
    run = run_strategy_scenario(SCENARIO)
    by_year = {m[:4]: d for m, _n, d in _year_starts(run)}
    drop = by_year["2030"]["payment"] - by_year["2031"]["payment"]
    assert drop > 0
    cut = by_year["2030"]["spend"] - by_year["2031"]["spend"]
    # Dollar for dollar (less a year's inflation and the plan's own change):
    # far more than the payment's share of the owned spending.
    assert cut > 0.8 * drop
    assert by_year["2031"]["spend"] >= 0


def test_ceiling_and_floor_hold_the_real_change(
    strategy_csv: Callable[[str, str], Path], run_strategy_scenario: Callable[[str], Path],
) -> None:
    _amortize(strategy_csv, ceiling="2%", floor="1%")
    run = run_strategy_scenario(SCENARIO)
    starts = _year_starts(run)
    assert [n for _m, n, _d in starts if "held to the" in n]
    cpi = _cpi(run)
    for (m0, _n0, d0), (m1, _n1, d1) in zip(starts, starts[1:], strict=False):
        real_change = d1["spend"] / (d0["spend"] * cpi[m1] / cpi[m0])
        assert 0.99 - 1e-6 <= real_change <= 1.02 + 1e-6, (m1, real_change)


def test_an_explicit_real_return_replaces_the_blend(
    strategy_csv: Callable[[str, str], Path], run_strategy_scenario: Callable[[str], Path],
) -> None:
    _amortize(strategy_csv, real_return="0%")
    run = run_strategy_scenario(SCENARIO)
    first = _year_starts(run)[0][2]
    assert first["real_return"] == 0 and first["rate"] == pytest.approx(1 / 42)


def test_the_rmd_method_divides_by_the_irs_divisor(
    strategy_csv: Callable[[str, str], Path], run_strategy_scenario: Callable[[str], Path],
) -> None:
    _amortize(strategy_csv, horizon="rmd")
    run = run_strategy_scenario(SCENARIO)
    by_year = {m[:4]: (n, d) for m, n, d in _year_starts(run)}
    # Alice, the elder, is 61 at the end of 2026: below the table, so her
    # life expectancy is the divisor.
    note, detail = by_year["2026"]
    assert "life expectancy" in note and "alice's 61" in note
    assert 20 < detail["divisor"] < 30
    # She is 73 at the end of 2038: the table's first age, 26.5.
    note, detail = by_year["2038"]
    assert "rmd_factors 26.5 at alice's" in note
    assert detail["rate"] == pytest.approx(1 / 26.5)
    assert detail["payment"] == round(detail["portfolio"] / 26.5)


def test_a_death_moves_the_horizon_to_the_survivor(
    strategy_csv: Callable[[str, str], Path], run_strategy_scenario: Callable[[str], Path],
) -> None:
    _amortize(strategy_csv)
    run = run_strategy_scenario(SCENARIO)
    plain = {m[:4]: d["years"] for m, _n, d in _year_starts(run)}
    run.unlink()
    strategy_csv(SCENARIO, "lifespan.csv").write_text("person,age_at_death\nbill,62\n")
    widowed = {m[:4]: d["years"] for m, _n, d in _year_starts(run_strategy_scenario(SCENARIO))}
    # Bill dies in August 2029; until then his age sets the horizon.
    assert all(widowed[y] == plain[y] for y in ("2026", "2027", "2028", "2029"))
    assert plain["2030"] == 100 - 62 and widowed["2030"] == 100 - 64  # Alice is 64 in January 2030


def test_the_horizon_never_reads_a_drawn_lifespan(
    strategy_csv: Callable[[str, str], Path], run_strategy_scenario: Callable[[str], Path],
) -> None:
    """Two lifespans that both end after the run: the survival horizon is
    the same, so it read the table and the ages, not the final month."""
    _amortize(strategy_csv, horizon="survival:10%")
    found = []
    for age in ("90", "99"):
        strategy_csv(SCENARIO, "lifespan.csv").write_text(f"person,age_at_death\nbill,{age}\nalice,{age}\n")
        run = run_strategy_scenario(SCENARIO)
        found.append([(m, d["years"], d["spend"]) for m, _n, d in _year_starts(run)])
        run.unlink()
    assert found[0] == found[1]
    years = [y for _m, y, _s in found[0]]
    assert 30 < years[0] < 45


def test_survival_years_lengthen_the_planned_age_as_the_household_survives(
    strategy_scenarios: Path, intent_dir: Path,
) -> None:
    loaded = load_scenario(strategy_scenarios, SCENARIO, intent_dir=intent_dir, allow_dirty_data=True,
                           compute_stamps=False)
    con = sqlite3.connect(loaded.intent_path)
    try:
        tables = _TableMortality(con)
        both = ("alice", "bill")
        now, later = date(2026, 5, 31), date(2036, 5, 31)
        years_now = tables.survival_years(0.10, both, now)
        # Bill's age when the horizon ends: surviving pushes it out, never in.
        planned = [58 + n + tables.survival_years(0.10, both, date(2026 + n, 5, 31)) for n in range(0, 31, 5)]
        assert planned == sorted(planned) and planned[-1] > planned[0], planned
        assert tables.survival_years(0.25, both, now) < years_now
        # Two people outlast either one.
        assert tables.survival_years(0.10, ("bill",), now) < years_now
        assert 15 < tables.life_expectancy("alice", now) < 35
        assert tables.life_expectancy("alice", later) < tables.life_expectancy("alice", now)
        assert tables.rmd_divisor("alice", date(2038, 1, 31)) == 26.5
        assert tables.rmd_divisor("alice", now) is None
    finally:
        con.close()


# ---------- loading ----------

@pytest.mark.parametrize(("params", "message"), [
    ({"horizon": "until:100"}, "age:N, survival:P% or rmd"),
    ({"horizon": "rmd", "real_return": "3%"}, "the rmd horizon takes no real_return"),
])
def test_bad_parameters_fail_the_load(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path], intent_dir: Path,
    params: dict[str, str], message: str,
) -> None:
    _amortize(strategy_csv, **params)
    assert message in _load_errors(strategy_scenarios, intent_dir)


def test_a_survival_horizon_needs_each_persons_sex(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path], intent_dir: Path,
) -> None:
    persons = strategy_scenarios / "persons.csv"
    persons.write_text(persons.read_text().replace(",1967-08-20,m,", ",1967-08-20,,"))
    _amortize(strategy_csv, horizon="survival:10%")
    assert "the survival horizon needs a sex in persons.csv for bill" in _load_errors(
        strategy_scenarios, intent_dir)


def test_a_class_with_no_return_needs_real_return(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path], intent_dir: Path,
) -> None:
    allocations = strategy_scenarios / "allocations.csv"
    allocations.write_text(allocations.read_text().replace("BONDX,,bond,100%", "BONDX,,real_estate,100%"))
    _amortize(strategy_csv)
    assert "holds real_estate, which has no class return: give real_return" in _load_errors(
        strategy_scenarios, intent_dir)
    _amortize(strategy_csv, real_return="3%")
    load_scenario(strategy_scenarios, SCENARIO, intent_dir=intent_dir, allow_dirty_data=True,
                  compute_stamps=False)


def test_an_ensemble_varies_the_real_return(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path], intent_dir: Path,
) -> None:
    _amortize(strategy_csv, real_return="3%")
    vary = strategy_csv(SCENARIO, "strategy_vary/flex.csv")
    vary.parent.mkdir(exist_ok=True)
    vary.write_text("param,vary_distribution,vary_low,vary_high,vary_mode,vary_correlate_with,"
                    "vary_correlation\nreal_return,uniform,1%,5%,,,\n")
    recipe = load_scenario(strategy_scenarios, SCENARIO, intent_dir=intent_dir / "recipe",
                           allow_dirty_data=True, compute_stamps=False).intent_path
    drawn = []
    for i in range(3):
        member = resolve_member(recipe, intent_dir / f"m{i}.sqlite3", seed=7, member_index=i,
                                member_count=3, recipe_tree_id="t")
        (params,) = _query(member, "SELECT params_json FROM strategy WHERE name = 'flex'")[0]
        drawn.append(json.loads(params)["real_return"])
    assert drawn[0] == 30_000
    assert all(10_000 <= r <= 50_000 for r in drawn) and len(set(drawn)) == 3


def test_a_real_return_of_minus_100_percent_fails_the_load(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path], intent_dir: Path,
) -> None:
    _amortize(strategy_csv, real_return="-100%")
    assert "real_return must be above -100%" in _load_errors(strategy_scenarios, intent_dir)


def test_a_survival_horizon_needs_the_life_tables_people_use(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path], intent_dir: Path,
) -> None:
    """Each person's own table is checked: the one their lifespan row names."""
    _amortize(strategy_csv, horizon="survival:10%")
    lifespan = strategy_csv(SCENARIO, "lifespan.csv")
    lifespan.write_text("person,age_at_death,life_table\nalice,90,jp\nbill,90,nowhere\n")
    errors = _load_errors(strategy_scenarios, intent_dir)
    assert "the survival horizon needs aux life_table__nowhere.csv" in errors
    assert "life_table__jp" not in errors
    lifespan.write_text("person,age_at_death,life_table\nalice,90,jp\nbill,90,jp\n")
    load_scenario(strategy_scenarios, SCENARIO, intent_dir=intent_dir, allow_dirty_data=True,
                  compute_stamps=False)


def test_the_rmd_divisor_is_the_latest_row_for_the_age(
    strategy_scenarios: Path, intent_dir: Path,
) -> None:
    """A newer row for one age doesn't hide another age's older row, as for
    the rmd rule; and the table is read afresh, so a swap is seen."""
    loaded = load_scenario(strategy_scenarios, SCENARIO, intent_dir=intent_dir, allow_dirty_data=True,
                           compute_stamps=False)
    con = sqlite3.connect(loaded.intent_path)
    try:
        tables = _TableMortality(con)
        assert tables.rmd_divisor("alice", date(2039, 1, 31)) == 25.5  # 74 at the end of 2039
        con.execute("INSERT INTO aux_table(kind, series, effective_date, payload_json) VALUES "
                    "('rmd_factors', NULL, '2030-01-31', ?)",
                    (json.dumps({"age": 73, "factor_micros": 27_000_000}),))
        assert tables.rmd_divisor("alice", date(2039, 1, 31)) == 25.5
        assert tables.rmd_divisor("alice", date(2038, 1, 31)) == 27.0
    finally:
        con.close()


def test_an_empty_portfolio_pays_nothing_once_started() -> None:
    from analysis.sim.strategies.spending import Amortize
    from analysis.sim.strategy_api import Context, PersonView, percent

    strategy = Amortize("flex", Amortize.Params(portfolio="portfolio:invest", real_return=percent("2%")))
    memory = {"first_payment": 2_000_000, "first_since": "2026-05-31", "spend": 3_000_000,
              "since": "2029-01-31", "factor": 1.0}
    bill = PersonView(id="bill", role="primary", birthdate=date(1967, 8, 20), age=62, alive=True, final_month=None)
    ctx = Context(month=date(2030, 1, 31), lots={}, weights=lambda ref: {}, targets=lambda name: {},
                  memory=memory, people={"bill": bill}, budget=lambda: 3_000_000,
                  cpi=lambda since: 1_000_000)
    decision = strategy.year_start(ctx)
    assert decision is not None and decision.detail["payment"] == 0
    # The owned spending loses the whole first payment: $30,000 - $20,000.
    assert decision.detail["spend"] == 1_000_000 and memory["factor"] == pytest.approx(1 / 3)
