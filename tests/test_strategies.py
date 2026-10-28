# cspell:ignore nohooks nosuch
"""Responsive strategies (#326 stage 1a, ADR-010): the API, finding kinds,
validating strategies.csv/targets.csv at load, and the engine asking a
strategy at a refill draw.

The persona scenarios come from the ``strategy_scenarios`` fixture:
``alice_rebalance`` (built-in ``rebalance`` owning checking's refill chain
Savings -> Brokerage (USSTK) -> IRA (BONDX), targets 60/40) and
``alice_last_leaf`` (the data-repo kind ``last_leaf``)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from analysis.sim import provenance, strategy_code
from analysis.sim.asset_classes import UNCLASSIFIED, class_weights, is_lot_key, lot_key
from analysis.sim.loader import LoadError, load_scenario
from analysis.sim.strategy_api import (
    Allocation, Dollars, LotRef, Percent, Strategy, StrategyError, Tag, TargetSet,
    is_registered, param_units, percent, register,
)
from analysis.sim.strategy_load import convert_params, parse_enabled

IRA = "Asset_TraditionalIRA_Alice:default"
BROKERAGE = "Asset_Brokerage_Joint:default"


def _decisions(run: Path) -> list[tuple]:
    con = sqlite3.connect(run)
    try:
        return con.execute(
            "SELECT s.month, d.hook, d.asked_cents, d.chosen_cents, d.fallback_cents, d.note, "
            "       d.detail_json, d.memory_json "
            "FROM strategy_decision d JOIN simulation_step s ON s.id = d.step_id ORDER BY d.id"
        ).fetchall()
    finally:
        con.close()


def _load_errors(data_dir: Path, scenario: str, intent_dir: Path) -> str:
    with pytest.raises(LoadError) as info:
        load_scenario(data_dir, scenario, intent_dir=intent_dir, allow_dirty_data=True,
                      compute_stamps=False)
    return str(info.value)


# ---------- the API ----------

def test_register_records_hooks_and_marks_only_the_class() -> None:
    @register
    class Drawer(Strategy):
        """Draws."""

        kind = "drawer_test"

        def draw(self, ctx, chain, need):
            return None

    class Sub(Drawer):
        pass

    assert Drawer.hooks == frozenset({"draw"})
    assert is_registered(Drawer)
    assert not is_registered(Sub)


@pytest.mark.parametrize(("body", "message"), [
    ("kind = 'no hooks'\n", "kind must be set to an identifier"),
    ("kind = 'nohooks'\n", "implements no hook"),
])
def test_register_refuses_bad_classes(body: str, message: str) -> None:
    namespace: dict = {}
    exec(f"class Bad(Strategy):\n    {body}", {"Strategy": Strategy}, namespace)
    with pytest.raises(StrategyError, match=message):
        register(namespace["Bad"])


def test_param_units_and_conversion() -> None:
    @register
    class Units(Strategy):
        """Uses every unit."""

        kind = "units_test"

        @dataclass(frozen=True)
        class Params:
            portfolio: Tag
            targets: TargetSet
            band: Percent = percent("5%")
            floor: Dollars = 0
            label: str = ""

        def draw(self, ctx, chain, need):
            return None

    assert {k: u.name if u else None for k, u in param_units(Units.Params).items()} == {
        "portfolio": "tag", "targets": "target_set", "band": "percent", "floor": "dollars",
        "label": None,
    }
    out, problems = convert_params(
        Units, {"portfolio": "portfolio:invest", "targets": "main", "band": "2.5%",
                "floor": "1200.50", "label": "x"},
        tags={"portfolio:invest"}, target_sets={"main"},
    )
    assert problems == []
    assert out == {"portfolio": "portfolio:invest", "targets": "main", "band": 25_000,
                   "floor": 120_050, "label": "x"}
    _out, problems = convert_params(
        Units, {"portfolio": "portfolio:other", "targets": "nope", "band": 5, "extra": 1},
        tags={"portfolio:invest"}, target_sets={"main"},
    )
    assert any("unknown parameter 'extra'" in p for p in problems)
    assert any("no account carries tag 'portfolio:other'" in p for p in problems)
    assert any("no target set 'nope'" in p for p in problems)
    assert any("'band'" in p and "percent string" in p for p in problems)
    _out, problems = convert_params(Units, {}, tags=(), target_sets=())
    assert "parameter 'portfolio' is required" in problems


@pytest.mark.parametrize(("raw", "expected"), [
    ("", True), ("true", True), ("Yes", True), ("1", True), ("false", False), ("no", False), ("0", False),
])
def test_parse_enabled(raw: str, expected: bool) -> None:
    assert parse_enabled(raw) is expected


def test_parse_enabled_refuses_other_words() -> None:
    with pytest.raises(ValueError, match="enabled must be true or false"):
        parse_enabled("maybe")


def test_allocation_after_moves_class_dollars() -> None:
    stock, bond = LotRef("B", "default"), LotRef("I", "default")
    allocation = Allocation({"stock_us": 30_000, "bond": 70_000},
                            {stock: {"stock_us": 1.0}, bond: {"bond": 1.0}})
    after = allocation.after({bond: -20_000, LotRef("Savings", "default"): -5_000})
    assert after.dollars == {"stock_us": 30_000, "bond": 50_000}
    assert after.shares == pytest.approx({"stock_us": 0.375, "bond": 0.625})


# ---------- asset classes for symbol-less lots ----------

def test_lot_keys_classify_a_lot_without_a_symbol() -> None:
    assert lot_key("Asset_House", "default") == "Asset_House:default"
    assert is_lot_key("Asset_House:default")
    assert not is_lot_key("USSTK")
    sets = {"Asset_House:default": [("", {"real_estate": 1.0})]}
    weights = class_weights(symbol=None, account="Asset_House", lot="default", subkind="real_estate",
                            sets=sets, date="2027-01-31")
    assert weights == {"real_estate": 1.0}
    unknown = class_weights(symbol=None, account="Asset_Other", lot="default", subkind="brokerage_taxable",
                            sets=sets, date="2027-01-31")
    assert UNCLASSIFIED in unknown


# ---------- finding kinds ----------

def test_builtin_kinds_include_rebalance() -> None:
    kinds = strategy_code.StrategyKinds({})
    assert kinds.origin["rebalance"] == "built-in"
    assert kinds.kinds["rebalance"].hooks == frozenset({"draw", "place"})


_ECHO = '''
from analysis.sim.strategy_api import Strategy, register

@register
class Echo(Strategy):
    """Version {n}."""
    kind = "echo"
    def draw(self, ctx, chain, need):
        return None
'''


def test_two_versions_of_a_module_stay_apart() -> None:
    one = strategy_code.StrategyKinds({"echo": _ECHO.format(n=1)})
    two = strategy_code.StrategyKinds({"echo": _ECHO.format(n=2)})
    assert one.problems == two.problems == []
    assert one.kinds["echo"] is not two.kinds["echo"]
    assert "Version 1" in (one.kinds["echo"].__doc__ or "")
    assert "Version 2" in (two.kinds["echo"].__doc__ or "")
    assert one.origin["echo"] == "strategies/echo.py"


@pytest.mark.parametrize(("source", "message"), [
    ("raise RuntimeError('boom')\n", "failed to import"),
    ("API_VERSION = 99\n", "written for strategy API 99"),
    (_ECHO.format(n=1).replace('"echo"', '"rebalance"'), "kind 'rebalance' is already defined (built-in)"),
])
def test_module_problems_are_reported_not_raised(source: str, message: str) -> None:
    kinds = strategy_code.StrategyKinds({"mine": source})
    assert any(message in p for p in kinds.problems), kinds.problems
    assert kinds.origin["rebalance"] == "built-in"


# ---------- loading ----------

def test_rebalance_loads_into_the_intent(
    strategy_scenarios: Path, load_test_scenario: Callable[[str], Path],
) -> None:
    intent = load_test_scenario("alice_rebalance")
    con = sqlite3.connect(intent)
    try:
        name, kind, chain, params = con.execute(
            "SELECT s.name, s.kind, c.name, s.params_json FROM strategy s JOIN chain c ON c.id = s.chain_id"
        ).fetchone()
        targets = con.execute(
            "SELECT target_set, asset_class, weight_micros FROM allocation_target ORDER BY asset_class"
        ).fetchall()
        sources = con.execute("SELECT COUNT(*) FROM strategy_source").fetchone()[0]
    finally:
        con.close()
    assert (name, kind, chain) == ("draw_by_allocation", "rebalance", "portfolio_draw")
    assert json.loads(params) == {"portfolio": "portfolio:invest", "targets": "main"}
    assert targets == [("main", "bond", 400_000), ("main", "stock_us", 600_000)]
    assert sources == 0  # a built-in needs no snapshot


def test_a_data_repo_kind_snapshots_every_module(
    strategy_scenarios: Path, load_test_scenario: Callable[[str], Path],
) -> None:
    con = sqlite3.connect(load_test_scenario("alice_last_leaf"))
    try:
        modules = [m for (m,) in con.execute("SELECT module FROM strategy_source ORDER BY module")]
    finally:
        con.close()
    assert modules == ["_helpers", "last_leaf"]


def _rewrite_strategy(strategy_csv: Callable[[str, str], Path], scenario: str, **changes: str) -> None:
    import csv

    path = strategy_csv(scenario, "strategies.csv")
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    rows[0].update(changes)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


@pytest.mark.parametrize(("changes", "message"), [
    ({"kind": "nosuch"}, "unknown kind 'nosuch'"),
    ({"chain": "Asset_Checking"}, "chain 'Asset_Checking' is an account"),
    ({"chain": "no_chain"}, "no chain named 'no_chain'"),
    ({"params_json": "{"}, "params_json is not valid JSON"),
    ({"params_json": '{"portfolio":"portfolio:invest","targets":"other"}'}, "no target set 'other'"),
    ({"params_json": '{"portfolio":"portfolio:none","targets":"main"}'}, "no account carries tag"),
    ({"start": "2030-01-31", "end": "2029-01-31"}, "start 2030-01-31 is after end 2029-01-31"),
    ({"enabled": "maybe"}, "enabled must be true or false"),
])
def test_bad_strategy_rows_fail_the_load(
    strategy_scenarios: Path, intent_dir: Path, strategy_csv: Callable[[str, str], Path],
    changes: dict[str, str], message: str,
) -> None:
    _rewrite_strategy(strategy_csv, "alice_rebalance", **changes)
    errors = _load_errors(strategy_scenarios, "alice_rebalance", intent_dir)
    assert message in errors
    assert "strategies.csv" in errors and "draw_by_allocation" in errors


def test_targets_must_add_up_to_100(
    strategy_scenarios: Path, intent_dir: Path, strategy_csv: Callable[[str, str], Path],
) -> None:
    strategy_csv("alice_rebalance", "targets.csv").write_text(
        "target_set,as_of,asset_class,weight\nmain,,stock_us,60%\nmain,,bond,30%\n"
    )
    errors = _load_errors(strategy_scenarios, "alice_rebalance", intent_dir)
    assert "targets.csv (main): weights add up to 90%, not 100%" in errors


def test_two_strategies_cannot_own_one_chain(
    strategy_scenarios: Path, intent_dir: Path, strategy_csv: Callable[[str, str], Path],
) -> None:
    path = strategy_csv("alice_rebalance", "strategies.csv")
    lines = path.read_text().splitlines()
    path.write_text("\n".join([*lines, lines[1].replace("draw_by_allocation", "second", 1)]) + "\n")
    errors = _load_errors(strategy_scenarios, "alice_rebalance", intent_dir)
    assert "chain 'portfolio_draw' is already owned by strategy 'draw_by_allocation'" in errors


def test_a_disabled_strategy_is_checked_but_not_written(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path],
    load_test_scenario: Callable[[str], Path],
) -> None:
    _rewrite_strategy(strategy_csv, "alice_rebalance", enabled="false")
    con = sqlite3.connect(load_test_scenario("alice_rebalance"))
    try:
        assert con.execute("SELECT COUNT(*) FROM strategy").fetchone()[0] == 0
    finally:
        con.close()


def test_an_unclassified_portfolio_lot_names_the_row_to_add(
    strategy_scenarios: Path, intent_dir: Path,
) -> None:
    (strategy_scenarios / "allocations.csv").write_text(
        "symbol,as_of,asset_class,weight\nUSSTK,,stock_us,100%\n"
    )
    errors = _load_errors(strategy_scenarios, "alice_rebalance", intent_dir)
    assert f"lot {IRA} in portfolio portfolio:invest has no asset class" in errors
    assert "symbol BONDX" in errors


def test_an_account_lot_key_classifies_a_lot(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path],
    load_test_scenario: Callable[[str], Path],
) -> None:
    """``account:lot`` in allocations.csv classes a lot with no symbol (Bob, #326)."""
    lots = strategy_csv("alice_rebalance", "account_lots.csv")
    lots.write_text(lots.read_text().replace(",BONDX,", ",,"))
    (strategy_scenarios / "allocations.csv").write_text(
        f"symbol,as_of,asset_class,weight\nUSSTK,,stock_us,100%\n{IRA},,bond,100%\n"
    )
    load_test_scenario("alice_rebalance")


def test_a_broken_module_fails_only_a_scenario_with_strategies(
    strategy_scenarios: Path, strategy_module: Callable[[str, str], Path], intent_dir: Path,
    load_test_scenario: Callable[[str], Path],
) -> None:
    strategy_module("broken", "raise RuntimeError('boom')\n")
    errors = _load_errors(strategy_scenarios, "alice_rebalance", intent_dir)
    assert "strategies/broken.py failed to import" in errors
    result = load_scenario(strategy_scenarios, "retire_alice", intent_dir=intent_dir / "plain",
                           allow_dirty_data=True, compute_stamps=False)
    assert any("strategies/broken.py" in u for u in result.unused)


def test_strategies_folder_is_in_the_provenance_scope(strategy_scenarios: Path) -> None:
    assert "strategies" in provenance._scoped_paths(strategy_scenarios, "alice_rebalance")


# ---------- running ----------

def _legs(run: Path) -> list[tuple]:
    """Each strategy-linked leg: (decision id, from account:lot, amount)."""
    con = sqlite3.connect(run)
    try:
        return con.execute(
            "SELECT t.strategy_decision_id, a.name || ':' || m.lot, t.amount "
            "FROM transfer_event t JOIN account_lot m ON m.id = t.from_lot_id "
            "JOIN account a ON a.id = m.account_id "
            "WHERE t.strategy_decision_id IS NOT NULL ORDER BY t.id"
        ).fetchall()
    finally:
        con.close()


def test_rebalance_draws_from_the_overweight_class(
    run_strategy_scenario: Callable[[str], Path],
) -> None:
    """Bonds (the IRA) start overweight against a 40% target: refills come from
    the IRA, not Savings, the chain's first step."""
    run = run_strategy_scenario("alice_rebalance")
    rows = _decisions(run)
    assert rows
    _month, hook, asked, chosen, fallback, note, detail, _memory = rows[0]
    assert hook == "draw"
    assert chosen == asked and fallback == 0
    assert note.startswith("bond ") and f"from {IRA}" in note
    assert json.loads(detail)["targets"] == {"bond": 0.4, "stock_us": 0.6}
    assert {source for _d, source, _a in _legs(run)} == {IRA}


def test_rebalance_draws_stock_when_stock_is_overweight(
    strategy_csv: Callable[[str, str], Path], run_strategy_scenario: Callable[[str], Path],
) -> None:
    strategy_csv("alice_rebalance", "targets.csv").write_text(
        "target_set,as_of,asset_class,weight\nmain,,stock_us,20%\nmain,,bond,80%\n"
    )
    run = run_strategy_scenario("alice_rebalance")
    note = _decisions(run)[0][5]
    assert note.startswith("stock_us") and f"from {BROKERAGE}" in note
    assert BROKERAGE in {source for _d, source, _a in _legs(run)}


def test_within_the_band_the_chain_decides(
    strategy_csv: Callable[[str, str], Path], run_strategy_scenario: Callable[[str], Path],
) -> None:
    """A band wide enough that nothing ever drifts past it: no decisions, and
    the run matches one without the strategy."""
    _rewrite_strategy(
        strategy_csv, "alice_rebalance",
        params_json='{"portfolio":"portfolio:invest","targets":"main","band":"100%"}',
    )
    run = run_strategy_scenario("alice_rebalance")
    assert _decisions(run) == []
    assert _legs(run) == []


def test_a_data_repo_strategy_runs_and_keeps_memory(
    run_strategy_scenario: Callable[[str], Path],
) -> None:
    rows = _decisions(run_strategy_scenario("alice_last_leaf"))
    assert rows
    assert [json.loads(r[7]) for r in rows[:3]] == [{"draws": 1}, {"draws": 2}, {"draws": 3}]
    assert all(r[5] == f"all from {IRA}" for r in rows)


def test_a_run_is_the_same_every_time(
    run_strategy_scenario: Callable[[str], Path], runs_dir: Path,
) -> None:
    first = _decisions(run_strategy_scenario("alice_rebalance"))
    (runs_dir / "alice_rebalance.sqlite3").rename(runs_dir / "first.sqlite3")
    assert _decisions(run_strategy_scenario("alice_rebalance")) == first


_STRATEGY = '''
from analysis.sim.strategy_api import Decision, LotRef, Put, Strategy, Take, register

@register
class Odd(Strategy):
    """For tests."""
    kind = "odd"
    def draw(self, ctx, chain, need):
{body}
'''


def _odd(strategy_module: Callable[[str, str], Path], strategy_csv: Callable[[str, str], Path],
         body: str) -> None:
    strategy_module("odd", _STRATEGY.format(body="\n".join("        " + b for b in body.splitlines())))
    _rewrite_strategy(strategy_csv, "alice_last_leaf", kind="odd")


@pytest.mark.parametrize(("body", "message"), [
    ("raise ValueError('nope')", "ValueError: nope"),
    ("return 5", "returned int, not a Decision"),
    ("return Decision([Put(chain.leaves[0], need)])", "a draw answers with Take"),
    ("return Decision([Take(LotRef('Asset_Checking', 'default'), need)])", "is not a leaf of chain"),
    ("return Decision([Take(chain.leaves[0], need + 1)])", "more than the"),
    ("return Decision([Take(chain.leaves[0], 0.5)])", "must be whole cents"),
    ("ctx.memory['when'] = ctx.month\nreturn None", "memory must hold JSON values"),
])
def test_a_misbehaving_strategy_fails_the_run(
    strategy_module: Callable[[str, str], Path], strategy_csv: Callable[[str, str], Path],
    run_strategy_scenario: Callable[[str], Path], body: str, message: str,
) -> None:
    _odd(strategy_module, strategy_csv, body)
    with pytest.raises(StrategyError, match=message) as info:
        run_strategy_scenario("alice_last_leaf")
    assert "strategy 'draw_by_allocation' (odd) draw in " in str(info.value)


def test_a_partial_take_falls_back_to_the_chain(
    strategy_module: Callable[[str, str], Path], strategy_csv: Callable[[str, str], Path],
    run_strategy_scenario: Callable[[str], Path],
) -> None:
    """The strategy pays $1 of each need; the chain's own steps pay the rest."""
    _odd(strategy_module, strategy_csv,
         "return Decision([Take(chain.leaves[-1], 100)], note='a dollar')")
    rows = _decisions(run_strategy_scenario("alice_last_leaf"))
    assert rows
    for _month, _hook, asked, chosen, fallback, note, _detail, _memory in rows:
        assert note == "a dollar"
        assert chosen == 100
        assert chosen + fallback == asked


# ---------- names that follow a rename, and keys that aren't symbols ----------

def _sim(data_dir: Path, *args: str, scenario: str = "alice_rebalance"):
    from click.testing import CliRunner

    from analysis.cli import main

    return CliRunner().invoke(
        main, ["sim", *args, "--data-dir", str(data_dir), "--scenario", scenario], terminal_width=200,
    )


def _ira_by_lot_key(strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path]) -> None:
    lots = strategy_csv("alice_rebalance", "account_lots.csv")
    lots.write_text(lots.read_text().replace(",BONDX,", ",,"))
    (strategy_scenarios / "allocations.csv").write_text(
        f"symbol,as_of,asset_class,weight\nUSSTK,,stock_us,100%\n{IRA},,bond,100%\n"
    )


