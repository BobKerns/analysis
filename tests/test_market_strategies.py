"""Down-market and bucket rules (#463, #326 stage 5, ADR-010 §6.5): the
built-in ``down_market`` and ``buckets`` kinds, and ``ctx.trailing_return``.

``alice_buckets`` (from the ``strategy_scenarios`` fixture) holds cash
(money-market lots in the brokerage and Alice's Roth), bonds (her
traditional IRA and a Roth bond lot) and stocks (the brokerage and the
Roth), with stocks at 7%, bonds 4% and cash 2% a year. A ``buckets``
strategy owns checking's refill chain with a need of $40,000 a year. The
household draws on the portfolio from 2033, after both salaries end.
``_crash`` sets stocks to -30% for 2034 and back to 7% from 2035."""

from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from analysis.sim.loader import LoadError, load_scenario
from analysis.sim.strategy_api import StrategyError

SCENARIO = "alice_buckets"
BUCKETS = "strategies/buckets.csv"


def _strategy(strategy_csv: Callable[[str, str], Path], kind: str = "buckets", **params: object) -> None:
    """Make the scenario's one strategy ``kind`` with ``params``."""
    path = strategy_csv(SCENARIO, BUCKETS)
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    rows[0].update(kind=kind, params_json=json.dumps(params))
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _buckets(strategy_csv: Callable[[str, str], Path], **params: object) -> None:
    _strategy(strategy_csv, "buckets", **{"portfolio": "portfolio:invest", "need": "40000.00", **params})


def _crash(strategy_csv: Callable[[str, str], Path]) -> None:
    header = ("date,kind,scheduled_name,account_1,lot_1,account_2,lot_2,series,amount,param_date,"
              "tax_character,params_json,description,tags,name")
    strategy_csv(SCENARIO, "calendar.csv").write_text(
        f"{header}\n"
        "2034-01-31,set_rate,,,,,,stock_us,-30%,,,,a crash,,crash\n"
        "2035-01-31,set_rate,,,,,,stock_us,7%,,,,recovery,,recovery\n")


def _query(path: Path, sql: str, *args: object) -> list[tuple]:
    con = sqlite3.connect(path)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def _decisions(run: Path, hook: str) -> list[tuple[str, str, dict, int]]:
    """``(month, note, detail, fallback cents)`` of each ``hook`` decision."""
    return [(month, note, json.loads(detail), fallback) for month, note, detail, fallback in _query(run, (
        "SELECT s.month, d.note, d.detail_json, d.fallback_cents FROM strategy_decision d "
        "JOIN simulation_step s ON s.id = d.step_id WHERE d.hook = ? ORDER BY d.id"), hook)]


def _legs(run: Path, hook: str) -> list[tuple[str, str, str, str, int, str]]:
    """``(month, from account, from lot, to account, amount, character)`` of
    each leg a ``hook`` decision chose."""
    return _query(run, (
        "SELECT s.month, fa.name, fl.lot, ta.name, e.amount, e.tax_character FROM transfer_event e "
        "JOIN strategy_decision d ON d.id = e.strategy_decision_id "
        "JOIN simulation_step s ON s.id = e.step_id "
        "JOIN account_lot fl ON fl.id = e.from_lot_id JOIN account fa ON fa.id = fl.account_id "
        "JOIN account_lot tl ON tl.id = e.to_lot_id JOIN account ta ON ta.id = tl.account_id "
        "WHERE d.hook = ? ORDER BY e.id"), hook)


# ---------- ctx.trailing_return ----------

_PROBE = '''
from analysis.sim.strategy_api import Decision, Strategy, register

@register
class Probe(Strategy):
    """For tests: notes each month's trailing returns."""
    kind = "probe"
    def month_start(self, ctx):
        stocks = [ref for ref in ctx.tagged("portfolio:invest") if ref.lot == "default"
                  and ref.account == "Asset_Roth_Alice"]
        ctx.memory[ctx.month.isoformat()] = {{
            "trailing": ctx.trailing_return(stocks, 12),
            "two_years": ctx.trailing_return(stocks, 24),
            "annual": ctx.annual_return(stocks, ctx.year - 1) if ctx.month.month == 1 else None,
        }}
        {extra}
        return None
'''


