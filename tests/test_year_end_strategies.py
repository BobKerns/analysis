"""Year-end actions (#421, #326 stage 3a, ADR-010): the ``year_end`` hook,
the :class:`Transfer` and :class:`Realize` moves, ``ctx.tax``, and the
built-in ``harvest`` and ``gift``.

``alice_year_end`` (from the ``strategy_scenarios`` fixture) builds on
``alice_rebalance`` (a joint brokerage of $300,000 with $200,000 basis) with
a federal ``tax_accumulator`` settled each April, an ``Expense_Family`` gift
account, and a ``harvest`` strategy filling the 0% long-term gains band."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

from analysis.sim import reports
from analysis.cli import main
from analysis.sim.connection import RunConnection
from analysis.sim.loader import LoadError
from analysis.sim.strategy_api import StrategyError

SCENARIO = "alice_year_end"
BROKERAGE = "Asset_Brokerage_Joint"


def _strategy(strategy_csv: Callable[[str, str], Path], name: str, kind: str, **params: object) -> None:
    """Write ``strategies/<name>.csv`` with one strategy of ``kind``."""
    cells = json.dumps(params).replace('"', '""')
    strategy_csv(SCENARIO, f"strategies/{name}.csv").write_text(
        f'kind,chain,rules,start,end,enabled,params_json,description\n{kind},,,,,,"{cells}",\n')


def _kept(run: Path, as_name: str) -> Path:
    """Move a run file aside, so the scenario can run again."""
    return run.rename(run.with_name(f"{as_name}.sqlite3"))


def _no_harvest(strategy_csv: Callable[[str, str], Path]) -> None:
    strategy_csv(SCENARIO, "strategies/harvest.csv").unlink()


def _query(run: Path, sql: str, *args: object) -> list[tuple]:
    con = sqlite3.connect(run)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def _year_ends(run: Path) -> list[tuple[str, int, str, dict]]:
    """Each year_end decision: month, cents moved, note, detail."""
    return [(month, chosen, note, json.loads(detail)) for month, chosen, note, detail in _query(run, (
        "SELECT s.month, d.chosen_cents, d.note, d.detail_json FROM strategy_decision d "
        "JOIN simulation_step s ON s.id = d.step_id WHERE d.hook = 'year_end' ORDER BY d.id"))]


def _summary(run: Path, year: int, bucket: str) -> int:
    rows = _query(run, "SELECT amount FROM tax_year_summary WHERE year = ? AND bucket = ?", year, bucket)
    return rows[0][0] if rows else 0


def _lot(run: Path, account: str, month: str, lot: str = "default") -> tuple[int, int | None]:
    """A lot's balance and cost basis at a month end."""
    return _query(run, (
        "SELECT ls.balance, ls.cost_basis FROM lot_state ls JOIN simulation_step s ON s.id = ls.step_id "
        "JOIN account_lot m ON m.id = ls.lot_id JOIN account a ON a.id = m.account_id "
        "WHERE a.name = ? AND m.lot = ? AND s.month = ?"), account, lot, month)[0]


# ---------- harvest ----------

def test_harvest_fills_the_zero_percent_band_to_the_cent(run_strategy_scenario) -> None:
    run = run_strategy_scenario(SCENARIO)
    harvests = [d for d in _year_ends(run) if d[1] > 0]
    assert harvests, "some year should have room in the 0% band"
    for month, _moved, note, detail in harvests:
        year = int(month[:4])
        assert note.startswith("realized $") and "0% band" in note
        taxable = _summary(run, year, "federal_taxable_income")
        # The April settlement sees taxable income at the band's top, within a cent.
        assert detail["band_top"] - 1 <= taxable <= detail["band_top"]
        assert _summary(run, year, "federal_ltcg_income") >= sum(detail["realized"].values())


