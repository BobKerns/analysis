"""#115: a varying set_rate row with ``vary_process = ar1`` keeps its draw as
the level and expands into one set_rate row per year moving around it
(ADR-006 §4a), with yearly shocks drawn through its correlation tree by
calendar year (ADR-008 §5)."""

from __future__ import annotations

import json
import math
import random
import sqlite3
import statistics
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner, Result

from analysis.cli import main
from analysis.sim.correlation import standard_normal, tail_scale
from analysis.sim.distributions import path_rng, path_tail_rng
from analysis.sim.ensemble import process_path, resolve_member
from analysis.sim.loader import LoadError, LoadResult

from tests.test_correlation import _factor

_PROCESS = {"vary_process": "ar1", "vary_persistence": "0.5", "vary_innovation_sd": "20000"}


def _process_rate(
    append_calendar_row: Callable[..., None], name: str, *, date: str = "2027-02-28",
    series: str = "Asset_Checking", **extra: str,
) -> None:
    fields = {
        "date": date, "kind": "set_rate", "name": name, "series": series, "amount": "45000",
        "vary_target": "amount", "vary_distribution": "uniform",
        "vary_low": "40000", "vary_high": "50000", **_PROCESS,
    }
    append_calendar_row(**(fields | extra))


def _issues(load: Callable[[], LoadResult]) -> list[str]:
    with pytest.raises(LoadError) as ei:
        load()
    return ei.value.issues


def _member(recipe: Path, out: Path, member_index: int, **kwargs) -> dict:  # type: ignore[no-untyped-def]
    """Resolve one member: its drawn values by name, every set_rate row for
    Asset_Checking as ``(date, amount, description, payload)``, and each
    named row's resolved amount."""
    member = resolve_member(recipe, out / f"member_{member_index:04d}.sqlite3", seed=21,
                            member_index=member_index, member_count=99,
                            recipe_tree_id="deadbeef", **kwargs)
    con = sqlite3.connect(member)
    try:
        draws = dict(con.execute("SELECT calendar_event_name, drawn_value FROM ensemble_draw"))
        rates = con.execute(
            "SELECT date, amount, description, payload_json FROM calendar_event "
            "WHERE kind = 'set_rate' AND series = 'Asset_Checking' ORDER BY date, id"
        ).fetchall()
        named = dict(con.execute(
            "SELECT name, amount FROM calendar_event WHERE name IS NOT NULL"))
    finally:
        con.close()
    return {"draws": draws, "rates": rates, "named": named}


# --- the path itself (pure) --------------------------------------------------------


def test_zero_shocks_give_a_flat_path_at_the_level() -> None:
    assert process_path(70_000, 0.5, 170_000, [0.0] * 5) == [70_000] * 5


def test_path_compounds_to_the_level_in_the_median() -> None:
    """Growth-factor form: the shocks' sum has median 0, so the compound rate
    over the path has median L, not L minus half the variance."""
    rng = random.Random(1)
    compounds = []
    for _ in range(4000):
        rates = process_path(70_000, 0.3, 170_000, [standard_normal(rng) for _ in range(20)])
        growth = math.prod(1 + r / 1e6 for r in rates)
        compounds.append(growth ** (1 / 20) - 1)
    assert statistics.median(compounds) == pytest.approx(0.07, abs=0.004)


def test_path_has_the_stationary_spread_from_year_zero() -> None:
    """``x0`` starts at the stationary SD ``sigma / sqrt(1 - phi^2)``, so year
    0 is as uncertain as year 10."""
    rng = random.Random(2)
    phi, sigma = 0.8, 0.05
    paths = [process_path(0, phi, 50_000, [standard_normal(rng) for _ in range(11)])
             for _ in range(6000)]
    expected = sigma / math.sqrt(1 - phi ** 2)
    for k in (0, 10):
        logs = [math.log1p(p[k] / 1e6) for p in paths]
        assert statistics.stdev(logs) == pytest.approx(expected, rel=0.05)


def test_persistence_carries_into_the_next_year() -> None:
    rng = random.Random(3)
    pairs = []
    for _ in range(6000):
        p = process_path(0, 0.6, 50_000, [standard_normal(rng) for _ in range(2)])
        pairs.append((math.log1p(p[0] / 1e6), math.log1p(p[1] / 1e6)))
    a, b = zip(*pairs, strict=True)
    assert statistics.correlation(a, b) == pytest.approx(0.6, abs=0.04)


