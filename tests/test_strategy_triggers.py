"""Strategy triggers on guard rails (#441, #326 stage 3b, ADR-010): a
strategy's ``trigger`` names a rail, the engine checks it each January on
the run so far, records a ``breach_event`` and asks the strategy's
``on_breach``; plus the ``month_start`` hook and ``ctx.history`` /
``ctx.rail``.

``alice_triggers`` (from the ``strategy_scenarios`` fixture) builds on
``alice_spending`` with a ``tight`` rail (spending above $60,000 a year in
today's dollars, from 2027) and a ``cut_on_breach`` strategy (a data-repo
kind) owning the ``flex:optional`` rules: after a year past the line it cuts
them by half until a year comes back inside. The ``guardrail`` strategy
inherited from ``alice_spending`` is switched off."""

from __future__ import annotations

import csv
import math
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim import guard_rails, provenance
from analysis.sim.connection import RunConnection
from analysis.sim.loader import LoadError
from analysis.sim.types import DollarMode

SCENARIO = "alice_triggers"
RESPOND = "strategies/respond.csv"


def _query(run: Path, sql: str, *args: object) -> list[tuple]:
    con = sqlite3.connect(run)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def _breaches(run: Path) -> list[tuple[int, str, int, int]]:
    """Each breach: year, state, years past, margin cents."""
    return _query(run, "SELECT year, state, years_past, margin_cents FROM breach_event ORDER BY id")


def _rewrite(path: Path, **changes: str) -> None:
    """Change the first row of a one-row CSV file."""
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    rows[0].update(changes)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _threshold(strategy_csv: Callable[[str, str], Path], dollars: str) -> None:
    _rewrite(strategy_csv(SCENARIO, "guard_rails.csv"), threshold=dollars)


def _report_margins(run: Path) -> dict[int, int]:
    """The ``tight`` rail's annual margins as ``sim report guard-rails``
    reads them from the whole run, in cents."""
    with RunConnection(run) as rc:
        rail, problems = guard_rails.parse_cells({
            "name": "tight", "measure": "spending", "side": "above", "threshold": "60000.00",
            "basis": "cpi_constant", "sense": "negative", "start": "2027-01",
        })
        assert rail is not None, problems
        (result,) = guard_rails.evaluate_run(rc, [rail], DollarMode.CPI_CONSTANT)
        years = result.years
    return {int(y): round(float(m) * 100)
            for y, m in zip(years.years, years.margin[0], strict=True) if not math.isnan(m)}


# ---------- the breach and its answer ----------

def test_each_january_reports_last_years_margin_and_the_answer_cuts(run_strategy_scenario) -> None:
    run = run_strategy_scenario(SCENARIO)
    told = _breaches(run)
    assert [year for year, *_ in told] == list(range(2027, 2040))
    assert told[0][1:3] == ("entered", 1)
    assert all(state == "continued" for _, state, _, _ in told[1:])
    assert [past for _, _, past, _ in told] == list(range(1, 14))
    # The first answer (January 2028) halves the optional rules, so 2028's
    # spending is closer to the line than 2027's.
    assert told[1][3] > told[0][3]
    months = _query(run, (
        "SELECT s.month, d.hook, d.note FROM strategy_decision d JOIN simulation_step s "
        "ON s.id = d.step_id WHERE d.breach_event_id IS NOT NULL ORDER BY d.id"))
    assert months[0] == ("2028-01-31", "on_breach", "tight annual entered")
    assert len(months) == len(told)


def test_the_run_and_the_report_see_the_same_margins(run_strategy_scenario) -> None:
    run = run_strategy_scenario(SCENARIO)
    in_run = {year: margin for year, _, _, margin in _breaches(run)}
    report = _report_margins(run)
    assert in_run == {year: report[year] for year in in_run}


def test_a_year_back_inside_is_left_and_ends_the_cut(run_strategy_scenario, strategy_csv) -> None:
    # $105,000 is just under 2027's full spending, so the half cut in 2028
    # brings it back inside; with the cut off, 2029 is past again.
    _threshold(strategy_csv, "105000.00")
    told = _breaches(run_strategy_scenario(SCENARIO))
    states = [(year, state) for year, state, _, _ in told]
    assert states[:3] == [(2027, "entered"), (2028, "left"), (2029, "entered")]
    left = told[1]
    assert left[2] == 0 and left[3] >= 0


def test_a_rail_never_past_tells_nothing(run_strategy_scenario, strategy_csv) -> None:
    _threshold(strategy_csv, "1000000.00")
    run = run_strategy_scenario(SCENARIO)
    assert _breaches(run) == []
    assert _query(run, "SELECT COUNT(*) FROM strategy_decision WHERE hook = 'on_breach'") == [(0,)]