def test_a_lot_key_follows_an_account_rename(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path],
    load_test_scenario: Callable[[str], Path],
) -> None:
    _ira_by_lot_key(strategy_scenarios, strategy_csv)
    result = _sim(strategy_scenarios, "account", "rename", "Asset_TraditionalIRA_Alice", "Asset_IRA_Alice")
    assert result.exit_code == 0, result.output
    text = (strategy_scenarios / "allocations.csv").read_text()
    assert "Asset_IRA_Alice:default,,bond,100%" in text
    load_test_scenario("alice_rebalance")


def test_a_lot_key_follows_a_lot_rename(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path],
    load_test_scenario: Callable[[str], Path],
) -> None:
    _ira_by_lot_key(strategy_scenarios, strategy_csv)
    result = _sim(strategy_scenarios, "account", "lot", "rename", "Asset_TraditionalIRA_Alice", "default",
                  "bonds")
    assert result.exit_code == 0, result.output
    text = (strategy_scenarios / "allocations.csv").read_text()
    assert "Asset_TraditionalIRA_Alice:bonds,,bond,100%" in text
    assert IRA not in text
    load_test_scenario("alice_rebalance")


def test_a_strategy_follows_its_chain_rename(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path],
    load_test_scenario: Callable[[str], Path],
) -> None:
    result = _sim(strategy_scenarios, "chain", "rename", "portfolio_draw", "refill_draw")
    assert result.exit_code == 0, result.output
    assert ",refill_draw," in strategy_csv("alice_rebalance", "strategies.csv").read_text()
    load_test_scenario("alice_rebalance")


