"""Spending strategies (#387, #326 stage 2, ADR-010): the ``year_start`` and
``scale`` hooks, rules owned by tag, the recorded baseline, the built-in
``threshold``, ``guardrail`` and ``ceiling_floor``, and ``sim report
spending``.

``alice_spending`` (from the ``strategy_scenarios`` fixture) builds on
``alice_rebalance`` with two optional rules tagged ``flex:optional``:
travel ($1,500 a month) and gifts ($12,000 each December), both indexed to
CPI, owned by a ``guardrail`` strategy named ``flex`` over the
``portfolio:invest`` accounts ($800,000 at the start)."""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest

from analysis.sim import reports
from analysis.sim.loader import LoadError, load_scenario
from analysis.sim.strategy_api import StrategyError

SCENARIO = "alice_spending"
FLEX = "strategies/flex.csv"


def _rewrite(path: Path, name: str, **changes: str) -> None:
    """Change the row named ``name`` of a CSV file."""
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        if row["name"] == name:
            row.update(changes)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _rewrite_flex(strategy_csv: Callable[[str, str], Path], **changes: str) -> None:
    """Change the ``flex`` strategy's file (one row)."""
    path = strategy_csv(SCENARIO, FLEX)
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    rows[0].update(changes)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _strategy(strategy_csv: Callable[[str, str], Path], kind: str, **params: object) -> None:
    _rewrite_flex(strategy_csv, kind=kind, params_json=json.dumps({"portfolio": "portfolio:invest", **params}))


def _down_year(strategy_csv: Callable[[str, str], Path]) -> None:
    """Give the portfolio one down year: US stocks (the brokerage's USSTK)
    lose 25% in 2030 and earn 6% from 2031."""
    header = ("date,kind,scheduled_name,account_1,lot_1,account_2,lot_2,series,amount,param_date,"
              "tax_character,params_json,description,tags,name")
    strategy_csv(SCENARIO, "calendar.csv").write_text(
        f"{header}\n"
        "2030-01-31,set_rate,,,,,,stock_us,-25%,,,,a down year,,crash\n"
        "2031-01-31,set_rate,,,,,,stock_us,6%,,,,recovery,,recovery\n")


def _query(run: Path, sql: str) -> list[tuple]:
    con = sqlite3.connect(run)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _year_starts(run: Path) -> list[tuple[str, str, dict]]:
    return [(month, note, json.loads(detail)) for month, note, detail in _query(run, (
        "SELECT s.month, d.note, d.detail_json FROM strategy_decision d "
        "JOIN simulation_step s ON s.id = d.step_id JOIN strategy st ON st.id = d.strategy_id "
        "WHERE st.name = 'flex' AND d.hook = 'year_start' ORDER BY d.id"))]


def _baseline_by_year(run: Path) -> dict[str, tuple[int, int]]:
    return {year: (budgeted, delivered) for year, budgeted, delivered in _query(run, (
        "SELECT substr(s.month, 1, 4), SUM(b.budgeted_cents), SUM(b.delivered_cents) "
        "FROM spending_baseline b JOIN simulation_step s ON s.id = b.step_id GROUP BY 1"))}


def _load_errors(data_dir: Path, intent_dir: Path) -> str:
    with pytest.raises(LoadError) as info:
        load_scenario(data_dir, SCENARIO, intent_dir=intent_dir, allow_dirty_data=True,
                      compute_stamps=False)
    return str(info.value)


def _cli(data_dir: Path, *args: str, input: str | None = None):  # noqa: A002 -- CliRunner's name
    from click.testing import CliRunner

    from analysis.cli import main

    return CliRunner().invoke(main, ["sim", *args, "--data-dir", str(data_dir)], input=input,
                              terminal_width=200)


# ---------- the baseline and the built-ins ----------

