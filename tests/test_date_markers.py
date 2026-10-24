"""#281: ``date_marker`` rows, and calendar dates written relative to one
(``NAME``, ``NAME+Nm``, ``NAME-Ny``). The loader writes each relative date
against the markers' base case; the ensemble step recomputes it from each
member's drawn markers (ADR-006 §3)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim.dates import RelativeDate, parse_relative_date, place_month
from analysis.sim.ensemble import resolve_member
from analysis.sim.loader import LoadResult

from tests.test_rate_paths import _issues

_MARKER = {"kind": "date_marker", "name": "ep_start"}
_VARYING_MARKER = {
    **_MARKER, "date": "2030-06", "vary_target": "date", "vary_distribution": "uniform",
    "vary_low": "2029-01", "vary_high": "2034-12",
}


def _rate(name: str, when: str, amount: str = "6%", **extra: str) -> dict[str, str]:
    return {"date": when, "kind": "set_rate", "name": name, "series": "Asset_Checking",
            "amount": amount, **extra}


def _rows(path: Path) -> dict[str, dict]:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        return {r["name"]: dict(r) for r in con.execute(
            "SELECT * FROM calendar_event WHERE name IS NOT NULL")}
    finally:
        con.close()


def _member(recipe: Path, out: Path, member_index: int, **kwargs) -> tuple[dict, dict]:  # type: ignore[no-untyped-def]
    member = resolve_member(recipe, out / f"member_{member_index:04d}.sqlite3", seed=21,
                            member_index=member_index, member_count=99,
                            recipe_tree_id="deadbeef", **kwargs)
    con = sqlite3.connect(member)
    try:
        draws = dict(con.execute("SELECT calendar_event_name, drawn_value FROM ensemble_draw"))
    finally:
        con.close()
    return _rows(member), draws


# --- the syntax (pure) ---------------------------------------------------------------


@pytest.mark.parametrize(("text", "expected"), [
    ("ep_start", RelativeDate("ep_start", 0)),
    ("ep_start+2y", RelativeDate("ep_start", 24)),
    ("ep_start-6m", RelativeDate("ep_start", -6)),
    ("ep-start+18m", RelativeDate("ep-start", 18)),
    ("2030-06", None),
    ("2030-06-30", None),
    ("ep_start+2", None),
    ("ep_start+2w", None),
])
def test_relative_dates_parse(text: str, expected: RelativeDate | None) -> None:
    assert parse_relative_date(text) == expected


def test_relative_dates_print_in_years_when_whole() -> None:
    assert [str(RelativeDate("m", n)) for n in (0, 24, -6, 18)] == ["m", "m+2y", "m-6m", "m+18m"]


def test_a_relative_month_lands_where_a_bare_month_would() -> None:
    anchor = date(2030, 6, 14)
    assert place_month(anchor, 20) == date(2032, 2, 29)
    assert place_month(anchor, -6, "low") == date(2029, 12, 1)
    assert place_month(anchor, 0, "point") == date(2030, 6, 15)
    assert place_month(anchor, 0, "high") == date(2030, 6, 30)


# --- loading -------------------------------------------------------------------------


def test_relative_dates_resolve_at_load(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    append_calendar_row(**_MARKER, date="2030-06")
    append_calendar_row(**_rate("ep_on", "ep_start"))
    append_calendar_row(**_rate("ep_off", "ep_start+2y", "4%"))
    append_calendar_row(**_rate("ep_warning", "ep_start-6m", "5%"))
    rows = _rows(load_test_copy().intent_path)
    assert rows["ep_start"]["date"] == "2030-06-30"
    assert rows["ep_start"]["date_refs_json"] is None
    assert {n: rows[n]["date"] for n in ("ep_on", "ep_off", "ep_warning")} == {
        "ep_on": "2030-06-30", "ep_off": "2032-06-30", "ep_warning": "2029-12-31"}
    assert json.loads(rows["ep_off"]["date_refs_json"]) == {"date": ["ep_start", 24]}


def test_a_marker_may_be_dated_from_another(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    append_calendar_row(**_MARKER, date="2030-06")
    append_calendar_row(kind="date_marker", name="ep_end", date="ep_start+3y")
    append_calendar_row(**_rate("ep_off", "ep_end+1y", "4%"))
    rows = _rows(load_test_copy().intent_path)
    assert rows["ep_end"]["date"] == "2033-06-30"
    assert rows["ep_off"]["date"] == "2034-06-30"


def test_two_markers_on_one_date_are_two_rows(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    """A marker is keyed by its name, as a latent factor is."""
    append_calendar_row(**_MARKER, date="2030-06")
    append_calendar_row(kind="date_marker", name="other_start", date="2030-06")
    append_calendar_row(**_rate("ep_on", "ep_start"))
    append_calendar_row(**_rate("other_on", "other_start", "7%"))
    rows = _rows(load_test_copy().intent_path)
    assert {"ep_start", "other_start"} <= set(rows)


def test_a_varying_marker_loads_at_its_base_case(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    """The middle of its range, as member 0 draws it."""
    append_calendar_row(**_VARYING_MARKER)
    append_calendar_row(**_rate("ep_off", "ep_start+2y", "4%"))
    rows = _rows(load_test_copy().intent_path)
    middle = date.fromordinal((date(2029, 1, 1).toordinal() + date(2034, 12, 31).toordinal()) // 2)
    assert rows["ep_off"]["date"] == place_month(middle, 24).isoformat()


@pytest.mark.parametrize(("rows", "fragment"), [
    ([_rate("ep_on", "nothing+1y")],
     "ep_on): date 'nothing+1y' is not a valid YYYY-MM or YYYY-MM-DD, nor relative to a "
     "date_marker (NAME, NAME+Nm, NAME-Ny): no date_marker is named 'nothing'"),
    ([_rate("ep_on", "2030-06"), _rate("ep_off", "ep_on+1y", "4%")],
     "date 'ep_on+1y' names 'ep_on', which is not a date_marker"),
    ([{"kind": "date_marker", "name": "a", "date": "b+1y"},
      {"kind": "date_marker", "name": "b", "date": "a-1y"}], "date_marker cycle"),
    ([{"kind": "date_marker", "date": "2030-06"}], "a date_marker row needs a name"),
    ([{**_MARKER}], "a date_marker row needs a date"),
    ([{**_MARKER, "date": "2030-06", "series": "Asset_Checking", "amount": "4%"}],
     "a date_marker row has no series, amount"),
    ([{**_MARKER, "date": "2030-06", "vary_target": "amount", "vary_distribution": "uniform",
       "vary_low": "1", "vary_high": "2"}], "vary_target='amount' is only valid on kind"),
])
def test_bad_markers_and_references_are_rejected(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    rows: list[dict[str, str]], fragment: str,
) -> None:
    for row in rows:
        append_calendar_row(**row)
    issues = _issues(load_test_copy)
    assert any(fragment in issue for issue in issues), issues


def test_a_marker_nothing_refers_to_is_reported_as_unused(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    append_calendar_row(**_MARKER, date="2030-06")
    unused = load_test_copy().unused
    assert any("(ep_start): no row's date refers to this date_marker yet" in u for u in unused)


# --- members -------------------------------------------------------------------------


def test_member_zero_keeps_the_base_case(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    append_calendar_row(**_VARYING_MARKER)
    append_calendar_row(**_rate("ep_off", "ep_start+2y", "4%"))
    intent = load_test_copy().intent_path
    loaded = _rows(intent)
    rows, draws = _member(intent, tmp_path, 0)
    assert rows["ep_off"]["date"] == loaded["ep_off"]["date"]
    assert "ep_start" in draws


@pytest.mark.parametrize("member_index", [1, 2, 3, 4])
def test_dependent_rows_move_with_the_drawn_marker(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path, member_index: int,
) -> None:
    append_calendar_row(**_VARYING_MARKER)
    append_calendar_row(kind="date_marker", name="ep_end", date="ep_start+3y")
    append_calendar_row(**_rate("ep_on", "ep_start"))
    append_calendar_row(**_rate("ep_off", "ep_end", "4%"))
    rows, draws = _member(load_test_copy().intent_path, tmp_path, member_index)
    drawn = date.fromisoformat(draws["ep_start"])
    assert date(2029, 1, 1) <= drawn <= date(2034, 12, 31)
    assert rows["ep_on"]["date"] == place_month(drawn, 0).isoformat()
    assert rows["ep_end"]["date"] == place_month(drawn, 36).isoformat()
    assert rows["ep_off"]["date"] == place_month(drawn, 36).isoformat()
    assert "ep_end" not in draws  # fixed relative to its marker, so not a draw


@pytest.mark.parametrize("member_index", [1, 2, 3])
def test_a_range_relative_to_a_marker_moves_with_it(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path, member_index: int,
) -> None:
    append_calendar_row(**_VARYING_MARKER)
    append_calendar_row(**_rate(
        "ep_off", "ep_start+2y", "4%", vary_target="date", vary_distribution="uniform",
        vary_low="ep_start+1y", vary_high="ep_start+3y",
    ))
    rows, draws = _member(load_test_copy().intent_path, tmp_path, member_index)
    marker = date.fromisoformat(draws["ep_start"])
    off = date.fromisoformat(rows["ep_off"]["date"])
    assert place_month(marker, 12, "low") <= off <= place_month(marker, 36, "high")
    assert draws["ep_off"] == rows["ep_off"]["date"]


def test_a_row_can_vary_its_amount_and_move_with_a_marker(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    append_calendar_row(**_VARYING_MARKER)
    append_calendar_row(**_rate(
        "ep_on", "ep_start", vary_target="amount", vary_distribution="uniform",
        vary_low="5%", vary_high="7%",
    ))
    rows, draws = _member(load_test_copy().intent_path, tmp_path, 2)
    assert rows["ep_on"]["date"] == place_month(date.fromisoformat(draws["ep_start"]), 0).isoformat()
    assert 50_000 <= rows["ep_on"]["amount"] <= 70_000


def test_overriding_the_marker_moves_its_rows(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    tmp_path: Path,
) -> None:
    """A sensitivity member at the marker's 90th percentile: the marker is
    the parameter (ADR-007), and its rows follow it."""
    append_calendar_row(**_VARYING_MARKER)
    append_calendar_row(**_rate("ep_off", "ep_start+2y", "4%"))
    rows, draws = _member(load_test_copy().intent_path, tmp_path, 1,
                          overrides={"ep_start": 0.9}, mode="sensitivity")
    drawn = date.fromisoformat(draws["ep_start"])
    assert drawn.year == 2034
    assert rows["ep_off"]["date"] == place_month(drawn, 24).isoformat()


# --- running -------------------------------------------------------------------------


def test_a_scenario_with_markers_runs(
    append_calendar_row: Callable[..., None], test_data_dir: Path,
    intent_dir: Path, runs_dir: Path,
) -> None:
    """The engine skips the marker; a row dated past the scenario's end does
    nothing, so a marker there drops its episode."""
    append_calendar_row(**_MARKER, date="2030-06")
    append_calendar_row(kind="date_marker", name="late_start", date="2045-06")
    append_calendar_row(**_rate("ep_on", "ep_start"))
    append_calendar_row(**_rate("late_on", "late_start", "9%"))
    runner = CliRunner()
    load = runner.invoke(main, [
        "sim", "load", "retire_alice", "--data-dir", str(test_data_dir),
        "--intent-dir", str(intent_dir), "--allow-dirty-data", "--no-run",
    ])
    assert load.exit_code == 0, load.output
    run = runner.invoke(main, [
        "sim", "run", "retire_alice", "--intent-dir", str(intent_dir),
        "--runs-dir", str(runs_dir), "--allow-dirty-data",
    ])
    assert run.exit_code == 0, run.output


# --- the command line ----------------------------------------------------------------


def _sim(test_data_dir: Path, *args: str):  # type: ignore[no-untyped-def]
    return CliRunner().invoke(main, [
        "sim", *args, "--scenario", "retire_alice", "--data-dir", str(test_data_dir),
    ])


def _calendar(retire_alice_csv: Callable[[str], Path]) -> list[dict[str, str]]:
    import csv
    with retire_alice_csv("calendar.csv").open(newline="") as fh:
        return list(csv.DictReader(fh))


def test_calendar_add_writes_a_marker_and_rows_dated_from_it(
    test_data_dir: Path, retire_alice_csv: Callable[[str], Path],
) -> None:
    added = _sim(test_data_dir, "calendar", "add", "date-marker", "ep_start", "2030-06",
                 "--vary-distribution", "triangular", "--vary-low", "2029-06",
                 "--vary-high", "2031-12", "--vary-mode", "2030-06")
    assert added.exit_code == 0, added.output
    rate = _sim(test_data_dir, "calendar", "add", "set-rate", "ep_start+2y", "Asset_Checking", "6")
    assert rate.exit_code == 0, rate.output
    rows = {r["name"] or r["date"]: r for r in _calendar(retire_alice_csv)}
    marker = rows["ep_start"]
    assert (marker["kind"], marker["date"], marker["vary_target"], marker["vary_distribution"],
            marker["vary_mode"]) == ("date_marker", "2030-06", "date", "triangular", "2030-06")
    assert "ep_start+2y" in rows


def test_calendar_add_refuses_an_unknown_marker(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "calendar", "add", "set-rate", "nothing+2y", "Asset_Checking", "6")
    assert result.exit_code != 0
    assert "no date_marker is named 'nothing'" in result.output


def test_calendar_add_refuses_a_date_that_is_neither(test_data_dir: Path) -> None:
    result = _sim(test_data_dir, "calendar", "add", "set-rate", "2030-6-1", "Asset_Checking", "6")
    assert result.exit_code != 0
    assert "or relative to a date_marker" in result.output


def test_a_marker_rows_are_dated_from_is_not_deleted(
    test_data_dir: Path, append_calendar_row: Callable[..., None],
) -> None:
    append_calendar_row(**_MARKER, date="2030-06")
    append_calendar_row(**_rate("ep_on", "ep_start"))
    result = _sim(test_data_dir, "calendar", "delete", "ep_start", "--yes")
    assert result.exit_code != 0
    assert "date_marker 'ep_start' dates" in result.output


def test_renaming_a_marker_moves_the_dates_written_from_it(
    test_data_dir: Path, append_calendar_row: Callable[..., None],
    retire_alice_csv: Callable[[str], Path],
) -> None:
    append_calendar_row(**_MARKER, date="2030-06")
    append_calendar_row(**_rate("ep_on", "ep_start+18m", vary_target="date",
                                vary_distribution="uniform", vary_low="ep_start+1y",
                                vary_high="ep_start+2y"))
    result = _sim(test_data_dir, "calendar", "rename", "ep_start", "crash_start")
    assert result.exit_code == 0, result.output
    row = next(r for r in _calendar(retire_alice_csv) if r["name"] == "ep_on")
    assert (row["date"], row["vary_low"], row["vary_high"]) == (
        "crash_start+18m", "crash_start+1y", "crash_start+2y")