def test_lot_keys_are_not_symbols(strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path]) -> None:
    from analysis.sim import names, prices, symbols

    _ira_by_lot_key(strategy_scenarios, strategy_csv)
    assert IRA not in prices.known_symbols(strategy_scenarios)
    assert "USSTK" in prices.known_symbols(strategy_scenarios)
    assert IRA not in names.symbols(strategy_scenarios)
    assert IRA not in set(symbols.symbol_table(strategy_scenarios)["symbol"])


# ---------- the commands ----------

def _cli(data_dir: Path, *args: str, input: str | None = None):  # noqa: A002 -- CliRunner's name
    from click.testing import CliRunner

    from analysis.cli import main

    return CliRunner().invoke(main, ["sim", *args, "--data-dir", str(data_dir)], input=input,
                              terminal_width=200)


def test_strategy_kinds_lists_parameters_with_their_docs(strategy_scenarios: Path) -> None:
    result = _cli(strategy_scenarios, "strategy", "kinds")
    assert result.exit_code == 0, result.output
    assert "rebalance  (built-in; hooks: draw, place)" in result.output
    assert "last_leaf  (strategies/last_leaf.py; hooks: draw)" in result.output
    assert "band       (percent, default 5%)  Drift from target, per class" in result.output
    assert "portfolio  (tag, required)  Account tag whose asset lots are the portfolio." in result.output