def test_every_owned_posting_records_its_baseline(run_strategy_scenario: Callable[[str], Path]) -> None:
    """With nothing to correct, the guardrail answers "no opinion" to each
    posting: every one is recorded, budgeted and delivered alike."""
    run = run_strategy_scenario(SCENARIO)
    starts = _year_starts(run)
    assert starts[0][:2] == ("2026-05-31", "initial withdrawal rate 3.75%: budget $30,000 of $800,000")
    rows = _query(run, (
        "SELECT r.name, COUNT(*), SUM(b.budgeted_cents = b.delivered_cents), "
        "       COUNT(b.strategy_decision_id) FROM spending_baseline b "
        "JOIN scheduled_transaction r ON r.id = b.scheduled_transaction_id GROUP BY r.name"))
    # travel: May 2026 to Dec 2040; gifts: each December.
    assert sorted(rows) == [("gifts", 15, 15, 0), ("travel", 176, 176, 0)]


def test_threshold_skips_years_below_its_minimum(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path],
    run_strategy_scenario: Callable[[str], Path],
) -> None:
    _strategy(strategy_csv, "threshold", minimum="900000.00")
    run = run_strategy_scenario(SCENARIO)
    notes = [(month[:4], note) for month, note, _detail in _year_starts(run)]
    assert notes[0] == ("2026", "skip this year: portfolio $800,000 below $900,000")
    assert notes[1][0] == "2030" and notes[1][1].startswith("spend again: portfolio $1,0")
    baseline = _baseline_by_year(run)
    assert baseline["2027"][0] > 0 and baseline["2027"][1] == 0
    assert baseline["2030"][0] == baseline["2030"][1] > 0
    # The expense account got only what was delivered.
    paid = _query(run, (
        "SELECT COALESCE(SUM(amount), 0) FROM transfer_event e JOIN account a ON a.id = e.to_account "
        "WHERE a.name = 'Expense_Travel' AND substr(e.date, 1, 4) = '2027'"))[0][0]
    assert paid == 0
    log = _cli(strategy_scenarios, "strategy", "log", str(run), "--strategy", "flex", "--year", "2027")
    assert log.exit_code == 0, log.output
    assert "scale" in log.output and "travel: 0% of plan" in log.output


def test_threshold_can_skip_the_year_after_a_loss(
    strategy_csv: Callable[[str, str], Path], run_strategy_scenario: Callable[[str], Path],
) -> None:
    """The year after the portfolio's down year is skipped, and only that one."""
    _down_year(strategy_csv)
    _strategy(strategy_csv, "threshold", skip_after_loss=True)
    run = run_strategy_scenario(SCENARIO)
    starts = _year_starts(run)
    assert [m[:4] for m, n, _d in starts if n.startswith("skip")] == ["2031"]
    for month, note, detail in starts:
        if note.startswith("skip"):
            assert detail["last_return"] < 0, (month, note)
            assert "last year's return -" in note
        else:
            assert detail["last_return"] is None or detail["last_return"] >= 0


# The cuts slow the drain but don't stop it: checking runs short in the 2030s.
@pytest.mark.allow_negative_balance
def test_guardrail_cuts_when_the_rate_climbs(
    strategy_csv: Callable[[str, str], Path], run_strategy_scenario: Callable[[str], Path],
) -> None:
    """Travel at $6,000 a month drains the portfolio: the rate passes the
    initial one plus 20%, and spending is cut 10% at a time."""
    _rewrite(strategy_csv(SCENARIO, "scheduled.csv"), "travel", amount="6000.00")
    # The scenario is 15 years: with the default final_years, no year may cut.
    _strategy(strategy_csv, "guardrail", final_years=0)
    run = run_strategy_scenario(SCENARIO)
    starts = _year_starts(run)
    cuts = [(m, n, d) for m, n, d in starts if n.startswith("cut 10%")]
    assert cuts, starts
    month, _note, detail = cuts[0]
    assert detail["rate"] > detail["initial_rate"] * 1.2
    assert detail["factor"] == pytest.approx(0.9)
    year = month[:4]
    budgeted, delivered = _baseline_by_year(run)[year]
    assert delivered == pytest.approx(budgeted * 0.9, abs=20)  # rounded per posting
    # No cuts in the last 15 years: here, from 2026 on.
    run.unlink()
    _strategy(strategy_csv, "guardrail")
    run = run_strategy_scenario(SCENARIO)
    assert not [n for _m, n, _d in _year_starts(run) if n.startswith("cut")]


