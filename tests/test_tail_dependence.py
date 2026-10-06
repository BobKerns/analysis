"""#68: the Student-t copula -- a correlation tree's root sets
``vary_tail_dependence`` (nu), every score in the tree shares one scale per
member, and the t CDF maps scores to probabilities. Marginals stay exact;
joint extremes become likelier as nu falls."""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner
from scipy.stats import kendalltau, kstest
from scipy.stats import t as student_t

from analysis.cli import main
from analysis.sim import sensitivity
from analysis.sim.correlation import (
    Link,
    expected_kendall_tau,
    probability,
    resolve_scores,
    rho_from_kendall_tau,
    score_at,
    standard_normal,
    tail_scale,
    tree_root,
)
from analysis.sim.distributions import TruncatedNormal, member_rng, tail_rng
from analysis.sim.loader import LoadError, LoadResult

from tests.test_correlation import _correlated_recipe, _factor, _resolve, _varying_rate

SIBLINGS = {"a": Link("f", 0.6), "b": Link("f", 0.6)}


def _t_sample(
    links: dict[str, Link], nu: float | None, members: int = 20_000,
) -> pd.DataFrame:
    """Per member, each node's probability ``u`` under a tree with ``nu``
    (``None``: Gaussian). The noise and scale streams are fixed, so samples
    for different ``nu`` share their Gaussian scores."""
    rng, scale_rng = random.Random(5), random.Random(6)
    nodes = sorted({*links, *(link.parent for link in links.values())})
    rows = []
    for _ in range(members):
        scores = resolve_scores(links, {n: standard_normal(rng) for n in nodes})
        s = 1.0 if nu is None else tail_scale(nu, scale_rng)
        rows.append({n: probability(z * s, nu) for n, z in scores.items()})
    return pd.DataFrame(rows)


# --- the math (pure) -----------------------------------------------------------------


def test_probability_and_score_at_invert_each_other_under_t() -> None:
    for q in (0.01, 0.3, 0.5, 0.9):
        assert probability(score_at(q, 4.0), 4.0) == pytest.approx(q)
    assert score_at(0.9, 4.0) == pytest.approx(student_t.ppf(0.9, 4.0))
    assert score_at(0.9, 4.0) > score_at(0.9)  # t is wider than the normal


def test_tail_scale_has_the_inverse_chi2_mean() -> None:
    """``S^2 = nu / W`` with ``W ~ chi2(nu)`` has mean ``nu / (nu - 2)``."""
    rng = random.Random(3)
    squares = [tail_scale(5.0, rng) ** 2 for _ in range(40_000)]
    assert min(squares) > 0
    assert sum(squares) / len(squares) == pytest.approx(5 / 3, rel=0.05)


def test_tree_root_walks_to_the_top() -> None:
    links = {"inflation": Link("economy", 0.8), "rate": Link("inflation", 0.9)}
    assert tree_root(links, "rate") == "economy"
    assert tree_root(links, "economy") == "economy"
    assert tree_root(links, "elsewhere") == "elsewhere"


@pytest.mark.parametrize("rho", [-0.6, 0.0, 0.36, 0.9])
def test_kendall_tau_and_rho_are_inverses(rho: float) -> None:
    assert rho_from_kendall_tau(expected_kendall_tau(rho)) == pytest.approx(rho)


@pytest.mark.parametrize("nu", [None, 30.0, 3.0])
def test_every_marginal_stays_uniform(nu: float | None) -> None:
    """Each node's ``u`` is uniform under any nu, so each row keeps exactly
    its authored distribution."""
    sample = _t_sample(SIBLINGS, nu)
    for node in sample.columns:
        assert kstest(sample[node], "uniform").statistic < 0.015


def test_joint_extremes_get_likelier_as_nu_falls() -> None:
    """Both siblings above their 95th percentile at once: rarer under the
    Gaussian copula than under t, and more common as nu falls."""
    joint = [
        int(((sample["a"] > 0.95) & (sample["b"] > 0.95)).sum())
        for sample in (_t_sample(SIBLINGS, nu) for nu in (None, 30.0, 5.0, 3.0))
    ]
    assert joint == sorted(joint) and len(set(joint)) == 4, joint
    assert joint[-1] > 1.4 * joint[0], joint