def test_strategy_list_marks_inherited_rows(strategy_scenarios: Path) -> None:
    result = _cli(strategy_scenarios, "strategy", "list", "alice_last_leaf")
    assert result.exit_code == 0, result.output
    assert "draw_by_allocation  last_leaf on portfolio_draw" in result.output
    result = _cli(strategy_scenarios, "strategy", "list", "retire_alice")
    assert "no strategies in retire_alice" in result.output


def _drop_strategies(data_dir: Path, strategy_csv: Callable[[str, str], Path]) -> None:
    strategy_csv("alice_last_leaf", "strategies.csv").unlink()
    result = _cli(data_dir, "strategy", "delete", "draw_by_allocation", "--yes", "--scenario", "alice_rebalance")
    assert result.exit_code == 0, result.output


def test_strategy_add_with_options_writes_a_checked_row(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path],
) -> None:
    _drop_strategies(strategy_scenarios, strategy_csv)
    result = _cli(strategy_scenarios, "strategy", "add", "steer", "--kind", "rebalance",
                  "--chain", "portfolio_draw", "--param", "portfolio=portfolio:invest",
                  "--param", "targets=main", "--param", "band=3%", "--start", "2027-01",
                  "--scenario", "alice_rebalance")
    assert result.exit_code == 0, result.output
    text = strategy_csv("alice_rebalance", "strategies.csv").read_text()
    assert 'steer,rebalance,portfolio_draw,2027-01,,,"{""portfolio"":""portfolio:invest"",""targets"":' in text
    assert '""band"":""3%""' in text


