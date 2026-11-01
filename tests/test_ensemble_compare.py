"""#163: comparing ensembles on risk -- tail means (CVaR), reserve shares,
percentile-breakpoint deltas and paired per-future deltas, and the
``sim ensemble compare`` command that prints them."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim.connection import RunConnection
from analysis.sim.ensemble import (
    comparison_deltas,
    load_ensemble,
    member_pairs,
    members_balance_by_month,
    members_net_worth_by_month,
    paired_deltas,
    quantile_deltas,
    reserve_shares,
    risk_summary,
    tail_means,
)
from analysis.sim.types import DollarMode

MONTHS = pd.date_range("2027-01-31", periods=3, freq="ME")


def _members(rows: list[list[float]]) -> pd.DataFrame:
    """A month x member frame, one list of member values per month."""
    return pd.DataFrame(
        rows, index=MONTHS[:len(rows)],
        columns=[f"member_{i:04d}" for i in range(len(rows[0]))],
    )


def test_tail_means_average_the_worst_share_rounded_up_to_whole_members() -> None:
    frame = _members([list(range(1, 21))])  # 1..20, shuffled order irrelevant
    tails = tail_means(frame)
    assert list(tails.columns) == ["cvar05", "cvar20"]
    assert tails["cvar05"].iloc[0] == 1  # 5% of 20 is one member
    assert tails["cvar20"].iloc[0] == (1 + 2 + 3 + 4) / 4
    # 5% of 30 is 1.5 members: rounded up to two, never down to one.
    assert tail_means(_members([list(range(1, 31))]))["cvar05"].iloc[0] == 1.5
    # 20% of 80 is exactly 16, despite 0.2 * 80 not being exact in floating point.
    assert tail_means(_members([list(range(1, 81))]))["cvar20"].iloc[0] == 8.5


def test_tail_means_take_at_least_one_member() -> None:
    assert tail_means(_members([[7.0, 9.0]]))["cvar05"].iloc[0] == 7.0


def test_reserve_shares_count_members_below_now_and_ever() -> None:
    frame = _members([[50.0, 150.0, 200.0, 300.0], [150.0, 150.0, 90.0, 300.0]])
    shares = reserve_shares(frame, 100.0)
    assert list(shares["below_reserve"]) == [0.25, 0.25]
    # The first member recovered, but it has been below: path-dependent.
    assert list(shares["ever_below_reserve"]) == [0.25, 0.5]


def test_risk_summary_measures_the_reserve_on_the_cpi_constant_frame() -> None:
    nominal = _members([[150.0, 300.0]])
    cpi_constant = _members([[90.0, 200.0]])
    assert list(risk_summary(nominal).columns) == ["cvar05", "cvar20"]
    summary = risk_summary(nominal, 100.0, cpi_constant)
    assert summary["cvar05"].iloc[0] == 150.0  # tails stay in the shown dollars
    assert summary["below_reserve"].iloc[0] == 0.5


def test_quantile_deltas_compare_each_breakpoint_with_the_same_breakpoint() -> None:
    baseline = _members([[10.0, 20.0, 30.0, 40.0, 50.0]])
    deltas = quantile_deltas(baseline, baseline + 7)
    assert list(deltas.columns) == ["p05", "p20", "p40", "p60", "p80", "p95"]
    assert (deltas == 7).all().all()


def test_comparison_deltas_add_tail_deltas_and_the_paired_share_ahead() -> None:
    baseline = _members([[10.0, 20.0, 30.0, 40.0]])
    variant = _members([[15.0, 18.0, 35.0, 40.0]])
    pairs = [(c, c) for c in baseline.columns]
    deltas = comparison_deltas(baseline, variant, pairs)
    assert list(deltas.columns) == [
        "Δp05", "Δp20", "Δp40", "Δp60", "Δp80", "Δp95", "Δcvar05", "Δcvar20", "P(ahead)",
    ]
    assert deltas["Δcvar05"].iloc[0] == 15.0 - 10.0
    # Ahead in two of four futures; a tie isn't ahead.
    assert deltas["P(ahead)"].iloc[0] == 0.5
    assert "P(ahead)" not in comparison_deltas(baseline, variant).columns
    assert list(paired_deltas(baseline, variant, pairs).iloc[0]) == [5.0, -2.0, 5.0, 0.0]


@pytest.mark.slow
def test_members_with_one_seed_pair_and_draw_the_same_future(
    seeded_ensemble_batch: Callable[..., Path],
) -> None:
    baseline = load_ensemble(seeded_ensemble_batch("baseline", 1))
    child = load_ensemble(seeded_ensemble_batch("child", 1))
    other = load_ensemble(seeded_ensemble_batch("other", 2))
    try:
        pairs, reason = member_pairs(baseline, child)
        assert reason is None
        assert pairs == [(f"member_{i:04d}", f"member_{i:04d}") for i in range(3)]
        deltas = paired_deltas(
            members_net_worth_by_month(baseline), members_net_worth_by_month(child), pairs,
        )
        assert (deltas.abs() < 1e-6).all().all()

        pairs, reason = member_pairs(baseline, other)
        assert pairs == []
        assert reason is not None and "different seeds" in reason
    finally:
        for conns in (baseline, child, other):
            for con in conns.values():
                con.close()


def test_one_accounts_balance_matches_the_runs_own_balance_pivot(
    seeded_ensemble_batch: Callable[..., Path],
) -> None:
    connections = load_ensemble(seeded_ensemble_batch("baseline", 1))
    try:
        con: RunConnection = connections["member_0001"]
        for mode, pivot in (
            (DollarMode.NOMINAL, con.balance_pivot()),
            (DollarMode.CPI_CONSTANT, con.cpi_balance_pivot()),
        ):
            balance = con.account_balance("Asset_Checking", mode)
            pd.testing.assert_series_equal(
                balance, pivot["Asset_Checking"], check_names=False, check_freq=False,
            )
        reads: list[int] = []
        frame = members_balance_by_month(
            connections, "Asset_Checking", on_member=lambda: reads.append(len(reads)),
        )
        assert list(frame.columns) == sorted(connections)
        assert len(reads) == len(connections)  # one progress step per member (#235)
        assert con.account_balance("No_Such_Account").empty
        with pytest.raises(Exception, match="no account 'No_Such_Account'"):
            members_balance_by_month(connections, "No_Such_Account")
    finally:
        for con in connections.values():
            con.close()


def test_sim_ensemble_compare_prints_deltas_and_each_recipes_tail(
    seeded_ensemble_batch: Callable[..., Path], runs_dir: Path,
) -> None:
    seeded_ensemble_batch("baseline", 1)
    seeded_ensemble_batch("child", 1)
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "compare", "baseline", "child",
        "--runs-dir", str(runs_dir), "--reserve", "1000000",
    ])
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()  # the series store note goes to stderr
    assert lines[0].startswith("child vs baseline: after-tax net worth, CPI-constant dollars; 3 and 3")
    assert lines[1] == "  paired: 3 future(s)"
    assert lines[3].split() == [
        "year", "Δp05", "Δp20", "Δp40", "Δp60", "Δp80", "Δp95", "Δcvar05", "Δcvar20",
        "Δbelow_reserve", "Δever_below_reserve", "P(ahead)",
    ]
    assert "baseline tail, year end; reserve $1,000,000 (CPI-constant):" in lines
    tail_header = lines[lines.index("child tail, year end; reserve $1,000,000 (CPI-constant):") + 1]
    assert tail_header.split() == [
        "year", "cvar05", "cvar20", "below_reserve", "ever_below_reserve",
    ]


def test_sim_ensemble_compare_says_why_recipes_dont_pair(
    seeded_ensemble_batch: Callable[..., Path], runs_dir: Path,
) -> None:
    seeded_ensemble_batch("baseline", 1)
    seeded_ensemble_batch("other", 2)
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "compare", "baseline", "other", "--runs-dir", str(runs_dir),
        "--account", "Asset_Checking", "--dollars", "nominal",
    ])
    assert result.exit_code == 0, result.output
    assert "account Asset_Checking, nominal dollars" in result.output
    assert "not paired: the recipes have different seeds" in result.output
    assert "P(ahead)" not in result.output


def test_sim_ensemble_summary_reports_one_account(
    seeded_ensemble_batch: Callable[..., Path], runs_dir: Path,
) -> None:
    seeded_ensemble_batch("baseline", 1)
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "summary", "baseline", "--runs-dir", str(runs_dir),
        "--account", "Asset_Checking", "--reserve", "5000",
    ])
    assert result.exit_code == 0, result.output
    assert "account Asset_Checking; reserve $5,000 (CPI-constant)" in result.output
    assert result.stdout.splitlines()[1].split()[-2:] == ["below_reserve", "ever_below_reserve"]


def test_sim_ensemble_compare_rejects_a_reserve_that_is_not_dollars(
    seeded_ensemble_batch: Callable[..., Path], runs_dir: Path,
) -> None:
    seeded_ensemble_batch("baseline", 1)
    seeded_ensemble_batch("child", 1)
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "compare", "baseline", "child", "--runs-dir", str(runs_dir),
        "--reserve", "lots",
    ])
    assert result.exit_code != 0
    assert "--reserve" in result.output


def test_comparison_deltas_add_reserve_share_deltas_given_a_reserve() -> None:
    baseline = _members([[50.0, 150.0, 200.0, 300.0], [150.0, 150.0, 90.0, 300.0]])
    variant = baseline + 100
    deltas = comparison_deltas(baseline, variant, reserve=100.0)
    assert list(deltas.columns)[-2:] == ["Δbelow_reserve", "Δever_below_reserve"]
    assert list(deltas["Δbelow_reserve"]) == [-0.25, -0.25]
    assert list(deltas["Δever_below_reserve"]) == [-0.25, -0.5]
    # Measured on the CPI-constant pair when given, not the frames shown.
    cpi = comparison_deltas(baseline, variant, reserve=100.0, cpi_frames=(variant, baseline))
    assert list(cpi["Δbelow_reserve"]) == [0.25, 0.25]


def _rename_account(batch_dir: Path, old: str, new: str) -> None:
    """Stand in for a recipe without ``old``: rename it in every member."""
    import sqlite3

    for member in batch_dir.glob("member_*.sqlite3"):
        with sqlite3.connect(member) as con:
            con.execute("UPDATE account SET name = ? WHERE name = ?", (new, old))


def test_sim_ensemble_compare_shows_a_one_sided_account_without_deltas(
    seeded_ensemble_batch: Callable[[str, int], Path], runs_dir: Path,
) -> None:
    seeded_ensemble_batch("baseline", 1)
    _rename_account(seeded_ensemble_batch("child", 1), "Asset_Checking", "Asset_Renamed")
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "compare", "baseline", "child", "--runs-dir", str(runs_dir),
        "--account", "Asset_Renamed",
    ])
    assert result.exit_code == 0, result.output
    assert "only one side: baseline has no account Asset_Renamed" in result.output
    assert "Δp05" not in result.output
    assert "child tail, year end:" in result.output
    assert "baseline tail" not in result.output


# --chart and --chart-file (#167, #393)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def test_sim_ensemble_summary_chart_file_writes_an_svg_before_the_tables(
    seeded_ensemble_batch: Callable[..., Path], runs_dir: Path, tmp_path: Path,
) -> None:
    seeded_ensemble_batch("baseline", 1)
    chart = tmp_path / "fan.svg"
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "summary", "baseline", "--runs-dir", str(runs_dir),
        "--chart-file", str(chart), "--table",
    ])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0] == f"wrote {chart}"  # the chart comes first (#393)
    assert result.stdout.splitlines()[2].split()[0] == "year"
    svg = chart.read_text()
    assert "<svg" in svg and "net worth fan chart (nominal) (baseline)" in svg


def test_sim_ensemble_compare_graph_draws_every_panel_the_options_ask_for(
    seeded_ensemble_batch: Callable[..., Path], runs_dir: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Paired recipes and a reserve: all four of the notebook's panels, in
    the dollars --dollars asks for; with no image terminal, as a PNG file."""
    import tempfile

    from analysis.sim import ensemble_plots

    figures: list = []
    real_figure = ensemble_plots.ensemble_compare_grid_figure

    def _spy(*args, **kwargs):
        figures.append(real_figure(*args, **kwargs))
        return figures[-1]

    monkeypatch.setattr(ensemble_plots, "ensemble_compare_grid_figure", _spy)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    seeded_ensemble_batch("baseline", 1)
    seeded_ensemble_batch("child", 1)
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "compare", "baseline", "child", "--runs-dir", str(runs_dir),
        "--reserve", "1000000", "--dollars", "nominal", "--chart",
    ])
    assert result.exit_code == 0, result.output
    (figure,) = figures
    titles = [ax.get_title() for ax in figure.axes]
    assert len(titles) == 4
    assert "3 paired members" in titles[0] and "CVaR" in titles[1]
    assert "(nominal)" in titles[2] and "$1,000,000 reserve" in titles[3]
    written = next(tmp_path.glob("ensemble-*.png"))
    assert f"wrote {written}" in result.output and written.read_bytes().startswith(PNG_MAGIC)
    assert "can't show images" in result.output