def test_a_cumulative_trigger_watches_the_running_total(run_strategy_scenario, strategy_csv) -> None:
    _rewrite(strategy_csv(SCENARIO, RESPOND), trigger="tight:cumulative")
    told = _query(run_strategy_scenario(SCENARIO),
                  "SELECT DISTINCT margin_kind FROM breach_event")
    assert told == [("cumulative",)]


MOVER = '''
from analysis.sim.strategy_api import Decision, LotRef, Strategy, Transfer, register


@register
class Mover(Strategy):
    """On entering a breach, move $1,000 from savings to checking."""
    kind = "mover"

    def on_breach(self, ctx, breach):
        if breach.state != "entered":
            return None
        return Decision([Transfer(LotRef("Asset_Savings", "default"),
                                  LotRef("Asset_Checking", "default"), 100_000)],
                        note="topped up")
'''


def test_on_breach_may_move_money(run_strategy_scenario, strategy_csv, strategy_module) -> None:
    strategy_module("mover", MOVER)
    _rewrite(strategy_csv(SCENARIO, RESPOND), kind="mover", rules="")
    run = run_strategy_scenario(SCENARIO)
    legs = _query(run, (
        "SELECT t.date, t.amount FROM transfer_event t JOIN strategy_decision d "
        "ON d.id = t.strategy_decision_id WHERE d.hook = 'on_breach'"))
    assert legs == [("2028-01-31", 100_000)]


# ---------- month_start, history and rail ----------

SWEEP = '''
from dataclasses import dataclass
from analysis.sim.strategy_api import Decision, LotRef, Strategy, Transfer, register

SAVINGS = LotRef("Asset_Savings", "default")
CHECKING = LotRef("Asset_Checking", "default")


@register
class Sweep(Strategy):
    """Each month, move savings above a cap into checking."""
    kind = "sweep"

    @dataclass(frozen=True)
    class Params:
        cap: int = 1_000_000

    def month_start(self, ctx):
        balance = ctx.lots[SAVINGS].balance
        history = ctx.history.balances([SAVINGS])
        assert len(history) == len(ctx.history.months)
        assert all(m < ctx.month for m in ctx.history.months)
        if ctx.month.month == 1 and ctx.month.year > 2027:
            years = ctx.rail("tight")
            assert years.years[-1] == ctx.month.year - 1
        if balance <= self.params.cap:
            return None
        return Decision([Transfer(SAVINGS, CHECKING, balance - self.params.cap)],
                        detail={"history": len(history)})

    def on_breach(self, ctx, breach):
        return None
'''


def test_a_month_start_sweep_reads_history_and_rail(
        run_strategy_scenario, strategy_csv, strategy_module) -> None:
    strategy_module("sweep", SWEEP)
    _rewrite(strategy_csv(SCENARIO, RESPOND), kind="sweep", rules="")
    run = run_strategy_scenario(SCENARIO)
    sweeps = _query(run, (
        "SELECT s.month, d.chosen_cents, json_extract(d.detail_json, '$.history') "
        "FROM strategy_decision d JOIN simulation_step s ON s.id = d.step_id "
        "WHERE d.hook = 'month_start' ORDER BY d.id"))
    assert sweeps, "savings starts above the cap"
    first_month, moved, months_before = sweeps[0]
    assert moved > 0 and months_before == 0  # the first month has no history yet
    balances = _query(run, (
        "SELECT ls.balance FROM lot_state ls JOIN simulation_step s ON s.id = ls.step_id "
        "JOIN account_lot m ON m.id = ls.lot_id JOIN account a ON a.id = m.account_id "
        "WHERE a.name = 'Asset_Savings' AND s.month = ?"), first_month)
    assert balances[0][0] <= 1_000_000 + 100_000  # swept to the cap, plus a month's interest at most


# ---------- load errors ----------

@pytest.mark.parametrize(("trigger", "message"), [
    ("tight", "trigger 'tight' must be <rail>:annual or <rail>:cumulative"),
    ("tight:yearly", "must be <rail>:annual or <rail>:cumulative"),
    ("loose:annual", "no guard rail named 'loose'"),
    ("tight:annual tight:annual", "is named twice"),
])
def test_a_bad_trigger_fails_the_load(run_strategy_scenario, strategy_csv, trigger: str, message: str) -> None:
    _rewrite(strategy_csv(SCENARIO, RESPOND), trigger=trigger)
    with pytest.raises(LoadError, match=message):
        run_strategy_scenario(SCENARIO)


