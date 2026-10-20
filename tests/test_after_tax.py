"""After-tax net worth (#251): the engine's per-year drawdown tax, the
RunConnection frame built on it, the loader's check of its settings, and
the ``--after-tax`` options."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim import compare
from analysis.sim.connection import RunConnection
from analysis.sim.engine import run_scenario
from analysis.sim.loader import LoadError, LoadResult
from analysis.sim.types import DollarMode


def _set_after_tax(retire_alice_csv: Callable[[str], Path], settings: object) -> None:
    path = retire_alice_csv("scenario.csv")
    scenario = pd.read_csv(path, dtype=str, keep_default_na=False)
    scenario["meta_json"] = json.dumps({"after_tax": settings})
    scenario.to_csv(path, index=False)


def _rerun(load_test_copy: Callable[[], LoadResult], runs_dir: Path, name: str) -> RunConnection:
    run = run_scenario(load_test_copy().intent_path, runs_dir / f"{name}.sqlite3", run_by="TEST",
                       allow_dirty_data=True, allow_dirty_code=True)
    return RunConnection(run)


def _summary(rc: RunConnection) -> pd.DataFrame:
    return rc.items("tax_year_summary").pivot_table(
        index="year", columns="bucket", values="amount", aggfunc="sum")


def test_the_engine_records_each_years_drawdown_tax(report_run: RunConnection) -> None:
    summary = _summary(report_run)
    assert (summary["federal_drawdown_base"] > 0).all()
    assert (summary["federal_drawdown_tax"] > 0).all()
    # Ten years' drawdown at that year's rates: well under the whole base.
    assert (summary["federal_drawdown_tax"] < summary["federal_drawdown_base"] * 0.4).all()


def test_after_tax_net_worth_takes_the_deferred_and_unpaid_tax_off(
    report_run: RunConnection,
) -> None:
    frame = report_run.after_tax_net_worth_frame()
    pre = report_run.net_worth_frame()["nominal"]
    expected = pre - frame["deferred_tax"] - frame["unpaid_tax"]
    pd.testing.assert_series_equal(frame["nominal"], expected, check_names=False)
    assert (frame["deferred_tax"] > 0).all()
    pd.testing.assert_series_equal(
        report_run.net_worth_series(DollarMode.CPI_CONSTANT, after_tax=True),
        frame["cpi_constant"])


def test_unpaid_tax_is_owed_from_year_end_until_its_april_payment(
    report_run: RunConnection,
) -> None:
    unpaid = report_run.after_tax_net_worth_frame()["unpaid_tax"]
    due = report_run.outstanding_tax_by_year()[2027]
    assert due > 0
    assert unpaid["2027-11"].iloc[0] == 0
    for month in ("2027-12", "2028-01", "2028-03"):
        assert unpaid[month].iloc[0] == pytest.approx(due)
    assert unpaid["2028-04"].iloc[0] == 0  # the April payment posts that month


def test_a_run_without_tax_rules_has_no_after_tax_deduction(
    retire_alice_run: RunConnection,
) -> None:
    frame = retire_alice_run.after_tax_net_worth_frame()
    assert (frame["deferred_tax"] == 0).all()
    assert (frame["unpaid_tax"] == 0).all()
    pd.testing.assert_series_equal(
        retire_alice_run.net_worth_series(after_tax=True),
        retire_alice_run.net_worth_series(), check_names=False)


def test_drawdown_years_and_step_up_change_the_deferred_tax(
    report_run: RunConnection, retire_alice_csv: Callable[[str], Path],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    ten = _summary(report_run)
    _set_after_tax(retire_alice_csv, {"drawdown_years": 1})
    with _rerun(load_test_copy, runs_dir, "one_year") as rc:
        one = _summary(rc)
    # All of it in one year climbs the brackets: more tax on the same money.
    # Only before Social Security starts, though: over ten years the draws
    # make more of ten years' benefits taxable, against one year's at most.
    pd.testing.assert_series_equal(one["federal_drawdown_base"], ten["federal_drawdown_base"])
    before_ss = ten["federal_ss_income"] == 0
    assert before_ss.sum() >= 3
    assert (one["federal_drawdown_tax"] > ten["federal_drawdown_tax"])[before_ss].all()

    _set_after_tax(retire_alice_csv, {"step_up": True})
    with _rerun(load_test_copy, runs_dir, "step_up") as rc:
        stepped = _summary(rc)
        deferred = rc.after_tax_net_worth_frame()["deferred_tax"]
        rates = rc._drawdown_rate_by_year()
    assert (stepped["federal_drawdown_base"] <= ten["federal_drawdown_base"]).all()
    assert (stepped["federal_drawdown_base"] < ten["federal_drawdown_base"]).any()
    assert (deferred > 0).all()  # the traditional balances are still taxed
    assert (rates > 0).all()


@pytest.mark.parametrize(("settings", "message"), [
    (5, "after_tax must be a JSON object"),
    ({"drawdown_years": 0}, "drawdown_years must be a whole number of years, at least 1"),
    ({"drawdown_years": 2.5}, "drawdown_years must be a whole number"),
    ({"step_up": "yes"}, "step_up must be true or false"),
    ({"years": 5}, "unknown key 'years'"),
])
def test_the_loader_checks_the_after_tax_settings(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult],
    settings: object, message: str,
) -> None:
    _set_after_tax(retire_alice_csv, settings)
    with pytest.raises(LoadError, match=message):
        load_test_copy()


def test_compare_net_worth_after_tax(report_run: RunConnection,
                                     retire_alice_run: RunConnection) -> None:
    connections = {"taxed": report_run, "untaxed": retire_alice_run}
    pre = compare.net_worth_by_month(connections, list(connections))
    post = compare.net_worth_by_month(connections, list(connections), after_tax=True)
    assert (post["taxed"] < pre["taxed"]).all()
    pd.testing.assert_series_equal(post["untaxed"], pre["untaxed"])


def test_sim_networth_is_after_tax_unless_pre_tax(report_run_file: Path) -> None:
    result = CliRunner().invoke(main, [
        "sim", "networth", "--runfile", str(report_run_file), "--dollars", "nominal",
    ])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].startswith("after-tax net worth, nominal dollars")
    assert lines[1].split() == ["year", "net_worth", "deferred_tax", "unpaid_tax", "after_tax"]
    with RunConnection(report_run_file) as rc:
        frame = rc.after_tax_net_worth_frame()
    last = frame["nominal"].iloc[-1]
    assert lines[-1].split()[-1] == f"{last:,.0f}"

    pre = CliRunner().invoke(main, [
        "sim", "networth", "--runfile", str(report_run_file), "--dollars", "nominal",
        "--pre-tax",
    ])
    assert pre.output.splitlines()[0].startswith("net worth, nominal dollars")