def _probe(strategy_module: Callable[[str, str], Path], strategy_csv: Callable[[str, str], Path],
           extra: str = "") -> None:
    strategy_module("probe", _PROBE.format(extra=extra))
    path = strategy_csv(SCENARIO, BUCKETS)
    path.write_text("kind,chain,start,end,enabled,params_json,description\nprobe,,,,,{},probe\n")


def _probe_memory(run: Path) -> dict[str, dict]:
    rows = _query(run, "SELECT memory_json FROM strategy_decision WHERE memory_json IS NOT NULL "
                       "ORDER BY id DESC LIMIT 1")
    assert rows, "the probe recorded nothing"
    return json.loads(rows[0][0])


def test_trailing_return_over_twelve_months_in_january_is_last_years_return(
    strategy_module: Callable[[str, str], Path], strategy_csv: Callable[[str, str], Path],
    run_strategy_scenario: Callable[[str], Path],
) -> None:
    _probe(strategy_module, strategy_csv, extra='return Decision([], note="probe")')
    run = run_strategy_scenario(SCENARIO)
    memory = _probe_memory(run)
    first = memory["2026-05-31"]
    assert first["trailing"] is None  # no month has finished yet
    # Seven percent a year, a twelfth of it each month: May and June.
    assert memory["2026-07-31"]["trailing"] == pytest.approx((1 + 0.07 / 12) ** 2 - 1, rel=1e-4)  # whole cents
    for year in range(2028, 2041):
        january = memory[f"{year}-01-31"]
        assert january["trailing"] == pytest.approx(january["annual"], rel=1e-9), year
    # A window reaching back before the run starts with it.
    assert memory["2027-03-31"]["two_years"] == pytest.approx((1 + 0.07 / 12) ** 10 - 1, rel=1e-4)


@pytest.mark.parametrize("months", ["0", "1.5", "True"])
def test_trailing_return_needs_whole_months(
    strategy_module: Callable[[str, str], Path], strategy_csv: Callable[[str, str], Path],
    run_strategy_scenario: Callable[[str], Path], months: str,
) -> None:
    _probe(strategy_module, strategy_csv, extra=f"ctx.trailing_return([], {months})")
    with pytest.raises(StrategyError, match="whole number of months"):
        run_strategy_scenario(SCENARIO)


# ---------- down_market ----------

def _down_market(strategy_csv: Callable[[str, str], Path], **params: object) -> None:
    """down_market owning a chain that sells stocks first, then the Roth's
    bonds, then savings."""
    _strategy(strategy_csv, "down_market", **params)
    strategy_csv(SCENARIO, "chains/source/portfolio_draw.csv").write_text(
        "account_name,lot,maximum,percent,series,cadence\n"
        "Asset_Brokerage_Joint,default,,,,\n"
        "Asset_Roth_Alice,default,,,,\n"
        "Asset_Roth_Alice,bonds,,,,\n"
        "Asset_Savings,,,,,\n")


@pytest.mark.allow_negative_balance  # the crash runs the household short late in the run
def test_down_market_skips_what_fell_and_leaves_the_rest_to_the_chain(
    strategy_csv: Callable[[str, str], Path], run_strategy_scenario: Callable[[str], Path],
) -> None:
    _crash(strategy_csv)
    _down_market(strategy_csv)
    run = run_strategy_scenario(SCENARIO)
    draws = _decisions(run, "draw")
    assert draws
    # Nothing fell before the crash: the chain decided alone.
    assert min(month for month, *_ in draws) > "2034-01-31"
    for month, note, detail, _fallback in draws:
        assert set(detail["fallen"]) == {"Asset_Brokerage_Joint:default", "Asset_Roth_Alice:default"}, month
        assert note.startswith("skipped Asset_Brokerage_Joint:default ("), note
    took = {(account, lot) for _m, account, lot, *_rest in _legs(run, "draw")}
    assert took <= {("Asset_Roth_Alice", "bonds"), ("Asset_Savings", "default")}
    # Once bonds and savings ran out the rest fell through to the chain's
    # steps, which sold the stocks after all.
    assert [note for _m, note, *_ in draws if "left to the chain" in note]
    assert sum(fallback for *_x, fallback in draws) > 0
    # A year after the crash ended its months have rolled out of the window.
    assert max(month for month, *_ in draws) < "2036-01-31"


