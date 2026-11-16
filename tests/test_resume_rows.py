"""#291: a ``set_rate`` row with ``vary_resume`` puts its series back to
whatever governed it the day before that date (ADR-006 §4b): a process
resumes with its own level, persistence and yearly shocks, and a fixed rate
comes back at once."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim import cli
from analysis.sim.engine import EngineError, run_scenario
from analysis.sim.ensemble import EnsembleError, process_path, resolve_member
from analysis.sim.loader import LoadError, load_scenario

INFLATION = '"{""aux_kind"": ""inflation""}"'
# Past the template base's end, so a resumed path has years to show.
LATE = "2080-12-31"
HEADER = "date,kind,name,series,amount,params_json,vary_resume,vary_target,vary_distribution,vary_low,vary_high"


def _load(data_dir: Path, scenario: str, tmp_path: Path) -> Path:
    return load_scenario(data_dir, scenario, intent_dir=tmp_path / "intent",
                         compute_stamps=False, allow_dirty_data=True).intent_path


def _member(recipe: Path, tmp_path: Path, index: int, tag: str = "") -> Path:
    return resolve_member(recipe, tmp_path / f"member{tag}_{index:04d}.sqlite3", seed=42,
                          member_index=index, member_count=20, recipe_tree_id="deadbeef")


def _rates(member: Path, series: str, since: str = "") -> list[tuple[str, int]]:
    con = sqlite3.connect(member)
    try:
        return [(d, int(a)) for d, a in con.execute(
            "SELECT date, amount FROM calendar_event WHERE kind = 'set_rate' AND series = ? "
            "AND date >= ? ORDER BY date, id", (series, since))]
    finally:
        con.close()


def _row(member: Path, name: str) -> sqlite3.Row:
    con = sqlite3.connect(member)
    con.row_factory = sqlite3.Row
    try:
        return con.execute("SELECT * FROM calendar_event WHERE name = ?", (name,)).fetchone()
    finally:
        con.close()


def test_a_fixed_rate_comes_back_at_once(
    template_data_dir: Path, tmp_path: Path, derive_template_scenario,
) -> None:
    """``base`` has only the series files, so when the Great Inflation ends
    CPI-U is back at the rate in effect before it began: the series file's 3%."""
    derive_template_scenario("with_episode", "base:t_great_inflation")
    member = _member(_load(template_data_dir, "with_episode", tmp_path), tmp_path, 0)
    back = _row(member, "great_inflation_cpi_return")
    assert (back["date"], back["amount"], back["vary_resume"]) == ("2060-04-30", 30_000, None)


def test_a_process_resumes_from_where_the_episode_left_it(
    template_data_dir: Path, tmp_path: Path, derive_template_scenario,
) -> None:
    """Under ``t_inflation_ensemble`` CPI-U was ``cpi_path``'s process, so
    after the episode it reverts from the episode's last rate to that row's
    drawn level (member 0: 2.5%) at its persistence, 0.4."""
    derive_template_scenario("with_episode", "base:t_inflation_ensemble:t_great_inflation", end=LATE)
    member = _member(_load(template_data_dir, "with_episode", tmp_path), tmp_path, 0)
    cpi = _rates(member, "cpi", since="2059-12-31")
    last_episode_rate = cpi[0][1]
    resumed = [a for d, a in cpi if d >= "2060-04-30"]
    assert resumed == process_path(25_000, 0.4, 14_000, [0.0] * len(resumed),
                                   start_micros=last_episode_rate)
    assert resumed[-1] == pytest.approx(25_000, abs=100)


@pytest.mark.parametrize("index", [3, 7])
def test_a_resumed_process_takes_its_own_yearly_shocks(
    template_data_dir: Path, tmp_path: Path, derive_template_scenario, index: int,
) -> None:
    """A member with the episode and the same member without it share
    ``cpi_path``'s shocks for every year after the episode, so their CPI-U
    paths come back together as the episode's effect decays."""
    derive_template_scenario("without", "base:t_inflation_ensemble", end=LATE)
    derive_template_scenario("with_episode", "base:t_inflation_ensemble:t_great_inflation", end=LATE)
    without = _member(_load(template_data_dir, "without", tmp_path / "a"), tmp_path, index, "a")
    with_ = _member(_load(template_data_dir, "with_episode", tmp_path / "b"), tmp_path, index, "b")
    back = _row(with_, "great_inflation_cpi_return")["date"]

    def shocks(member: Path) -> dict[int, float]:
        con = sqlite3.connect(member)
        try:
            return dict(con.execute(
                "SELECT year, shock FROM ensemble_path_shock WHERE node = 'cpi_path' AND year > ?",
                (int(back[:4]),)))
        finally:
            con.close()

    assert shocks(with_) and shocks(with_) == shocks(without)

    def year_end(member: Path, year: int) -> int:
        return [a for d, a in _rates(member, "cpi") if d <= f"{year}-12-31"][-1]

    years = range(int(back[:4]) + 1, int(LATE[:4]) + 1)
    gaps = [abs(year_end(with_, y) - year_end(without, y)) for y in years]
    # The gap decays at cpi_path's persistence, 0.4 a year, from wherever the
    # episode left it; rounding to whole micros is all that's left by the end.
    assert gaps[-1] <= 2 and gaps[-1] < gaps[0]