def test_ceiling_floor_holds_real_change_within_its_limits(
    strategy_csv: Callable[[str, str], Path], run_strategy_scenario: Callable[[str], Path],
) -> None:
    _strategy(strategy_csv, "ceiling_floor", rate="5%")
    run = run_strategy_scenario(SCENARIO)
    starts = _year_starts(run)
    first = starts[0][2]
    assert first["spend"] == 4_000_000 and first["factor"] == pytest.approx(4_000_000 / 3_000_000)
    held = [note for _m, note, _d in starts if "held to the" in note]
    assert held, starts
    cpi = dict(_query(run, (
        "SELECT s.month, i.cumulative_index_micros FROM inflation_index_state i "
        "JOIN simulation_step s ON s.id = i.step_id WHERE i.series = 'cpi'")))
    for (m0, _n0, d0), (m1, _n1, d1) in zip(starts, starts[1:], strict=False):
        real_change = d1["spend"] / (d0["spend"] * cpi[m1] / cpi[m0])
        assert 0.975 - 1e-6 <= real_change <= 1.05 + 1e-6, (m1, real_change)


# ---------- the context ----------

_PROBE = '''
from analysis.sim.strategy_api import Decision, Spend, Strategy, register

@register
class Probe(Strategy):
    """For tests: notes what the context shows each January."""
    kind = "probe"
    def year_start(self, ctx):
        lots = ctx.tagged("portfolio:invest")
        ctx.memory[str(ctx.year)] = {{
            "budget": ctx.budget(), "end": ctx.end.isoformat(),
            "cpi": ctx.cpi(ctx.month), "ages": {{p: v.age for p, v in ctx.people.items()}},
            "return": ctx.annual_return(lots, ctx.year - 1),
        }}
        return Decision([], note="probe")
    def scale(self, ctx, rule, amount):
{scale}
'''


def _probe(strategy_module: Callable[[str, str], Path], strategy_csv: Callable[[str, str], Path],
           scale: str = "return None") -> None:
    strategy_module("probe", _PROBE.format(scale="\n".join("        " + b for b in scale.splitlines())))
    _rewrite_flex(strategy_csv, kind="probe", params_json="")


def test_the_context_shows_budget_people_cpi_and_returns(
    strategy_module: Callable[[str, str], Path], strategy_csv: Callable[[str, str], Path],
    run_strategy_scenario: Callable[[str], Path],
) -> None:
    _probe(strategy_module, strategy_csv)
    run = run_strategy_scenario(SCENARIO)
    memory = json.loads(_query(run, (
        "SELECT memory_json FROM strategy_decision WHERE memory_json IS NOT NULL ORDER BY id DESC LIMIT 1"
    ))[0][0])
    first, second = memory["2026"], memory["2027"]
    # Twelve months from May: travel 12 x $1,500 and December's gifts.
    assert first["budget"] == 3_000_000
    assert second["budget"] > first["budget"]  # indexed to January's prices
    assert first["end"] == "2040-12-31" and first["cpi"] == 1_000_000
    assert first["return"] is None  # 2025 is before the run
    assert isinstance(second["return"], float)
    assert second["ages"]["alice"] == first["ages"]["alice"] + 1 or second["ages"]["alice"] == first["ages"]["alice"]


def test_the_budget_stops_at_the_end_of_the_run(
    strategy_module: Callable[[str, str], Path], strategy_csv: Callable[[str, str], Path],
    run_strategy_scenario: Callable[[str], Path],
) -> None:
    _probe(strategy_module, strategy_csv)
    _rewrite(strategy_csv(SCENARIO, "scenario.csv"), SCENARIO, end="2040-06-30")
    run = run_strategy_scenario(SCENARIO)
    memory = json.loads(_query(run, (
        "SELECT memory_json FROM strategy_decision WHERE memory_json IS NOT NULL ORDER BY id DESC LIMIT 1"
    ))[0][0])
    # 2039: twelve months of travel ($18,000) and December's gifts ($12,000).
    # 2040 ends in June: six months of travel, no gifts, a year's CPI later.
    ratio = memory["2040"]["budget"] / memory["2039"]["budget"]
    assert 9_000 / 30_000 < ratio < 9_000 / 30_000 * 1.1, ratio


