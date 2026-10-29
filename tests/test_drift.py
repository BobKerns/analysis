"""Allocation drift (#359): class dollars from a run, drift against dated
targets, the ensemble store's copy of both, and the CLI and widget views."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim import drift
from analysis.sim.series_db import open_series

PORTFOLIO = "portfolio:invest"


def _con(run: Path) -> sqlite3.Connection:
    return sqlite3.connect(run)


def _asset_balances(con: sqlite3.Connection, tag: str | None = None) -> pd.Series:
    query = (
        "SELECT s.month, SUM(ls.balance) / 100.0 FROM lot_state ls "
        "JOIN simulation_step s ON s.id = ls.step_id JOIN account_lot m ON m.id = ls.lot_id "
        "JOIN account a ON a.id = m.account_id WHERE a.kind = 'asset'"
    )
    args: tuple[str, ...] = ()
    if tag:
        query += (" AND a.id IN (SELECT at.account_id FROM account_tag at JOIN tag t ON t.id = at.tag_id "
                  "WHERE t.role || ':' || t.name = ?)")
        args = (tag,)
    return pd.Series(dict(con.execute(query + " GROUP BY s.month", args).fetchall()))


def test_drift_is_share_less_target_with_the_set_in_force() -> None:
    dollars = pd.DataFrame(
        {"stock_us": [60.0, 50.0, 0.0], "bond": [20.0, 50.0, 0.0], "cash": [20.0, 0.0, 0.0]},
        index=pd.Index(["2030-01-31", "2035-01-31", "2036-01-31"], name="month"),
    )
    targets = [("", {"stock_us": 0.6, "bond": 0.4}), ("2035-01-31", {"stock_us": 0.5, "bond": 0.5})]
    frame = drift.drift(dollars, targets)
    assert list(frame.columns) == ["stock_us", "bond", "cash", drift.MAX_ABS]
    assert frame.loc["2030-01-31"].tolist() == [0.0, -20.0, 20.0, 20.0]
    # The glide path's later set applies from its as_of on.
    assert frame.loc["2035-01-31"].tolist() == [0.0, 0.0, 0.0, 0.0]
    # A month with nothing held is all zero, not NaN.
    assert frame.loc["2036-01-31"].tolist() == [0.0, 0.0, 0.0, 0.0]


def test_class_dollars_add_up_to_the_asset_lots(run_strategy_scenario: Callable[[str], Path]) -> None:
    con = _con(run_strategy_scenario("alice_rebalance"))
    try:
        frames = drift.class_dollars(con)
        assert drift.ALL_LOTS in frames and PORTFOLIO in frames
        for portfolio, tag in ((drift.ALL_LOTS, None), (PORTFOLIO, PORTFOLIO)):
            got = frames[portfolio].sum(axis=1)
            want = _asset_balances(con, tag).reindex(got.index)
            np.testing.assert_allclose(got.to_numpy(), want.to_numpy(), atol=0.005, err_msg=portfolio)
        # The portfolio is the brokerage (USSTK) and the IRA (BONDX): two classes.
        assert set(frames[PORTFOLIO].columns) == {"stock_us", "bond"}
    finally:
        con.close()


def test_a_dated_allocation_applies_from_its_month(
    strategy_scenarios: Path, run_strategy_scenario: Callable[[str], Path],
) -> None:
    """USSTK becomes half bonds from 2030: the stretch of months on each side
    of the as_of gets its own weights."""
    path = strategy_scenarios / "allocations.csv"
    path.write_text(path.read_text() + "USSTK,2030-01,stock_us,50%\nUSSTK,2030-01,bond,50%\n")
    con = _con(run_strategy_scenario("alice_rebalance"))
    try:
        frame = drift.class_dollars(con, portfolios=[PORTFOLIO])[PORTFOLIO]
        brokerage = dict(con.execute(
            "SELECT s.month, ls.balance / 100.0 FROM lot_state ls JOIN simulation_step s ON s.id = ls.step_id "
            "JOIN account_lot m ON m.id = ls.lot_id JOIN account a ON a.id = m.account_id "
            "WHERE a.name = 'Asset_Brokerage_Joint'").fetchall())
    finally:
        con.close()
    assert frame.loc["2029-12-31", "stock_us"] == pytest.approx(brokerage["2029-12-31"])
    assert frame.loc["2030-01-31", "stock_us"] == pytest.approx(brokerage["2030-01-31"] / 2)


def test_choose_defaults_to_the_strategy_targets_portfolio_and_band(
    run_strategy_scenario: Callable[[str], Path],
) -> None:
    con = _con(run_strategy_scenario("alice_rebalance"))
    try:
        assert drift.choose(con) == ("main", PORTFOLIO, 5.0)
        assert drift.choose(con, "main", "") == ("main", "", None)
        with pytest.raises(ValueError, match="no target set 'other'"):
            drift.choose(con, "other")
        by_month = drift.drift_by_month(con, "main", portfolio=PORTFOLIO)
    finally:
        con.close()
    assert set(by_month.columns) == {"stock_us", "bond", drift.MAX_ABS}
    assert list(drift.year_end_rows(by_month).index)[-1] == by_month.index[-1]


def test_pick_needs_a_target_set_when_nothing_settles_it() -> None:
    with pytest.raises(ValueError, match="say which target set: a, b"):
        drift.pick([], ["a", "b"])
    assert drift.pick([], ["only"]) == ("only", None, None)


def test_the_store_keeps_each_members_drift(strategy_ensemble_batch: Path) -> None:
    """The store's class dollars give each member the drift its own run file
    gives, and the batch's targets and strategy band come with them."""
    store = open_series(strategy_ensemble_batch)
    assert sorted(store.target_sets()) == ["main"]
    assert [(w.strategy, w.target_set, w.portfolio, w.band) for w in store.watches()] == [
        ("draw_by_allocation", "main", PORTFOLIO, 5.0)]
    assert drift.ALL_LOTS in store.portfolios() and PORTFOLIO in store.portfolios()
    members = store.drift("main", PORTFOLIO)
    assert len(members) == 3
    for stem, got in members.items():
        con = _con(strategy_ensemble_batch / f"{stem}.sqlite3")
        try:
            want = drift.drift_by_month(con, "main", portfolio=PORTFOLIO)
        finally:
            con.close()
        pd.testing.assert_frame_equal(got, want, check_names=False, atol=0.051)
    frame = store.drift_frame("main", PORTFOLIO)
    assert list(frame.columns) == sorted(members)
    assert isinstance(frame.index, pd.DatetimeIndex)