def test_kendall_tau_still_follows_rho_under_t() -> None:
    """``(2/pi)·asin(rho)`` holds for every elliptical copula, so the
    siblings' implied 0.36 shows up in tau under t as under the Gaussian."""
    sample = _t_sample(SIBLINGS, 3.0, members=5_000)
    tau = kendalltau(sample["a"], sample["b"]).statistic
    assert tau == pytest.approx(expected_kendall_tau(0.36), abs=0.03)


def test_score_correlations_reads_rho_through_kendall_tau() -> None:
    """The achieved-correlation check holds up on heavy-tailed t scores,
    where a Pearson correlation would be dominated by a few members."""
    sample = _t_sample(SIBLINGS, 3.0, members=3_000)
    scores = sample.apply(lambda col: col.map(lambda u: score_at(u, 3.0)))
    pairs = sensitivity.score_correlations(scores, SIBLINGS, ["a", "b", "f"]).set_index(["a", "b"])
    assert not pairs["outside"].any()
    assert pairs.loc[("a", "b"), "achieved"] == pytest.approx(0.36, abs=0.05)


# --- loader --------------------------------------------------------------------------


def _issues(load: Callable[[], LoadResult]) -> list[str]:
    with pytest.raises(LoadError) as ei:
        load()
    return ei.value.issues


@pytest.mark.usefixtures("without_prototype_defaults")
def test_a_root_factor_carries_nu_into_the_intent(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    test_data_dir: Path,
) -> None:
    import sqlite3

    _correlated_recipe(append_calendar_row)
    _set_nu(test_data_dir, "economy", "4")
    result = load_test_copy()
    assert not result.warnings
    con = sqlite3.connect(result.intent_path)
    try:
        assert con.execute(
            "SELECT vary_tail_dependence FROM calendar_event WHERE name = 'economy'"
        ).fetchone() == (4.0,)
    finally:
        con.close()


