"""#261: a batch's draws, and what they depend on, in one database."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim.correlation import probability, tail_scale
from analysis.sim.distributions import Uniform, tail_rng
from analysis.sim.draws_db import build_draws_db, draws_db_path, draws_frames, ks_uniform
from analysis.sim.ensemble import build_distribution, resolve_member, write_batch_manifest
from analysis.sim.loader import LoadResult

from tests.test_correlation import _factor
from tests.test_rate_paths import _process_rate


def _query(path: Path, sql: str, *args: object) -> list[tuple]:
    con = sqlite3.connect(path)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def _resolve(recipe: Path, intent_batch: Path, count: int, seed: int = 21) -> None:
    for i in range(count):
        resolve_member(recipe, intent_batch / f"member_{i:04d}.sqlite3", seed=seed,
                       member_index=i, member_count=count, recipe_tree_id="deadbeef")


@pytest.fixture
def resolved_batch(tmp_path: Path) -> Callable[[Path, int], Path]:
    """Resolve ``count`` members of a recipe into an intent batch dir, with no
    run files, and build its draws database; returns the database path."""
    def _build(recipe: Path, count: int) -> Path:
        intent_batch, runs_batch = tmp_path / "intent" / "b", tmp_path / "runs" / "b"
        intent_batch.mkdir(parents=True)
        runs_batch.mkdir(parents=True)
        _resolve(recipe, intent_batch, count)
        return build_draws_db(runs_batch, intent_batch)
    return _build


def test_a_monte_carlo_batch_writes_its_draws_database(mc_batch: Path) -> None:
    db = draws_db_path(mc_batch)
    assert db.is_file()
    assert _query(db, "SELECT mode, master_seed, member_count FROM batch") == [("monte_carlo", 7, 8)]
    members = _query(db, "SELECT member_index, is_base, status FROM member ORDER BY member_index")
    assert members == [(i, int(i == 0), "succeeded") for i in range(8)]
    nodes = dict(_query(db, "SELECT name, node_kind FROM node"))
    assert nodes["gift"] == "row" and nodes["retire_bill_vary"] == "row"
    assert "factor" in nodes.values()  # the prototype's default factors
    assert _query(db, "SELECT count(*) FROM draw") == [(8 * len(nodes),)]
    assert _query(db, "SELECT count(*) FROM draws_wide") == [(7,)]


def test_stored_values_match_the_member_files_and_views_translate(mc_batch: Path) -> None:
    db = draws_db_path(mc_batch)
    for member in sorted(mc_batch.glob("member_*.sqlite3")):
        index = int(member.stem.removeprefix("member_"))
        recorded = dict(_query(member, "SELECT calendar_event_name, drawn_value FROM ensemble_draw"))
        stored = dict(_query(db, "SELECT node, value FROM draw WHERE member_index = ?", index))
        shown = dict(_query(db, "SELECT node, value FROM draw_display WHERE member_index = ?", index))
        assert stored["gift"] == float(recorded["gift"])
        assert shown["gift"] == pytest.approx(float(recorded["gift"]) / 100)
        assert stored["retire_bill_vary"] == date.fromisoformat(recorded["retire_bill_vary"]).toordinal()
        assert shown["retire_bill_vary"] == recorded["retire_bill_vary"]
    low, high = _query(db, "SELECT low, high FROM node_display WHERE name = 'retire_bill_vary'")[0]
    assert (low, high) == ("2030-06-30", "2031-06-30")


def test_an_independent_draw_records_the_uniform_it_came_from(mc_batch: Path) -> None:
    """A value is its distribution's quantile at the recorded ``u``: exactly
    for an amount (rounded to cents), the drawn day for a uniform date."""
    db = draws_db_path(mc_batch)
    gift = Uniform(1_000_000, 3_000_000)
    for value, u in _query(db, "SELECT value, u FROM draw d JOIN member m USING (member_index) "
                               "WHERE node = 'gift' AND m.is_base = 0"):
        assert round(gift.quantile(u)) == value
    days = Uniform(date(2030, 6, 30).toordinal(), date(2031, 6, 30).toordinal())
    for value, u in _query(db, "SELECT value, u FROM draw WHERE node = 'retire_bill_vary' "
                               "AND member_index > 0"):
        assert days.value_at_int(u) == value
    assert _query(db, "SELECT u, noise, z FROM draw WHERE node = 'gift' AND member_index = 0") == [
        (0.5, None, None)]


def test_draw_check_compares_drawn_and_expected_statistics(mc_batch: Path) -> None:
    db = draws_db_path(mc_batch)
    n, expected_mean, drawn_mean, ks, p = _query(
        db, "SELECT n, expected_mean, drawn_mean, ks_u, ks_u_p FROM draw_check WHERE node = 'gift'")[0]
    assert n == 7
    assert expected_mean == pytest.approx(2_000_000, abs=1)
    assert 1_000_000 <= drawn_mean <= 3_000_000
    assert 0 < ks < 1 and 0 <= p <= 1
    shown = _query(db, "SELECT expected_mean FROM draw_check_display WHERE node = 'gift'")
    assert shown == [(pytest.approx(20_000, abs=0.01),)]


def test_a_sensitivity_batch_records_quantiles_but_no_check(oat_batch: Path) -> None:
    db = draws_db_path(oat_batch)
    assert _query(db, "SELECT mode, master_seed FROM batch") == [("sensitivity", None)]
    assert _query(db, "SELECT count(*) FROM draw_check") == [(0,)]
    moved = _query(db, "SELECT quantile, u FROM draw WHERE quantile IS NOT NULL")
    assert moved and all(q == u for q, u in moved)
    assert _query(db, "SELECT count(*) FROM node_pair WHERE achieved_rho IS NOT NULL") == [(0,)]


@pytest.mark.usefixtures("without_prototype_defaults")
def test_a_t_tree_records_its_scale_noise_and_path_shocks(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    resolved_batch: Callable[[Path, int], Path],
) -> None:
    _factor(append_calendar_row, "market", vary_tail_dependence="4")
    _process_rate(append_calendar_row, "early", vary_correlate_with="market", vary_correlation="0.6")
    db = resolved_batch(load_test_copy().intent_path, 4)

    assert _query(db, "SELECT tree_root, nu, correlate_with, rho FROM node WHERE name = 'early'") == [
        ("market", 4.0, "market", 0.6)]
    pair = _query(db, "SELECT a, b, kind, implied_rho, expected_kendall_tau, achieved_rho FROM node_pair")
    assert pair == [("early", "market", "link", 0.6, pytest.approx(2 / math.pi * math.asin(0.6)),
                     pytest.approx(pair[0][5]))]
    for i in range(1, 4):
        (scale,) = _query(db, "SELECT scale FROM tail_scale WHERE member_index = ? "
                              "AND tree_root = 'market' AND year IS NULL", i)[0]
        assert scale == tail_scale(4.0, tail_rng(21, i, "market"))
        z, noise, u = _query(db, "SELECT z, noise, u FROM draw WHERE member_index = ? "
                                 "AND node = 'market'", i)[0]
        assert z == pytest.approx(noise * scale)
        assert u == pytest.approx(probability(z, 4.0))
        z, u = _query(db, "SELECT z, u FROM draw WHERE member_index = ? AND node = 'early'", i)[0]
        assert u == pytest.approx(probability(z, 4.0))
        shocks = _query(db, "SELECT score, scale, shock FROM path_shock WHERE member_index = ? "
                            "AND node = 'early' ORDER BY year", i)
        assert shocks and all(shock == pytest.approx(score * s) for score, s, shock in shocks)
    assert _query(db, "SELECT DISTINCT shock, noise FROM path_shock WHERE member_index = 0") == [(0.0, None)]
    assert _query(db, "SELECT count(*) FROM tail_scale WHERE member_index = 0") == [(0,)]


def test_path_rates_match_the_generated_set_rate_rows(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    resolved_batch: Callable[[Path, int], Path], tmp_path: Path,
) -> None:
    _process_rate(append_calendar_row, "stock_path")
    db = resolved_batch(load_test_copy().intent_path, 2)
    member = tmp_path / "intent" / "b" / "member_0001.sqlite3"
    generated = [a for (a,) in _query(
        member, "SELECT amount FROM calendar_event WHERE kind = 'set_rate' AND series = 'Asset_Checking' "
                "AND (name = 'stock_path' OR description LIKE '%for stock_path') ORDER BY date")]
    rates = [r for (r,) in _query(db, "SELECT rate FROM path_shock WHERE member_index = 1 "
                                      "AND node = 'stock_path' ORDER BY year")]
    assert rates == generated


def test_a_life_table_lifespan_is_a_node(
    write_lifespans: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    resolved_batch: Callable[[Path, int], Path],
) -> None:
    write_lifespans("alice,80,life_table,,,,,")
    db = resolved_batch(load_test_copy().intent_path, 3)
    node = _query(db, "SELECT node_kind, unit, distribution, sex, set_forward > 3, base_value "
                      "FROM node WHERE name = 'lifespan:alice'")
    assert node == [("lifespan", "years", "life_table", "f", 1, 80.0)]
    assert _query(db, "SELECT u FROM draw WHERE node = 'lifespan:alice' AND member_index = 0") == [(None,)]
    drawn = _query(db, "SELECT value, u FROM draw WHERE node = 'lifespan:alice' AND member_index > 0")
    assert all(0 < u < 1 for _v, u in drawn)


def test_rebuilding_replaces_the_file_and_frames_read_it(mc_batch: Path) -> None:
    db = draws_db_path(mc_batch)
    db.write_bytes(b"not a database")
    build_draws_db(mc_batch)
    frames = draws_frames(mc_batch)
    assert len(frames["member"]) == 8
    assert frames["node"].set_index("name").loc["gift", "unit"] == "dollars"
    assert list(frames["draws_wide"]["member_index"]) == list(range(1, 8))
    stored = draws_frames(mc_batch, display=False)
    assert stored["node"].set_index("name").loc["gift", "unit"] == "cents"


def test_a_member_without_files_is_listed_with_no_draws(mc_batch: Path) -> None:
    (mc_batch / "member_0003.sqlite3").unlink()
    (mc_batch / "member_0005.sqlite3").unlink()
    write_batch_manifest(mc_batch, member_count=8, failures=[(5, mc_batch / "x", "boom")])
    build_draws_db(mc_batch)
    db = draws_db_path(mc_batch)
    assert _query(db, "SELECT member_index, status, source_file FROM member "
                      "WHERE member_index IN (3, 5) ORDER BY member_index") == [
        (3, "missing", None), (5, "failed_to_resolve", None)]
    assert _query(db, "SELECT count(*) FROM draw WHERE member_index IN (3, 5)") == [(0,)]


def test_build_distribution_reads_the_node_parameters(mc_batch: Path) -> None:
    """``node``'s columns describe the distribution the row was drawn from."""
    db = draws_db_path(mc_batch)
    low, high, median, mean = _query(db, "SELECT low, high, median, mean FROM node WHERE name = 'gift'")[0]
    row = {"name": "gift", "vary_distribution": "uniform", "vary_low": "1000000", "vary_high": "3000000"}
    dist = build_distribution(row, "amount")
    assert isinstance(dist, Uniform)
    assert (low, high, median) == (dist.low, dist.high, dist.quantile(0.5))
    assert mean == pytest.approx(2_000_000, abs=1)


