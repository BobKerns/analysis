"""Spending against the plan across ensembles, in compare and on charts (#408)."""

from __future__ import annotations

import csv
import json
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim.connection import RunConnection
from analysis.sim.reports import ensemble_spending_summary, ensemble_spending_table, spending_by_year
from analysis.sim.series_db import open_series
from analysis.sim.types import DollarMode

SCENARIO = "alice_spending"


def _strategy(strategy_csv: Callable[[str, str], Path], kind: str, **params: object) -> None:
    """Make alice_spending's ``flex`` strategy ``kind`` with ``params``."""
    path = strategy_csv(SCENARIO, "strategies.csv")
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    rows[0].update(kind=kind, params_json=json.dumps({"portfolio": "portfolio:invest", **params}))
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def cut_batch(strategy_csv: Callable[[str, str], Path], strategy_ensemble: Callable[[str], Path]) -> Path:
    """alice_spending's members with a threshold too high to meet after the
    first years, so every member skips its travel and gifts some years."""
    _strategy(strategy_csv, "threshold", minimum="700000.00")
    return strategy_ensemble(SCENARIO)


def test_the_store_keeps_each_members_spending(cut_batch: Path) -> None:
    store = open_series(cut_batch)
    delivered, budgeted = store.spending(DollarMode.CPI_CONSTANT)
    assert len(delivered.columns) == 3
    for stem in delivered.columns:
        rc = RunConnection(cut_batch / f"{stem}.sqlite3")
        try:
            want_d, want_b = spending_by_year(rc)
        finally:
            rc.close()
        pd.testing.assert_series_equal(delivered[stem], want_d.sum(axis=1), check_names=False, atol=0.01)
        pd.testing.assert_series_equal(budgeted[stem], want_b.sum(axis=1), check_names=False, atol=0.01)
    assert (budgeted - delivered).max().max() > 0
    other_d, other_b = store.spending(DollarMode.NOMINAL, "other")
    assert (other_d <= other_b + 0.01).all().all() and (other_d < other_b).any().any()


def test_ensemble_spending_table_and_summary() -> None:
    delivered = pd.DataFrame({"a": [100.0, 80.0], "b": [100.0, 100.0], "c": [50.0, 100.0]},
                             index=pd.Index([2030, 2031], name="year"))
    budgeted = pd.DataFrame(100.0, index=delivered.index, columns=delivered.columns)
    table = ensemble_spending_table(delivered, budgeted)
    assert table.loc[2030, "median"] == 100 and table.loc[2030, "cvar05"] == 50
    assert table.loc[2031, "cut"] == pytest.approx(1 / 3) and table.loc[2030, "of_plan"] == 1
    summary = ensemble_spending_summary(delivered, budgeted)
    assert summary["worst_year_median"] == 80 and summary["worst_year_cvar05"] == 50
    assert summary["members_ever_cut"] == pytest.approx(2 / 3)
    assert summary["years_cut_mean"] == pytest.approx(1 / 3)
    assert summary["of_plan_worst"] == pytest.approx(0.75)


def test_sim_ensemble_spending(cut_batch: Path, tmp_path: Path) -> None:
    graph = tmp_path / "spending.png"
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "spending", SCENARIO, "--runs-dir", str(cut_batch.parent.parent),
        "--graph-file", str(graph)], terminal_width=200)
    assert result.exit_code == 0, result.output
    assert "3 member(s)" in result.output and "cvar05" in result.output
    assert "members ever cut 100%" in result.output
    assert graph.stat().st_size > 0


def _cli(data_dir: Path, *args: str):
    return CliRunner().invoke(main, ["sim", *args, "--data-dir", str(data_dir)], terminal_width=200)


