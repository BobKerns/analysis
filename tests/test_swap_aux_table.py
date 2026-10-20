"""``swap_aux_table`` calendar events (#197): from a date, an aux table is
replaced by another of the same kind -- a move to another state's brackets,
say -- and the calendar kinds the engine never built are gone."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner

from analysis.cli import main
from analysis.sim.engine import EngineError, run_scenario
from analysis.sim.loader import LoadError, LoadResult


def _swap(append_calendar_row: Callable[..., None], date: str, aux_kind: str,
          series: str, source: str, **extra: str) -> None:
    append_calendar_row(
        date=date, kind="swap_aux_table", series=series,
        params_json=json.dumps({"aux_kind": aux_kind, "source_series": source}),
        **extra,
    )


def _run(load_test_copy: Callable[[], LoadResult], runs_dir: Path, name: str) -> Path:
    run_path = runs_dir / f"{name}.sqlite3"
    run_scenario(load_test_copy().intent_path, run_path, run_by="test", allow_dirty_data=True)
    return run_path


def _salary(run: Path) -> dict[str, int]:
    """salary_alice's posting by month; it inflates with cpi."""
    with sqlite3.connect(run) as con:
        return dict(con.execute(
            "SELECT te.date, te.amount FROM transfer_event te "
            "JOIN scheduled_transaction s ON s.id = te.reason_id AND te.reason_kind = 'rule' "
            "WHERE s.name = 'salary_alice'"
        ).fetchall())


def _aux_rows(run: Path, kind: str, series: str) -> list[tuple[str, dict]]:
    with sqlite3.connect(run) as con:
        return [
            (effective, json.loads(payload))
            for effective, payload in con.execute(
                "SELECT effective_date, payload_json FROM aux_table "
                "WHERE kind = ? AND series = ? ORDER BY effective_date, id",
                (kind, series),
            )
        ]


def _cpi_index(run: Path) -> dict[str, int]:
    with sqlite3.connect(run) as con:
        return dict(con.execute(
            "SELECT st.month, i.cumulative_index_micros FROM inflation_index_state i "
            "JOIN simulation_step st ON st.id = i.step_id WHERE i.series = 'cpi'"
        ).fetchall())