def test_strategy_add_is_guided(strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path]) -> None:
    _drop_strategies(strategy_scenarios, strategy_csv)
    # kind, chain, then portfolio, targets, band (default), step (default)
    result = _cli(strategy_scenarios, "strategy", "add", "steer", "--scenario", "alice_rebalance",
                  input="rebalance\nportfolio_draw\nportfolio:invest\nmain\n\n\n")
    assert result.exit_code == 0, result.output
    assert "chains: asset_checking_liquidity, portfolio_draw" in result.output
    assert ',"{""portfolio"":""portfolio:invest"",""targets"":""main""}",' in (
        strategy_csv("alice_rebalance", "strategies.csv").read_text())


def test_a_bad_strategy_is_refused_with_nothing_written(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path],
) -> None:
    before = strategy_csv("alice_rebalance", "strategies.csv").read_text()
    result = _cli(strategy_scenarios, "strategy", "edit", "draw_by_allocation", "--param", "targets=nope",
                  "--scenario", "alice_rebalance")
    assert result.exit_code != 0
    assert "no target set 'nope'" in result.output
    assert strategy_csv("alice_rebalance", "strategies.csv").read_text() == before


def test_editing_an_inherited_strategy_writes_an_override(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path],
    load_test_scenario: Callable[[str], Path],
) -> None:
    child = strategy_csv("alice_last_leaf", "strategies.csv")
    child.unlink()
    result = _cli(strategy_scenarios, "strategy", "edit", "draw_by_allocation", "--param", "band=2%",
                  "--scenario", "alice_last_leaf")
    assert result.exit_code == 0, result.output
    assert "overriding alice_rebalance's" in result.output
    assert '""band"":""2%""' in child.read_text()
    con = sqlite3.connect(load_test_scenario("alice_last_leaf"))
    try:
        (params,) = con.execute("SELECT params_json FROM strategy").fetchone()
    finally:
        con.close()
    assert json.loads(params)["band"] == 20_000


