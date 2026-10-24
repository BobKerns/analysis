"""#280: a process row with ``vary_process_start = current`` starts its path at
the rate in effect just before its date, from the series file or an earlier
row, and reverts to its drawn level at its persistence (ADR-006 §4a)."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim.correlation import standard_normal
from analysis.sim.distributions import path_rng
from analysis.sim.ensemble import process_path
from analysis.sim.loader import LoadResult

from tests.test_rate_paths import _generated, _issues, _member, _process_rate, _set_rate

_CURRENT = {"vary_process_start": "current"}


def _path(member: dict, name: str) -> list[int]:
    """A process row's whole path: year 0 on the row, then its generated years."""
    return [member["named"][name], *(a for _, a in _generated(member, name))]


def _x(rate_micros: int, level_micros: int) -> float:
    """A rate's log departure from the level, in the path's growth-factor terms."""
    return 12 * math.log((1 + rate_micros / 12e6) / (1 + level_micros / 12e6))


# --- the path itself (pure) --------------------------------------------------------


def test_zero_shocks_decay_from_the_start_to_the_level() -> None:
    """Member 0's path: year 0 is the current rate exactly, and each later
    year keeps ``phi`` of the departure, so it closes in on the level."""
    rates = process_path(70_000, 0.5, 20_000, [0.0] * 6, start_micros=30_000)
    assert rates[0] == 30_000
    x0 = _x(30_000, 70_000)
    for k, rate in enumerate(rates[1:], start=1):
        assert _x(rate, 70_000) == pytest.approx(0.5 ** k * x0, abs=1e-5)
    assert rates == sorted(rates)
    assert rates[-1] < 70_000


def test_year_zeros_shock_is_not_used() -> None:
    a = process_path(70_000, 0.5, 20_000, [3.0, 0.0], start_micros=30_000)
    b = process_path(70_000, 0.5, 20_000, [-3.0, 0.0], start_micros=30_000)
    assert a == b


def test_no_persistence_is_back_at_the_level_in_a_year() -> None:
    assert process_path(70_000, 0.0, 20_000, [0.0] * 3, start_micros=30_000) == [
        30_000, 70_000, 70_000]


def test_later_shocks_apply_on_top_of_the_decay() -> None:
    rates = process_path(70_000, 0.5, 20_000, [0.0, 1.0], start_micros=30_000)
    assert _x(rates[1], 70_000) == pytest.approx(0.5 * _x(30_000, 70_000) + 0.02, abs=1e-5)


# --- loading -------------------------------------------------------------------------