def test_an_inflation_swap_keeps_the_history_before_it(
    retire_alice_csv: Callable[[str], Path], append_calendar_row: Callable[..., None],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    """cpi becomes a flat series from 2027-01: Alice's salary, inflated from
    2026-05, keeps what cpi gave it before the swap and stops growing after."""
    before = _salary(_run(load_test_copy, runs_dir, "before"))
    retire_alice_csv("series/inflation__flat.csv").write_text(
        "effective_date,annual_rate\n2020-01-31,0%\n"
    )
    _swap(append_calendar_row, "2027-01", "inflation", "cpi", "flat")
    after = _salary(_run(load_test_copy, runs_dir, "after"))

    assert {m: a for m, a in after.items() if m < "2027-01-31"} == {
        m: a for m, a in before.items() if m < "2027-01-31"
    }
    assert after["2027-02-28"] == after["2027-06-30"]
    assert after["2027-06-30"] < before["2027-06-30"]
    # The run file holds the table the run used, for CPI-constant reports.
    rows = _aux_rows(runs_dir / "after.sqlite3", "inflation", "cpi")
    assert rows[-1] == ("2027-01-31", {"annual_rate_micros": 0})
    assert all(effective < "2027-01-31" for effective, _ in rows[:-1])
    # The cumulative index stops moving in the swap's own month, as the
    # salary does.
    index = _cpi_index(runs_dir / "after.sqlite3")
    assert index["2027-01-31"] == index["2026-12-31"]
    assert index["2026-12-31"] > index["2026-11-30"]


def test_a_swapped_table_is_the_source_table_from_then_on(
    retire_alice_csv: Callable[[str], Path], append_calendar_row: Callable[..., None],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    """A rate series (looked up only for the month being run) takes the
    source's rows whole, each on its own date."""
    retire_alice_csv("series/rate__later.csv").write_text(
        "effective_date,annual_rate\n2026-01-31,1%\n2029-01-31,2%\n"
    )
    _swap(append_calendar_row, "2028-03", "rate", "Asset_Checking", "later")
    rows = _aux_rows(_run(load_test_copy, runs_dir, "swap"), "rate", "Asset_Checking")
    assert rows == [
        ("2026-01-31", {"annual_rate_micros": 10_000}),
        ("2029-01-31", {"annual_rate_micros": 20_000}),
    ]


def test_a_source_with_nothing_yet_in_force_fails_the_run(
    retire_alice_csv: Callable[[str], Path], append_calendar_row: Callable[..., None],
    load_test_copy: Callable[[], LoadResult], runs_dir: Path,
) -> None:
    retire_alice_csv("series/rate__later.csv").write_text(
        "effective_date,annual_rate\n2030-01-31,2%\n"
    )
    _swap(append_calendar_row, "2028-03", "rate", "Asset_Checking", "later")
    with pytest.raises(EngineError, match="nothing to swap in yet"):
        _run(load_test_copy, runs_dir, "early")


@pytest.mark.parametrize(("params", "extra", "message"), [
    ({"aux_kind": "brackets", "source_series": "flat"}, {}, "needs params_json aux_kind"),
    ({"aux_kind": "inflation"}, {}, "needs params_json source_series"),
    ({"aux_kind": "inflation", "source_series": "cpi"}, {}, "is the series being replaced"),
    ({"aux_kind": "inflation", "source_series": "missing"}, {},
     r"source_series names inflation__missing\.csv"),
    ({"aux_kind": "security_price", "source_series": "flat"}, {}, "needs params_json aux_kind"),
    ({"aux_kind": "inflation", "source_series": "flat"}, {"amount": "3%"}, "takes no amount"),
    ({"aux_kind": "inflation", "source_series": "flat"}, {"lot_1": "default"}, "takes no lot_1"),
    ({"aux_kind": "inflation", "source_series": "flat"}, {"tax_character": "ordinary"},
     "takes no tax_character"),
])
def test_the_loader_rejects_a_malformed_swap(
    retire_alice_csv: Callable[[str], Path], append_calendar_row: Callable[..., None],
    load_test_copy: Callable[[], LoadResult],
    params: dict[str, str], extra: dict[str, str], message: str,
) -> None:
    retire_alice_csv("series/inflation__flat.csv").write_text(
        "effective_date,annual_rate\n2020-01-31,0%\n"
    )
    append_calendar_row(
        date="2027-01", kind="swap_aux_table", series="cpi",
        params_json=json.dumps(params), **extra,
    )
    with pytest.raises(LoadError, match=message):
        load_test_copy()


def test_the_loader_rejects_params_that_are_not_an_object(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
) -> None:
    append_calendar_row(date="2027-01", kind="swap_aux_table", series="cpi", params_json="[]")
    with pytest.raises(LoadError, match="params_json must be a JSON object"):
        load_test_copy()


def test_the_loader_rejects_a_swap_of_a_table_nothing_supplies(
    retire_alice_csv: Callable[[str], Path], append_calendar_row: Callable[..., None],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    retire_alice_csv("series/inflation__flat.csv").write_text(
        "effective_date,annual_rate\n2020-01-31,0%\n"
    )
    _swap(append_calendar_row, "2027-01", "inflation", "cpi_typo", "flat")
    with pytest.raises(LoadError, match=r"swap_aux_table series names inflation__cpi_typo\.csv"):
        load_test_copy()


@pytest.mark.parametrize("kind", ["add_scheduled", "sell_asset", "buy_asset", "life_transition"])
def test_the_dropped_kinds_are_unknown(
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    kind: str,
) -> None:
    append_calendar_row(date="2027-01", kind=kind)
    with pytest.raises(LoadError, match=f"unknown event kind '{kind}'"):
        load_test_copy()


def test_two_swaps_on_one_date_are_two_events(
    write_aux: Callable[[str, str], Path], append_calendar_row: Callable[..., None],
    load_test_copy: Callable[[], LoadResult],
) -> None:
    """Kinds without a series swapped on the same date (a law change) differ
    only in their aux_kind, which keeps them apart."""
    write_aux("federal_brackets__sunset.csv",
              "effective_date,filing_status,bracket_index,floor,marginal_rate\n"
              "2026-01-31,MFJ,0,0.00,10%\n")
    write_aux("standard_deduction__sunset.csv",
              "effective_date,filing_status,amount\n2026-01-31,MFJ,15000.00\n")
    _swap(append_calendar_row, "2027-01", "federal_brackets", "", "sunset")
    _swap(append_calendar_row, "2027-01", "standard_deduction", "", "sunset")
    with sqlite3.connect(load_test_copy().intent_path) as con:
        kinds = sorted(
            json.loads(payload)["aux_kind"] for (payload,) in con.execute(
                "SELECT payload_json FROM calendar_event WHERE kind = 'swap_aux_table'"
            )
        )
    assert kinds == ["federal_brackets", "standard_deduction"]


def test_calendar_add_swap_aux_table_writes_a_row_that_loads(
    test_data_dir: Path, write_aux: Callable[[str, str], Path],
    retire_alice_csv: Callable[[str], Path], load_test_copy: Callable[[], LoadResult],
) -> None:
    write_aux("state_brackets__WA.csv",
              "effective_date,filing_status,bracket_index,floor,marginal_rate\n"
              "2026-01-31,MFJ,0,0.00,0%\n")
    result = CliRunner().invoke(main, [
        "sim", "calendar", "add", "swap-aux-table", "2031-07", "state_brackets", "WA",
        "--series", "CA", "--description", "move",
        "--data-dir", str(test_data_dir), "--scenario", "retire_alice",
    ])
    assert result.exit_code == 0, result.output
    assert "swap_aux_table state_brackets__CA -> state_brackets__WA on 2031-07" in result.output
    rows = pd.read_csv(retire_alice_csv("calendar.csv"), dtype=str, keep_default_na=False)
    row = rows.query("kind == 'swap_aux_table'").iloc[0]
    assert row["series"] == "CA"
    assert json.loads(row["params_json"]) == {"aux_kind": "state_brackets", "source_series": "WA"}
    load_test_copy()


def test_a_move_to_another_state_takes_its_standard_deduction(
    add_state_tax: Callable[..., None], write_aux: Callable[[str, str], Path],
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    runs_dir: Path,
) -> None:
    """Swapping state_brackets swaps state_standard_deduction with it (#248),
    so the new state's brackets don't run with the old state's deduction."""
    add_state_tax(bracket_series="CA")
    write_aux("state_brackets__WA.csv",
              "effective_date,filing_status,bracket_index,floor,marginal_rate\n"
              "2026-01-31,MFJ,0,0.00,0%\n")
    write_aux("state_standard_deduction__WA.csv",
              "effective_date,filing_status,amount\n2026-01-31,MFJ,1000.00\n")
    _swap(append_calendar_row, "2028-03", "state_brackets", "CA", "WA")
    assert not any("standard deduction" in w for w in load_test_copy().warnings)
    run = _run(load_test_copy, runs_dir, "move")
    with sqlite3.connect(run) as con:
        deduction = dict(con.execute(
            "SELECT year, amount FROM tax_year_summary WHERE bucket = 'state_standard_deduction'"
        ).fetchall())
    assert deduction[2026] == 11_080_00  # California's, before the move
    assert deduction[2028] == 1_000_00


def test_a_move_to_a_state_without_a_standard_deduction_falls_back_to_federal(
    add_state_tax: Callable[..., None], write_aux: Callable[[str, str], Path],
    append_calendar_row: Callable[..., None], load_test_copy: Callable[[], LoadResult],
    runs_dir: Path,
) -> None:
    add_state_tax(bracket_series="CA")
    write_aux("state_brackets__WA.csv",
              "effective_date,filing_status,bracket_index,floor,marginal_rate\n"
              "2026-01-31,MFJ,0,0.00,0%\n")
    _swap(append_calendar_row, "2028-03", "state_brackets", "CA", "WA")
    warnings = load_test_copy().warnings
    assert any("after the move to WA" in w and "state_standard_deduction__WA.csv" in w
               for w in warnings)
    run = _run(load_test_copy, runs_dir, "move")
    with sqlite3.connect(run) as con:
        deduction = dict(con.execute(
            "SELECT year, amount FROM tax_year_summary WHERE bucket = 'state_standard_deduction'"
        ).fetchall())
    assert deduction[2028] == 32_200_00  # the federal figure, not California's
