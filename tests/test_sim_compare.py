"""``sim compare`` (#188): plain scenario runs compared from the terminal,
as scenario_compare.ipynb compares them."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from analysis.cli import main


@pytest.fixture
def compare_runs(
    test_data_dir: Path, intent_dir: Path, runs_dir: Path,
    derive_scenario: Callable[..., Path], scheduled_override: Callable[..., str],
) -> Callable[..., Result]:
    """retire_alice and ``higher_pay``, which raises Alice's salary, each
    run; returns a runner for ``sim compare`` over them."""
    derive_scenario("higher_pay", "retire_alice",
                    {"scheduled.csv": scheduled_override(salary_alice="9000.00")})
    for name in ("retire_alice", "higher_pay"):
        _invoke("run", name, "--data-dir", str(test_data_dir), "--intent-dir", str(intent_dir),
                "--runs-dir", str(runs_dir), "--allow-dirty-data")

    def _compare(*args: str, input: str | None = None) -> Result:
        return CliRunner().invoke(main, [
            "sim", "compare", *args, "--data-dir", str(test_data_dir), "--runs-dir", str(runs_dir),
        ], input=input)
    return _compare


def _invoke(*args: str) -> None:
    result = CliRunner().invoke(main, ["sim", *args])
    assert result.exit_code == 0, result.output


def _section(output: str, heading: str) -> list[str]:
    """The lines under ``heading``, up to the next blank line."""
    lines = output.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(heading)) + 1
    end = next((i for i in range(start, len(lines)) if not lines[i]), len(lines))
    return lines[start:end]


@pytest.mark.slow
def test_sim_compare_prints_every_section(compare_runs: Callable[..., Result]) -> None:
    result = compare_runs("retire_alice", "higher_pay")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == "higher_pay vs retire_alice, CPI-constant dollars"
    runs = _section(result.output, "Runs:")
    assert runs[0].split() == ["scenario", "status", "finished", "code", "data"]
    assert [line.split()[0] for line in runs[1:3]] == ["retire_alice", "higher_pay"]
    rules = _section(result.output, "Rules that differ")
    assert rules[0].split() == ["rule", "field", "retire_alice", "higher_pay"]
    assert rules[1].split() == ["salary_alice", "amount", "$8,000.00", "$9,000.00"]
    assert _section(result.output, "Calendar events that differ:") == ["  none"]
    assert _section(result.output, "Accounts and lots defined differently:") == ["  none"]
    net_worth = _section(result.output, "Net worth at year end (CPI-constant):")
    assert net_worth[0].split() == ["year", "retire_alice", "higher_pay", "Δ", "higher_pay"]
    assert all(not line.split()[-1].startswith("-") for line in net_worth[1:])
    breakeven = _section(result.output, "Break-even vs retire_alice")
    assert breakeven[0].split()[:2] == ["variant", "break-even"]
    assert breakeven[1].split()[:2] == ["higher_pay", "2026-05"]
    assert "Asset_Checking" not in result.output  # no account section without --account


@pytest.mark.slow
def test_sim_compare_sections_account_and_graph_file(
    compare_runs: Callable[..., Result], tmp_path: Path,
) -> None:
    chart = tmp_path / "checking.svg"
    result = compare_runs("retire_alice", "higher_pay", "--section", "account",
                          "--account", "Asset_Checking", "--dollars", "nominal",
                          "--graph-file", str(chart))
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "higher_pay vs retire_alice, nominal dollars"
    assert lines[2] == "Asset_Checking at year end (nominal):"
    assert "Runs:" not in result.output and "Net worth" not in result.output
    assert lines[-1] == f"wrote {chart}"
    assert "Asset_Checking, nominal" in chart.read_text()


@pytest.mark.slow
def test_sim_compare_asks_for_a_variant_with_a_run(
    compare_runs: Callable[..., Result], derive_scenario: Callable[..., Path],
) -> None:
    derive_scenario("never_run", "retire_alice")
    result = CliRunner().invoke(main, ["sim", "compare", "--help"])
    assert "BASELINE" in result.output
    result = compare_runs("retire_alice", "--section", "break-even", input="higher_pay\n")
    assert result.exit_code == 0, result.output
    assert "scenarios with runs: higher_pay" in result.output  # not never_run or base
    assert "higher_pay vs retire_alice" in result.output


@pytest.mark.parametrize("args, message", [
    (["--lot", "a"], "--lot needs --account"),
    (["--section", "account"], "--section account needs --account"),
    (["--graph", "--graph-file", "x.png"], "use one of --graph and --graph-file"),
    (["--graph-file", "x.gif"], "must end in .png or .svg"),
])
def test_sim_compare_checks_its_options_first(
    test_data_dir: Path, args: list[str], message: str,
) -> None:
    result = CliRunner().invoke(main, [
        "sim", "compare", "retire_alice", "base", *args, "--data-dir", str(test_data_dir),
    ])
    assert result.exit_code == 2 and message in result.output, result.output


@pytest.mark.slow
def test_sim_compare_refuses_a_scenario_without_a_run_or_named_twice(
    compare_runs: Callable[..., Result],
) -> None:
    result = compare_runs("retire_alice", "base")
    assert result.exit_code == 1
    assert "no runs for 'base'" in result.output and "sim run base" in result.output
    twice = compare_runs("retire_alice", "higher_pay", "retire_alice")
    assert twice.exit_code == 2 and "name each scenario once" in twice.output


@pytest.mark.slow
def test_sim_compare_graph_draws_at_most_four_variants(
    compare_runs: Callable[..., Result], derive_scenario: Callable[..., Path],
) -> None:
    for i in range(4):
        derive_scenario(f"v{i}", "retire_alice")
    result = compare_runs("retire_alice", "higher_pay", "v0", "v1", "v2", "v3", "--graph")
    assert result.exit_code == 2 and "at most 4 variants" in result.output


@pytest.mark.slow
def test_sim_compare_treats_an_empty_scenario_as_zero(
    compare_runs: Callable[..., Result], test_data_dir: Path, intent_dir: Path, runs_dir: Path,
    derive_scenario: Callable[..., Path],
) -> None:
    """#174: a scenario with no accounts is a valid compare point."""
    derive_scenario("empty", "base")
    _invoke("run", "empty", "--data-dir", str(test_data_dir), "--intent-dir", str(intent_dir),
            "--runs-dir", str(runs_dir), "--allow-dirty-data")
    result = compare_runs("empty", "retire_alice", "--section", "net-worth",
                          "--dollars", "nominal")
    assert result.exit_code == 0, result.output
    rows = _section(result.output, "Net worth at year end (nominal):")[1:]
    assert rows and all(row.split()[1] == "0" for row in rows)


