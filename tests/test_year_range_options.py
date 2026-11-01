"""The chart and report options the notebooks' controls have (#331):
``--start``/``--end`` (the Years slider, #245) on the run, compare and
ensemble reports, ``--dollars`` on ``sim ensemble summary``, and ``--log``
(the Scale toggle) on the ensemble charts."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.frames import row_years


def _sim(*args: str) -> Result:
    return CliRunner().invoke(main, ["sim", *args])


def _first_column(output: str, header: str) -> list[str]:
    """The first cell of each row under the line starting with ``header``,
    up to a blank line or the next unindented line."""
    lines = output.splitlines()
    start = next(i for i, line in enumerate(lines) if line.split()[:1] == [header]) + 1
    rows = []
    for line in lines[start:]:
        if not line.strip() or not line.startswith(" "):
            break
        rows.append(line.split()[0])
    return rows


def _report_years(output: str) -> list[str]:
    """The year column of a report printed as a rich table."""
    return [line.split("│")[1].strip() for line in output.splitlines()
            if line.startswith("│") and line.split("│")[1].strip().isdigit()]


def test_row_years_reads_months_and_years() -> None:
    months = pd.DataFrame({"v": [1, 2]}, index=pd.to_datetime(["2030-01-31", "2031-12-31"]))
    assert list(row_years(months)) == [2030, 2031]
    assert list(row_years(pd.Series([1, 2], index=["2030-01", "2031-06"]))) == [2030, 2031]
    assert list(row_years(pd.Series([1, 2], index=[2030, 2031]))) == [2030, 2031]


# ---------- one run: networth, reports, account history ----------


@pytest.mark.parametrize("pre_tax", [False, True])
def test_sim_networth_shows_only_the_years_asked_for(
    report_run_file: Path, pre_tax: bool,
) -> None:
    result = _sim("networth", *(["--pre-tax"] if pre_tax else []),
                  "--start", "2030", "--end", "2032", "--runfile", str(report_run_file))
    assert result.exit_code == 0, result.output
    rows = [line.split()[0] for line in result.output.splitlines()[1:]]
    assert [r for r in rows if r.isdigit()] == ["2030", "2031", "2032"]


def test_sim_networth_monthly_with_one_year(report_run_file: Path) -> None:
    result = _sim("networth", "--monthly", "--start", "2031", "--end", "2031",
                  "--runfile", str(report_run_file))
    assert result.exit_code == 0, result.output
    assert _first_column(result.output, "month") == [f"2031-{m:02d}" for m in range(1, 13)]


def test_sim_networth_chart_draws_only_the_years_asked_for(
    report_run_file: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from analysis.sim import run_charts

    drawn: list[pd.DataFrame] = []
    real = run_charts.net_worth_figure

    def spy(lines: pd.DataFrame, *args, **kwargs):
        drawn.append(lines)
        return real(lines, *args, **kwargs)

    monkeypatch.setattr(run_charts, "net_worth_figure", spy)
    result = _sim("networth", "--end", "2028", "--chart-file", str(tmp_path / "nw.png"),
                  "--runfile", str(report_run_file))
    assert result.exit_code == 0, result.output
    (lines,) = drawn
    assert max(row_years(lines)) == 2028


@pytest.mark.parametrize("args, message", [
    (["--start", "2200"], "nothing to show from 2200 on: the run covers"),
    (["--end", "1900"], "nothing to show up to 1900"),
    (["--start", "2033", "--end", "2030"], "--end must be on or after --start"),
])
def test_sim_networth_refuses_a_range_with_nothing_in_it(
    report_run_file: Path, args: list[str], message: str,
) -> None:
    result = _sim("networth", *args, "--runfile", str(report_run_file))
    assert result.exit_code != 0
    assert message in result.output


@pytest.mark.parametrize("report", [["income"], ["expenses"], ["gains"], ["tax"],
                                    ["tax", "--reconciliation"]])
def test_sim_report_shows_only_the_years_asked_for(
    report_run_file: Path, report: list[str],
) -> None:
    result = _sim("report", *report, "--start", "2030", "--end", "2031",
                  "--runfile", str(report_run_file))
    assert result.exit_code == 0, result.output
    assert _report_years(result.output) == ["2030", "2031"]


def test_sim_report_income_refuses_a_year_with_a_range(report_run_file: Path) -> None:
    result = _sim("report", "income", "--year", "2030", "--start", "2030",
                  "--runfile", str(report_run_file))
    assert result.exit_code == 2
    assert "--year shows one year" in result.output


def test_sim_account_history_takes_a_bare_year(report_run_file: Path) -> None:
    result = _sim("account", "history", "Asset_Checking", "--start", "2030", "--end", "2031",
                  "--runfile", str(report_run_file))
    assert result.exit_code == 0, result.output
    months = [line.split("│")[1].strip() for line in result.output.splitlines()
              if line.startswith("│")]
    assert months[0] == "2030-01" and months[-1] == "2031-12"


# ---------- ensembles: summary and compare ----------


def test_sim_ensemble_summary_takes_dollars_and_years(
    seeded_ensemble_batch: Callable[..., Path], runs_dir: Path,
) -> None:
    seeded_ensemble_batch("baseline", 1)
    base = ["ensemble", "summary", "baseline", "--runs-dir", str(runs_dir)]
    cpi = _sim(*base)
    nominal = _sim(*base, "--dollars", "nominal", "--start", "2030", "--end", "2031")
    assert cpi.exit_code == 0, cpi.output
    assert nominal.exit_code == 0, nominal.output
    assert "failed; CPI-constant dollars" in cpi.stdout
    assert "failed; nominal dollars" in nominal.stdout
    assert _first_column(nominal.stdout, "year") == ["2030", "2031"]
    assert len(_first_column(cpi.stdout, "year")) > 2
    # Nominal mean in a later year is higher than the same in today's dollars.
    def mean(output: str, year: str) -> float:
        row = next(line for line in output.splitlines() if line.split()[:1] == [year])
        return float(row.split()[1].replace(",", ""))
    assert mean(nominal.stdout, "2031") > mean(cpi.stdout, "2031")


@pytest.mark.slow
def test_sim_ensemble_compare_shows_only_the_years_asked_for(
    seeded_ensemble_batch: Callable[..., Path], runs_dir: Path,
) -> None:
    seeded_ensemble_batch("baseline", 1)
    seeded_ensemble_batch("child", 1)
    result = _sim("ensemble", "compare", "baseline", "child", "--runs-dir", str(runs_dir),
                  "--reserve", "100000", "--start", "2029", "--end", "2030")
    assert result.exit_code == 0, result.output
    assert _first_column(result.output, "year") == ["2029", "2030"]
    tables = [line for line in result.output.splitlines() if line.split()[:1] == ["year"]]
    assert len(tables) == 3  # the deltas, then each recipe's tail
    years = [line.split()[0] for line in result.output.splitlines()
             if line.startswith("  ") and line.split()[0].isdigit()]
    assert set(years) == {"2029", "2030"}


@pytest.mark.parametrize("command", ["summary", "compare"])
def test_sim_ensemble_log_needs_a_chart(
    seeded_ensemble_batch: Callable[..., Path], runs_dir: Path, command: str,
) -> None:
    names = ["baseline", "child"] if command == "compare" else ["baseline"]
    result = _sim("ensemble", command, *names, "--runs-dir", str(runs_dir), "--log")
    assert result.exit_code == 2
    assert "--log needs --chart or --chart-file" in result.output


@pytest.mark.slow
def test_sim_ensemble_summary_log_chart_of_the_years_asked_for(
    seeded_ensemble_batch: Callable[..., Path], runs_dir: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from analysis.sim import ensemble_plots

    figures: list = []
    shown: list[pd.DataFrame] = []
    real = ensemble_plots.ensemble_fan_figure

    def spy(summary: pd.DataFrame, *args, **kwargs):
        shown.append(summary)
        figures.append(real(summary, *args, **kwargs))
        return figures[-1]

    monkeypatch.setattr(ensemble_plots, "ensemble_fan_figure", spy)
    seeded_ensemble_batch("baseline", 1)
    result = _sim("ensemble", "summary", "baseline", "--runs-dir", str(runs_dir),
                  "--start", "2028", "--end", "2030", "--log",
                  "--chart-file", str(tmp_path / "fan.png"))
    assert result.exit_code == 0, result.output
    (figure,), (summary,) = figures, shown
    assert figure.axes[0].get_yscale() == "log"
    assert sorted(set(row_years(summary))) == [2028, 2029, 2030]


@pytest.mark.slow
def test_sim_ensemble_compare_log_chart_uses_symmetric_log(
    seeded_ensemble_batch: Callable[..., Path], runs_dir: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from analysis.sim import ensemble_plots

    figures: list = []
    real = ensemble_plots.ensemble_compare_figure

    def spy(*args, **kwargs):
        figures.append(real(*args, **kwargs))
        return figures[-1]

    monkeypatch.setattr(ensemble_plots, "ensemble_compare_figure", spy)
    seeded_ensemble_batch("baseline", 1)
    seeded_ensemble_batch("child", 1)
    result = _sim("ensemble", "compare", "baseline", "child", "--runs-dir", str(runs_dir),
                  "--end", "2030", "--log", "--chart-file", str(tmp_path / "compare.png"))
    assert result.exit_code == 0, result.output
    (figure,) = figures
    assert {ax.get_yscale() for ax in figure.axes} == {"symlog"}
    # The zero rule's x data is the axes' [0, 1]; the delta lines are by month.
    ends = [pd.Timestamp(line.get_xdata()[-1]) for ax in figure.axes for line in ax.get_lines()
            if len(line.get_xdata()) > 2]
    assert ends and max(ends).year == 2030


def test_a_fan_reaching_zero_is_drawn_linear_with_a_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from analysis.sim.cli import _fan_can_log
    from analysis.sim.ensemble import summary_with_percentiles

    index = pd.to_datetime(["2030-01-31", "2030-02-28"])
    members = pd.DataFrame({"m0": [10.0, -5.0], "m1": [20.0, 30.0]}, index=index)
    assert not _fan_can_log(summary_with_percentiles(members), "net worth")
    assert "can't show net worth that reaches zero or below" in capsys.readouterr().err
    assert _fan_can_log(summary_with_percentiles(members.abs() + 1), "net worth")


# ---------- sim compare ----------


@pytest.mark.slow
def test_sim_compare_shows_only_the_years_asked_for(
    test_data_dir: Path, intent_dir: Path, runs_dir: Path,
    derive_scenario: Callable[..., Path], scheduled_override: Callable[..., str],
) -> None:
    derive_scenario("higher_pay", "retire_alice",
                    {"scheduled.csv": scheduled_override(salary_alice="9000.00")})
    for name in ("retire_alice", "higher_pay"):
        ran = _sim("run", name, "--data-dir", str(test_data_dir), "--intent-dir",
                   str(intent_dir), "--runs-dir", str(runs_dir), "--allow-dirty-data")
        assert ran.exit_code == 0, ran.output
    where = ["--data-dir", str(test_data_dir), "--runs-dir", str(runs_dir)]
    whole = _sim("compare", "retire_alice", "higher_pay", "--section", "break-even", *where)
    result = _sim("compare", "retire_alice", "higher_pay", "--section", "net-worth",
                  "--section", "break-even", "--start", "2029", "--end", "2030", *where)
    assert result.exit_code == 0, result.output
    assert _first_column(result.output, "year") == ["2029", "2030"]
    # Break-even is a whole-run result: the range doesn't change it.
    assert whole.output.splitlines()[-2:] == result.output.splitlines()[-2:]


def test_sim_report_guard_rails_shows_the_years_and_refuses_none(
    retire_alice_run_file: Path, data_dir: Path,
) -> None:
    where = ["--runfile", str(retire_alice_run_file), "--data-dir", str(data_dir)]
    result = _sim("report", "guard-rails", "--start", "2032", "--end", "2033", *where)
    assert result.exit_code == 0, result.output
    assert _report_years(result.output.split("\n\n")[0]) == ["2032", "2033"]
    outside = _sim("report", "guard-rails", "--start", "2200", *where)
    assert outside.exit_code == 1
    assert "nothing to show from 2200 on: the run covers" in outside.output


@pytest.mark.slow
def test_sim_compare_refuses_a_range_outside_the_runs_whatever_the_section(
    test_data_dir: Path, intent_dir: Path, runs_dir: Path,
    derive_scenario: Callable[..., Path],
) -> None:
    derive_scenario("same", "retire_alice", {})
    for name in ("retire_alice", "same"):
        ran = _sim("run", name, "--data-dir", str(test_data_dir), "--intent-dir",
                   str(intent_dir), "--runs-dir", str(runs_dir), "--allow-dirty-data")
        assert ran.exit_code == 0, ran.output
    result = _sim("compare", "retire_alice", "same", "--section", "strategies",
                  "--start", "2200", "--data-dir", str(test_data_dir), "--runs-dir", str(runs_dir))
    assert result.exit_code == 1, result.output
    assert "nothing to show from 2200 on" in result.output
