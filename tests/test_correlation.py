"""ADR-008: correlated parameters -- Phase 0: latent factors, the correlation
columns, their validation across scenario inheritance, and load warnings."""

from __future__ import annotations

import json
import math
import random
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim import scenario
from analysis.sim.correlation import (
    CorrelationError,
    Link,
    expected_rank_correlation,
    implied_correlation,
    probability,
    resolve_scores,
    standard_normal,
    topological_order,
    tree_members,
)
from analysis.sim.distributions import Triangular, TruncatedNormal, Uniform
from analysis.sim.engine import run_scenario
from analysis.sim.loader import LoadError, LoadResult, load_scenario
from analysis.sim.registries import CALENDAR_HEADER


def _factor(append_calendar_row: Callable[..., None], name: str, **extra: str) -> None:
    append_calendar_row(kind="latent_factor", name=name, **extra)


def _varying_rate(append_calendar_row: Callable[..., None], name: str, **extra: str) -> None:
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name=name, series="Asset_Checking", amount="45000",
        vary_target="amount", vary_distribution="uniform", vary_low="40000", vary_high="50000",
        **extra,
    )


def _issues(load: Callable[[], LoadResult]) -> list[str]:
    with pytest.raises(LoadError) as ei:
        load()
    return ei.value.issues