def test_nu_below_the_root_is_rejected_naming_the_root(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    _factor(append_calendar_row, "economy")
    _factor(append_calendar_row, "inflation", vary_correlate_with="economy",
            vary_correlation="0.8", vary_tail_dependence="4")
    _varying_rate(append_calendar_row, "rate", vary_correlate_with="inflation",
                  vary_correlation="0.9")
    issues = _issues(load_test_copy)
    assert any(
        "inflation" in issue and "root of a correlation tree ('economy' here)" in issue
        for issue in issues
    ), issues


@pytest.mark.parametrize(("value", "message"), [
    ("0", "must be a number from 1 up"),
    ("0.001", "must be a number from 1 up"),  # its chi-square draws underflow
    ("-3", "must be a number from 1 up"),
    ("inf", "must be a number from 1 up"),
    ("heavy", "is not a number"),
])
def test_bad_nu_is_rejected(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    value: str, message: str,
) -> None:
    _factor(append_calendar_row, "economy", vary_tail_dependence=value)
    _varying_rate(append_calendar_row, "rate", vary_correlate_with="economy",
                  vary_correlation="0.9")
    assert any(message in issue for issue in _issues(load_test_copy))


def test_nu_on_a_literal_row_is_rejected(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    append_calendar_row(date="2027-02-28", kind="set_rate", series="Asset_Checking",
                        amount="45000", vary_tail_dependence="4")
    assert any("only valid on a varying row" in issue for issue in _issues(load_test_copy))


@pytest.mark.usefixtures("without_prototype_defaults")
def test_nu_on_a_lone_row_is_a_warning(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    _varying_rate(append_calendar_row, "rate", vary_tail_dependence="4")
    result = load_test_copy()
    assert len(result.warnings) == 1 and "rate" in result.warnings[0]
    assert "affects nothing yet" in result.warnings[0]


# --- resolution ----------------------------------------------------------------------


def _set_nu(test_data_dir: Path, name: str, nu: str) -> None:
    """Set ``name``'s vary_tail_dependence in the test copy's calendar.csv,
    as ``test_member_zero_is_unchanged_by_correlation`` edits its columns."""
    path = test_data_dir / "scenarios" / "retire_alice" / "calendar.csv"
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df.loc[df["name"] == name, "vary_tail_dependence"] = nu
    df.to_csv(path, index=False)


def test_a_t_tree_scales_its_scores_by_one_shared_draw(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    test_data_dir: Path,
    tmp_path: Path,
) -> None:
    """Each member's t-tree scores are its Gaussian scores times one scale
    from the root's own tail stream; rows outside the tree draw as before,
    and each value is its distribution's quantile at the t CDF."""
    import shutil

    _correlated_recipe(append_calendar_row)
    # A reload rewrites the same intent file, so keep the Gaussian one aside.
    gaussian = Path(shutil.copy(load_test_copy().intent_path, tmp_path / "gaussian.sqlite3"))
    _set_nu(test_data_dir, "economy", "4")
    t_recipe = load_test_copy().intent_path
    for i in (1, 2, 3):
        plain = _resolve(gaussian, tmp_path / f"g{i}", i)["draws"]
        heavy = _resolve(t_recipe, tmp_path / f"t{i}", i)["draws"]
        scale = tail_scale(4.0, tail_rng(21, i, "economy"))
        for name in ("economy", "tied_rate", "tied_date"):
            assert heavy[name][2] == pytest.approx(plain[name][2] * scale)
        for name in ("free_rate", "free_date"):
            assert heavy[name] == plain[name]
        rate = TruncatedNormal(45_000, 5_000, 38_000, 70_000, skew=0.5)
        assert heavy["tied_rate"][1] == str(round(rate.value_at(probability(heavy["tied_rate"][2], 4.0))))


def test_the_tail_stream_is_not_the_roots_own_noise() -> None:
    assert tail_rng(21, 1, "economy").random() != member_rng(21, 1, "economy").random()


def test_member_zero_is_the_median_under_t(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    test_data_dir: Path,
    tmp_path: Path,
) -> None:
    _correlated_recipe(append_calendar_row)
    _set_nu(test_data_dir, "economy", "4")
    draws = _resolve(load_test_copy().intent_path, tmp_path, 0)["draws"]
    assert all(draws[n][2] == 0.0 for n in ("economy", "tied_rate", "tied_date"))
    assert draws["tied_rate"][1] == "45000"


def test_oat_on_a_t_tree_factor_uses_the_t_quantile(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    test_data_dir: Path,
    tmp_path: Path,
) -> None:
    """The t copula is elliptical, so a descendant's conditional median is
    still the rho product times the factor's (now t) score."""
    _correlated_recipe(append_calendar_row)
    _set_nu(test_data_dir, "economy", "4")
    draws = _resolve(load_test_copy().intent_path, tmp_path, 1, seed=None,
                     overrides={"economy": 0.9}, mode="sensitivity")["draws"]
    z = student_t.ppf(0.9, 4.0)
    assert draws["economy"][2] == pytest.approx(z)
    assert draws["tied_rate"][2] == pytest.approx(0.8 * z)
    rate = TruncatedNormal(45_000, 5_000, 38_000, 70_000, skew=0.5)
    assert draws["tied_rate"][1] == str(round(rate.value_at(probability(0.8 * z, 4.0))))


# --- authoring -----------------------------------------------------------------------


def test_cli_writes_nu_on_a_factor(test_data_dir: Path) -> None:
    result = CliRunner().invoke(main, [
        "sim", "calendar", "add", "latent-factor", "economy", "--data-dir", str(test_data_dir),
        "--vary-tail-dependence", "4",
    ])
    assert result.exit_code == 0, result.output
    rows = pd.read_csv(test_data_dir / "scenarios" / "retire_alice" / "calendar.csv",
                       dtype=str, keep_default_na=False).set_index("name")
    assert rows.loc["economy", "vary_tail_dependence"] == "4"


@pytest.mark.parametrize(("extra", "message"), [
    (["--vary-correlate-with", "economy", "--vary-correlation", "0.5",
      "--vary-tail-dependence", "4"], "goes on the root of a correlation tree"),
    (["--vary-tail-dependence", "0.5"], "must be at least 1"),
    (["--vary-tail-dependence", str(math.nan)], "must be at least 1"),
])
def test_cli_rejects_bad_tail_dependence(
    test_data_dir: Path, extra: list[str], message: str,
) -> None:
    result = CliRunner().invoke(main, [
        "sim", "calendar", "add", "set-rate", "2027-02-28", "Asset_Checking", "4.5",
        "--data-dir", str(test_data_dir), "--name", "rate_vary",
        "--vary-target", "amount", "--vary-distribution", "uniform",
        "--vary-low", "4.0", "--vary-high", "5.0", *extra,
    ])
    assert result.exit_code != 0 and message in result.output


def test_guide_asks_a_root_factor_for_nu(test_data_dir: Path) -> None:
    result = CliRunner().invoke(
        main, ["sim", "calendar", "add", "--data-dir", str(test_data_dir)],
        input="latent-factor\neconomy\n\nn\n4\n\n",
    )
    assert result.exit_code == 0, result.output
    assert "--vary-tail-dependence 4" in result.output  # the equivalent command
    rows = pd.read_csv(test_data_dir / "scenarios" / "retire_alice" / "calendar.csv",
                       dtype=str, keep_default_na=False).set_index("name")
    assert rows.loc["economy", "vary_tail_dependence"] == "4"


@pytest.mark.parametrize("nu", [None, 3.0])
def test_score_correlation_intervals_keep_their_coverage(nu: float | None) -> None:
    """Over many correctly generated 150-member batches, the expected rho
    falls inside the 95% interval about 95% of the time, under the t copula
    as under the Gaussian: the interval's error comes from the scores, not
    from a Gaussian-only formula, which covers only about 90% at nu = 3."""
    import numpy as np
    from scipy.stats import chi2

    rng = np.random.default_rng(7)
    links = {"b": Link("a", 0.6)}
    batches, inside = 400, 0
    for _ in range(batches):
        noise = rng.standard_normal((150, 2))
        a = noise[:, 0]
        b = 0.6 * a + 0.8 * noise[:, 1]
        if nu is not None:
            scale = np.sqrt(nu / chi2.rvs(nu, size=150, random_state=rng))
            a, b = a * scale, b * scale
        pairs = sensitivity.score_correlations(pd.DataFrame({"a": a, "b": b}), links, ["a", "b"])
        inside += not pairs["outside"].iloc[0]
    assert 0.92 <= inside / batches <= 0.98, inside / batches


@pytest.mark.parametrize(("links", "t_roots", "dependent"), [
    ({"a": Link("f", 0.0), "b": Link("f", 0.0)}, {"f"}, True),   # one t tree, zero rho
    ({"a": Link("f", 0.0), "b": Link("f", 0.0)}, set(), False),  # the same tree, Gaussian
    ({"a": Link("f", 0.0), "b": Link("g", 0.0)}, {"f", "g"}, False),  # separate t trees
    ({"b": Link("a", 0.0)}, {"a"}, True),  # a t root and its zero-rho child
])
def test_rows_sharing_a_t_tree_are_dependent(
    links: dict[str, Link], t_roots: set[str], dependent: bool,
) -> None:
    """The shared scale ties a t tree's rows together even at zero implied
    rho, so the Monte Carlo report must rank them by SRRC; Gaussian zero-rho
    rows and rows in different trees stay independent."""
    assert sensitivity._any_dependent(links, ["a", "b"], t_roots) is dependent


def test_the_smallest_nu_draws_a_finite_scale_even_at_the_extreme() -> None:
    """At nu = 1, the floor, even the smallest uniform draw gives a positive
    chi-square and a finite scale, so no member's scores overflow."""
    from analysis.sim.registries import MIN_TAIL_DEPENDENCE

    class Smallest(random.Random):
        def random(self) -> float:
            return 2.0 ** -53

    scale = tail_scale(MIN_TAIL_DEPENDENCE, Smallest())
    assert math.isfinite(scale) and scale > 1e10
    assert 0.0 < probability(1.0 * scale, MIN_TAIL_DEPENDENCE) <= 1.0


def test_kendall_concordance_matches_the_pairwise_definition() -> None:
    """The O(n log n) sweep gives exactly what the pairwise sign sum does."""
    import numpy as np

    rng = np.random.default_rng(3)
    x = rng.standard_normal(200)
    y = 0.5 * x + rng.standard_normal(200)
    signs = np.sign(x[:, None] - x[None, :]) * np.sign(y[:, None] - y[None, :])
    concordance = signs.sum(axis=1) / 199
    tau, se = sensitivity._kendall_tau(x, y)
    assert tau == pytest.approx(concordance.mean(), abs=1e-12)
    assert tau == pytest.approx(kendalltau(x, y).statistic, abs=1e-12)
    assert se == pytest.approx(np.sqrt(4 * concordance.var(ddof=1) / 200), abs=1e-12)