@pytest.mark.allow_negative_balance
def test_down_market_lookback_two_years_spares_stocks_still_under_water(
    strategy_csv: Callable[[str, str], Path], run_strategy_scenario: Callable[[str], Path],
) -> None:
    _crash(strategy_csv)
    _down_market(strategy_csv, lookback=2)
    run = run_strategy_scenario(SCENARIO)
    draws = _decisions(run, "draw")
    # In 2036 the last twelve months gained, but the last 24 haven't made
    # the crash back.
    assert [month for month, *_ in draws if month.startswith("2036")]
    assert "over 24 months" in draws[-1][1]


@pytest.mark.allow_negative_balance
def test_down_market_annual_cadence_reads_the_last_calendar_year(
    strategy_csv: Callable[[str, str], Path], run_strategy_scenario: Callable[[str], Path],
) -> None:
    _crash(strategy_csv)
    _down_market(strategy_csv, cadence="annual")
    run = run_strategy_scenario(SCENARIO)
    draws = _decisions(run, "draw")
    # Only 2035's draws see a fallen 2034.
    assert {month[:4] for month, *_ in draws} == {"2035"}
    assert all(" in 2034: took" in note for _m, note, *_ in draws)


# ---------- buckets ----------

def test_buckets_draws_cash_first_and_refills_from_stocks_in_a_good_year(
    strategy_csv: Callable[[str, str], Path], run_strategy_scenario: Callable[[str], Path],
) -> None:
    run = run_strategy_scenario(SCENARIO)
    draws = _legs(run, "draw")
    assert draws
    assert {(account, lot) for _m, account, lot, *_ in draws} <= {
        ("Asset_Savings", "default"), ("Asset_Brokerage_Joint", "sweep"), ("Asset_Roth_Alice", "mm")}
    refills = _decisions(run, "month_start")
    month, note, detail, _ = refills[0]
    # The first month tops cash up to two years ($80,000) and bonds to
    # five ($200,000), from stocks.
    assert month == "2026-05-31"
    assert detail["targets"] == {"1": 8_000_000, "2": 20_000_000}
    assert detail["held"]["1"] == 8_000_000 and detail["held"]["2"] == 20_000_000
    assert "no bonds lot in Asset_Brokerage_Joint" in note
    assert all(d["stocks_up"] for _m, _n, d, _f in refills)
    # Every refill came out of a stock lot.
    for _m, account, lot, _to, _amount, _char in _legs(run, "month_start"):
        assert (account, lot) in {("Asset_Brokerage_Joint", "default"), ("Asset_Roth_Alice", "default")}


def test_buckets_refills_stay_inside_each_account(
    strategy_csv: Callable[[str, str], Path], run_strategy_scenario: Callable[[str], Path],
) -> None:
    run = run_strategy_scenario(SCENARIO)
    legs = _legs(run, "month_start")
    assert {account for _m, account, *_ in legs} == {"Asset_Brokerage_Joint", "Asset_Roth_Alice"}
    for _m, account, _lot, to, _amount, character in legs:
        if account == "Asset_Roth_Alice":
            # Within the Roth: no distribution, no income.
            assert to == "Asset_Roth_Alice" and character == "none", (to, character)
        elif to != account:
            # A taxable sale: its gain leg goes to the capital-gains account.
            assert character in ("ltcg", "stcg"), (to, character)
    assert any(character == "ltcg" for *_x, character in legs)  # the brokerage's sale realized a gain


