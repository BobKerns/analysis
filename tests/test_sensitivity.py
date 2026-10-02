"""ADR-007 Phases 3-4: OAT deltas, tornado tables, rank grid, attribution and
the report CLI."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim import sensitivity
from analysis.sim.types import DollarMode

# --- pure functions over synthetic deltas ----------------------------------


def _deltas(points: dict[str, dict[float, float]], year: int = 2030, base: float = 1e6) -> pd.DataFrame:
    """A minimal ``oat_deltas``-shaped frame: row -> {quantile: delta}."""
    records = []
    for row, by_q in points.items():
        for q, d in by_q.items():
            for measure in sensitivity.MEASURES:
                records.append({
                    "row": row, "target": "amount", "quantile": q, "value": f"v{q:g}",
                    "year": year, "measure": measure, "level": base + d, "base": base,
                    "delta": d, "relative": d / base,
                })
    return pd.DataFrame(records)


def test_oat_table_sorts_by_swing_and_shares_sum_to_one() -> None:
    table = sensitivity.oat_table(_deltas({
        "small": {0.1: -10_000, 0.9: 10_000},
        "big": {0.1: -50_000, 0.9: 40_000},
    }), 2030)
    assert list(table.index) == ["big", "small"]
    assert table.loc["big", "swing"] == 90_000
    assert table["share"].sum() == pytest.approx(1.0)
    assert table.loc["big", "value@0.1"] == "v0.1"


def test_oat_table_relative_uses_relative_column() -> None:
    table = sensitivity.oat_table(_deltas({"r": {0.1: -10_000, 0.9: 20_000}}), 2030, relative=True)
    assert table.loc["r", "swing"] == pytest.approx(0.03)


def test_non_monotonic_row_ranks_by_impact_not_swing() -> None:
    """Both quantiles move the outcome +$100k: signed swing is 0, but the row
    matters, so it must rank first and never be a pin candidate (PR #23
    review)."""
    frame = _deltas({
        "u_shaped": {0.1: 100_000, 0.9: 100_000},
        "monotonic": {0.1: -10_000, 0.9: 10_000},
    })
    table = sensitivity.oat_table(frame, 2030)
    assert table.loc["u_shaped", "swing"] == 0
    assert table.loc["u_shaped", "impact"] == 100_000
    assert list(table.index) == ["u_shaped", "monotonic"]
    assert table.loc["u_shaped", "share"] == pytest.approx(100 / 120)
    assert "u_shaped" not in sensitivity.pin_candidates(frame, 0.05)


def test_impact_includes_the_base() -> None:
    table = sensitivity.oat_table(_deltas({"r": {0.1: -30_000, 0.9: 50_000}}), 2030)
    assert table.loc["r", "impact"] == 80_000
    one_sided = sensitivity.oat_table(_deltas({"r": {0.1: 20_000, 0.9: 50_000}}), 2030)
    assert one_sided.loc["r", "impact"] == 50_000  # from the base (0) up to the larger


def test_asymmetric_flag() -> None:
    table = sensitivity.oat_table(_deltas({
        "one_sided": {0.1: -30_000, 0.9: 0},
        "lopsided": {0.1: -10_000, 0.9: 50_000},
        "balanced": {0.1: -10_000, 0.9: 12_000},
    }), 2030)
    assert table.loc["one_sided", "flags"] == "asymmetric"
    assert table.loc["lopsided", "flags"] == "asymmetric"
    assert table.loc["balanced", "flags"] == ""


def test_bend_flag_needs_a_ladder_and_a_kink() -> None:
    linear = {0.1: -40, 0.25: -25, 0.4: -10, 0.6: 10, 0.75: 25, 0.9: 40}
    step = {0.1: 0, 0.25: 0, 0.4: 0, 0.6: 0, 0.75: 50_000, 0.9: 50_000}
    reversal = {0.1: -20_000, 0.25: -10_000, 0.4: -5_000, 0.6: 5_000, 0.75: 10_000, 0.9: -30_000}
    table = sensitivity.oat_table(_deltas({
        "linear": {q: d * 1_000 for q, d in linear.items()},
        "step": step, "reversal": reversal,
    }), 2030)
    assert "bend" not in table.loc["linear", "flags"]
    assert "bend" in table.loc["step", "flags"]
    assert "bend" in table.loc["reversal", "flags"]
    two_point = sensitivity.oat_table(_deltas({"s": {0.1: 0, 0.9: 50_000}}), 2030)
    assert "bend" not in two_point.loc["s", "flags"]


def test_rank_by_year_and_pin_candidates() -> None:
    frame = pd.concat([
        _deltas({"early": {0.1: -50_000, 0.9: 50_000}, "late": {0.1: 0, 0.9: 0},
                 "tiny": {0.1: -100, 0.9: 100}}, year=2030),
        _deltas({"early": {0.1: -10_000, 0.9: 10_000}, "late": {0.1: -90_000, 0.9: 90_000},
                 "tiny": {0.1: -100, 0.9: 100}}, year=2040),
    ])
    ranks = sensitivity.rank_by_year(frame, value="rank")
    assert ranks.loc["early", 2030] == 1 and ranks.loc["late", 2040] == 1
    assert pd.isna(ranks.loc["late", 2030])  # no effect that year: unranked, not tied
    share = sensitivity.rank_by_year(frame, value="share")
    assert list(share.index)[:2] == ["early", "late"] or list(share.index)[:2] == ["late", "early"]
    # "early" fades, but still matters in 2030 -- only "tiny" is negligible in every year.
    assert sensitivity.pin_candidates(frame, 0.05) == ["tiny"]


def test_default_report_years() -> None:
    years = list(range(2026, 2041))
    months = {y: 12 for y in years} | {2026: 8}
    assert sensitivity.default_report_years(years, months) == [2027, 2032, 2037, 2040]
    assert sensitivity.default_report_years([2030]) == [2030]


@pytest.mark.parametrize(("kind", "target", "raw", "expected"), [
    ("set_rate", "amount", "45000", "4.50%"),
    ("one_off_transfer", "amount", "5000000", "$50,000"),
    ("retire_scheduled", "date", "2031-06-30", "2031-06-30"),
])
def test_display_value(kind: str, target: str, raw: str, expected: str) -> None:
    assert sensitivity.display_value(kind, target, raw) == expected


# --- end to end on the retire_alice prototype ------------------------------


def test_oat_deltas_shape_and_timing(oat_batch: Path) -> None:
    deltas = sensitivity.oat_deltas(oat_batch, DollarMode.NOMINAL)
    nw = deltas[deltas["measure"] == "net_worth"]
    gift = nw[nw["row"] == "gift"].pivot(index="year", columns="quantile", values="delta")
    # No effect before the gift arrives; after, a bigger gift is worth more.
    assert (gift.loc[:2028].abs() < 1).all().all()
    assert (gift.loc[2029:, 0.1] < 0).all() and (gift.loc[2029:, 0.9] > 0).all()
    retire = nw[nw["row"] == "retire_bob_vary"].pivot(index="year", columns="quantile", values="delta")
    assert (retire.loc[:2029].abs() < 1).all().all()  # before the earliest date in range
    assert (retire.loc[2031:, 0.1] < 0).all()  # retiring earlier costs money


def test_oat_table_ranks_retirement_date_above_gift(oat_batch: Path) -> None:
    deltas = sensitivity.oat_deltas(oat_batch, DollarMode.NOMINAL)
    table = sensitivity.oat_table(deltas, 2035)
    assert list(table.index) == ["retire_bob_vary", "gift"]
    assert table.loc["retire_bob_vary", "delta@0.1"] < 0 < table.loc["retire_bob_vary", "delta@0.9"]
    assert table["share"].sum() == pytest.approx(1.0)


def test_relative_delta_matches_across_dollar_modes_for_a_non_inflation_row(oat_batch: Path) -> None:
    nominal = sensitivity.oat_deltas(oat_batch, DollarMode.NOMINAL)
    real = sensitivity.oat_deltas(oat_batch, DollarMode.CPI_CONSTANT)
    key = ["row", "quantile", "year", "measure"]
    merged = nominal.merge(real, on=key, suffixes=("_nom", "_cpi")).dropna(
        subset=["relative_nom", "relative_cpi"]
    )
    # net_worth only: min_net_worth legitimately differs between dollar modes
    # (the nominal minimum is the opening balance; the CPI-constant one can
    # come later, as inflation erodes it).
    gift = merged[(merged["row"] == "gift") & (merged["measure"] == "net_worth")]
    assert (gift["relative_nom"] - gift["relative_cpi"]).abs().max() < 1e-6


def test_relative_delta_differs_across_dollar_modes_for_the_inflation_row(
    inflation_batch: Path,
) -> None:
    """ADR-007 §4: nominal-% and CPI-%-deltas only differ when the perturbed
    row is the inflation rate itself -- the base and member deflators differ."""
    nominal = sensitivity.oat_deltas(inflation_batch, DollarMode.NOMINAL)
    real = sensitivity.oat_deltas(inflation_batch, DollarMode.CPI_CONSTANT)
    key = ["row", "quantile", "year", "measure"]
    merged = nominal.merge(real, on=key, suffixes=("_nom", "_cpi"))
    late = merged[(merged["year"] == merged["year"].max()) & (merged["measure"] == "net_worth")]
    assert (late["relative_nom"] - late["relative_cpi"]).abs().min() > 1e-3


def test_relative_is_blank_near_zero_base() -> None:
    frame = _deltas({"r": {0.1: -10, 0.9: 10}}, base=500.0)
    frame["relative"] = frame["delta"] / frame["base"].abs().where(
        frame["base"].abs() >= sensitivity.RELATIVE_NEAR_ZERO_DOLLARS
    )
    assert frame["relative"].isna().all()


def test_cli_reports_oat_batch(oat_batch: Path, runs_dir: Path) -> None:
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "sensitivity", "retire_alice", "--runs-dir", str(runs_dir),
        "--years", "2030,2035",
    ])
    assert result.exit_code == 0, result.output
    assert f"sensitivity retire_alice ({oat_batch.name})" in result.output
    assert "  2030" in result.output and "  2035" in result.output
    assert "gift" in result.output and "share=" in result.output
    assert "rank by year:" in result.output


def test_cli_relative_and_min_measure(oat_batch: Path, runs_dir: Path) -> None:
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "sensitivity", "retire_alice", "--runs-dir", str(runs_dir),
        "--relative", "--measure", "min_net_worth", "--dollars", "nominal",
    ])
    assert result.exit_code == 0, result.output
    assert "min_net_worth, % of base" in result.output


def test_cli_rejects_unknown_year(oat_batch: Path, runs_dir: Path) -> None:
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "sensitivity", "retire_alice", "--runs-dir", str(runs_dir),
        "--years", "1999",
    ])
    assert result.exit_code != 0
    assert "1999" in result.output


# --- Phase 4: attribution ----------------------------------------------------


@pytest.mark.parametrize("by", sensitivity.ATTRIBUTION_AXES)
@pytest.mark.parametrize("dollars", list(DollarMode))
@pytest.mark.parametrize("row", ["gift", "retire_bob_vary"])
def test_attribution_sums_exactly(oat_batch: Path, row: str, by: str, dollars: DollarMode) -> None:
    """The double-entry identity: rows sum to the net-worth delta, with
    nothing unattributed, along every axis and in both dollar modes."""
    for table in sensitivity.oat_attribution(oat_batch, row, by=by, dollars=dollars).values():
        assert (table[sensitivity.UNATTRIBUTED] == 0).all()
        parts = table.drop(columns=[sensitivity.UNATTRIBUTED, sensitivity.TOTAL])
        assert (parts.sum(axis=1) - table[sensitivity.TOTAL]).abs().max() < sensitivity.UNATTRIBUTED_TOLERANCE


def test_attribution_total_matches_oat_deltas(oat_batch: Path) -> None:
    deltas = sensitivity.oat_deltas(oat_batch, DollarMode.NOMINAL)
    table = sensitivity.oat_attribution(oat_batch, "gift", dollars=DollarMode.NOMINAL)[0.9]
    expected = deltas[(deltas["row"] == "gift") & (deltas["quantile"] == 0.9)
                      & (deltas["measure"] == "net_worth")].set_index("year")["delta"]
    assert (table[sensitivity.TOTAL] - expected).abs().max() < 0.01


def test_attribution_places_the_gift_in_its_category(oat_batch: Path) -> None:
    """A smaller gift (q=0.1: $12,000 vs the central $20,000) shows up as
    other_income, starting the year it arrives."""
    table = sensitivity.oat_attribution(oat_batch, "gift", dollars=DollarMode.NOMINAL)[0.1]
    assert table.loc[2028, "other_income"] == 0
    assert table.loc[2029, "other_income"] == pytest.approx(-8_000)
    holdings = sensitivity.oat_attribution(
        oat_batch, "gift", by="holding_account", dollars=DollarMode.NOMINAL,
    )[0.1]
    assert holdings.loc[2029, "Asset_Brokerage_Joint"] < 0


def test_cpi_attribution_carries_opening_balance_row_for_inflation(inflation_batch: Path) -> None:
    """Perturbing inflation deflates the same opening balance differently in
    base and member; that's its own row, so CPI-constant rows still sum
    exactly."""
    table = sensitivity.oat_attribution(
        inflation_batch, "cpi_vary", dollars=DollarMode.CPI_CONSTANT,
    )[0.9]
    assert (table[sensitivity.UNATTRIBUTED] == 0).all()
    assert table[sensitivity.OPENING_BALANCES].iloc[-1] < 0  # higher inflation erodes it
    nominal = sensitivity.oat_attribution(inflation_batch, "cpi_vary", dollars=DollarMode.NOMINAL)[0.9]
    assert sensitivity.OPENING_BALANCES not in nominal.columns  # identical, so it cancels


def test_oat_attribution_unknown_row(oat_batch: Path) -> None:
    from analysis.sim.ensemble import EnsembleError

    with pytest.raises(EnsembleError, match="nope"):
        sensitivity.oat_attribution(oat_batch, "nope")


def test_cli_attribute(oat_batch: Path, runs_dir: Path) -> None:
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "sensitivity", "retire_alice", "--runs-dir", str(runs_dir),
        "--attribute", "gift", "--years", "2030", "--dollars", "nominal",
    ])
    assert result.exit_code == 0, result.output
    assert "gift by category, nominal $" in result.output
    assert "other_income" in result.output and "total" in result.output
    assert "unattributed" not in result.output  # only shown when non-zero


def test_cli_attribute_rejects_unknown_row(oat_batch: Path, runs_dir: Path) -> None:
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "sensitivity", "retire_alice", "--runs-dir", str(runs_dir),
        "--attribute", "nope",
    ])
    assert result.exit_code != 0
    assert "nope" in result.output


# --- Phase 5: correlation over a Monte Carlo batch ----------------------------


def _synthetic(n: int, outcome, seed: int = 3) -> tuple[pd.DataFrame, pd.DataFrame]:
    import numpy as np

    rng = np.random.default_rng(seed)
    draws = pd.DataFrame({"x1": rng.uniform(-1, 1, n), "x2": rng.uniform(-1, 1, n)},
                         index=[f"member_{i:04d}" for i in range(1, n + 1)])
    noise = rng.normal(0, 0.1, n)
    outcomes = pd.DataFrame({2030: outcome(draws["x1"], draws["x2"]) + noise}, index=draws.index)
    return draws, outcomes


def test_correlation_separates_signal_from_nothing_at_large_n() -> None:
    draws, outcomes = _synthetic(400, lambda x1, x2: 3 * x1)
    table = sensitivity.correlation_table(draws, outcomes)
    by_row = table.set_index("row")
    assert by_row.loc["x1", "rho"] > 0.9
    assert by_row.loc["x1", "r2"] > 0.9
    assert abs(by_row.loc["x2", "srrc"]) < 0.1
    status = sensitivity.classify(table, 0.05)
    assert status["x1"] == "keep" and status["x2"] == "pin_candidate"


def test_correlation_is_inconclusive_at_small_n() -> None:
    draws, outcomes = _synthetic(30, lambda x1, x2: 3 * x1)
    status = sensitivity.classify(sensitivity.correlation_table(draws, outcomes), 0.05)
    assert status["x2"] == "inconclusive"


def test_pure_interaction_is_never_pinned() -> None:
    """Each row's marginal rho is ~0, but together they decide the outcome:
    low R^2 must make them inconclusive, not droppable (PR #23 review)."""
    draws, outcomes = _synthetic(400, lambda x1, x2: x1 * x2)
    table = sensitivity.correlation_table(draws, outcomes)
    assert table["r2"].iloc[0] < 0.2
    status = sensitivity.classify(table, 0.05)
    assert status.to_dict() == {"x1": "inconclusive", "x2": "inconclusive"}


def test_constant_outcome_year_is_skipped_not_negligible() -> None:
    draws, outcomes = _synthetic(400, lambda x1, x2: 3 * x1)
    outcomes[2020] = 5.0  # before anything varies
    table = sensitivity.correlation_table(draws, outcomes)
    assert table[table["year"] == 2020]["rho"].isna().all()
    assert sensitivity.classify(table, 0.05)["x1"] == "keep"


@pytest.mark.parametrize("rho", [1.0, -1.0, 0.9999999, -0.9999999, 0.95, 0.0])
def test_rho_interval_always_contains_rho(rho: float) -> None:
    """|rho| = 1 (every member ranked identically) used to give an interval
    that excluded rho, and a negative error bar in the notebook."""
    low, high = sensitivity.rho_interval(rho, 30)
    assert low <= rho <= high


def test_required_members_and_interval() -> None:
    assert 80 <= sensitivity.required_members(0.05) <= 100
    assert sensitivity.required_members(0.01) > 400 * 0.9
    low, high = sensitivity.rho_interval(0.0, 100)
    assert low == pytest.approx(-high) and 0.15 < high < 0.25


def test_draws_frame_excludes_member_zero(mc_batch: Path) -> None:
    from analysis.sim.ensemble import load_ensemble

    connections = load_ensemble(mc_batch)
    try:
        draws = sensitivity.draws_frame(connections)
    finally:
        for rc in connections.values():
            rc.close()
    assert "member_0000" not in draws.index
    assert len(draws) == 7
    assert list(draws.columns) == ["gift", "retire_bob_vary"]


def test_cli_reports_monte_carlo_batch(mc_batch: Path, runs_dir: Path) -> None:
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "sensitivity", "retire_alice", "--runs-dir", str(runs_dir),
        "--mode", "monte_carlo", "--years", "2035",
    ])
    assert result.exit_code == 0, result.output
    assert "7 drawn member(s)" in result.output
    assert "warning: 7 members can't call a row negligible" in result.output
    assert "R2=" in result.output and "rho=" in result.output
    assert "status (rho^2 vs 5%, every year):" in result.output