def test_an_absurd_shock_is_clamped_not_an_overflow() -> None:
    rates = process_path(70_000, 0.0, 50_000_000, [100.0, -100.0])
    assert rates[0] == round((1.07 * math.exp(5) - 1) * 1e6)
    assert rates[1] == round((1.07 * math.exp(-5) - 1) * 1e6)
    assert rates[1] > -1_000_000


# --- validation ----------------------------------------------------------------------


@pytest.mark.parametrize(("extra", "fragment"), [
    ({"vary_process": "random_walk"}, "unknown vary_process 'random_walk'"),
    ({"vary_persistence": ""}, "requires vary_persistence and vary_innovation_sd"),
    ({"vary_innovation_sd": ""}, "requires vary_persistence and vary_innovation_sd"),
    ({"vary_persistence": "1"}, "vary_persistence must be at least 0 and below 1"),
    ({"vary_persistence": "-0.1"}, "vary_persistence must be at least 0 and below 1"),
    ({"vary_persistence": "nan"}, "vary_persistence must be at least 0 and below 1"),
    ({"vary_persistence": "x"}, "vary_persistence 'x' is not a number"),
    ({"vary_innovation_sd": "0"}, "vary_innovation_sd must be positive"),
    ({"vary_innovation_sd": "1.5"}, "is not a whole number of micros"),
    ({"vary_ramp_months": "6"}, "vary_process and vary_ramp_months don't combine"),
    ({"vary_low": "-1000000"}, "level must stay above -100%"),
])
def test_bad_process_rows_are_rejected(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    extra: dict[str, str], fragment: str,
) -> None:
    _process_rate(append_calendar_row, "stock_path", **extra)
    issues = _issues(load_test_copy)
    assert any(fragment in issue for issue in issues), issues