def test_buckets_need_is_indexed_to_cpi(
    strategy_csv: Callable[[str, str], Path], run_strategy_scenario: Callable[[str], Path],
) -> None:
    run = run_strategy_scenario(SCENARIO)
    cpi = dict(_query(run, (
        "SELECT s.month, i.cumulative_index_micros FROM inflation_index_state i "
        "JOIN simulation_step s ON s.id = i.step_id WHERE i.series = 'cpi'")))
    refills = {month: detail for month, _n, detail, _f in _decisions(run, "month_start")}
    first, later = "2026-05-31", "2036-01-31"
    expected = 4_000_000 * cpi[later] / cpi[first]
    assert refills[later]["need"] == pytest.approx(expected, abs=5)


@pytest.mark.allow_negative_balance
def test_buckets_refills_cash_from_bonds_after_stocks_fall(
    strategy_csv: Callable[[str, str], Path], run_strategy_scenario: Callable[[str], Path],
) -> None:
    _crash(strategy_csv)
    run = run_strategy_scenario(SCENARIO)
    refills = {month: (note, detail) for month, note, detail, _f in _decisions(run, "month_start")}
    note, detail = refills["2034-12-31"]
    assert not detail["stocks_up"] and detail["stock_return"] < 0
    assert note.startswith("stocks -") and "bonds not refilled" in note and "from bonds" in note
    legs = [leg for leg in _legs(run, "month_start") if leg[0] == "2034-12-31"]
    assert legs and {(account, lot) for _m, account, lot, *_ in legs} == {("Asset_Roth_Alice", "bonds")}
    # Bonds are refilled again once stocks are back up.
    assert any(d["stocks_up"] and "bonds to" in n for m, (n, d) in refills.items() if m > "2035")


def test_buckets_annual_cadence_refills_only_in_january(
    strategy_csv: Callable[[str, str], Path], run_strategy_scenario: Callable[[str], Path],
) -> None:
    _buckets(strategy_csv, cadence="annual")
    run = run_strategy_scenario(SCENARIO)
    refills = _decisions(run, "month_start")
    assert refills and {month[5:7] for month, *_ in refills} == {"01"}
    assert " in 2026)" in refills[0][1]


@pytest.mark.parametrize(("params", "message"), [
    ({"cadence": "weekly"}, "cadence 'weekly': monthly or annual"),
    ({"lookback": 0}, "lookback 0: a whole number of years"),
    ({"cash_years": -1}, "cash_years -1: a number of years"),
    ({"bond_years": 1e309}, "bond_years inf: a number of years"),
    ({"need": "0.00"}, "need must be more than"),
    ({"portfolio": "portfolio:stocks_only"}, "has no lot mostly cash or cd and none mostly bond"),
])
def test_buckets_load_checks(
    strategy_csv: Callable[[str, str], Path], strategy_scenarios: Path, intent_dir: Path,
    params: dict[str, object], message: str,
) -> None:
    if params.get("portfolio") == "portfolio:stocks_only":
        path = strategy_csv(SCENARIO, "accounts.csv")
        path.write_text(path.read_text() + (
            "Asset_Stocks,asset,brokerage_taxable,joint,taxable,,gains,2020-01-31,,{},portfolio:stocks_only\n"))
        lots = strategy_csv(SCENARIO, "account_lots.csv")
        lots.write_text(lots.read_text() + "Asset_Stocks,default,,USSTK,,,,1000.00,1000.00,,,,,,,,,\n")
    _buckets(strategy_csv, **params)
    with pytest.raises(LoadError, match=message):
        load_scenario(strategy_scenarios, SCENARIO, intent_dir=intent_dir, allow_dirty_data=True,
                      compute_stamps=False)