@pytest.mark.parametrize(("scale", "message"), [
    ("return Decision([])", "a scale answers with one Spend"),
    ("return Decision([Spend(-1)])", "must be whole cents >= 0"),
    ("return Decision([Spend(1.5)])", "must be whole cents >= 0"),
    ("ctx.annual_return([], ctx.year)\nreturn None", r"annual_return\(\d+\) needs a year before"),
])
def test_a_misbehaving_spending_strategy_fails_the_run(
    strategy_module: Callable[[str, str], Path], strategy_csv: Callable[[str, str], Path],
    run_strategy_scenario: Callable[[str], Path], scale: str, message: str,
) -> None:
    _probe(strategy_module, strategy_csv, scale)
    with pytest.raises(StrategyError, match=message) as info:
        run_strategy_scenario(SCENARIO)
    assert "strategy 'flex' (probe) scale in " in str(info.value)


def test_year_start_moves_nothing(
    strategy_module: Callable[[str, str], Path], strategy_csv: Callable[[str, str], Path],
    run_strategy_scenario: Callable[[str], Path],
) -> None:
    strategy_module("probe", _PROBE.format(scale="        return None").replace(
        'return Decision([], note="probe")', 'return Decision([Spend(1)])'))
    _rewrite_flex(strategy_csv, kind="probe", params_json="")
    with pytest.raises(StrategyError, match="year_start moves nothing"):
        run_strategy_scenario(SCENARIO)


# ---------- loading ----------

@pytest.mark.parametrize(("changes", "message"), [
    ({"rules": "flex:nothing"}, "no scheduled rule carries tag flex:nothing"),
    ({"rules": "flex"}, "must be a rule tag like"),
    ({"rules": ""}, "give a chain, a rules tag, or both"),
    ({"kind": "rebalance", "params_json": '{"portfolio":"portfolio:invest","targets":"main"}'},
     "kind 'rebalance' has no scale hook"),
])
def test_bad_rule_ownership_fails_the_load(
    strategy_scenarios: Path, intent_dir: Path, strategy_csv: Callable[[str, str], Path],
    changes: dict[str, str], message: str,
) -> None:
    _rewrite_flex(strategy_csv, **changes)
    errors = _load_errors(strategy_scenarios, intent_dir)
    assert message in errors and "(flex)" in errors


def test_only_fixed_amount_rules_can_be_owned(
    strategy_scenarios: Path, intent_dir: Path, strategy_csv: Callable[[str, str], Path],
) -> None:
    path = strategy_csv(SCENARIO, "scheduled.csv")
    with path.open() as f:
        header = f.readline()
    path.write_text(path.read_text() + (
        "mortgage_pmt,Asset_Checking,,Liability_Mortgage,,monthly,,mortgage_payment,"
        "Expense_Interest_Mortgage,,,6%,,1850.00,2026-05-31,,none,,2026-05-31,,300,,,30yr fixed 6%,"
        "{},,flex:optional\n"))
    assert header.rstrip().endswith(",tags")
    errors = _load_errors(strategy_scenarios, intent_dir)
    assert "rule 'mortgage_pmt' carries tag flex:optional but is mortgage_payment" in errors


def test_two_strategies_cannot_own_one_rule(
    strategy_scenarios: Path, intent_dir: Path, strategy_csv: Callable[[str, str], Path],
) -> None:
    path = strategy_csv(SCENARIO, FLEX)
    path.with_name("second.csv").write_text(path.read_text())
    errors = _load_errors(strategy_scenarios, intent_dir)
    assert "rule 'gifts' (tag flex:optional) is already owned by strategy 'flex'" in errors


# ---------- the CLI ----------