def test_harvest_raises_basis_and_keeps_the_balance(run_strategy_scenario, strategy_csv) -> None:
    on = _kept(run_strategy_scenario(SCENARIO), "on")
    first, _moved, _note, detail = next(d for d in _year_ends(on) if d[1] > 0)
    gain = detail["realized"][f"{BROKERAGE}:default"]
    _no_harvest(strategy_csv)
    off = run_strategy_scenario(SCENARIO)
    balance_on, basis_on = _lot(on, BROKERAGE, first)
    balance_off, basis_off = _lot(off, BROKERAGE, first)
    assert balance_on == balance_off
    assert basis_on == basis_off + gain
    # Sold and rebought within the lot: every leg runs from the lot to itself.
    legs = _query(on, (
        "SELECT t.from_lot_id = t.to_lot_id, t.tax_character, t.reason_kind FROM transfer_event t "
        "JOIN strategy_decision d ON d.id = t.strategy_decision_id WHERE d.hook = 'year_end'"))
    assert legs and all(same for same, _c, _k in legs)
    assert {(c, k) for _s, c, k in legs} == {("return_of_capital", "strategy"), ("ltcg", "strategy")}


def test_harvest_stops_where_social_security_would_become_taxable(
        run_strategy_scenario, strategy_csv) -> None:
    run = _kept(run_strategy_scenario(SCENARIO), "default")
    held = [d for d in _year_ends(run) if d[1] == 0]
    assert held, "a low-income year should hold the harvest back"
    month, _moved, note, detail = held[0]
    assert note.startswith("no harvest: the first $100 of gain would cost")
    assert detail["marginal"] > 0 and detail["limit"] == 0
    # Allowing a marginal rate lets that year harvest up to the band.
    _strategy(strategy_csv, "harvest", "harvest", portfolio="portfolio:invest", max_marginal_rate="30%")
    allowed = run_strategy_scenario(SCENARIO)
    assert any(m == month and moved > 0 for m, moved, _n, _d in _year_ends(allowed))


def test_harvest_losses_carry_into_the_next_year(run_strategy_scenario, strategy_csv) -> None:
    header = ("date,kind,scheduled_name,account_1,lot_1,account_2,lot_2,series,amount,param_date,"
              "tax_character,params_json,description,tags,name")
    strategy_csv(SCENARIO, "calendar.csv").write_text(
        f"{header}\n2027-01-31,set_rate,,,,,,stock_us,-60%,,,,a crash,,crash\n"
        "2028-01-31,set_rate,,,,,,stock_us,6%,,,,recovery,,recovery\n")
    _strategy(strategy_csv, "harvest", "harvest", portfolio="portfolio:invest", losses=True)
    run = run_strategy_scenario(SCENARIO)
    month, _moved, note, detail = next(d for d in _year_ends(run) if "losses" in d[2])
    assert month == "2027-12-31"
    loss = -detail["losses"][f"{BROKERAGE}:default"]
    assert loss > 0 and note == f"harvested ${loss / 100:,.0f} of losses in 1 lot(s)"
    # $3,000 offsets ordinary income; the rest carries into 2028.
    assert _summary(run, 2027, "federal_capital_loss_carryover") >= loss - 300_000 - 1


# ---------- gift ----------

def test_gift_in_kind_gives_basis_and_realizes_no_gain(run_strategy_scenario, strategy_csv) -> None:
    _no_harvest(strategy_csv)
    _strategy(strategy_csv, "family", "gift", to="Expense_Family", amount="19000.00",
              portfolio="portfolio:invest")
    run = run_strategy_scenario(SCENARIO)
    month, moved, note, _detail = _year_ends(run)[0]
    assert (month, moved, note) == ("2026-12-31", 1_900_000, "gave $19,000 in kind to Expense_Family")
    legs = _query(run, (
        "SELECT a.name, t.amount, t.tax_character FROM transfer_event t "
        "JOIN account a ON a.id = t.to_account JOIN strategy_decision d ON d.id = t.strategy_decision_id "
        "WHERE d.hook = 'year_end' AND t.date = '2026-12-31'"))
    assert legs == [("Expense_Family", 1_900_000, "none")]
    before = _query(run, (
        "SELECT ls.balance, ls.cost_basis FROM lot_state ls JOIN simulation_step s ON s.id = ls.step_id "
        "JOIN account_lot m ON m.id = ls.lot_id JOIN account a ON a.id = m.account_id "
        "WHERE a.name = ? AND s.month = '2026-11-30'"), BROKERAGE)[0]
    after = _lot(run, BROKERAGE, "2026-12-31")
    # December's growth posts before the gift; the basis leaves pro rata.
    december_balance = after[0] + 1_900_000
    assert before[1] is not None and after[1] == before[1] - before[1] * 1_900_000 // december_balance
    rc = RunConnection(run)
    try:
        expenses = rc.expenses_by_category()
    finally:
        rc.close()
    assert expenses.loc["2026-12-31", "gifts"] == pytest.approx(19_000)