def test_ensemble_drift_table_counts_members_past_the_band() -> None:
    members = pd.DataFrame(
        {"m0": [1.0, 6.0, 2.0], "m1": [7.0, 1.0, 1.0]},
        index=pd.Index(["2030-12-31", "2031-12-31", "2032-06-30"]),
    )
    table = drift.ensemble_drift_table(members, band=5.0)
    assert table["past_band"].tolist() == [0.5, 0.5, 0.0]
    assert table["ever_past"].tolist() == [0.5, 1.0, 1.0]
    assert table["max"].tolist() == [7.0, 6.0, 2.0]


def test_sim_ensemble_drift_reports_by_year(
    strategy_ensemble_batch: Path, strategy_scenarios: Path, runs_dir: Path, tmp_path: Path,
) -> None:
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "drift", "alice_rebalance", "--data-dir", str(strategy_scenarios),
        "--runs-dir", str(runs_dir), "--graph-file", str(tmp_path / "drift.png"),
    ], terminal_width=200)
    assert result.exit_code == 0, result.output
    assert ("3 member(s); drift from target set main (accounts tagged portfolio:invest), "
            "largest drift either way, percentage points; band ±5") in result.output
    assert "past_band" in result.output and "ever_past" in result.output
    assert (tmp_path / "drift.png").stat().st_size > 0
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "drift", "alice_rebalance", "--data-dir", str(strategy_scenarios),
        "--runs-dir", str(runs_dir), "--asset-class", "bond", "--targets", "nope",
    ])
    assert result.exit_code != 0
    assert "no target set 'nope'" in result.output


