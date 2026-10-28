"""Guard rails (#305): reading guard_rails.csv, the loader's checks, and
evaluating rails on runs."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from analysis.sim import guard_rails as gr
from analysis.sim import measures, reports
from analysis.sim.connection import RunConnection
from analysis.sim.loader import LoadError, LoadResult
from analysis.sim.provenance import data_stamps
from analysis.sim.types import DollarMode

RAIL = {"name": "r", "measure": "withdrawals", "side": "above", "threshold": "4%",
        "basis": "portfolio"}


def _by_name(results: list[gr.RailResult]) -> dict[str, gr.RailResult]:
    return {r.rail.name: r for r in results}


@pytest.fixture
def prototype_results(retire_alice_run: RunConnection, scenarios_dir: Path) -> dict[str, gr.RailResult]:
    """The prototype retire_alice's own rails on its run, CPI-constant."""
    return _by_name(gr.evaluate_run(retire_alice_run, gr.run_rails(retire_alice_run, scenarios_dir)))


def test_the_prototype_rails_read_as_the_proposal_describes(
    prototype_results: dict[str, gr.RailResult], retire_alice_run: RunConnection,
) -> None:
    """retire_alice bridges 2031-2037 to Bill's Social Security at 70: the
    yearly withdrawal rails flag the bridge; the 4% rule is still overdrawn
    cumulatively at the end; the budget is crossed early, then made up."""
    four = gr.rail_frame(prototype_results["four_percent"])
    assert list(four.index) == list(range(2031, 2041))
    # 4% of the portfolio at the end of 2030, CPI-indexed: the same every year in CPI-constant dollars.
    assert four["threshold"].max() - four["threshold"].min() < 1.0
    assert (four.loc[2031:2036, "margin"] < 0).all()
    assert (four.loc[2037:, "margin"] > 0).all()
    np.testing.assert_allclose(four["cumulative"], four["margin"].cumsum())
    assert four.loc[2040, "cumulative"] < 0
    # Carried at the portfolio's (positive) returns, the overdraft grew.
    assert four.loc[2040, "carried"] < four.loc[2040, "cumulative"]

    budget = gr.rail_frame(prototype_results["budget"])
    assert (budget.loc[2031:2034, "margin"] < 0).all()
    assert budget.loc[2034, "cumulative"] < 0 < budget.loc[2040, "cumulative"]

    lower = gr.rail_frame(prototype_results["gk_lower"])
    assert prototype_results["gk_lower"].rail.positive
    assert (lower.loc[2038:, "margin"] < 0).all()  # past the positive rail: room to spend
    assert (lower["cumulative"] > 0).all()


def test_spending_is_the_income_reports_spending(
    prototype_results: dict[str, gr.RailResult], retire_alice_run: RunConnection,
) -> None:
    """A spending rail measures the same spending as `sim report income` (#249)."""
    economic = reports.flows_by_year(retire_alice_run, "income", mode=DollarMode.CPI_CONSTANT)
    budget = gr.rail_frame(prototype_results["budget"])
    np.testing.assert_allclose(budget["measure"], economic.loc[budget.index, "spending"])


def test_select_picks_spending_by_rule_then_account_tag(
    report_run: RunConnection, test_scenarios_dir: Path,
) -> None:
    """report_run tags Expense_Living budget:core but its ``living`` rule
    budget:fun: the rule's tag wins in that role, as `--by tag:budget` reads it."""
    rails = [
        gr.GuardRail("all", "spending", "above", 1, "nominal"),
        gr.GuardRail("core", "spending", "above", 1, "nominal", select=gr.TagFilter.parse("budget:core")),
        gr.GuardRail("not_fun", "spending", "above", 1, "nominal",
                     select=gr.TagFilter.parse("-budget:fun")),
        gr.GuardRail("budget", "spending", "above", 1, "nominal", select=gr.TagFilter.parse("budget:*")),
    ]
    measured = {name: gr.rail_frame(r)["measure"]
                for name, r in _by_name(gr.evaluate_run(report_run, rails, DollarMode.NOMINAL)).items()}
    by_tag = reports.flows_by_year(report_run, "expenses", by="tag:budget")
    years = measured["core"].index
    np.testing.assert_allclose(measured["core"], by_tag.loc[years, "core"])
    np.testing.assert_allclose(measured["budget"], by_tag.loc[years, ["core", "home", "fun"]].sum(axis=1))
    np.testing.assert_allclose(measured["not_fun"], measured["all"] - by_tag.loc[years, "fun"])