def test_deleting_an_inherited_strategy_is_refused(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path],
) -> None:
    strategy_csv("alice_last_leaf", "strategies.csv").unlink()
    result = _cli(strategy_scenarios, "strategy", "delete", "draw_by_allocation", "--yes",
                  "--scenario", "alice_last_leaf")
    assert result.exit_code != 0
    assert "inherited from alice_rebalance" in result.output and "--disable" in result.output


def test_disable_switches_a_strategy_off(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path],
) -> None:
    result = _cli(strategy_scenarios, "strategy", "edit", "draw_by_allocation", "--disable",
                  "--scenario", "alice_rebalance")
    assert result.exit_code == 0, result.output
    assert "(disabled)" in result.output
    assert ",false," in strategy_csv("alice_rebalance", "strategies.csv").read_text()


def test_log_explain_and_drift_read_the_run(
    strategy_scenarios: Path, run_strategy_scenario: Callable[[str], Path],
) -> None:
    run = str(run_strategy_scenario("alice_rebalance"))
    result = _cli(strategy_scenarios, "strategy", "log", run, "--strategy", "draw_by_allocation")
    assert result.exit_code == 0, result.output
    assert "draw_by_allocation" in result.output and f"from {IRA}" in result.output
    assert "chain steps" in result.output
    result = _cli(strategy_scenarios, "explain", run)
    assert "strategies (1;" in result.output
    assert "draw_by_allocation (rebalance on portfolio_draw):" in result.output
    result = _cli(strategy_scenarios, "allocation", "--drift", "--runfile", run)
    assert result.exit_code == 0, result.output
    assert "drift from target set main, percentage points (accounts tagged portfolio:invest" in result.output
    assert "bond" in result.output and "stock_us" in result.output and "max_abs" in result.output