def test_the_start_loads_and_is_stored(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    _process_rate(append_calendar_row, "stock_path", **_CURRENT)
    con = sqlite3.connect(load_test_copy().intent_path)
    try:
        stored = con.execute(
            "SELECT vary_process_start FROM calendar_event WHERE name = 'stock_path'"
        ).fetchone()
    finally:
        con.close()
    assert stored == ("current",)


@pytest.mark.parametrize(("extra", "fragment"), [
    ({"vary_process_start": "later"}, "unknown vary_process_start 'later'"),
    ({"vary_process": "", "vary_persistence": "", "vary_shock_sd": "",
      "vary_process_start": "current"}, "vary_process_start is only read with vary_process"),
])
def test_bad_starts_are_rejected(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    extra: dict[str, str], fragment: str,
) -> None:
    _process_rate(append_calendar_row, "stock_path", **extra)
    issues = _issues(load_test_copy)
    assert any(fragment in issue for issue in issues), issues


def test_a_current_start_needs_a_rate_before_its_date(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    retire_alice_csv: Callable[[str], Path],
) -> None:
    """The series file's first entry is on the row's own date, which the row
    replaces, and no earlier row sets the series: nothing to start from."""
    retire_alice_csv("series/rate__Asset_Checking.csv").write_text(
        "effective_date,annual_rate\n2026-12-31,3%\n")
    _process_rate(append_calendar_row, "stock_path", date="2026-12-31", **_CURRENT)
    issues = _issues(load_test_copy)
    assert any("vary_process_start 'current' needs a rate in effect before 2026-12-31" in i
               for i in issues), issues


def test_an_earlier_row_whose_date_varies_is_not_counted_at_load(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    retire_alice_csv: Callable[[str], Path],
) -> None:
    """It could land after the process row, so only a fixed date supplies a
    start for certain."""
    retire_alice_csv("series/rate__Asset_Checking.csv").write_text(
        "effective_date,annual_rate\n2026-12-31,3%\n")
    append_calendar_row(date="2026-10-31", kind="set_rate", series="Asset_Checking",
                        amount="2%", name="moved", vary_target="date",
                        vary_distribution="uniform", vary_low="2026-08", vary_high="2027-06")
    _process_rate(append_calendar_row, "stock_path", date="2026-12-31", **_CURRENT)
    assert any("needs a rate in effect before" in i for i in _issues(load_test_copy))


# --- resolution ----------------------------------------------------------------------


def test_member_zero_reverts_from_an_earlier_row_to_the_level(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    """retire_alice sets the checking rate to 4.5% in 2027-01; a process from
    2027-02 with a 7% level starts there and climbs toward 7%."""
    _process_rate(append_calendar_row, "stock_path", vary_low="6%", vary_high="8%",
                  **_CURRENT)
    member = _member(load_test_copy().intent_path, tmp_path, 0)
    assert member["draws"]["stock_path"] == "70000"  # ensemble_draw records the level
    path = _path(member, "stock_path")
    assert path == process_path(70_000, 0.5, 20_000, [0.0] * len(path), start_micros=45_000)
    assert path[0] == 45_000 and path == sorted(path) and path[-1] < 70_000


def test_the_start_is_strictly_before_the_rows_date(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    retire_alice_csv: Callable[[str], Path], tmp_path: Path,
) -> None:
    """A series entry on the row's own date is replaced by the row (the
    engine applies the row after the file), so the start is the one before."""
    retire_alice_csv("series/rate__Asset_Checking.csv").write_text(
        "effective_date,annual_rate\n2026-01-31,3%\n2026-06-30,2%\n2026-12-31,5%\n")
    _process_rate(append_calendar_row, "stock_path", date="2026-12-31", **_CURRENT)
    member = _member(load_test_copy().intent_path, tmp_path, 0)
    # The authored 2027-01 row cuts the path after year 0.
    assert _path(member, "stock_path") == [20_000]


def test_a_varying_earlier_row_is_resolved_first(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    """The process row comes first in the file, but pass 2 runs in date
    order, so it starts from the earlier row's drawn value."""
    _process_rate(append_calendar_row, "stock_path", date="2029-02-28", **_CURRENT)
    append_calendar_row(date="2027-02-28", kind="set_rate", series="Asset_Checking",
                        amount="5%", name="rise", vary_target="amount",
                        vary_distribution="uniform", vary_low="5%", vary_high="6%")
    member = _member(load_test_copy().intent_path, tmp_path, 3)
    assert member["named"]["stock_path"] == int(member["draws"]["rise"])


def test_a_path_after_another_starts_from_its_last_year(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    _process_rate(append_calendar_row, "early")
    _process_rate(append_calendar_row, "later", date="2030-02-28", **_CURRENT)
    member = _member(load_test_copy().intent_path, tmp_path, 4)
    early = _generated(member, "early")
    assert [d for d, _ in early][-1] == "2029-02-28"
    assert member["named"]["later"] == early[-1][1]


def test_a_members_path_takes_its_shocks_after_year_zero(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    _process_rate(append_calendar_row, "stock_path", **_CURRENT)
    recipe = load_test_copy().intent_path
    member = _member(recipe, tmp_path, 4)
    level = int(member["draws"]["stock_path"])
    shocks = [standard_normal(path_rng(21, 4, "stock_path", y)) for y in range(2027, 2041)]
    assert _path(member, "stock_path") == process_path(
        level, 0.5, 20_000, shocks, start_micros=45_000)
    con = sqlite3.connect(tmp_path / "member_0004.sqlite3")
    try:
        recorded = dict(con.execute(
            "SELECT year, shock FROM ensemble_path_shock WHERE node = 'stock_path'"))
    finally:
        con.close()
    assert recorded[2027] == 0.0
    assert recorded[2028] == pytest.approx(shocks[1])


# --- the command line ----------------------------------------------------------------


def test_set_rate_writes_the_start(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    result = _set_rate(test_data_dir, "--vary-process", "ar1", "--vary-persistence", "0.2",
                       "--vary-shock-sd", "17", "--vary-process-start", "current")
    assert result.exit_code == 0, result.output
    rows = pd.read_csv(retire_alice_csv("calendar.csv"), dtype=str, keep_default_na=False)
    assert rows.query("name == 'stock_path'").iloc[0]["vary_process_start"] == "current"


def test_set_rate_needs_a_process_for_a_start(test_data_dir: Path) -> None:
    result = _set_rate(test_data_dir, "--vary-process-start", "current")
    assert result.exit_code != 0
    assert "go with --vary-process" in result.output


def test_sensitivity_describes_the_start(
    append_calendar_row: Callable[..., None], test_data_dir: Path,
    intent_dir: Path, runs_dir: Path,
) -> None:
    from analysis.sim import sensitivity
    from analysis.sim.loader import latest_ensemble_batch

    _process_rate(append_calendar_row, "stock_path", **_CURRENT)
    runner = CliRunner()
    load = runner.invoke(main, [
        "sim", "load", "retire_alice", "--data-dir", str(test_data_dir),
        "--intent-dir", str(intent_dir), "--allow-dirty-data", "--no-run",
    ])
    assert load.exit_code == 0, load.output
    run = runner.invoke(main, [
        "sim", "run", "retire_alice", "--intent-dir", str(intent_dir),
        "--runs-dir", str(runs_dir), "--ensemble-size", "2", "--allow-dirty-data",
    ])
    assert run.exit_code == 0, run.output
    batch = latest_ensemble_batch(runs_dir / "retire_alice")
    assert batch is not None
    row = sensitivity.parameters(batch).loc["stock_path"]
    assert row["process"] == "ar1, persistence 0.5, shock 2.00%, from the current rate"