def test_the_drift_widget_draws_a_run(
    run_strategy_scenario: Callable[[str], Path], capsys: pytest.CaptureFixture[str],
) -> None:
    from analysis.sim.connection import RunConnection
    from analysis.sim.notebook_ui.allocation_widgets import allocation_drift_widget

    rc = RunConnection(run_strategy_scenario("alice_rebalance"))
    try:
        box = allocation_drift_widget(rc)
        targets, portfolio = box.children[0].children[:2]
        assert (targets.value, portfolio.value) == ("main", PORTFOLIO)
        portfolio.value = "(every asset lot)"
        # Outside a kernel, what the widget displays is printed: a chart and
        # a table each time it draws.
        assert capsys.readouterr().out.count("Image object") == 2
    finally:
        rc.close()


def test_the_ensemble_drift_widget_draws_a_batch(
    strategy_ensemble_batch: Path, strategy_scenarios: Path, runs_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from analysis.sim.notebook_ui.ensemble_widgets import ensemble_drift_widget

    box = ensemble_drift_widget(data_dir=strategy_scenarios, runs_dir=runs_dir, default="alice_rebalance")
    targets, portfolio, measure = box.children[1].children[:3]
    assert (targets.value, portfolio.value) == ("main", PORTFOLIO)
    assert list(measure.options) == ["largest either way", "stock_us", "bond"]
    measure.value = "bond"
    printed = capsys.readouterr().out
    assert printed.count("Image object") == 2, printed


def test_sim_compare_puts_a_strategy_on_and_off_side_by_side(strategy_scenarios: Path) -> None:
    def sim(*args: str):
        return CliRunner().invoke(main, ["sim", *args, "--data-dir", str(strategy_scenarios)],
                                  terminal_width=200)

    for name in ("alice_rebalance", "alice_rebalance_off"):
        result = sim("run", name, "--allow-dirty-data", "--allow-dirty-code")
        assert result.exit_code == 0, result.output
    result = sim("compare", "alice_rebalance", "alice_rebalance_off",
                 "--section", "differences", "--section", "strategies")
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Strategies that differ" in out and "draw_by_allocation" in out
    assert "Target weights that differ (percent):\n  none" in out
    assert "Strategies over the run" in out
    assert "alice_rebalance  draw_by_allocation  rebalance  portfolio_draw" in out
    assert "draw_by_allocation  kind" in out  # the off run doesn't run it: a difference
    assert ("Largest drift from target set main at year end (accounts tagged portfolio:invest), "
            "percentage points; band ±5:") in out
    # The strategy keeps its run's drift no larger than the run without it.
    lines = out.split("percentage points; band ±5:\n", 1)[1].splitlines()
    rows = [line.split() for line in lines[1:] if line.strip()[:2] == "20"]
    assert rows and all(float(on) <= float(off) + 1e-9 for _m, on, off in rows[-3:])


def test_the_strategy_compare_widget_overlays_the_runs(
    run_strategy_scenario: Callable[[str], Path], runs_dir: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from analysis.sim.connection import RunConnection
    from analysis.sim.notebook_ui.compare_plots import rules_and_events_widget, strategy_compare_widget
    from analysis.sim.notebook_ui.pickers import MultiScenarioConnections

    on = run_strategy_scenario("alice_rebalance")
    (runs_dir / "on.sqlite3").write_bytes(on.read_bytes())
    off = run_strategy_scenario("alice_rebalance_off")
    connections = {"alice_rebalance": RunConnection(runs_dir / "on.sqlite3"), "alice_rebalance_off": RunConnection(off)}
    try:
        container = MultiScenarioConnections(connections, "alice_rebalance", ["alice_rebalance_off"], {})
        strategy_compare_widget(container)
        printed = capsys.readouterr().out
        assert printed.count("Image object") == 1, printed
        assert printed.count("Styler object") == 2, printed  # drift by year end, then what the strategies did
        rules_and_events_widget(container, connections, container.scenarios, container.variants,
                                baseline="alice_rebalance")
        # The strategy the off run doesn't run is a difference.
        assert "draw_by_allocation" in capsys.readouterr().out
    finally:
        for rc in connections.values():
            rc.close()
