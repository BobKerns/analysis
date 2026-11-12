"""Tax brackets, standard deductions and long-term gain bands indexed for
inflation by a growth series per row (#467)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from analysis.sim import engine
from analysis.sim.engine import run_scenario
from analysis.sim.loader import LoadError, load_scenario

# prototype_data/aux/standard_deduction.csv: 2027 MFJ, indexed by cpi.
MFJ_2027 = 32_200_00


def _cpi_2_5() -> dict[tuple[str, str | None], list[tuple[str, dict]]]:
    return {("inflation", "cpi"): [("2025-01-31", {"annual_rate_micros": 25_000})]}


def _aux_with(kind: str, rows: list[tuple[str, dict]]) -> dict:
    return {**_cpi_2_5(), (kind, None): rows}


def test_an_indexed_figure_grows_to_the_tax_years_january_rounded_down_to_50() -> None:
    aux = _aux_with("standard_deduction", [
        ("2027-01-31", {"filing_status": "MFJ", "amount": MFJ_2027, "amount_series": "cpi"}),
    ])
    cache: dict = {}
    factor = engine._cumulative_inflation_factor(
        aux, "cpi", date(2027, 1, 31), date(2030, 1, 31), cache)
    [row] = engine._indexed_tax_rows(aux, "standard_deduction", None, 2030, cache)
    exact = MFJ_2027 * factor // 1_000_000
    assert row["amount"] == exact // 5_000 * 5_000
    assert 34_650_00 <= row["amount"] <= 34_700_00  # about 2.5% a year for three years


def test_the_tables_own_year_and_a_row_with_no_series_are_as_written() -> None:
    aux = _aux_with("federal_brackets", [
        ("2027-01-31", {"filing_status": "MFJ", "floor": 0, "floor_series": "cpi"}),
        ("2027-01-31", {"filing_status": "MFJ", "floor": 24_800_00, "floor_series": "cpi"}),
        ("2027-01-31", {"filing_status": "MFJ", "floor": 100_800_00, "floor_series": None}),
    ])
    same_year = engine._indexed_tax_rows(aux, "federal_brackets", None, 2027, {})
    assert [r["floor"] for r in same_year] == [0, 24_800_00, 100_800_00]
    later = engine._indexed_tax_rows(aux, "federal_brackets", None, 2035, {})
    assert later[0]["floor"] == 0
    assert later[1]["floor"] > 24_800_00 * 12 // 10
    assert later[2]["floor"] == 100_800_00


def test_a_later_dated_row_restarts_from_its_own_figures() -> None:
    aux = _aux_with("standard_deduction", [
        ("2027-01-31", {"filing_status": "MFJ", "amount": MFJ_2027, "amount_series": "cpi"}),
        ("2033-01-31", {"filing_status": "MFJ", "amount": 20_000_00, "amount_series": "cpi"}),
    ])
    [row] = engine._indexed_tax_rows(aux, "standard_deduction", None, 2033, {})
    assert row["amount"] == 20_000_00


def _run(
    tmp_path: Path, repo: Path, fund_asset_savings: Callable[..., None],
    write_tax: Callable[..., Path],
) -> sqlite3.Connection:
    fund_asset_savings()
    write_tax(repo / "scenarios" / "retire_alice")
    intent_path = load_scenario(
        repo, "retire_alice", intent_dir=tmp_path / "intent", loaded_by="test",
        allow_dirty_data=True,
    ).intent_path
    run_path = tmp_path / "r.sqlite3"
    run_scenario(intent_path, run_path, run_by="test", allow_dirty_data=True)
    return sqlite3.connect(run_path)


def _standard_deduction(con: sqlite3.Connection, year: int) -> int:
    return con.execute(
        "SELECT amount FROM tax_year_summary WHERE year = ? "
        "AND bucket = 'federal_standard_deduction'", (year,),
    ).fetchone()[0]


def test_a_run_indexes_the_supplied_standard_deduction(
    test_data_dir: Path, tmp_path: Path, fund_asset_savings: Callable[..., None],
    write_tax: Callable[..., Path],
) -> None:
    con = _run(tmp_path, test_data_dir, fund_asset_savings, write_tax)
    try:
        assert _standard_deduction(con, 2027) == MFJ_2027
        assert MFJ_2027 < _standard_deduction(con, 2030) < MFJ_2027 * 11 // 10
    finally:
        con.close()


def test_a_table_without_the_column_holds_flat(
    test_data_dir: Path, tmp_path: Path, fund_asset_savings: Callable[..., None],
    write_tax: Callable[..., Path],
) -> None:
    (test_data_dir / "aux" / "standard_deduction.csv").write_text(
        "effective_date,filing_status,amount\n"
        "2026-01-31,MFJ,31500.00\n2026-01-31,Single,15750.00\n2026-01-31,MFS,15750.00\n"
        "2027-01-31,MFJ,32200.00\n2027-01-31,Single,16100.00\n2027-01-31,MFS,16100.00\n",
        encoding="utf-8",
    )
    con = _run(tmp_path, test_data_dir, fund_asset_savings, write_tax)
    try:
        assert _standard_deduction(con, 2030) == MFJ_2027
    finally:
        con.close()


def test_a_growth_series_must_exist(
    test_data_dir: Path, tmp_path: Path, fund_asset_savings: Callable[..., None],
    write_tax: Callable[..., Path],
) -> None:
    (test_data_dir / "aux" / "standard_deduction.csv").write_text(
        "effective_date,filing_status,amount,amount_series\n"
        "2027-01-31,MFJ,32200.00,no_such_series\n",
        encoding="utf-8",
    )
    with pytest.raises(LoadError, match="amount_series 'no_such_series' doesn't exist"):
        _run(tmp_path, test_data_dir, fund_asset_savings, write_tax)


def test_a_growth_series_must_have_a_rate_from_the_tables_date(
    test_data_dir: Path, tmp_path: Path, fund_asset_savings: Callable[..., None],
    write_tax: Callable[..., Path],
) -> None:
    """Growing 2027's table into 2028 reads the series from February 2027."""
    (test_data_dir / "aux" / "inflation__late.csv").write_text(
        "effective_date,annual_rate\n2028-01-31,3%\n", encoding="utf-8")
    (test_data_dir / "aux" / "standard_deduction.csv").write_text(
        "effective_date,filing_status,amount,amount_series\n"
        "2026-01-31,MFJ,31500.00,\n2027-01-31,MFJ,32200.00,late\n",
        encoding="utf-8",
    )
    with pytest.raises(LoadError, match="amount_series 'late' has no rate until 2028-01, "
                                        "but grows the table from 2027-02"):
        _run(tmp_path, test_data_dir, fund_asset_savings, write_tax)



def test_a_calculators_own_figure_grows_like_a_tables() -> None:
    """``TaxYear.grown`` (#568): from January of the year the figure was set
    to January of the tax year, unrounded; an unknown series is a TaxError."""
    cache: dict = {}
    factor = engine._cumulative_inflation_factor(
        _cpi_2_5(), "cpi", date(2025, 1, 31), date(2030, 1, 31), cache)
    assert engine._grown_amount(_cpi_2_5(), 1_425_00, "cpi", 2025, 2030, cache) \
        == 1_425_00 * factor // 1_000_000 > 1_425_00
    with pytest.raises(engine.tax_api.TaxError, match="no inflation series 'wages'"):
        engine._grown_amount(_cpi_2_5(), 1_425_00, "wages", 2025, 2030, cache)