def test_compare_sets_spending_against_the_plan(
    strategy_scenarios: Path,
) -> None:
    """A strategy that skips spending in a variant shows as less delivered,
    and below plan, beside the baseline that kept to it."""
    child = strategy_scenarios / "scenarios" / "alice_spending_skip"
    child.mkdir()
    (child / "scenario.csv").write_text(
        "name,description,start,end,seed,parent_scenario,created_by,meta_json\n"
        "alice_spending_skip,,2026-05-31,2040-12-31,42,alice_spending,alice,{}\n")
    (child / "README.md").write_text("# alice_spending_skip\n\nBuilds on: alice_spending\n")
    (child / "strategies.csv").write_text(
        "name,kind,chain,rules,start,end,enabled,params_json,description\n"
        'flex,threshold,,flex:optional,,,,"{""portfolio"":""portfolio:invest"",'
        '""minimum"":""5000000.00""}",never enough\n')
    for name in (SCENARIO, "alice_spending_skip"):
        result = _cli(strategy_scenarios, "run", name, "--allow-dirty-data", "--allow-dirty-code")
        assert result.exit_code == 0, result.output
    result = _cli(strategy_scenarios, "compare", SCENARIO, "alice_spending_skip",
                  "--section", "spending")
    assert result.exit_code == 0, result.output
    by_year, overall = result.output.split("Spending delivered by year", 1)[1].split(
        "Spending against each scenario's plan", 1)
    assert "Δ alice_spending_skip" in by_year and "-$29,938" in by_year
    rows = {line.split()[0]: line.split() for line in overall.splitlines()[2:] if line.strip()}
    assert rows[SCENARIO][3] == "100.0%" and rows["alice_spending_skip"][3] == "74.0%"
    assert rows["alice_spending_skip"][5] != "2026"  # a whole year, not the part year 2026
    result = _cli(strategy_scenarios, "compare", SCENARIO, "alice_spending_skip", "--section", "runs")
    assert "Spending" not in result.output


# ---------- notebook widgets ----------


def test_the_spending_widget_draws_a_run(
    strategy_csv: Callable[[str, str], Path], run_strategy_scenario: Callable[[str], Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    from analysis.sim.notebook_ui.networth_plots import spending_widget

    _strategy(strategy_csv, "threshold", minimum="5000000.00")
    rc = RunConnection(run_strategy_scenario(SCENARIO))
    try:
        box = spending_widget(rc)
        by = box.children[0].children[0]
        assert "tag:flex" in by.options
        by.value = "tag:flex"
        # Outside a kernel, what the widget displays is printed (as each
        # object's repr): a chart, a table and the summary each time it draws.
        printed = capsys.readouterr().out
        assert printed.count("Image object") == 2, printed
        assert printed.count("StyledMarkdown object") == 2, printed  # the summary under each table
    finally:
        rc.close()


def test_the_ensemble_spending_widget_draws_a_batch(
    cut_batch: Path, strategy_scenarios: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from analysis.sim.notebook_ui.ensemble_widgets import ensemble_spending_widget

    box = ensemble_spending_widget(data_dir=strategy_scenarios, runs_dir=cut_batch.parent.parent,
                                   default=SCENARIO)
    category = box.children[1].children[0]
    assert "other" in category.options
    category.value = "other"
    printed = capsys.readouterr().out
    assert printed.count("Image object") == 2, printed
    assert printed.count("Styler object") == 2, printed


def test_the_spending_compare_widget_sets_runs_side_by_side(
    strategy_csv: Callable[[str, str], Path], run_strategy_scenario: Callable[[str], Path],
    runs_dir: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from analysis.sim.notebook_ui.compare_plots import spending_compare_widget
    from analysis.sim.notebook_ui.pickers import MultiScenarioConnections

    kept = run_strategy_scenario(SCENARIO)
    (runs_dir / "kept.sqlite3").write_bytes(kept.read_bytes())
    kept.unlink()
    _strategy(strategy_csv, "threshold", minimum="5000000.00")
    skipped = run_strategy_scenario(SCENARIO)
    connections = {"kept": RunConnection(runs_dir / "kept.sqlite3"), "skipped": RunConnection(skipped)}
    try:
        container = MultiScenarioConnections(connections, "kept", ["skipped"], {})
        spending_compare_widget(container)
        printed = capsys.readouterr().out
        assert printed.count("Image object") == 1, printed
        assert printed.count("Styler object") == 2, printed  # by year, then over the run
    finally:
        for rc in connections.values():
            rc.close()