@pytest.mark.slow
def test_sim_compare_an_ensemble_on_member_0(
    ensemble_recipe: str, test_data_dir: Path, intent_dir: Path, runs_dir: Path,
) -> None:
    _invoke("run", "retire_alice", "--data-dir", str(test_data_dir), "--intent-dir",
            str(intent_dir), "--runs-dir", str(runs_dir), "--allow-dirty-data")
    result = CliRunner().invoke(main, [
        "sim", "compare", "retire_alice", ensemble_recipe, "--section", "runs",
        "--data-dir", str(test_data_dir), "--runs-dir", str(runs_dir),
    ])
    assert result.exit_code == 0, result.output
    assert _section(result.output, "Runs:")[2].split()[:2] == [ensemble_recipe, "succeeded"]


def test_comparison_figure_draws_values_over_each_variants_difference() -> None:
    import numpy as np
    import pandas as pd

    from analysis.sim.compare_charts import comparison_figure

    months = pd.date_range("2027-01-31", periods=3, freq="ME")
    values = pd.DataFrame({"a": [1.0, 2.0, 3.0], "b": [1.0, 3.0, 5.0]}, index=months)
    figure = comparison_figure(values, "a", ["b"], "Net worth, nominal")
    top, bottom = figure.axes
    assert top.get_title() == "Net worth, nominal"
    assert [line.get_label() for line in top.lines] == ["a", "b"]
    (delta, _zero) = bottom.lines
    assert np.asarray(delta.get_ydata()).tolist() == [0.0, 1.0, 2.0]
    assert bottom.get_ylabel() == "difference vs a"