@pytest.mark.parametrize(("us", "distance"), [
    ([0.5], 0.5), ([0.25, 0.75], 0.25), ([0.1, 0.2, 0.3], 0.7),
])
def test_ks_uniform_distance(us: list[float], distance: float) -> None:
    assert ks_uniform(us)[0] == pytest.approx(distance)


def test_ks_uniform_p_value_is_small_for_bunched_draws() -> None:
    assert ks_uniform([0.01 * i for i in range(1, 51)])[1] < 0.001
    assert ks_uniform([(i + 0.5) / 50 for i in range(50)])[1] > 0.99


def _draws_cli(runs_dir: Path, intent_dir: Path, *args: str) -> str:
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "draws", "retire_alice", "--runs-dir", str(runs_dir),
        "--intent-dir", str(intent_dir), *args,
    ])
    assert result.exit_code == 0, result.output
    return result.output


def test_sim_ensemble_draws_summarizes_the_batch(
    mc_batch: Path, runs_dir: Path, intent_dir: Path,
) -> None:
    output = _draws_cli(runs_dir, intent_dir)
    assert f"retire_alice ({mc_batch.name}): monte_carlo, 8 member(s), 8 succeeded, seed 7" in output
    assert str(draws_db_path(mc_batch)) in output
    gift = next(line for line in output.splitlines() if line.strip().startswith("gift "))
    assert "uniform" in gift and "$20,000" in gift
    retire = next(line for line in output.splitlines() if line.strip().startswith("retire_bill_vary"))
    assert "2030-12-29" in retire and "106 days" in retire  # a year's span: median, sd