def _inputs(transfers: list[tuple[str, str, str, str, str, float]]) -> gr._RunInputs:
    """Run inputs of one month, from (from, from_kind, from_subkind, to,
    to_kind, amount) transfers."""
    months = pd.DatetimeIndex(["2031-01-31"])
    frame = pd.DataFrame(transfers, columns=["from_name", "from_kind", "from_subkind",
                                             "to_name", "to_kind", "amount"])
    frame["month"] = months[0]
    return gr._RunInputs(
        months=months, axis=measures.year_axis(np.array([2031])), index=np.array([1e6]),
        expenses=pd.DataFrame(columns=["month", "amount", "tags"]), transfers=frame,
        balances=pd.DataFrame(index=months), accounts=pd.DataFrame(columns=["kind", "subkind", "tag_set"]),
        markers={}, last_month=months[0],
    )


def test_withdrawals_count_what_leaves_the_portfolio_net_of_outside_money() -> None:
    """Moves inside the portfolio (an RMD to checking) are not withdrawals;
    returns earned inside it don't offset them; outside income does."""
    inputs = _inputs([
        ("IRA", "asset", "retirement", "Checking", "asset", 100.0),  # inside: nothing
        ("Checking", "asset", "cash", "Expense_Living", "expense", 50.0),  # out
        ("Checking", "asset", "cash", "Home", "asset", 20.0),  # out, to an asset outside
        ("Income_SS", "income", "ss_benefit", "Checking", "asset", 30.0),  # outside money in
        ("Income_Interest", "income", "interest", "Brokerage", "asset", 10.0),  # a return
        ("Brokerage", "asset", "brokerage_taxable", "Income_CapitalGain", "income", 5.0),  # a drop
    ])
    assert gr._withdrawals(inputs, ["IRA", "Checking", "Brokerage"]).tolist() == [40.0]


def test_the_default_portfolio_leaves_out_homes_and_cars(retire_alice_run: RunConnection) -> None:
    inputs = gr._run_inputs(retire_alice_run)
    rail = gr.GuardRail("r", "withdrawals", "above", 40_000, "portfolio")
    portfolio = gr._portfolio(inputs, rail)
    assert "Asset_Home" not in portfolio
    assert {"Asset_Checking", "Asset_Brokerage_Joint", "Asset_TraditionalIRA_Alice"} <= set(portfolio)
    only_ira = gr._portfolio(inputs, gr.GuardRail("r", "withdrawals", "above", 40_000, "portfolio",
                                                  portfolio=gr.TagFilter.parse("-x:y")))
    assert only_ira == portfolio  # an exclusion nothing carries leaves the default


@pytest.mark.parametrize(("cells", "problem"), [
    ({"measure": "income"}, "measure must be one of spending, withdrawals"),
    ({"threshold": "85000.00"}, "threshold: percent value '85000.00' needs its % sign"),
    ({"basis": "nominal"}, "is a percent, but basis nominal takes dollars"),
    ({"select": "budget:core"}, "select picks expenses, so it applies only to measure spending"),
    ({"start": "soon!"}, "start 'soon!' is not YYYY-MM"),
    ({"sense": "good"}, "sense must be one of negative, positive"),
    ({"threshold": "0%"}, "threshold must be more than zero"),
    ({"portfolio": "Bad:Tag"}, "portfolio: invalid tag"),
])
def test_a_bad_rail_fails_the_load(
    write_guard_rails: Callable[..., Path], load_test_copy: Callable[[], LoadResult],
    cells: dict[str, str], problem: str,
) -> None:
    write_guard_rails({**RAIL, **cells})
    with pytest.raises(LoadError) as caught:
        load_test_copy()
    assert any(problem in issue and "guard_rails.csv line 2 (r)" in issue
               for issue in caught.value.issues), caught.value.issues


@pytest.mark.parametrize(("cells", "problem"), [
    ({"portfolio": "core:invest"}, "portfolio names core:invest, which no asset account carries"),
    ({"measure": "spending", "threshold": "1000.00", "basis": "nominal",
      "select": "-budget:fun"}, "select names budget:fun, which no expense account"),
    ({"start": "retire+1y"}, "start names 'retire', which is no date_marker"),
])
def test_a_rail_naming_what_the_scenario_lacks_fails_the_load(
    write_guard_rails: Callable[..., Path], load_test_copy: Callable[[], LoadResult],
    cells: dict[str, str], problem: str,
) -> None:
    write_guard_rails({**RAIL, **cells})
    with pytest.raises(LoadError) as caught:
        load_test_copy()
    assert any(problem in issue for issue in caught.value.issues), caught.value.issues