def test_cli_attribute_rejects_monte_carlo_batch(mc_batch: Path, runs_dir: Path) -> None:
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "sensitivity", "retire_alice", "--runs-dir", str(runs_dir),
        "--mode", "monte_carlo", "--attribute", "gift",
    ])
    assert result.exit_code != 0
    assert "needs a sensitivity batch" in result.output


# --- PR #25 review fixes ---------------------------------------------------------


def test_incomplete_row_is_never_measured_as_zero() -> None:
    """A row whose p90 member failed must not reuse p10 for both ends."""
    frame = _deltas({
        "whole": {0.1: -10_000, 0.9: 10_000},
        "missing_high": {0.1: -1_000},
    })
    table = sensitivity.oat_table(frame, 2030)
    assert table.loc["missing_high", "flags"] == "incomplete"
    assert pd.isna(table.loc["missing_high", "impact"])
    assert pd.isna(table.loc["missing_high", "share"])
    assert list(table.index) == ["whole", "missing_high"]  # unmeasurable rows last
    assert table.loc["whole", "share"] == 1.0
    assert "missing_high" not in sensitivity.pin_candidates(frame, 0.05)


def test_undefined_relative_delta_is_not_zero() -> None:
    frame = _deltas({"r": {0.1: -10_000, 0.9: 10_000}})
    frame.loc[frame["quantile"] == 0.9, "relative"] = float("nan")  # base near zero
    table = sensitivity.oat_table(frame, 2030, relative=True)
    assert table.loc["r", "flags"] == "undefined"
    assert pd.isna(table.loc["r", "impact"])