def test_strategy_add_asks_for_the_rule_tag(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path],
) -> None:
    strategy_csv(SCENARIO, FLEX).unlink()
    # alice_triggers, built on alice_spending, owns the same rules (#441).
    strategy_csv("alice_triggers", "strategies/respond.csv").unlink()
    # kind, rule tag, then portfolio, minimum and skip_after_loss (defaults)
    result = _cli(strategy_scenarios, "strategy", "add", "trim", "--scenario", SCENARIO,
                  input="threshold\nflex:optional\nportfolio:invest\n\n\n")
    assert result.exit_code == 0, result.output
    assert "rule tags: category:gifts, category:travel, flex:optional" in result.output
    text = strategy_csv(SCENARIO, "strategies/trim.csv").read_text()
    assert 'threshold,,flex:optional,,,,,"{""portfolio"":""portfolio:invest""}",' in text
    result = _cli(strategy_scenarios, "strategy", "list", SCENARIO)
    assert "trim                threshold on rules flex:optional" in result.output


def test_strategy_kinds_shows_the_spending_hooks(strategy_scenarios: Path) -> None:
    result = _cli(strategy_scenarios, "strategy", "kinds")
    assert result.exit_code == 0, result.output
    assert "guardrail  (built-in; hooks: scale, year_start)" in result.output
    assert "ceiling_floor  (built-in; hooks: scale, year_start)" in result.output


def test_spending_report_sets_delivered_against_the_plan(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path],
    run_strategy_scenario: Callable[[str], Path],
) -> None:
    _strategy(strategy_csv, "threshold", minimum="900000.00")
    run = str(run_strategy_scenario(SCENARIO))
    result = _cli(strategy_scenarios, "report", "spending", "--runfile", run, "--by", "tag:category")
    assert result.exit_code == 0, result.output
    assert "spending against plan by category" in result.output
    for column in ("budgeted", "delivered", "of_plan", "redirected", "filled:travel", "filled:gifts"):
        assert column in result.output
    assert "filled: travel 27%, gifts 27%" in result.output or "filled: gifts 27%, travel 27%" in result.output
    result = _cli(strategy_scenarios, "explain", run)
    assert "flex (threshold on rules flex:optional):" in result.output


def test_spending_report_on_a_run_without_strategies(
    strategy_scenarios: Path, run_strategy_scenario: Callable[[str], Path],
) -> None:
    run = str(run_strategy_scenario("alice_rebalance"))
    result = _cli(strategy_scenarios, "report", "spending", "--runfile", run)
    assert result.exit_code == 0, result.output
    assert "no spending strategy changed this run's spending" in result.output


def test_spending_report_chart_file(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path],
    run_strategy_scenario: Callable[[str], Path], tmp_path: Path,
) -> None:
    """#430: --chart-file writes the notebook's chart; the table only with --table."""
    _strategy(strategy_csv, "threshold", minimum="900000.00")
    run = str(run_strategy_scenario(SCENARIO))
    chart = tmp_path / "spending.svg"
    result = _cli(strategy_scenarios, "report", "spending", "--runfile", run,
                  "--by", "tag:category", "--chart-file", str(chart))
    assert result.exit_code == 0, result.output
    assert "Spending against plan by category" in chart.read_text()
    assert "redirected" not in result.output
    chart.unlink()
    result = _cli(strategy_scenarios, "report", "spending", "--runfile", run,
                  "--chart-file", str(chart), "--table")
    assert result.exit_code == 0, result.output
    assert chart.stat().st_size > 0
    assert "spending against plan by category" in result.output
    assert "redirected:" in result.output


def test_spending_report_chart_without_strategies(
    strategy_scenarios: Path, run_strategy_scenario: Callable[[str], Path], tmp_path: Path,
) -> None:
    """The plan alone is still drawn when no strategy changed it."""
    run = str(run_strategy_scenario("alice_rebalance"))
    chart = tmp_path / "spending.png"
    result = _cli(strategy_scenarios, "report", "spending", "--runfile", run, "--chart-file", str(chart))
    assert result.exit_code == 0, result.output
    assert chart.stat().st_size > 0


