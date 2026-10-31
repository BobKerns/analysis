"""Varying a strategy's parameters across an ensemble (#408):
``strategy_vary/<name>.csv`` beside the strategy's own ``strategies/<name>.csv``."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from analysis.sim.ensemble import resolve_member, varying_names
from analysis.sim.loader import LoadError, load_scenario
from analysis.sim.engine import run_scenario

SCENARIO = "alice_spending"
HEADER = "param,vary_distribution,vary_low,vary_high,vary_mode,vary_correlate_with,vary_correlation\n"
UPPER = "strategy:flex.upper"


def _vary(strategy_csv: Callable[[str, str], Path], rows: str, scenario: str = SCENARIO,
          name: str = "flex") -> Path:
    path = strategy_csv(scenario, f"strategy_vary/{name}.csv")
    path.parent.mkdir(exist_ok=True)
    path.write_text(HEADER + rows)
    return path


def _load(data_dir: Path, intent_dir: Path, scenario: str = SCENARIO):
    return load_scenario(data_dir, scenario, intent_dir=intent_dir / scenario, allow_dirty_data=True,
                         compute_stamps=False)


def _errors(data_dir: Path, intent_dir: Path) -> str:
    with pytest.raises(LoadError) as info:
        _load(data_dir, intent_dir)
    return str(info.value)


def _params(intent: Path) -> dict:
    con = sqlite3.connect(intent)
    try:
        return json.loads(con.execute("SELECT params_json FROM strategy WHERE name = 'flex'").fetchone()[0])
    finally:
        con.close()


_ROWS = ("upper,uniform,10%,30%,,,\n"
         "adjust,triangular,5%,15%,,,\n"
         "final_years,uniform,10,20,,,\n")


def test_members_draw_the_parameters_and_member_0_keeps_the_authored_ones(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path], intent_dir: Path,
) -> None:
    _vary(strategy_csv, _ROWS)
    recipe = _load(strategy_scenarios, intent_dir).intent_path
    con = sqlite3.connect(recipe)
    try:
        assert {UPPER, "strategy:flex.adjust", "strategy:flex.final_years"} <= set(varying_names(con))
        (mode,) = con.execute("SELECT vary_mode FROM strategy_vary WHERE param = 'adjust'").fetchone()
    finally:
        con.close()
    assert mode == "100000"  # a blank mode is the authored value, 10%
    drawn = []
    for i in range(4):
        member = resolve_member(recipe, intent_dir / f"m{i}.sqlite3", seed=7, member_index=i,
                                member_count=4, recipe_tree_id="t")
        drawn.append(_params(member))
    assert drawn[0] == {"portfolio": "portfolio:invest", "upper": 200_000, "adjust": 100_000,
                        "final_years": 15}
    for params in drawn[1:]:
        assert 100_000 <= params["upper"] <= 300_000 and 50_000 <= params["adjust"] <= 150_000
        assert isinstance(params["final_years"], int) and 10 <= params["final_years"] <= 20
    assert len({p["upper"] for p in drawn}) == 4
    con = sqlite3.connect(intent_dir / "m1.sqlite3")
    try:
        draws = dict(con.execute(
            "SELECT calendar_event_name, vary_target FROM ensemble_draw WHERE calendar_event_name LIKE 'strategy:%'"
        ).fetchall())
        assert con.execute("SELECT COUNT(*) FROM strategy_vary WHERE vary_distribution IS NOT NULL").fetchone() == (0,)
    finally:
        con.close()
    assert draws == {UPPER: "upper", "strategy:flex.adjust": "adjust",
                     "strategy:flex.final_years": "final_years"}
    run_scenario(intent_dir / "m1.sqlite3", intent_dir / "run_m1.sqlite3", run_by="test",
                 allow_dirty_data=True, allow_dirty_code=True)


def test_a_dollar_parameter_varies_in_dollars(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path], intent_dir: Path,
) -> None:
    strategy_csv(SCENARIO, "strategies/flex.csv").write_text(
        "kind,chain,rules,params_json\n"
        'threshold,,flex:optional,"{""portfolio"":""portfolio:invest"",""minimum"":""500000.00""}"\n')
    _vary(strategy_csv, "minimum,uniform,400000.00,600000.00,,,\n")
    recipe = _load(strategy_scenarios, intent_dir).intent_path
    member = resolve_member(recipe, intent_dir / "m1.sqlite3", seed=7, member_index=1, member_count=2,
                            recipe_tree_id="t")
    assert 40_000_000 <= _params(member)["minimum"] <= 60_000_000


def test_a_parameter_correlates_with_a_lifespan_or_another_parameter(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path], intent_dir: Path,
) -> None:
    _vary(strategy_csv, f"upper,uniform,10%,30%,,,\nlower,uniform,10%,30%,,{UPPER},1\n")
    recipe = _load(strategy_scenarios, intent_dir).intent_path
    for i in (1, 2, 3):
        params = _params(resolve_member(recipe, intent_dir / f"m{i}.sqlite3", seed=7, member_index=i,
                                        member_count=4, recipe_tree_id="t"))
        assert params["upper"] == params["lower"]


def test_a_child_file_replaces_the_parent_file_whole(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path], intent_dir: Path,
) -> None:
    _vary(strategy_csv, _ROWS)
    child = strategy_scenarios / "scenarios" / "alice_spending_vary"
    child.mkdir()
    (child / "scenario.csv").write_text(
        "name,description,start,end,seed,parent_scenario,created_by,meta_json\n"
        "alice_spending_vary,,2026-05-31,2040-12-31,42,alice_spending,alice,{}\n")
    _vary(strategy_csv, "lower,uniform,10%,30%,,,\n", scenario="alice_spending_vary")
    con = sqlite3.connect(_load(strategy_scenarios, intent_dir, "alice_spending_vary").intent_path)
    try:
        assert [r for (r,) in con.execute("SELECT param FROM strategy_vary")] == ["lower"]
    finally:
        con.close()


@pytest.mark.parametrize(("rows", "message"), [
    ("no_such,uniform,1,2,,,\n", "kind 'guardrail' has no parameter 'no_such'"),
    ("portfolio,uniform,1,2,,,\n", "'portfolio' can't vary: only a percent, dollars or a number can"),
    ("skip_inflation_after_loss,uniform,0,1,,,\n", "'skip_inflation_after_loss' can't vary"),
    ("upper,uniform,25%,30%,,,\n", "the authored upper must lie within the range it varies over"),
    ("upper,uniform,10,30,,,\n", "vary_low percent value '10' needs its % sign"),
    ("upper,uniform,10%,30%,20%,,\n", "vary_mode is not used by uniform"),
    ("upper,normal,10%,30%,,,\n", "vary_distribution must be one of"),
    ("upper,uniform,10%,,,,\n", "uniform requires vary_high"),
    ("upper,uniform,10%,30%,,,\nupper,uniform,10%,30%,,,\n", "upper is varied twice"),
    ("upper,uniform,10%,30%,,nowhere,0.5\n", "vary_correlate_with 'nowhere' names no"),
    ("upper,uniform,10%,30%,,strategy:flex.upper,0.5\n", "a parameter can't correlate with itself"),
    ("upper,uniform,10%,30%,,,0.5\n", "vary_correlate_with and vary_correlation go together"),
])
def test_bad_vary_rows_fail_the_load(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path], intent_dir: Path,
    rows: str, message: str,
) -> None:
    _vary(strategy_csv, rows)
    errors = _errors(strategy_scenarios, intent_dir)
    assert message in errors and "strategy_vary/flex.csv" in errors


def test_a_vary_file_needs_its_strategy(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path], intent_dir: Path,
) -> None:
    _vary(strategy_csv, "upper,uniform,10%,30%,,,\n", name="other")
    assert "no strategy 'other' (strategies/other.csv)" in _errors(strategy_scenarios, intent_dir)


def test_a_vary_file_of_a_disabled_strategy_is_unused(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path], intent_dir: Path,
) -> None:
    path = strategy_csv(SCENARIO, "strategies/flex.csv")
    path.write_text(path.read_text().replace(",,,,", ",,,false,", 1))
    _vary(strategy_csv, "upper,uniform,10%,30%,,,\n")
    result = _load(strategy_scenarios, intent_dir)
    assert any("strategy 'flex' is disabled" in u for u in result.unused)


def test_a_calendar_row_cannot_take_a_strategy_node_name(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path], intent_dir: Path,
) -> None:
    strategy_csv(SCENARIO, "calendar.csv").write_text(
        "date,kind,scheduled_name,account_1,lot_1,account_2,lot_2,series,amount,param_date,"
        "tax_character,params_json,description,tags,name\n"
        "2030-01-31,set_rate,,,,,,stock_us,5%,,,,,,strategy:flex.upper\n")
    assert "can't start with 'strategy:'" in _errors(strategy_scenarios, intent_dir)


def test_the_parameters_table_and_draws_db_describe_a_strategy_node(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path], intent_dir: Path,
    runs_dir: Path,
) -> None:
    from analysis.sim.draws_db import build_draws_db
    from analysis.sim.sensitivity import parameters

    _vary(strategy_csv, "upper,triangular,10%,30%,,,\n")
    recipe = _load(strategy_scenarios, intent_dir).intent_path
    batch = runs_dir / SCENARIO / "2026-01-01T000000Z"
    for i in range(3):
        member = resolve_member(recipe, intent_dir / f"m{i}.sqlite3", seed=5, member_index=i,
                                member_count=3, recipe_tree_id="t")
        run_scenario(member, batch / f"member_{i:04d}.sqlite3", run_by="test",
                     allow_dirty_data=True, allow_dirty_code=True)
    table = parameters(batch)
    row = table.loc[UPPER]
    assert row["affects"] == "flex" and row["target"] == "upper"
    assert row["range"] == "10.00% to 30.00%" and row["center"] == "20.00%"
    assert row["shape"] == "mode 20.00%"
    db = build_draws_db(batch)
    con = sqlite3.connect(db)
    try:
        node = con.execute("SELECT node_kind, affects, unit, low, high, mode FROM node WHERE name = ?",
                           (UPPER,)).fetchone()
    finally:
        con.close()
    assert node == ("strategy", "flex", "micros", 100_000, 300_000, 200_000)