def test_cash_gift_is_held_to_its_tax_limit(run_strategy_scenario, strategy_csv) -> None:
    _no_harvest(strategy_csv)
    base = _kept(run_strategy_scenario(SCENARIO), "base")
    _strategy(strategy_csv, "family", "gift", to="Expense_Family", amount="19000.00",
              portfolio="portfolio:invest", in_kind=False, max_tax="500.00")
    run = run_strategy_scenario(SCENARIO)
    month, moved, note, _detail = _year_ends(run)[0]
    assert month == "2026-12-31" and note.endswith(", held to $500 of tax") and 0 < moved < 1_900_000
    extra = (_summary(run, 2026, "federal_tax_liability")
             - _summary(base, 2026, "federal_tax_liability"))
    assert 49_000 < extra <= 50_000
    # Sold, not given in kind: the gain legs land in the gift account.
    characters = {c for (c,) in _query(run, (
        "SELECT DISTINCT t.tax_character FROM transfer_event t "
        "JOIN strategy_decision d ON d.id = t.strategy_decision_id WHERE t.date = '2026-12-31'"))}
    assert characters == {"return_of_capital", "ltcg"}


def test_gift_waits_for_a_surplus(run_strategy_scenario, strategy_csv) -> None:
    _no_harvest(strategy_csv)
    _strategy(strategy_csv, "family", "gift", to="Expense_Family", amount="19000.00",
              portfolio="portfolio:invest", minimum="5000000.00")
    run = run_strategy_scenario(SCENARIO)
    month, moved, note, _detail = _year_ends(run)[0]
    assert month == "2026-12-31" and moved == 0
    assert note.startswith("no gift: portfolio $") and note.endswith(" not above $5,000,000")


# ---------- the hook, the moves and ctx.tax ----------

ESTIMATE = '''
from dataclasses import dataclass
from analysis.sim.strategy_api import Decision, Strategy, register


@register
class Peek(Strategy):
    """Record December's tax estimate."""
    kind = "peek"

    @dataclass(frozen=True)
    class Params:
        pass

    def year_end(self, ctx):
        est = ctx.tax.estimate()
        return Decision([], note="peek", detail={
            "tax": est.tax, "taxable": est.taxable_income, "agi": est.agi,
            "status": ctx.tax.filing_status, "ytd": dict(ctx.tax.ytd),
            "last_year": ctx.tax.last_year is not None, "floors": list(ctx.tax.irmaa_floors()),
        })
'''


def test_december_estimate_matches_the_april_settlement(
        run_strategy_scenario, strategy_csv, strategy_module) -> None:
    _no_harvest(strategy_csv)
    strategy_module("peek", ESTIMATE)
    _strategy(strategy_csv, "peek", "peek")
    run = run_strategy_scenario(SCENARIO)
    decisions = _year_ends(run)
    assert [d[0][:4] for d in decisions] == [str(y) for y in range(2026, 2041)]
    for month, _moved, _note, detail in decisions[:-1]:
        year = int(month[:4])
        assert detail["tax"] == _summary(run, year, "federal_tax_liability"), year
        assert detail["taxable"] == _summary(run, year, "federal_taxable_income"), year
        assert detail["status"] == "MFJ"
        assert detail["ytd"].get("ordinary", 0) > 0 or year > 2030
    assert decisions[0][3]["last_year"] is False and decisions[1][3]["last_year"] is True


CONVERT = '''
from dataclasses import dataclass
from analysis.sim.strategy_api import Decision, LotRef, Strategy, Transfer, register


@register
class Convert(Strategy):
    """Convert $10,000 a year to Roth."""
    kind = "convert"

    @dataclass(frozen=True)
    class Params:
        pass

    def year_end(self, ctx):
        return Decision([Transfer(LotRef("Asset_TraditionalIRA_Alice", "default"),
                                  LotRef("Asset_Roth_Alice", "default"), 1_000_000)], note="convert")
'''