def test_sim_ensemble_compare_graph_of_a_one_sided_account_is_its_fan_chart(
    seeded_ensemble_batch: Callable[[str, int], Path], runs_dir: Path, tmp_path: Path,
) -> None:
    seeded_ensemble_batch("baseline", 1)
    _rename_account(seeded_ensemble_batch("child", 1), "Asset_Checking", "Asset_Renamed")
    chart = tmp_path / "one-sided.svg"
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "compare", "baseline", "child", "--runs-dir", str(runs_dir),
        "--account", "Asset_Renamed", "--chart-file", str(chart),
    ])
    assert result.exit_code == 0, result.output
    assert "account Asset_Renamed fan chart (CPI-constant) (child)" in chart.read_text()


def test_sim_ensemble_graph_shows_in_an_image_terminal(
    seeded_ensemble_batch: Callable[..., Path], runs_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from analysis.sim import chain_graph

    monkeypatch.setattr(chain_graph, "terminal_protocol", lambda: "kitty")
    monkeypatch.setattr(chain_graph, "cell_size", lambda: (10.0, 24.0))
    monkeypatch.setattr(chain_graph, "swallow_replies", lambda: None)
    seeded_ensemble_batch("baseline", 1)
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "summary", "baseline", "--runs-dir", str(runs_dir), "--chart",
    ])
    assert result.exit_code == 0, result.output
    assert "\x1b_Ga=T,f=100,q=2" in result.output
    assert "wrote" not in result.output