def _resume_scenario(derive_template_scenario, rows: str, name: str = "resumes") -> None:
    derive_template_scenario(name, "base", files={"calendar.csv": f"{HEADER}\n{rows}"})


def test_a_member_whose_resume_date_lands_after_the_row_is_refused(
    template_data_dir: Path, tmp_path: Path, derive_template_scenario,
) -> None:
    _resume_scenario(derive_template_scenario, "\n".join([
        "2035-06-30,date_marker,ep_start,,,,,date,uniform,2030-01-31,2040-12-31",
        f"ep_start,set_rate,ep_cpi,cpi,9%,{INFLATION},,,,,",
        f"2037-06-30,set_rate,ep_cpi_return,cpi,,{INFLATION},ep_start,,,,",
    ]) + "\n")
    recipe = _load(template_data_dir, "resumes", tmp_path)
    with pytest.raises(EnsembleError, match="on or after the row's own date"):
        for index in range(1, 20):
            _member(recipe, tmp_path, index)


def test_a_plain_scenario_with_a_resume_row_runs_its_base_case(
    template_data_dir: Path, tmp_path: Path, derive_template_scenario,
) -> None:
    """Nothing varies, but the resume row still needs resolving: a plain
    run goes through member 0, and the engine refuses the row unresolved."""
    _resume_scenario(derive_template_scenario, "\n".join([
        f"2030-01-31,set_rate,spike,cpi,9%,{INFLATION},,,,,",
        f"2032-01-31,set_rate,spike_return,cpi,,{INFLATION},2030-01-31,,,,",
    ]) + "\n")
    recipe = _load(template_data_dir, "resumes", tmp_path)
    assert cli._has_unresolved_vary(recipe)
    with pytest.raises(EngineError, match="vary_resume"):
        run_scenario(recipe, tmp_path / "run.sqlite3", run_by="test")
    member = _member(recipe, tmp_path, 0)
    assert _row(member, "spike_return")["amount"] == 30_000


@pytest.mark.parametrize(("rows", "message"), [
    (f"2032-01-31,set_rate,r,cpi,3%,{INFLATION},2030-01-31,,,,", "has no amount"),
    (f"2032-01-31,set_rate,r,cpi,,{INFLATION},2032-01-31,,,,", "must come before the row's own date"),
    (f"2032-01-31,set_rate,r,no_such,,{INFLATION},2030-01-31,,,,", "no_such"),
])
def test_the_loader_checks_a_resume_row(
    template_data_dir: Path, tmp_path: Path, derive_template_scenario, rows: str, message: str,
) -> None:
    _resume_scenario(derive_template_scenario, rows + "\n")
    with pytest.raises(LoadError, match=message):
        _load(template_data_dir, "resumes", tmp_path)


def test_calendar_add_set_rate_writes_a_resume_row(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    result = CliRunner().invoke(main, [
        "sim", "calendar", "add", "set-rate", "2029-01-31", "Asset_Checking",
        "--resume", "2028-01-31", "--name", "checking_back",
        "--data-dir", str(test_data_dir), "--scenario", "retire_alice",
    ])
    assert result.exit_code == 0, result.output
    assert "resume what governed before 2028-01-31" in result.output
    df = pd.read_csv(retire_alice_csv("calendar.csv"), dtype=str, keep_default_na=False)
    row = df[df["name"] == "checking_back"].iloc[0]
    assert (row["kind"], row["amount"], row["vary_resume"]) == ("set_rate", "", "2028-01-31")


@pytest.mark.parametrize("extra", [["4.5"], ["--vary-target", "amount"]])
def test_calendar_add_set_rate_resume_refuses_a_rate_or_a_draw(
    test_data_dir: Path, extra: list[str],
) -> None:
    result = CliRunner().invoke(main, [
        "sim", "calendar", "add", "set-rate", "2029-01-31", "Asset_Checking", *extra,
        "--resume", "2028-01-31",
        "--data-dir", str(test_data_dir), "--scenario", "retire_alice",
    ])
    assert result.exit_code != 0
    assert "--resume takes no" in result.output