def test_a_trigger_needs_a_kind_with_on_breach(run_strategy_scenario, strategy_csv) -> None:
    _rewrite(strategy_csv(SCENARIO, RESPOND), kind="threshold",
             params_json='{"portfolio": "portfolio:invest"}')
    with pytest.raises(LoadError, match="kind 'threshold' has no on_breach hook, which a trigger needs"):
        run_strategy_scenario(SCENARIO)


# ---------- provenance ----------

def test_rails_join_the_tree_only_where_a_trigger_reads_them(strategy_scenarios, commit_test_data) -> None:
    data = commit_test_data()
    triggered = provenance.data_stamps(data, SCENARIO).tree_id
    untriggered = provenance.data_stamps(data, "alice_spending").tree_id
    rails = data / "scenarios" / "alice_spending" / "guard_rails.csv"
    rails.write_text(
        "name,measure,select,portfolio,side,threshold,basis,sense,start,end,description\n"
        "loose,spending,,,above,90000.00,cpi_constant,negative,,,\n")
    assert provenance.data_stamps(data, "alice_spending").tree_id == untriggered
    assert not provenance.data_stamps(data, "alice_spending").dirty
    # alice_triggers inherits alice_spending, so its trigger reads that rail too.
    after = provenance.data_stamps(data, SCENARIO)
    assert after.tree_id != triggered and after.dirty


# ---------- reports ----------

def test_strategy_log_explain_and_guard_rails_show_the_breaches(
        run_strategy_scenario, strategy_scenarios) -> None:
    run = run_strategy_scenario(SCENARIO)
    runner = CliRunner()
    log = runner.invoke(main, ["sim", "strategy", "log", "--runfile", str(run), "--breaches"],
                        terminal_width=200)
    assert log.exit_code == 0, log.output
    assert "entered" in log.output and "continued" in log.output and "-$" in log.output
    explain = runner.invoke(main, ["sim", "explain", "--runfile", str(run)], terminal_width=200)
    assert explain.exit_code == 0, explain.output
    assert ("tight:annual: past 1 time(s), 13 year(s) in all, answered 13 of 13, "
            "back inside 0 time(s)") in explain.output
    report = runner.invoke(main, ["sim", "report", "guard-rails", "--runfile", str(run),
                                  "--data-dir", str(strategy_scenarios)],
                           terminal_width=200)
    assert report.exit_code == 0, report.output
    assert "tight:annual past from 2027, respond answered 13, not back inside" in report.output
    logged = runner.invoke(main, ["sim", "strategy", "log", "--runfile", str(run)], terminal_width=200)
    assert "tight:annual 2027 entered" in logged.output


# ---------- editing ----------

def test_strategy_edit_sets_and_clears_a_trigger(strategy_scenarios, strategy_csv) -> None:
    def edit(*args: str, input: str | None = None):
        result = CliRunner().invoke(main, ["sim", "strategy", "edit", "respond", "--scenario", SCENARIO,
                                           "--data-dir", str(strategy_scenarios), *args], input=input)
        assert result.exit_code == 0, result.output
        return result.output

    assert "answering tight:annual tight:cumulative" in edit("--trigger", "tight:annual  tight:cumulative")
    assert _read_row(strategy_csv)["trigger"] == "tight:annual tight:cumulative"
    bad = CliRunner().invoke(main, ["sim", "strategy", "edit", "respond", "--scenario", SCENARIO,
                                    "--data-dir", str(strategy_scenarios), "--trigger", "loose:annual"])
    assert bad.exit_code != 0 and "no guard rail named 'loose'" in bad.output
    edit("--trigger", "")
    assert _read_row(strategy_csv)["trigger"] == ""


def test_strategy_add_asks_a_breach_kind_for_its_trigger(strategy_scenarios, strategy_csv) -> None:
    strategy_csv(SCENARIO, RESPOND).unlink()
    # kind, rule tag, trigger, then the cut (its default)
    result = CliRunner().invoke(
        main, ["sim", "strategy", "add", "again", "--scenario", SCENARIO, "--data-dir", str(strategy_scenarios)],
        input="cut_on_breach\nflex:optional\ntight:cumulative\n\n")
    assert result.exit_code == 0, result.output
    assert "guard rails: budget, four_percent, gk_lower, gk_upper, tight" in result.output
    with strategy_csv(SCENARIO, "strategies/again.csv").open(newline="") as f:
        assert next(csv.DictReader(f))["trigger"] == "tight:cumulative"


def _read_row(strategy_csv: Callable[[str, str], Path]) -> dict[str, str]:
    with strategy_csv(SCENARIO, RESPOND).open(newline="") as f:
        return next(csv.DictReader(f))