def test_buckets_judges_a_blended_fund_by_its_bucket(
    strategy_csv: Callable[[str, str], Path], strategy_scenarios: Path, intent_dir: Path,
) -> None:
    """Cash and a 60/40 fund hold bonds, but no lot is mostly bonds: there
    is no bond bucket to refill or sell."""
    path = strategy_csv(SCENARIO, "accounts.csv")
    path.write_text(path.read_text() + (
        "Asset_Balanced,asset,brokerage_taxable,joint,taxable,,gains,2020-01-31,,{},portfolio:balanced\n"))
    lots = strategy_csv(SCENARIO, "account_lots.csv")
    lots.write_text(lots.read_text() + (
        "Asset_Balanced,default,,,,,,1000.00,1000.00,,,,,,,,,\n"
        "Asset_Balanced,cash,,CASH,,,money_market,100.00,100.00,,,,,,,,,\n"))
    allocations = strategy_scenarios / "allocations.csv"
    allocations.write_text(allocations.read_text()
                           + "Asset_Balanced:default,,stock_us,60%\nAsset_Balanced:default,,bond,40%\n")
    _strategy(strategy_csv, "buckets", portfolio="portfolio:balanced", need="1000.00")
    with pytest.raises(LoadError, match="no lot mostly bond"):
        load_scenario(strategy_scenarios, SCENARIO, intent_dir=intent_dir, allow_dirty_data=True,
                      compute_stamps=False)


def test_buckets_never_refills_across_traditional_and_roth_lots(
    strategy_csv: Callable[[str, str], Path], run_strategy_scenario: Callable[[str], Path],
) -> None:
    """A retirement account can hold traditional and Roth lots; selling a
    traditional stock lot into the Roth cash lot would be a conversion."""
    lots = strategy_csv(SCENARIO, "account_lots.csv")
    lots.write_text(lots.read_text()
                    + "Asset_Roth_Alice,rollover,traditional_ira,USSTK,,,,400000.00,,400000.00,,,,,,,,\n")
    run = run_strategy_scenario(SCENARIO)
    legs = _legs(run, "month_start")
    assert legs
    assert not [leg for leg in legs if leg[1:3] == ("Asset_Roth_Alice", "rollover")]
    assert all(character != "tax_deferred_out" for *_x, character in legs)


def test_buckets_without_bonds_is_fine_with_bond_years_zero(
    strategy_csv: Callable[[str, str], Path], strategy_scenarios: Path, intent_dir: Path,
) -> None:
    path = strategy_csv(SCENARIO, "accounts.csv")
    path.write_text(path.read_text() + (
        "Asset_Stocks,asset,brokerage_taxable,joint,taxable,,gains,2020-01-31,,{},portfolio:lean\n"))
    lots = strategy_csv(SCENARIO, "account_lots.csv")
    lots.write_text(lots.read_text() + (
        "Asset_Stocks,default,,USSTK,,,,1000.00,1000.00,,,,,,,,,\n"
        "Asset_Stocks,cash,,CASH,,,money_market,100.00,100.00,,,,,,,,,\n"))
    _strategy(strategy_csv, "buckets", portfolio="portfolio:lean", need="1000.00")
    with pytest.raises(LoadError, match="no lot mostly bond"):
        load_scenario(strategy_scenarios, SCENARIO, intent_dir=intent_dir, allow_dirty_data=True,
                      compute_stamps=False)
    _strategy(strategy_csv, "buckets", portfolio="portfolio:lean", need="1000.00", bond_years=0)
    load_scenario(strategy_scenarios, SCENARIO, intent_dir=intent_dir, allow_dirty_data=True,
                  compute_stamps=False)


def test_down_market_load_checks(
    strategy_csv: Callable[[str, str], Path], strategy_scenarios: Path, intent_dir: Path,
) -> None:
    _strategy(strategy_csv, "down_market", cadence="yearly", lookback=-1)
    with pytest.raises(LoadError) as info:
        load_scenario(strategy_scenarios, SCENARIO, intent_dir=intent_dir, allow_dirty_data=True,
                      compute_stamps=False)
    assert "cadence 'yearly'" in str(info.value) and "lookback -1" in str(info.value)