def test_sim_ensemble_draws_lists_a_node_and_writes_csv(
    mc_batch: Path, runs_dir: Path, intent_dir: Path, tmp_path: Path,
) -> None:
    csv = tmp_path / "draws.csv"
    output = _draws_cli(runs_dir, intent_dir, "--node", "gift", "--csv", str(csv))
    assert "0 (base)" in output and "$20,000" in output
    assert "wrote 7 member(s)" in output
    header = csv.read_text().splitlines()[0].split(",")
    assert header[0] == "member_index" and "gift" in header


def test_sim_ensemble_draws_rejects_an_unknown_node(
    mc_batch: Path, runs_dir: Path, intent_dir: Path,
) -> None:
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "draws", "retire_alice", "--runs-dir", str(runs_dir),
        "--intent-dir", str(intent_dir), "--node", "nope",
    ])
    assert result.exit_code == 2
    assert "'nope' isn't drawn in this batch" in result.output


def test_sim_ensemble_draws_gathers_a_batch_that_has_none(
    mc_batch: Path, runs_dir: Path, intent_dir: Path,
) -> None:
    draws_db_path(mc_batch).unlink()
    _draws_cli(runs_dir, intent_dir)
    assert draws_db_path(mc_batch).is_file()


def test_sim_ensemble_draws_on_a_sensitivity_batch(
    oat_batch: Path, runs_dir: Path, intent_dir: Path,
) -> None:
    output = _draws_cli(runs_dir, intent_dir, "--mode", "sensitivity")
    assert "sensitivity" in output and "seed" not in output.splitlines()[0]
    assert "KS p(u)" not in output