def test_account_completion_offers_every_compared_scenarios_accounts(
    test_data_dir: Path, derive_scenario: Callable[..., Path], monkeypatch: pytest.MonkeyPatch,
) -> None:

    derive_scenario("extra", "retire_alice", {
        "accounts.csv": (test_data_dir / "scenarios" / "retire_alice" / "accounts.csv")
        .read_text().splitlines()[0] + "\nAsset_Extra_Only,asset,cash,joint,taxable,,,,,{},\n"})
    monkeypatch.setenv("ANALYSIS_DATA_DIR", str(test_data_dir))
    from analysis.sim.cli import compare_command as command

    ctx = command.make_context("compare", ["retire_alice", "extra"], resilient_parsing=True)
    account = next(p for p in command.params if p.name == "account")
    offered = [item.value for item in account.type.shell_complete(ctx, account, "Asset_")]
    assert "Asset_Extra_Only" in offered and "Asset_Checking" in offered


@pytest.mark.slow
def test_sim_compare_two_empty_scenarios_and_a_zero_account(
    compare_runs: Callable[..., Result], test_data_dir: Path, intent_dir: Path, runs_dir: Path,
    derive_scenario: Callable[..., Path],
) -> None:
    """Two scenarios with no months break even "never" rather than crash, and
    an account that stays at zero is compared, not called missing."""
    for name in ("empty", "empty_too"):
        derive_scenario(name, "base")
        _invoke("run", name, "--data-dir", str(test_data_dir), "--intent-dir",
                str(intent_dir), "--runs-dir", str(runs_dir), "--allow-dirty-data")
    result = compare_runs("empty", "empty_too", "--dollars", "nominal")
    assert result.exit_code == 0, result.output
    assert _section(result.output, "Break-even vs empty")[1].split()[:2] == ["empty_too", "never"]

    missing = compare_runs("retire_alice", "higher_pay", "--section", "account",
                           "--account", "No_Such_Account")
    assert missing.exit_code == 1 and "no No_Such_Account" in missing.output
    # --section leaves out the account section, so --account goes unchecked.
    runs_only = compare_runs("retire_alice", "higher_pay", "--section", "runs",
                             "--account", "No_Such_Account")
    assert runs_only.exit_code == 0, runs_only.output


@pytest.mark.slow
def test_sim_compare_refuses_a_cpi_constant_balance_without_a_cpi_index(
    compare_runs: Callable[..., Result], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run with no CPI index has an all-NaN CPI-constant balance: an error
    naming the cause, not a balance of zero."""
    import pandas as pd

    from analysis.sim.connection import RunConnection
    from analysis.sim.types import DollarMode

    original = RunConnection.account_balance

    def no_cpi(self: RunConnection, account: str, mode: DollarMode = DollarMode.NOMINAL) -> pd.Series:
        balance = original(self, account)
        return balance * float("nan") if mode == DollarMode.CPI_CONSTANT else balance
    monkeypatch.setattr(RunConnection, "account_balance", no_cpi)
    result = compare_runs("retire_alice", "higher_pay", "--section", "account",
                          "--account", "Asset_Checking")
    assert result.exit_code == 1 and "no 'cpi' inflation index" in result.output


def test_balance_by_month_keeps_an_all_nan_balance_and_zero_fills_a_missing_one() -> None:
    import pandas as pd

    from analysis.sim.compare import balance_by_month

    months = pd.period_range("2030-01", periods=2, freq="M").to_timestamp()

    class Run:
        def __init__(self, balance: pd.Series) -> None:
            self.balance = balance

        def account_balance(self, account: str, mode: object) -> pd.Series:
            return self.balance

    connections = {
        "has": Run(pd.Series([1.0, 2.0], index=months)),
        "lacks": Run(pd.Series([], dtype=float)),
        "no_cpi": Run(pd.Series([float("nan")] * 2, index=months)),
    }
    frame = balance_by_month(connections, list(connections), "X")  # ty: ignore[invalid-argument-type]  # duck-typed runs
    assert (frame["lacks"] == 0).all()
    assert frame["no_cpi"].isna().all()


def test_breakeven_of_no_months_is_none() -> None:
    import pandas as pd

    from analysis.sim.compare import breakeven_month

    assert breakeven_month(pd.Series([], dtype=float)) is None