def test_transfer_to_roth_is_a_taxed_conversion(run_strategy_scenario, strategy_csv, strategy_module) -> None:
    _no_harvest(strategy_csv)
    strategy_module("convert", CONVERT)
    _strategy(strategy_csv, "convert", "convert")
    run = run_strategy_scenario(SCENARIO)
    legs = _query(run, (
        "SELECT t.amount, t.tax_character, t.reason_kind FROM transfer_event t "
        "JOIN strategy_decision d ON d.id = t.strategy_decision_id WHERE t.date = '2026-12-31'"))
    assert legs == [(1_000_000, "tax_deferred_out", "strategy")]


BAD = '''
from dataclasses import dataclass
from analysis.sim.strategy_api import Decision, LotRef, Realize, Strategy, Take, Transfer, register


@register
class Bad(Strategy):
    """Make the move named by its parameter."""
    kind = "bad"

    @dataclass(frozen=True)
    class Params:
        which: str = "ira"

    def year_end(self, ctx):
        ira = LotRef("Asset_TraditionalIRA_Alice", "default")
        moves = {
            "ira": [Realize(ira, 100)],
            "in_kind_to_asset": [Transfer(LotRef("Asset_Brokerage_Joint", "default"),
                                          LotRef("Asset_Savings", "default"), 100, in_kind=True)],
            "take": [Take(ira, 100)],
        }[self.params.which]
        return Decision(moves)
'''


@pytest.mark.parametrize(("which", "message"), [
    ("ira", "doesn't track a taxable gain"),
    ("in_kind_to_asset", "in_kind gives into an expense account"),
    ("take", "a year_end answers with Transfer and Realize"),
])
def test_a_move_year_end_may_not_make_fails_the_run(
        run_strategy_scenario, strategy_csv, strategy_module, which: str, message: str) -> None:
    _no_harvest(strategy_csv)
    strategy_module("bad", BAD)
    _strategy(strategy_csv, "bad", "bad", which=which)
    with pytest.raises(StrategyError, match=message):
        run_strategy_scenario(SCENARIO)


def test_a_year_end_kind_owns_nothing_but_a_draw_kind_must(run_strategy_scenario, strategy_csv) -> None:
    run_strategy_scenario(SCENARIO)  # harvest has no chain or rules
    _strategy(strategy_csv, "harvest", "rebalance", portfolio="portfolio:invest", targets="main")
    with pytest.raises(LoadError, match="give a chain, a rules tag, or both"):
        run_strategy_scenario(SCENARIO)


def test_a_gift_needs_an_account_that_exists(run_strategy_scenario, strategy_csv) -> None:
    _strategy(strategy_csv, "family", "gift", to="Expense_Nobody", amount="100.00",
              portfolio="portfolio:invest")
    with pytest.raises(LoadError, match="no account named 'Expense_Nobody'"):
        run_strategy_scenario(SCENARIO)


# ---------- reports ----------

def test_explain_and_gains_report_show_the_harvest(run_strategy_scenario) -> None:
    run = run_strategy_scenario(SCENARIO)
    harvested = sum(sum(d[3]["realized"].values()) for d in _year_ends(run) if d[1] > 0)
    out = CliRunner().invoke(main, ["sim", "explain", "--runfile", str(run)], terminal_width=200)
    assert out.exit_code == 0, out.output
    line = next(ln for ln in out.output.splitlines() if ln.startswith("  harvest (harvest)"))
    assert line.endswith(f"realizing ${harvested / 100:,.2f} of gain")
    rc = RunConnection(run)
    try:
        gains = reports.gains_by_year(rc)
    finally:
        rc.close()
    assert gains["harvested"].sum() == pytest.approx(harvested / 100)
    log = CliRunner().invoke(
        main, ["sim", "strategy", "log", "--runfile", str(run), "--strategy", "harvest"], terminal_width=200)
    assert log.exit_code == 0, log.output
    assert "year_end" in log.output and "0% band" in log.output
