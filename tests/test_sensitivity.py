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
    retire = nw[nw["row"] == "retire_bill_vary"].pivot(index="year", columns="quantile", values="delta")
    assert (retire.loc[:2029].abs() < 1).all().all()  # before the earliest date in range
    assert (retire.loc[2031:, 0.1] < 0).all()  # retiring earlier costs money


def test_oat_table_ranks_retirement_date_above_gift(oat_batch: Path) -> None:
    deltas = sensitivity.oat_deltas(oat_batch, DollarMode.NOMINAL)
    table = sensitivity.oat_table(deltas, 2035)
    assert list(table.index) == ["retire_bill_vary", "gift"]
    assert table.loc["retire_bill_vary", "delta@0.1"] < 0 < table.loc["retire_bill_vary", "delta@0.9"]
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
@pytest.mark.parametrize("row", ["gift", "retire_bill_vary"])
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
    assert list(draws.columns) == ["gift", "retire_bill_vary"]


# --- #38: correlated inputs -------------------------------------------------------


def _correlated(
    n: int, rho: float = 0.8, seed: int = 7, noise: float = 0.3,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """x2 correlated with x1 at ``rho``; the outcome depends on x1 alone."""
    import numpy as np

    rng = np.random.default_rng(seed)
    z1 = rng.normal(size=n)
    z2 = rho * z1 + np.sqrt(1 - rho**2) * rng.normal(size=n)
    draws = pd.DataFrame({"x1": z1, "x2": z2}, index=[f"member_{i:04d}" for i in range(1, n + 1)])
    outcomes = pd.DataFrame({2030: 3 * z1 + rng.normal(0, noise, n)}, index=draws.index)
    return draws, outcomes


def test_srrc_pins_a_row_whose_rho_is_only_its_correlate() -> None:
    draws, outcomes = _correlated(400)
    table = sensitivity.correlation_table(draws, outcomes)
    by_row = table.set_index("row")
    assert by_row.loc["x2", "rho"] > 0.6  # credited with x1's effect
    assert by_row.loc["x2", "srrc_low"] <= by_row.loc["x2", "srrc"] <= by_row.loc["x2", "srrc_high"]
    assert sensitivity.classify(table, 0.05, by="rho")["x2"] == "keep"
    status = sensitivity.classify(table, 0.05, by="srrc")
    assert status.to_dict() == {"x1": "keep", "x2": "pin_candidate"}


def test_srrc_interval_narrows_as_r2_rises() -> None:
    """SRRC's interval scales with the unexplained variance, unlike rho's:
    30 members can pin x2 when x1 explains nearly everything, but not when
    R^2 is near the trust threshold."""
    draws, outcomes = _correlated(30)
    sharp = sensitivity.correlation_table(draws, outcomes)
    assert sensitivity.classify(sharp, 0.05, by="srrc")["x2"] == "pin_candidate"
    draws, outcomes = _correlated(30, noise=1.3)
    noisy = sensitivity.correlation_table(draws, outcomes)
    assert noisy["r2"].iloc[0] >= sensitivity.R2_TRUST
    assert sensitivity.classify(noisy, 0.05, by="srrc")["x2"] == "inconclusive"


def test_prcc_is_the_partial_rank_correlation() -> None:
    """PRCC from the coefficient's t statistic equals the correlation of x1's
    and the outcome's ranks after regressing x2's ranks out of both."""
    import numpy as np

    draws, outcomes = _correlated(200, rho=0.5)
    table = sensitivity.correlation_table(draws, outcomes).set_index("row")

    def residual(v: np.ndarray, on: np.ndarray) -> np.ndarray:
        design = np.column_stack([np.ones_like(on), on])
        return v - design @ np.linalg.lstsq(design, v, rcond=None)[0]

    x1, x2 = draws["x1"].rank().to_numpy(), draws["x2"].rank().to_numpy()
    y = outcomes[2030].rank().to_numpy()
    expected = np.corrcoef(residual(x1, x2), residual(y, x2))[0, 1]
    assert table.loc["x1", "prcc"] == pytest.approx(expected, abs=1e-9)


def test_srrc_interval_is_undefined_for_a_row_that_never_varies() -> None:
    draws, outcomes = _correlated(100)
    table = sensitivity.correlation_table(draws.assign(x3=1.0), outcomes).set_index("row")
    assert pd.isna(table.loc["x3", "srrc_low"]) and pd.isna(table.loc["x3", "prcc"])
    assert not pd.isna(table.loc["x1", "srrc_low"])
    status = sensitivity.classify(table.reset_index(), 0.05, by="srrc")
    assert status["x3"] == "inconclusive"


def test_classify_rejects_an_unknown_measure() -> None:
    draws, outcomes = _correlated(30)
    with pytest.raises(ValueError, match="by must be one of"):
        sensitivity.classify(sensitivity.correlation_table(draws, outcomes), 0.05, by="prcc")


def test_factor_rows_carry_rho_and_no_status() -> None:
    draws, outcomes = _correlated(200)
    factors = pd.DataFrame({"f": draws["x1"] + draws["x2"]}, index=draws.index)
    table = sensitivity.correlation_table(draws, outcomes, factors)
    f = table[table["row"] == "f"].iloc[0]
    assert bool(f["factor"]) and f["rho"] > 0.8 and pd.isna(f["srrc"])
    assert not table.loc[table["row"] != "f", "factor"].any()
    # The factor isn't a regressor: adding it leaves the rows' SRRC unchanged.
    plain = sensitivity.correlation_table(draws, outcomes).set_index("row")
    assert table.set_index("row").loc["x1", "srrc"] == pytest.approx(plain.loc["x1", "srrc"])
    assert "f" not in sensitivity.classify(table, 0.05, by="srrc")


def test_score_correlations_check_links_and_implied_pairs() -> None:
    import numpy as np

    from analysis.sim.correlation import Link, resolve_scores

    links = {"a": Link("f", 0.8), "b": Link("f", 0.5)}
    rng = np.random.default_rng(11)
    scores = pd.DataFrame([
        resolve_scores(links, {node: rng.normal() for node in ("f", "a", "b")})
        for _ in range(2000)
    ])
    pairs = sensitivity.score_correlations(scores, links, ["a", "b", "f"]).set_index(["a", "b"])
    assert pairs.loc[("a", "f"), "kind"] == "link"
    assert pairs.loc[("a", "b"), "kind"] == "implied"
    assert pairs.loc[("a", "b"), "expected"] == pytest.approx(0.4)
    assert not pairs["outside"].any()
    assert (pairs["low"] <= pairs["achieved"]).all() and (pairs["achieved"] <= pairs["high"]).all()
    # The same scores against a structure they weren't drawn from.
    wrong = {"a": Link("f", 0.2), "b": Link("f", 0.5)}
    flagged = sensitivity.score_correlations(scores, wrong, ["a", "b", "f"]).set_index(["a", "b"])
    assert flagged.loc[("a", "f"), "outside"]


def test_score_correlations_skip_uncorrelated_pairs() -> None:
    from analysis.sim.correlation import Link

    scores = pd.DataFrame({"a": [0.1, 0.2, 0.3, 0.4], "c": [0.4, 0.1, 0.3, 0.2]})
    pairs = sensitivity.score_correlations(scores, {"a": Link("f", 0.8)}, ["a", "c"])
    assert pairs.empty and list(pairs.columns)[:3] == ["a", "b", "kind"]


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


def test_missing_arm_detected_from_batch_quantiles() -> None:
    """A one-row batch whose p90 member failed: without the batch's own
    quantile list, p10 alone would look like the complete set."""
    frame = _deltas({"only": {0.1: -10_000}})
    frame.attrs["quantiles"] = (0.1, 0.9)
    table = sensitivity.oat_table(frame, 2030)
    assert table.loc["only", "flags"] == "incomplete"
    assert pd.isna(table.loc["only", "impact"])


def test_oat_deltas_carries_batch_quantiles(oat_batch: Path) -> None:
    assert sensitivity.oat_deltas(oat_batch).attrs["quantiles"] == (0.1, 0.9)


def test_adjusted_r2_does_not_trust_overfit_noise() -> None:
    """With rows approaching members, in-sample R^2 is high for pure noise;
    adjusted R^2 isn't, so nothing becomes a pin candidate on its strength."""
    import numpy as np

    rng = np.random.default_rng(5)
    n, k = 12, 9
    draws = pd.DataFrame(rng.uniform(size=(n, k)), columns=[f"x{i}" for i in range(k)],
                         index=[f"member_{i:04d}" for i in range(1, n + 1)])
    outcomes = pd.DataFrame({2030: rng.normal(size=n)}, index=draws.index)
    table = sensitivity.correlation_table(draws, outcomes)
    assert table["r2"].iloc[0] < sensitivity.R2_TRUST
    assert "pin_candidate" not in set(sensitivity.classify(table, 0.05))


def test_adjusted_r2_undefined_without_residual_freedom() -> None:
    draws, outcomes = _synthetic(4, lambda x1, x2: 3 * x1)  # n - k - 1 = 1
    three = draws.assign(x3=draws["x1"] * 0.5 + draws["x2"])
    table = sensitivity.correlation_table(three, outcomes)
    assert table["r2"].isna().all()


def test_cli_attribute_relative_and_measure(oat_batch: Path, runs_dir: Path) -> None:
    relative = CliRunner().invoke(main, [
        "sim", "ensemble", "sensitivity", "retire_alice", "--runs-dir", str(runs_dir),
        "--attribute", "gift", "--years", "2030", "--relative",
    ])
    assert relative.exit_code == 0, relative.output
    assert "gift by category, % of base" in relative.output
    assert "%" in relative.output.splitlines()[-1]
    rejected = CliRunner().invoke(main, [
        "sim", "ensemble", "sensitivity", "retire_alice", "--runs-dir", str(runs_dir),
        "--attribute", "gift", "--measure", "min_net_worth",
    ])
    assert rejected.exit_code == 2
    assert "min_net_worth" in rejected.output


# --- the parameters table --------------------------------------------------------


def test_parameters_describes_an_oat_batch(oat_batch: Path) -> None:
    table = sensitivity.parameters(oat_batch)
    assert list(table.index) == ["gift", "retire_bill_vary"]
    gift = table.loc["gift"]
    assert gift["kind"] == "one_off_transfer"
    assert gift["affects"].startswith("Income_Inheritance → Asset_Brokerage_Joint")
    assert gift["distribution"] == "uniform"
    assert gift["range"] == "$10,000 to $30,000"
    assert gift["center"] == "$20,000"
    assert (gift["value@0.1"], gift["value@0.9"]) == ("$12,000", "$28,000")
    assert gift["shape"] == ""  # uniform has no shape parameters
    assert gift["moments"].startswith("mean $20,000, sd $5,774, skew +0.00, excess kurtosis -1.20")
    retire = table.loc["retire_bill_vary"]
    assert (retire["target"], retire["affects"]) == ("date", "salary_bill")
    assert retire["range"] == "2030-06-30 to 2031-06-30"


def test_parameters_formats_a_rate_range_as_percent(inflation_batch: Path) -> None:
    row = sensitivity.parameters(inflation_batch).loc["cpi_vary"]
    assert row["distribution"] == "uniform"
    assert row["range"] == "1.50% to 3.50%"


def test_parameters_describes_a_monte_carlo_batch(mc_batch: Path) -> None:
    table = sensitivity.parameters(mc_batch)
    assert "drawn" in table.columns
    assert table.loc["gift", "drawn"].endswith("(7 draws)")
    assert not any(c.startswith("value@") for c in table.columns)


def test_parameters_tolerates_batches_before_schema_28(oat_batch: Path) -> None:
    import sqlite3

    for member in oat_batch.glob("member_*.sqlite3"):
        con = sqlite3.connect(member)
        con.execute("ALTER TABLE ensemble_draw DROP COLUMN distribution")
        con.commit()
        con.close()
    table = sensitivity.parameters(oat_batch)
    assert (table["distribution"] == "").all() and (table["range"] == "").all()
    assert table.loc["gift", "value@0.1"] == "$12,000"



def test_parameters_describes_shape_and_moments(
    test_data_dir: Path, append_calendar_row: Callable[..., None],
    intent_dir: Path, runs_dir: Path,
) -> None:
    """#31: a skewed truncated_normal shows its shape and implied moments; a
    triangular date shows its mode, and its moments in days."""
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="checking_vary",
        series="Asset_Checking", amount="45000",
        vary_target="amount", vary_distribution="truncated_normal",
        vary_median="45000", vary_sd="5000", vary_low="40000", vary_high="70000",
        vary_skew="0.7",
    )
    append_calendar_row(
        date="2030-12-31", kind="retire_scheduled", name="retire_bill_vary",
        scheduled_name="salary_bill",
        vary_target="date", vary_distribution="triangular",
        vary_low="2030-06-30", vary_mode="2030-12-31", vary_high="2031-12-31",
    )
    runner = CliRunner()
    load = runner.invoke(main, [
        "sim", "load", "retire_alice", "--data-dir", str(test_data_dir),
        "--intent-dir", str(intent_dir), "--allow-dirty-data", "--no-run",
    ])
    assert load.exit_code == 0, load.output
    run = runner.invoke(main, [
        "sim", "run", "retire_alice", "--sensitivity", "--intent-dir", str(intent_dir),
        "--runs-dir", str(runs_dir), "--allow-dirty-data",
    ])
    assert run.exit_code == 0, run.output
    from analysis.sim.ensemble import latest_ensemble_batch

    table = sensitivity.parameters(latest_ensemble_batch(runs_dir / "retire_alice"))
    rate = table.loc["checking_vary"]
    assert rate["center"] == "4.50%" and rate["shape"] == "scale 0.50%, skew 0.7"
    assert "skew +" in rate["moments"]  # a positive skew parameter gives a right-skewed shape
    retire = table.loc["retire_bill_vary"]
    assert retire["shape"] == "mode 2030-12-31"
    assert retire["moments"].startswith("mean 2031-") and "days" in retire["moments"]