def test_a_rail_named_twice_in_one_file_fails_the_load(
    write_guard_rails: Callable[..., Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    """Only a child's row replaces a rail: within one file a repeated name is
    a mistake, not a silent merge."""
    write_guard_rails(RAIL, {**RAIL, "threshold": "5%"})
    with pytest.raises(LoadError) as caught:
        load_test_copy()
    assert any("line 3: rail 'r' is already on line 2" in issue
               for issue in caught.value.issues), caught.value.issues


def test_an_unknown_column_fails_the_load(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    retire_alice_csv("guard_rails.csv").write_text("name,measure,colour\nr,spending,red\n")
    with pytest.raises(LoadError, match="unknown column"):
        load_test_copy()


def test_a_child_overrides_an_inherited_rail_by_name(
    test_child_scenario: Path, test_scenarios_dir: Path,
) -> None:
    (test_child_scenario / "guard_rails.csv").write_text(
        "name,threshold\ngk_upper,7%\n", encoding="utf-8")
    rails = {r.name: r for r in gr.scenario_rails(test_scenarios_dir, "child")}
    assert rails["gk_upper"].threshold_text() == "7%"
    assert rails["gk_upper"].basis == "portfolio"  # the cells the child leaves out are inherited
    assert rails["gk_upper"].defined_in == "child"
    assert rails["budget"].defined_in == "retire_alice"


def test_a_rail_can_start_at_a_date_marker(
    write_guard_rails: Callable[..., Path], append_calendar_row: Callable[..., None],
    run_test_copy: Callable[[], Path], test_scenarios_dir: Path,
) -> None:
    append_calendar_row(name="retire", kind="date_marker", date="2031-06")
    write_guard_rails({**RAIL, "threshold": "1200.00", "basis": "nominal", "start": "retire+1y"})
    with RunConnection(run_test_copy()) as rc:
        rails = gr.run_rails(rc, test_scenarios_dir)
        frame = gr.rail_frame(gr.evaluate_run(rc, rails, DollarMode.NOMINAL)[0])
    # A rail starts with its start month: June to December 2032, then whole years.
    assert frame.index[0] == 2032
    assert frame["threshold"].iloc[:2].tolist() == pytest.approx([700.0, 1200.0])


@pytest.mark.allow_negative_balance  # the prototype's rules don't stop at the deaths
def test_rails_end_at_the_households_last_death(
    write_lifespans: Callable[..., None], run_test_copy: Callable[[], Path],
    test_scenarios_dir: Path,
) -> None:
    """After the second death spending stops; a floor would read every later
    year as past it."""
    write_lifespans("alice,72,,,,,,", "bill,71,,,,,,")
    with RunConnection(run_test_copy()) as rc:
        last = max(d.month for d in rc.deaths())
        results = gr.evaluate_run(rc, gr.run_rails(rc, test_scenarios_dir))
    for result in results:
        assert gr.rail_frame(result).index.max() == last.year


def test_editing_rails_changes_no_scenario_tree(
    commit_test_data: Callable[[], Path], write_guard_rails: Callable[..., Path],
) -> None:
    """Rails are a lens: an edit, committed or not, leaves the scenario's
    tree and cleanliness alone, so nothing reruns."""
    data = commit_test_data()
    before = data_stamps(data, "retire_alice")
    write_guard_rails(RAIL)
    after = data_stamps(data, "retire_alice")
    assert after.tree_id == before.tree_id
    assert not after.dirty


def test_summary_and_margins_frames(prototype_results: dict[str, gr.RailResult]) -> None:
    results = list(prototype_results.values())
    margins = gr.margins_frame(results, carried=True)
    assert {"four_percent", "four_percent cum", "four_percent carried"} <= set(margins.columns)
    summary = gr.summary_frame(results).set_index(["rail", "form"])
    four = summary.loc[("four_percent", "annual")]
    assert four["years_past"] == 6 and four["worst_year"] == 2031
    assert four["rms_over"] > 0
    assert summary.loc[("gk_lower", "cumulative"), "years_past"] == 0
    episodes = gr.rail_episodes(prototype_results["budget"])
    assert [(e.first, e.last) for e in episodes["annual"]] == [(2031, 2034)]