@pytest.mark.parametrize("options, message", [
    (["--chart", "--chart-file", "chart.png"], "use one of --chart and --chart-file"),
    (["--chart-file", "chart.gif"], "must end in .png or .svg"),
])
def test_sim_ensemble_graph_options_are_checked_before_loading(
    runs_dir: Path, options: list[str], message: str,
) -> None:
    for command in (["summary", "baseline"], ["compare", "baseline", "child"]):
        result = CliRunner().invoke(main, [
            "sim", "ensemble", *command, "--runs-dir", str(runs_dir), *options,
        ])
        assert result.exit_code == 2 and message in result.output, result.output


def test_figure_dpi_matches_the_terminal_text_height() -> None:
    from analysis.sim.cli import _figure_dpi

    # 10pt text in a 24px cell's 20px line: 20px per 10pt is 144 dpi.
    assert _figure_dpi((10.0, 24.0)) == pytest.approx(144.0)
    assert _figure_dpi(None) == 200.0
    assert _figure_dpi((1.0, 2.0)) == 72.0  # never below matplotlib's point size


# Several variants (#328)


def test_sim_ensemble_compare_with_several_variants_prints_the_notebooks_two_tables(
    seeded_ensemble_batch: Callable[..., Path], runs_dir: Path,
) -> None:
    seeded_ensemble_batch("baseline", 1)
    seeded_ensemble_batch("child", 1)
    seeded_ensemble_batch("other", 2)
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "compare", "baseline", "child", "other",
        "--runs-dir", str(runs_dir), "--reserve", "1000000",
    ])
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == (
        "child, other vs baseline: after-tax net worth, CPI-constant dollars; "
        "baseline 3, child 3, other 3 member(s)")
    assert lines[1] == "  child: paired, 3 future(s)"
    assert lines[2].startswith("  other: not paired: the recipes have different seeds")
    assert lines[3] == "Against baseline, by year end:"
    assert lines[4].split() == [
        "year", "recipe", "Δp05", "Δp20", "Δp40", "Δp60", "Δp80", "Δp95", "Δcvar05",
        "Δcvar20", "Δbelow_reserve", "Δever_below_reserve", "P(ahead)",
    ]
    tail_at = lines.index("Tail and reserve, by recipe, year end; reserve $1,000,000 (CPI-constant):")
    deltas = lines[5:tail_at]
    assert [row.split()[1] for row in deltas[:2]] == ["child", "other"]
    # Only the paired variant has a share ahead; the other's cell is blank.
    assert len(deltas[0].split()) == len(lines[4].split())
    assert len(deltas[1].split()) == len(lines[4].split()) - 1
    assert lines[tail_at + 1].split() == [
        "year", "recipe", "cvar05", "cvar20", "below_reserve", "ever_below_reserve",
    ]
    assert [row.split()[1] for row in lines[tail_at + 2:tail_at + 5]] == [
        "baseline", "child", "other"]
    # The same tables as the notebook's widget.
    from analysis.sim.ensemble_tables import RecipeTables
    from analysis.sim.series_db import open_series

    tables = RecipeTables(
        {name: open_series(runs_dir / name / "2026-01-01T000000Z")
         for name in ("baseline", "child", "other")},
        None, None, DollarMode.CPI_CONSTANT, 1_000_000.0, after_tax=True)
    assert len(deltas) == len(tables.deltas_table("baseline", ["child", "other"]))


