"""#329: `sim ensemble sensitivity` draws the notebook's sensitivity charts
(``sim/sensitivity_charts.py``), and shows one varying row with --row."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim import sensitivity
from analysis.sim import sensitivity_charts as sc
from analysis.sim.types import DollarMode


def _sensitivity(runs_dir: Path, *args: str):
    return CliRunner().invoke(main, [
        "sim", "ensemble", "sensitivity", "retire_alice", "--runs-dir", str(runs_dir), *args,
    ])


def _titles(fig) -> list[str]:
    return [ax.get_title(loc="left") for ax in fig.axes if ax.get_title(loc="left")]


# --- the figures ------------------------------------------------------------


def test_oat_figure_is_the_heatmap_over_the_tornado(oat_batch: Path) -> None:
    deltas = sensitivity.oat_deltas(oat_batch)
    fig = sc.oat_figure(deltas, 2035, "retire_alice", measure="min_net_worth")
    assert _titles(fig) == ["retire_alice: which parameter matters when",
                            "Effect on lowest net worth so far in 2035"]


@pytest.mark.parametrize(("ladder", "panels"), [(False, 1), (True, 2)])
def test_row_figure_adds_the_response_curve_for_a_ladder(
    oat_batch: Path, ladder: bool, panels: int,
) -> None:
    deltas = sensitivity.oat_deltas(oat_batch)
    fig = sc.row_figure(deltas, "gift", 2035, "retire_alice", relative=True, ladder=ladder)
    assert len(_titles(fig)) == panels
    labels = [str(line.get_label()) for line in fig.axes[0].get_lines()]
    assert {label.split()[0] for label in labels if label.startswith("q=")} == {"q=0.1", "q=0.9"}


def test_correlation_figure_is_r2_over_the_correlations(mc_batch: Path) -> None:
    report = sensitivity.mc_report(mc_batch)
    year = int(report.table["year"].max())
    fig = sc.correlation_figure(report.table, year, 0.05, "retire_alice", by=report.by)
    titles = _titles(fig)
    assert len(titles) == 2 and titles[0].endswith("how much per-parameter effects explain, by year")
    assert str(year) in titles[1]


def test_attribution_figure_returns_its_numbers(oat_batch: Path) -> None:
    tables = sensitivity.oat_attribution(oat_batch, "gift", dollars=DollarMode.NOMINAL)
    _fig, numbers = sc.attribution_figure(tables, "gift", 2030, "retire_alice")
    assert list(numbers.columns) == ["q=0.1", "q=0.9"]
    assert sensitivity.TOTAL in numbers.index


# --- the command ------------------------------------------------------------


@pytest.mark.parametrize("args", [
    (),
    ("--row", "gift"),
    ("--attribute", "gift", "--years", "2030"),
])
def test_chart_file_draws_instead_of_the_tables(
    oat_batch: Path, runs_dir: Path, tmp_path: Path, args: tuple[str, ...],
) -> None:
    chart = tmp_path / "sensitivity.png"
    result = _sensitivity(runs_dir, *args, "--chart-file", str(chart))
    assert result.exit_code == 0, result.output
    assert chart.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert "retire_alice (" not in result.output  # the tables' header line
    both = _sensitivity(runs_dir, *args, "--chart-file", str(tmp_path / "s.svg"), "--table")
    assert both.exit_code == 0, both.output
    assert f"retire_alice ({oat_batch.name})" in both.output


def test_chart_file_on_a_monte_carlo_batch(mc_batch: Path, runs_dir: Path, tmp_path: Path) -> None:
    chart = tmp_path / "correlation.svg"
    result = _sensitivity(runs_dir, "--chart-file", str(chart), "--table")
    assert result.exit_code == 0, result.output
    assert chart.read_text().lstrip().startswith("<?xml")
    assert f"correlation retire_alice ({mc_batch.name})" in result.output


def test_row_prints_its_delta_by_year_and_quantile(oat_batch: Path, runs_dir: Path) -> None:
    result = _sensitivity(runs_dir, "--row", "gift", "--dollars", "nominal")
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == (f"row retire_alice ({oat_batch.name}): gift (amount), "
                        "net_worth vs. the base, nominal $")
    assert lines[1].startswith("  moved to: q=0.1 ") and "q=0.9" in lines[1]
    assert lines[3].split() == ["year", "@0.1", "@0.9"]
    deltas = sensitivity.oat_deltas(oat_batch, DollarMode.NOMINAL)
    years = sorted(deltas["year"].unique())
    assert [int(line.split()[0]) for line in lines[4:]] == years  # every year by default
    expected = deltas[(deltas["row"] == "gift") & (deltas["quantile"] == 0.1)
                      & (deltas["measure"] == "net_worth") & (deltas["year"] == 2030)]["delta"]
    row_2030 = next(line for line in lines if line.startswith("  2030"))
    assert row_2030.split()[1] == f"{float(expected.iloc[0]):+,.0f}"


def test_row_takes_years_relative_and_measure(oat_batch: Path, runs_dir: Path) -> None:
    result = _sensitivity(runs_dir, "--row", "gift", "--years", "2030,2035", "--relative",
                          "--measure", "min_net_worth")
    assert result.exit_code == 0, result.output
    assert "min_net_worth vs. the base, % of base" in result.output
    assert [line.split()[0] for line in result.output.splitlines()[4:]] == ["2030", "2035"]


@pytest.mark.parametrize(("args", "message"), [
    (("--row", "nope"), "'nope' is not a row this batch varied; it varied: "),
    (("--row", "gift", "--attribute", "gift"), "use one of --row and --attribute"),
])
def test_row_refusals(oat_batch: Path, runs_dir: Path, args: tuple[str, ...], message: str) -> None:
    result = _sensitivity(runs_dir, *args)
    assert result.exit_code != 0
    assert message in result.output


def test_row_needs_a_sensitivity_batch(mc_batch: Path, runs_dir: Path) -> None:
    result = _sensitivity(runs_dir, "--row", "gift", "--mode", "monte_carlo")
    assert result.exit_code == 2
    assert "--row needs a sensitivity batch" in result.output


def test_row_completes_over_the_rows_the_batch_varied(
    oat_batch: Path, runs_dir: Path, test_data_dir: Path,
) -> None:
    line = f"ensemble sensitivity retire_alice --runs-dir {runs_dir} --row "
    env = {"_SIM_COMPLETE": "bash_complete", "COMP_WORDS": f"sim {line}",
           "COMP_CWORD": str(len(line.split()) + 1), "ANALYSIS_DATA_DIR": str(test_data_dir)}
    from analysis.sim.cli import sim_group_command

    result = CliRunner().invoke(sim_group_command, [], prog_name="sim", env=env)
    assert result.exit_code == 0, result.output
    offered = [item.split(",", 1)[1] for item in result.output.splitlines() if item]
    assert set(offered) == {"gift", "retire_bill_vary"}