def test_shape_measures() -> None:
    """The proposal's example: plan 60/30/10, delivered 70/20/10."""
    plan = pd.Series({"a": 60.0, "b": 30.0, "c": 10.0})
    spent = pd.Series({"a": 70.0, "b": 20.0, "c": 10.0})
    assert reports.redirected_share(spent, plan) == pytest.approx(0.10)
    assert reports.cosine_similarity(spent, plan) == pytest.approx(0.983, abs=5e-4)
    # A proportional cut keeps the shape: level only.
    assert reports.redirected_share(plan / 2, plan) == 0
    frame, summary = reports.spending_report(
        pd.DataFrame([[50.0, 30.0]], index=[2030], columns=["living", "travel"]),
        pd.DataFrame([[50.0, 50.0]], index=[2030], columns=["living", "travel"]),
    )
    assert frame.loc[2030, "of_plan"] == pytest.approx(0.8)
    assert frame.loc[2030, "filled:travel"] == pytest.approx(0.6)
    assert "filled:living" not in frame.columns
    assert summary["rms"] == pytest.approx(20.0)


# ---------- review fixes ----------

def test_compare_lists_a_rules_only_strategy_that_differs(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path],
) -> None:
    """A spending strategy owns no chain; it still shows in the differences."""
    child = strategy_scenarios / "scenarios" / "alice_spending_trim"
    child.mkdir()
    (child / "scenario.csv").write_text(
        "name,description,start,end,seed,parent_scenario,created_by,meta_json\n"
        "alice_spending_trim,,2026-05-31,2040-12-31,42,alice_spending,alice,{}\n")
    (child / "README.md").write_text("# alice_spending_trim\n\nBuilds on: alice_spending\n")
    (child / "strategies").mkdir()
    (child / FLEX).write_text(strategy_csv(SCENARIO, FLEX).read_text().replace("guardrail,", "threshold,"))
    for name in (SCENARIO, "alice_spending_trim"):
        result = _cli(strategy_scenarios, "run", name, "--allow-dirty-data", "--allow-dirty-code")
        assert result.exit_code == 0, result.output
    result = _cli(strategy_scenarios, "compare", SCENARIO, "alice_spending_trim",
                  "--section", "differences", "--section", "strategies")
    assert result.exit_code == 0, result.output
    differs = result.output.split("Strategies that differ", 1)[1].split("Target weights", 1)[0]
    assert re.search(r"flex +kind +guardrail +threshold", differs), differs
    assert "rules flex:optional" in result.output  # what it owns, in the activity table


def test_unspent_budget_counts_only_for_rules_into_expenses(
    strategy_scenarios: Path, strategy_csv: Callable[[str, str], Path],
    run_strategy_scenario: Callable[[str], Path],
) -> None:
    """Gifts saved into savings instead of spent: skipping them is not a
    spending cut, so the report's budget leaves them out."""
    _rewrite(strategy_csv(SCENARIO, "scheduled.csv"), "gifts", to_account="Asset_Savings")
    _strategy(strategy_csv, "threshold", minimum="900000.00")
    run = run_strategy_scenario(SCENARIO)
    from analysis.sim.connection import RunConnection

    rc = RunConnection(run)
    try:
        delivered, budgeted = reports.spending_by_year(rc, by="tag:category")
    finally:
        rc.close()
    assert "gifts" not in budgeted.columns
    assert budgeted.loc[2027, "travel"] > 0 and delivered.loc[2027, "travel"] == 0


@pytest.mark.allow_negative_balance  # the drain that sets off the rule runs checking short
def test_guardrail_never_cuts_a_fixed_budget_for_inflation(
    strategy_csv: Callable[[str, str], Path], run_strategy_scenario: Callable[[str], Path],
) -> None:
    """A skipped raise is the raise the budget got: indexed rules lose one
    after a down year, fixed-amount rules (which got none) lose nothing."""
    _rewrite(strategy_csv(SCENARIO, "scheduled.csv"), "travel", amount="6000.00")
    _down_year(strategy_csv)
    _strategy(strategy_csv, "guardrail", final_years=100, lower="99%")
    run = run_strategy_scenario(SCENARIO)
    assert [n for _m, n, _d in _year_starts(run) if "inflation raise after a -" in n]
    run.unlink()
    for rule in ("travel", "gifts"):
        _rewrite(strategy_csv(SCENARIO, "scheduled.csv"), rule, series="", base_date="")
    run = run_strategy_scenario(SCENARIO)
    assert not [n for _m, n, _d in _year_starts(run) if "inflation raise" in n]
