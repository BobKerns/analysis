"""Basis step-up at a death (#255, IRC §1014): at the end of a person's final
month, the lots they held reset their cost basis to their value, down as well
as up. A joint lot steps up in full under community property, or with no one
left alive; otherwise only the decedent's half does. Retirement money never
steps up. Charts mark each death with a red line."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pandas as pd
import pytest
from click.testing import CliRunner
from matplotlib.dates import date2num
from matplotlib.figure import Figure

from analysis.cli import main
from analysis.sim import compare, engine
from analysis.sim.connection import RunConnection
from analysis.sim.engine import run_scenario
from analysis.sim.ensemble import base_case_deaths
from analysis.sim.loader import LoadError, LoadResult
from analysis.sim.plotting import mark_deaths
from analysis.sim.types import Death

BROKERAGE = "Asset_Brokerage_Joint"
ALICE_DIES = "alice,70,,,,,,"  # born 1965-03-15: final month 2035-03
BILL_DIES = "bill,72,,,,,,"  # born 1967-08-20: final month 2039-08
ALICE_FINAL, BILL_FINAL = "2035-03-31", "2039-08-31"


def _plan(load: Callable[[], LoadResult]) -> dict[str, set[tuple[str, str, int]]]:
    """The step-ups by final month, as (account, person, share)."""
    with sqlite3.connect(load().intent_path) as con:
        names = dict(con.execute(
            "SELECT m.id, a.name FROM account_lot m JOIN account a ON a.id = m.account_id"
        ).fetchall())
        plan = engine._basis_step_ups(con)
    return {month: {(names[s.lot_id], s.person, s.share) for s in ups} for month, ups in plan.items()}


def test_no_lifespan_no_step_up(load_test_copy: Callable[[], LoadResult]) -> None:
    assert _plan(load_test_copy) == {}


def test_a_joint_lot_steps_up_half_at_the_first_death_and_in_full_at_the_second(
    write_lifespans: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    write_lifespans(ALICE_DIES, BILL_DIES)
    # Cash, the IRAs and the Roths never step up; the home and brokerage do.
    assert _plan(load_test_copy) == {
        ALICE_FINAL: {(BROKERAGE, "alice", 2), ("Asset_Home", "alice", 2)},
        BILL_FINAL: {(BROKERAGE, "bill", 1), ("Asset_Home", "bill", 1)},
    }


def test_community_property_steps_up_both_halves_at_the_first_death(
    write_lifespans: Callable[..., None], set_scenario_meta: Callable[..., None],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    write_lifespans(ALICE_DIES)
    set_scenario_meta(community_property=True)
    assert _plan(load_test_copy) == {
        ALICE_FINAL: {(BROKERAGE, "alice", 1), ("Asset_Home", "alice", 1)},
    }


BILL_FIRST = "2027-08-31"
HOME = {BILL_FIRST: {("Asset_Home", "bill", 2)}, ALICE_FINAL: {("Asset_Home", "alice", 1)}}


@pytest.mark.parametrize(("owner", "brokerage"), [
    # Alice's own lot: nothing at Bill's death, all of it at hers.
    ("alice", {ALICE_FINAL: {(BROKERAGE, "alice", 1)}}),
    # Bill's: all of it at his death, and again when Alice, who inherited it, dies.
    ("bill", {BILL_FIRST: {(BROKERAGE, "bill", 1)}, ALICE_FINAL: {(BROKERAGE, "alice", 1)}}),
])
def test_a_lot_one_person_owns_steps_up_at_their_death_and_their_heirs(
    owner: str, brokerage: dict[str, set[tuple[str, str, int]]],
    write_lifespans: Callable[..., None], update_account: Callable[..., None],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    write_lifespans(ALICE_DIES, "bill,60,,,,,,")  # Bill dies first, in 2027-08
    update_account(BROKERAGE, owner=owner)
    # The joint home: Bill's half at his death, then all of it at Alice's.
    assert _plan(load_test_copy) == {
        month: HOME[month] | brokerage.get(month, set()) for month in HOME}


def test_two_deaths_in_one_month_step_a_joint_lot_up_once_in_full(
    write_lifespans: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    write_lifespans(ALICE_DIES, "bill,67.5833,,,,,,")  # also 2035-03
    assert _plan(load_test_copy) == {
        ALICE_FINAL: {(BROKERAGE, "alice", 1), ("Asset_Home", "alice", 1)},
    }


def test_a_step_up_resets_basis_down_too_and_makes_the_lot_long_term(
    ledger_con: sqlite3.Connection,
) -> None:
    balances = {1: 100_00, 2: 100_00, 3: 100_00}
    cost_bases: dict[int, int | None] = {1: 40_00, 2: 160_00, 3: None}
    acquired: dict[int, date | None] = dict.fromkeys(balances, date(2035, 1, 15))
    engine._step_up_basis(
        ledger_con, 1, "2035-03-31",
        [engine.StepUp(1, "alice", 2), engine.StepUp(2, "alice", 1), engine.StepUp(3, "alice", 1)],
        balances, cost_bases, acquired,
    )
    # Half of a 60.00 gain; all of a 60.00 loss; a lot with no basis is left alone.
    assert cost_bases == {1: 70_00, 2: 100_00, 3: None}
    assert acquired == {1: None, 2: None, 3: date(2035, 1, 15)}
    assert ledger_con.execute(
        "SELECT lot_id, person, balance, basis_before, basis_after FROM basis_step_up ORDER BY id"
    ).fetchall() == [(1, "alice", 100_00, 40_00, 70_00), (2, "alice", 100_00, 160_00, 100_00)]


def _run_step_up(load: Callable[[], LoadResult], runs_dir: Path) -> tuple[int, int, int, int]:
    """The brokerage's step-up at Alice's death, (balance, basis_before,
    basis_after, the lot's basis the month before), checked against lot_state."""
    run = run_scenario(load().intent_path, runs_dir / "step_up.sqlite3", run_by="T",
                       allow_dirty_data=True, allow_dirty_code=True)
    with sqlite3.connect(run) as con:
        (balance, before, after), = con.execute(
            "SELECT b.balance, b.basis_before, b.basis_after FROM basis_step_up b "
            "JOIN account_lot m ON m.id = b.lot_id JOIN account a ON a.id = m.account_id "
            "WHERE a.name = ? AND b.date = ?", (BROKERAGE, ALICE_FINAL),
        ).fetchall()
        states = dict(con.execute(
            "SELECT s.month, ls.balance || ':' || ls.cost_basis FROM lot_state ls "
            "JOIN simulation_step s ON s.id = ls.step_id JOIN account_lot m ON m.id = ls.lot_id "
            "JOIN account a ON a.id = m.account_id WHERE a.name = ? AND s.month IN (?, ?)",
            (BROKERAGE, "2035-02-28", ALICE_FINAL),
        ).fetchall())
    assert states[ALICE_FINAL] == f"{balance}:{after}"
    return balance, before, after, int(states["2035-02-28"].split(":")[1])


@pytest.mark.parametrize("community", [False, True])
def test_the_run_resets_the_basis_at_the_end_of_the_final_month(
    community: bool, write_lifespans: Callable[..., None],
    set_scenario_meta: Callable[..., None], fund_asset_savings: Callable[..., None],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    fund_asset_savings("500000.00")  # the death halves the income
    write_lifespans(ALICE_DIES)
    set_scenario_meta(community_property=community)
    balance, before, after, month_before = _run_step_up(load_test_copy, runs_dir)
    assert before == month_before
    assert balance > before
    assert after == (balance if community else before + (balance - before) // 2)


def test_a_lot_worth_less_than_its_basis_steps_down(
    write_lifespans: Callable[..., None], set_scenario_meta: Callable[..., None],
    update_account_value: Callable[..., None], fund_asset_savings: Callable[..., None],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    fund_asset_savings("500000.00")  # the death halves the income
    write_lifespans(ALICE_DIES)
    set_scenario_meta(community_property=True)
    update_account_value(BROKERAGE, "default", opening_cost_basis="5000000.00")
    balance, before, after, _ = _run_step_up(load_test_copy, runs_dir)
    assert after == balance < before


def test_community_property_must_be_true_or_false(
    set_scenario_meta: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    set_scenario_meta(community_property="yes")
    with pytest.raises(LoadError, match="community_property must be true or false"):
        load_test_copy()


def test_sim_explain_lists_the_step_ups(
    write_lifespans: Callable[..., None], fund_asset_savings: Callable[..., None],
    test_data_dir: Path, intent_dir: Path, runs_dir: Path,
) -> None:
    fund_asset_savings("500000.00")  # the death halves the income
    write_lifespans(ALICE_DIES)
    runner = CliRunner()
    loaded = runner.invoke(main, [
        "sim", "load", "retire_alice", "--data-dir", str(test_data_dir),
        "--intent-dir", str(intent_dir), "--runs-dir", str(runs_dir), "--allow-dirty-data",
    ])
    assert loaded.exit_code == 0, loaded.output
    result = runner.invoke(main, [
        "sim", "explain", "retire_alice", "--data-dir", str(test_data_dir), "--runs-dir", str(runs_dir),
    ])
    assert result.exit_code == 0, result.output
    lines = result.output.split("basis step-ups at a death (2):", 1)[1].splitlines()[1:3]
    assert [line.split()[:3] for line in lines] == [
        ["2035-03", "Asset_Brokerage_Joint", "(default)"], ["2035-03", "Asset_Home", "alice"],
    ]
    assert all("basis $" in line and " -> $" in line for line in lines)


# -- Deaths on charts (#255) --------------------------------------------------

ALICE, BILL = Death("Alice", date(2035, 3, 31)), Death("Bill", date(2039, 8, 31))


def _death_lines(ax) -> list[tuple[float, str]]:
    return [(line.get_xdata()[0], line.get_linestyle())
            for line in ax.get_lines() if line.get_color() == "red"]


def test_deaths_are_dashed_red_lines_not_in_the_legend() -> None:
    ax = Figure().subplots()
    ax.plot(pd.date_range("2030-01-31", "2040-12-31", freq="ME"), range(132), label="net worth")
    xlim = ax.get_xlim()
    mark_deaths(ax, [BILL, ALICE])
    assert _death_lines(ax) == [(date2num(BILL.month), "--"), (date2num(ALICE.month), "--")]
    assert [t.get_text() for t in ax.legend().get_texts()] == ["net worth"]
    assert ax.get_xlim() == xlim


def test_a_death_outside_the_chart_is_left_out() -> None:
    ax = Figure().subplots()
    ax.plot(pd.date_range("2030-01-31", "2040-12-31", freq="ME"), range(132))
    xlim = ax.get_xlim()
    mark_deaths(ax, [ALICE, Death("Carol", date(2050, 1, 31))])
    # Carol's, past the end, doesn't widen the axis.
    assert _death_lines(ax) == [(date2num(ALICE.month), "--")]
    assert ax.get_xlim() == xlim


def test_on_a_bar_chart_of_years_a_death_falls_in_its_years_bar() -> None:
    ax = Figure().subplots()
    years = list(range(2033, 2041))
    ax.bar([str(y) for y in years], [1] * len(years))
    mark_deaths(ax, [ALICE, BILL], years=years)
    assert _death_lines(ax) == [(2 + (3 - 6.5) / 12, "--"), (6 + (8 - 6.5) / 12, "--")]


def test_a_run_reads_its_deaths(
    write_lifespans: Callable[..., None], fund_asset_savings: Callable[..., None],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    fund_asset_savings("500000.00")
    write_lifespans(BILL_DIES, ALICE_DIES)
    run = run_scenario(load_test_copy().intent_path, runs_dir / "deaths.sqlite3", run_by="T",
                       allow_dirty_data=True, allow_dirty_code=True)
    with RunConnection(run) as rc:
        assert [(d.person, d.month) for d in rc.deaths()] == [
            ("Alice (fake)", date(2035, 3, 31)), ("Bill (fake)", date(2039, 8, 31)),
        ]


def _fake_run(deaths: list[Death], member: int | None = None) -> RunConnection:
    return cast(RunConnection, SimpleNamespace(
        deaths=lambda: deaths, ensemble_member=lambda: None if member is None else (member, 1)))


def test_a_comparison_marks_each_scenarios_deaths_once() -> None:
    same = {"a": _fake_run([ALICE]), "b": _fake_run([ALICE])}
    assert compare.scenario_deaths(same, ["a", "b"]) == [ALICE]
    later = Death("Alice", date(2037, 3, 31))
    moved = {"a": _fake_run([ALICE, BILL]), "b": _fake_run([later, BILL])}
    assert compare.scenario_deaths(moved, ["a", "b"]) == [ALICE, later, BILL]


def test_an_ensemble_chart_marks_member_0s_deaths() -> None:
    members = {"member_0001": _fake_run([BILL], 1), "member_0000": _fake_run([ALICE], 0)}
    assert base_case_deaths(members) == [ALICE]
    assert base_case_deaths({"member_0001": _fake_run([BILL], 1)}) == []


@pytest.mark.slow
def test_the_net_worth_notebook_charts_mark_the_deaths(
    write_lifespans: Callable[..., None], fund_asset_savings: Callable[..., None],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from analysis.sim import reports, run_charts
    from analysis.sim.notebook_ui import networth_plots

    fund_asset_savings("500000.00")
    write_lifespans(ALICE_DIES, BILL_DIES)
    run = run_scenario(load_test_copy().intent_path, runs_dir / "charts.sqlite3", run_by="T",
                       allow_dirty_data=True, allow_dirty_code=True)
    with RunConnection(run) as rc:
        for widget in (networth_plots.net_worth_widget, networth_plots.income_widget,
                       networth_plots.expenses_widget, networth_plots.gains_widget,
                       networth_plots.economic_income_widget, networth_plots.tax_widget):
            widget(rc)
        # The widgets draw these (#327); each marks both deaths.
        deaths = rc.deaths()
        income = reports.flows_by_year(rc, "income")
        figures = [
            run_charts.net_worth_figure(rc.net_worth_frame()[["nominal"]], "t", deaths=deaths),
            run_charts.flows_figure(income, "t", deaths=deaths),
            run_charts.flows_figure(reports.flows_by_year(rc, "expenses"), "t", deaths=deaths),
            run_charts.gains_figure(reports.gains_by_year(rc), "t", deaths=deaths),
            run_charts.economic_income_figure(income, "t", deaths=deaths),
        ]
    # An error inside a widget's Output is printed, not raised.
    assert "Traceback" not in "".join(capsys.readouterr())
    assert all(len(_death_lines(fig.axes[0])) == 2 for fig in figures)


def test_a_joint_lot_steps_up_half_however_many_survive(
    test_data_dir: Path, write_lifespans: Callable[..., None],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    # No primary persons: the household is everyone with a role, here three.
    persons = test_data_dir / "persons.csv"
    persons.write_text(persons.read_text().replace(",primary,", ",secondary,")
                       + "carol,Carol (fake),1990-01-01,f,secondary,{}\n")
    write_lifespans(ALICE_DIES)
    assert _plan(load_test_copy) == {
        ALICE_FINAL: {(BROKERAGE, "alice", 2), ("Asset_Home", "alice", 2)},
    }


def _living_months(load: Callable[[], LoadResult], runs_dir: Path) -> list[str]:
    """The months the `living` rule posted."""
    run = run_scenario(load().intent_path, runs_dir / "joint.sqlite3", run_by="T",
                       allow_dirty_data=True, allow_dirty_code=True)
    with sqlite3.connect(run) as con:
        return [r[0] for r in con.execute(
            "SELECT DISTINCT s.month FROM transfer_event e "
            "JOIN simulation_step s ON s.id = e.step_id "
            "JOIN scheduled_transaction t ON t.id = e.reason_id "
            "WHERE e.reason_kind = 'rule' AND t.name = 'living' ORDER BY s.month")]


def test_a_joint_rule_stops_after_the_second_death(
    write_lifespans: Callable[..., None], update_scheduled: Callable[..., None],
    fund_asset_savings: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    runs_dir: Path,
) -> None:
    fund_asset_savings("500000.00")
    write_lifespans(ALICE_DIES, BILL_DIES)
    update_scheduled("living", person="joint")
    months = _living_months(load_test_copy, runs_dir)
    assert ALICE_FINAL < months[-1] == BILL_FINAL


def test_a_joint_rule_runs_on_while_anyone_has_no_lifespan(
    write_lifespans: Callable[..., None], update_scheduled: Callable[..., None],
    fund_asset_savings: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    runs_dir: Path,
) -> None:
    fund_asset_savings("500000.00")
    write_lifespans(ALICE_DIES)
    update_scheduled("living", person="joint")
    with sqlite3.connect(load_test_copy().intent_path) as con:
        end = con.execute('SELECT "end" FROM scenario').fetchone()[0]
    assert _living_months(load_test_copy, runs_dir)[-1] == end