def test_a_batch_whose_every_member_failed_still_lists_them(tmp_path: Path) -> None:
    batch = tmp_path / "runs" / "retire_alice" / "b"
    batch.mkdir(parents=True)
    write_batch_manifest(batch, member_count=2,
                         failures=[(0, batch / "x", "boom"), (1, batch / "y", "boom")])
    db = build_draws_db(batch)
    assert _query(db, "SELECT ensemble_name, mode, member_count, master_seed FROM batch") == [
        ("retire_alice", "monte_carlo", 2, None)]
    assert _query(db, "SELECT member_index, status FROM member ORDER BY member_index") == [
        (0, "failed_to_resolve"), (1, "failed_to_resolve")]
    assert _query(db, "SELECT count(*) FROM node") == [(0,)]


def test_a_date_marker_is_a_node_that_affects_the_rows_dated_from_it(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    resolved_batch: Callable[[Path, int], Path],
) -> None:
    """#281: a marker is drawn like a date row, and names the rows it moves."""
    append_calendar_row(kind="date_marker", name="ep_start", date="2030-06", vary_target="date",
                        vary_distribution="uniform", vary_low="2029-01", vary_high="2034-12")
    append_calendar_row(date="ep_start+2y", kind="set_rate", name="ep_off",
                        series="Asset_Checking", amount="4%")
    db = resolved_batch(load_test_copy().intent_path, 3)
    node = _query(db, "SELECT node_kind, calendar_kind, unit, affects FROM node "
                      "WHERE name = 'ep_start'")
    assert node == [("row", "date_marker", "day_ordinal", "ep_off")]


def test_a_draws_database_from_before_pools_is_gathered_again(mc_batch: Path) -> None:
    """One built before ADR-009 has no member.component: `ensemble draws`
    rebuilds it rather than failing on the per-component queries."""
    from click.testing import CliRunner

    from analysis.cli import main
    from analysis.sim.draws_db import draws_db_current

    path = draws_db_path(mc_batch)
    path.unlink(missing_ok=True)
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE member (member_index INTEGER PRIMARY KEY)")
    assert not draws_db_current(mc_batch)
    result = CliRunner().invoke(main, [
        "sim", "ensemble", "draws", "retire_alice", "--runs-dir", str(mc_batch.parent.parent)])
    assert result.exit_code == 0, result.output
    assert draws_db_current(mc_batch)
    assert "component" not in result.output  # one component: no per-component headings