def test_strategy_check_runs_twice(strategy_scenarios: Path) -> None:
    result = _cli(strategy_scenarios, "strategy", "check", "alice_last_leaf")
    assert result.exit_code == 0, result.output
    assert "the same in both runs" in result.output


def test_strategy_check_finds_a_strategy_that_reads_the_clock(
    strategy_scenarios: Path, strategy_module: Callable[[str, str], Path],
    strategy_csv: Callable[[str, str], Path],
) -> None:
    _odd(strategy_module, strategy_csv,
         "import time\nreturn Decision([Take(chain.leaves[-1], 100)], note=str(time.perf_counter_ns()))")
    result = _cli(strategy_scenarios, "strategy", "check", "alice_last_leaf")
    assert result.exit_code != 0
    assert "decided differently in" in result.output


def test_target_set_list_and_delete(strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path]) -> None:
    result = _cli(strategy_scenarios, "target", "set", "main", "stock_us=70", "--as-of", "2030-01",
                  "--scenario", "alice_rebalance")
    assert result.exit_code != 0
    assert "targets.csv (main as of 2030-01-31): weights add up to 70%, not 100%" in result.output
    result = _cli(strategy_scenarios, "target", "set", "main", "stock_us=70%", "bond=30%", "--as-of", "2030-01",
                  "--scenario", "alice_rebalance")
    assert result.exit_code == 0, result.output
    result = _cli(strategy_scenarios, "target", "list", "alice_last_leaf")
    assert "main: bond 40%, stock_us 60%  [from alice_rebalance]" in result.output
    assert "main as of 2030-01: bond 30%, stock_us 70%  [from alice_rebalance]" in result.output
    result = _cli(strategy_scenarios, "target", "delete", "main", "--as-of", "2030-01",
                  "--scenario", "alice_last_leaf")
    assert result.exit_code != 0 and "inherited from alice_rebalance" in result.output
    result = _cli(strategy_scenarios, "target", "delete", "main", "--as-of", "2030-01",
                  "--scenario", "alice_rebalance")
    assert result.exit_code == 0, result.output
    assert "2030" not in strategy_csv("alice_rebalance", "targets.csv").read_text()
    result = _cli(strategy_scenarios, "target", "delete", "main", "--scenario", "alice_rebalance")
    assert result.exit_code != 0 and "no target set 'main'" in result.output