def test_classify_reports_unestimable_rows_as_inconclusive() -> None:
    draws, outcomes = _synthetic(3, lambda x1, x2: 3 * x1)  # too few members for rho
    table = sensitivity.correlation_table(draws, outcomes)
    assert table["rho"].isna().all()
    assert sensitivity.classify(table, 0.05).to_dict() == {
        "x1": "inconclusive", "x2": "inconclusive",
    }


def test_cumulative_flows_exclude_both_ytd_kinds(
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], object],
    tmp_path: Path,
) -> None:
    """ytd and ytd_rule catch-ups are balance-neutral (engine._post_ytd_transfer):
    counting them would put pre-simulation history into attribution."""
    import sqlite3

    from analysis.sim.connection import RunConnection
    from analysis.sim.engine import run_scenario

    scheduled = retire_alice_csv("scheduled.csv")
    df = pd.read_csv(scheduled, dtype=str, keep_default_na=False)
    df.loc[df["name"] == "salary_alice", "ytd_preroll"] = "1"
    df.to_csv(scheduled, index=False)
    run_path = tmp_path / "r.sqlite3"
    run_scenario(load_test_copy().intent_path, run_path, run_by="test", allow_dirty_data=True)

    con = sqlite3.connect(run_path)
    try:
        assert con.execute(
            "SELECT COUNT(*) FROM transfer_event WHERE reason_kind = 'ytd_rule'"
        ).fetchone()[0] > 0
        # Simulated salary in the first year: income-account outflows only,
        # catch-ups excluded.
        first_year, simulated = con.execute(
            "SELECT CAST(substr(te.date, 1, 4) AS INTEGER) AS y, SUM(te.amount) "
            "FROM transfer_event te JOIN account a ON a.id = te.from_account "
            "WHERE a.subkind = 'salary' AND te.reason_kind NOT IN ('ytd', 'ytd_rule') "
            "GROUP BY y ORDER BY y LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    rc = RunConnection(run_path)
    try:
        flows = sensitivity._cumulative_flows(rc, "category")
        assert flows.loc[first_year, "salary"] == pytest.approx(simulated / 100)
    finally:
        rc.close()