def test_two_level_tree_loads_and_is_stored(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    """factor 'economy' -> factor 'inflation' -> two rows, plus one row hung
    directly on 'economy'; a factor's blank date becomes the scenario start."""
    _factor(append_calendar_row, "economy", description="the broad economy")
    _factor(append_calendar_row, "inflation", vary_correlate_with="economy",
            vary_correlation="0.7")
    _varying_rate(append_calendar_row, "rate_a", vary_correlate_with="inflation",
                  vary_correlation="0.8")
    append_calendar_row(
        date="2028-02-29", kind="set_rate", name="rate_b", series="Asset_Checking",
        amount="45000", vary_target="amount", vary_distribution="truncated_normal",
        vary_median="45000", vary_sd="3000", vary_correlate_with="economy",
        vary_correlation="-0.4",
    )
    result = load_test_copy()
    assert result.warnings == []
    con = sqlite3.connect(result.intent_path)
    try:
        rows = dict(
            (name, (kind, date, parent, rho)) for name, kind, date, parent, rho in con.execute(
                "SELECT name, kind, date, vary_correlate_with, vary_correlation "
                "FROM calendar_event WHERE name IS NOT NULL"
            )
        )
        start = con.execute("SELECT start FROM scenario").fetchone()[0]
    finally:
        con.close()
    assert rows["economy"] == ("latent_factor", start, None, None)
    assert rows["inflation"] == ("latent_factor", start, "economy", 0.7)
    assert rows["rate_a"][2:] == ("inflation", 0.8)
    assert rows["rate_b"][2:] == ("economy", -0.4)


def test_two_factors_in_one_scenario_both_survive_the_merge(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    """Factors have blank event fields; keyed by those, the second would
    collide with the first (PR #36 review). They're keyed by name instead."""
    _factor(append_calendar_row, "f1")
    _factor(append_calendar_row, "f2")
    _varying_rate(append_calendar_row, "r1", vary_correlate_with="f1", vary_correlation="0.5")
    append_calendar_row(
        date="2028-02-29", kind="set_rate", name="r2", series="Asset_Checking", amount="45000",
        vary_target="amount", vary_distribution="uniform", vary_low="40000", vary_high="50000",
        vary_correlate_with="f2", vary_correlation="0.5",
    )
    con = sqlite3.connect(load_test_copy().intent_path)
    try:
        factors = sorted(r[0] for r in con.execute(
            "SELECT name FROM calendar_event WHERE kind = 'latent_factor'"
        ))
    finally:
        con.close()
    assert factors == ["f1", "f2"]


def test_child_scenario_overrides_and_uses_a_parent_factor(
    append_calendar_row: Callable[..., None], test_data_dir: Path,
    test_scenarios_dir: Path, intent_dir: Path,
) -> None:
    """A child scenario hangs a row on its parent's factor, and its own
    same-named factor row replaces the parent's (merged by name)."""
    _factor(append_calendar_row, "economy", description="parent's")
    scenario.create_scenario(test_data_dir, "variant", "retire_alice")
    child = pd.DataFrame([
        dict.fromkeys(CALENDAR_HEADER, "") | {
            "kind": "latent_factor", "name": "economy", "description": "child's",
        },
        dict.fromkeys(CALENDAR_HEADER, "") | {
            "date": "2027-02-28", "kind": "set_rate", "name": "rate_a",
            "series": "Asset_Checking", "amount": "45000", "vary_target": "amount",
            "vary_distribution": "uniform", "vary_low": "40000", "vary_high": "50000",
            "vary_correlate_with": "economy", "vary_correlation": "0.6",
        },
    ])
    child.to_csv(test_scenarios_dir / "variant" / "calendar.csv", index=False)
    result = load_scenario(test_data_dir, "variant", intent_dir=intent_dir,
                           scenarios_dir=test_scenarios_dir, allow_dirty_data=True)
    assert result.warnings == []  # the parent's factor now has a dependent
    con = sqlite3.connect(result.intent_path)
    try:
        factors = con.execute(
            "SELECT name, description FROM calendar_event WHERE kind = 'latent_factor'"
        ).fetchall()
    finally:
        con.close()
    assert factors == [("economy", "child's")]


@pytest.mark.parametrize(("rows", "message"), [
    ([("rate_a", {"vary_correlate_with": "nowhere", "vary_correlation": "0.5"})],
     "names no varying row or latent_factor"),
    ([("rate_a", {"vary_correlate_with": "rate_a", "vary_correlation": "0.5"})],
     "correlation cycle rate_a -> rate_a"),
    ([("rate_a", {"vary_correlate_with": "rate_b", "vary_correlation": "0.5"}),
      ("rate_b", {"vary_correlate_with": "rate_a", "vary_correlation": "0.5"})],
     "correlation cycle"),
    ([("rate_a", {"vary_correlate_with": "f", "vary_correlation": "1.5"})],
     "must be between -1 and 1"),
    ([("rate_a", {"vary_correlate_with": "f", "vary_correlation": "lots"})],
     "is not a number"),
    ([("rate_a", {"vary_correlate_with": "f"})], "go together"),
    ([("rate_a", {"vary_correlation": "0.5"})], "go together"),
])
def test_correlation_rejections(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    rows: list[tuple[str, dict[str, str]]], message: str,
) -> None:
    _factor(append_calendar_row, "f")
    for i, (name, extra) in enumerate(rows):
        append_calendar_row(
            date=f"202{7 + i}-02-28", kind="set_rate", name=name, series="Asset_Checking",
            amount="45000", vary_target="amount", vary_distribution="uniform",
            vary_low="40000", vary_high="50000", **extra,
        )
    assert any(message in issue for issue in _issues(load_test_copy)), _issues(load_test_copy)


def test_correlation_on_a_literal_row_is_rejected(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    _factor(append_calendar_row, "f")
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="fixed", series="Asset_Checking",
        amount="45000", vary_correlate_with="f", vary_correlation="0.5",
    )
    assert any("only valid on a varying row" in i for i in _issues(load_test_copy))


@pytest.mark.parametrize(("extra", "message"), [
    ({"vary_distribution": "uniform"}, "has no vary_distribution"),
    ({"amount": "100"}, "has no amount"),
    ({"series": "cpi"}, "has no series"),
])
def test_latent_factor_carries_nothing_but_a_name(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    extra: dict[str, str], message: str,
) -> None:
    _factor(append_calendar_row, "f", **extra)
    _varying_rate(append_calendar_row, "rate_a", vary_correlate_with="f", vary_correlation="0.5")
    assert any(message in i for i in _issues(load_test_copy))


def test_latent_factor_needs_a_name(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    append_calendar_row(kind="latent_factor")
    assert any("needs a name" in i for i in _issues(load_test_copy))


def test_unused_factor_is_a_warning_not_an_error(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    _factor(append_calendar_row, "someday")
    result = load_test_copy()
    assert len(result.warnings) == 1 and "someday" in result.warnings[0]
    assert "no dependents" in result.warnings[0]


def test_engine_treats_a_factor_as_inert(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    """An ordinary (non-recipe) scenario with a factor runs normally, and
    the factor isn't reported as an unsupported calendar kind."""
    _factor(append_calendar_row, "someday")
    run_path = tmp_path / "run.sqlite3"
    run_scenario(load_test_copy().intent_path, run_path, run_by="test", allow_dirty_data=True)
    con = sqlite3.connect(run_path)
    try:
        status, run_args = con.execute("SELECT status, run_args_json FROM simulation_run").fetchone()
    finally:
        con.close()
    assert status == "succeeded"
    assert "latent_factor" not in json.loads(run_args or "{}").get("skipped_calendar_kinds", {})


def test_cli_prints_a_warning_once(
    append_calendar_row: Callable[..., None], test_data_dir: Path,
    intent_dir: Path, runs_dir: Path,
) -> None:
    """`sim check` prints WARN: lines and exits 0 (ADR-004 §6); `sim load`,
    which goes on to run (and so loads again), prints each warning once."""
    _factor(append_calendar_row, "someday")
    runner = CliRunner()
    check = runner.invoke(main, ["sim", "check", "retire_alice", "--data-dir", str(test_data_dir)])
    assert check.exit_code == 0, check.output
    assert check.output.count("WARN:") == 1 and "someday" in check.output
    load = runner.invoke(main, [
        "sim", "load", "retire_alice", "--data-dir", str(test_data_dir),
        "--intent-dir", str(intent_dir), "--runs-dir", str(runs_dir), "--allow-dirty-data",
    ])
    assert load.exit_code == 0, load.output
    assert load.output.count("WARN:") == 1


# --- Phase 1: the copula math (pure) -----------------------------------------------

MEMBERS = 5_000

# economy -> inflation -> {rate_a, rate_b}; economy -> stocks (anti-correlated)
TREE = {
    "inflation": Link("economy", 0.8),
    "rate_a": Link("inflation", 0.9),
    "rate_b": Link("inflation", 0.6),
    "stocks": Link("economy", -0.5),
}


def _spearman(a: pd.Series, b: pd.Series) -> float:
    """Spearman's rho as Pearson on ranks (pandas' method="spearman" needs scipy)."""
    return a.rank().corr(b.rank())


def _simulate(links: dict[str, Link], members: int = MEMBERS, seed: int = 11) -> pd.DataFrame:
    """Scores per member, one independent noise draw per node per member."""
    rng = random.Random(seed)
    nodes = sorted(tree_members(links))  # set order varies with PYTHONHASHSEED
    rows = [resolve_scores(links, {n: standard_normal(rng) for n in nodes})
            for _ in range(members)]
    return pd.DataFrame(rows)


def test_topological_order_puts_parents_first() -> None:
    order = topological_order(TREE)
    assert order[0] == "economy"
    for child, link in TREE.items():
        assert order.index(link.parent) < order.index(child)
    assert tree_members(TREE) == {"economy", "inflation", "rate_a", "rate_b", "stocks"}


def test_topological_order_rejects_a_cycle() -> None:
    with pytest.raises(CorrelationError, match="cycle"):
        topological_order({"a": Link("b", 0.5), "b": Link("a", 0.5)})


def test_implied_correlation_multiplies_along_the_path() -> None:
    assert implied_correlation(TREE, "rate_a", "inflation") == pytest.approx(0.9)
    assert implied_correlation(TREE, "rate_a", "rate_b") == pytest.approx(0.9 * 0.6)
    assert implied_correlation(TREE, "rate_a", "economy") == pytest.approx(0.9 * 0.8)
    assert implied_correlation(TREE, "rate_a", "stocks") == pytest.approx(0.9 * 0.8 * -0.5)
    assert implied_correlation(TREE, "rate_a", "rate_a") == 1.0
    assert implied_correlation({**TREE, "other": Link("elsewhere", 0.5)}, "rate_a", "other") == 0.0


def test_scores_are_standard_normal_and_correlate_as_implied() -> None:
    scores = _simulate(TREE)
    for node in scores.columns:  # each score is standard normal on its own
        assert abs(scores[node].mean()) < 0.05 and abs(scores[node].std() - 1) < 0.05
    for a, b in [("rate_a", "inflation"), ("rate_a", "rate_b"), ("rate_b", "stocks"),
                 ("inflation", "economy")]:
        assert scores[a].corr(scores[b]) == pytest.approx(implied_correlation(TREE, a, b), abs=0.03)


@pytest.mark.parametrize("dist", [
    Uniform(40_000, 50_000),
    TruncatedNormal(median=45_000, sd=5_000, clip_low=38_000, clip_high=70_000, skew=0.6),
    Triangular(40_000, 42_000, 60_000),
])
def test_each_rows_authored_distribution_is_preserved(dist) -> None:  # type: ignore[no-untyped-def]
    """A correlated child's values follow its own distribution exactly: its
    empirical quantiles match dist.quantile, and it never leaves its bounds."""
    scores = _simulate(TREE)
    values = scores["rate_a"].map(lambda z: dist.value_at(probability(z)))
    for q in (0.1, 0.25, 0.5, 0.75, 0.9):
        assert (values <= dist.quantile(q)).mean() == pytest.approx(q, abs=0.02)
    assert values.min() >= dist.quantile(0) and values.max() <= dist.quantile(1)


def test_continuous_values_rank_correlate_as_expected() -> None:
    """For continuous values the Spearman correlation of the *values* is
    (6/pi)·asin(rho/2), whatever the two distributions (ADR-008 §1)."""
    scores = _simulate(TREE)
    skewed = TruncatedNormal(median=6, sd=2, clip_low=0, clip_high=20, skew=0.8)
    flat = Uniform(0, 1)
    a = scores["rate_a"].map(lambda z: skewed.value_at(probability(z)))
    b = scores["rate_b"].map(lambda z: flat.value_at(probability(z)))
    expected = expected_rank_correlation(implied_correlation(TREE, "rate_a", "rate_b"))
    assert _spearman(a, b) == pytest.approx(expected, abs=0.03)


@pytest.mark.parametrize("rho", [1.0, -1.0, 0.0])
def test_extreme_and_zero_correlations(rho: float) -> None:
    scores = _simulate({"child": Link("parent", rho)})
    observed = _spearman(scores["child"], scores["parent"])
    if rho:
        assert observed == pytest.approx(rho)  # the child is a monotone function of the parent
    else:
        assert abs(observed) < 0.05


def test_expected_rank_correlation_stays_near_rho() -> None:
    assert max(abs(r / 100 - expected_rank_correlation(r / 100)) for r in range(-100, 101)) < 0.02
    assert expected_rank_correlation(0.6) == pytest.approx(0.582, abs=1e-3)


def test_uniform_date_value_at_int_weights_every_day_equally() -> None:
    """A correlated uniform date matches draw_int: each whole day equally
    likely (not quantile_int's half-weight end days)."""
    dist = Uniform(738_000, 738_002)  # three days
    counts = pd.Series([dist.value_at_int(i / 30_000 + 1 / 60_000) for i in range(30_000)]).value_counts()
    assert sorted(counts.index) == [738_000, 738_001, 738_002]
    assert counts.min() / counts.max() > 0.99
    assert dist.value_at_int(1.0) == 738_002 and dist.value_at_int(0.0) == 738_000


def test_triangular_date_value_at_int_rounds_like_draw_int() -> None:
    dist = Triangular(738_000, 738_030, 738_300)
    assert dist.value_at_int(0.5) == round(dist.quantile(0.5))
    assert isinstance(dist.value_at_int(0.3), int)


def test_standard_normal_is_standard() -> None:
    rng = random.Random(3)
    draws = [standard_normal(rng) for _ in range(MEMBERS)]
    mean = sum(draws) / MEMBERS
    sd = math.sqrt(sum((d - mean) ** 2 for d in draws) / MEMBERS)
    assert abs(mean) < 0.05 and abs(sd - 1) < 0.05


# --- Phase 2: resolve_member integration and provenance -----------------------------


def _resolve(recipe: Path, out: Path, member_index: int, *, seed: int = 21, **kwargs) -> dict:  # type: ignore[no-untyped-def]
    """Resolve one member; ``{name: (vary_target, drawn_value, z)}`` from its
    ensemble_draw, plus its calendar rows by name."""
    from analysis.sim.ensemble import resolve_member

    member = resolve_member(recipe, out / f"member_{member_index:04d}.sqlite3", seed=seed,
                            member_index=member_index, member_count=999,
                            recipe_tree_id="deadbeef", **kwargs)
    con = sqlite3.connect(member)
    try:
        draws = {n: (t, v, z) for n, t, v, z in con.execute(
            "SELECT calendar_event_name, vary_target, drawn_value, z FROM ensemble_draw")}
        calendar = {n: (d, a) for n, d, a in con.execute(
            "SELECT name, date, amount FROM calendar_event WHERE name IS NOT NULL")}
    finally:
        con.close()
    return {"draws": draws, "calendar": calendar}


def _correlated_recipe(append_calendar_row: Callable[..., None], *, correlated: bool = True) -> None:
    """A factor with a correlated rate and a correlated date under it, plus an
    independent rate and an independent date outside every tree."""
    link = (lambda parent, rho: {"vary_correlate_with": parent, "vary_correlation": rho}) \
        if correlated else (lambda parent, rho: {})
    if correlated:
        _factor(append_calendar_row, "economy")
    append_calendar_row(
        date="2027-02-28", kind="set_rate", name="tied_rate", series="Asset_Checking",
        amount="45000", vary_target="amount", vary_distribution="truncated_normal",
        vary_median="45000", vary_sd="5000", vary_low="38000", vary_high="70000",
        vary_skew="0.5", **link("economy", "0.8"),
    )
    append_calendar_row(
        date="2030-12-31", kind="retire_scheduled", name="tied_date", scheduled_name="salary_bill",
        vary_target="date", vary_distribution="uniform", vary_low="2030-06-30",
        vary_high="2031-06-30", **link("economy", "-0.6"),
    )
    append_calendar_row(
        date="2028-02-29", kind="set_rate", name="free_rate", series="Asset_Checking",
        amount="45000", vary_target="amount", vary_distribution="uniform",
        vary_low="40000", vary_high="50000",
    )
    append_calendar_row(
        date="2028-06-30", kind="retire_scheduled", name="free_date", scheduled_name="salary_alice",
        vary_target="date", vary_distribution="uniform", vary_low="2028-03-31",
        vary_high="2028-09-30",
    )


def test_rows_outside_every_tree_draw_exactly_as_before(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    """ADR-008 §5: an untouched row's draw is the same call on the same keyed
    stream as before correlation existed -- draw() for an amount, draw_int()
    (randint) for a uniform date."""
    from datetime import date

    from analysis.sim.distributions import member_rng

    _correlated_recipe(append_calendar_row)
    recipe = load_test_copy().intent_path
    for i in (1, 2, 3):
        draws = _resolve(recipe, tmp_path, i)["draws"]
        rate = round(Uniform(40_000, 50_000).draw(member_rng(21, i, "free_rate")))
        day = Uniform(date(2028, 3, 31).toordinal(), date(2028, 9, 30).toordinal()).draw_int(
            member_rng(21, i, "free_date"))
        assert draws["free_rate"] == ("amount", str(rate), None)
        assert draws["free_date"] == ("date", date.fromordinal(day).isoformat(), None)


def test_member_zero_is_unchanged_by_correlation(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    test_data_dir: Path, tmp_path: Path,
) -> None:
    _correlated_recipe(append_calendar_row)
    correlated = _resolve(load_test_copy().intent_path, tmp_path / "c", 0)
    assert all(z == 0.0 for _t, _v, z in
               (correlated["draws"][n] for n in ("tied_rate", "tied_date", "economy")))
    assert correlated["draws"]["tied_rate"][1] == "45000"  # the authored median
    # The same recipe without its correlation columns resolves member 0 identically.
    path = test_data_dir / "scenarios" / "retire_alice" / "calendar.csv"
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df = df[df["kind"] != "latent_factor"]
    df["vary_correlate_with"] = ""
    df["vary_correlation"] = ""
    df.to_csv(path, index=False)
    plain = _resolve(load_test_copy().intent_path, tmp_path / "p", 0)
    for name in ("tied_rate", "tied_date", "free_rate", "free_date"):
        assert correlated["calendar"][name] == plain["calendar"][name]


def test_correlated_rows_record_scores_and_stay_in_bounds(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    _correlated_recipe(append_calendar_row)
    recipe = load_test_copy().intent_path
    for i in range(1, 30):
        draws = _resolve(recipe, tmp_path, i)["draws"]
        target, value, z = draws["tied_rate"]
        assert z is not None and 38_000 <= int(value) <= 70_000
        assert "2030-06-30" <= draws["tied_date"][1] <= "2031-06-30"
        assert draws["economy"][0] == "factor" and float(draws["economy"][1]) == draws["economy"][2]


def test_scores_correlate_as_specified_across_members(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    """Over 200 resolved members, the recorded scores correlate at rho (and
    the two siblings at the product), checked on z, which never ties."""
    _correlated_recipe(append_calendar_row)
    recipe = load_test_copy().intent_path
    rows = []
    for i in range(1, 201):
        draws = _resolve(recipe, tmp_path, i)["draws"]
        rows.append({n: draws[n][2] for n in ("economy", "tied_rate", "tied_date")})
    scores = pd.DataFrame(rows)
    assert scores["tied_rate"].corr(scores["economy"]) == pytest.approx(0.8, abs=0.1)
    assert scores["tied_date"].corr(scores["economy"]) == pytest.approx(-0.6, abs=0.12)
    assert scores["tied_rate"].corr(scores["tied_date"]) == pytest.approx(-0.48, abs=0.12)


def test_an_unrelated_tree_does_not_change_existing_draws(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    """Keyed streams (ADR-006 §3): adding another factor and row elsewhere
    leaves an existing tree's draws untouched."""
    _correlated_recipe(append_calendar_row)
    before = _resolve(load_test_copy().intent_path, tmp_path / "b", 5)["draws"]
    _factor(append_calendar_row, "markets")
    append_calendar_row(
        date="2029-02-28", kind="set_rate", name="other_rate", series="Asset_Checking",
        amount="45000", vary_target="amount", vary_distribution="uniform", vary_low="40000",
        vary_high="50000", vary_correlate_with="markets", vary_correlation="0.5",
    )
    after = _resolve(load_test_copy().intent_path, tmp_path / "a", 5)["draws"]
    for name in ("economy", "tied_rate", "tied_date", "free_rate", "free_date"):
        assert after[name] == before[name]


def test_oat_moves_only_the_overridden_row(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    """OAT stays "this row alone" (ADR-008 §5): overriding the factor's child
    records its score, and its sibling stays central at z = 0."""
    from statistics import NormalDist

    _correlated_recipe(append_calendar_row)
    draws = _resolve(load_test_copy().intent_path, tmp_path, 1, seed=None,
                     overrides={"tied_rate": 0.9}, mode="sensitivity")["draws"]
    assert draws["tied_rate"][2] == pytest.approx(NormalDist().inv_cdf(0.9))
    assert draws["tied_date"][2] == 0.0 and draws["economy"][2] == 0.0


def test_correlated_ramp_lands_on_its_drawn_target(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    _factor(append_calendar_row, "economy")
    append_calendar_row(
        date="2027-06-30", kind="set_rate", name="ramped", series="Asset_Checking",
        amount="45000", vary_target="amount", vary_distribution="uniform",
        vary_low="50000", vary_high="70000", vary_ramp_months="3",
        vary_correlate_with="economy", vary_correlation="0.7",
    )
    recipe = load_test_copy().intent_path
    from analysis.sim.ensemble import resolve_member

    member = resolve_member(recipe, tmp_path / "m.sqlite3", seed=21, member_index=4,
                            member_count=9, recipe_tree_id="deadbeef")
    con = sqlite3.connect(member)
    try:
        target = int(con.execute(
            "SELECT drawn_value FROM ensemble_draw WHERE calendar_event_name = 'ramped'"
        ).fetchone()[0])
        last = con.execute(
            "SELECT amount FROM calendar_event WHERE series = 'Asset_Checking' AND kind = 'set_rate' "
            "AND date >= '2027-06-30' ORDER BY date DESC LIMIT 1"
        ).fetchone()[0]
    finally:
        con.close()
    assert 50_000 <= target <= 70_000 and last == target


# --- Phase 3: authoring and reporting --------------------------------------------


def test_cli_adds_a_factor_and_a_correlated_row(test_data_dir: Path) -> None:
    runner = CliRunner()
    factor = runner.invoke(main, [
        "sim", "calendar", "add", "latent-factor", "economy", "--data-dir", str(test_data_dir),
        "--description", "the broad economy",
    ])
    assert factor.exit_code == 0, factor.output
    assert "WARN:" in factor.output and "no dependents" in factor.output  # not yet used
    rate = runner.invoke(main, [
        "sim", "calendar", "add", "set-rate", "2027-02-28", "Asset_Checking", "4.5",
        "--data-dir", str(test_data_dir), "--name", "rate_vary",
        "--vary-target", "amount", "--vary-distribution", "uniform",
        "--vary-low", "4.0", "--vary-high", "5.0",
        "--vary-correlate-with", "economy", "--vary-correlation", "0.7",
    ])
    assert rate.exit_code == 0, rate.output
    assert "WARN:" not in rate.output  # the factor has a dependent now
    rows = pd.read_csv(test_data_dir / "scenarios" / "retire_alice" / "calendar.csv",
                       dtype=str, keep_default_na=False).set_index("name")
    assert rows.loc["economy", "kind"] == "latent_factor"
    assert rows.loc["rate_vary", ["vary_correlate_with", "vary_correlation"]].tolist() == ["economy", "0.7"]


@pytest.mark.parametrize(("extra", "message"), [
    (["--vary-correlate-with", "economy"], "go together"),
    (["--vary-correlate-with", "economy", "--vary-correlation", "2"], "between -1 and 1"),
])
def test_cli_rejects_bad_correlation_options(
    test_data_dir: Path, extra: list[str], message: str,
) -> None:
    result = CliRunner().invoke(main, [
        "sim", "calendar", "add", "set-rate", "2027-02-28", "Asset_Checking", "4.5",
        "--data-dir", str(test_data_dir), "--name", "rate_vary",
        "--vary-target", "amount", "--vary-distribution", "uniform",
        "--vary-low", "4.0", "--vary-high", "5.0", *extra,
    ])
    assert result.exit_code != 0 and message in result.output


def test_cli_rejects_correlation_without_a_distribution(test_data_dir: Path) -> None:
    result = CliRunner().invoke(main, [
        "sim", "calendar", "add", "set-rate", "2027-02-28", "Asset_Checking", "4.5",
        "--data-dir", str(test_data_dir),
        "--vary-correlate-with", "economy", "--vary-correlation", "0.5",
    ])
    assert result.exit_code != 0 and "require --vary-distribution" in result.output


@pytest.fixture
def correlated_mc_batch(
    append_calendar_row: Callable[..., None], test_data_dir: Path,
    intent_dir: Path, runs_dir: Path,
) -> Path:
    """A 6-member Monte Carlo batch of the correlated recipe."""
    from analysis.sim.loader import latest_ensemble_batch

    _correlated_recipe(append_calendar_row)
    runner = CliRunner()
    load = runner.invoke(main, [
        "sim", "load", "retire_alice", "--data-dir", str(test_data_dir),
        "--intent-dir", str(intent_dir), "--allow-dirty-data", "--no-run",
    ])
    assert load.exit_code == 0, load.output
    con = sqlite3.connect(intent_dir / "retire_alice.sqlite3")
    con.execute("UPDATE scenario SET seed = 5")
    con.commit()
    con.close()
    run = runner.invoke(main, [
        "sim", "run", "retire_alice", "--intent-dir", str(intent_dir),
        "--runs-dir", str(runs_dir), "--ensemble-size", "6", "--allow-dirty-data",
    ])
    assert run.exit_code == 0 and "6/6 member(s) succeeded" in run.output, run.output
    batch = latest_ensemble_batch(runs_dir / "retire_alice")
    assert batch is not None
    return batch


def test_parameters_show_parents_rho_and_factors(correlated_mc_batch: Path) -> None:
    from analysis.sim import sensitivity

    table = sensitivity.parameters(correlated_mc_batch)
    assert table.loc["tied_rate", ["correlate_with", "rho"]].tolist() == ["economy", "+0.8"]
    assert table.loc["tied_date", ["correlate_with", "rho"]].tolist() == ["economy", "-0.6"]
    assert table.loc["free_rate", ["correlate_with", "rho"]].tolist() == ["", ""]
    assert table.loc["economy", "kind"] == "latent_factor"
    assert table.loc["economy", "target"] == "factor"
    assert table.loc["economy", "nu"] == ""  # a Gaussian tree


def test_monte_carlo_report_ranks_by_srrc_and_shows_factors_and_scores(
    correlated_mc_batch: Path, runs_dir: Path,
) -> None:
    """#38: the factor isn't a regressor, but gets an informational line; a
    correlated batch is ranked and pinned by SRRC and checks its scores."""
    from analysis.sim import sensitivity
    from analysis.sim.ensemble import load_ensemble

    connections = load_ensemble(correlated_mc_batch)
    try:
        assert "economy" not in sensitivity.draws_frame(connections).columns
    finally:
        for rc in connections.values():
            rc.close()
    assert sensitivity.has_correlated_rows(correlated_mc_batch)
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "sensitivity", "retire_alice", "--runs-dir", str(runs_dir),
        "--mode", "monte_carlo",  # default years: 2035 doesn't vary in this recipe yet
    ])
    assert result.exit_code == 0, result.output
    assert "rows are correlated" in result.output
    assert "ranked and pinned by SRRC" in result.output
    assert "status (srrc^2 vs 5%, every year):" in result.output
    assert "economy (factor)" in result.output
    assert "correlations achieved (rho from the scores' Kendall tau) vs expected:" in result.output
    assert "tied_rate ~ economy" in result.output or "economy ~ tied_rate" in result.output
    report = sensitivity.mc_report(correlated_mc_batch)
    assert report.correlated and report.by == "srrc"
    assert set(report.table.loc[report.table["factor"], "row"]) == {"economy"}
    pairs = report.scores.set_index(["a", "b"])
    assert pairs.loc[("tied_date", "tied_rate"), "kind"] == "implied"
    assert pairs.loc[("tied_date", "tied_rate"), "expected"] == pytest.approx(0.8 * -0.6)
    assert "free_rate" not in set(report.scores["a"]) | set(report.scores["b"])


def test_uncorrelated_batch_has_no_correlation_note(mc_batch: Path) -> None:
    from analysis.sim import sensitivity

    assert not sensitivity.has_correlated_rows(mc_batch)


def test_an_unused_factor_still_draws_and_is_recorded(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    """PR #37 review: a factor with neither parent nor dependents (allowed,
    with a warning) still resolves to its own draw and gets provenance, and
    its presence doesn't disturb any row."""
    from analysis.sim.distributions import member_rng

    _correlated_recipe(append_calendar_row)
    before = _resolve(load_test_copy().intent_path, tmp_path / "b", 3)["draws"]
    _factor(append_calendar_row, "someday")
    recipe = load_test_copy().intent_path
    after = _resolve(recipe, tmp_path / "a", 3)["draws"]
    expected = standard_normal(member_rng(21, 3, "someday"))
    assert after["someday"] == ("factor", repr(expected), expected)
    assert {n: v for n, v in after.items() if n != "someday"} == before
    assert _resolve(recipe, tmp_path / "z", 0)["draws"]["someday"][2] == 0.0


def _small_mc_batch(test_data_dir: Path, intent_dir: Path, runs_dir: Path) -> Path:
    """Load the private retire_alice copy (after a test's calendar edits) and
    run a 3-member Monte Carlo batch of it."""
    from analysis.sim.loader import latest_ensemble_batch

    runner = CliRunner()
    load = runner.invoke(main, [
        "sim", "load", "retire_alice", "--data-dir", str(test_data_dir),
        "--intent-dir", str(intent_dir), "--allow-dirty-data", "--no-run",
    ])
    assert load.exit_code == 0, load.output
    con = sqlite3.connect(intent_dir / "retire_alice.sqlite3")
    con.execute("UPDATE scenario SET seed = 5")
    con.commit()
    con.close()
    run = runner.invoke(main, [
        "sim", "run", "retire_alice", "--intent-dir", str(intent_dir),
        "--runs-dir", str(runs_dir), "--ensemble-size", "3", "--allow-dirty-data",
    ])
    assert run.exit_code == 0, run.output
    batch = latest_ensemble_batch(runs_dir / "retire_alice")
    assert batch is not None
    return batch


@pytest.mark.parametrize("case", ["unused factor", "lone child", "zero rho"])
def test_independent_inputs_are_not_marked_correlated(
    append_calendar_row: Callable[..., None], test_data_dir: Path,
    intent_dir: Path, runs_dir: Path, case: str,
) -> None:
    """PR #37 review: the note needs two modeled rows that actually depend on
    each other -- not an unused factor's provenance, a lone row under a
    factor, or a link whose rho is 0."""
    from analysis.sim import sensitivity

    _factor(append_calendar_row, "f")
    if case == "lone child":
        _varying_rate(append_calendar_row, "tied", vary_correlate_with="f", vary_correlation="0.8")
    elif case == "zero rho":
        _varying_rate(append_calendar_row, "tied", vary_correlate_with="f", vary_correlation="0.8")
        append_calendar_row(
            date="2028-02-29", kind="set_rate", name="untied", series="Asset_Checking",
            amount="45000", vary_target="amount", vary_distribution="uniform",
            vary_low="40000", vary_high="50000", vary_correlate_with="f", vary_correlation="0",
        )
    append_calendar_row(
        date="2029-02-28", kind="set_rate", name="free_rate", series="Asset_Checking",
        amount="45000", vary_target="amount", vary_distribution="uniform",
        vary_low="40000", vary_high="50000",
    )
    batch = _small_mc_batch(test_data_dir, intent_dir, runs_dir)
    assert not sensitivity.has_correlated_rows(batch)


def test_two_dependent_rows_mark_a_batch_correlated(
    append_calendar_row: Callable[..., None], test_data_dir: Path,
    intent_dir: Path, runs_dir: Path,
) -> None:
    from analysis.sim import sensitivity

    _factor(append_calendar_row, "f")
    _varying_rate(append_calendar_row, "a", vary_correlate_with="f", vary_correlation="0.8")
    append_calendar_row(
        date="2028-02-29", kind="set_rate", name="b", series="Asset_Checking", amount="45000",
        vary_target="amount", vary_distribution="uniform", vary_low="40000", vary_high="50000",
        vary_correlate_with="f", vary_correlation="0.5",
    )
    assert sensitivity.has_correlated_rows(_small_mc_batch(test_data_dir, intent_dir, runs_dir))


@pytest.mark.parametrize(("column", "value"), [("params_json", '{"x": 1}'), ("tags", "role:x")])
def test_latent_factor_rejects_payload_and_tags(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    column: str, value: str,
) -> None:
    """ADR-003 §3.10: a factor carries only a name, a description and the
    correlation pair (PR #37 review: params_json was missing from the check)."""
    _factor(append_calendar_row, "f", **{column: value})
    _varying_rate(append_calendar_row, "rate_a", vary_correlate_with="f", vary_correlation="0.5")
    assert any(f"has no {column}" in i for i in _issues(load_test_copy))


# --- #39: one-at-a-time perturbation of latent factors ------------------------------


def test_conditional_scores_multiply_rho_down_the_family() -> None:
    """Each descendant at its conditional median given the node: the product
    of rho along the path, times the node's score. Other trees, and the
    node's own ancestors, aren't included."""
    from analysis.sim.correlation import conditional_scores

    links = {
        "a": Link("f", 0.5), "b": Link("a", 0.4), "c": Link("f", -0.6),
        "f": Link("top", 0.9), "x": Link("other", 0.7),
    }
    assert conditional_scores(links, "f", 2.0) == pytest.approx({"a": 1.0, "b": 0.4, "c": -1.2})
    assert conditional_scores(links, "b", 2.0) == {}


def _quantiles(member: Path) -> dict[str, float | None]:
    con = sqlite3.connect(member)
    try:
        return dict(con.execute("SELECT calendar_event_name, quantile FROM ensemble_draw"))
    finally:
        con.close()


def test_oat_factor_moves_its_family_to_conditional_medians(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    """ADR-007 §2: perturbing a factor sets its score to Phi^-1(q) and moves
    every descendant to its conditional median, through its own value_at;
    rows outside the tree stay central, and only the factor records a
    quantile, so the report knows which node the member moved."""
    from datetime import date
    from statistics import NormalDist

    _correlated_recipe(append_calendar_row)
    recipe = load_test_copy().intent_path
    z = NormalDist().inv_cdf(0.9)
    resolved = _resolve(recipe, tmp_path, 1, seed=None, overrides={"economy": 0.9},
                        mode="sensitivity")
    draws = resolved["draws"]
    assert draws["economy"] == ("factor", repr(z), z)
    assert draws["tied_rate"][2] == pytest.approx(0.8 * z)
    assert draws["tied_date"][2] == pytest.approx(-0.6 * z)
    rate = TruncatedNormal(45_000, 5_000, 38_000, 70_000, skew=0.5)
    assert draws["tied_rate"][1] == str(round(rate.value_at(probability(0.8 * z))))
    day = Uniform(date(2030, 6, 30).toordinal(), date(2031, 6, 30).toordinal()).value_at_int(
        probability(-0.6 * z))
    assert draws["tied_date"][1] == date.fromordinal(day).isoformat()
    # A higher-than-median rate and an earlier-than-median date: the family moved.
    assert int(draws["tied_rate"][1]) > 45_000 and draws["tied_date"][1] < "2030-12-30"
    base = _resolve(recipe, tmp_path / "base", 0, seed=None, overrides={}, mode="sensitivity")
    for name in ("free_rate", "free_date"):
        assert draws[name] == base["draws"][name]
    quantiles = _quantiles(tmp_path / "member_0001.sqlite3")
    assert quantiles == {"economy": 0.9, "tied_rate": None, "tied_date": None,
                         "free_rate": None, "free_date": None}


def test_oat_plan_sweeps_factors_that_move_something(tmp_path: Path) -> None:
    from analysis.sim.ensemble import EnsembleError, oat_plan

    plan = oat_plan(["r1", "r2"], (0.1, 0.9), factors={"economy": 2, "someday": 0})
    assert plan == [{}, {"economy": 0.1}, {"economy": 0.9}, {"r1": 0.1}, {"r1": 0.9},
                    {"r2": 0.1}, {"r2": 0.9}]
    assert oat_plan(["r1"], (0.1, 0.9), ["economy"], {"economy": 2}) == [
        {}, {"economy": 0.1}, {"economy": 0.9}]
    with pytest.raises(EnsembleError, match="no varying row depends on"):
        oat_plan(["r1"], (0.1, 0.9), ["someday"], {"someday": 0})
    with pytest.raises(EnsembleError, match="no varying row or latent factor"):
        oat_plan(["r1"], (0.1, 0.9), ["econmy"], {"economy": 2})  # cspell:ignore econmy


def test_factor_reach_counts_varying_descendants(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    """Through a sub-factor, too; an unused factor reaches nothing."""
    from analysis.sim.ensemble import factor_reach

    _correlated_recipe(append_calendar_row)
    _factor(append_calendar_row, "regime")
    _factor(append_calendar_row, "inflation", vary_correlate_with="regime", vary_correlation="0.7")
    append_calendar_row(
        date="2029-02-28", kind="set_rate", name="cpi_rate", series="Asset_Checking",
        amount="45000", vary_target="amount", vary_distribution="uniform", vary_low="40000",
        vary_high="50000", vary_correlate_with="inflation", vary_correlation="0.9",
    )
    _factor(append_calendar_row, "someday")
    _factor(append_calendar_row, "inert")
    append_calendar_row(
        date="2029-05-31", kind="set_rate", name="unmoved_rate", series="Asset_Checking",
        amount="45000", vary_target="amount", vary_distribution="uniform", vary_low="40000",
        vary_high="50000", vary_correlate_with="inert", vary_correlation="0",
    )
    reach = factor_reach(load_test_copy().intent_path)
    # A row reached only through a zero rho isn't moved (PR #53 review).
    assert reach == {"economy": 2, "inert": 0, "inflation": 1, "regime": 1, "someday": 0}


@pytest.fixture
def factor_oat_batch(
    append_calendar_row: Callable[..., None], test_data_dir: Path,
    intent_dir: Path, runs_dir: Path,
) -> Path:
    """An OAT batch of the correlated recipe, perturbing the factor and one
    ordinary row (5 members)."""
    from analysis.sim.loader import latest_ensemble_batch

    _correlated_recipe(append_calendar_row)
    runner = CliRunner()
    load = runner.invoke(main, [
        "sim", "load", "retire_alice", "--data-dir", str(test_data_dir),
        "--intent-dir", str(intent_dir), "--allow-dirty-data", "--no-run",
    ])
    assert load.exit_code == 0, load.output
    run = runner.invoke(main, [
        "sim", "run", "retire_alice", "--sensitivity", "--only", "economy,free_rate",
        "--intent-dir", str(intent_dir), "--runs-dir", str(runs_dir), "--allow-dirty-data",
    ])
    assert run.exit_code == 0 and "5/5 member(s) succeeded" in run.output, run.output
    batch = latest_ensemble_batch(runs_dir / "retire_alice")
    assert batch is not None
    return batch


def test_factor_rows_are_labeled_and_left_out_of_share(
    factor_oat_batch: Path, runs_dir: Path,
) -> None:
    """A factor appears in the table as target 'factor', its share is over
    the ordinary rows' total (so theirs still sum to 1), it's never a pin
    candidate, and the CLI and parameters view label it."""
    from analysis.sim import sensitivity

    deltas = sensitivity.oat_deltas(factor_oat_batch)
    assert sensitivity.factor_rows(deltas) == {"economy"}
    table = sensitivity.oat_table(deltas, 2035)
    assert table.loc["economy", "target"] == "factor"
    assert table.loc["economy", "value@0.9"] == "z=+1.28"
    assert table.loc[table["target"] != "factor", "share"].sum() == pytest.approx(1.0)
    assert "economy" not in sensitivity.pin_candidates(deltas, negligible=0.999)
    assert sensitivity.rank_by_year(deltas).attrs["factors"] == {"economy"}
    assert sensitivity.parameters(factor_oat_batch).loc["economy", "value@0.1"] == "z=-1.28"
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "sensitivity", "retire_alice", "--runs-dir", str(runs_dir),
        "--years", "2035",
    ])
    assert result.exit_code == 0, result.output
    assert "economy (factor)" in result.output


def test_attribution_works_for_a_factor(factor_oat_batch: Path) -> None:
    """A factor member's delta decomposes exactly like a row's: nothing
    unattributed."""
    from analysis.sim import sensitivity

    by_q = sensitivity.oat_attribution(factor_oat_batch, "economy")
    assert sorted(by_q) == [0.1, 0.9]
    for table in by_q.values():
        assert (table[sensitivity.UNATTRIBUTED] == 0).all()


def test_conditional_scores_at_an_endpoint_respect_a_zero_rho() -> None:
    """PR #53 review: an infinite (endpoint) score propagates as +/-inf, and
    a zero rho leaves its child at its median rather than 0 * inf = nan."""
    from analysis.sim.correlation import conditional_scores

    links = {"a": Link("f", 0.5), "b": Link("f", 0.0), "c": Link("f", -0.3)}
    assert conditional_scores(links, "f", math.inf) == {"a": math.inf, "b": 0.0, "c": -math.inf}


def test_oat_factor_at_an_endpoint_puts_its_family_on_their_bounds(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    """PR #53 review: q = 1 is a valid quantile. The factor's family lands
    exactly on its conditional endpoint -- each row at its own bound -- not
    just short of it."""
    _correlated_recipe(append_calendar_row)
    draws = _resolve(load_test_copy().intent_path, tmp_path, 1, seed=None,
                     overrides={"economy": 1.0}, mode="sensitivity")["draws"]
    assert draws["economy"][2] == math.inf
    assert draws["tied_rate"][1] == "70000"  # the upper clip bound (rho +0.8)
    assert draws["tied_date"][1] == "2030-06-30"  # the earliest date (rho -0.6)


def test_factor_share_survives_a_zero_ordinary_total() -> None:
    """PR #53 review: rows whose own impact is zero (say, they only matter
    jointly) mustn't make a factor's real impact read as a zero share."""
    from analysis.sim import sensitivity

    records = []
    for row, target, d in (("f", sensitivity.FACTOR_TARGET, 50_000.0), ("r", "amount", 0.0)):
        for q, sign in ((0.1, -1), (0.9, 1)):
            records.append({"row": row, "target": target, "quantile": q, "value": "", "year": 2030,
                            "measure": "net_worth", "level": 1e6 + sign * d, "base": 1e6,
                            "delta": sign * d, "relative": sign * d / 1e6})
    table = sensitivity.oat_table(pd.DataFrame(records), 2030)
    assert table.loc["f", "share"] == pytest.approx(1.0)
    assert table.loc["r", "share"] == 0.0


@pytest.mark.parametrize("q", [-0.1, 1.1, math.nan])
def test_oat_rejects_an_out_of_range_factor_quantile(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path, q: float,
) -> None:
    """PR #53 review: a factor has no distribution to reject a bad quantile,
    so resolve_member must, rather than treat it as an endpoint."""
    from analysis.sim.ensemble import EnsembleError

    _correlated_recipe(append_calendar_row)
    with pytest.raises(EnsembleError, match="quantile must be in"):
        _resolve(load_test_copy().intent_path, tmp_path, 1, seed=None,
                 overrides={"economy": q}, mode="sensitivity")


def test_a_zero_rho_date_under_a_swept_factor_stays_exactly_at_the_base(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    """PR #53 review: a uniform date under a zero rho keeps central_int(),
    which can differ by a day from value_at_int(0.5), when a sibling makes
    the factor worth sweeping."""
    _correlated_recipe(append_calendar_row)
    # A two-day range: central_int() rounds the midpoint half to even (the
    # 1st here), while value_at_int(0.5) picks the second of the two days.
    append_calendar_row(
        date="2030-01-01", kind="retire_scheduled", name="flat_date", scheduled_name="salary_alice",
        vary_target="date", vary_distribution="uniform", vary_low="2030-01-01",
        vary_high="2030-01-02", vary_correlate_with="economy", vary_correlation="0",
    )
    recipe = load_test_copy().intent_path
    base = _resolve(recipe, tmp_path / "base", 0, seed=None, overrides={}, mode="sensitivity")
    moved = _resolve(recipe, tmp_path, 1, seed=None, overrides={"economy": 0.9},
                     mode="sensitivity")
    assert moved["draws"]["flat_date"][1] == base["draws"]["flat_date"][1]