def test_sim_ensemble_compare_names_the_recipes_without_the_account(
    seeded_ensemble_batch: Callable[[str, int], Path], runs_dir: Path,
) -> None:
    seeded_ensemble_batch("baseline", 1)
    seeded_ensemble_batch("child", 1)
    _rename_account(seeded_ensemble_batch("renamed", 1), "Asset_Checking", "Asset_Renamed")
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "compare", "baseline", "child", "renamed",
        "--runs-dir", str(runs_dir), "--account", "Asset_Checking",
    ])
    assert result.exit_code == 0, result.output
    assert "  no account Asset_Checking in renamed" in result.output
    assert "  child: paired, 3 future(s)" in result.output
    assert "renamed:" not in result.output


def test_sim_ensemble_compare_refuses_a_variant_named_twice(
    seeded_ensemble_batch: Callable[..., Path], runs_dir: Path,
) -> None:
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "compare", "baseline", "child", "child", "--runs-dir", str(runs_dir),
    ])
    assert result.exit_code == 2 and "name each variant once" in result.output


def test_sim_ensemble_compare_graph_draws_a_column_for_each_variant(
    seeded_ensemble_batch: Callable[..., Path], runs_dir: Path, tmp_path: Path,
) -> None:
    seeded_ensemble_batch("baseline", 1)
    seeded_ensemble_batch("child", 1)
    seeded_ensemble_batch("other", 2)
    chart = tmp_path / "grid.svg"
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "compare", "baseline", "child", "other",
        "--runs-dir", str(runs_dir), "--chart-file", str(chart),
    ])
    assert result.exit_code == 0, result.output
    svg = chart.read_text()
    assert "child − baseline, future by future (3 paired members)" in svg
    assert "other − baseline: no paired futures (different seeds)" in svg
    assert "other − baseline: mean of the worst 5% and 20% (CVaR)" in svg


def test_sim_ensemble_compare_graph_refuses_more_variants_than_it_draws(
    runs_dir: Path,
) -> None:
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "compare", "e0", "a", "b", "c", "d", "e",
        "--runs-dir", str(runs_dir), "--chart",
    ])
    assert result.exit_code == 2
    assert "--chart draws at most 4 variants" in result.output