def test_an_inferred_bound_below_minus_100_percent_is_rejected(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    """The truncated_normal's default bound (median - 3 sd) counts too."""
    _process_rate(append_calendar_row, "stock_path", vary_distribution="truncated_normal",
                  vary_low="", vary_high="", vary_median="70000", vary_sd="400000")
    assert any("level must stay above -100%" in i for i in _issues(load_test_copy))


def test_process_columns_need_a_varying_set_rate_amount_row(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    append_calendar_row(date="2027-03-31", kind="set_rate", series="Asset_Checking",
                        amount="45000", **_PROCESS)
    append_calendar_row(
        date="2030-12-31", kind="retire_scheduled", name="late", scheduled_name="salary_bill",
        vary_target="date", vary_distribution="uniform", vary_low="2030-06-30",
        vary_high="2031-06-30", **_PROCESS,
    )
    _process_rate(append_calendar_row, "orphan", vary_process="")
    issues = _issues(load_test_copy)
    assert any("vary_* columns set without vary_distribution" in i for i in issues), issues
    assert any("(late): vary_process is only valid" in i for i in issues), issues
    assert any("(orphan): vary_persistence/vary_innovation_sd are only read" in i
               for i in issues), issues


@pytest.mark.usefixtures("without_prototype_defaults")
def test_a_t_tree_holding_a_process_row_needs_nu_above_2(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    _factor(append_calendar_row, "market", vary_tail_dependence="2")
    _process_rate(append_calendar_row, "stock_path", vary_correlate_with="market",
                  vary_correlation="0.9")
    assert any("needs vary_tail_dependence above 2" in i for i in _issues(load_test_copy))


def test_a_process_row_loads_and_is_stored(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    _process_rate(append_calendar_row, "stock_path")
    con = sqlite3.connect(load_test_copy().intent_path)
    try:
        stored = con.execute(
            "SELECT vary_process, vary_persistence, vary_innovation_sd FROM calendar_event "
            "WHERE name = 'stock_path'"
        ).fetchone()
    finally:
        con.close()
    assert stored == ("ar1", 0.5, 20000)


# --- resolution ----------------------------------------------------------------------


def _generated(member: dict, name: str) -> list[tuple[str, int]]:
    return [(d, a) for d, a, desc, _ in member["rates"]
            if desc and desc.endswith(f" for {name}") and "process year" in desc]


def test_member_zero_writes_a_flat_yearly_path_to_the_scenario_end(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    _process_rate(append_calendar_row, "stock_path")
    member = _member(load_test_copy().intent_path, tmp_path, 0)
    assert member["draws"]["stock_path"] == "45000"
    generated = _generated(member, "stock_path")
    # 2028-02-29 through 2040-02-29: the scenario ends 2040-12-31.
    assert [d for d, _ in generated] == [f"{y}-02-{29 if y % 4 == 0 else 28}"
                                         for y in range(2028, 2041)]
    assert {a for _, a in generated} == {45000}
    assert ("2027-02-28", 45000) in [(d, a) for d, a, _, _ in member["rates"]]


def test_a_path_stops_before_the_next_authored_rate_for_its_series(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    _process_rate(append_calendar_row, "stock_path")
    append_calendar_row(date="2031-02-28", kind="set_rate", series="Asset_Checking",
                        amount="30000", description="authored later")
    # A different aux_kind on the same series name doesn't cut the path.
    append_calendar_row(date="2029-06-30", kind="set_rate", series="Asset_Checking",
                        amount="20000", params_json='{"aux_kind": "inflation"}')
    member = _member(load_test_copy().intent_path, tmp_path, 3)
    assert [d for d, _ in _generated(member, "stock_path")] == [
        "2028-02-29", "2029-02-28", "2030-02-28",
    ]


def test_generated_rows_keep_the_rows_payload(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    _process_rate(append_calendar_row, "stock_path", params_json='{"aux_kind": "rate"}')
    member = _member(load_test_copy().intent_path, tmp_path, 1)
    payloads = {p for _, _, desc, p in member["rates"] if desc and "process year" in desc}
    assert {json.loads(p)["aux_kind"] for p in payloads} == {"rate"}


def test_a_lone_rows_shocks_come_from_its_own_path_streams(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    _process_rate(append_calendar_row, "stock_path")
    member = _member(load_test_copy().intent_path, tmp_path, 4)
    level = int(member["draws"]["stock_path"])
    shocks = [standard_normal(path_rng(21, 4, "stock_path", y)) for y in range(2027, 2041)]
    expected = process_path(level, 0.5, 20_000, shocks)
    path = [member["named"]["stock_path"], *(a for _, a in _generated(member, "stock_path"))]
    assert path == expected


def test_adding_a_process_leaves_the_level_draw_alone(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    retire_alice_csv: Callable[[str], Path], tmp_path: Path,
) -> None:
    _process_rate(append_calendar_row, "stock_path")
    with_process = _member(load_test_copy().intent_path, tmp_path / "a", 5)
    path = retire_alice_csv("calendar.csv")
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    for column in _PROCESS:
        df[column] = ""
    df.to_csv(path, index=False)
    held = _member(load_test_copy().intent_path, tmp_path / "b", 5)
    assert with_process["draws"]["stock_path"] == held["draws"]["stock_path"]


@pytest.mark.usefixtures("without_prototype_defaults")
def test_shocks_in_one_tree_correlate_by_calendar_year(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    """Two paths under one factor at rho = 1 get the same shock in each
    calendar year, though one starts two years later; in a t tree the
    year's scale multiplies both."""
    _factor(append_calendar_row, "market", vary_tail_dependence="4")
    _process_rate(append_calendar_row, "early", vary_correlate_with="market",
                  vary_correlation="1")
    _process_rate(append_calendar_row, "late", date="2029-02-28", series="brokerage_growth",
                  vary_correlate_with="market", vary_correlation="1")
    recipe = load_test_copy().intent_path
    member_path = resolve_member(recipe, tmp_path / "m.sqlite3", seed=21, member_index=6,
                                 member_count=99, recipe_tree_id="deadbeef")
    con = sqlite3.connect(member_path)
    try:
        def path_of(series: str, name: str) -> dict[int, int]:
            return {int(d[:4]): a for d, a in con.execute(
                "SELECT date, amount FROM calendar_event WHERE kind = 'set_rate' AND series = ? "
                "AND (name = ? OR description LIKE ?) ORDER BY date",
                (series, name, f"%for {name}"))}
        early = path_of("Asset_Checking", "early")
        late = path_of("brokerage_growth", "late")
        levels = dict(con.execute(
            "SELECT calendar_event_name, drawn_value FROM ensemble_draw "
            "WHERE calendar_event_name IN ('early', 'late')"))
    finally:
        con.close()
    assert min(late) == 2029
    shocks = {y: standard_normal(path_rng(21, 6, "market", y))
              * tail_scale(4.0, path_tail_rng(21, 6, "market", y)) for y in range(2027, 2041)}
    assert list(early.values()) == process_path(
        int(levels["early"]), 0.5, 20_000, [shocks[y] for y in sorted(early)])
    assert list(late.values()) == process_path(
        int(levels["late"]), 0.5, 20_000, [shocks[y] for y in sorted(late)])


def test_an_oat_member_has_flat_paths(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    _process_rate(append_calendar_row, "stock_path")
    member = _member(load_test_copy().intent_path, tmp_path, 1,
                     overrides={"stock_path": 1.0}, mode="sensitivity")
    assert member["draws"]["stock_path"] == "50000"
    assert {a for _, a in _generated(member, "stock_path")} == {50000}


# --- the command line ----------------------------------------------------------------


def _set_rate(test_data_dir: Path, *options: str) -> Result:
    return CliRunner().invoke(main, [
        "sim", "calendar", "add", "set-rate", "2027-02", "Asset_Checking", "7",
        "--name", "stock_path", "--vary-target", "amount", "--vary-distribution", "uniform",
        "--vary-low", "5", "--vary-high", "9", *options,
        "--data-dir", str(test_data_dir), "--scenario", "retire_alice",
    ])


@pytest.mark.parametrize(("options", "fragment"), [
    (("--vary-process", "ar1", "--vary-persistence", "0.2"),
     "requires --vary-persistence and --vary-innovation-sd"),
    (("--vary-persistence", "0.2"), "go with --vary-process"),
    (("--vary-process", "ar1", "--vary-persistence", "1", "--vary-innovation-sd", "17"),
     "at least 0 and below 1"),
    (("--vary-process", "ar1", "--vary-persistence", "0.2", "--vary-innovation-sd", "17",
      "--vary-ramp-months", "6"), "don't combine"),
])
def test_set_rate_checks_process_options(
    test_data_dir: Path, options: tuple[str, ...], fragment: str,
) -> None:
    result = _set_rate(test_data_dir, *options)
    assert result.exit_code != 0
    assert fragment in result.output


def test_editing_a_process_row_keeps_its_process(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    result = _set_rate(test_data_dir, "--vary-process", "ar1", "--vary-persistence", "0.2",
                       "--vary-innovation-sd", "17")
    assert result.exit_code == 0, result.output
    result = CliRunner().invoke(main, [
        "sim", "calendar", "edit", "stock_path",
        "--data-dir", str(test_data_dir), "--scenario", "retire_alice",
    ], input="description\nstocks\n\n")
    assert result.exit_code == 0, result.output
    rows = pd.read_csv(retire_alice_csv("calendar.csv"), dtype=str, keep_default_na=False)
    row = rows.query("name == 'stock_path'").iloc[0]
    assert (row["description"], row["vary_process"], row["vary_persistence"],
            row["vary_innovation_sd"]) == ("stocks", "ar1", "0.2", "170000")


# --- end to end ----------------------------------------------------------------------


def test_an_ensemble_with_a_process_row_runs_and_reports_it(
    append_calendar_row: Callable[..., None], test_data_dir: Path,
    intent_dir: Path, runs_dir: Path,
) -> None:
    from analysis.sim import sensitivity
    from analysis.sim.loader import latest_ensemble_batch

    _process_rate(append_calendar_row, "stock_path")
    runner = CliRunner()
    load = runner.invoke(main, [
        "sim", "load", "retire_alice", "--data-dir", str(test_data_dir),
        "--intent-dir", str(intent_dir), "--allow-dirty-data", "--no-run",
    ])
    assert load.exit_code == 0, load.output
    run = runner.invoke(main, [
        "sim", "run", "retire_alice", "--intent-dir", str(intent_dir),
        "--runs-dir", str(runs_dir), "--ensemble-size", "4", "--allow-dirty-data",
    ])
    assert run.exit_code == 0 and "4/4 member(s) succeeded" in run.output, run.output
    batch = latest_ensemble_batch(runs_dir / "retire_alice")
    assert batch is not None
    row = sensitivity.parameters(batch).loc["stock_path"]
    assert row["process"] == "ar1, persistence 0.5, shock 2.00%"
