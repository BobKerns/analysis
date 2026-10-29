"""One run's charts (#327): ``sim/run_charts.py`` and ``sim/allocation_charts.py``,
and ``--graph``/``--graph-file`` on ``sim networth``, ``sim report ...``,
``sim account history`` and ``sim allocation`` (whose own tests are in test_allocation.py)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim import reports, run_charts
from analysis.sim.allocation_charts import allocation_drift_figure
from analysis.sim.connection import RunConnection
from analysis.sim.types import DollarMode

BROKERAGE = "Asset_Brokerage_Joint"


def _invoke(*args: str, run_file: Path, data_dir: Path | None = None):
    extra = ["--data-dir", str(data_dir)] if data_dir is not None else []
    return CliRunner().invoke(main, ["sim", *args, "--runfile", str(run_file), *extra],
                              env={"COLUMNS": "250"})


def _texts(fig) -> str:
    return " ".join(t.get_text() for t in fig.texts)


def _legend(ax) -> list[str]:
    legend = ax.get_legend()
    assert legend is not None
    return [t.get_text() for t in legend.get_texts()]


# ---------- the commands ----------


@pytest.mark.parametrize(("command", "table"), [
    (["networth"], "after-tax net worth"),
    (["networth", "--pre-tax", "--dollars", "nominal"], "net worth, nominal"),
    (["report", "income"], "income by category"),
    (["report", "income", "--by", "tag:source"], "income by source"),
    (["report", "income", "--year", "2030"], "income in 2030 by category"),
    (["report", "expenses", "--by", "account"], "expenses by account"),
    (["report", "gains"], "realized vs. unrealized gains"),
    (["report", "tax"], "tax and taxable income"),
    (["report", "tax", "--reconciliation"], "tax reconciliation"),
    (["account", "history", BROKERAGE, "--yearly"], BROKERAGE),
])
def test_graph_file_writes_the_chart_after_the_table(
    report_run_file: Path, tmp_path: Path, command: list[str], table: str,
) -> None:
    chart = tmp_path / "chart.svg"
    result = _invoke(*command, "--graph-file", str(chart), run_file=report_run_file)
    assert result.exit_code == 0, result.output
    assert table in result.output
    assert chart.read_text().lstrip().startswith("<?xml")
    assert "<svg" in chart.read_text()


def test_graph_options_are_checked_before_the_report(report_run_file: Path, tmp_path: Path) -> None:
    both = _invoke("report", "gains", "--graph", "--graph-file", str(tmp_path / "c.png"),
                   run_file=report_run_file)
    assert both.exit_code == 2 and "use one of --graph and --graph-file" in both.output
    gif = _invoke("networth", "--graph-file", str(tmp_path / "c.gif"), run_file=report_run_file)
    assert gif.exit_code == 2 and "must end in .png or .svg" in gif.output
    assert "tax" not in gif.output.splitlines()[0]  # no table before the error


def test_graph_without_a_terminal_image_protocol_writes_a_png(
    report_run_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    result = _invoke("report", "tax", "--graph", run_file=report_run_file)  # no terminal
    assert result.exit_code == 0, result.output
    written = next(tmp_path.glob("tax-*.png"))
    assert f"wrote {written}" in result.output
    assert written.read_bytes().startswith(b"\x89PNG")


@pytest.mark.slow
def test_guard_rails_graph(retire_alice_run_file: Path, data_dir: Path, tmp_path: Path) -> None:
    chart = tmp_path / "rails.png"
    result = CliRunner().invoke(main, [
        "sim", "report", "guard-rails", "--runfile", str(retire_alice_run_file),
        "--data-dir", str(data_dir), "--carried", "--graph-file", str(chart),
    ], env={"COLUMNS": "250"})
    assert result.exit_code == 0, result.output
    assert "guard rail margins" in result.output
    assert chart.read_bytes().startswith(b"\x89PNG")


# ---------- the figures ----------


def test_income_figure_follows_the_story_down_to_net_worth(report_run: RunConnection) -> None:
    frame = reports.flows_by_year(report_run, "income", mode=DollarMode.CPI_CONSTANT)
    fig = run_charts.income_figure(frame, "Income")
    top, bottom = fig.axes
    assert _legend(top) == run_charts.flow_groups(frame)
    assert {"soc security", "investment"} <= set(run_charts.flow_groups(frame))
    lines = [line.get_label() for line in bottom.get_lines()]
    assert {"tax", "Δnet worth"} <= set(lines)
    bars = {c.get_label() for c in bottom.containers}
    assert {"after tax income", "spending", "inflation"} <= bars


def test_tagged_income_has_no_story_panel(report_run: RunConnection) -> None:
    frame = reports.flows_by_year(report_run, "income", by="tag:source")
    fig = run_charts.income_figure(frame, "Income", legend_title="source")
    assert len(fig.axes) == 1
    assert _legend(fig.axes[0]) == ["work"]


def test_categories_keep_their_colors_across_charts(report_run: RunConnection) -> None:
    frame = reports.flows_by_year(report_run, "expenses")
    ax = run_charts.flows_figure(frame, "Expenses").axes[0]
    from matplotlib.colors import to_rgba
    from matplotlib.container import BarContainer

    colors = {c.get_label(): c.patches[0].get_facecolor()
              for c in ax.containers if isinstance(c, BarContainer)}
    assert colors["housing"] == to_rgba(run_charts.CATEGORY_COLORS["housing"])
    assert colors["living"] == to_rgba(run_charts.CATEGORY_COLORS["living"])


def test_tax_figure_puts_the_effective_rate_on_its_own_axis(report_run: RunConnection) -> None:
    frame = reports.tax_by_year(report_run)
    fig = run_charts.tax_figure(frame, "Tax")
    bars, rate = fig.axes
    assert [c.get_label() for c in bars.containers] == ["federal"]
    (line,) = rate.get_lines()
    assert line.get_label() == "effective rate"
    assert rate.get_ylim()[1] < 1  # a rate, not dollars


def test_one_year_figure_draws_a_bar_per_group(report_run: RunConnection) -> None:
    frame = reports.flows_for_year(report_run, "expenses", 2030)
    fig = run_charts.one_year_figure(frame, "Expenses in 2030")
    (bars,) = fig.axes[0].containers
    assert len(bars) == int((frame.drop(index="total")["amount"] != 0).sum())


def test_lot_history_draws_its_cost_basis(two_lot_run: RunConnection) -> None:
    frame = reports.account_history(two_lot_run, BROKERAGE, lot="b", yearly=True)
    fig = run_charts.account_history_figure(frame, "lot b")
    assert [line.get_label() for line in fig.axes[0].get_lines()] == ["balance", "cost basis"]


def test_net_worth_figure_draws_the_first_line_solid() -> None:
    months = pd.date_range("2030-01-31", periods=3, freq="ME")
    lines = pd.DataFrame({"after tax": [1.0, 2.0, 3.0], "pre-tax": [2.0, 3.0, 4.0]}, index=months)
    first, second = run_charts.net_worth_figure(lines, "Net worth").axes[0].get_lines()
    assert (first.get_linestyle(), second.get_linestyle()) == ("-", "--")
    assert run_charts.net_worth_figure(lines, "t").axes[0].get_ylim()[0] == 0


@pytest.mark.parametrize("figure", [
    lambda: run_charts.flows_figure(pd.DataFrame(), "t"),
    lambda: run_charts.gains_figure(pd.DataFrame(), "t"),
    lambda: run_charts.tax_figure(pd.DataFrame(), "t"),
    lambda: run_charts.economic_income_figure(pd.DataFrame(), "t"),
    lambda: run_charts.account_history_figure(pd.DataFrame(), "t"),
    lambda: allocation_drift_figure(pd.DataFrame(), "t"),
])
def test_an_empty_frame_says_so(figure) -> None:
    fig = figure()
    assert not fig.axes and "No " in _texts(fig)


def test_drift_figure_draws_a_line_per_class() -> None:
    frame = pd.DataFrame({"bond": [1.0, -2.0], "stock_us": [-1.0, 2.0]},
                         index=pd.Index(["2030-12-31", "2031-12-31"], name="month"))
    ax = allocation_drift_figure(frame, "Drift").axes[0]
    assert [line.get_label() for line in ax.get_lines()][:2] == ["bond", "stock_us"]