def test_a_child_target_set_zeroes_classes_it_leaves_out(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path],
) -> None:
    result = _cli(strategy_scenarios, "target", "set", "main", "stock_us=100%", "--scenario", "alice_last_leaf")
    assert result.exit_code == 0, result.output
    assert "stock_us 100%, bond 0%" in result.output
    assert "main,,bond,0%" in strategy_csv("alice_last_leaf", "targets.csv").read_text()


def test_rebalance_places_overflow_into_the_underweight_class(
    strategy_csv: Callable[[str, str], Path], run_strategy_scenario: Callable[[str], Path],
) -> None:
    """Checking overflows into a strategy-owned destination chain listing the
    IRA first: with bonds overweight, the money goes to the brokerage's stock."""
    destination = strategy_csv("alice_rebalance", "chains") / "destination"
    destination.mkdir()
    (destination / "portfolio_place.csv").write_text(
        "account_name,lot,maximum,percent,series,cadence\n"
        "Asset_TraditionalIRA_Alice,default,,,,\nAsset_Brokerage_Joint,default,,,,\n"
    )
    lots = strategy_csv("alice_rebalance", "account_lots.csv")
    lots.write_text(lots.read_text().replace(
        "Asset_Checking,,,,,,,5000.00,,,2000.00,,portfolio_draw,,,,,",
        "Asset_Checking,,,,,,,5000.00,,,2000.00,6000.00,portfolio_draw,,,,portfolio_place,",
    ))
    path = strategy_csv("alice_rebalance", "strategies.csv")
    path.write_text(path.read_text() + 'place_by_allocation,rebalance,portfolio_place,,,,'
                    '"{""portfolio"":""portfolio:invest"",""targets"":""main""}",\n')
    run = run_strategy_scenario("alice_rebalance")
    places = [r for r in _decisions(run) if r[1] == "place"]
    assert places, "checking never overflowed"
    _month, _hook, asked, chosen, fallback, note, _detail, _memory = places[0]
    assert chosen == asked and fallback == 0
    assert note.startswith("stock_us ") and f"into {BROKERAGE}" in note
